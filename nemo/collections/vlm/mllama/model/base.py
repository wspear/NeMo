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

import copy
import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import pytorch_lightning as L
import torch
import torch.distributed
from einops import rearrange
from megatron.core.enums import ModelType
from megatron.core.models.vision.multimodal_projector import MultimodalProjector
from megatron.core.optimizer import OptimizerConfig
from megatron.core.tensor_parallel.layers import ColumnParallelLinear
from megatron.core.transformer import MegatronModule
from megatron.core.transformer.mlp import MLPSubmodules
from megatron.core.transformer.spec_utils import ModuleSpec
from megatron.core.transformer.transformer_config import TransformerConfig
from PIL import Image as PIL_Image
from torch import nn

from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.collections.llm import fn
from nemo.collections.llm.gpt.model import local_layer_spec, transformer_engine_layer_spec
from nemo.collections.llm.gpt.model.base import get_batch_on_this_context_parallel_rank, get_packed_seq_params
from nemo.collections.llm.gpt.model.llama import Llama31Config, apply_rope_scaling
from nemo.collections.vlm.mllama.model.language import CrossAttentionTextModel
from nemo.collections.vlm.mllama.model.utils import _generate_cross_attention_mask, _pad_attention_masks
from nemo.collections.vlm.mllama.model.vision import VisionEncoder
from nemo.lightning import get_vocab_size, io
from nemo.lightning.megatron_parallel import MaskedTokenLossReduction
from nemo.lightning.pytorch.optim import MegatronOptimizerModule, OptimizerModule
from nemo.utils import logging


def llama_data_step(dataloader_iter) -> Dict[str, torch.Tensor]:
    from megatron.core import parallel_state

    # Based on: https://github.com/NVIDIA/Megatron-LM/blob/main/pretrain_gpt.py#L87
    # https://github.com/NVIDIA/NeMo/blob/main/nemo/collections/nlp/models/language_modeling/megatron_gpt_model.py#L828-L842

    batch = next(dataloader_iter)

    _batch: dict
    if isinstance(batch, tuple) and len(batch) == 3:
        _batch = batch[0]
    else:
        _batch = batch

    required_keys = set()
    required_keys.update(
        (
            "attention_mask",
            "tokens",
            "batch_masks",
            "position_ids",
            "num_chunks",
        )
    )
    if parallel_state.is_pipeline_first_stage():
        required_keys.update(
            (
                "batch_images",
                "aspect_ratio_ids",
            )
        )
    if parallel_state.is_pipeline_last_stage():
        required_keys.update(
            (
                "labels",
                "loss_mask",
            )
        )

    _batch = {
        key: val.cuda(non_blocking=True) if key in required_keys and isinstance(val, torch.Tensor) else val
        for key, val in _batch.items()
    }
    # slice batch along sequence dimension for context parallelism
    output = get_batch_on_this_context_parallel_rank(_batch)

    return output


def llama_forward_step(model, batch) -> torch.Tensor:
    forward_config = {
        "batch_images": batch["batch_images"],
        "batch_masks": batch["batch_masks"],
        "tokens": batch["tokens"],
        "position_ids": batch["position_ids"],
        "aspect_ratio_ids": batch["aspect_ratio_ids"],
        "num_chunks": batch["num_chunks"],
        "labels": batch.get("labels", None),
    }

    if 'cu_seqlens' in batch:
        forward_config['packed_seq_params'] = get_packed_seq_params(batch)

    return model(**forward_config)


def set_input_tensor(self, tensor):
    pass


@dataclass
class CrossAttentionVisionConfig(TransformerConfig, io.IOMixin):
    # core params

    bias_activation_fusion: bool = True
    bias_dropout_add_fusion: bool = True

    # vision model params
    num_layers: int = 32
    hidden_size: int = 1280
    num_attention_heads: int = 16
    vision_chunk_size: int = -1  # image resolution for image models
    vision_max_num_chunks: int = 4
    num_global_layers: int = 8
    max_num_tiles: int = 4
    text_hidden_size: int = 4096
    hidden_dropout: float = 0.0
    attention_dropout: float = 0.0
    ffn_dropout: float = 0.0
    gated: bool = False
    supported_aspect_ratios: Tuple[Tuple[int, int], ...] = (
        (1, 1),
        (1, 2),
        (1, 3),
        (1, 4),
        (2, 1),
        (2, 2),
        (3, 1),
        (4, 1),
    )

    @property
    def max_aspect_ratio_id(self) -> int:
        return len(self.supported_aspect_ratios)

    def configure_model(self) -> "CrossAttentionVisionModel":
        return CrossAttentionVisionModel(
            self,
        )


@dataclass
class CrossAttentionTextConfig(Llama31Config):
    rotary_base: int = 500_000
    seq_length: int = 8192
    num_layers: int = 32
    hidden_size: int = 4096
    ffn_hidden_size: int = 14336
    num_attention_heads: int = 32
    num_cross_attention_layers: int = 8
    vocab_size: int = 128256
    apply_rope_fusion: bool = False

    def _init_fusion_schedule(self, num_layers: int) -> List[int]:
        llama_layers = list(range(self.num_layers))
        # uniformly spread the layers
        k = math.ceil(len(llama_layers) / num_layers)
        return llama_layers[::-1][::k][:num_layers][::-1]

    def configure_model(self, tokenizer, pre_process=True, post_process=True):
        self.fusion_schedule = self._init_fusion_schedule(self.num_cross_attention_layers)
        vp_size = self.virtual_pipeline_model_parallel_size
        if vp_size:
            p_size = self.pipeline_model_parallel_size
            assert (
                self.num_layers // p_size
            ) % vp_size == 0, "Make sure the number of model chunks is the same across all pipeline stages."

        transformer_layer_spec = self.transformer_layer_spec
        if not isinstance(transformer_layer_spec, ModuleSpec):
            transformer_layer_spec = transformer_layer_spec(self)

        if hasattr(self, 'vocab_size'):
            vocab_size = self.vocab_size
            logging.info(
                f"Use preset vocab_size: {vocab_size}, original vocab_size: {tokenizer.vocab_size}, dummy tokens:"
                f" {vocab_size - tokenizer.vocab_size}."
            )
        else:
            vocab_size = get_vocab_size(self, tokenizer.vocab_size, self.make_vocab_size_divisible_by)

        model = CrossAttentionTextModel(
            self,
            transformer_layer_spec=transformer_layer_spec,
            vocab_size=vocab_size,
            max_sequence_length=self.seq_length,
            fp16_lm_cross_entropy=self.fp16_lm_cross_entropy,
            parallel_output=self.parallel_output,
            share_embeddings_and_output_weights=self.share_embeddings_and_output_weights,
            position_embedding_type=self.position_embedding_type,
            rotary_percent=self.rotary_percent,
            rotary_base=self.rotary_base,
            seq_len_interpolation_factor=self.seq_len_interpolation_factor,
            pre_process=pre_process,
            post_process=post_process,
        )
        model.rotary_pos_emb.inv_freq = apply_rope_scaling(
            model.rotary_pos_emb.inv_freq,
            factor=self.scale_factor,
            low_freq_factor=self.low_freq_factor,
            high_freq_factor=self.high_freq_factor,
            old_context_len=self.old_context_len,
        )
        return model


@dataclass
class MLlamaModelConfig(TransformerConfig, io.IOMixin):
    language_model_config: Optional[CrossAttentionTextConfig] = None
    vision_model_config: Optional[CrossAttentionVisionConfig] = None

    encoder_pipeline_model_parallel_size: int = 0
    encoder_tensor_model_parallel_size: int = 1
    vision_num_cross_attention_layers: int = -1
    num_layers: int = 1  # Placeholder, NOT used!
    num_attention_heads: int = 8  # Placeholder, NOT used!

    language_model_from_pretrained: Optional[str] = None  # TODO
    vision_model_from_pretrained: Optional[str] = None  # TODO

    forward_step_fn: Callable = llama_forward_step
    data_step_fn: Callable = llama_data_step

    def __post_init__(self):
        model_config_attr = [
            'num_layers',
            'hidden_size',
            'num_attention_heads',
            'num_query_groups',
            'ffn_hidden_size',
            'kv_channels',
            'hidden_dropout',
            'attention_dropout',
            'fp32_residual_connection',
            'apply_residual_connection_post_layernorm',
            'layernorm_epsilon',
            'layernorm_zero_centered_gamma',
            'add_bias_linear',
            'add_qkv_bias',
            'gated_linear_unit',
            'activation_func',
            'activation_func_fp8_input_store',
            'num_moe_experts',
            'rotary_interleaved',
            'window_size',
            'normalization',
            'qk_layernorm',
            'test_mode',
            'calculate_per_token_loss',
        ]

        if self.language_model_config is not None:
            for attr in model_config_attr:
                setattr(self, attr, getattr(self.language_model_config, attr))

    def configure_model(self, tokenizer) -> "MLlamaBaseModel":
        from megatron.core import parallel_state as ps

        self.language_model_config.tensor_model_parallel_size = self.tensor_model_parallel_size
        self.vision_model_config.tensor_model_parallel_size = self.tensor_model_parallel_size
        self.language_model_config.pipeline_model_parallel_size = self.pipeline_model_parallel_size

        if self.encoder_pipeline_model_parallel_size > 0:
            assert self.encoder_pipeline_model_parallel_size == 1, "ViT can only live on 1 pipeline stage."
            self.vision_model_config.pipeline_model_parallel_size = self.encoder_pipeline_model_parallel_size
            self.language_model_config.encoder_pipeline_model_parallel_size = self.encoder_pipeline_model_parallel_size
            if self.encoder_tensor_model_parallel_size > 0:
                self.vision_model_config.tensor_model_parallel_size = self.encoder_tensor_model_parallel_size

        model = MLlamaBaseModel(
            config=self,
            tokenizer=tokenizer,
            pre_process=ps.is_pipeline_first_stage()
            or ps.get_pipeline_model_parallel_rank() == self.encoder_pipeline_model_parallel_size,
            post_process=ps.is_pipeline_last_stage(),
            add_encoder=ps.is_pipeline_first_stage(),
            add_decoder=ps.is_pipeline_last_stage()
            or ps.get_pipeline_model_parallel_rank() >= self.encoder_pipeline_model_parallel_size,
        )

        return model


class CrossAttentionVisionModel(MegatronModule):
    def __init__(self, config) -> None:
        super().__init__(config=config)
        return_intermediate = "3,7,15,23,30"
        self.vision_input_dim = 1280
        self.image_res = config.vision_chunk_size
        self.max_num_chunks = config.vision_max_num_chunks
        if return_intermediate is not None:
            return_intermediate = [int(l) for l in return_intermediate.split(",")]
            self.vision_input_dim = (len(return_intermediate) + 1) * self.vision_input_dim
        self.patch_size = 14
        self.vision_encoder = VisionEncoder(
            config=config,
            image_size=config.vision_chunk_size,
            patch_size=self.patch_size,
            return_intermediate=return_intermediate,
        ).to(config.params_dtype)

        projection_config = copy.deepcopy(config)
        projection_config.hidden_size = config.text_hidden_size
        affine_layer_spec = MLPSubmodules(linear_fc1=ColumnParallelLinear, linear_fc2=None)
        self.vision_projection = MultimodalProjector(
            config=projection_config,
            submodules=affine_layer_spec,
            projector_type="affine",
            input_size=self.vision_input_dim,
        )
        self.vision_projection.encoder.skip_bias_add = False  # Temporary fix for a MCore side bug

    def forward(self, images: torch.Tensor, aspect_ratio_ids: torch.Tensor) -> torch.Tensor:
        # vision_tokens: (B, T, D)
        # aspect_ratio_ids: (B, 1)
        # h: (B, T, D)
        vision_tokens = self.vision_encoder(images.to(dtype=torch.bfloat16), aspect_ratio_ids)
        vision_shape = vision_tokens.shape
        vision_tokens = self.vision_projection(vision_tokens.reshape(-1, *vision_shape[-2:]))
        vision_tokens = vision_tokens.reshape(*vision_shape[:-1], -1)
        return vision_tokens

    def set_input_tensor(self, tensor):
        pass


class MLlamaBaseModel(MegatronModule):
    def __init__(
        self,
        config: MLlamaModelConfig,
        tokenizer: Optional = None,
        pre_process: bool = True,
        post_process: bool = True,
        add_encoder: bool = True,
        add_decoder: bool = True,
    ) -> None:
        super().__init__(config=config)

        language_model_config = config.language_model_config
        vision_model_config = config.vision_model_config
        self.pre_process = pre_process
        self.post_process = post_process

        self.encoder_hidden_state = None
        self.vision_model: Optional[CrossAttentionVisionModel] = None
        self.language_model: Optional[CrossAttentionTextModel] = None

        self.share_embeddings_and_output_weights = False
        self.add_decoder = (language_model_config is not None) and add_decoder
        self.add_encoder = (vision_model_config is not None) and add_encoder

        if self.add_decoder:
            self.language_model = language_model_config.configure_model(
                tokenizer=tokenizer, pre_process=pre_process, post_process=post_process
            )
            self.share_embeddings_and_output_weights = self.language_model.share_embeddings_and_output_weights

        if self.add_encoder:
            self.vision_model = vision_model_config.configure_model()

        self.model_type = ModelType.encoder_and_decoder
        self.xattn_needed = True

        self.patch_size = 14
        self.image_res = vision_model_config.vision_chunk_size
        self.max_num_chunks = vision_model_config.vision_max_num_chunks

    def setup_cache(self, max_batch_size: int, dtype: torch.dtype):
        self.language_model.setup_cache(max_batch_size, dtype)

    def compute_xattn_caches_masks(
        self,
        vision_tokens: torch.Tensor,
        vision_orig_shape: Tuple[int, int, int, int, int],
        batch_masks: torch.Tensor,
        num_chunks: torch.Tensor,
        total_len: int,
    ) -> Tuple[List, torch.Tensor, torch.Tensor]:
        bsz, nimg, nchunk, ntok, image_token_dim = vision_orig_shape

        xattn_caches = [
            layer.compute_xattn_kv_cache(vision_tokens) for layer in self.language_model.decoder.xattn_layers
        ]

        padded_masks = _pad_attention_masks(
            batch_masks,
            num_chunks,
            total_len,
            self.max_num_chunks,
            vision_tokens.device,
        )
        vision_tokens = rearrange(
            vision_tokens, "(nimg nchk ntok) b dim -> b nimg nchk ntok dim", nimg=nimg, nchk=nchunk, ntok=ntok
        )
        cross_attention_masks, full_text_row_masked_out_mask = _generate_cross_attention_mask(
            text_token_count=total_len,
            text_device="cuda",
            text_dtype=next(self.language_model.parameters()).dtype,
            vision_tokens=vision_tokens,
            cross_attention_masks=padded_masks,
        )

        return (xattn_caches, cross_attention_masks, full_text_row_masked_out_mask)

    def forward(
        self,
        position_ids: torch.Tensor,
        tokens: torch.Tensor,
        labels: Optional[torch.Tensor] = None,
        batch_images: Optional[torch.Tensor] = None,
        batch_masks: Optional[torch.Tensor] = None,
        num_chunks: Optional[torch.Tensor] = None,
        aspect_ratio_ids: Optional[torch.Tensor] = None,
        cross_attention_masks: Optional[torch.Tensor] = None,
        full_text_row_masked_out_mask: Optional[torch.Tensor] = None,
        xattn_caches: Optional[List] = None,
    ) -> torch.Tensor:
        if xattn_caches is None:
            bsz, max_num_images = batch_images.size(0), batch_images.size(1)
            vision_orig_shape = (
                bsz,
                max_num_images,
                self.max_num_chunks,
                int((self.image_res / self.patch_size) ** 2 + 1),
                self.config.hidden_size,
            )
            skip_vision_encoder = False
            logging.warning("NeMo Mllama always pad images to max number of tiles. The fix is coming soon!")
            num_chunks[num_chunks > 0] = self.max_num_chunks
            if max_num_images == 0:
                skip_vision_encoder = True

            if self.encoder_hidden_state is not None:
                vision_tokens = self.encoder_hidden_state
            else:
                if skip_vision_encoder:
                    vision_tokens = torch.zeros(
                        vision_orig_shape,
                        device="cuda",
                        dtype=torch.bfloat16,
                    )
                else:
                    vision_tokens = self.vision_model(batch_images, aspect_ratio_ids)
                    vision_tokens = rearrange(
                        vision_tokens, "b nimg nchk ntok dim -> (nimg nchk ntok) b dim"
                    ).contiguous()

            if not self.add_decoder:
                return vision_tokens

            xattn_caches, cross_attention_masks, full_text_row_masked_out_mask = self.compute_xattn_caches_masks(
                vision_tokens=vision_tokens,
                vision_orig_shape=vision_orig_shape,
                batch_masks=batch_masks,
                num_chunks=num_chunks,
                total_len=position_ids.shape[1],
            )

        assert self.add_decoder, "Language model required for forward pass."
        language_embeddings = None
        if self.pre_process:
            language_embeddings = self.language_model.get_partially_trainable_embedding(tokens)
            language_embeddings = language_embeddings.transpose(1, 0).contiguous()  # [text_seq_len, b, h_language]

        full_text_row_masked_out_mask = (
            full_text_row_masked_out_mask[:, :, position_ids[0]].permute(2, 0, 1, 3).squeeze(2)
            if cross_attention_masks is not None
            else None
        )
        output = self.language_model(
            input_ids=tokens,
            position_ids=position_ids,
            labels=labels,
            decoder_input=language_embeddings,
            attention_mask=None,
            cross_attention_masks=(
                cross_attention_masks[:, :, position_ids[0]] if cross_attention_masks is not None else None
            ),
            full_text_row_masked_out_mask=full_text_row_masked_out_mask,
            xattn_caches=xattn_caches,
        )
        return output

    def set_input_tensor(self, input_tensor) -> None:
        """Set model chunk input tensor."""
        if not isinstance(input_tensor, list):
            input_tensor = [input_tensor]

        if self.add_encoder:
            self.vision_model.set_input_tensor(input_tensor[0])
        elif self.add_decoder and self.pre_process:
            self.encoder_hidden_state = input_tensor[0]
        else:
            assert len(input_tensor) == 2, 'input_tensor should contain encoder output.'
            self.language_model.set_input_tensor(input_tensor[0])
            self.encoder_hidden_state = input_tensor[1]


class MLlamaModel(L.LightningModule, io.IOMixin, io.ConnectorMixin, fn.FNMixin):
    def __init__(
        self,
        config: MLlamaModelConfig,
        optim: Optional[OptimizerModule] = None,
        tokenizer: Optional["TokenizerSpec"] = None,
        model_transform: Optional[Callable[[nn.Module], nn.Module]] = None,
    ):
        super().__init__()
        self.config = config
        self.tokenizer = tokenizer
        self.optim = optim or MegatronOptimizerModule(config=OptimizerConfig(lr=1e-4, use_distributed_optimizer=True))
        self.optim.connect(self)  # This will bind the `configure_optimizers` method
        self.model_transform = model_transform
        self._training_loss_reduction = None
        self._validation_loss_reduction = None

    def configure_model(self) -> None:
        if not hasattr(self, "module"):
            self.module: MLlamaBaseModel = self.config.configure_model(self.tokenizer)

    def forward(
        self,
        batch_images: List[List[PIL_Image.Image]],
        tokens: torch.LongTensor,
        position_ids: torch.LongTensor,
        batch_masks: Optional[torch.Tensor] = None,
        num_chunks: Optional[torch.Tensor] = None,
        aspect_ratio_ids: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        cross_attention_masks: Optional[torch.Tensor] = None,
        full_text_row_masked_out_mask: Optional[torch.Tensor] = None,
        xattn_caches: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:

        output_tensor = self.module(
            position_ids=position_ids,
            tokens=tokens,
            batch_images=batch_images,
            batch_masks=batch_masks,
            num_chunks=num_chunks,
            aspect_ratio_ids=aspect_ratio_ids,
            labels=labels,
            cross_attention_masks=cross_attention_masks,
            full_text_row_masked_out_mask=full_text_row_masked_out_mask,
            xattn_caches=xattn_caches,
        )

        return output_tensor

    def data_step(self, dataloader_iter) -> Dict[str, torch.Tensor]:
        return self.config.data_step_fn(dataloader_iter)

    def forward_step(self, batch) -> torch.Tensor:
        return self.config.forward_step_fn(self, batch)

    def training_step(self, batch, batch_idx=None) -> torch.Tensor:
        # In mcore the loss-function is part of the forward-pass (when labels are provided)
        return self.forward_step(batch)

    def validation_step(self, batch, batch_idx=None) -> torch.Tensor:
        # In mcore the loss-function is part of the forward-pass (when labels are provided)

        return self.forward_step(batch)

    @property
    def training_loss_reduction(self) -> MaskedTokenLossReduction:
        if not self._training_loss_reduction:
            self._training_loss_reduction = MaskedTokenLossReduction()

        return self._training_loss_reduction

    @property
    def validation_loss_reduction(self) -> MaskedTokenLossReduction:
        if not self._validation_loss_reduction:
            self._validation_loss_reduction = MaskedTokenLossReduction(validation_step=True)

        return self._validation_loss_reduction


__all__ = [
    "MLlamaModel",
    "MLlamaModelConfig",
    "CrossAttentionTextConfig",
    "CrossAttentionVisionConfig",
    "llama_data_step",
    "llama_forward_step",
    "transformer_engine_layer_spec",
    "local_layer_spec",
]
