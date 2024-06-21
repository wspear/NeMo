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

import torch
from omegaconf.dictconfig import DictConfig
from pytorch_lightning.trainer.trainer import Trainer
from nemo.collections.nlp.models.language_modeling.megatron_gpt_model import MegatronGPTModel
from megatron.core.models.mamba import MambaModel
from megatron.core.models.mamba.mamba_layer_specs import get_mamba_layer_with_transformer_engine_spec

class MegatronJambaModel(MegatronGPTModel):
    """
    Megatron Griffin pretraining.
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):

        self.vocab_size = cfg.get('vocab_size', 65536)
        self.cfg = cfg
        super().__init__(cfg=cfg, trainer=trainer)
        self.mcore_gpt = True

    def model_provider_func(self, pre_process, post_process):

        self.hybrid_override_pattern="O"#self.cfg.get('hybrid_override_pattern', "M" * self.transformer_config.num_layers)#"M-MOM-MO*-MOM-MO" #  #"M-MOM-MO*-MOM-MO"*4
        mamba_stack_spec = get_mamba_layer_with_transformer_engine_spec(self.transformer_config.num_moe_experts, moe_grouped_gemm=False)
        self.transformer_config.activation_func = torch.nn.functional.silu
        self.transformer_config.add_bias_linear=self.cfg.get('add_bias_linear', False)
        
        model = MambaModel(
            config=self.transformer_config,
            max_sequence_length=self.cfg.get('encoder_seq_length', 2048),
            vocab_size=self.cfg.get('vocab_size', 65536),
            mamba_stack_spec=mamba_stack_spec, 
            hybrid_override_pattern=self.hybrid_override_pattern)
        
        return model

    def forward(self, input_ids, position_ids=None, attention_mask=None, labels=None):

        output_tensor = self.model(
            input_ids=input_ids, position_ids=position_ids, attention_mask=attention_mask, labels=labels
        )
        return output_tensor

    def build_transformer_config(self):
        transformer_config = super().build_transformer_config()
        transformer_config.gated_linear_unit = self.cfg.get('gated_linear_unit', True)
        return transformer_config

    def on_validation_epoch_end(self):

        averaged_loss = torch.tensor(0.0, dtype=torch.float32).cuda()
        return averaged_loss

    def sharded_state_dict(self, prefix: str = ''):
        return None

    def _reset_activation_checkpointing_args(self):
        return

    def _restore_activation_checkpointing_args(self):
        return

    def _reset_sequence_parallelism_args(self):
        return

    def _restore_sequence_parallelism_args(self):
        return
