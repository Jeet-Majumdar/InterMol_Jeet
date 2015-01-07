import os
import logging
import pdb
import warnings
import numpy as np
import re


import simtk.unit as units
from intermol.atom import Atom

from intermol.forces import *
import intermol.forces.forcefunctions as ff
from intermol.molecule import Molecule
from intermol.moleculetype import MoleculeType
from intermol.system import System

logger = logging.getLogger('InterMolLog')


def load_lammps(in_file):
    """Load a LAMMPS input file into a `System`.

    Args:
        in_file:
        include_dir:
        defines:
    Returns:
        system:
    """
    parser = LammpsParser(in_file)
    return parser.read()


def write_lammps(in_file, system, unit_set='real'):
    """Load a LAMMPS input file into a `System`.

    Args:
        in_file:
        include_dir:
        defines:
    Returns:
        system:
    """
    parser = LammpsParser(in_file, system, unit_set)
    return parser.write()


class LammpsParser(object):
    """A class containing methods to read and write LAMMPS files."""
    SCALE_INTO = 2.0
    SCALE_FROM = 0.5

    lammps_bonds = {
        'harmonic': HarmonicBond,
        'morse': MorseBond,
        'class2': QuarticBond,
        'fene/expand': FeneExpandableBond,
        'quartic': QuarticBreakableBond,
        'nonlinear': NonlinearBond
        }
    lookup_lammps_bonds = {v: k for k, v in lammps_bonds.items()}
    # Add some non 1-to-1 mappings.
    lookup_lammps_bonds[HarmonicPotentialBond] = 'harmonic'
    lammps_bond_types = dict(
        (k, eval(v.__name__ + 'Type')) for k, v in lammps_bonds.items())

    def canonical_bond(self, kwds, bond, direction='into'):
        """Convert to/from the canonical form of this interaction. """
        # TODO: Gromacs says harmonic potential bonds do not have constraints or
        #       exclusions. Check that this logic is supported.
        if direction == 'into':
            canonical_force_scale = self.SCALE_INTO
        else:
            typename = self.lookup_lammps_bonds[bond]
            canonical_force_scale = self.SCALE_FROM

        if bond in [HarmonicBond, HarmonicPotentialBond]:
            kwds['k'] *= canonical_force_scale

        if bond == HarmonicPotentialBond:
            typename = 'harmonic'

        if direction == 'into':
            return bond, kwds
        else:
            return typename, [kwds]  # we expect a list

    lammps_angles = {
        'harmonic': HarmonicAngle,
        'cosine': CosineAngle,
        'charmm': UreyBradleyAngle
        }
    lookup_lammps_angles = dict((v, k) for k, v in lammps_angles.items())
    lammps_angle_types = dict(
        (k, eval(v.__name__ + 'Type')) for k, v in lammps_angles.items())

    def canonical_angle(self, kwds, angle, direction):
        """Convert from the canonical form of this interaction. """
        if direction == 'into':
            canonical_force_scale = self.SCALE_INTO
        else:
            typename = self.lookup_lammps_angles[angle]
            canonical_force_scale = self.SCALE_FROM

        if angle in [HarmonicAngle, CosineSquaredAngle, UreyBradleyAngle]:
            kwds['k'] *= canonical_force_scale

        if angle == UreyBradleyAngle:
            kwds['kUB'] *= canonical_force_scale

        if direction == 'into':
            return angle, kwds
        else:
            return typename, [kwds]  # We expect a list

    lammps_dihedrals = {
        'opls': FourierDihedral,
        'multi/harmonic': RbDihedral,
        'charmm': ProperPeriodicDihedral,
        # not quite canonical form, but easily interconvertible
        }
    # Have to manually reverse dihedrals -- not unique.
    lookup_lammps_dihedrals = {
        TrigDihedral: 'Trig',
        RbDihedral: 'multi/harmonic',
        FourierDihedral: 'opls',
        ProperPeriodicDihedral: 'charmm'
        # not quite canonical form, but easily interconvertible
        }
    lammps_dihedral_types = dict(
        (k, eval(v.__name__ + 'Type')) for k, v in lammps_dihedrals.items())

    lammps_impropers = {
        'harmonic': ImproperHarmonicDihedral,
        'cvff': TrigDihedral
        }
    lookup_lammps_impropers = dict((v, k) for k, v in lammps_impropers.items())
    lammps_improper_types = dict(
        (k, eval(v.__name__ + 'Type')) for k, v in lammps_impropers.items())

    def canonical_dihedral(self, params, dihedral, direction='into'):
        """Convert from the canonical form of this interaction. """
        if direction == 'into':
            canonical_force_scale = self.SCALE_INTO
        else:
            canonical_force_scale = self.SCALE_FROM

        if direction == 'into':
            converted_dihedral = dihedral  # Default
            if dihedral == ProperPeriodicDihedral:  # Proper dihedral
                convertfunc = convert_dihedral_from_proper_to_trig
                converted_dihedral = TrigDihedral
            elif dihedral == ImproperHarmonicDihedral:
                convertfunc = convert_nothing
            elif dihedral == RbDihedral:
                convertfunc = convert_dihedral_from_RB_to_trig
                converted_dihedral = TrigDihedral
            elif dihedral == FourierDihedralType:
                convertfunc = convert_dihedral_from_fourier_to_trig
                converted_dihedral = TrigDihedral
                # Now actually convert the dihedral.
            params = convertfunc(params)
            return converted_dihedral, params

        else:  # writing out
            try:
                typename = self.lookup_lammps_dihedrals[dihedral]
            except KeyError:
                typename = self.lookup_lammps_impropers[dihedral]

            if dihedral == TrigDihedral:
                paramlist = convert_dihedral_from_trig_to_proper(params)
                if params['phi'].value_in_unit(units.degrees) in [0, 180]:
                    tmpparams = convert_dihedral_from_trig_to_RB(params)
                    if tmpparams['C6']._value == 0 and tmpparams['C5']._value == 0:
                        if params['phi'].value_in_unit(
                                units.degrees) == 180:  # stupid convention?
                            params['phi']._value = 0
                        else:
                            params['phi']._value = 180
                        tmpparams = convert_dihedral_from_trig_to_RB(params)
                        typename = 'multi/harmonic'
                        # is a rb dihedral done analyzing
                        paramlist = [tmpparams]
                    else:
                        typename = 'charmm'
                        # if C6 and C5 is not zero, then we have to print it out as multiple harmonic
                if typename in ['charmm', 'Trig']:
                    # print as proper dihedral; if one nonzero term, as a type 1, if multiple, type 9
                    paramlist = convert_dihedral_from_trig_to_proper(params)
                    typename = 'charmm'
                    for p in paramlist:
                        p['weight'] = 0.0 * units.dimensionless  # for now, might get from Sys?

            elif dihedral == ImproperHarmonicDihedral:
                params['k'] *= canonical_force_scale
                paramlist = [params]

            return typename, paramlist

    def create_kwds_from_entries(self, entries, force_class, offset=0):
        return ff.create_kwds_from_entries(self.unitvars, self.paramlist,
                entries, force_class, offset=offset)

    def get_parameter_list_from_force(self, force):
        return ff.get_parameter_list_from_force(force, self.paramlist)

    def get_parameter_kwds_from_force(self, force):
        return ff.get_parameter_kwds_from_force(
                force, self.get_parameter_list_from_force, self.paramlist)

    def __init__(self, in_file, system=None, unit_set='real'):
        """
        """
        self.in_file = in_file
        if not system:
            system = System()
        self.system = system
        self.data_file = None

    def set_units(self, unit_set):
        """Set what unit set to use."""

        self.RAD = units.radians
        self.DEGREE = units.degrees
        self.MOLE = units.mole
        self.TEMP = units.kelvin
        if unit_set == 'real':
            self.DIST = units.angstroms
            self.VEL = units.angstroms / units.femtosecond
            self.ENERGY = units.kilocalorie / units.mole
            self.MASS = units.grams / units.mole
            self.CHARGE = units.elementary_charge
        elif unit_set == 'metal':
            self.DIST = units.angstroms
            self.VEL = units.angstroms / units.picosecond
            self.ENERGY = units.joule / units.coulomb * units.elementary_charge
            self.MASS = units.grams / units.mole
            self.CHARGE = units.elementary_charge
        elif unit_set == 'si':
            self.DIST = units.meters
            self.VEL = units.meters / units.second
            self.ENERGY = units.joules
            self.MASS = units.kilograms
            self.CHARGE = units.coulomb
        elif unit_set == 'cgs':
            self.DIST = units.centimeter
            self.VEL = units.centimeter / units.second
            self.ENERGY = units.erg
            self.MASS = units.grams
            self.CHARGE = np.sqrt(units.erg * units.centimeter)
        elif unit_set == 'micro':
            self.DIST = units.micrometers
            self.VEL = units.nanometers / units.nanosecond
            self.ENERGY = units.picogram * (
                units.micrometer / units.microsecond) ^ 2
            self.MASS = units.attograms
            self.CHARGE = units.elementary_charge
        elif unit_set == 'nano':
            self.DIST = units.nanometers
            self.VEL = units.nanometer / units.nanosecond
            self.ENERGY = units.attogram * (
                units.nanometer / units.nanosecond) ^ 2
            self.MASS = units.attograms
            self.CHARGE = units.elementary_charge
        elif unit_set == 'lj':
            self.DIST = units.dimensionless
            self.VEL = units.dimensionless
            self.ENERGY = units.dimensionless
            self.MASS = units.dimensionless
            self.CHARGE = units.dimensionless
            logger.warn("Using unit type lj: All values are dimensionless. "
                        "This is untested and will likely fail. "
                        "See LAMMPS doc for more.")
        elif unit_set == 'electron':
            self.DIST = units.bohr
            self.VEL = units.bohr / units.atu
            self.ENERGY = units.hartree
            self.MASS = units.amu
            self.CHARGE = units.elementary_charge
        else:
            raise Exception(
                "Unsupported unit set specified: {0}".format(unit_set))

        # Now create the dictionary of which units go in which order
        # for each command.  we need to pass 'self' so that we can
        # access the different unit sets, but the function unitvars is
        # not actually a member, so we have to do it in a nonstandard way.
        self.paramlist = ff.build_paramlist('lammps')
        self.unitvars = ff.build_unitvars('lammps', self.paramlist, dumself=self)

    def read(self):
        """Reads a LAMMPS input file and a data file specified within.

        Args:
            input_file (str): Name of LAMMPS input file to read in.
        """
        self.read_input(self.in_file)
        if self.data_file:
            self.read_data(self.data_file)
        else:
            raise Exception("No data file found in input script")

    def read_input(self):
        """Reads a LAMMPS input file.

        Args:
            input_file (str): Name of LAMMPS input file to read in.
        """
        self.input_dir = os.path.dirname(os.path.realpath(self.in_file))
        parsable_keywords = {
            'units': self.parse_units,
            'atom_style': self.parse_atom_style,
            'dimension': self.parse_dimension,
            'boundary': self.parse_boundary,
            'pair_style': self.parse_pair_style,
            'kspace_style': self.parse_kspace_style,
            'pair_modify': self.parse_pair_modify,
            'bond_style': self.parse_bond_style,
            'angle_style': self.parse_angle_style,
            'dihedral_style': self.parse_dihedral_style,
            'improper_style': self.parse_improper_style,
            'special_bonds': self.parse_special_bonds,
            'read_data': self.parse_read_data}

        defaults = [
            'units lj',
            'atom_style atomic',
            'dimension 3',
            'boundary p p p',
            'pair_style none',
            'kspace_style none',
            'pair_modify mix geometric shift no table 12 tabinner sqrt(2.0) tail no compute yes',
            'bond_style none',
            'angle_style none',
            'dihedral_style none',
            'improper_style none',
            'special_bonds lj 0.0 0.0 0.0 coul 0.0 0.0 0.0 angle no dihedral no extra 0']

        keyword_defaults = {x.split()[0]: x for x in defaults}
        keyword_check = {x: False for x in keyword_defaults.keys()}

        with open(self.in_file, 'r') as input_lines:
            for line in input_lines:
                if line.strip():
                    keyword = line.split()[0]
                    if keyword in parsable_keywords:
                        parsable_keywords[keyword](line.split())
            keyword_check[keyword] = True

        for key in keyword_check.keys():
            if not (keyword_check[key]):
                logger.warn(
                    'Keyword {0} not set, using LAMMPS default value {1}'.format(
                        key, " ".join(keyword_defaults[key].split()[1:])))
                parsable_keywords[key](keyword_defaults[key].split())

        self.set_units(self.unit_set)

    def read_data(self, data_file):
        """Reads a LAMMPS data file.

        Args:
            data_file (str): name of LAMMPS data file to read in.
        """
        # Read box, masses and forcefield info from data file.
        parsable_keywords = {'Masses': self.parse_masses,
                             'Pair Coeffs': self.parse_pair_coeffs,
                             'Bond Coeffs': self.parse_bond_coeffs,
                             'Angle Coeffs': self.parse_angle_coeffs,
                             'Dihedral Coeffs': self.parse_dihedral_coeffs,
                             'Improper Coeffs': self.parse_improper_coeffs}

        with open(data_file, 'r') as data_lines:
            self.molecule_name = next(data_lines).strip()
            # Currently only reading a single molecule/moleculeType
            # per LAMMPS file.
            self.current_mol = Molecule(self.molecule_name)
            self.system.add_molecule(self.current_mol)
            self.current_mol_type = self.system._molecules[self.molecule_name]
            self.current_mol_type.nrexcl = 3  # TODO: automate determination!

            for line in data_lines:
                if line.strip():
                    # Catch all box dimensions.
                    if ('xlo' in line) and ('xhi' in line):
                        self.parse_box(line.split(), 0)
                    elif ('ylo' in line) and ('yhi' in line):
                        self.parse_box(line.split(), 1)
                    elif ('zlo' in line) and ('zhi' in line):
                        self.parse_box(line.split(), 2)
                    # Other headers.
                    else:
                        keyword = line.strip()
                        if keyword in parsable_keywords:
                            parsable_keywords[keyword](data_lines)

        # Read atoms, velocities and connectivity information from data file.
        parsable_keywords = {'Atoms': self.parse_atoms,
                             'Velocities': self.parse_velocities,
                             'Bonds': self.parse_bonds,
                             'Angles': self.parse_angles,
                             'Dihedrals': self.parse_dihedrals,
                             'Impropers': self.parse_impropers}

        with open(data_file, 'r') as data_lines:
            for line in data_lines:
                if line.strip():
                    keyword = line.strip()
                    if keyword in parsable_keywords:
                        parsable_keywords[keyword](data_lines)

    def parse_units(self, line):
        """ """
        assert (len(line) == 2), "Invalid units specified in input file."
        self.unit_set = line[1]

    def parse_atom_style(self, line):
        """
        Note:
            Assuming 'full' as default for everything else.
        """
        self.atom_style = line[1]
        if len(line) > 2:
            warnings.warn("Unsupported atom_style in input file.")

    def parse_dimension(self, line):
        """ """
        self.dimension = int(line[1])
        if self.dimension not in [2, 3]:
            raise ValueError("Invalid dimension specified in input file "
                             "(must be 2 or 3).")

    def parse_boundary(self, line):
        """ """
        self.boundaries = [line[1], line[2], line[3]]
        if len(self.boundaries) != self.dimension:
            raise ValueError("Boundaries do not match specified dimension "
                             "in input file")

    def parse_pair_style(self, line):
        """ """
        self.pair_style = []
        if line[1] == 'hybrid':
            warnings.warn("Hybrid pair styles not yet implemented.")
        elif line[1] == 'lj/cut/coul/long':
            self.pair_style.append(line[1])
            self.system.nonbonded_function = 1

    def parse_kspace_style(self, line):
        """
        Note:
            Currently ignored.
        """
        if line[1] == 'pppm':
            pass

    def parse_pair_modify(self, line):
        """
        """
        if line[1] == 'mix':
            if line[2] == 'geometric':
                self.system.combination_rule = 'Multiply-Sigeps'
            elif line[2] == 'arithmetic':
                self.system.combination_rule = 'Lorentz-Berthelot'
            else:
                warnings.warn(
                    "Unsupported pair_modify mix argument in input file!")
        else:
            warnings.warn("Unsupported pair_modify style in input file!")

    def parse_bonded_style(self, line):
        """ """
        style_set = set()
        if len(line) == 2:
            style_set.add(line[1])
        elif line[1] == 'hybrid':
            for style in line[2:]:
                style_set.add(style)
        else:
            raise ValueError("Invalid style in input file!")
        return style_set

    def parse_bond_style(self, line):
        """ """
        self.bond_style = self.parse_bonded_style(line)

    def parse_angle_style(self, line):
        """ """
        self.angle_style = self.parse_bonded_style(line)

    def parse_dihedral_style(self, line):
        """ """
        self.dihedral_style = self.parse_bonded_style(line)
        # TODO: correctly determine gen-pairs state
        if self.dihedral_style == 'opls':
            self.system.genpairs = 'yes'

    def parse_improper_style(self, line):
        """ """
        self.improper_style = self.parse_bonded_style(line)

    def parse_special_bonds(self, line):
        """ """
        if 'lj/coul' in line:
            self.system.lj_correction = float(line[line.index('lj/coul') + 3])
            self.system.coulomb_correction = float(
                line[line.index('lj/coul') + 3])
        elif 'lj' in line and 'coul' in line:
            self.system.lj_correction = float(line[line.index('lj') + 3])
            self.system.coulomb_correction = float(line[line.index('coul') + 3])
        elif 'lj' in line:
            self.system.lj_correction = float(line[line.index('lj') + 3])
        elif 'coul' in line:
            self.system.coulomb_correction = float(line[line.index('coul') + 3])
        else:
            warnings.warn("Unsupported special_bonds in input file.")

    def parse_read_data(self, line):
        """ """
        if len(line) == 2:
            self.data_file = os.path.join(self.input_dir, line[1])
        else:
            warnings.warn("Unsupported read_data arguments in input file.")

    def parse_box(self, line, dim):
        """Read box information from data file.

        Args:
            line (str): Current line in input file.
            dim (int): Dimension specified in line.
        """
        fields = [float(field) for field in line[:2]]
        box_length = fields[1] - fields[0]
        if box_length > 0:
            self.box_vector[dim, dim] = box_length
        else:
            raise ValueError("Negative box length specified in data file.")
        self.system.box_vector = self.box_vector * self.DIST

    def parse_masses(self, data_lines):
        """Read masses from data file."""
        next(data_lines)  # toss out blank line
        self.mass_dict = dict()
        for line in data_lines:
            if not line.strip():
                break  # found another blank line
            fields = line.split()
            self.mass_dict[int(fields[0])] = float(fields[1]) * self.MASS

    def parse_pair_coeffs(self, data_lines):
        """Read pair coefficients from data file."""
        next(data_lines)  # toss out blank line
        self.nb_types = dict()
        for line in data_lines:
            if not line.strip():
                break  # found another blank line
            fields = [float(field) for field in line.split()]
            if len(self.pair_style) == 1:
                # TODO: lookup of type of pairstyle to determine format
                if self.system.nonbonded_function == 1:
                    self.nb_types[int(fields[0])] = [fields[1] * self.ENERGY,
                                                     fields[2] * self.DIST]
                else:
                    warnings.warn(
                        "Unsupported pair coeff formatting in data file!")
            else:
                warnings.warn("Unsupported pair coeff formatting in data file!")

    def parse_force_coeffs(self, data_lines, force_name, force_classes,
                           force_style, lammps_forces, canonical_force):
        """Read force coefficients from data file."""
        next(data_lines)  # toss out blank line

        for line in data_lines:
            if not line.strip():
                break  # found another blank line
            fields = line.split()

            warn = False
            if len(force_style) == 1:
                style = list(force_style)[0]  # awkward to have to translate to list to get the only member!
                if style == fields[1]:
                    field_offset = 2
                else:
                    if re.search('[a-zA-Z]+', fields[1]):
                        if style == 'none':
                            style = fields[1]
                            field_offset = 2
                        else:
                            warn = True
                    else:
                        field_offset = 1

            elif len(force_style) > 1:
                style = fields[1]
                field_offset = 2
                if style not in force_style:
                    warn = True
            else:
                raise ValueError(
                    "No entries found in '%s_style'." % (force_name))

            if warn:
                warnings.warn("{0} type found in {1} Coeffs that was not "
                              "specified in {2}_style: {3}".format(
                                    force_name, force_name, force_name, style))

            # what internal force correspond to this style
            force_class = lammps_forces[style]

            # Get the parameters from the line and translate into keywords
            kwds = self.create_kwds_from_entries(fields, force_class,
                                                 offset=field_offset)
            # translate the force into canonical form
            force_class, kwds = canonical_force(kwds, force_class,
                                                direction='into')
            # add to the dictionary of this force term
            force_classes[int(fields[0])] = [force_class, kwds]

    def parse_bond_coeffs(self, data_lines):

        self.bond_classes = dict()
        self.parse_force_coeffs(data_lines, "Bond",
                                self.bond_classes, self.bond_style,
                                self.lammps_bonds, self.canonical_bond)

    def parse_angle_coeffs(self, data_lines):

        self.angle_classes = dict()
        self.parse_force_coeffs(data_lines, "Angle",
                                self.angle_classes, self.angle_style,
                                self.lammps_angles, self.canonical_angle)

    def parse_dihedral_coeffs(self, data_lines):

        self.dihedral_classes = dict()
        self.parse_force_coeffs(data_lines, "Dihedral",
                                self.dihedral_classes, self.dihedral_style,
                                self.lammps_dihedrals, self.canonical_dihedral)

    def parse_improper_coeffs(self, data_lines):

        self.improper_classes = dict()
        self.parse_force_coeffs(data_lines, "Improper",
                                self.improper_classes, self.improper_style,
                                self.lammps_impropers, self.canonical_improper)

    def parse_atoms(self, data_lines):
        """Read atoms from data file."""
        next(data_lines)  # toss out blank line
        for line in data_lines:
            if not line.strip():
                break  # found another blank line
            fields = line.split()

            if len(fields) in [7, 10]:
                if len(fields) == 10:
                    # TODO: store image flags?
                    pass
                new_atom_type = None
                if self.system.combination_rule == "Multiply-C6C12":
                    warnings.warn(
                        "Combination rule 'Multiply-C6C12' not yet implemented")
                elif self.system.combination_rule in ['Multiply-Sigeps',
                                                      'Lorentz-Berthelot']:
                    new_atom_type = AtomSigepsType(
                        fields[2],  # atomtype
                        fields[2],  # bondtype
                        -1,  # atomic_number
                        self.mass_dict[int(fields[2])],  # mass
                        float(fields[3]) * self.CHARGE,  # charge
                        'A',  # ptype
                        self.nb_types[int(fields[2])][1],  # sigma
                        self.nb_types[int(fields[2])][0])  # epsilon

                self.system._atomtypes.add(new_atom_type)

                atom = Atom(int(fields[0]),  # index
                            fields[2],  # name
                            int(fields[1]),  # residue_index (molNum)
                            fields[1])  # residue_name (molNum)
                atom.atomtype = (0, fields[2])  # atomNum for LAMMPS
                atom.atomic_number = 0  #TODO: this must be defined for Desmond output; can we get this from LAMMPS?
                atom.cgnr = 0  # TODO: look into alternatives
                atom.charge = (0, float(fields[3]) * self.CHARGE)
                atom.mass = (0, self.mass_dict[int(fields[2])])
                atom.position = [float(fields[4]) * self.DIST,
                                 float(fields[5]) * self.DIST,
                                 float(fields[6]) * self.DIST]

                # Probably unneccessary since I don't think LAMMPS has anything
                # in the data files akin to A/B states in GROMACS.
                for ab_state, atom_type in enumerate(atom.atomtype):
                    # Searching for a matching atom_type
                    temp = AbstractAtomType(atom.atomtype[ab_state])
                    atom_type = self.system._atomtypes.get(temp)
                    if atom_type:
                        atom.sigma = (ab_state, atom_type.sigma)
                        atom.epsilon = (ab_state, atom_type.epsilon)
                        atom.bondtype = atom_type.bondtype
                    else:
                        warnings.warn("Corresponding AtomType was not found. "
                                      "Insert missing values yourself.")
            self.current_mol.add_atom(atom)

    def parse_velocities(self, data_lines):
        """ """
        next(data_lines)
        atoms = self.current_mol.atoms
        vel_dict = dict()
        for line in data_lines:
            if not line.strip():
                break
            fields = [field for field in line.split()]
            vel_dict[int(fields[0])] = fields[1:4]
        for atom in atoms:
            atom._velocity = [float(vel) * self.VEL for vel in
                              vel_dict[atom.index]]

    def parse_force(self, data_lines, force_classes, forceSet, n=0):
        """Read bonds, angles, dihedrals, impropers from data file."""
        next(data_lines)  # toss out blank line
        for line in data_lines:
            if not line.strip():
                break  # found another blank line
            fields = [int(field) for field in line.split()]

            new_force = None
            coeff_num = fields[1]
            atom_nums = fields[2:n + 2]
            paraminfo = force_classes[coeff_num]
            kwds = paraminfo[1]
            new_force = paraminfo[0](*atom_nums, **kwds)
            forceSet.add(new_force)

    def parse_bonds(self, data_lines):
        self.parse_force(data_lines, self.bond_classes,
                         self.current_mol_type.bondForceSet, n=2)

    def parse_angles(self, data_lines):
        self.parse_force(data_lines, self.angle_classes,
                         self.current_mol_type.angleForceSet, n=3)

    def parse_dihedrals(self, data_lines):
        self.parse_force(data_lines, self.dihedral_classes,
                         self.current_mol_type.dihedralForceSet, n=4)

    def parse_impropers(self, data_lines):
        self.parse_force(data_lines, self.improper_classes,
                         self.current_mol_type.dihedralForceSet, n=4)

    def get_force_atoms(self, force, forceclass):
        """Return the atoms involved in a force. """
        if forceclass in ['Bond', 'Pair']:
            return [force.atom1, force.atom2]
        elif forceclass in ['Angle']:
            return [force.atom1, force.atom2, force.atom3]
        elif forceclass in ['Dihedral', 'Improper']:
            return [force.atom1, force.atom2, force.atom3, force.atom4]
        else:
            warnings.warn("No interaction type %s defined!" % (forceclass))

    def write_forces(self, molecule, offset, force_name, lookup_lammps_force,
                     lammps_force_types, canonical_force, forces):
        """The general force writing function.

        Currently supports bonds, angles, dihedrals, impropers.
        """
        logger.debug("        Writing {0:s}s...".format(force_name))
        count = 1
        ilist = list()
        ilist.append('\n{0:s}s\n\n'.format(force_name))
        coeffs = list()
        coeffs.append('\n{0:s} Coeffs\n\n'.format(force_name))

        style_list = set()

        type_dict = dict()  # typeObject:int_type
        type_i = 1  # counter for bond types

        for force in forces:
            atoms = self.get_force_atoms(force, force_name)  # Atoms in force.
            #atomtypes = map(lambda atom: molecule.atoms[atom - 1].bondtype, atoms)
            atomtypes = [molecule.atoms[atom - 1] for atom in atoms]
            try:
                lookup_lammps_force[force.__class__]
            except KeyError:
                warnings.warn("Found unimplemented %s type %s for LAMMPS!" % (
                    force_name, force.__class__.__name__))

            # Get the parameters of the force.
            kwds = self.get_parameter_kwds_from_force(force)

            # Convert keywords from canonical form.
            style, kwdslist = canonical_force(kwds, force.__class__,
                                              direction='from')
            force_type = lammps_force_types[style]

            # A single force can produce multiple forces.
            for kwds in kwdslist:
                temp_force_type = force_type(*atomtypes, **kwds)
                # Keep track of which types we've seen so far.

                if temp_force_type not in type_dict:
                    # New type found. Write out the force coefficients.

                    # Get the numerical type for this interaction.
                    type_dict[temp_force_type] = type_i
                    line = ('{0:d} {1}').format(type_i, style)

                    # Generate the list of parameters for this force in the
                    # order they appear in the file format.
                    params = self.get_parameter_list_from_force(temp_force_type)

                    # Generate the units for this force.
                    u = self.unitvars[force_type.__name__]
                    for i, p in enumerate(params):
                        if p.unit == units.dimensionless and isinstance(p._value, int):
                            # LAMMPS expects an integer.
                            line += "%10d" % (p.value_in_unit(u[i]))
                        else:
                            line += "%18.8e" % (p.value_in_unit(u[i]))
                    line += '\n'
                    coeffs.append(line)
                    type_i += 1

                # Write out which forces correspond to which coefficents: "Bonds or Angles"
                # Print the interaction number.
                line = '{0:-6d} {1:6d}'.format(count, type_dict[temp_force_type])
                for atom in atoms:
                    line += '{0:6d}'.format(atom + offset)
                line += '\n'
                ilist.append(line)
                style_list.add(style)
                count += 1

        if len(style_list) > 1:
            logger.warn("More than one %s style found!" % (force_name))

        return style_list, coeffs, ilist

    def write_bonds(self, mol_type, molecule, offset):
        return self.write_forces(molecule, offset, "Bond",
                                 self.lookup_lammps_bonds,
                                 self.lammps_bond_types,
                                 self.canonical_bond, mol_type.bond_forces)

    def write_angles(self, mol_type, molecule, offset):
        return self.write_forces(molecule, offset, "Angle",
                                 self.lookup_lammps_angles,
                                 self.lammps_angle_types,
                                 self.canonical_angle, mol_type.angle_forces)

    def write_dihedrals(self, mol_type, molecule, offset):
        """Separate dihedrals from impropers. """
        dihedral_forces = dict()
        for force in mol_type.dihedral_forces:
            if not force.improper:
                dihedral_forces[force] = force

        return self.write_forces(molecule, offset, "Dihedral",
                                 self.lookup_lammps_dihedrals,
                                 self.lammps_dihedral_types,
                                 self.canonical_dihedral, dihedral_forces)

    def write_impropers(self, mol_type, molecule, offset):
        """Separate dihedrals from impropers. """
        improper_forces = dict()
        for force in mol_type.dihedral_forces:
            if force.improper:
                improper_forces[force] = force

        return self.write_forces(molecule, offset, "Improper",
                                 self.lookup_lammps_impropers,
                                 self.lammps_improper_types,
                                 self.canonical_dihedral, improper_forces)

    def write_virtuals(self, mol_type, molecule, offset):
        if len(mol_type.virtual_forces) > 0:
            warnings.warn(
                "Virtuals not currently supported: will need to be implemeneted from shake and rigid")

    def write(self, unit_set='real'):
        """Writes a LAMMPS data and corresponding input file.

        Args:
            data_file (str): Name of LAMMPS data file to write to.
            unit_set (str): LAMMPS unit set for output file.
        """
        self.data_file = os.path.splitext(self.in_file)[0] + '.lmp'
        self.set_units(unit_set)
        # Containers for lines which are ultimately written to output files.
        mass_list = list()
        mass_list.append('\nMasses\n\n')

        pair_coeffs = list()
        pair_coeffs.append('\nPair Coeffs\n\n')

        atom_list = list()
        atom_list.append('\nAtoms\n\n')

        vel_list = list()
        vel_list.append('\nVelocities\n\n')

        # Dicts for type information.
        atom_type_dict = dict()  # str_type:int_type
        a_type_i = 1  # counter for atom types

        # Read all atom specific and FF information.
        for mol_name, mol_type in self.system.molecule_types.iteritems():
            logger.debug(
                "    Writing moleculetype {0}...".format(mol_name))

            # Atom index offsets from 1 for each molecule.
            offsets = [0]
            for molecule in mol_type.molecules:
                offsets.append(len(molecule.atoms) + offsets[-1])
            offsets.pop()  # We don't actually want to iterate over the last one

            # OrderedSet isn't indexable so get the first molecule by iterating.
            molecule = next(iter(mol_type.molecules))
            atoms = molecule.atoms
            for offset in offsets:
                bond_styles, bond_coeffs, bond_list = self.write_bonds(
                        mol_type, molecule, offset)
                angle_styles, angle_coeffs, angle_list = self.write_angles(
                        mol_type, molecule, offset)
                dihedral_styles, dihedral_coeffs, dihedral_list = self.write_dihedrals(
                        mol_type, molecule, offset)
                improper_styles, improper_coeffs, improper_list = self.write_impropers(
                        mol_type, molecule, offset)
                # Only issues warning now
                self.write_virtuals(mol_type, molecule, offset)

            # atom specific information
            x_min = y_min = z_min = np.inf
            logger.debug("    Writing atoms...")
            cumulative_atoms = 0
            atom_charges = False
            for molecule in mol_type.molecules:
                for atom in molecule.atoms:
                    # type, mass and pair coeffs
                    if atom.atomtype[0] not in atom_type_dict:
                        atom_type_dict[atom.atomtype[0]] = a_type_i
                        mass_list.append('{0:d} {1:8.4f}\n'.format(
                            a_type_i, atom._mass[0].in_units_of(self.MASS)._value))
                        pair_coeffs.append('{0:d} {1:8.4f} {2:8.4f}\n'.format(
                            a_type_i,
                            atom.epsilon[0].in_units_of(self.ENERGY)._value,
                            atom.sigma[0].in_units_of(self.DIST)._value))
                        a_type_i += 1

                    # box minima
                    x_coord = atom._position[0].in_units_of(self.DIST)._value
                    y_coord = atom._position[1].in_units_of(self.DIST)._value
                    z_coord = atom._position[2].in_units_of(self.DIST)._value
                    if x_coord < x_min:
                        x_min = x_coord
                    if y_coord < y_min:
                        y_min = y_coord
                    if z_coord < z_min:
                        z_min = z_coord

                    # atom
                    atom_list.append(
                        '{0:-6d} {1:-6d} {2:-6d} {3:5.8f} {4:12.6f} {5:12.6f} {6:12.6f}\n'.format(
                            atom.index + cumulative_atoms,
                            atom.residue_index,
                            atom_type_dict[atom.atomtype[0]],
                            atom._charge[0].in_units_of(self.CHARGE)._value,
                            x_coord,
                            y_coord,
                            z_coord))
                    # velocity
                    if atom._charge[0]._value != 0:
                        atom_charges = True
                    if atom._velocity:
                        vel_list.append(
                            '{0:-6d} {1:8.4f} {2:8.4f} {3:8.4f}\n'.format(
                                atom.index + cumulative_atoms,
                                atom._velocity[0].in_units_of(self.VEL)._value,
                                atom._velocity[1].in_units_of(self.VEL)._value,
                                atom._velocity[2].in_units_of(self.VEL)._value))
                    else:
                        vel_list.append(
                            '{0:-6d} {1:8.4f} {2:8.4f} {3:8.4f}\n'.format(
                                atom.index + cumulative_atoms, 0, 0, 0))
                cumulative_atoms += len(molecule.atoms)

                # Write the actual data file.
        with open(self.data_file, 'w') as f:
            # front matter
            f.write(self.system.name + '\n')
            f.write('\n')

            n_atoms = len(atom_list) - 1
            n_bonds = len(bond_list) - 1
            n_angles = len(angle_list) - 1
            n_dihedrals = len(dihedral_list) - 1
            n_impropers = len(improper_list) - 1

            n_atom_types = len(pair_coeffs) - 1
            n_bond_types = len(bond_coeffs) - 1
            n_angle_types = len(angle_coeffs) - 1
            n_dihedral_types = len(dihedral_coeffs) - 1
            n_improper_types = len(improper_coeffs) - 1

            f.write('{0} atoms\n'.format(n_atoms))
            f.write('{0} bonds\n'.format(n_bonds))
            f.write('{0} angles\n'.format(n_angles))
            f.write('{0} dihedrals\n'.format(n_dihedrals))
            f.write('{0} impropers\n'.format(n_impropers))
            f.write('\n')

            f.write('{0} atom types\n'.format(n_atom_types))
            if n_bond_types > 0:
                f.write('{0} bond types\n'.format(n_bond_types))
            if n_angle_types > 0:
                f.write('{0} angle types\n'.format(n_angle_types))
            if n_dihedral_types > 0:
                f.write('{0} dihedral types\n'.format(n_dihedral_types))
            if n_improper_types > 0:
                f.write('{0} improper types\n'.format(n_improper_types))
            f.write('\n')

            # shifting of box dimensions
            f.write('{0:10.6f} {1:10.6f} xlo xhi\n'.format(
                    x_min, x_min + self.system.box_vector[0][0].value_in_units(
                            self.DIST)))
            f.write('{0:10.6f} {1:10.6f} ylo yhi\n'.format(
                    y_min, y_min + self.system.box_vector[1][1].value_in_units(
                            self.DIST)))
            f.write('{0:10.6f} {1:10.6f} zlo zhi\n'.format(
                    z_min, z_min + self.system.box_vector[2][2].value_in_units(
                            self.DIST)))

            # masses
            for mass in mass_list:
                f.write(mass)

            # print forcefield coefficients
            coeffs = [pair_coeffs, bond_coeffs, angle_coeffs, dihedral_coeffs,
                      improper_coeffs]
            for coeff in coeffs:
                if len(coeff) > 1:
                    for c in coeff:
                        f.write(c)

            # atoms and velocities
            for atom in atom_list:
                f.write(atom)
            for vel in vel_list:
                f.write(vel)

            # print force list
            force_lists = [bond_list, angle_list, dihedral_list, improper_list]
            for force_list in force_lists:
                if len(force_list) > 1:
                    for force in force_list:
                        f.write(force)

        # Write the corresponding input file.
        with open(self.in_file, 'w') as f:
            f.write('units {0}\n'.format(unit_set))
            f.write('atom_style full\n')  # TODO
            f.write('\n')

            f.write('dimension 3\n')  # TODO
            f.write('boundary p p p\n')  # TODO
            f.write('\n')

            # non-bonded
            if atom_charges:
                f.write('pair_style lj/cut/coul/long 15.0 15.0\n')  # TODO: match mdp
                #f.write('pair_style lj/cut/coul/long 9.999 9.999\n')
                f.write('kspace_style pppm 1.0e-8\n')  # TODO: match mdp
                #f.write('kspace_style ewald 1.0e-6\n')
            else:
                f.write('pair_style lj/cut/coul/cut 15.0 15.0\n')  # TODO: match mdp
                #f.write('pair_style lj/cut/coul/cut 9.999 9.999\n')
                f.write('kspace_style none\n')  # if there are no charges

            if self.system.combination_rule == 'Lorentz-Berthelot':
                f.write('pair_modify mix arithmetic\n')
            elif self.system.combination_rule == 'Multiply-Sigeps':
                f.write('pair_modify mix geometric\n')
            else:
                logger.warn("Unsupported pair combination rule on writing input file!")
            f.write('\n')

            # bonded
            if len(bond_coeffs) > 1:
                f.write('bond_style hybrid {0}\n'.format(
                    " ".join(bond_styles)))
            if len(angle_coeffs) > 1:
                f.write('angle_style hybrid {0}\n'.format(
                    " ".join(angle_styles)))
            if len(dihedral_coeffs) > 1:
                f.write('dihedral_style hybrid {0}\n'.format(
                    " ".join(dihedral_styles)))
            if len(improper_coeffs) > 1:
                f.write('improper_style hybrid {0}\n'.format(
                    " ".join(improper_styles)))

            f.write('special_bonds lj {0} {1} {2} coul {3} {4} {5}\n'.format(
                0.0, 0.0, self.system.lj_correction,
                0.0, 0.0, self.system.coulomb_correction))
            f.write('\n')

            # read data
            f.write('read_data {0}\n'.format(os.path.basename(self.data_file)))
            f.write('\n')

            # output energies
            energy_terms = " ".join(['ebond',
                                     'eangle',
                                     'edihed',
                                     'eimp',
                                     'epair',
                                     'evdwl',
                                     'ecoul',
                                     'elong',
                                     'etail',
                                     'pe'])

            f.write('thermo_style custom {0}\n'.format(energy_terms))
            f.write('\n')

            f.write('run 0\n')
