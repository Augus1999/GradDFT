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
from random import shuffle
from typing import Optional, Tuple, Union, Sequence, Dict
from itertools import chain, combinations_with_replacement, product

import h5py
import numpy as np
from jax import numpy as jnp
from jax.lax import Precision
from jax import vmap
from jax.tree_util import tree_map
from jaxtyping import Array, Scalar

import pyscf.data.elements as elements
from pyscf import dft
from pyscf.pbc import df
from pyscf.dft import Grids, numint
from pyscf.pbc.dft import numint as pbc_numint
from pyscf.gto import Mole
from pyscf.pbc.gto.cell import Cell
from pyscf.pbc.lib.kpts import KPoints
from pyscf.pbc.df.fft import FFTDF
from pyscf.pbc.df.mdf import MDF
from pyscf.pbc.df.df import GDF
from pyscf.ao2mo import restore

from grad_dft.molecule import Grid, Molecule, Reaction, make_reaction
from grad_dft.solid import Solid, KPointInfo
from grad_dft.utils import DType, default_dtype, DensityFunctional, HartreeFock
from grad_dft.external import _nu_chunk


def grid_from_pyscf(grids: Grids, dtype: Optional[DType] = None) -> Grid:
    if grids.coords is None:
        grids.build()

    coords, weights = to_device_arrays(grids.coords, grids.weights, dtype=dtype)

    return Grid(coords, weights)


def kpt_info_from_pyscf(kmf: DensityFunctional):
    kpts = kmf.kpts
    if isinstance(kpts, KPoints):
        msg = """PySCF KPoint object detected. Symmetry adapted calculations are not yet possible. Please ensure
        that the supplied k-points to the PySCF Molecule object have space_group_symmetry=False and time_reversal_symmetry=False.
        """
        raise NotImplementedError(msg)
        # 1BZ single k-points: kinetic + external terms
        kpts_abs = kpts.kpts
        kpts_scaled = kpts.kpts_scaled
        weights = kpts.weights_ibz
        bz2ibz_map = kpts.bz2ibz
        ibz2bz_map = kpts.ibz2bz
        kpts_ir_abs = kpts.kpts_ibz
        kpts_ir_scaled = kpts.kpts_scaled_ibz
    else:
        # No symmetries used

        # Equal weights

        # bz2ibz_map = None
        # ibz2bz_map = None
        # kpts_ir_abs = None
        # kpts_ir_scaled = None
        kpts_abs, kpts_scaled, weights = to_device_arrays(
            kpts,
            kmf.cell.get_scaled_kpts(kpts),
            np.ones(shape=(kpts.shape[0],)) / kpts.shape[0],
            dtype=None,
        )
    return KPointInfo(
        kpts_abs,
        kpts_scaled,
        weights,
        # bz2ibz_map,
        # ibz2bz_map,
        # kpts_ir_abs,
        # kpts_ir_scaled,
    )


def molecule_from_pyscf(
    mf: DensityFunctional,
    dtype: Optional[DType] = None,
    omegas: Optional[Array] = None,
    energy: Optional[Scalar] = None,
    name: Optional[Array] = None,
    scf_iteration: Scalar = jnp.int32(50),
    chunk_size: Optional[Scalar] = jnp.int32(1024),
    grad_order: Optional[Scalar] = jnp.int32(2),
) -> Molecule:
    if hasattr(mf, "kpts"):
        if not np.array_equal(mf.kpts, np.array([[0.0, 0.0, 0.0]])):
            raise RuntimeError(
                "Input was periodic with BZ sampling beyond gamma-point only. Use solid_from_pyscf instead."
            )
    # mf, grids = _maybe_run_kernel(mf, grids)
    grid = grid_from_pyscf(mf.grids, dtype=dtype)

    (
        ao,
        grad_ao,
        grad_n_ao,
        rdm1,
        energy_nuc,
        h1e,
        vj,
        mo_coeff,
        mo_energy,
        mo_occ,
        mf_e_tot,
        s1e,
        fock,
        rep_tensor,
        kpt_info,
    ) = to_device_arrays(
        *_package_outputs(mf, mf.grids, scf_iteration, grad_order), dtype=dtype
    )

    atom_index, nuclear_pos = to_device_arrays(
        [elements.ELEMENTS.index(e) for e in mf.mol.elements],
        mf.mol.atom_coords(unit="bohr"),
        dtype=dtype,
    )

    basis = jnp.array(
        [ord(char) for char in mf.mol.basis]
    )  # jax doesn't support strings, so we convert it to integers
    unit_Angstrom = True
    if name:
        name = jnp.array([ord(char) for char in name])

    if omegas is not None:
        chi = generate_chi_tensor(
            rdm1=rdm1,
            ao=ao,
            grid_coords=grid.coords,
            mol=mf.mol,
            omegas=omegas,
            chunk_size=chunk_size,
        )
        # chi = to_device_arrays(chi, dtype=dtype)
        # omegas = to_device_arrays(omegas, dtype=dtype)
    else:
        chi = None

    spin = jnp.int32(mf.mol.spin)
    charge = jnp.int32(mf.mol.charge)
    if isinstance(
        mf.grids, Grids
    ):  # check if it's the open boundary grid. Otherwise we have a uniform grid with no level
        grid_level = jnp.int32(mf.grids.level)
    else:
        grid_level = None

    return Molecule(
        grid,
        atom_index,
        nuclear_pos,
        ao,
        grad_ao,
        grad_n_ao,
        rdm1,
        energy_nuc,
        h1e,
        vj,
        mo_coeff,
        mo_occ,
        mo_energy,
        mf_e_tot,
        s1e,
        omegas,
        chi,
        rep_tensor,
        energy,
        basis,
        name,
        spin,
        charge,
        unit_Angstrom,
        grid_level,
        scf_iteration,
        fock,
    )


def solid_from_pyscf(
    kmf: DensityFunctional,
    dtype: Optional[DType] = None,
    omegas: Optional[Array] = None,
    energy: Optional[Scalar] = None,
    name: Optional[Array] = None,
    scf_iteration: Scalar = jnp.int32(50),
    chunk_size: Optional[Scalar] = jnp.int32(1024),
    grad_order: Optional[Scalar] = jnp.int32(2),
) -> Solid:
    if np.array_equal(kmf.kpts, np.array([[0.0, 0.0, 0.0]])):
        raise RuntimeError("Use molecule_from_pyscf for Gamma point only calculations")
    elif not hasattr(kmf, "cell"):
        raise RuntimeError(
            "Input was an isolated system. Use molecule_from_pyscf instead."
        )

    grid = grid_from_pyscf(kmf.grids, dtype=dtype)
    pyscf_dat = _package_outputs(kmf, kmf.grids, scf_iteration, grad_order)
    kpt_info = pyscf_dat[-1]
    (
        ao,
        grad_ao,
        grad_n_ao,
        rdm1,
        energy_nuc,
        h1e,
        vj,
        mo_coeff,
        mo_energy,
        mo_occ,
        mf_e_tot,
        s1e,
        fock,
        rep_tensor,
    ) = to_device_arrays(*pyscf_dat[0:-1], dtype=dtype)

    atom_index, nuclear_pos = to_device_arrays(
        [elements.ELEMENTS.index(e) for e in kmf.mol.elements],
        kmf.mol.atom_coords(unit="bohr"),
        dtype=dtype,
    )

    basis = jnp.array(
        [ord(char) for char in kmf.mol.basis]
    )  # jax doesn't support strings, so we convert it to integers
    unit_Angstrom = True
    if name:
        name = jnp.array([ord(char) for char in name])

    if omegas is not None:
        chi = generate_chi_tensor(
            rdm1=rdm1,
            ao=ao,
            grid_coords=grid.coords,
            mol=kmf.mol,
            omegas=omegas,
            chunk_size=chunk_size,
        )
        # chi = to_device_arrays(chi, dtype=dtype)
        # omegas = to_device_arrays(omegas, dtype=dtype)
    else:
        chi = None

    spin = jnp.int32(kmf.mol.spin)
    charge = jnp.int32(kmf.mol.charge)
    if isinstance(
        kmf.grids, Grids
    ):  # check if it's the open boundary grid. Otherwise we have a uniform grid with no level
        grid_level = jnp.int32(kmf.grids.level)
    else:
        grid_level = None
    lattice_vectors = kmf.cell.lattice_vectors()
    return Solid(
        grid,
        kpt_info,
        atom_index,
        lattice_vectors,
        nuclear_pos,
        ao,
        grad_ao,
        grad_n_ao,
        rdm1,
        energy_nuc,
        h1e,
        vj,
        mo_coeff,
        mo_occ,
        mo_energy,
        mf_e_tot,
        s1e,
        omegas,
        chi,
        rep_tensor,
        energy,
        basis,
        name,
        spin,
        charge,
        unit_Angstrom,
        grid_level,
        scf_iteration,
        fock,
    )


def mol_from_Molecule(molecule: Molecule):
    r"""Converts a Molecule object to a PySCF Mole object.
    WARNING: the mol returned is not the same as the orginal mol used to create the Molecule object.
    """

    mol = Mole()

    charges = np.asarray(molecule.atom_index)
    positions = np.asarray(molecule.nuclear_pos)

    mol.atom = [[int(charge), pos] for charge, pos in zip(charges, positions)]
    mol.basis = "".join(
        chr(num) for num in molecule.basis
    )  # The basis will generally be encoded as a jax array of ints
    mol.unit = "angstrom" if molecule.unit_Angstrom else "bohr"

    mol.spin = int(molecule.spin)
    mol.charge = int(molecule.charge)

    mol.build()

    return mol


# @partial(jax.jit, static_argnames=["kernel_fn", "chunk_size", "precision"])
def saver(
    fname: str,
    reactions: Optional[Union[Reaction, Sequence[Reaction]]] = (),
    molecules: Optional[Union[Molecule, Sequence[Molecule]]] = (),
):
    r"""
    Saves the molecule data to a file, and computes and saves the corresponding chi

    Parameters
    ----------
    fname : str
        Name of the file to save the chi object to.
    omegas : Union[Scalar, Sequence[Scalar]], optional
        Range-separation parameter. A value of 0 disables range-separation
        (i.e. uses the kernel v(r,r') = 1/|r-r'| instead of
        v(r,r') = erf(\omega |r-r'|) / |r-r'|)
        If multiple omegas are given, the chi object is calculated for each omega
        and concatenated along the last axis.
    reactions : Union[Reaction, Sequence[Reaction]]
        Reaction object(s) to calculate the chi object for.
    molecules : Union[Molecule, Sequence[Molecule]]
        Molecule object(s) to calculate the chi object for.

    Notes
    -----
    chi: Array
        $$\chi_{bd}(r) = \Gamma_{ac} \psi_a \int dr' (\chi_b(r') v(r, r') \chi_d(r'))$$,
        used to compute chi_a objects in equation S4 in DM21 paper, and save it to a file.
        Uses the extenal _nu_chunk function from the original DM21 paper, in the _hf_density.py file.
        chi will have dimensions (n_grid_points, n_omegas, n_spin, n_orbitals)

    nu: Array
        The density matrix, with dimensions (n_grid_points, n_spin, n_orbitals, n_orbitals)
        $$nu = \int dr' (\chi_b(r') v(r, r') \chi_d(r'))$$

    Saves
    -------
    In hdf5 format, the following datasets are saved:
        |- Reaction (attributes: energy)
            |- Molecule  (attributes: reactant/product, reactant/product_numbers)
                |- All the attributes in Molecule class
                |- chi (attributes: omegas)
        |- Molecule (attributes: energy)
            |- All the attributes in Molecule class
            |- chi (attributes: omegas)


    Raises:
    -------
        ValueError: if omega is negative.
        TypeError: if molecules is not a Molecule or Sequence of Molecules; if reactions is not a Reaction or Sequence of Reactions.
    """

    #######

    fname = fname.replace(".hdf5", "").replace(".h5", "")

    if isinstance(molecules, Molecule):
        molecules = (molecules,)
    if isinstance(reactions, Reaction):
        reactions = (reactions,)

    with h5py.File(f"{fname}.hdf5", "a") as file:
        # First we save the reactions
        for i, reaction in enumerate(reactions):
            if reaction.name:
                react = file.create_group(f"reaction_{reaction.name}_{i}")
            else:
                react = file.create_group(f"reaction_{i}")
            react["energy"] = reaction.energy

            for j, molecule in enumerate(
                list(chain(reaction.reactants, reaction.products))
            ):
                if molecule.name is not None:
                    mol_group = react.create_group(
                        "molecule_"
                        + f"".join(chr(num) for num in molecule.name)
                        + f"_{j}"
                    )
                else:
                    mol_group = react.create_group(f"molecule_{j}")
                save_molecule_data(mol_group, molecule)
                if j < len(reaction.reactants):
                    mol_group.attrs["type"] = "reactant"
                    mol_group["reactant_numbers"] = reaction.reactant_numbers[j]
                else:
                    mol_group.attrs["type"] = "product"
                    mol_group["product_numbers"] = reaction.product_numbers[
                        j - len(reaction.reactant_numbers)
                    ]

        # Then we save the molecules
        for j, molecule in enumerate(molecules):
            if molecule.name is not None:
                mol_group = file.create_group(
                    "molecule_" + f"".join(chr(num) for num in molecule.name) + f"_{j}"
                )
            else:
                mol_group = file.create_group(f"molecule_{j}")
            save_molecule_data(mol_group, molecule)


def loader(
    fname: str,
    randomize: Optional[bool] = True,
    training: Optional[bool] = True,
    config_omegas: Optional[Union[Scalar, Sequence[Scalar]]] = None,
):
    r"""
    Reads the molecule, energy and precomputed chi matrix from a file.

    Parameters
    ----------
    fname : str
        Name of the file to read the fxx matrix from.
    key : PRNGKeyArray
        Key to use for randomization of the order of elements output.
    randomize : bool, optional
        Whether to randomize the order of elements output, by default False
    training : bool, optional
        Whether we are training or not, by default True
    omegas : Union[Scalar, Sequence[Scalar]], optional
        Range-separation parameter. Use to select the chi matrix to load, by default None

    Yields
    -------
    type: str
        Whether it is a Molecule or Reaction.
    molecule/reaction : Molecule or Reaction
        The molecule or reaction object.

    todo: randomize input
    """

    fname = fname.replace(".hdf5", "").replace(".h5", "")

    with h5py.File(os.path.normpath(f"{fname}.hdf5"), "r") as file:
        items = list(file.items())  # List of tuples
        if randomize and training:
            shuffle(items)

        for grp_name, group in items:
            if "molecule" in grp_name:
                args = {}
                for key, value in group.items():
                    if key in ["name", "basis"]:
                        args[key] = jnp.array(
                            [ord(char) for char in str(value[()])], dtype=jnp.int64
                        )
                    elif key in ["energy"]:
                        args[key] = jnp.float64(value)
                    elif key in ["scf_iteration", "spin", "charge"]:
                        args[key] = jnp.int64(value)
                    elif key in ["grad_n_ao"]:
                        args[key] = {
                            int(k): jnp.asarray(v, dtype=jnp.float64)
                            for k, v in value.items()
                        }
                    elif key == "chi":
                        # select the indices from the omegas array and load the corresponding chi matrix
                        if config_omegas is None:
                            args[key] = jnp.asarray(value)
                        elif list(config_omegas) == []:
                            args[key] = None
                        else:
                            omegas = list(group["omegas"])
                            if isinstance(omegas, (int, float)):
                                omegas = (omegas,)
                            assert all(
                                [omega in omegas for omega in config_omegas]
                            ), f"chi tensors for omega list {config_omegas} were not all precomputed in the molecule"
                            indices = [omegas.index(omega) for omega in config_omegas]
                            args[key] = jnp.stack(
                                [
                                    jnp.asarray(value, dtype=jnp.float64)[:, i]
                                    for i in indices
                                ],
                                axis=1,
                            )
                    else:
                        args[key] = jnp.asarray(value, dtype=jnp.float64)

                for key, value in group.attrs.items():
                    if not training:
                        args[key] = str(value)

                grid = Grid(args["coords"], args["weights"])
                del args["coords"], args["weights"]

                molecule = Molecule(grid, **args)

                yield "molecule", molecule

            if "reaction" in grp_name:
                reactants = []
                products = []
                reactant_numbers = []
                product_numbers = []

                if not training:
                    name = jnp.array(
                        [ord(char) for char in str(grp_name.split("_")[1:])]
                    )
                    energy = jnp.float64(group["energy"])
                else:
                    name = None
                    energy = jnp.float64(group["energy"])

                for molecule_name, molecule in group.items():
                    if molecule_name == "energy":
                        continue

                    args = {}
                    if not training:
                        args["name"] = molecule_name.split("_")[1]
                    for key, value in molecule.items():
                        if key in ["reactant_numbers", "product_numbers"]:
                            continue
                        elif key in ["name", "basis"]:
                            args[key] = jnp.array(
                                [ord(char) for char in str(value[()])]
                            )
                        elif key in ["energy"]:
                            args[key] = jnp.float64(value)
                        elif key in ["grad_n_ao"]:
                            args[key] = {
                                int(k): jnp.asarray(v) for k, v in value.items()
                            }
                        elif key == "chi":
                            # select the indices from the omegas array and load the corresponding chi matrix
                            if config_omegas is None:
                                args[key] = jnp.asarray(value)
                            elif config_omegas == []:
                                args[key] = None
                            else:
                                omegas = list(group["omegas"])
                                if isinstance(omegas, (int, float)):
                                    omegas = (omegas,)
                                assert all(
                                    [omega in omegas for omega in config_omegas]
                                ), f"chi tensors for omega list {config_omegas} were not all precomputed in the molecule"
                                indices = [
                                    omegas.index(omega) for omega in config_omegas
                                ]
                                args[key] = jnp.stack(
                                    [jnp.asarray(value)[:, i] for i in indices], axis=1
                                )
                        else:
                            args[key] = jnp.asarray(value)

                    for key, value in molecule.attrs.items():
                        if not training and key not in ["type"]:
                            args[key] = value

                    grid = Grid(args["coords"], args["weights"])
                    del args["coords"], args["weights"]
                    args.pop("reactant_numbers", None)
                    args.pop("product_numbers", None)

                    if molecule.attrs["type"] == "reactant":
                        reactants.append(Molecule(grid, **args))
                        reactant_numbers.append(jnp.int32(molecule["reactant_numbers"]))
                    else:
                        products.append(Molecule(grid, **args))
                        product_numbers.append(jnp.int32(molecule["product_numbers"]))

                reaction = make_reaction(
                    reactants, products, reactant_numbers, product_numbers, energy, name
                )

                yield "reaction", reaction


def save_molecule_data(mol_group: h5py.Group, molecule: Molecule):
    r"""Auxiliary function to save all data except for chi"""

    to_numpy = lambda arr: (
        arr if (isinstance(arr, str) or isinstance(arr, float)) else np.asarray(arr)
    )
    d = tree_map(to_numpy, molecule.to_dict())

    for name, data in d.items():
        if data is None:
            data = jnp.empty([1])
        elif name in ["name", "basis"]:
            mol_group.create_dataset(name, data="".join(chr(num) for num in data))
        elif name == "grad_n_ao":
            d = mol_group.create_group(name)
            for k, v in data.items():
                d.create_dataset(f"{k}", data=v)
        else:
            mol_group.create_dataset(name, data=data)


def save_molecule_chi(
    molecule: Molecule,
    omegas: Union[Sequence[Scalar], Scalar],
    chunk_size: int,
    mol_group: h5py.Group,
    precision: Precision = Precision.HIGHEST,
):
    r"""Auxiliary function to save chi tensor
    Deprecated: the chi tensor is now saved as another molecule property in save_molecule_data
    """

    grid_coords = molecule.grid.coords
    mol = mol_from_Molecule(molecule)

    chunks = (
        (np.ceil(grid_coords.shape[0] / chunk_size).astype(int), 1, 1, 1)
        if chunk_size
        else None
    )
    shape = (
        grid_coords.shape[0],
        len(omegas),
        molecule.rdm1.shape[0],
        molecule.ao.shape[1],
    )
    # Remember that molecule.rdm1.shape[0] represents the spin

    if chunk_size is None:
        chunk_size = grid_coords.shape[0]

    chi = generate_chi_tensor(
        molecule.rdm1,
        molecule.ao,
        molecule.grid.coords,
        mol,
        omegas=omegas,
        precision=precision,
    )

    mol_group.create_dataset(
        f"chi", shape=shape, chunks=chunks, dtype="float64", data=chi
    )
    mol_group.create_dataset(f"omegas", data=omegas)


##############################################################################################################


def to_device_arrays(*arrays, dtype: Optional[DType] = None):
    if dtype is None:
        dtype = default_dtype()

    out = []
    for array in arrays:
        if isinstance(array, dict):
            for k, v in array.items():
                array[k] = jnp.asarray(v)
            out.append(array)
        elif isinstance(array, Scalar):
            out.append(array)
        elif array is None:
            out.append(None)  # or out.append(jnp.nan) ?
        else:
            out.append(jnp.asarray(array))
    # print(out)
    return out


def _maybe_run_kernel(mf: HartreeFock, grids: Optional[Grids] = None):
    if mf.mo_coeff is None:
        # kernel not run yet

        if hasattr(mf, "grids"):  # Is probably DFT
            if grids is not None:
                mf.grids = grids
            elif mf.grids is not None:
                grids = mf.grids
            else:
                raise RuntimeError(
                    "A `Grids` object has to be provided either through `mf` or explicitly!"
                )

        mf.verbose = 0
        mf.kernel()

    return mf, grids


def ao_grads(mol: Mole, coords: Array, order=2) -> Dict:
    r"""Function to compute nth order atomic orbital grads, for n > 1.

    .. math::
            \nabla^n \psi

    Outputs
    ----------
    Dict
    For each order n > 1, result[n] is an array of shape
    (n_grid, n_ao, 3) where the third coordinate indicates
    .. math::
        \frac{\partial^n \psi}{\partial x_i^n}

    for :math:`x_i` is one of the usual cartesian coordinates x, y or z.
    """

    ao_ = numint.eval_ao(mol, coords, deriv=order)
    if order == 0:
        return ao_[0]
    result = {}
    i = 4
    for n in range(2, order + 1):
        result[n] = jnp.empty((ao_[0].shape[0], ao_[0].shape[1], 0))
        for c in combinations_with_replacement("xyz", r=n):
            if len(set(c)) == 1:
                result[n] = jnp.concatenate(
                    (result[n], jnp.expand_dims(ao_[i], axis=2)), axis=2
                )
            i += 1
    return result


def pbc_ao_grads(cell: Cell, coords: Array, order=2, kpts=None) -> Dict:
    r"""Function to compute nth order crystal atomic orbital grads, for n > 1.

    .. math::
            \nabla^n \psi

    Outputs
    ----------
    Dict
    For each order n > 1, result[n] is an array of shape
    (n_kpt, n_grid, n_ao, 3) where the fourth coordinate indicates
    .. math::
        \frac{\partial^n \psi}{\partial x_i^n}

    for :math:`x_i` is one of the usual cartesian coordinates x, y or z.
    """
    if kpts is None:
        # Default is Gamma only
        ao_ = pbc_numint.eval_ao_kpts(cell, coords, kpts=np.zeros(3), deriv=order)
        ao_ = np.asarray(ao_)
        aos = ao_[:, 0, :, :]
        res_shape = (1, aos.shape[1], aos.shape[2], 0)
    else:
        ao_ = pbc_numint.eval_ao_kpts(cell, coords, kpts=kpts, deriv=order)
        ao_ = np.asarray(ao_)
        aos = ao_[:, 0, :, :]
        res_shape = (kpts.shape[0], aos.shape[1], aos.shape[2], 0)
    if order == 0:
        return ao_
    result = {}
    i = 4
    for n in range(2, order + 1):
        result[n] = jnp.empty(res_shape)
        for c in combinations_with_replacement("xyz", r=n):
            if len(set(c)) == 1:
                result[n] = jnp.concatenate(
                    (result[n], jnp.expand_dims(ao_[:, i, :, :], axis=3)), axis=3
                )
            i += 1
    return result


def calc_eri_with_pyscf(mf, kpts=np.zeros(3)) -> np.ndarray:
    r"""Calculate the ERIs using the method detected from the PySCF mean field object.

    Inputs
    ----------

    mf:
        PySCF mean field object
    kpts:
        Array of k-points (absolute, not fractional).

    Outputs
    ----------
    np.ndarray

    The ERIs. Output shape is (nao, nao, nao, nao) for isolated molecules and gamma-point only
    periodic calculations. For full BZ calculations, the output shape is (nkpt, nkpt, nao, nao, nao, nao).
    """
    # Solid or Isolated molecule?
    if hasattr(mf, "cell"):  # Periodic system

        # Check for the three density fitting methods. DF is always used for periodic calculations
        if isinstance(mf.with_df, FFTDF):
            density_fitter = FFTDF(mf.cell, kpts=kpts)
        elif isinstance(
            mf.with_df, MDF
        ):  # Check for MDF before GDF becuase MDF inherits from GDF
            density_fitter = MDF(mf.cell, kpts=kpts)
        elif isinstance(mf.with_df, GDF):
            density_fitter = GDF(mf.cell, kpts=kpts)

        # Calculate the Periodic ERI's.
        if np.array_equal(kpts, np.zeros(3)):
            # Assume Gamma point only
            eri_compressed = density_fitter.get_eri(kpts=np.zeros(3))
            eri = restore(1, eri_compressed, mf.cell.nao_nr())
        else:
            # Loop over all k-pairs. This will be a fall back in the future. We will encourage users
            # to save ERIs to disk after a PySCF calculation.
            nkpt = kpts.shape[0]
            nao = mf.cell.nao_nr()
            # Empty array for all k points in uncompressed format.
            eri = np.empty(shape=(nkpt, nkpt, nao, nao, nao, nao), dtype=np.complex128)
            for ikpt, jkpt in product(range(nkpt), range(nkpt)):
                k_quartet = np.array([kpts[ikpt], kpts[ikpt], kpts[jkpt], kpts[jkpt]])
                eri_kquartet = density_fitter.get_eri(
                    compact=False, kpts=k_quartet
                ).reshape(nao, nao, nao, nao)
                eri[ikpt, jkpt, :, :, :, :] = eri_kquartet

    else:  # Isolated system
        try:
            _ = mf.with_df
        except AttributeError:
            eri = mf.mol.intor("int2e")
            return eri
        # Use default DF method when DF is used on molecules
        density_fitter = df.DF(mf.mol)
        eri_compressed = density_fitter.get_eri()
        eri = restore(1, eri_compressed, mf.mol.nao_nr())
    return eri


def _package_outputs(
    mf: DensityFunctional,
    grids: Optional[Grids] = None,
    scf_iteration: Scalar = jnp.int32(50),
    grad_order: Scalar = jnp.int32(2),
):
    if scf_iteration != 0:
        rdm1 = mf.make_rdm1(mf.mo_coeff, mf.mo_occ)
    else:
        rdm1 = mf.get_init_guess(mf.mol, mf.init_guess)

    # Depending on the shapes of arrays and the type of PySCF mean field object passed,
    # the correct way to process data is now inferred.

    # Restricted (non-spin polarized), open boundary conditions
    if rdm1.ndim == 2 and not hasattr(mf, "cell"):
        ao_and_1deriv = numint.eval_ao(
            mf.mol, grids.coords, deriv=1
        )  # , non0tab=grids.non0tab)
        ao = ao_and_1deriv[0]
        grad_ao = ao_and_1deriv[1:4].transpose(1, 2, 0)
        grad_n_ao = ao_grads(mf.mol, jnp.array(mf.grids.coords), order=grad_order)
        s1e = mf.get_ovlp(mf.mol)
        h1e = mf.get_hcore(mf.mol)
        half_dm = rdm1 / 2
        half_mo_coeff = mf.mo_coeff
        half_mo_energy = mf.mo_energy
        half_mo_occ = mf.mo_occ / 2

        rdm1 = np.stack([half_dm, half_dm], axis=0)
        mo_coeff = np.stack([half_mo_coeff, half_mo_coeff], axis=0)
        mo_energy = np.stack([half_mo_energy, half_mo_energy], axis=0)
        mo_occ = np.stack([half_mo_occ, half_mo_occ], axis=0)
        vj = 2 * mf.get_j(
            mf.mol, rdm1, hermi=1
        )  # The 2 is to compensate for the /2 in the definition of the density matrix
        dm = mf.make_rdm1(mf.mo_coeff, mf.mo_occ)
        fock = np.stack([h1e, h1e], axis=0) + mf.get_veff(mf.mol, dm)
        rep_tensor = calc_eri_with_pyscf(mf)
        kpt_info = None

    # Unrestricted (spin polarized), open boundary conditions
    elif rdm1.ndim == 3 and not hasattr(mf, "cell"):
        ao_and_1deriv = numint.eval_ao(
            mf.mol, grids.coords, deriv=1
        )  # , non0tab=grids.non0tab)
        ao = ao_and_1deriv[0]
        grad_ao = ao_and_1deriv[1:4].transpose(1, 2, 0)
        grad_n_ao = ao_grads(mf.mol, jnp.array(mf.grids.coords), order=grad_order)
        s1e = mf.get_ovlp(mf.mol)
        h1e = mf.get_hcore(mf.mol)
        mo_coeff = np.stack(mf.mo_coeff, axis=0)
        mo_energy = np.stack(mf.mo_energy, axis=0)
        mo_occ = np.stack(mf.mo_occ, axis=0)
        vj = 2 * mf.get_j(
            mf.mol, rdm1, hermi=1
        )  # The 2 is to compensate for the /2 in the definition of the density matrix
        dm = mf.make_rdm1(mf.mo_coeff, mf.mo_occ)
        fock = np.stack([h1e, h1e], axis=0) + mf.get_veff(mf.mol, dm)
        rep_tensor = calc_eri_with_pyscf(mf)
        kpt_info = None

    # Restricted (non-spin polarized), periodic boundary conditions, full BZ sampling
    elif rdm1.ndim == 3 and hasattr(mf, "cell") and rdm1.shape[0] != 1:
        ao_and_1deriv = pbc_numint.eval_ao_kpts(
            mf.cell, grids.coords, kpts=mf.kpts, deriv=1
        )
        ao_and_1deriv = np.asarray(ao_and_1deriv)
        ao = ao_and_1deriv[:, 0, :, :]
        grad_ao = ao_and_1deriv[:, 1:4, :, :].transpose(0, 2, 3, 1)
        grad_n_ao = pbc_ao_grads(
            mf.cell, jnp.array(mf.grids.coords), order=grad_order, kpts=mf.kpts
        )
        # grad_n_ao = ao_grads(mf.mol, jnp.array(mf.grids.coords), order=grad_order)
        s1e = mf.get_ovlp(mf.mol)
        h1e = mf.get_hcore(mf.mol)

        half_dm = rdm1 / 2
        half_mo_coeff = mf.mo_coeff
        half_mo_energy = mf.mo_energy
        half_mo_occ = np.asarray(mf.mo_occ) / 2

        rdm1 = np.stack([half_dm, half_dm], axis=0)
        mo_coeff = np.stack([half_mo_coeff, half_mo_coeff], axis=0)
        mo_energy = np.stack([half_mo_energy, half_mo_energy], axis=0)
        mo_occ = np.stack([half_mo_occ, half_mo_occ], axis=0)

        vj = 2 * mf.get_j(
            mf.mol, rdm1, hermi=1
        )  # The 2 is to compensate for the /2 in the definition of the density matrix
        dm = mf.make_rdm1(mf.mo_coeff, mf.mo_occ)
        fock = np.stack([h1e, h1e], axis=0) + mf.get_veff(mf.mol, dm)

        kpt_info = kpt_info_from_pyscf(mf)
        # Compute ERIs for all pairs of k-points. Needed for Coulomb energy calculation
        rep_tensor = calc_eri_with_pyscf(mf, kpts=mf.kpts)

    # Unrestricted (spin polarized), periodic boundary conditions, full BZ sampling
    elif rdm1.ndim == 4 and hasattr(mf, "cell") and rdm1.shape[1] != 1:

        ao_and_1deriv = pbc_numint.eval_ao_kpts(
            mf.cell, grids.coords, kpts=mf.kpts, deriv=1
        )
        ao_and_1deriv = np.asarray(ao_and_1deriv)
        ao = ao_and_1deriv[:, 0, :, :]
        grad_ao = ao_and_1deriv[:, 1:4, :, :].transpose(0, 2, 3, 1)
        grad_n_ao = pbc_ao_grads(
            mf.cell, jnp.array(mf.grids.coords), order=grad_order, kpts=mf.kpts
        )

        s1e = mf.get_ovlp(mf.mol)
        h1e = mf.get_hcore(mf.mol)
        mo_coeff = np.stack(mf.mo_coeff, axis=0)
        mo_energy = np.stack(mf.mo_energy, axis=0)
        mo_occ = np.stack(mf.mo_occ, axis=0)

        vj = 2 * mf.get_j(mf.mol, rdm1, hermi=1)

        dm = mf.make_rdm1(mf.mo_coeff, mf.mo_occ)
        fock = np.stack([h1e, h1e], axis=0) + mf.get_veff(mf.mol, dm)

        kpt_info = kpt_info_from_pyscf(mf)
        # Compute ERIs for all pairs of k-points. Needed for Coulomb energy calculation
        rep_tensor = calc_eri_with_pyscf(mf, kpts=mf.kpts)

    # Restricted (non-spin polarized), periodic boundary conditions, gamma point only
    elif rdm1.ndim == 3 and hasattr(mf, "cell") and rdm1.shape[0] == 1:
        ao_and_1deriv = pbc_numint.eval_ao_kpts(
            mf.cell, grids.coords, kpts=mf.kpts, deriv=1
        )
        ao_and_1deriv = np.asarray(ao_and_1deriv)
        ao = ao_and_1deriv[:, 0, :, :]
        grad_ao = ao_and_1deriv[:, 1:4, :, :].transpose(0, 2, 3, 1)
        grad_n_ao = pbc_ao_grads(mf.cell, jnp.array(mf.grids.coords), order=grad_order)
        # Collapse the redundant extra dimension from k-points: gamma only
        ao = np.squeeze(ao, axis=0)
        grad_ao = np.squeeze(grad_ao, axis=0)
        for key in grad_n_ao.keys():
            grad_n_ao[key] = np.squeeze(grad_n_ao[key], axis=0)

        s1e = mf.get_ovlp(mf.mol)
        s1e = np.squeeze(s1e, axis=0)
        h1e = mf.get_hcore(mf.mol)
        # h1e = np.squeeze(h1e, axis=0)
        # rdm1 = np.squeeze(rdm1, axis=1)
        mo_coeff = np.squeeze(mf.mo_coeff, axis=0)
        mo_occ = np.squeeze(mf.mo_occ, axis=0)

        half_dm = rdm1 / 2
        half_mo_coeff = mo_coeff
        half_mo_energy = mf.mo_energy
        half_mo_occ = mo_occ / 2

        rdm1 = np.stack([half_dm, half_dm], axis=0)
        vj = 2 * mf.get_j(
            mf.mol, rdm1, hermi=1
        )  # The 2 is to compensate for the /2 in the definition of the density matrix
        rdm1 = np.squeeze(rdm1, axis=1)
        mo_coeff = np.stack([half_mo_coeff, half_mo_coeff], axis=0)
        mo_energy = np.stack([half_mo_energy, half_mo_energy], axis=0)
        mo_energy = np.squeeze(mo_energy, axis=1)
        mo_occ = np.stack([half_mo_occ, half_mo_occ], axis=0)

        dm = mf.make_rdm1(mf.mo_coeff, mf.mo_occ)
        fock = np.stack([h1e, h1e], axis=0) + mf.get_veff(mf.mol, dm)
        fock = np.squeeze(fock, axis=1)
        vj = np.squeeze(vj, axis=1)
        h1e = np.squeeze(h1e, axis=0)
        rep_tensor = calc_eri_with_pyscf(mf)
        kpt_info = None

    # Unrestricted (spin polarized), periodic boundary conditions, gamma point only
    elif rdm1.ndim == 4 and hasattr(mf, "cell") and rdm1.shape[1] == 1:
        ao_and_1deriv = pbc_numint.eval_ao_kpts(
            mf.cell, grids.coords, kpts=mf.kpts, deriv=1
        )
        ao_and_1deriv = np.asarray(ao_and_1deriv)
        ao = ao_and_1deriv[:, 0, :, :]
        grad_ao = ao_and_1deriv[:, 1:4, :, :].transpose(0, 2, 3, 1)
        grad_n_ao = pbc_ao_grads(mf.cell, jnp.array(mf.grids.coords), order=grad_order)

        # Collapse the redundant extra dimension from k-points: gamma only
        for key in grad_n_ao.keys():
            grad_n_ao[key] = np.squeeze(grad_n_ao[key], axis=0)
        ao = np.squeeze(ao, axis=0)
        grad_ao = np.squeeze(grad_ao, axis=0)
        s1e = mf.get_ovlp(mf.mol)
        s1e = np.squeeze(s1e, axis=0)
        h1e = mf.get_hcore(mf.mol)
        # h1e = np.squeeze(h1e, axis=0)
        vj = 2 * mf.get_j(
            mf.mol, rdm1, hermi=1
        )  # The 2 is to compensate for the /2 in the definition of the density matrix
        # Collapse the redundant extra dimension from k-points: gamma only

        rdm1 = np.squeeze(rdm1, axis=1)
        mo_coeff = np.squeeze(mf.mo_coeff, axis=1)
        mo_occ = np.squeeze(mf.mo_occ, axis=1)

        mo_coeff = np.stack(mo_coeff, axis=0)
        mo_energy = np.stack(mf.mo_energy, axis=0)
        mo_energy = np.squeeze(mo_energy, axis=1)
        mo_occ = np.stack(mo_occ, axis=1).T

        dm = mf.make_rdm1(mf.mo_coeff, mf.mo_occ)
        fock = np.stack([h1e, h1e], axis=0) + mf.get_veff(mf.mol, dm)
        fock = np.squeeze(fock, axis=1)
        vj = np.squeeze(vj, axis=1)
        h1e = np.squeeze(h1e, axis=0)
        rep_tensor = calc_eri_with_pyscf(mf)
        kpt_info = None

    else:
        raise RuntimeError(
            f"Invalid density matrix shape. Got {rdm1.shape} for AO shape {ao.shape}"
        )
    mf_e_tot = mf.e_tot
    energy_nuc = mf.energy_nuc()

    return (
        ao,
        grad_ao,
        grad_n_ao,
        rdm1,
        energy_nuc,
        h1e,
        vj,
        mo_coeff,
        mo_energy,
        mo_occ,
        mf_e_tot,
        s1e,
        fock,
        rep_tensor,
        kpt_info,
    )


##############################################################################################################


def process_mol(
    mol,
    compute_energy=False,
    grid_level: int = 2,
    training: bool = False,
    max_cycle: Optional[int] = None,
    xc_functional="wB97M_V",
) -> Tuple[Optional[float], Union[dft.RKS, dft.UKS]]:
    if mol.multiplicity == 1:
        mf = dft.RKS(mol)
    else:
        mf = dft.UKS(mol)
    mf.grids.level = int(grid_level)
    mf.grids.build()  # with_non0tab=True
    if training:
        mf.xc = xc_functional
        # mf.nlc='VV10'
    if max_cycle is not None:
        mf.max_cycle = max_cycle
    elif not training:
        mf.max_cycle = 0
    energy = mf.kernel()
    if not compute_energy:
        energy = None

    return energy, mf


def generate_chi_tensor(
    rdm1, ao, grid_coords, mol, omegas, chunk_size=1024, precision=Precision.HIGHEST
):
    r"""
    Generates the chi tensor, according to the molecular data and omegas provided.

    Parameters
    ----------
    rdm1: Array
        The molecular reduced density matrix Γbd^σ
        Expected shape: (n_spin, n_orbitals, n_orbitals)

    ao: Array
        The atomic orbitals ψa(r)
        Expected shape: (n_grid_points, n_orbitals)

    grid_coords: Array
        The coordinates of the grid.
        Expected shape: (n_grid_points)

    mol: A Pyscf mol object.

    omegas: List

    chunk_size : int, optional
        The batch size for the number of lattice points the integral
        evaluation is looped over. For a grid of N points, the solution
        formally requires the construction of a N x N matrix in an intermediate
        step. If `chunk_size` is given, the calculation is broken down into
        smaller subproblems requiring construction of only chunk_size x N matrices.
        Practically, higher `chunk_size`s mean faster calculations with larger
        memory requirements and vice-versa.

    precision: Precision, optional


    Returns
    ----------
    chi : Array
        Xa^σ = Γbd^σ ψb(r) ∫ dr' f(|r-r'|) ψa(r') ψd(r')
        Expected shape: (n_grid_points, n_omegas, n_spin, n_orbitals)
    """

    def chi_make(dm_, ao_, nu):
        return jnp.einsum("...bd,b,da->...a", dm_, ao_, nu, precision=precision)

    chi = []
    for omega in omegas:
        chi_omega = []
        for chunk_index, end_index, nu_chunk in _nu_chunk(
            mol, grid_coords, omega, chunk_size
        ):
            chi_chunk = vmap(chi_make, in_axes=(None, 0, 0), out_axes=0)(
                rdm1, ao[chunk_index:end_index], nu_chunk
            )
            chi_omega.append(chi_chunk)
        chi_omega = jnp.concatenate(chi_omega, axis=0)
        chi.append(chi_omega)
    if chi:
        return jnp.stack(chi, axis=1)
    else:
        return jnp.array(chi)
