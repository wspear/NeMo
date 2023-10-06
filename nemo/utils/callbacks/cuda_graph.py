# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from dataclasses import dataclass
from types import MethodType
from typing import Any, Dict, List, Optional, Type

import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks import Callback
from pytorch_lightning.loops.optimization.optimizer_loop import ClosureResult
from pytorch_lightning.utilities.rank_zero import rank_zero_info, rank_zero_warn
from pytorch_lightning.utilities.signature_utils import is_param_in_hook_signature
from pytorch_lightning.utilities.types import STEP_OUTPUT
from torch.nn.parallel import DistributedDataParallel

__all__ = ["CUDAGraphCallback"]


def struct_copy_one(src):
    if isinstance(src, tuple):
        return tuple(struct_copy_one(i) for i in src)
    elif isinstance(src, list):
        return list(struct_copy_one(i) for i in src)
    elif isinstance(src, dict):
        return {k: struct_copy_one(src[k]) for k in src}
    elif isinstance(src, torch.Tensor):
        return src.clone().detach().cuda()
    else:
        return src


def struct_copy_two(tgt, src):
    if isinstance(src, tuple):
        raise Exception(f"Unsupported copy for tuple yet: {type(src)}")
    elif isinstance(src, list):
        for i in range(len(src)):
            if isinstance(src[i], (tuple, list, dict, torch.Tensor)):
                struct_copy_two(tgt[i], src[i])
            else:
                tgt[i] = src[i]
    elif isinstance(src, dict):
        for k in src:
            if isinstance(src[k], (tuple, list, dict, torch.Tensor)):
                struct_copy_two(tgt[k], src[k])
            else:
                tgt[k] = src[k]
    elif isinstance(src, torch.Tensor):
        tgt.copy_(src, non_blocking=True)
    else:
        raise Exception(f"Expect top-level as container type but got: {type(src)}")


class StaticBufferLoader:
    """Load data to static buffers."""

    def __init__(self, loader):
        self.loader = loader
        self.stream = torch.cuda.Stream()
        self.static = None

    def __iter__(self):
        for inputs in self.loader:
            if self.static is None:
                with torch.cuda.stream(self.stream):
                    self.static = struct_copy_one(inputs)

            with torch.cuda.stream(self.stream):
                struct_copy_two(self.static, inputs)
            torch.cuda.current_stream().wait_stream(self.stream)
            yield self.static

    def __len__(self):
        return len(self.loader)


class StaticBufferLRScheduler:
    """Make sure LR updated in static buffers."""

    def __init__(self, lr_scheduler):
        self.lr_scheduler = lr_scheduler

    def get_lr(self):
        lrs = self.lr_scheduler.get_lr()
        if not hasattr(self, "static_lrs"):
            self.static_lrs = lrs
        for i in range(len(lrs)):
            self.static_lrs[i].copy_(lrs[i])
        return self.static_lrs

    def step(self, epoch=None):
        self.lr_scheduler.step(epoch)


def zero_grad(optimizer, *args, **kwargs):
    # We invoke zero_grad before graph capturing.
    if torch.cuda.is_current_stream_capturing():
        rank_zero_info("CUDAGraphCallback: set optimizer.zero_grad as nop during graph capturing.")
    else:
        optimizer.__orig_zero_grad__(*args, **kwargs)


def get_optimizer_step(state):
    def optimizer_step(
        self,
        epoch,
        batch_idx,
        optimizer,
        optimizer_idx: int = 0,
        optimizer_closure=None,
        on_tpu: bool = False,
        using_lbfgs: bool = False,
    ) -> None:
        # Not all optimizer supports set_to_none.
        if not hasattr(optimizer, "support_set_to_none"):
            optimizer.support_set_to_none = is_param_in_hook_signature(
                optimizer.zero_grad, "set_to_none", explicit=True
            )
        if optimizer.support_set_to_none:
            zero_grad_kwargs = {"set_to_none": True}
        else:
            zero_grad_kwargs = {}

        if 0 <= state.current_iteration < state.capture_iteration or state.capture_iteration < 0:
            state.stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(state.stream):
                optimizer.zero_grad(**zero_grad_kwargs)
                self.__orig_optimizer_step__(
                    epoch,
                    batch_idx,
                    optimizer,
                    optimizer_idx=optimizer_idx,
                    optimizer_closure=optimizer_closure,
                    on_tpu=on_tpu,
                    using_lbfgs=using_lbfgs,
                )
            torch.cuda.current_stream().wait_stream(state.stream)

        if state.current_iteration == state.capture_iteration:
            optimizer.zero_grad(**zero_grad_kwargs)
            torch.cuda.synchronize()
            rank_zero_info("CUDAGraphCallback: capturing CUDA graph for module %s.", self.__class__.__name__)
            with torch.cuda.graph(state.graph):
                self.__orig_optimizer_step__(
                    epoch,
                    batch_idx,
                    optimizer,
                    optimizer_idx=optimizer_idx,
                    optimizer_closure=optimizer_closure,
                    on_tpu=on_tpu,
                    using_lbfgs=using_lbfgs,
                )
            torch.cuda.synchronize()

        # Graph replay and reconstruct missing result
        if state.current_iteration >= state.capture_iteration >= 0:
            state.graph.replay()
            optimizer_closure._result = ClosureResult.from_training_step_output(state.output)

        # If something is not capturable, try to put it there, e.g. `self.log()`.
        if hasattr(self, "non_cuda_graph_capturable"):
            self.non_cuda_graph_capturable()

        state.current_iteration += 1

    return optimizer_step


def get_training_step(state):
    def training_step(self, batch, batch_idx):
        results = self.__orig_training_step__(batch, batch_idx)
        if state.output is None:
            state.output = struct_copy_one(results)

        # Copy results to static buffer to rebuild states required by PL.
        with torch.no_grad():
            struct_copy_two(state.output, results)
        return results

    return training_step


def get_amp_autocast_init(state):
    def amp_autocast_init(self, *args, **kwargs):
        if "cache_enabled" not in kwargs:
            kwargs["cache_enabled"] = False
        if state.current_iteration == 0:
            rank_zero_info("CUDAGraphCallback: disable autocast cache.")
        return self.__orig_init__(*args, **kwargs)

    return amp_autocast_init


def get_ddp_init(state):
    def init(self, *args, **kwargs):
        rank_zero_info("CUDAGraphCallback: init DDP on side stream.")
        with torch.cuda.stream(state.stream):
            self.__orig_init__(*args, **kwargs)

    return init


@dataclass
class CUDAGraphState:
    current_iteration: int = 0
    capture_iteration: int = -1
    stream: torch.cuda.Stream = None
    graph: torch.cuda.CUDAGraph = None
    output: Any = None  # static forward output


class CUDAGraphCallback(Callback):
    """Full iteration CUDA graph callback.

    Dataloader and LR scheduler are not included in the CUDA graph with this callback.
    """

    def __init__(self, capture_iteration=-1):
        super().__init__()

        # Required by CUDA graph with DDP
        # Ref: https://pytorch.org/docs/stable/notes/cuda.html#usage-with-distributeddataparallel
        if 0 <= capture_iteration <= 11:
            raise Exception("Warmup must run at least 11 DDP-enabled eager iterations before capture.")
        os.environ["NCCL_ASYNC_ERROR_HANDLING"] = "0"

        self.state = CUDAGraphState(capture_iteration=capture_iteration, stream=torch.cuda.Stream(),)
        if capture_iteration > 0:
            self.state.graph = torch.cuda.CUDAGraph()

    def setup(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", stage: str) -> None:
        """Called when fit, validate, test, predict, or tune begins."""
        # Hack to avoid CUDA graph issue with AMP, PyTorch Lightning doesn't support
        # changing autocast arguments for now.
        # https://github.com/pytorch/pytorch/blob/v1.13.1/torch/cuda/graphs.py#L234
        torch.autocast.__orig_init__ = torch.autocast.__init__
        torch.autocast.__init__ = get_amp_autocast_init(self.state)

        # Before full-backward capture, DDP must be constructed in a side-stream context.
        # We've merged the change that init DDP on side stream to PyTorch Lightning V2,
        # but not all user defined strategy init DDP on side stream.
        DistributedDataParallel.__orig_init__ = DistributedDataParallel.__init__
        DistributedDataParallel.__init__ = get_ddp_init(self.state)

    def teardown(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", stage: str) -> None:
        """Called when fit, validate, test, predict, or tune ends."""
        torch.autocast.__init__ = torch.autocast.__orig_init__
        del torch.autocast.__orig_init__

        DistributedDataParallel.__init__ = DistributedDataParallel.__orig_init__
        del DistributedDataParallel.__orig_init__

    def on_fit_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        """Called when fit begins."""
        if is_param_in_hook_signature(pl_module, "dataloader_iter", explicit=True):
            raise Exception(
                "Found `dataloader_iter` argument in the `training_step`. This is "
                "not supported by full iteration CUDA graph capturing yet since "
                "dataloader will be within the CUDA graph capturing range.\n"
                "Try to change `dataloader_iter` to `batch` and remove "
                "`next(dataloader_iter)` from `training_step`."
            )

    def on_fit_end(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        """Called when fit ends."""
        pass

    def on_train_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        """Called when the train begins."""
        if self.state.capture_iteration < 0:
            return

        # Ensure training dataloader loads data to static buffer
        dataloader = trainer.train_dataloader.loaders
        assert isinstance(
            dataloader, torch.utils.data.dataloader.DataLoader
        ), f"Expect Dataloader type but got {type(dataloader)}"
        trainer.train_dataloader.__orig_dataloader__ = dataloader
        static_loader = StaticBufferLoader(dataloader)
        trainer.train_dataloader.loaders = static_loader

        # Warn if `optimizer.zero_grad()` invoked during graph capturing
        for optimizer in trainer.optimizers:
            assert isinstance(optimizer, torch.optim.Optimizer), f"Expect Optimizer type but got {type(optimizer)}"
            optimizer.__orig_zero_grad__ = optimizer.zero_grad
            optimizer.zero_grad = MethodType(zero_grad, optimizer)

        # Ensure LR scheduler writes to static buffer
        # We don't include LR scheduler in the full CUDA graph for now since
        # its overhead is very small.
        for config in trainer.lr_scheduler_configs:
            assert isinstance(
                config.scheduler, torch.optim.lr_scheduler._LRScheduler
            ), f"Expect _LRScheduler type but got {type(dataloader)}"
            config.__orig_scheduler__ = config.scheduler
            config.scheduler = StaticBufferLRScheduler(config.scheduler)

        # Save model outputs to static buffer for PL states reconstruct
        pl_module.__orig_training_step__ = pl_module.training_step
        training_step = get_training_step(self.state)
        pl_module.training_step = MethodType(training_step, pl_module)

        # Capture CUDA graph from model forward propagation to optimizer step
        pl_module.__orig_optimizer_step__ = pl_module.optimizer_step
        optimizer_step = get_optimizer_step(self.state)
        pl_module.optimizer_step = MethodType(optimizer_step, pl_module)

    def on_train_end(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        """Called when the train ends."""
        if self.state.capture_iteration < 0:
            return

        dataloader = trainer.train_dataloader.__orig_dataloader__
        trainer.train_dataloader.loaders = dataloader
        del trainer.train_dataloader.__orig_dataloader__

        for optimizer in trainer.optimizers:
            optimizer.zero_grad = optimizer.__orig_zero_grad__
            del optimizer.__orig_zero_grad__

        for config in trainer.lr_scheduler_configs:
            config.scheduler = config.__orig_scheduler__
            del config.__orig_scheduler__

        pl_module.training_step = pl_module.__orig_training_step__
        del pl_module.__orig_training_step__

        pl_module.optimizer_step = pl_module.__orig_optimizer_step__
        del pl_module.__orig_optimizer_step__

    def on_train_epoch_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        """Called when the train epoch begins."""
        pass

    def on_train_epoch_end(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        """Called when the train epoch ends.

        To access all batch outputs at the end of the epoch, either:

        1. Implement `training_epoch_end` in the `LightningModule` and access outputs via the module OR
        2. Cache data across train batch hooks inside the callback implementation to post-process in this hook.
        """
        pass

    def on_train_batch_start(
        self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", batch: Any, batch_idx: int
    ) -> None:
        """Called when the train batch begins."""
        pass

    def on_train_batch_end(
        self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", outputs: STEP_OUTPUT, batch: Any, batch_idx: int
    ) -> None:
        """Called when the train batch ends.

        Note:
            The value ``outputs["loss"]`` here will be the normalized value w.r.t ``accumulate_grad_batches`` of the
            loss returned from ``training_step``.
        """
        pass
