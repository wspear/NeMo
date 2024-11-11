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
import lightning.pytorch as pl
import torch
from megatron.core.distributed import DistributedDataParallelConfig
from lightning.pytorch.callbacks.callback import Callback

from nemo import lightning as nl
from nemo.collections import llm
from nemo.collections.llm.api import finetune, pretrain
from nemo.collections.llm.gpt.data.mock import MockDataModule
from nemo.collections.llm.recipes.log.default import default_log, default_resume, tensorboard_logger
from nemo.collections.llm.recipes.optim.adam import distributed_fused_adam_with_cosine_annealing
from nemo.collections.llm.recipes.precision.mixed_precision import bf16_mixed
from nemo.collections.nlp.modules.common.tokenizer_utils import get_nmt_tokenizer
from nemo.utils.exp_manager import TimingCallback

NAME = "mamba2_hybrid_8b"


@run.cli.factory(name=NAME)
def tokenizer(tokenizer_model: str = None) -> run.Config[pl.LightningModule]:

    return run.Config(
        get_nmt_tokenizer,
        library='megatronNVIDIAMambaConfig8B',
        model_name="GPTSentencePieceTokenizer",
        tokenizer_model=tokenizer_model,
        use_fast=True,
    )


@run.cli.factory(name=NAME)
def model(tokenizer_model: str = None) -> run.Config[pl.LightningModule]:
    """
    Factory function to create a Mamba2 Hybrid 8B model configuration.

    Returns:
        run.Config[pl.LightningModule]: Configuration for the Mamba2 Hybrid 8B model.

    Examples:
        CLI usage:
            $ nemo llm pretrain model=mamba2_hybrid_8b ...

        Python API usage:
            >>> model_config = model()
            >>> print(model_config)
    """
    return run.Config(
        llm.GPTModel,
        config=run.Config(llm.NVIDIAMambaHybridConfig8B),
        tokenizer=tokenizer(tokenizer_model=tokenizer_model),
    )


def trainer(
    tensor_parallelism: int = 8,
    pipeline_parallelism: int = 1,
    pipeline_parallelism_type: Optional[torch.dtype] = None,
    virtual_pipeline_parallelism: Optional[int] = None,
    context_parallelism: int = 1,
    sequence_parallelism: bool = False,
    num_nodes: int = 1,
    num_gpus_per_node: int = 8,
    max_steps: int = 1168251,
    callbacks: Optional[list[run.Config[Callback]]] = None,
) -> run.Config[nl.Trainer]:
    """
    Configure the NeMo Lightning Trainer for Mamba2 Hybrid 8B model.

    This function sets up the distributed training strategy and other training parameters.

    Args:
        tensor_parallelism (int): Degree of tensor model parallelism.
        pipeline_parallelism (int): Degree of pipeline model parallelism.
        pipeline_parallelism_type (Optional[torch.dtype]): Data type for pipeline parallelism.
        virtual_pipeline_parallelism (Optional[int]): Size of virtual pipeline parallelism.
        context_parallelism (int): Degree of context parallelism.
        sequence_parallelism (bool): Whether to use sequence parallelism.
        num_nodes (int): Number of compute nodes to use.
        num_gpus_per_node (int): Number of GPUs per node.
        max_steps (int): Maximum number of training steps.
        callbacks (Optional[list[run.Config[Callback]]]): List of callback configurations.

    Returns:
        run.Config[nl.Trainer]: Configuration for the NeMo Lightning Trainer.

    Examples:
        CLI usage:
            $ nemo llm pretrain trainer=mamba2_hybrid_8b ...

        Python API usage:
            >>> trainer_config = trainer(num_nodes=1, num_gpus_per_node=1)
            >>> print(trainer_config)

    Note:
        For more information on distributed training strategies, refer to the
        NeMo documentation on multi-GPU and multi-node training.
    """
    strategy = run.Config(
        nl.MegatronStrategy,
        tensor_model_parallel_size=tensor_parallelism,
        pipeline_model_parallel_size=pipeline_parallelism,
        pipeline_dtype=pipeline_parallelism_type,
        virtual_pipeline_model_parallel_size=virtual_pipeline_parallelism,
        context_parallel_size=context_parallelism,
        sequence_parallel=sequence_parallelism,
        gradient_as_bucket_view=True,
        ckpt_async_save=False,
        ckpt_parallel_load=True,
        ddp=run.Config(
            DistributedDataParallelConfig,
            check_for_nan_in_grad=True,
            grad_reduce_in_fp32=True,
            overlap_grad_reduce=True,
            overlap_param_gather=True,
        ),
    )

    trainer = run.Config(
        nl.Trainer,
        accelerator="gpu",
        accumulate_grad_batches=1,
        callbacks=callbacks,
        devices=num_gpus_per_node,
        limit_test_batches=50,
        limit_val_batches=32,
        log_every_n_steps=10,
        max_steps=max_steps,
        num_nodes=num_nodes,
        plugins=bf16_mixed(),
        strategy=strategy,
        use_distributed_sampler=False,
        val_check_interval=2000,
    )

    return trainer


@run.cli.factory(target=pretrain, name=NAME)
def pretrain_recipe(
    dir: Optional[str] = None,
    name: str = "default",
    tokenizer_model: str = None,
    num_nodes: int = 1,
    num_gpus_per_node: int = 8,
    fn=pretrain,
) -> run.Partial:
    """
    Create a pre-training recipe for Mamba2 Hybrid 8B model.

    This function sets up a complete configuration for pre-training, including
    model, trainer, data, logging, optimization, and resumption settings.

    Args:
        dir (Optional[str]): Directory for saving logs and checkpoints.
        name (str): Name of the pre-training run.
        num_nodes (int): Number of compute nodes to use.
        num_gpus_per_node (int): Number of GPUs per node.
        fn (Callable): The pre-training function to use.

    Returns:
        run.Partial: Partial configuration for pre-training.

    Examples:
        CLI usage:
            $ nemo llm pretrain --factory mamba2_hybrid_8b
            $ nemo llm pretrain --factory "mamba2_hybrid_8b(num_nodes=1, name='my_pretrain')"

        Python API usage:
            >>> recipe = pretrain_recipe(name="mamba2_hybrid_8b_pretrain", num_nodes=1)
            >>> print(recipe)

    Note:
        For more details on pre-training LLMs with NeMo, see the pre-training
        guide in the `examples/llm/pretrain/` directory.
    """
    return run.Partial(
        fn,
        model=model(),
        trainer=trainer(
            num_nodes=num_nodes,
            num_gpus_per_node=num_gpus_per_node,
            callbacks=[run.Config(TimingCallback)],
        ),
        data=run.Config(
            MockDataModule,
            seq_length=4096,
            global_batch_size=8,
            micro_batch_size=1,
            tokenizer=tokenizer(tokenizer_model=tokenizer_model),
        ),
        log=default_log(dir=dir, name=name, tensorboard_logger=tensorboard_logger(name=name)),
        optim=distributed_fused_adam_with_cosine_annealing(max_lr=3e-4),
        resume=default_resume(),
    )


@run.cli.factory(target=finetune, name=NAME)
def finetune_recipe(
    resume_path,
    tokenizer_model,
    dir: Optional[str] = None,
    name: str = "default",
    num_nodes: int = 1,
    num_gpus_per_node: int = 8,
    gbs: int = 8,
    mbs: int = 1,
    peft_scheme: Optional[str] = 'none',
) -> run.Partial:
    """
    Create a fine-tuning recipe for Mamba2 Hybrid 8B model.

    This function sets up a complete configuration for fine-tuning, including
    model, trainer, data, logging, optimization, and resumption settings.

    Args:
        dir (Optional[str]): Directory for saving logs and checkpoints.
        name (str): Name of the fine-tuning run.
        resume_path (str): Path to the NeMo checkpoint (refer to notes below
                            on how to convert a pytorch checkpoint to NeMo)
        tokenizer_model (str): Path to tokenizer model (defaults to None)
        num_nodes (int): Number of compute nodes to use.
        num_gpus_per_node (int): Number of GPUs per node.
    Returns:
        run.Partial: Partial configuration for fine-tuning.

    Examples:
        CLI usage:
            $ nemo llm finetune --factory mamba2_hybrid_8b

        Python API usage:
            >>> recipe = finetune_recipe(name="mamba2_hybrid_8b_finetune", num_nodes=1)
            >>> print(recipe)

    Note:
        This recipe uses the SQuAD dataset for fine-tuning. For more information
        on fine-tuning LLMs with NeMo, see the fine-tuning guide in the
        `examples/llm/finetune/` directory.
        For converting an SSM pytorch checkpoint, use the following line of python code:

        llm.GPTModel(llm.NVIDIAMambaHybridConfig8B(), tokenizer=tokenizer(tokenizer_model=tokenizer_model)).import_ckpt(
            path="pytorch://ABSOLUTE_PATH_TO_CKPT/your_pytorch_state_dict_file",
            model_config=llm.NVIDIAMambaHybridConfig8B())
        This line will cache the nemo checkpoint to following directory:
            /root/.cache/nemo/models/your_pytorch_state_dict_file

    """
    nemo_resume = run.Config(
        nl.AutoResume,
        restore_config=run.Config(nl.RestoreConfig, path=resume_path),
    )
    strategy = run.Config(
        nl.MegatronStrategy,
        tensor_model_parallel_size=8,
        pipeline_model_parallel_size=1,
        gradient_as_bucket_view=True,
        ckpt_load_optimizer=False,
        ckpt_save_optimizer=False,
        ckpt_async_save=False,
    )
    checkpoint_callback = run.Config(
        nl.ModelCheckpoint,
        every_n_train_steps=10,
        dirpath=dir,
    )
    trainer = run.Config(
        nl.Trainer,
        accelerator="gpu",
        accumulate_grad_batches=1,
        devices=num_gpus_per_node,
        limit_test_batches=10,
        limit_val_batches=10,
        log_every_n_steps=20,
        max_steps=100,
        num_nodes=num_nodes,
        plugins=run.Config(
            nl.MegatronMixedPrecision,
            precision="bf16-mixed",
            params_dtype=torch.bfloat16,
        ),
        callbacks=[checkpoint_callback],
        strategy=strategy,
        use_distributed_sampler=False,
        val_check_interval=20,
    )
    recipe = run.Partial(
        llm.finetune,
        model=model(tokenizer_model=tokenizer_model),
        trainer=trainer,
        data=run.Config(
            llm.SquadDataModule,
            seq_length=2048,
            global_batch_size=gbs,
            micro_batch_size=mbs,
            tokenizer=tokenizer(tokenizer_model=tokenizer_model),
        ),
        log=llm.default_log(dir=dir, name=name, tensorboard_logger=tensorboard_logger(name=name)),
        optim=distributed_fused_adam_with_cosine_annealing(max_lr=1e-4, min_lr=0, warmup_steps=50),
        resume=nemo_resume,
    )
    if peft_scheme is None or peft_scheme.lower() == 'none':
        recipe.trainer.strategy.tensor_model_parallel_size = 8
        recipe.optim.config.lr = 5e-6
    else:
        raise ValueError(f"Unrecognized peft scheme: {peft_scheme}")
    return recipe
