import time
import datetime
import os
import random
import warnings

import ase.io
import numpy as np
import skorch.net
import torch
from skorch import NeuralNetRegressor
from skorch.callbacks import LRScheduler
from skorch.dataset import CVSplit
from collections import OrderedDict

from amptorch.dataset import AtomsDataset, DataCollater, construct_descriptor
from amptorch.descriptor.util import list_symbols_to_indices
from amptorch.metrics import evaluator
from amptorch.model import BPNN, CustomLoss
from amptorch.preprocessing import AtomsToData
from amptorch.utils import to_tensor, train_end_load_best_loss
from amptorch.data_parallel import DataParallel, ParallelCollater
from amptorch.ase_utils import AMPtorch


class AtomsTrainer:
    def __init__(self, config={}):
        self.config = config
        self.pretrained = False

    def load(self, load_dataset=True):
        self.load_config()
        self.load_rng_seed()
        if load_dataset:
            self.load_dataset()
        self.load_model()
        self.load_criterion()
        self.load_optimizer()
        self.load_logger()
        self.load_extras()
        self.load_skorch()

    def load_config(self):
        dtype = self.config["cmd"].get("dtype", torch.FloatTensor)
        torch.set_default_tensor_type(dtype)
        self.timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        self.identifier = self.config["cmd"].get("identifier", False)
        if self.identifier:
            self.identifier = self.timestamp + "-{}".format(self.identifier)
        else:
            self.identifier = self.timestamp

        self.gpus = self.config["optim"].get("gpus", 0)
        if self.gpus > 0:
            self.output_device = 0
            self.device = f"cuda:{self.output_device}"
        else:
            self.device = "cpu"
            self.output_device = -1
        self.debug = self.config["cmd"].get("debug", False)
        run_dir = self.config["cmd"].get("run_dir", "./")
        os.chdir(run_dir)
        if not self.debug:
            self.cp_dir = os.path.join(run_dir, "checkpoints", self.identifier)
            print(f"Results saved to {self.cp_dir}")
            os.makedirs(self.cp_dir, exist_ok=True)

    def load_rng_seed(self):
        seed = self.config["cmd"].get("seed", 0)
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    def get_unique_elements(self, training_images):
        elements = np.array(
            [atom.symbol for atoms in training_images for atom in atoms]
        )
        elements = np.unique(elements)
        return elements

    def load_dataset(self):
        training_images = self.config["dataset"]["raw_data"]
        # TODO: Scalability when dataset to large to fit into memory
        if isinstance(training_images, str):
            training_images = ase.io.read(training_images, ":")
        del self.config["dataset"]["raw_data"]

        self.elements = self.config["dataset"].get(
            "elements", self.get_unique_elements(training_images)
        )

        self.forcetraining = self.config["model"].get("get_forces", True)
        self.fp_scheme = self.config["dataset"].get("fp_scheme", "gaussian").lower()
        self.fp_params = self.config["dataset"]["fp_params"]
        self.save_fps = self.config["dataset"].get("save_fps", True)
        self.cutoff_params = self.config["dataset"].get(
            "cutoff_params", {"cutoff_func": "Cosine"}
        )
        descriptor_setup = (
            self.fp_scheme,
            self.fp_params,
            self.cutoff_params,
            self.elements,
        )
        self.train_dataset = AtomsDataset(
            images=training_images,
            descriptor_setup=descriptor_setup,
            forcetraining=self.forcetraining,
            save_fps=self.config["dataset"].get("save_fps", True),
            scaling=self.config["dataset"].get(
                "scaling", {"type": "normalize", "range": (0, 1)}
            ),
        )
        self.feature_scaler = self.train_dataset.feature_scaler
        self.target_scaler = self.train_dataset.target_scaler
        self.input_dim = self.train_dataset.input_dim
        self.val_split = self.config["dataset"].get("val_split", 0)
        self.config["dataset"]["descriptor"] = descriptor_setup
        if not self.debug:
            normalizers = {
                "target": self.target_scaler,
                "feature": self.feature_scaler,
            }
            torch.save(normalizers, os.path.join(self.cp_dir, "normalizers.pt"))
            # clean/organize config
            self.config["dataset"]["fp_length"] = self.input_dim
            torch.save(self.config, os.path.join(self.cp_dir, "config.pt"))
        print("Loading dataset: {} images".format(len(self.train_dataset)))

    def load_model(self):
        elements = list_symbols_to_indices(self.elements)
        self.model = BPNN(
            elements=elements, input_dim=self.input_dim, **self.config["model"]
        )
        print("Loading model: {} parameters".format(self.model.num_params))
        self.forcetraining = self.config["model"].get("get_forces", True)
        collate_fn = DataCollater(train=True, forcetraining=self.forcetraining)
        self.parallel_collater = ParallelCollater(self.gpus, collate_fn)
        if self.gpus > 0:
            self.model = DataParallel(
                self.model,
                output_device=self.output_device,
                num_gpus=self.gpus,
            )

    def load_extras(self):
        callbacks = []
        load_best_loss = train_end_load_best_loss(self.identifier)
        self.val_split = self.config["dataset"].get("val_split", 0)
        self.split = CVSplit(cv=self.val_split) if self.val_split != 0 else 0

        metrics = evaluator(
            self.val_split,
            self.config["optim"].get("metric", "mae"),
            self.identifier,
            self.forcetraining,
        )
        callbacks.extend(metrics)

        if not self.debug:
            callbacks.append(load_best_loss)
        scheduler = self.config["optim"].get("scheduler", None)
        if scheduler:
            scheduler = LRScheduler(scheduler["policy"], **scheduler["params"])
            callbacks.append(scheduler)
        if self.config["cmd"].get("logger", False):
            from skorch.callbacks import WandbLogger

            callbacks.append(
                WandbLogger(
                    self.wandb_run,
                    save_model=False,
                    keys_ignored="dur",
                )
            )
        self.callbacks = callbacks

    def load_criterion(self):
        self.criterion = self.config["optim"].get("loss_fn", CustomLoss)

    def load_optimizer(self):
        self.optimizer = {
            "optimizer": self.config["optim"].get("optimizer", torch.optim.Adam)
        }
        optimizer_args = self.config["optim"].get("optimizer_args", False)
        if optimizer_args:
            self.optimizer.update(optimizer_args)

    def load_logger(self):
        if self.config["cmd"].get("logger", False):
            import wandb

            self.wandb_run = wandb.init(
                name=self.identifier,
                config=self.config,
            )

    def load_skorch(self):
        skorch.net.to_tensor = to_tensor

        self.net = NeuralNetRegressor(
            module=self.model,
            criterion=self.criterion,
            criterion__force_coefficient=self.config["optim"].get(
                "force_coefficient", 0
            ),
            criterion__loss=self.config["optim"].get("loss", "mse"),
            lr=self.config["optim"].get("lr", 1e-1),
            batch_size=self.config["optim"].get("batch_size", 32),
            max_epochs=self.config["optim"].get("epochs", 100),
            iterator_train__collate_fn=self.parallel_collater,
            iterator_train__shuffle=True,
            iterator_train__pin_memory=True,
            iterator_valid__collate_fn=self.parallel_collater,
            iterator_valid__shuffle=False,
            iterator_valid__pin_memory=True,
            device=self.device,
            train_split=self.split,
            callbacks=self.callbacks,
            verbose=self.config["cmd"].get("verbose", True),
            **self.optimizer,
        )
        print("Loading skorch trainer")

    def train(self, raw_data=None):
        if raw_data is not None:
            self.config["dataset"]["raw_data"] = raw_data
        if not self.pretrained:
            self.load()

        stime = time.time()
        self.net.fit(self.train_dataset, None)
        elapsed_time = time.time() - stime
        print(f"Training completed in {elapsed_time}s")

    def predict(self, images, disable_tqdm=True):
        if len(images) < 1:
            warnings.warn("No images found!", stacklevel=2)
            return images

        self.descriptor = construct_descriptor(self.config["dataset"]["descriptor"])

        a2d = AtomsToData(
            descriptor=self.descriptor,
            r_energy=False,
            r_forces=False,
            save_fps=self.config["dataset"].get("save_fps", True),
            fprimes=self.forcetraining,
            cores=1,
        )

        data_list = a2d.convert_all(images, disable_tqdm=disable_tqdm)
        self.feature_scaler.norm(data_list, disable_tqdm=disable_tqdm)

        self.net.module.eval()
        collate_fn = DataCollater(train=False, forcetraining=self.forcetraining)

        predictions = {"energy": [], "forces": []}
        for data in data_list:
            collated = collate_fn([data]).to(self.device)
            energy, forces = self.net.module([collated])

            energy = self.target_scaler.denorm(
                energy.detach().cpu(), pred="energy"
            ).tolist()
            forces = self.target_scaler.denorm(
                forces.detach().cpu(), pred="forces"
            ).numpy()

            predictions["energy"].extend(energy)
            predictions["forces"].append(forces)

        return predictions

    def load_pretrained(self, checkpoint_path=None, gpu2cpu=False):
        """
        Args:
            checkpoint_path: str, Path to checkpoint directory
            gpu2cpu: bool, True if checkpoint was trained with GPUs and you
            wish to load on cpu instead.
        """

        self.pretrained = True
        print(f"Loading checkpoint from {checkpoint_path}")
        assert os.path.isdir(
            checkpoint_path
        ), f"Checkpoint: {checkpoint_path} not found!"
        if not self.config:
            # prediction only
            self.config = torch.load(os.path.join(checkpoint_path, "config.pt"))
            self.config["cmd"]["debug"] = True
            self.elements = self.config["dataset"]["descriptor"][-1]
            self.input_dim = self.config["dataset"]["fp_length"]
            if gpu2cpu:
                self.config["optim"]["gpus"] = 0
            self.load(load_dataset=False)
        else:
            # prediction+retraining
            self.load(load_dataset=True)
        self.net.initialize()

        if gpu2cpu:
            params_path = os.path.join(checkpoint_path, "params_cpu.pt")
            if not os.path.exists(params_path):
                params = torch.load(
                    os.path.join(checkpoint_path, "params.pt"),
                    map_location=torch.device("cpu"),
                )
                new_dict = OrderedDict()
                for k, v in params.items():
                    name = k[7:]
                    new_dict[name] = v
                torch.save(new_dict, params_path)
        else:
            params_path = os.path.join(checkpoint_path, "params.pt")

        try:
            self.net.load_params(
                f_params=params_path,
                f_optimizer=os.path.join(checkpoint_path, "optimizer.pt"),
                f_criterion=os.path.join(checkpoint_path, "criterion.pt"),
                f_history=os.path.join(checkpoint_path, "history.json"),
            )
            normalizers = torch.load(os.path.join(checkpoint_path, "normalizers.pt"))
            self.feature_scaler = normalizers["feature"]
            self.target_scaler = normalizers["target"]
        except NotImplementedError:
            print("Unable to load checkpoint!")

    def get_calc(self):
        return AMPtorch(self)
