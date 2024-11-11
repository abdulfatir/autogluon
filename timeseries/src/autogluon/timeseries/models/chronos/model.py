import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Union

import numpy as np
import pandas as pd

from autogluon.common.loaders import load_pkl
from autogluon.timeseries.dataset.ts_dataframe import TimeSeriesDataFrame
from autogluon.timeseries.models.abstract import AbstractTimeSeriesModel
from autogluon.timeseries.utils.forecast import get_forecast_horizon_index_ts_dataframe
from autogluon.timeseries.utils.warning_filters import disable_duplicate_logs, warning_filter

logger = logging.getLogger("autogluon.timeseries.models.chronos")


# allowed HuggingFace model paths with custom parameter definitions
MODEL_CONFIGS = {
    "chronos-t5-tiny": {
        "num_gpus": 0,  # minimum number of required GPUs
        "default_torch_dtype": "auto",
        "default_batch_size": 16,
    },
    "chronos-t5-mini": {
        "num_gpus": 0,
        "default_torch_dtype": "auto",
        "default_batch_size": 16,
    },
    "chronos-t5-small": {
        "num_gpus": 1,
        "default_torch_dtype": "bfloat16",
        "default_batch_size": 16,
    },
    "chronos-t5-base": {
        "num_gpus": 1,
        "default_torch_dtype": "bfloat16",
        "default_batch_size": 16,
    },
    "chronos-t5-large": {
        "num_gpus": 1,
        "default_torch_dtype": "bfloat16",
        "default_batch_size": 8,
    },
    "chronos-bolt-mini": {
        "num_gpus": 0,
        "default_torch_dtype": "auto",
        "default_batch_size": 256,
    },
    "chronos-bolt-small": {
        "num_gpus": 0,
        "default_torch_dtype": "auto",
        "default_batch_size": 256,
    },
    "chronos-bolt-base": {
        "num_gpus": 0,
        "default_torch_dtype": "auto",
        "default_batch_size": 256,
    },
}


MODEL_ALIASES = {
    "tiny": "autogluon/chronos-t5-tiny",
    "mini": "autogluon/chronos-t5-mini",
    "small": "autogluon/chronos-t5-small",
    "base": "autogluon/chronos-t5-base",
    "large": "autogluon/chronos-t5-large",
    "bolt-mini": "autogluon/chronos-bolt-mini",
    "bolt-small": "autogluon/chronos-bolt-small",
    "bolt-base": "autogluon/chronos-bolt-base",
}


class ChronosModel(AbstractTimeSeriesModel):
    """Chronos pretrained time series forecasting models. Models can be based on the original
    `ChronosModel <https://github.com/amazon-science/chronos-forecasting/blob/main/src/chronos/chronos.py>`_ implementation,
    as well as a newer family of Chronos-Bolt models which are capable of much faster inference.

    The original Chronos is a family of pretrained models, based on the T5 family, with number of parameters ranging between
    8M and 710M. The full collection of Chronos models is available on
    `Hugging Face <https://huggingface.co/collections/amazon/chronos-models-65f1791d630a8d57cb718444>`_. For Chronos small,
    base, and large variants a GPU is required to perform inference efficiently. Chronos takes a minimalistic approach to
    pretraining time series models, by discretizing time series data directly into bins which are treated as tokens,
    effectively performing regression by classification. This results in a simple and flexible framework
    for using any language model in the context of time series forecasting. See [Ansari2024]_ for more information.

    The newer Chronos-Bolt variants enable much faster inference by first "patching" the time series. The resulting
    time series is then fed into a T5 model for forecasting. The Chronos-Bolt variants are capable of performing inference
    on CPUs, and are available on Hugging Face <https://huggingface.co/collections/>`_.
    Chronos-Bolt variants are capable of much faster inference, especially on CPUs. Chronos-Bolt variants can be used with

    References
    ----------
    .. [Ansari2024] Ansari, Abdul Fatir, Stella, Lorenzo et al.
        "Chronos: Learning the Language of Time Series."
        http://arxiv.org/abs/2403.07815


    Other Parameters
    ----------------
    model_path: str, default = "autogluon/chronos-t5-small"
        Model path used for the model, i.e., a HuggingFace transformers ``name_or_path``. Can be a
        compatible model name on HuggingFace Hub or a local path to a model directory. Original
        Chronos models (i.e., ``autogluon/chronos-t5-{model_size}``) can be specified with aliases
        ``tiny``, ``mini`` , ``small``, ``base``, and ``large``. Chronos-Bolt models can be specified
        with ``bolt-mini``, ``bolt-small``, and ``bolt-base``.
    batch_size : int, default = 16
        Size of batches used during inference
    num_samples : int, default = 20
        Number of samples used during inference
    fine_tune : bool, default = False
        If True, the pretrained model will be fine-tuned
    fine_tune_lr: float, default = 0.0001
        The learning rate used for fine-tuning
    fine_tune_steps : int, default = 5000
        The number of gradient update steps to fine-tune for
    fine_tune_batch_size : int, default = 16
        The batch size to use for fine-tuning
    fine_tune_shuffle_buffer_size : int, default = 10000
        The size of the shuffle buffer to shuffle the data during fine-tuning. If None, shuffling will
        be turned off.
    eval_during_fine_tune : bool, default = False
        If True, validation will be performed during fine-tuning to select the best checkpoint.
        Setting this argument to True may result in slower fine-tuning.
    fine_tune_eval_max_items : int, default = 100
        The maximum number of randomly-sampled time series to use from the validation set for evaluation
        during fine-tuning. If None, the entire validation dataset will be used.
    fine_tune_trainer_kwargs : dict, optional
        Extra keyword arguments passed to ``transformers.TrainingArguments``
    device : str, default = None
        Device to use for inference (and fine-tuning, if enabled). If None, model will use the GPU if available.
        For larger model sizes `small`, `base`, and `large`; inference will fail if no GPU is available.
    context_length : int or None, default = None
        The context length to use in the model. Shorter context lengths will decrease model accuracy, but result
        in faster inference. If None, the model will infer context length from the data set length at inference
        time, but set it to a maximum of 2048. Note that this is only the context length used to pass data into
        the model. Individual model implementations may have different context lengths specified in their configuration,
        and may truncate the context further. For example, original Chronos models have a context length of 512, but
        Chronos-Bolt models handle contexts up to 2048.
    optimization_strategy : {None, "onnx", "openvino"}, default = None
        Optimization strategy to use for inference on CPUs. If None, the model will use the default implementation.
        If `onnx`, the model will be converted to ONNX and the inference will be performed using ONNX. If ``openvino``,
        inference will be performed with the model compiled to OpenVINO. These optimizations are only available for
        the original set of Chronos models, and not in Chronos-Bolt where they are not needed.
    torch_dtype : torch.dtype or {"auto", "bfloat16", "float32", "float64"}, default = "auto"
        Torch data type for model weights, provided to ``from_pretrained`` method of Hugging Face AutoModels. If
        original Chronos models are specified and the model size is ``small``, ``base``, or ``large``, the
        ``torch_dtype`` will be set to ``bfloat16`` to enable inference on GPUs.
    data_loader_num_workers : int, default = 0
        Number of worker processes to be used in the data loader. See documentation on ``torch.utils.data.DataLoader``
        for more information.
    keep_transformers_logs: bool, default = False
        If True, the logs generated by transformers will NOT be removed after fine-tuning
    """

    # default number of samples for prediction
    default_num_samples: int = 20
    default_model_path = "autogluon/chronos-t5-small"
    maximum_context_length = 2048
    fine_tuned_ckpt_name: str = "fine-tuned-ckpt"

    def __init__(
        self,
        freq: Optional[str] = None,
        prediction_length: int = 1,
        path: Optional[str] = None,
        name: Optional[str] = None,
        eval_metric: str = None,
        hyperparameters: Dict[str, Any] = None,
        **kwargs,  # noqa
    ):
        hyperparameters = hyperparameters if hyperparameters is not None else {}

        model_path_input = hyperparameters.get("model_path", self.default_model_path)
        self.model_path = MODEL_ALIASES.get(model_path_input, model_path_input)

        # TODO: automatically determine batch size based on GPU / memory availability
        self.batch_size = hyperparameters.get("batch_size", self.default_batch_size)
        self.num_samples = hyperparameters.get("num_samples", self.default_num_samples)
        self.device = hyperparameters.get("device")

        # if the model requires a GPU, set the torch dtype to bfloat16
        self.torch_dtype = hyperparameters.get("torch_dtype", self.default_torch_dtype)

        self.data_loader_num_workers = hyperparameters.get("data_loader_num_workers", 0)
        self.optimization_strategy: Optional[Literal["onnx", "openvino"]] = hyperparameters.get(
            "optimization_strategy", None
        )
        self.context_length = hyperparameters.get("context_length")

        if self.context_length is not None and self.context_length > self.maximum_context_length:
            logger.warning(
                f"\tContext length {self.context_length} exceeds maximum context length {self.maximum_context_length}."
                f"Context length will be set to {self.maximum_context_length}."
            )
            self.context_length = self.maximum_context_length

        # we truncate the name to avoid long path errors on Windows
        model_path_safe = str(model_path_input).replace("/", "__").replace(os.path.sep, "__")[-50:]
        name = (name if name is not None else "Chronos") + f"[{model_path_safe}]"

        super().__init__(
            path=path,
            freq=freq,
            prediction_length=prediction_length,
            name=name,
            eval_metric=eval_metric,
            hyperparameters=hyperparameters,
            **kwargs,
        )

        self.model_pipeline: Optional[Any] = None  # of type BaseChronosPipeline
        self.time_limit: Optional[float] = None

    def save(self, path: str = None, verbose: bool = True) -> str:
        pipeline = self.model_pipeline
        self.model_pipeline = None
        path = super().save(path=path, verbose=verbose)
        self.model_pipeline = pipeline

        return str(path)

    @classmethod
    def load(cls, path: str, reset_paths: bool = True, verbose: bool = True) -> "ChronosModel":
        model = load_pkl.load(path=os.path.join(path, cls.model_file_name), verbose=verbose)
        if reset_paths:
            model.set_contexts(path)

        fine_tune_ckpt_path = Path(model.path) / cls.fine_tuned_ckpt_name
        if fine_tune_ckpt_path.exists():
            logger.debug(f"Fine-tuned checkpoint exists, setting model_path to {fine_tune_ckpt_path}")
            model.model_path = fine_tune_ckpt_path

        return model

    def _is_gpu_available(self) -> bool:
        import torch.cuda

        return torch.cuda.is_available()

    @property
    def ag_default_config(self) -> Dict[str, Any]:
        """The default configuration of the model used by AutoGluon if the model is one of those
        defined in MODEL_CONFIGS. For now, these are ``autogluon/chronos-t5-*`` family of models.
        """
        model_name = str(self.model_path).split("/")[-1]
        return MODEL_CONFIGS.get(model_name, {})

    @property
    def min_num_gpus(self) -> int:
        """Minimum number of GPUs required for the model. For models not defined in AutoGluon,
        this value defaults to 0.
        """
        return self.ag_default_config.get("num_gpus", 0)

    @property
    def default_batch_size(self) -> int:
        """Default batch size used for the model. For models not defined in AutoGluon, this value
        defaults to 8.
        """
        return self.ag_default_config.get("default_batch_size", 8)

    @property
    def default_torch_dtype(self) -> Any:
        """Default torch data type used for the model. For models not defined in AutoGluon, this value
        defaults to "auto".
        """
        return self.ag_default_config.get("default_torch_dtype", "auto")

    def get_minimum_resources(self, is_gpu_available: bool = False) -> Dict[str, Union[int, float]]:
        minimum_resources = {"num_cpus": 1}
        # if GPU is available, we train with 1 GPU per trial
        if is_gpu_available:
            minimum_resources["num_gpus"] = self.min_num_gpus
        return minimum_resources

    def load_model_pipeline(self, is_training: bool = False):
        from .pipeline import BaseChronosPipeline

        gpu_available = self._is_gpu_available()

        if not gpu_available and self.min_num_gpus > 0:
            raise RuntimeError(
                f"{self.name} requires a GPU to run, but no GPU was detected. "
                "Please make sure that you are using a computer with a CUDA-compatible GPU and "
                "`import torch; torch.cuda.is_available()` returns `True`."
            )

        device = self.device or ("cuda" if gpu_available else "cpu")

        pipeline = BaseChronosPipeline.from_pretrained(
            self.model_path,
            device_map=device,
            # optimization cannot be used during fine-tuning
            optimization_strategy=None if is_training else self.optimization_strategy,
            torch_dtype=self.torch_dtype,
        )

        self.model_pipeline = pipeline

    def persist(self) -> "ChronosModel":
        self.load_model_pipeline()
        return self

    def _has_tf32(self):
        import torch.cuda

        return torch.cuda.is_available() and torch.cuda.get_device_capability()[0] >= 8

    def _get_model_params(self) -> dict:
        """Gets params that are passed to the inner model."""
        init_args = super()._get_model_params().copy()

        init_args.setdefault("fine_tune", False)
        init_args.setdefault("keep_transformers_logs", False)
        init_args.setdefault("fine_tune_lr", 1e-4)
        init_args.setdefault("fine_tune_steps", 5000)
        init_args.setdefault("fine_tune_batch_size", self.default_batch_size)
        init_args.setdefault("eval_during_fine_tune", False)
        init_args.setdefault("fine_tune_eval_max_items", 100)
        init_args.setdefault("fine_tune_shuffle_buffer_size", 10_000)

        eval_during_fine_tune = init_args["eval_during_fine_tune"]
        output_dir = Path(self.path) / "transformers_logs"
        fine_tune_trainer_kwargs = dict(
            output_dir=str(output_dir),
            per_device_train_batch_size=init_args["fine_tune_batch_size"],
            per_device_eval_batch_size=init_args["fine_tune_batch_size"],
            learning_rate=init_args["fine_tune_lr"],
            lr_scheduler_type="linear",
            warmup_ratio=0.0,
            optim="adamw_torch_fused",
            logging_dir=str(output_dir),
            logging_strategy="steps",
            logging_steps=100,
            report_to="none",
            max_steps=init_args["fine_tune_steps"],
            gradient_accumulation_steps=1,
            dataloader_num_workers=self.data_loader_num_workers,
            tf32=self._has_tf32(),
            save_only_model=True,
            prediction_loss_only=True,
            save_total_limit=1,
            save_strategy="steps" if eval_during_fine_tune else "no",
            save_steps=100 if eval_during_fine_tune else None,
            evaluation_strategy="steps" if eval_during_fine_tune else "no",
            eval_steps=100 if eval_during_fine_tune else None,
            load_best_model_at_end=True if eval_during_fine_tune else False,
            metric_for_best_model="eval_loss" if eval_during_fine_tune else None,
        )
        user_fine_tune_trainer_kwargs = init_args.get("fine_tune_trainer_kwargs", {})
        fine_tune_trainer_kwargs.update(user_fine_tune_trainer_kwargs)

        init_args["fine_tune_trainer_kwargs"] = fine_tune_trainer_kwargs

        return init_args

    def _fit(
        self,
        train_data: TimeSeriesDataFrame,
        val_data: Optional[TimeSeriesDataFrame] = None,
        time_limit: int = None,
        **kwargs,
    ) -> None:
        from transformers.trainer import PrinterCallback, Trainer, TrainingArguments

        from .pipeline import ChronosBoltPipeline
        from .pipeline.utils import ChronosFineTuningDataset, EvaluateAndSaveFinalStepCallback, TimeLimitCallback

        # verbosity < 3: all logs and warnings from transformers will be suppressed
        # verbosity >= 3: progress bar and loss logs will be logged
        # verbosity 4: everything will be logged
        verbosity = kwargs.get("verbosity", 2)
        for logger_name in logging.root.manager.loggerDict:
            if "transformers" in logger_name:
                transformers_logger = logging.getLogger(logger_name)
                transformers_logger.setLevel(logging.ERROR if verbosity <= 3 else logging.INFO)

        self._check_fit_params()

        fine_tune_args = self._get_model_params()
        do_fine_tune = fine_tune_args["fine_tune"]

        if do_fine_tune:
            assert train_data is not None, "train_data cannot be None when fine_tune=True"

        eval_during_fine_tune = val_data is not None and fine_tune_args["eval_during_fine_tune"]

        start_time = time.monotonic()
        if do_fine_tune:
            context_length = self._get_context_length(train_data)
            # load model pipeline to device memory
            self.load_model_pipeline(is_training=True)

            extra_fine_tune_trainer_kwargs = {}
            fine_tune_prediction_length = self.prediction_length
            if isinstance(self.model_pipeline, ChronosBoltPipeline):
                # custom label_names is needed for validation to work with ChronosBolt models
                extra_fine_tune_trainer_kwargs = dict(label_names=["target"])

                # truncate prediction_length if it goes beyond ChronosBolt's prediction_length
                fine_tune_prediction_length = min(
                    self.model_pipeline.inner_model.config.chronos_config["prediction_length"], self.prediction_length
                )

                if self.prediction_length != fine_tune_prediction_length:
                    logger.debug(
                        f"ChronosBolt models can only be fine-tuned with a maximum prediction_length of {fine_tune_prediction_length}. "
                        f"Setting prediction_length to {fine_tune_prediction_length}."
                    )

            fine_tune_trainer_kwargs = fine_tune_args["fine_tune_trainer_kwargs"]
            fine_tune_trainer_kwargs["disable_tqdm"] = fine_tune_trainer_kwargs.get("disable_tqdm", (verbosity < 3))
            output_dir = Path(fine_tune_trainer_kwargs["output_dir"])

            if not eval_during_fine_tune:
                # turn of eval-related trainer args
                fine_tune_trainer_kwargs["evaluation_strategy"] = "no"
                fine_tune_trainer_kwargs["eval_steps"] = None
                fine_tune_trainer_kwargs["load_best_model_at_end"] = False
                fine_tune_trainer_kwargs["metric_for_best_model"] = None

            training_args = TrainingArguments(**fine_tune_trainer_kwargs, **extra_fine_tune_trainer_kwargs)
            tokenizer_train_dataset = ChronosFineTuningDataset(
                target_df=train_data,
                target_column=self.target,
                context_length=context_length,
                prediction_length=fine_tune_prediction_length,
                # if tokenizer exists, then the data is returned in the HF-style format accepted by
                # the original Chronos models otherwise the data is returned in ChronosBolt's format
                tokenizer=getattr(self.model_pipeline, "tokenizer", None),
                mode="training",
            ).shuffle(fine_tune_args["fine_tune_shuffle_buffer_size"])

            callbacks = []
            if time_limit is not None:
                callbacks.append(TimeLimitCallback(time_limit=time_limit))

            if val_data is not None:
                callbacks.append(EvaluateAndSaveFinalStepCallback())
                # evaluate on a randomly-sampled subset
                fine_tune_eval_max_items = (
                    min(val_data.num_items, fine_tune_args["fine_tune_eval_max_items"])
                    if fine_tune_args["fine_tune_eval_max_items"] is not None
                    else val_data.num_items
                )

                if fine_tune_eval_max_items < val_data.num_items:
                    eval_items = np.random.choice(val_data.item_ids.values, size=fine_tune_eval_max_items, replace=False)
                    val_data = val_data.loc[eval_items]

                tokenizer_val_dataset = ChronosFineTuningDataset(
                    target_df=val_data,
                    target_column=self.target,
                    context_length=context_length,
                    prediction_length=fine_tune_prediction_length,
                    tokenizer=getattr(self.model_pipeline, "tokenizer", None),
                    mode="validation",
                )

            trainer = Trainer(
                model=self.model_pipeline.inner_model,
                args=training_args,
                train_dataset=tokenizer_train_dataset,
                eval_dataset=tokenizer_val_dataset if val_data is not None else None,
                callbacks=callbacks,
            )

            if verbosity < 3:
                # remove PrinterCallback from callbacks which logs to the console via a print() call,
                # so it cannot be handled by setting the log level
                trainer.pop_callback(PrinterCallback)
            else:
                logger.warning(
                    "Transformers logging is turned on during fine-tuning. Note that losses reported by transformers "
                    "may not correspond to those specified via `eval_metric`."
                )

            if val_data is not None:
                # evaluate once before training
                zero_shot_eval_loss = trainer.evaluate()["eval_loss"]

            trainer.train()

            if eval_during_fine_tune:
                # get the best eval_loss logged during fine-tuning
                log_history_df = pd.DataFrame(trainer.state.log_history)
                best_train_eval_loss = log_history_df["eval_loss"].min()
            elif val_data is not None:
                # evaluate at the end of fine-tuning
                best_train_eval_loss = trainer.evaluate()["eval_loss"]

            if val_data is None or best_train_eval_loss <= zero_shot_eval_loss:
                fine_tuned_ckpt_path = Path(self.path) / self.fine_tuned_ckpt_name
                logger.info(f"Saving fine-tuned model to {fine_tuned_ckpt_path}")
                self.model_pipeline.inner_model.save_pretrained(Path(self.path) / self.fine_tuned_ckpt_name)
            else:
                logger.info("Validation loss worsened after fine-tuning. Reverting to the pretrained model.")
                self.model_pipeline = None
                self.load_model_pipeline(is_training=False)

            if not fine_tune_args["keep_transformers_logs"]:
                logger.debug(f"Removing transformers_logs directory {output_dir}")
                shutil.rmtree(output_dir)

        if time_limit is not None:
            self.time_limit = time_limit - (time.monotonic() - start_time)  # inference time budget

    def _get_inference_data_loader(
        self,
        data: TimeSeriesDataFrame,
        context_length: int,
        num_workers: int = 0,
        time_limit: Optional[float] = None,
    ):
        from .pipeline.utils import ChronosInferenceDataLoader, ChronosInferenceDataset, timeout_callback

        chronos_dataset = ChronosInferenceDataset(
            target_df=data,
            target_column=self.target,
            context_length=context_length,
        )

        return ChronosInferenceDataLoader(
            chronos_dataset,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=num_workers,
            on_batch=timeout_callback(seconds=time_limit),
        )

    def _get_context_length(self, data: TimeSeriesDataFrame) -> int:
        context_length = self.context_length or min(
            data.num_timesteps_per_item().max(),
            self.maximum_context_length,
        )
        return context_length

    def _predict(
        self,
        data: TimeSeriesDataFrame,
        known_covariates: Optional[TimeSeriesDataFrame] = None,
        **kwargs,
    ) -> TimeSeriesDataFrame:
        # We defer initialization of the model pipeline. i.e., the model is only loaded to device memory
        # during inference. We also infer the maximum length of the time series in the inference data set
        # and use that to determine the context length of the model. If the context length is specified
        # during initialization, this is always used. If not, the context length is set to the longest
        # item length. The context length is always capped by self.maximum_context_length.
        # Note that this is independent of the model's own context length set in the model's config file.
        # For example, if the context_length is set to 2048 here but the model expects context length
        # (according to its config.json file) of 512, it will further truncate the series during inference.
        context_length = self._get_context_length(data)

        with warning_filter(all_warnings=True):
            import torch

            if self.model_pipeline is None:
                # FIXME: optimization_strategy is ignored when model is fine-tuned
                # load model pipeline to device memory
                self.load_model_pipeline()

            inference_data_loader = self._get_inference_data_loader(
                data=data,
                num_workers=self.data_loader_num_workers,
                context_length=context_length,
                time_limit=kwargs.get("time_limit"),
            )

            self.model_pipeline.model.eval()
            with torch.inference_mode(), disable_duplicate_logs(logger):
                batch_quantiles, batch_means = [], []
                for batch in inference_data_loader:
                    qs, mn = self.model_pipeline.predict_quantiles(
                        batch,
                        prediction_length=self.prediction_length,
                        quantile_levels=self.quantile_levels,
                        num_samples=self.num_samples,
                    )
                    batch_quantiles.append(qs.numpy())
                    batch_means.append(mn.numpy())

        df = pd.DataFrame(
            np.concatenate(
                [
                    np.concatenate(batch_means, axis=0).reshape(-1, 1),
                    np.concatenate(batch_quantiles, axis=0).reshape(-1, len(self.quantile_levels)),
                ],
                axis=1,
            ),
            columns=["mean"] + [str(q) for q in self.quantile_levels],
            index=get_forecast_horizon_index_ts_dataframe(data, self.prediction_length, freq=self.freq),
        )

        return TimeSeriesDataFrame(df)

    def _more_tags(self) -> Dict:
        return {"allow_nan": True, "can_use_val_data": True}

    def score_and_cache_oof(
        self,
        val_data: TimeSeriesDataFrame,
        store_val_score: bool = False,
        store_predict_time: bool = False,
        **predict_kwargs,
    ) -> None:
        # All computation happens during inference, so we provide the time_limit at prediction time
        # TODO: Once custom predict_kwargs is allowed, make sure that `time_limit` is not among the keys
        super().score_and_cache_oof(
            val_data, store_val_score, store_predict_time, time_limit=self.time_limit, **predict_kwargs
        )
