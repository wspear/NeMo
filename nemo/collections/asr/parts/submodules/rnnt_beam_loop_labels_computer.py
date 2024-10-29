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

from typing import Any, Optional, Tuple

import torch
from omegaconf import DictConfig


from nemo.collections.asr.parts.utils import rnnt_utils
from nemo.collections.asr.parts.utils.asr_confidence_utils import ConfidenceMethodMixin
from nemo.collections.common.parts.optional_cuda_graphs import WithOptionalCudaGraphs


class LoopLabelsState:
    """
    State for Loop Labels algorithm. Used only with CUDA graphs.
    In initialization phase it is possible to assign values (tensors) to the state.
    For algorithm code the storage should be reused (prefer copy data instead of assigning tensors).
    """

    max_time: int  # maximum length of internal storage for time dimension
    batch_size: int  # (maximum) length of internal storage for batch dimension
    device: torch.device  # device to store preallocated tensors

    all_durations: torch.Tensor

    encoder_output_projected: torch.Tensor  # projected output from the encoder for decoding algorithm
    encoder_output_length: torch.Tensor  # length of the (projected) output from the encoder

    labels: torch.Tensor  # storage for current labels
    scores: torch.Tensor  # storage for current scores

    batch_indices: torch.Tensor  # indices of elements in batch (constant, range [0, batch_size-1])

    time_indices: torch.Tensor  # current time indices for each element in batch
    safe_time_indices: torch.Tensor  # current time indices, but guaranteed to be < encoder_output_length
    time_indices_current_labels: torch.Tensor  # time indices for found labels (corresponding to `labels` field)
    last_timesteps: torch.Tensor  # indices of the last timesteps for each element (encoder_output_length - 1)

    active_mask: torch.Tensor  # mask for active hypotheses (the decoding is finished for the utterance if it is False)
    advance_mask: torch.Tensor  # mask for "advancing" hypotheses (blank is found for the element on the current step)
    blank_mask: torch.Tensor  # if the element is blank
    # if the element was active on the previous step: to identify the end of decoding and store final hidden state
    active_mask_prev: torch.Tensor
    became_inactive_mask: torch.Tensor  # mask for elements that became inactive (end of decoding)

    active_mask_any: torch.Tensor  # 0-dim bool tensor, condition for outer loop ('any element is still active')
    advance_mask_any: torch.Tensor  # 0-dim bool tensor, condition for inner loop ('should advance any index')

    last_decoder_state: Any  # last state from the decoder, needed for the output
    decoder_state: Any  # current decoder state
    decoder_output: torch.Tensor  # output from the decoder (projected)

    batched_hyps: rnnt_utils.BatchedHyps  # batched hypotheses - decoding result
    alignments: Optional[rnnt_utils.BatchedAlignments] = None  # batched alignments

    def __init__(
        self,
        batch_size: int,
        max_time: int,
        encoder_dim: int,
        max_symbols: int,
        device: torch.device,
        float_dtype: torch.dtype,
        logits_dim: int,
        preserve_alignments=False,
        preserve_frame_confidence=False,
        include_duration_confidence: bool = False,
    ):
        """

        Args:
            batch_size: batch size for encoder output storage
            max_time: maximum time for encoder output storage
            encoder_dim: last dimension for encoder output storage (projected encoder output)
            max_symbols: max symbols per step (to avoid infinite looping and pre-allocate storage)
            device: device to store tensors
            float_dtype: default float dtype for tensors (should match projected encoder output)
            logits_dim: output dimension for Joint
            preserve_alignments: if alignments are needed
            preserve_frame_confidence: if frame confidence is needed
            include_duration_confidence: if duration confidence is needed to be added to the frame confidence
        """
        self.device = device
        self.float_dtype = float_dtype
        self.batch_size = batch_size
        self.max_time = max_time

        self.encoder_output_projected = torch.zeros(
            (self.batch_size, self.max_time, encoder_dim),
            dtype=float_dtype,
            device=self.device,
        )
        self.encoder_output_length = torch.zeros((self.batch_size,), dtype=torch.long, device=self.device)

        self.labels = torch.zeros([self.batch_size], dtype=torch.long, device=self.device)
        self.scores = torch.zeros([self.batch_size], dtype=float_dtype, device=self.device)

        # indices of elements in batch (constant)
        self.batch_indices = torch.arange(self.batch_size, dtype=torch.long, device=self.device)

        self.time_indices = torch.zeros_like(self.batch_indices)
        self.safe_time_indices = torch.zeros_like(self.batch_indices)
        self.time_indices_current_labels = torch.zeros_like(self.time_indices)
        self.last_timesteps = torch.zeros_like(self.time_indices)

        self.active_mask = torch.zeros([self.batch_size], dtype=torch.bool, device=self.device)
        self.advance_mask = torch.zeros_like(self.active_mask)
        self.blank_mask = torch.zeros_like(self.active_mask)
        self.active_mask_prev = torch.zeros_like(self.active_mask)
        self.became_inactive_mask = torch.zeros_like(self.active_mask)

        self.active_mask_any = torch.tensor(True, device=self.device, dtype=torch.bool)
        self.advance_mask_any = torch.tensor(True, device=self.device, dtype=torch.bool)

        self.batched_hyps = rnnt_utils.BatchedHyps(
            batch_size=self.batch_size,
            init_length=self.max_time * max_symbols,
            device=self.device,
            float_dtype=float_dtype,
        )
        if preserve_alignments or preserve_frame_confidence:
            self.alignments = rnnt_utils.BatchedAlignments(
                batch_size=batch_size,
                logits_dim=logits_dim,
                init_length=max_time * (max_symbols + 1),
                device=self.device,
                float_dtype=self.float_dtype,
                store_alignments=preserve_alignments,
                store_frame_confidence=preserve_frame_confidence,
                with_duration_confidence=include_duration_confidence,
            )
        else:
            self.alignments = None

    def need_reinit(self, encoder_output_projected: torch.Tensor) -> bool:
        """Check if need to reinit state: larger batch_size/max_time, or new device"""
        return (
            self.batch_size < encoder_output_projected.shape[0]
            or self.max_time < encoder_output_projected.shape[1]
            or self.device.index != encoder_output_projected.device.index
        )


class BeamBatchedRNNTLoopLabelsComputer(WithOptionalCudaGraphs, ConfidenceMethodMixin):
    """
    Label Looping algorithm implementation: optimized batched greedy decoding. Callable.
    Iterates over labels, on each step finding the next non-blank label
    (evaluating Joint multiple times in inner loop); It uses a minimal possible amount of calls
    to prediction network (with maximum possible batch size),
    which makes it especially useful for scaling the prediction network.
    During decoding all active hypotheses ("texts") have the same lengths.
    """

    INITIAL_MAX_TIME = 375  # initial max time, used to init state for Cuda graphs

    def __init__(
        self,
        decoder,
        joint,
        beam_size: int,
        blank_index: int,
        max_symbols_per_step: Optional[int] = None,
        preserve_alignments=False,
        preserve_frame_confidence=False,
        include_duration_confidence: bool = False,
        confidence_method_cfg: Optional[DictConfig] = None,
    ):
        """
        Init method.
        Args:
            decoder: Prediction network from RNN-T
            joint: Joint module from RNN-T
            blank_index: index of blank symbol
            durations: list of TDT durations, e.g., [0, 1, 2, 4, 8]
            max_symbols_per_step: max symbols to emit on each step (to avoid infinite looping)
            preserve_alignments: if alignments are needed
            preserve_frame_confidence: if frame confidence is needed
            include_duration_confidence: if duration confidence is needed to be added to the frame confidence
            confidence_method_cfg: config for the confidence
        """
        super().__init__()
        self.decoder = decoder
        self.joint = joint
        # keep durations on CPU to avoid side effects in multi-gpu environments
        self._blank_index = blank_index
        self.max_symbols = max_symbols_per_step
        self.preserve_alignments = preserve_alignments
        self.preserve_frame_confidence = preserve_frame_confidence
        self.include_duration_confidence = include_duration_confidence
        self._SOS = self._blank_index
        self._init_confidence_method(confidence_method_cfg=confidence_method_cfg)
        assert self._SOS == self._blank_index  # "blank as pad" algorithm only
        
        vocab_size = len(joint.vocabulary)
        self.beam_size = min(beam_size, vocab_size)
        
        self.max_steps = 5

    def loop_labels_torch(
        self,
        encoder_output: torch.Tensor,
        encoder_output_length: torch.Tensor,
    ) -> Tuple[rnnt_utils.BatchedHyps, Optional[rnnt_utils.BatchedAlignments], Any]:
        """
        Pure PyTorch implementation

        Args:
            encoder_output: output from the encoder
            encoder_output_length: lengths of the utterances in `encoder_output`
        """
        batch_size, max_time, _unused = encoder_output.shape
        device = encoder_output.device
        init_length = max_time * self.max_symbols if self.max_symbols is not None else max_time

        # do not recalculate joint projection, project only once
        encoder_output_projected = self.joint.project_encoder(encoder_output)
        float_dtype = encoder_output_projected.dtype
        
        batch_indices = torch.arange(batch_size, device=device)
        beam_indices = torch.arange(self.beam_size, device=device)
        batch_blank = torch.full((batch_size, 1), self._blank_index, device=device)
        batch_zeros = torch.full((batch_size, 1), 0, device=device)
        batch_beam_zeros = torch.full((batch_size, self.beam_size, 1), 0, device=device)
        
        batch_max_time_indices = (encoder_output_length - 1).unsqueeze(-1)
        print(batch_max_time_indices.shape)
        
        # init empty batched hypotheses
        batched_hyps = rnnt_utils.BeamBatchedHyps(
            beam_size=self.beam_size,
            batch_size=batch_size,
            max_timesteps=encoder_output_length-1,
            init_length=init_length,
            device=device,
            float_dtype=float_dtype,
        )
        
        time_indices = torch.zeros((batch_size, self.beam_size), device=device, dtype=torch.long)
        init_state = self.decoder.initialize_state(encoder_output)
        init_labels = torch.full((batch_size, ), fill_value=self._SOS, device=device)
        
        decoder_output, state, *_ = self.decoder.predict(init_labels.unsqueeze(1), init_state, add_sos=False, batch_size=batch_size)
        decoder_output = self.joint.project_prednet(decoder_output)
        
        time_indices = torch.zeros(batch_size, device=device, dtype=torch.long)
        logits = self.joint.joint_after_projection(encoder_output_projected[batch_indices, time_indices].unsqueeze(1), decoder_output)
        logps = torch.log_softmax(logits, dim=-1).view(batch_size, -1).squeeze(1).squeeze(1)
        label_logps, labels = logps.topk(self.beam_size, dim=-1)
        blank_logps = logps[batch_indices, -1].unsqueeze(1)
        blank_mask = labels == self._blank_index
        
        labels_list = [labels]
        label_logps_list = [label_logps]
        blank_logps_list = [batch_zeros, blank_logps]
        
        iter_count = 0
        while blank_mask.any():
            time_indices += 1

            old_blank_mask = blank_mask.clone()
            
            logits = self.joint.joint_after_projection(encoder_output_projected[batch_indices, time_indices].unsqueeze(1), decoder_output)
            logps = torch.log_softmax(logits, dim=-1).view(batch_size, -1).squeeze(1).squeeze(1)
            label_logps, labels = logps.topk(self.beam_size, dim=-1)

            labels_list.append(labels)
            label_logps_list.append(label_logps)
                
            blank_mask = labels == self._blank_index
            blank_mask = torch.logical_and(old_blank_mask, blank_mask)
            
            # add blank logps if not last loop
            if blank_mask.any():
                blank_logps = logps[batch_indices, -1].unsqueeze(1)
                blank_logps_list.append(blank_logps)
            iter_count += 1
        
        batched_hyps, labels = self.initialize_beam(labels_list, label_logps_list, blank_logps_list, init_length, batch_size, float_dtype, device)
        
        encoder_output_projected = encoder_output_projected.repeat_interleave(self.beam_size, dim=0)
        state = (state[0].repeat_interleave(self.beam_size, dim=1), state[1].repeat_interleave(self.beam_size, dim=1))
        
        active_mask = batched_hyps.last_timestep < encoder_output_length.unsqueeze(1)
        
        big_iter_count = 0
        batch_beam_indices = torch.arange(self.beam_size * batch_size, device=device)
        while active_mask.any():
            print("big iter count", big_iter_count)
            time_indices = batched_hyps.last_timestep.clone().flatten()
            safe_time_indices = torch.minimum(time_indices.view(batch_size, self.beam_size), encoder_output_length.unsqueeze(1) - 1).flatten()
            print("####", safe_time_indices)
            
            decoder_output, state, *_ = self.decoder.predict(labels.view(-1, 1), state, add_sos=False, batch_size=batch_size)
            decoder_output = self.joint.project_prednet(decoder_output)
        
            logits = self.joint.joint_after_projection(encoder_output_projected[batch_beam_indices, safe_time_indices].unsqueeze(1), decoder_output)
            logps = torch.log_softmax(logits, dim=-1).view(batch_size, self.beam_size, -1)
        
            label_logps, labels = logps.topk(self.beam_size, dim=-1)
            blank_logps = logps[batch_indices.unsqueeze(1), beam_indices.unsqueeze(0), -1].unsqueeze(-1)
            blank_mask = labels == self._blank_index
            
            labels_list = [labels]
            label_logps_list = [label_logps]
            blank_logps_list = [batch_beam_zeros, blank_logps]

            iter_count = 0
            while blank_mask.any() and active_mask.any():
                time_indices += 1
                is_active = torch.less(time_indices.view(batch_size, self.beam_size), batch_max_time_indices)
                became_inactive = torch.eq(time_indices.view(batch_size, self.beam_size), batch_max_time_indices)
                is_inactive = torch.greater(time_indices.view(batch_size, self.beam_size), batch_max_time_indices)
                
                safe_time_indices = torch.minimum(time_indices.view(batch_size, self.beam_size), encoder_output_length.unsqueeze(1) - 1 ).flatten()
                print("####", safe_time_indices)
                # assert((time_indices.view(batch_size, self.beam_size) < encoder_output_length.unsqueeze(1)).all())

                old_blank_mask = blank_mask.clone()
                
                logits = self.joint.joint_after_projection(encoder_output_projected[batch_beam_indices, safe_time_indices].unsqueeze(1), decoder_output)
                logps = torch.log_softmax(logits, dim=-1).view(batch_size, self.beam_size, -1)
                label_logps, labels = logps.topk(self.beam_size, dim=-1)

                labels_list.append(labels)
                label_logps_list.append(label_logps)
                    
                blank_mask = labels == self._blank_index
                blank_mask = torch.logical_and(old_blank_mask, blank_mask)
                active_mask = time_indices.view(batch_size, self.beam_size) < encoder_output_length.unsqueeze(-1)
                
                # add blank logps if not last loop
                if blank_mask.any() and active_mask.any():
                    blank_logps = logps[batch_indices.unsqueeze(1), beam_indices.unsqueeze(0), -1].unsqueeze(-1)
                    blank_logps_list.append(blank_logps)
                iter_count += 1
                
            
            print("before update")
            labels, beam_idx = self.update_beam(batched_hyps, labels_list, label_logps_list, blank_logps_list)
            print("adter update")
            beam_idx = beam_idx.flatten() + torch.arange(batch_size, device=device).repeat_interleave(self.beam_size)
            self.decoder.batch_rearrange_states(state, beam_idx)
            print("after rearrange")
            
            active_mask = batched_hyps.last_timestep < encoder_output_length.unsqueeze(1)
            # batched_hyps.print()
            big_iter_count += 1
        
        exit()
        return batched_hyps, None, last_decoder_state
        
    def initialize_beam(self,
                        labels_list,
                        label_logps_list,
                        blank_logps_list,
                        init_length,
                        batch_size,
                        float_dtype,
                        device):
        labels = torch.cat(labels_list, dim=1)
        label_logps = torch.cat(label_logps_list, dim=1)
        
        blank_logps = torch.cat(blank_logps_list, dim=1)
        blank_logps = torch.cumsum(blank_logps, dim=-1)
        blank_logps = blank_logps.repeat_interleave(self.beam_size, dim=-1)
        
        total_logps = label_logps + blank_logps
        
        batch_size = labels.shape[0]
        device = labels.device
        batch_indices = torch.arange(batch_size, device=device).unsqueeze(1)
        
        # masking blank ending hypothesis
        blank_mask = labels == self._blank_index
        total_logps[blank_mask] = float("-inf")
        
        logps, idx = total_logps.topk(k = self.beam_size, dim=-1)
        num_blanks = idx // self.beam_size
        
        labels = labels[batch_indices, idx]
        label_logps = label_logps[batch_indices, idx]
        blank_logps = blank_logps[batch_indices, idx]
        
        assert((logps != float("-inf")).all())
        assert((label_logps + blank_logps == logps).all())
        
        # initializing empty batched hypotheses
        batched_hyps = rnnt_utils.BeamBatchedHyps(batch_size=batch_size,
                                                  beam_size=self.beam_size, 
                                                  init_length=init_length,
                                                  float_dtype=float_dtype,
                                                  max_timesteps=None,
                                                  device=device)
        batched_hyps.append_labels(labels=labels,
                                   label_logps=label_logps,
                                   blank_logps=blank_logps,
                                   num_blanks=num_blanks)
        batched_hyps.print()
        
        return batched_hyps, labels
        
    def update_beam(self,
                    batched_beam_hyps,
                    labels_list,
                    label_logps_list,
                    blank_logps_list):
        batch_size = batched_beam_hyps.batch_size
        device = batched_beam_hyps.device
        batch_indices = torch.arange(batch_size, device=device).unsqueeze(1)
        
        labels = torch.stack(labels_list, dim=1)
        label_logps = torch.stack(label_logps_list, dim=1)
        
        blank_logps = torch.stack(blank_logps_list, dim=1)
        blank_logps = torch.cumsum(blank_logps, dim=-1)
        blank_logps = blank_logps.repeat_interleave(self.beam_size, dim=-1)
        
        curr_scores = batched_beam_hyps.scores.unsqueeze(1).unsqueeze(-1)
        total_logps = curr_scores + label_logps + blank_logps
        
        # getting hypotheses that become inactive
        curr_time_indices = batched_beam_hyps.last_timestep
        num_blank_lengths = torch.arange(len(blank_logps_list), device=device).unsqueeze(-1).unsqueeze(-1)
        time_indices = curr_time_indices.unsqueeze(0) + num_blank_lengths
                
        # masking blank ending hypothesis
        blank_mask = labels == self._blank_index
        total_logps[blank_mask] = float("-inf")
        
        # masking inactive hypothesis
        
        
        labels = labels.view(batched_beam_hyps.batch_size, -1)
        label_logps = label_logps.view(batched_beam_hyps.batch_size, -1)
        blank_logps = blank_logps.view(batched_beam_hyps.batch_size, -1)
        total_logps = total_logps.view(batched_beam_hyps.batch_size, -1)
        
        logps, idx = total_logps.topk(k = self.beam_size, dim=-1)
        num_blanks = idx // (self.beam_size * self.beam_size)
        beam_index = idx % (self.beam_size * self.beam_size) // self.beam_size
        
        labels = labels[batch_indices, idx]
        label_logps = label_logps[batch_indices, idx]
        blank_logps = blank_logps[batch_indices, idx]
        
        assert((logps != float("-inf")).all())
        assert((beam_index < self.beam_size).all())
        assert((num_blanks < len(labels_list)).all())
        
        batched_beam_hyps.update_beam(labels=labels,
                                   label_logps=label_logps,
                                   blank_logps=blank_logps,
                                   num_blanks=num_blanks,
                                   beam_idx=beam_index)
        
        batched_beam_hyps.print()
        
        return labels, beam_index
                

    def __call__(
        self,
        x: torch.Tensor,
        out_len: torch.Tensor,
    ) -> Tuple[rnnt_utils.BatchedHyps, Optional[rnnt_utils.BatchedAlignments], Any]:
        return self.loop_labels_torch(encoder_output=x, encoder_output_length=out_len)


    def maybe_enable_cuda_graphs(self):
       return
   
    def disable_cuda_graphs(self):
        return()