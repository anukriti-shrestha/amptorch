import sys
import os
import time
from itertools import product
import numpy as np
import amptorch
from scipy.optimize import minimize
from ase.neighborlist import NeighborList, NewPrimitiveNeighborList
from amptorch.gaussian import NeighborlistCalculator, Data
from amptorch.utils import Logger, hash_images, get_hash
import matplotlib.pyplot as plt
import pandas as pd
from functools import lru_cache


class morse_potential:
    def __init__(self, images, cutoff, filename, combo="mean"):
        #TODO Implement optional morse params input
        os.makedirs("results", exist_ok=True)
        os.makedirs("results/logs", exist_ok=True)
        self.filename = filename
        self.data = images
        unique_elements = np.unique(
            np.array([atom.symbol for atoms in images for atom in atoms])
        )
        self.params = self.get_params(unique_elements)
        self.combo = combo
        self.cutoff = cutoff
        self.hashed_images = hash_images(images)
        self.hashed_keys = list(self.hashed_images.keys())
        calc = NeighborlistCalculator(cutoff=cutoff)
        self.neighborlist = Data(filename="amp-data-neighborlists", calculator=calc)
        self.neighborlist.calculate_items(self.hashed_images)
        log = Logger("results/logs/{}.txt".format(filename))
        self.logresults(log, self.params)

    def get_neighbors(self, neighborlist, image_hash):
        image_neighbors = neighborlist[image_hash]
        return image_neighbors

    def image_pred(self, image):
        params_dict = self.params
        chemical_symbols = np.array(image.get_chemical_symbols())
        params = []
        for element in chemical_symbols:
            re = params_dict[element]["re"]
            D = params_dict[element]["De"]
            # sig calculated from pubs.acs.org/doi/pdf/10.1021/acs.jpca.7b11252 
            sig = re - np.log(2)/params_dict[element]["a"]
            params.append(np.array([[re, D, sig]]))
        params = np.vstack(np.array(params))
        natoms = len(image)

        image_hash = get_hash(image)
        image_neighbors = self.get_neighbors(self.neighborlist, image_hash)

        positions = image.positions
        cell = image.cell

        energy = 0.0
        forces = np.zeros((natoms, 3))

        for a1 in range(natoms):
            re_1 = params[a1][0]
            D_1 = np.abs(params[a1][1])
            sig_1 = params[a1][2]
            neighbors, offsets = image_neighbors[a1]
            cells = np.dot(offsets, cell)
            d = positions[neighbors] + cells - positions[a1]
            re_n = params[neighbors][:, 0]
            D_n = params[neighbors][:, 1]
            sig_n = params[neighbors][:, 2]
            if self.combo == "mean":
                D = 9.975126    
                re = 1.682829
                sig = re - np.log(2)/1.51511
            elif self.combo == "yang":
                D = (2 * D_1 * D_n) / (D_1 + D_n)
                sig = (sig_1 * sig_n) * (sig_1 + sig_n) / (sig_1 ** 2 + sig_n ** 2)
                re = (re_1 * re_n) * (re_1 + re_n) / (re_1 ** 2 + re_n ** 2)
            r = np.sqrt((d ** 2).sum(1))
            r_star = r / sig
            re_star = re / sig
            C = np.log(2) / (re_star - 1)
            atom_energy = D * (
                np.exp(-2 * C * (r_star - re_star))
                - 2 * np.exp(-C * (r_star - re_star))
            )
            energy += atom_energy.sum()
            f = (
                (2 * D * C / sig)
                * (1 / r)
                * (
                    np.exp(-2 * C * (r_star - re_star))
                    - np.exp(-C * (r_star - re_star))
                )
            )[:, np.newaxis] * d
            forces[a1] -= f.sum(axis=0)
            for a2, f2 in zip(neighbors, f):
                forces[a2] += f2
        return energy, forces, natoms

    def morse_pred(self, data):
        predicted_energies = []
        predicted_forces = []
        num_atoms = []
        for image in data:
            energy, forces, natoms = self.image_pred(image)
            predicted_energies.append(energy)
            predicted_forces.append(forces)
            num_atoms.append(natoms)
        return predicted_energies, predicted_forces, num_atoms

    def get_params(self, elements):
        params = {}
        for elem in elements:
            home_dir = os.path.dirname(amptorch.delta_models.__file__)
            try:
                element_params = (
                    pd.read_csv(f"{home_dir}/morse_params/{elem}{elem}.csv")
                    .iloc[0]
                    .to_dict()
                )
            except Exception:
                print(
                    """Morse parameters not available for {elem}, requires
                manual definition"""
                )
            params[elem] = element_params
        return params

    def logresults(self, log, params):
        log("%s" % time.asctime())
        log("-" * 50)
        log("Model parameters: %s" % (params))
        log("Combination rule: {}\n".format(self.combo))

    def parity(self, predicted_energies, predicted_forces):
        fig = plt.figure(figsize=(7.0, 7.0))
        fig2 = plt.figure(figsize=(7.0, 7.0))
        ax = fig.add_subplot(111)
        ax2 = fig2.add_subplot(111)
        predicted_energies = np.squeeze(predicted_energies)
        predicted_forces = np.squeeze(predicted_forces).reshape(1, -1)
        target_energies = np.squeeze(self.target_energies)
        target_forces = np.squeeze(self.target_forces).reshape(1, -1)
        energy_min = min(target_energies)
        energy_max = max(target_energies)
        force_min = min(target_forces)
        force_max = max(target_forces)
        ax.plot(target_energies, predicted_energies, "bo", markersize=3)
        ax.plot([energy_min, energy_max], [energy_min, energy_max], "r-", lw=0.5)
        ax.set_xlabel("ab initio energy, eV")
        ax.set_ylabel("Morse energy, eV")
        ax.set_title("Energy")
        fig.savefig("results/morse_parity_e.pdf")
        ax2.plot(target_forces, predicted_forces, "bo", markersize=3)
        ax2.plot([force_min, force_max], [force_min, force_max], "r-", lw=0.5)
        ax2.set_xlabel("ab initio force, eV/A")
        ax2.set_ylabel("Morse force, eV/A")
        ax2.set_title("Force")
        fig2.savefig("results/morse_parity_f.pdf")
        plt.show()
