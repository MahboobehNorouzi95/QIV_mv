
import argparse
import pickle
import json
import os
from typing import Iterable, List, Literal, Optional, Tuple, Dict, Type

import matplotlib.pyplot as plt
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, random_split

from ..logging import info
from ..utils import default_validate_args, parse_project_and_run_name
from ..utils.conformal import OutcomeResidualPrediction
from ..utils.models import (MLP, GaussianNet, MixtureDensityNetwork,
                            OutcomeMLPBase, RidgeDensity)
from ..utils.nn import DensityModel
from ..utils.training import train_model
from .core import (IVDataset, IVDatasetWithGenotypes,
                   MREstimatorWithUncertainty, SupervisedLearningWrapper)


# Supported models for the exposure network.
ExposureNetTypeKey = Literal["mixture_density_net", "gaussian_net", "ridge"]

NET_TO_CLASS: Dict[ExposureNetTypeKey, Type[DensityModel]] = {
    "mixture_density_net": MixtureDensityNetwork,
    "gaussian_net": GaussianNet,
    "ridge": RidgeDensity
}


# Default values definitions.
# fmt: off
DEFAULTS = {
    "alpha": 0.1,
    "n_gaussians": 5,
    "exposure_network_type": "mixture_density_net",
    "exposure_hidden": [128, 64],
    "outcome_hidden": [32, 16],
    "exposure_learning_rate": 5e-4,
    "outcome_learning_rate": 5e-4,
    "exposure_batch_size": 10_000,
    "outcome_batch_size": 10_000,
    "exposure_max_epochs": 1000,
    "outcome_max_epochs": 1000,
    "exposure_weight_decay": 1e-4,
    "outcome_weight_decay": 1e-4,
    "exposure_add_input_batchnorm": False,
    "outcome_add_input_batchnorm": False,
    "accelerator": "gpu" if (
        torch.cuda.is_available() and torch.cuda.device_count() > 0
    ) else "cpu",
    "validation_proportion": 0.2,
    "output_dir": "deep_iv_estimate",
}
# fmt: on


class DeepIVMSEFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, prediction, target, samples):
        delta = samples - target
        output = torch.pow(delta, 2).mean()
        for d in delta.shape:
            delta /= d
        ctx.save_for_backward(delta)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        delta, = ctx.saved_tensors
        grad_prediction = grad_target = grad_samples = None
        if ctx.needs_input_grad[0]:
            grad_prediction = grad_output * 2 * delta

        # So that it works if someone flips the target and prediction in the
        # MSE.
        if ctx.needs_input_grad[1]:
            grad_target = -grad_output * 2 * delta

        return grad_prediction, grad_target, grad_samples


class DeepIVMSE(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, prediction, target, samples):
        if prediction.shape[0] != target.shape[0]:
            raise RuntimeError(
                "Size mismatch between prediction (%s) and target (%s)"
                % (prediction.shape, target.shape)
            )
        if samples is not None and prediction.shape[0] != samples.shape[0]:
            raise RuntimeError(
                "Size mismatch between prediction (%s) and samples (%s)"
                % (prediction.shape, samples.shape)
            )

        return DeepIVMSEFunction.apply(prediction, target, samples)


class OutcomeMLP(OutcomeMLPBase):
    def __init__(
        self,
        exposure_network: DensityModel,
        input_size: int,
        hidden: Iterable[int],
        lr: float,
        weight_decay: float = 0,
        add_input_layer_batchnorm: bool = False,
        add_hidden_layer_batchnorm: bool = False,
        activations: Iterable[nn.Module] = [nn.GELU()]
    ):
        super().__init__(
            exposure_network=exposure_network,
            input_size=input_size,
            hidden=hidden,
            lr=lr,
            sqr=False,  # Currently not supported.
            weight_decay=weight_decay,
            add_input_layer_batchnorm=add_input_layer_batchnorm,
            add_hidden_layer_batchnorm=add_hidden_layer_batchnorm,
            activations=activations
        )
        self.loss = DeepIVMSE()

    def forward(self, x, covars, taus=None):
        """Do not use this function for training."""
        assert taus is None
        stack = [x]
        if covars is not None:
            stack.append(covars)
        return self.mlp(torch.hstack(stack))

    def _step(self, batch, batch_index, log_prefix):
        _, y, ivs, covars = batch

        exposure_net_xs = torch.hstack(
            [tens for tens in (ivs, covars) if tens is not None]
        )

        with torch.no_grad():
            assert isinstance(self.exposure_network, DensityModel)
            x_samples = self.exposure_network.sample(
                exposure_net_xs,
                2,
                device=self.device
            )

        prediction = self.mlp(torch.hstack([x_samples[:, [0]], covars]))
        with torch.no_grad():
            samples = self.mlp(torch.hstack([x_samples[:, [1]], covars]))

        loss = self.loss(prediction, y, samples)
        self.log(f"outcome_{log_prefix}_loss", loss)
        return loss


class DeepIVEstimator(MREstimatorWithUncertainty):
    def __init__(
        self,
        exposure_network: DensityModel,
        outcome_network: OutcomeResidualPrediction,
    ):
        self.exposure_network = exposure_network
        self.outcome_network = outcome_network

    @property
    def alpha(self):
        return self.outcome_network.hparams.alpha  # type: ignore

    @classmethod
    def from_results(cls, dir_name: str) -> "DeepIVEstimator":
        with open(os.path.join(dir_name, "meta.json"), "rt") as f:
            meta = json.load(f)

        exposure_network = _load_exposure_model_from_dir(
            dir_name, meta["exposure_network_type"]
        )

        outcome_network = OutcomeMLP.load_from_checkpoint(
            os.path.join(dir_name, "outcome_network.ckpt"),
            exposure_network=exposure_network
        )

        outcome_network_calibrated = OutcomeResidualPrediction\
            .load_from_checkpoint(
                os.path.join(dir_name, "outcome_network_calibration.ckpt"),
                wrapped_model=outcome_network
            )

        # Set the q_hat
        with open(os.path.join(dir_name, "meta.json")) as f:
            meta = json.load(f)

        outcome_network_calibrated.q_hat = meta["q_hat"]  # type: ignore

        return cls(exposure_network, outcome_network_calibrated)

    def effect(self, x: torch.Tensor, covars: Optional[torch.Tensor] = None):
        return self.effect_with_prediction_interval(
            x,
            covars,
            alpha=self.alpha
        )[:, 1]

    def effect_with_prediction_interval(
        self,
        x: torch.Tensor,
        covars: Optional[torch.Tensor] = None,
        alpha: float = 0.05
    ) -> torch.Tensor:
        """Mean exposure to outcome effect at values of x."""
        if alpha != self.alpha:
            raise ValueError(
                f"Only alpha={self.alpha} was estimated for this model."
            )

        if x.ndim == 1:
            x = x.reshape(-1, 1)

        return self.average_treatment_effect(
            x, covars, self.outcome_network.x_to_y
        )


def main(args: argparse.Namespace) -> None:
    default_validate_args(args)

    if args.exposure_network_type != "mixture_density_net":
        args.n_gaussians = None

    dataset = IVDatasetWithGenotypes.from_argparse_namespace(args)

    # Automatically add the model hyperparameters.
    kwargs = {k: v for k, v in vars(args).items() if k in DEFAULTS.keys()}

    fit_deep_iv(
        dataset=dataset,
        no_plot=args.no_plot,
        wandb_project=args.wandb_project,
        **kwargs,
    )


def train_exposure_model(
    exposure_network_type: str,
    train_dataset: Dataset,
    val_dataset: Dataset,
    input_size: int,
    output_dir: str,
    hidden: List[int],
    learning_rate: float,
    weight_decay: float,
    batch_size: int,
    add_input_batchnorm: bool,
    max_epochs: int,
    n_gaussians: int = 5,
    accelerator: Optional[str] = None,
    wandb_project: Optional[str] = None
) -> Optional[float]:
    info("Training exposure model.")

    if exposure_network_type == "mixture_density_net":
        info(
            f"Using a Mixture Density Network with {n_gaussians} components "
            f"as the exposure model."
        )
        model = MixtureDensityNetwork(
            input_size=input_size,
            hidden=hidden,
            n_components=n_gaussians,
            lr=learning_rate,
            weight_decay=weight_decay,
            add_input_layer_batchnorm=add_input_batchnorm,
            add_hidden_layer_batchnorm=True
        )
        monitored_metric = "mdn_val_nll"

    elif exposure_network_type == "gaussian_net":
        info("Using a Gaussian NN for the exposure model.")
        info("The requested number of gaussian components will be ignored.")
        model = GaussianNet(
            input_size=input_size,
            hidden=hidden,
            lr=learning_rate,
            weight_decay=weight_decay,
            add_input_layer_batchnorm=add_input_batchnorm,
            add_hidden_layer_batchnorm=True
        )
        monitored_metric = "val_loss"

    elif exposure_network_type == "ridge":
        model = RidgeDensity()
        model.fit(train_dataset)  # type: ignore

        with open(os.path.join(output_dir, "exposure_network.pkl"), "wb") as f:
            pickle.dump(model, f)

        return None

    return train_model(
        SupervisedLearningWrapper(train_dataset),  # type: ignore
        SupervisedLearningWrapper(val_dataset),  # type: ignore
        model,
        monitored_metric=monitored_metric,
        output_dir=output_dir,
        checkpoint_filename="exposure_network.ckpt",
        batch_size=batch_size,
        max_epochs=max_epochs,
        accelerator=accelerator,
        wandb_project=wandb_project
    )


def train_outcome_model(
    train_dataset: Dataset,
    val_dataset: Dataset,
    exposure_network: MixtureDensityNetwork,
    output_dir: str,
    hidden: List[int],
    learning_rate: float,
    weight_decay: float,
    batch_size: int,
    add_input_batchnorm: bool,
    max_epochs: int,
    accelerator: Optional[str] = None,
    wandb_project: Optional[str] = None
) -> float:
    info("Training outcome model.")
    n_covars = train_dataset[0][3].numel()
    model = OutcomeMLP(
        exposure_network=exposure_network,
        input_size=1 + n_covars,
        lr=learning_rate,
        weight_decay=weight_decay,
        hidden=hidden,
        add_input_layer_batchnorm=add_input_batchnorm,
    )

    return train_model(
        train_dataset,
        val_dataset,
        model,
        monitored_metric="outcome_val_loss",
        output_dir=output_dir,
        checkpoint_filename="outcome_network.ckpt",
        batch_size=batch_size,
        max_epochs=max_epochs,
        accelerator=accelerator,
        wandb_project=wandb_project
    )


def train_conformal_predictor(
    train_dataset: Dataset,
    val_dataset: Dataset,
    outcome_network: OutcomeMLP,
    alpha: float,
    batch_size: int,
    output_dir: str,
    accelerator: str,
):
    n_covars = train_dataset[0][3].numel()
    model = OutcomeResidualPrediction(
        1 + n_covars, wrapped_model=outcome_network, alpha=alpha
    )

    train_model(
        train_dataset,
        val_dataset,
        model,
        monitored_metric="val_resid_pred_loss",
        output_dir=output_dir,
        checkpoint_filename="outcome_network_calibration.ckpt",
        batch_size=batch_size,
        max_epochs=100,
        accelerator=accelerator,
        wandb_project=None,
    )


def _load_exposure_model_from_dir(
    dirname: str,
    exposure_network_type: ExposureNetTypeKey
) -> DensityModel:
    if exposure_network_type == "ridge":
        filename = os.path.join(dirname, "exposure_network.pkl")
        with open(filename, "rb") as f:
            return pickle.load(f)

    filename = os.path.join(dirname, "exposure_network.ckpt")
    exposure_network = NET_TO_CLASS[exposure_network_type]\
        .load_from_checkpoint(filename).eval()  # type: ignore

    exposure_network.freeze()
    return exposure_network


def fit_deep_iv(
    dataset: IVDataset,
    exposure_network_type: ExposureNetTypeKey = DEFAULTS["exposure_network_type"],  # type: ignore # noqa: E501
    n_gaussians: int = DEFAULTS["n_gaussians"],  # type: ignore
    output_dir: str = DEFAULTS["output_dir"],  # type: ignore
    validation_proportion: float = DEFAULTS["validation_proportion"],  # type: ignore # noqa: E501
    no_plot: bool = False,
    alpha: float = DEFAULTS["alpha"],  # type: ignore
    exposure_hidden: List[int] = DEFAULTS["exposure_hidden"],  # type: ignore
    exposure_learning_rate: float = DEFAULTS["exposure_learning_rate"],  # type: ignore # noqa: E501
    exposure_weight_decay: float = DEFAULTS["exposure_weight_decay"],  # type: ignore # noqa: E501
    exposure_batch_size: int = DEFAULTS["exposure_batch_size"],  # type: ignore
    exposure_max_epochs: int = DEFAULTS["exposure_max_epochs"],  # type: ignore
    exposure_add_input_batchnorm: bool = DEFAULTS["exposure_add_input_batchnorm"],  # type: ignore # noqa: E501
    outcome_hidden: List[int] = DEFAULTS["outcome_hidden"],  # type: ignore
    outcome_learning_rate: float = DEFAULTS["outcome_learning_rate"],  # type: ignore # noqa: E501
    outcome_weight_decay: float = DEFAULTS["outcome_weight_decay"],  # type: ignore # noqa: E501
    outcome_batch_size: int = DEFAULTS["outcome_batch_size"],  # type: ignore
    outcome_max_epochs: int = DEFAULTS["outcome_max_epochs"],  # type: ignore
    outcome_add_input_batchnorm: bool = DEFAULTS["outcome_add_input_batchnorm"],  # type: ignore # noqa: E501
    accelerator: str = DEFAULTS["accelerator"],  # type: ignore
    wandb_project: Optional[str] = None
) -> DeepIVEstimator:
    # Create output directory if needed.
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    # Metadata dictionary that will be saved alongside the results.
    meta = locals()
    del meta["dataset"]  # We don't serialize the dataset.

    covars = dataset.save_covariables(output_dir)

    min_x = torch.min(dataset.exposure).item()
    max_x = torch.max(dataset.exposure).item()
    domain = (min_x, max_x)

    # Split here into train and val.
    train_dataset, val_dataset = random_split(
        dataset, [1 - validation_proportion, validation_proportion]
    )

    exposure_val_loss = train_exposure_model(
        exposure_network_type=exposure_network_type,
        train_dataset=train_dataset,
        val_dataset=val_dataset,
        input_size=dataset.n_exog(),
        output_dir=output_dir,
        hidden=exposure_hidden,
        learning_rate=exposure_learning_rate,
        weight_decay=exposure_weight_decay,
        batch_size=exposure_batch_size,
        add_input_batchnorm=exposure_add_input_batchnorm,
        max_epochs=exposure_max_epochs,
        n_gaussians=n_gaussians,
        accelerator=accelerator,
        wandb_project=wandb_project
    )

    meta["exposure_val_loss"] = exposure_val_loss

    exposure_network = _load_exposure_model_from_dir(
        output_dir, exposure_network_type
    )

    outcome_val_loss = train_outcome_model(
        train_dataset=train_dataset,
        val_dataset=val_dataset,
        exposure_network=exposure_network,
        output_dir=output_dir,
        hidden=outcome_hidden,
        learning_rate=outcome_learning_rate,
        weight_decay=outcome_weight_decay,
        batch_size=outcome_batch_size,
        add_input_batchnorm=outcome_add_input_batchnorm,
        max_epochs=outcome_max_epochs,
        accelerator=accelerator,
        wandb_project=wandb_project
    )

    meta["outcome_val_loss"] = outcome_val_loss

    outcome_network = OutcomeMLP.load_from_checkpoint(
        os.path.join(output_dir, "outcome_network.ckpt"),
        exposure_network=exposure_network,
    ).eval()  # type: ignore

    outcome_network.freeze()

    # Train the conformal predictor.
    train_conformal_predictor(
        train_dataset=train_dataset,
        val_dataset=val_dataset,
        outcome_network=outcome_network,
        alpha=alpha,
        batch_size=outcome_batch_size,
        output_dir=output_dir,
        accelerator=accelerator
    )

    outcome_network_calib: OutcomeResidualPrediction = (
        OutcomeResidualPrediction.load_from_checkpoint(
            os.path.join(output_dir, "outcome_network_calibration.ckpt"),
            wrapped_model=outcome_network
        )
    )

    outcome_network_calib.set_q_hat_from_data(val_dataset)
    assert isinstance(outcome_network_calib.q_hat, torch.Tensor)
    meta["q_hat"] = outcome_network_calib.q_hat.item()

    estimator = DeepIVEstimator(exposure_network, outcome_network_calib)

    with open(os.path.join(output_dir, "meta.json"), "wt") as f:
        json.dump(meta, f)

    save_estimator_statistics(
        estimator, covars, domain=domain,
        output_prefix=os.path.join(output_dir, "causal_estimates"),
    )

    if wandb_project is not None:
        import wandb
        _, run_name = parse_project_and_run_name(wandb_project)
        artifact = wandb.Artifact(
            "results" if run_name is None else f"{run_name}_results",
            type="results"
        )
        artifact.add_dir(output_dir)
        wandb.log_artifact(artifact)

    return estimator


def save_estimator_statistics(
    estimator: DeepIVEstimator,
    covars: Optional[torch.Tensor],
    domain: Tuple[float, float],
    output_prefix: str = "causal_estimates",
):
    # Save the causal effect at over the domain.
    xs = torch.linspace(domain[0], domain[1], 200)
    preds = estimator.effect_with_prediction_interval(
        xs, covars, alpha=estimator.alpha
    )
    df = pd.DataFrame({
        "x": xs,
        "y_do_x_lower": preds[:, 0],
        "y_do_x": preds[:, 1],
        "y_do_x_upper": preds[:, 2],
    })

    plt.figure()
    plt.scatter(df["x"], df["y_do_x"], label="Estimated Y | do(X=x)", s=3)

    if "y_do_x_lower" in df.columns:
        # Add the CI on the plot.
        plt.fill_between(
            df["x"],
            df["y_do_x_lower"],
            df["y_do_x_upper"],
            color="#dddddd",
            zorder=-1,
            label=f"{int((1 - estimator.alpha) * 100)}% Prediction interval"
        )

    plt.xlabel("X")
    plt.ylabel("Y")
    plt.legend()
    plt.savefig(f"{output_prefix}.png", dpi=600)
    plt.clf()

    df.to_csv(f"{output_prefix}.csv", index=False)


def configure_argparse(parser) -> None:
    parser.add_argument(
        "--n-gaussians",
        type=int,
        help="Number of gaussians used for the mixture density network.",
        default=DEFAULTS["n_gaussians"]
    )

    parser.add_argument(
        "--exposure-network-type",
        type=str,
        help="Density model for the exposure network.",
        choices=["mixture_density_net", "gaussian_net", "ridge"],
        default=DEFAULTS["exposure_network_type"]
    )

    parser.add_argument("--output-dir", default=DEFAULTS["output_dir"])

    parser.add_argument(
        "--no-plot",
        help="Disable plotting of diagnostics.",
        action="store_true",
    )

    parser.add_argument(
        "--alpha",
        help="Miscoverage level for the prediction interval.",
        type=float,
        default=DEFAULTS["alpha"]
    )

    parser.add_argument(
        "--outcome-type",
        default="continuous",
        choices=["continuous", "binary"],
        help="Variable type for the outcome (binary vs continuous).",
    )

    parser.add_argument(
        "--validation-proportion",
        type=float,
        default=DEFAULTS["validation_proportion"],
    )

    parser.add_argument(
        "--accelerator",
        default=DEFAULTS["accelerator"],
        help="Accelerator (e.g. gpu, cpu, mps) use to train the model. This "
        "will be passed to Pytorch Lightning.",
    )

    parser.add_argument(
        "--wandb-project",
        default=None,
        type=str,
        help="Activates the Weights and Biases logger using the provided "
             "project name. Patterns such as project:run_name are also "
             "allowed."
    )

    MLP.add_mlp_arguments(
        parser,
        "exposure-",
        "Exposure Model Parameters",
        defaults={
            "hidden": DEFAULTS["exposure_hidden"],
            "batch-size": DEFAULTS["exposure_batch_size"],
        },
    )

    MLP.add_mlp_arguments(
        parser,
        "outcome-",
        "Outcome Model Parameters",
        defaults={
            "hidden": DEFAULTS["outcome_hidden"],
            "batch-size": DEFAULTS["outcome_batch_size"],
        },
    )

    IVDatasetWithGenotypes.add_dataset_arguments(parser)


estimate = fit_deep_iv
load = DeepIVEstimator.from_results
