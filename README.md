<div align="center">

# Grad-DFT: a software library for machine learning enhanced density functional theory

[![build](https://img.shields.io/badge/build-passing-graygreen.svg "https://github.com/XanaduAI/GradDFT/actions")](https://github.com/XanaduAI/GradDFT/actions) ![arXiv](http://img.shields.io/badge/arXiv-2101.10279-B31B1B.svg "Grad-DFT") ![License](https://img.shields.io/badge/License-Apache%202.0-9F9F9F "https://github.com/XanaduAI/GradDFT/blob/main/LICENSE")

</div>

Grad-DFT is a JAX-based library enabling the differentiable design and experimentation of exchange-correlation functionals using machine learning techniques. This library supports a parametrization of exchange-correlation functionals based on energy densities and associated coefficient functions; the latter typically constructed using neural networks:

```math
E_{xc} = \int d\mathbf{r} \mathbf{c}_\theta[\rho](\mathbf{r})\cdot\mathbf{e}[\rho](\mathbf{r}).
```

Grad-DFT provides significant functionality, including fully differentiable and just-in-time compilable self-consistent loop, direct optimization of the orbitals, and implementation of many of the known constraints of the exact functional in the form of loss functionals.

## Use example

### Creating a molecule

The first step is to create a `Molecule` object.

```python
from grad_dft.interface import molecule_from_pyscf
from pyscf import gto, dft

# Define a PySCF mol object for the H2 molecule
mol = gto.M(atom = [['H', (0, 0, 0)], ['H', (0.74, 0, 0)]], basis = 'def2-tzvp', spin = 0)
# Create a PySCF mean-field object
mf = dft.UKS(mol)
mf.kernel()
# Create a Molecule from the mean-field object
molecule = molecule_from_pyscf(mf)
```

### Creating a neural functional

A more complex, neural functional can be created as

```python
from jax.nn import sigmoid, gelu
from jax.random import PRNGKey
from flax import linen as nn
from optax import adam, apply_updates
from tqdm import tqdm
from grad_dft.train import molecule_predictor
from grad_dft.functional import NeuralFunctional, default_loss
from grad_dft.interface import molecule_from_pyscf

def coefficient_inputs(molecule):
    rho = jnp.clip(molecule.density(), a_min = 1e-30)
    kinetic = jnp.clip(molecule.kinetic_density(), a_min = 1e-30)
    return jnp.concatenate((rho, kinetic))

def coefficients(self, rhoinputs):
    x = nn.Dense(features=1)(rhoinputs)
    x = nn.LayerNorm()(x)
    return gelu(x)

neuralfunctional = NeuralFunctional(coefficients, energy_densities, coefficient_inputs)
```

with the corresponding energy calculation

```python
key = PRNGKey(42)
cinputs = coefficient_inputs(molecule)
params = neuralfunctional.init(key, cinputs)

predicted_energy = neuralfunctional.energy(params, molecule)
```

### Training the neural functional

```python
# Defining training parameters
learning_rate = 1e-5
momentum = 0.9
tx = adam(learning_rate=learning_rate, b1=momentum)
opt_state = tx.init(params)

# and implement the optimization loop
n_epochs = 20
molecule_predict = molecule_predictor(neuralfunctional)
for iteration in tqdm(range(n_epochs), desc="Training epoch"):
    (cost_value, predicted_energy), grads = default_loss(
        params, molecule_predict, HH_molecule, ground_truth_energy
    )
    print("Iteration", iteration, "Predicted energy:", predicted_energy, "Cost value:", cost_value)
    updates, opt_state = tx.update(grads, opt_state, params)
    params = apply_updates(params, updates)

# Save checkpoint
neuralfunctional.save_checkpoints(params, tx, step=n_epochs)
```

## Install

A core dependency of Grad-DFT is [PySCF](https://pyscf.org). To successfully install this package in the forthcoming installion with `pip`, please ensure that `cmake` is installed and that

```bash
which cmake
```

returns the correct path to the `cmake` binary. For instructions on installing `cmake`, visit https://cmake.org.

Now, in a fresh [conda environment](https://conda.io/projects/conda/en/latest/user-guide/tasks/manage-environments.html#activating-an-environment), navigate to the root directory of this repository and issue

```bash
pip install -e .
```

to install the base package. If you wish to run the examples in `~/examples`, you can run

```bash
pip install -e ".[examples]"
```

to install the additional dependencies.

## Acknowledgements

We thank helpful comments and insights from Alain Delgado, Modjtaba Shokrian Zini, Stepan Fomichev, Soran Jahangiri, Diego Guala, Jay Soni, Utkarsh Azad, Vincent Michaud-Rioux, Maria Schuld and Nathan Wiebe. 

GradDFT often follows similar calculations and naming conventions as PySCF, though adapted for our purposes. Only a few non-jittable DIIS procedures were directly taken from it. Where this happens, it has been conveniently referenced in the documentation. The test were also implemented against PySCF results.

## Bibtex

```
@article{graddft,
  title={Grad-DFT: a software library for machine learning density functional theory},
  author={Casares, Pablo Antonio Moreno and Baker, Jack and Medvidovi{\'c}, Matija and Dos Reis, Roberto, and Arrazola, Juan Miguel},
  journal={arXiv preprint [number]},
  year={2023}
}
```
