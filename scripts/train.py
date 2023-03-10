"""
This is the script used to train DOLMA.

There is one required positional argument, the path to a YAML :class:`TrainConfig`.
Following the YAML path, you could pass any number of options to override
values in the :class:`TrainConfig`.

For example, to override :data:`TrainConfig.model.n_layers` to 5, pass ``--model.n_layers=5``:

```bash
python scripts/train.py train_config.yaml --model.n_layers=5
```

For distributed training you should run this script via the 'composer' CLI. For example:

```bash
composer scripts/train.py train_config.yaml ...
```
"""

import os
import sys
from typing import Any, Dict, List

import torch

from dolma import SchedulerConfig, TrainConfig
from dolma.data import build_dataloader
from dolma.exceptions import DolmaCliError, DolmaConfigurationError
from dolma.util import clean_opt, echo, prepare_cli_environment, update_batch_size_info


def build_scheduler(cfg: SchedulerConfig):
    from composer.optim.scheduler import (
        ConstantWithWarmupScheduler,
        CosineAnnealingWithWarmupScheduler,
        LinearWithWarmupScheduler,
    )

    if cfg.name == "constant_with_warmup":
        return ConstantWithWarmupScheduler(t_warmup=cfg.t_warmup)
    elif cfg.name == "cosine_with_warmup":
        return CosineAnnealingWithWarmupScheduler(t_warmup=cfg.t_warmup, alpha_f=cfg.alpha_f)
    elif cfg.name == "linear_decay_with_warmup":
        return LinearWithWarmupScheduler(t_warmup=cfg.t_warmup, alpha_f=cfg.alpha_f)
    else:
        raise DolmaConfigurationError(f"Not sure how to build scheduler: {cfg.name}")


def build_algorithm(name: str, kwargs: Dict[str, Any]):
    from composer import algorithms

    if name == "gradient_clipping":
        return algorithms.GradientClipping(**kwargs)
    #  elif name == 'alibi':
    #      return algorithms.Alibi(**kwargs)
    elif name == "fused_layernorm":
        return algorithms.FusedLayerNorm(**kwargs)
    elif name == "gated_linear_units":
        return algorithms.GatedLinearUnits(**kwargs)
    elif name == "low_precision_layernorm":
        return algorithms.LowPrecisionLayerNorm(**kwargs)
    else:
        raise ValueError(f"Not sure how to build algorithm: {name}")


def main(cfg: TrainConfig) -> None:
    from composer import Trainer
    from composer.core import Callback
    from composer.loggers import WandBLogger
    from composer.loggers.logger_destination import LoggerDestination
    from composer.utils import dist, get_device, reproducibility

    from dolma.composer import ComposerDolmaGPT, DolmaConsoleLogger, SpeedMonitorMFU

    echo.info("Configuration:", cfg, rank_zero_only=True)

    reproducibility.seed_all(cfg.seed)
    dist.initialize_dist(get_device(None))

    # Run name.
    if cfg.run_name is None:
        cfg.run_name = os.environ.get("COMPOSER_RUN_NAME", "llm")

    # Update batch size info.
    update_batch_size_info(cfg)
    assert isinstance(cfg.device_train_batch_size, int)
    echo.info(
        f"Using per-device training batch size of {cfg.device_train_batch_size} "
        f"for global batch size of {cfg.global_train_batch_size}",
        rank_zero_only=True,
    )

    # Model.
    model = ComposerDolmaGPT(cfg.model)
    echo.info(f"Total number of parameters: {model.model.num_params():,d}", rank_zero_only=True)
    echo.info(
        f"Number of non-embedding parameters: {model.model.num_params(include_embedding=False):,d}",
        rank_zero_only=True,
    )

    # Optimizer.
    optimizer = model.model.configure_optimizer(**cfg.optimizer.asdict())

    # Scheduler.
    scheduler = build_scheduler(cfg.scheduler)

    # Dataset / data loader.
    train_loader = build_dataloader(cfg, cfg.device_train_batch_size)

    # Algorithms.
    algorithms = [build_algorithm(name, algorithm_cfg) for name, algorithm_cfg in (cfg.algorithms or {}).items()]

    # Callbacks.
    callbacks: List[Callback] = [SpeedMonitorMFU()]

    # Loggers.
    loggers: List[LoggerDestination] = [DolmaConsoleLogger(log_interval=cfg.console_log_interval)]
    if cfg.wandb is not None:
        loggers.append(WandBLogger(**cfg.wandb.asdict()))

    # Trainer.
    trainer = Trainer(
        run_name=cfg.run_name,
        seed=cfg.seed,
        model=model,
        train_dataloader=train_loader,
        optimizers=optimizer,
        schedulers=scheduler,
        #  eval_dataloader=evaluators,
        #  eval_interval=cfg.eval_interval,
        #  eval_subset_num_batches=cfg.get('eval_subset_num_batches', -1),
        max_duration=cfg.max_duration,
        precision=cfg.precision,
        device_train_microbatch_size=cfg.device_train_microbatch_size,
        fsdp_config=cfg.fsdp_config,
        save_folder=cfg.save_folder,
        save_interval=cfg.save_interval,
        save_num_checkpoints_to_keep=cfg.save_num_checkpoints_to_keep,
        save_overwrite=cfg.save_overwrite,
        load_path=cfg.load_path,
        load_weights_only=cfg.load_weights_only,
        callbacks=callbacks,
        loggers=loggers,
        algorithms=algorithms,
        progress_bar=False,
        log_to_console=False,
        console_log_interval=cfg.console_log_interval,
    )

    device_id = trainer.state.device.name.upper()
    if device_id == "GPU":
        device_id += f" {torch.cuda.current_device()}"
    echo.info(
        f"Local rank: {dist.get_local_rank()}/{dist.get_local_world_size()}, "
        f"global rank: {dist.get_global_rank()}/{dist.get_world_size()}, "
        f"training on {device_id}"
    )

    if not cfg.dry_run:
        trainer.fit()

    trainer.close()

    if cfg.dry_run:
        echo.success("Dry run complete", rank_zero_only=True)
    else:
        echo.success("Training complete", rank_zero_only=True)


if __name__ == "__main__":
    prepare_cli_environment()

    try:
        yaml_path, args_list = sys.argv[1], sys.argv[2:]
    except IndexError:
        raise DolmaCliError(f"Usage: {sys.argv[0]} [CONFIG_PATH] [OPTIONS]")

    cfg = TrainConfig.load(yaml_path, [clean_opt(s) for s in args_list])
    main(cfg)