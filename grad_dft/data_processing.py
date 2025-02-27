# Copyright 2023 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import warnings
import numpy as np
from tqdm import tqdm
import pandas as pd
from pyscf import gto
from pyscf.data.elements import ELEMENTS, CONFIGURATION

from grad_dft import (
    saver as save,
    molecule_from_pyscf,
    make_reaction,
)
from grad_dft.utils.types import Hartree2kcalmol
from grad_dft.interface.pyscf import process_mol

dirpath = os.path.dirname(os.path.dirname(__file__))
data_dir = "data/"
data_path = os.path.join(dirpath, data_dir)

# Select the configuration here
basis = "def2-tzvp"  # This basis is available for all elements up to atomic number 86
grid_level = 2
omegas = (
    []
)  # [0., 0.4] # This indicates the values of omega in the range-separated exact-exchange.
# It is relatively memory intensive. omega = 0 is the usual Coulomb kernel.
# Leave empty if no Coulomb kernel is expected.

max_electrons = 20  # Select the largest number of electrons we allow for W4-17


def process_dimers(
    training=True, combine=False, max_cycle=None, xc_functional="b3lyp", omegas=[]
):
    r"""Generates the HDF5 files corresponding to the dimers of the XND dataset.

    Parameters
    ----------
    training : bool
        Whether to generate the training or evaluation dataset.
        If training, the electronic density will be the converged energy density
        generated by xc_functional. If evaluation, the electronic density will be
        an initial guess.
    combine : bool
        Whether to return the list of molecules to combine them with another dataset
        or save them in a file.
    max_cycle : int
        The maximum number of SCF iterations to perform to converge the electronic densities.
        If None, PySCF default will be used.
    xc_functional : str
        The PySCF exchange-correlation functional name to use to generate the training dataset.
    omegas : list
        The values of omega in the range-separated exact-exchange.

    Returns
    -------
    None or list of Molecules

    Notes
    -----
    This function is not as extensively tested as it can be substantially adapted by the user,
    for example depending on whether transition metal dimers should be excluded.
    """
    # We first read the excel file
    dataset_file = os.path.join(dirpath, data_dir, "raw/XND_dataset.xlsx")
    dimers_df = pd.read_excel(
        dataset_file, header=0, index_col=None, sheet_name="Dimers"
    )
    atoms_df = pd.read_excel(dataset_file, header=0, index_col=0, sheet_name="Atoms")

    # We will save three files: one with one transition metal (tm) dimers, another with non-tm dimers
    # and finally one with none
    molecules = []
    tm_molecules = []
    non_tm_molecules = []

    tms = ["Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn"]

    for i in tqdm(range(len(dimers_df.index)), desc="Processing dimers"):
        # Extract the molecule information
        atom1 = dimers_df["Atom1"][i]
        atom2 = dimers_df["Atom2"][i]

        if np.isnan(
            dimers_df["Energy (Hartrees) experimental from dissociation, D0"][i]
        ) or np.isnan(dimers_df["Zero-point energy correction"][i]):
            print(
                f"The dissociation energy or zero point correction of dimer {atom1}-{atom2} is not available"
            )
            continue
        else:
            dissociation_e = float(
                dimers_df["Energy (Hartrees) experimental from dissociation, D0"][i]
            )
            zero_e = -float(dimers_df["Zero-point energy correction"][i])
            atom1_e = float(atoms_df["ccsd(t)/cbs energy 3-point"][atom1])
            atom2_e = float(atoms_df["ccsd(t)/cbs energy 3-point"][atom2])
            energy = dissociation_e - zero_e + atom1_e + atom2_e

        spin = int(dimers_df["Multiplicity"][i]) - 1
        charge = 0
        bond_length = float(dimers_df["Bond distance (A)"][i])

        geometry = [[atom1, [0, 0, 0]], [atom2, [bond_length, 0, 0]]]

        # Create a mol and molecule
        mol = gto.M(atom=geometry, basis=basis, charge=charge, spin=spin)
        _, mf = process_mol(
            mol,
            compute_energy=False,
            grid_level=grid_level,
            training=training,
            max_cycle=max_cycle,
            xc_functional=xc_functional,
        )
        if max_cycle:
            energy = mf.e_tot
        molecule = molecule_from_pyscf(
            mf,
            name=atom1 + atom2,
            energy=energy,
            scf_iteration=max_cycle,
            omegas=omegas,
        )

        # Attach the molecule to the list of molecules
        if atom1 in tms or atom2 in tms:
            tm_molecules.append(molecule)
        else:
            non_tm_molecules.append(molecule)
        molecules.append(molecule)

    # If combine, then we return the lists
    if not combine:
        if training:
            data_folder = os.path.join(data_path, "training/")
        else:
            data_folder = os.path.join(data_path, "evaluation/")

        data_file = os.path.join(data_folder, "dimers/tm_dimers.h5")
        # save(molecules = tm_molecules, fname = data_file)

        data_file = os.path.join(data_folder, "dimers/non_tm_dimers.h5")
        # save(molecules = non_tm_molecules, fname = data_file)

        data_file = os.path.join(data_folder, f"dimers/dimers_{xc_functional}.h5")
        save(molecules=molecules, fname=data_file)

    else:
        return molecules, tm_molecules, non_tm_molecules


def process_atoms(training=True, combine=False, max_cycle=None, noise=0, omegas=[]):
    r"""
    Generates the HDF5 files corresponding to the atoms of the XND dataset.

    Parameters
    ----------
    training : bool
        Whether to generate the training or evaluation dataset.
        If training, the electronic density will be the converged energy density
        generated by xc_functional. If evaluation, the electronic density will be
        an initial guess.
    combine : bool
        Whether to return the list of molecules to combine them with another dataset
        or save them in a file.
    max_cycle : int
        The maximum number of SCF iterations to perform to converge the electronic densities.
        If None, PySCF default will be used.
    noise : float
        The standard deviation of the noise to add to the energy of the atoms.
    omegas : list
        The values of omega in the range-separated exact-exchange.

    Returns
    -------
    None or list of Molecules

    Notes
    -----
    This function is not as extensively tested as it can be substantially adapted by the user,
    for example depending on whether transition metal atoms should be excluded.

    """
    # We first read the excel file of the dataset
    dataset_file = os.path.join(dirpath, data_dir, "raw/XND_dataset.xlsx")
    atoms_df = pd.read_excel(dataset_file, header=0, index_col=0, sheet_name="Atoms")

    charge = 0
    molecules = []

    for i in tqdm(range(1, 37), desc="Processing atoms"):
        # Extract the molecule information
        atom = ELEMENTS[i]
        energy = float(atoms_df["ccsd(t)/cbs energy 3-point"][atom]) + np.random.normal(
            0, noise
        )
        spin = compute_spin_element(atom)
        geometry = [[atom, [0, 0, 0]]]

        # Create the Molecule object
        mol = gto.M(atom=geometry, basis=basis, charge=charge, spin=spin)
        _, mf = process_mol(
            mol,
            compute_energy=False,
            grid_level=grid_level,
            training=training,
            max_cycle=max_cycle,
        )
        if max_cycle:
            energy = mf.e_tot
        molecule = molecule_from_pyscf(
            mf, name=atom, energy=energy, scf_iteration=max_cycle, omegas=omegas
        )
        molecules.append(molecule)

    # Save or return the list of molecules
    if not combine:
        if training:
            data_folder = os.path.join(data_path, "training/")
        else:
            data_folder = os.path.join(data_path, "evaluation/")

        data_file = os.path.join(data_folder, "atoms/atoms.h5")
        save(molecules=molecules, fname=data_file)

    else:
        return molecules


def process_dissociation(
    atom1="H",
    atom2="H",
    spin=0,
    file="H2_dissociation.xlsx",
    training=True,
    combine=False,
    max_cycle=None,
    training_distances=None,
    noise=0,
    energy_column_name="energy (Ha)",
):
    r"""
    Generates the HDF5 files corresponding to the dissociation curves of the XND dataset.

    Parameters
    ----------
    atom1 : str
        The symbol of the first atom.
    atom2 : str
        The symbol of the second atom.
    spin : int
        The spin of the molecule.
    file : str
        The name of the excel file containing the dissociation curve.
    training : bool
        Whether to generate the training or evaluation dataset.
        If training, the electronic density will be the converged energy density
        generated by xc_functional. If evaluation, the electronic density will be
        an initial guess.
    combine : bool
        Whether to return the list of molecules to combine them with another dataset
        or save them in a file.
    max_cycle : int
        The maximum number of SCF iterations to perform to converge the electronic densities.
        If None, PySCF default will be used.
    training_distances : list
        The list of distances to use for the training dataset.
        If None, all distances will be used.
    noise : float
        The standard deviation of the noise to add to the energy of the atoms.
    energy_column_name : str
        The name of the column containing the dissociation energy in the excel file.
    omegas : list
        The values of omega in the range-separated exact-exchange.

    Returns
    -------
    None or list of Molecules

    Notes
    -----
    This function is not as extensively tested as it can be substantially adapted by the user.

    """
    # read file data/raw/dissociation/H2_dissociation.xlsx
    dissociation_file = os.path.join(dirpath, data_dir, "raw/dissociation/", file)
    dissociation_df = pd.read_excel(dissociation_file, header=0, index_col=0)

    molecules = []
    if training_distances is None:
        distances = dissociation_df.index
    else:
        distances = training_distances

    for i in tqdm(distances):
        # Extract the molecule information
        d = [dis for dis in dissociation_df.index if np.isclose(i, dis)][0]
        try:
            energy = dissociation_df.loc[d, energy_column_name] + np.random.normal(
                loc=0, scale=noise
            )
        except:
            warnings.warn(f"No dissociation energy data for distance {d}")
        geometry = [[atom1, [0, 0, 0]], [atom2, [0, 0, d]]]
        mol = gto.M(atom=geometry, basis=basis, charge=0, spin=spin)

        # Create a mol and molecule
        _, mf = process_mol(
            mol,
            compute_energy=False,
            grid_level=grid_level,
            training=training,
            max_cycle=max_cycle,
        )
        if max_cycle:
            energy = mf.e_tot
        name = "_".join([atom1, atom2, str(d)])
        molecule = molecule_from_pyscf(
            mf, name=name, energy=energy, scf_iteration=max_cycle, omegas=omegas
        )
        molecules.append(molecule)

    # Save or return the list of molecules
    if not combine:
        if training:
            data_folder = os.path.join(data_path, "training/")
        else:
            data_folder = os.path.join(data_path, "evaluation/")
        data_file = os.path.join(data_folder, "dissociation/", file[:-5] + ".h5")
        save(molecules=molecules, fname=data_file)
    else:
        return molecules


def process_w4x17(training=True, combine=False, max_cycle=None):
    """
    Processes the W4-17 dataset.
    A diverse and high-confidence dataset of atomization energies for benchmarking high-level electronic structure methods
    CCSD(T)/cc-pV(Q+d)Z optimized geometries
    """

    raw_dir = os.path.join(dirpath, data_dir, "raw/W4-17/")
    reactions = []

    for file in tqdm([f for f in os.listdir(raw_dir) if "W4-17" not in f]):
        print("Processing file", file)

        product_numbers = []
        products = []
        reactant_numbers = []
        reactants = []

        # Read information from raw data files
        with open(os.path.normpath(raw_dir + "/" + file), "r") as xyz:
            N = int(xyz.readline())
            # if N > config_variables["max_atoms"]: continue
            header = xyz.readline()
            charge = int(header.split()[0][-1])
            if charge != 0:
                warnings.warn("Charge is not 0 in file {}".format(file))
                continue
            multiplicity = int(header.split()[1][-1])
            geometry = []
            total_electrons = 0
            for line in xyz:
                atom, x, y, z = line.split()
                total_electrons += ELEMENTS.index(atom)
                geometry.append([atom, float(x), float(y), float(z)])
            if total_electrons > max_electrons:
                print(f"reaction {file[:-4]} has too many electrons, {total_electrons}")
                continue

        # Product: processing and creating associated molecule
        reactname = file[:-4]
        mol = gto.M(
            atom=geometry,
            unit="Angstrom",
            basis=basis,
            charge=charge,
            spin=multiplicity - 1,
        )
        product_numbers.append(1)
        _, mf = process_mol(
            mol,
            compute_energy=False,
            grid_level=grid_level,
            training=training,
            max_cycle=max_cycle,
        )
        product = molecule_from_pyscf(mf, name=reactname, omegas=omegas)
        products.append(product)

        # Reading out the energy
        reaction_energy = 0
        with open(os.path.normpath(raw_dir + "/W4-17_Ref_TAE0.txt"), "r") as txt:
            lines = txt.readlines()
            for line in lines[5:206]:
                molecule_name = file.replace(".xyz", "").replace("mr_", "")
                if molecule_name in line:
                    reaction_energy = -float(line.split()[1])
                    reaction_energy /= Hartree2kcalmol
                    break
        assert reaction_energy != 0

        # Reactants: processing and creating associated molecules
        atom_symbols = {}
        for atom in mol.atom:
            if atom[0] in atom_symbols.keys():
                atom_symbols[atom[0]] += 1
            else:
                atom_symbols[atom[0]] = 1

        n_electrons = 0
        for atom in atom_symbols.keys():
            geometry = [[atom, 0.0, 0.0, 0.0]]
            mol = gto.M(
                atom=geometry, basis=basis, symmetry=1, spin=compute_spin_element(atom)
            )
            _, mf = process_mol(
                mol,
                compute_energy=False,
                grid_level=grid_level,
                training=training,
                max_cycle=max_cycle,
            )
            reactant = molecule_from_pyscf(mf, name=atom, omegas=omegas)
            reactants.append(reactant)
            reactant_numbers.append(atom_symbols[atom])

            n_electrons += mf.mol.nelectron * atom_symbols[atom]

        # Make reaction
        reaction = make_reaction(
            reactants,
            products,
            reactant_numbers,
            product_numbers,
            reaction_energy,
            name=reactname,
        )
        reactname = reactname + "_" + str(n_electrons)
        reactions.append(reaction)

    # Saving the reaction
    if not combine:
        if training:
            data_folder = os.path.join(data_path, "training/")
        else:
            data_folder = os.path.join(data_path, "evaluation/")
        data_file = os.path.join(data_folder, "W4-17/", "W4-17.h5")
        save(reactions=reactions, fname=data_file)
    else:
        return reactions


########### Auxiliary functions #############
def compute_spin_element(atom):
    r"""
    Computes the spin of an atom from its electronic configuration.
    """
    i = ELEMENTS.index(atom)
    configuration = CONFIGURATION[
        i
    ]  # This indicates the electronic configuration of a given atom

    spin = configuration[0] % 2
    spin += min(configuration[1] % 6, 6 - configuration[1] % 6)
    spin += min(configuration[2] % 10, 10 - configuration[2] % 10)
    spin += min(configuration[3] % 14, 14 - configuration[3] % 14)

    return spin
