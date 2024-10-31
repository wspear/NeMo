# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
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

from typing import Optional

import nemo_run as run
import pytorch_lightning as pl
import torch

from nemo.collections.llm.api import finetune, pretrain
from nemo.collections.llm.gpt.data.mock import MockDataModule
from nemo.collections.llm.peft.lora import LoRA
from nemo.collections.llm.recipes.callbacks.default import straggler_det_callback
from nemo.collections.llm.recipes.finetune_default import default_finetune_recipe
from nemo.collections.llm.recipes.log.default import default_log, default_resume, tensorboard_logger
from nemo.collections.llm.recipes.optim.adam import distributed_fused_adam_with_cosine_annealing
from nemo.collections.llm.recipes.qwen2 import qwen2_model, qwen2_trainer
from nemo.utils.exp_manager import TimingCallback

NAME = "qwen2_1p5b"


@run.cli.factory(name=NAME)
def model() -> run.Config[pl.LightningModule]:
    """
    Factory function to create a Qwen2 1.5b model configuration.

    Returns:
        run.Config[pl.LightningModule]: Configuration for the Qwen2 1.5b model.

    Examples:
        CLI usage:
            $ nemo llm pretrain model=qwen2_1p5b ...

        Python API usage:
            >>> model_config = model()
            >>> print(model_config)
    """

    return qwen2_model(version=NAME)


@run.cli.factory(target=pretrain, name=NAME)
def pretrain_recipe(
    # General
    dir: Optional[str] = None,
    name: str = "default",
    # Trainer
    tensor_parallelism: int = 1,
    pipeline_parallelism: int = 1,
    pipeline_parallelism_type: Optional[torch.dtype] = None,
    virtual_pipeline_parallelism: Optional[int] = None,
    context_parallelism: int = 1,
    sequence_parallelism: bool = False,
    num_nodes: int = 1,
    num_gpus_per_node: int = 8,
    max_steps: int = 300000,
    precision: str = "bf16-mixed",
    accumulate_grad_batches: int = 1,
    gradient_clip_val: float = 1.0,
    limit_test_batches: int = 32,
    limit_val_batches: int = 32,
    log_every_n_steps: int = 10,
    val_check_interval: int = 500,
    # Data
    global_batch_size=32,
    micro_batch_size=2,
    seq_length=4096,
    # Optimizer
    warmup_steps=500,
    constant_steps=0,
    min_lr=3e-5,
    max_lr=3e-4,
    # Training function
    fn=pretrain,
) -> run.Partial:
    """
    Create a pre-training recipe for Qwen2 1.5b model.

    This function sets up a complete configuration for pre-training, including
    model, trainer, data, logging, optimization, and resumption settings.

    Args:
        dir (Optional[str]): Directory for saving logs and checkpoints.
        name (str): Name of the pre-training run.
        tensor_parallelism (int): Degree of tensor model parallelism.
        pipeline_parallelism (int): Degree of pipeline model parallelism.
        pipeline_parallelism_type (Optional[torch.dtype]): Data type for pipeline parallelism.
        virtual_pipeline_parallelism (Optional[int]): Size of virtual pipeline parallelism.
        context_parallelism (int): Degree of context parallelism.
        sequence_parallelism (bool): Whether to use sequence parallelism.
        num_nodes (int): Number of compute nodes to use.
        num_gpus_per_node (int): Number of GPUs per node.
        max_steps (int): Maximum number of training steps.
        precision (str): Precision configuration, one of fp32, 16-mixed or bf16-mixed.
        accumulate_grad_batches (int): Number of steps per gradient accumulation.
        gradient_clip_val (float): Value for gradient clipping.
        limit_test_batches (int): Limit the number of test batches.
        limit_val_batches (int): Limit the number of validation batches.
        log_every_n_steps (int): Log every n steps.
        val_check_interval (int): Run validation every N steps.
        global_batch_size (int): Global batch size.
        micro_batch_size (int): Micro batch size.
        seq_length (int): Sequence length.
        warmup_steps (int): Number of warmup steps.
        constant_steps (int): Number of constant steps.
        min_lr (float): Minimum learning rate.
        max_lr (float): Maximum learning rate.
        fn (Callable): The pre-training function to use.

    Returns:
        run.Partial: Partial configuration for pre-training.

    Examples:
        CLI usage:
            $ nemo llm pretrain --factory qwen2_1p5b
            $ nemo llm pretrain --factory "qwen2_1p5b(num_nodes=1, name='my_qwen2_pretrain')"

        Python API usage:
            >>> recipe = pretrain_recipe(name="qwen2_pretrain", num_nodes=1)
            >>> print(recipe)

    Note:
        This recipe uses a mock dataset, look for the finetune examples to see how to change the dataset.
    """
    return run.Partial(
        fn,
        model=model(),
        trainer=qwen2_trainer(
            tensor_parallelism=tensor_parallelism,
            pipeline_parallelism=pipeline_parallelism,
            pipeline_parallelism_type=pipeline_parallelism_type,
            virtual_pipeline_parallelism=virtual_pipeline_parallelism,
            context_parallelism=context_parallelism,
            sequence_parallelism=sequence_parallelism,
            num_nodes=num_nodes,
            num_gpus_per_node=num_gpus_per_node,
            max_steps=max_steps,
            precision=precision,
            accumulate_grad_batches=accumulate_grad_batches,
            limit_test_batches=limit_test_batches,
            limit_val_batches=limit_val_batches,
            log_every_n_steps=log_every_n_steps,
            val_check_interval=val_check_interval,
            callbacks=[run.Config(TimingCallback), straggler_det_callback()],
        ),
        data=run.Config(
            MockDataModule,
            seq_length=seq_length,
            global_batch_size=global_batch_size,
            micro_batch_size=micro_batch_size,
        ),
        log=default_log(dir=dir, name=name, tensorboard_logger=tensorboard_logger(name=name)),
        optim=distributed_fused_adam_with_cosine_annealing(
            precision=precision,
            warmup_steps=warmup_steps,
            constant_steps=constant_steps,
            min_lr=min_lr,
            max_lr=max_lr,
            clip_grad=gradient_clip_val,
        ),
        resume=default_resume(),
    )


@run.cli.factory(target=finetune, name=NAME)
def finetune_recipe(
    dir: Optional[str] = None,
    name: str = "default",
    num_nodes: int = 1,
    num_gpus_per_node: int = 8,
    peft_scheme: Optional[str] = 'lora',
    packed_sequence: bool = False,
) -> run.Partial:
    """
    Create a fine-tuning recipe for Qwen2 1.5b model.

    This function sets up a complete configuration for fine-tuning, including
    model, trainer, data, logging, optimization, and resumption settings.
    The recipe uses LoRA (Low-Rank Adaptation) for efficient fine-tuning, unless peft_scheme is set to None.

    Args:
        dir (Optional[str]): Directory for saving logs and checkpoints.
        name (str): Name of the fine-tuning run.
        num_nodes (int): Number of compute nodes to use.
        num_gpus_per_node (int): Number of GPUs per node.
        peft_scheme (Optional[str]): Name of the peft scheme to use for fine-tuning. Allowed values: 'lora', 'none'/None.
        packed_sequence (Optional[bool]): Packing multiple training sequences into one long sequence for training efficiency. Default sequence length is 2048.

    Returns:
        run.Partial: Partial configuration for fine-tuning.

    Examples:
        CLI usage:
            $ nemo llm finetune --factory qwen2_1p5b

        Python API usage:
            >>> recipe = finetune_recipe(name="qwen2_1p5b_finetune", num_nodes=2)
            >>> print(recipe)

    Note:
        This recipe uses the SQuAD dataset for fine-tuning. For more information
        on fine-tuning LLMs with NeMo, see the fine-tuning guide in the
        `examples/llm/finetune/` directory.
    """
    recipe = default_finetune_recipe(
        model(), "Qwen/Qwen2-1.5B", dir, name, num_nodes, num_gpus_per_node, packed_sequence
    )
    if peft_scheme is None or peft_scheme.lower() == 'none':
        recipe.optim.config.lr = 5e-6
    elif peft_scheme.lower() == 'lora':
        recipe.peft = run.Config(LoRA)
        recipe.optim.config.lr = 1e-4
    else:
        raise ValueError(f"Unrecognized peft scheme: {peft_scheme}")
    return recipe
