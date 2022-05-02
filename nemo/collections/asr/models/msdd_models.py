# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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
import itertools
import subprocess
import json
import math
import os
from tqdm import tqdm
import pickle as pkl
import numpy as np
import shutil
from typing import Dict, List, Optional, Union

import librosa
import torch
from statistics import mode
from omegaconf import DictConfig
from omegaconf.omegaconf import open_dict
from pytorch_lightning import Trainer
from torch.utils.data import ChainDataset
from collections import OrderedDict
from nemo.collections.asr.models import ClusteringDiarizer
from nemo.collections.asr.data.audio_to_label import AudioToSpeechLabelDataset, AudioToSpeechMSDDDataset
from nemo.collections.asr.data.audio_to_label_dataset import get_tarred_speech_label_dataset
from nemo.collections.asr.data.audio_to_text_dataset import convert_to_config_list
from nemo.collections.asr.losses.angularloss import AngularSoftmaxLoss
from nemo.collections.asr.losses.bce_loss import BCELoss
from nemo.collections.asr.models.asr_model import ExportableEncDecModel
from nemo.collections.asr.parts.preprocessing.features import WaveformFeaturizer
from nemo.collections.asr.parts.preprocessing.perturb import process_augmentations
from nemo.collections.asr.parts.utils.speaker_utils import embedding_normalize, get_uniqname_from_filepath
from nemo.collections.asr.parts.utils.nmesc_clustering import get_argmin_mat
from nemo.collections.common.losses import CrossEntropyLoss as CELoss
from nemo.collections.common.metrics import TopKClassificationAccuracy
from nemo.collections.asr.metrics.multi_binary_acc import MultiBinaryAccuracy
from nemo.collections.common.parts.preprocessing.collections import ASRSpeechLabel

from nemo.utils import logging, model_utils
from nemo.core.classes import ModelPT
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.core.neural_types import *
from nemo.utils import logging
from torchmetrics import Metric
from nemo.collections.asr.parts.utils.speaker_utils import (
    audio_rttm_map,
    get_embs_and_timestamps,
    get_uniqname_from_filepath,
    parse_scale_configs,
    perform_clustering,
    score_labels,
    segments_manifest_to_subsegments_manifest,
    write_rttm2manifest,
    rttm_to_labels,
    labels_to_pyannote_object


)
from nemo.core.neural_types import (
    AcousticEncodedRepresentation,
    LengthsType,
    LogitsType,
    ProbsType,
    EncodedRepresentation,
    NeuralType,
    SpectrogramType,
)
from nemo.core.neural_types.elements import ProbsType


def sprint(*args):
    if False:
        print(*args)
    else:
        pass

__all__ = ['EncDecDiarLabelModel', 'ClusterEmbedding']

def write_json_file(name, lines):
    with open(name, 'w') as fout:
        for i, dic in enumerate(lines):
            json.dump(dic, fout)
            fout.write('\n')
    logging.info("wrote", name)

def getMultiScaleCosAffinityMatrix(uniq_embs_and_timestamps):
    """
    Calculate cosine similarity values among speaker embeddings for each scale then
    apply multiscale weights to calculate the fused similarity matrix.

    Args:
        uniq_embs_and_timestamps: (dict)
            The dictionary containing embeddings, timestamps and multiscale weights.
            If uniq_embs_and_timestamps contains only one scale, single scale diarization 
            is performed.

    Returns:
        fused_sim_d (np.array):
            This function generates an ffinity matrix that is obtained by calculating
            the weighted sum of the affinity matrices from the different scales.
        base_scale_emb (np.array):
            The base scale embedding (the embeddings from the finest scale)
    """
    uniq_scale_dict = uniq_embs_and_timestamps['scale_dict']
    base_scale_idx = max(uniq_scale_dict.keys())
    base_scale_emb = np.array(uniq_scale_dict[base_scale_idx]['embeddings'])
    multiscale_weights = uniq_embs_and_timestamps['multiscale_weights']
    scale_mapping_argmat = {}

    session_scale_mapping_dict = get_argmin_mat(uniq_scale_dict)
    for scale_idx in sorted(uniq_scale_dict.keys()):
        mapping_argmat = session_scale_mapping_dict[scale_idx]
        scale_mapping_argmat[scale_idx] = mapping_argmat
    return scale_mapping_argmat

# def __init__(self, cfg: DictConfig, load_speaker_model=True):

class NeuralDiarizer:
    def __init__(self, cfg: DictConfig):# , load_speaker_model=True):
        self._cfg = cfg
        # ClusteringDiarizer.__init__(self, cfg=cfg, load_speaker_model=False)

        self._init_msdd_model()
        if load_speaker_model:
            # self._init_speaker_model()
            self._speaker_params = self._cfg.diarizer.speaker_embeddings.parameters
            self._speaker_dir = os.path.join(self._diarizer_params.out_dir, 'speaker_outputs')
            # shutil.rmtree(self._speaker_dir, ignore_errors=True)
            if not os.path.exists(self._speaker_dir):
                os.makedirs(self._speaker_dir)

            # Clustering params
            self._cluster_params = self._diarizer_params.clustering.parameters
            self._device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        
    def _init_msdd_model(self):
        device = 'cuda'
        if not torch.cuda.is_available():
            device = 'cpu'
            logging.warning("Running model on CPU, for faster performance it is adviced to use atleast one NVIDIA GPUs")

        if self._cfg.diarizer.msdd_model.model_path.endswith('.nemo'):
            logging.info(f"Using local speaker model from {self._cfg.diarizer.msdd_model.model_path}")
            self.msdd_model = EncDecDiarLabelModel.restore_from(restore_path=self._cfg.diarizer.msdd_model.model_path)
        elif self._cfg.diarizer.msdd_model.model_path.endswith('.ckpt'):
            self.msdd_model = EncDecDiarLabelModel.load_from_checkpoint(checkpoint_path=self._cfg.diarizer.msdd_model.model_path)

        self.transfer_diar_params_to_model_params()
        return self.msdd_model.cfg

    def transfer_diar_params_to_model_params(self):
        self.msdd_model.cfg.test_ds.manifest_filepath = self._cfg.diarizer.manifest_filepath
        self.msdd_model.cfg.test_ds.emb_dir = self._cfg.diarizer.out_dir
        self.msdd_model.cfg.test_ds.num_spks = self._cfg.msdd_model.max_num_of_spks
        self.msdd_model.cfg.base.diarizer.out_dir = self._cfg.msdd_model.test_ds.emb_dir
        self.msdd_model.cfg_base = self._cfg
        self.msdd_model._cfg_msdd = self.msdd_model.cfg
        return self.msdd_model.cfg

class ClusterEmbedding:
    def __init__(self, cfg_base: DictConfig, cfg_msdd_model: DictConfig):
        self.cfg_base = cfg_base
        self._cfg_msdd = cfg_msdd_model
        self.max_num_of_spks = int(self.cfg_base.diarizer.clustering.parameters.max_num_speakers)
        self.scale_n = None
        self.clus_emb_path = 'speaker_outputs/embeddings/clus_emb_info.pkl'
        self.clus_map_path = 'speaker_outputs/embeddings/clus_mapping.pkl'
        self.scale_map_path = 'speaker_outputs/embeddings/scale_mapping.pkl'
        self.multiscale_weights_list = None
        self.clusdiar_model = None
        self.msdd_model = None
        self.run_clus_from_loaded_emb = False
  
    def load_speaker_model(self):
        self._init_speaker_model()
        self._speaker_params = self._cfg.diarizer.speaker_embeddings.parameters
        self._speaker_dir = os.path.join(self._diarizer_params.out_dir, 'speaker_outputs')
        if not os.path.exists(self._speaker_dir):
            os.makedirs(self._speaker_dir)

        # Clustering params
        self._cluster_params = self._diarizer_params.clustering.parameters
        self._device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    

    def prepare_cluster_embs_train(self):
        """
        MSDD
        Prepare embeddings from clustering diarizer for TS-VAD style diarizer.
        """
        self.emb_sess_train_dict, self.emb_seq_train, self.clus_train_label_dict = self.run_clustering_diarizer(self._cfg_msdd.train_ds)
        self.emb_sess_dev_dict, self.emb_seq_dev, self.clus_dev_label_dict = self.run_clustering_diarizer(self._cfg_msdd.validation_ds)
        self.emb_sess_test_dict, self.emb_seq_test, self.clus_test_label_dict = self.run_clustering_diarizer(self._cfg_msdd.test_ds)
    
    def prepare_cluster_embs_infer(self):
        self.emb_sess_test_dict, self.emb_seq_test, self.clus_test_label_dict = self.run_clustering_diarizer(self._cfg_msdd.test_ds)
    
    def assign_labels_to_longer_segs(self, base_clus_label_dict, session_scale_mapping_dict):
        new_clus_label_dict = {scale_index: {} for scale_index in range(self.scale_n)}
        for uniq_id, uniq_scale_mapping_dict in session_scale_mapping_dict.items():
            base_scale_clus_label = np.array([ x[-1] for x in base_clus_label_dict[uniq_id]])
            new_clus_label_dict[self.scale_n-1][uniq_id] = base_scale_clus_label
            for scale_index in range(self.scale_n-1):
                new_clus_label = []
                assert uniq_scale_mapping_dict[scale_index].shape[0] == base_scale_clus_label.shape[0], "The number of base scale labels does not match the segment numbers in uniq_scale_mapping_dict"
                max_index = max(uniq_scale_mapping_dict[scale_index])
                for seg_idx in range(max_index+1):
                    if seg_idx in uniq_scale_mapping_dict[scale_index]:
                        seg_clus_label = mode(base_scale_clus_label[uniq_scale_mapping_dict[scale_index] == seg_idx])
                    else:
                        seg_clus_label = 0 if len(new_clus_label) == 0 else new_clus_label[-1]
                    new_clus_label.append(seg_clus_label)
                new_clus_label_dict[scale_index][uniq_id] = new_clus_label
        return new_clus_label_dict

    def get_clus_emb(self, emb_scale_seq_dict, clus_label, speaker_mapping_dict, session_scale_mapping_dict):
        """
        MSDD
        Get an average embedding vector for each cluster (speaker).
        """
        self.scale_n = len(emb_scale_seq_dict.keys())
        base_clus_label_dict = {key: [] for key in emb_scale_seq_dict[self.scale_n-1].keys()}
        all_scale_clus_label_dict  = {scale_index:{key: [] for key in emb_scale_seq_dict[self.scale_n-1].keys() } for scale_index in emb_scale_seq_dict.keys()}
        emb_sess_avg_dict = {scale_index:{key: [] for key in emb_scale_seq_dict[self.scale_n-1].keys() } for scale_index in emb_scale_seq_dict.keys()}
        for line in clus_label:
            uniq_id = line.split()[0]
            label = int(line.split()[-1].split('_')[-1])
            stt, end = [round(float(x), 2) for x in line.split()[1:3]]
            base_clus_label_dict[uniq_id].append([stt, end, label])
        
        all_scale_clus_label_dict = self.assign_labels_to_longer_segs(base_clus_label_dict, session_scale_mapping_dict)
        dim = emb_scale_seq_dict[0][uniq_id][0].shape[0]
        for scale_index in emb_scale_seq_dict.keys():
            for uniq_id, _emb_tensor in emb_scale_seq_dict[scale_index].items():
                if type(_emb_tensor) == list:
                    emb_tensor = torch.tensor(np.array(_emb_tensor))
                else:
                    emb_tensor = _emb_tensor
                clus_label_list = all_scale_clus_label_dict[scale_index][uniq_id]
                spk_set = set(clus_label_list)
                # Create a label array which identifies clustering result for each segment.
                spk_N = len(spk_set)
                assert spk_N <= self.max_num_of_spks, f"uniq_id {uniq_id} - self.max_num_of_spks {self.max_num_of_spks} is smaller than the actual number of speakers: {spk_N}"
                label_array = torch.Tensor(clus_label_list)
                avg_embs = torch.zeros(dim, self.max_num_of_spks)
                for spk_idx in spk_set:
                    selected_embs = emb_tensor[label_array == spk_idx]
                    avg_embs[:, spk_idx] = torch.mean(selected_embs, dim=0)
                inv_map = {clus_key: rttm_key for rttm_key, clus_key in speaker_mapping_dict[uniq_id].items()}
                emb_sess_avg_dict[scale_index][uniq_id] = {'mapping': inv_map, 'avg_embs': avg_embs}
        return emb_sess_avg_dict, base_clus_label_dict
    
    def get_manifest_uniq_ids(self, manifest_filepath):
        manifest_lines = []
        with open(manifest_filepath) as f:
            manifest_lines = f.readlines()
            for jsonObj in f:
                student_dict = json.loads(jsonObj)
                manifest_lines.append(student_dict)
        uniq_id_list, json_dict_list  = [], []
        for json_string in manifest_lines:
            json_dict = json.loads(json_string) 
            json_dict_list.append(json_dict)
            uniq_id = get_uniqname_from_filepath(json_dict['audio_filepath'])
            uniq_id_list.append(uniq_id)
        return uniq_id_list, json_dict_list
    
    def get_uniq_id(self, rttm_path):
        return rttm_path.split('/')[-1].split('.rttm')[0]
    
    def read_rttm_file(self, rttm_path):
        return open(rttm_path).readlines()
    
    def s2n(self, x, ROUND=2):
        return round(float(x), ROUND)
    
    def parse_rttm(self, rttm_path):
        rttm_lines = self.read_rttm_file(rttm_path)
        uniq_id = self.get_uniq_id(rttm_path)
        speaker_list = []
        for line in rttm_lines:
            rttm = line.strip().split()
            start, end, speaker = self.s2n(rttm[3]), self.s2n(rttm[4]) + self.s2n(rttm[3]), rttm[7]
            speaker_list.append(speaker)
        return set(speaker_list)

    def check_embedding_and_RTTM(self, emb_sess_avg_dict, manifest_filepath):
        uniq_id_list, json_lines_list = self.get_manifest_uniq_ids(manifest_filepath)
        output_json_list = []
        for scale_index in emb_sess_avg_dict.keys():
            for uniq_id, json_dict in zip(uniq_id_list, json_lines_list):
                rttm_filepath = json_dict['rttm_filepath']
                rttm_speaker_set = self.parse_rttm(rttm_filepath)
                dict_speaker_set = set(list(emb_sess_avg_dict[scale_index][uniq_id]['mapping'].keys()))
                dict_speaker_value_set = set(list(emb_sess_avg_dict[scale_index][uniq_id]['mapping'].values()))
                if rttm_speaker_set != dict_speaker_set:
                    remainder_rttm_keys = rttm_speaker_set - dict_speaker_set
                    total_spk_set = set(['speaker_'+str(x) for x in range(len(rttm_speaker_set))])
                    remainder_dict_keys = total_spk_set - dict_speaker_value_set
                    for rttm_key, dict_key in zip(remainder_rttm_keys, remainder_dict_keys):
                        emb_sess_avg_dict[scale_index][uniq_id]['mapping'][rttm_key] = dict_key
                    dict_speaker_set = set(list(emb_sess_avg_dict[scale_index][uniq_id]['mapping'].keys()))
                    assert rttm_speaker_set == dict_speaker_set
        return emb_sess_avg_dict

    def run_clustering_diarizer(self, cfg_split_ds):
        """
        MSDD
        Run clustering diarizer to get initial clustering results.
        """
        manifest_filepath = cfg_split_ds.manifest_filepath
        emb_dir = cfg_split_ds.emb_dir
        # import ipdb; ipdb.set_trace()
        isEmbReady = True
        if os.path.exists(f'{emb_dir}/speaker_outputs/embeddings'):
            print(f"-- Embedding path exists {emb_dir}/speaker_outputs/embeddings")
            try:
                try:
                    emb_sess_avg_dict, session_scale_mapping_dict = self.load_dict_from_pkl(emb_dir) 
                except:
                    emb_scale_seq_dict = self.load_emb_scale_seq_dict(emb_dir)
                    print("---- Scale embeddings exist, but average embedding results do not exist. Calculating average emb result.")
                    score = self.run_multiscale_clustering(manifest_filepath, emb_scale_seq_dict, cfg_split_ds.emb_dir, run_clustering=False)
                    metric, speaker_mapping_dict = score
                    session_scale_mapping_dict = self.get_scale_map(self.clusdiar_model.embs_and_timestamps)
                    emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict = self.load_embeddings_from_pickle(cfg_split_ds.emb_dir, 
                                                                                                              speaker_mapping_dict, 
                                                                                                              session_scale_mapping_dict)
                    self.save_dict_as_pkl(emb_dir, emb_sess_avg_dict, speaker_mapping_dict, session_scale_mapping_dict)

                uniq_id_list, _ = self.get_manifest_uniq_ids(manifest_filepath)
                base_scale_index = max(emb_sess_avg_dict.keys())
                condA = set(uniq_id_list).issubset(set(emb_sess_avg_dict[base_scale_index].keys()))
                condB = set(uniq_id_list).issubset(set(session_scale_mapping_dict.keys()))
                isEmbReady = condA and condB

            except:
                isEmbReady = False
        else:
            isEmbReady = False
        
        if isEmbReady:    
            print(f"--- Embedding isEmbReady: {isEmbReady}")
            speaker_mapping_dict = self.load_mapping_from_pkl(emb_dir) 
            emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict = self.load_embeddings_from_pickle(emb_dir, 
                                                                                                  speaker_mapping_dict, 
                                                                                                  session_scale_mapping_dict)
            if self.run_clus_from_loaded_emb:
                score = self.run_multiscale_clustering(manifest_filepath, 
                                                       emb_scale_seq_dict, 
                                                       emb_dir, 
                                                       run_clustering=True)
                metric, speaker_mapping_dict = score
                session_scale_mapping_dict = self.get_scale_map(self.clusdiar_model.embs_and_timestamps)
                emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict = self.load_embeddings_from_pickle(emb_dir, 
                                                                                                          speaker_mapping_dict, 
                                                                                                          session_scale_mapping_dict)
                self.save_dict_as_pkl(emb_dir, emb_sess_avg_dict, speaker_mapping_dict, session_scale_mapping_dict)
                emb_scale_seq_dict['session_scale_mapping'] = session_scale_mapping_dict
                return emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict
        else:
            print("--- Embedding path does not exist")
            self.cfg_base.diarizer.manifest_filepath = manifest_filepath
            self.cfg_base.diarizer.out_dir = emb_dir
            import ipdb; ipdb.set_trace
            self.clusdiar_model = ClusteringDiarizer(cfg=self.cfg_base)
            score = self.clusdiar_model.diarize(batch_size=self.cfg_base.batch_size)
            metric, speaker_mapping_dict = score 
            session_scale_mapping_dict = self.get_scale_map(self.clusdiar_model.embs_and_timestamps)
            emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict = self.load_embeddings_from_pickle(emb_dir, 
                                                                                                      speaker_mapping_dict, 
                                                                                                      session_scale_mapping_dict)
            self.save_dict_as_pkl(emb_dir, emb_sess_avg_dict, speaker_mapping_dict, session_scale_mapping_dict)

        logging.info("Checking clustering results and rttm files. Don't use this for inference. This is for training.")
        emb_sess_avg_dict = self.check_embedding_and_RTTM(emb_sess_avg_dict, manifest_filepath)
        logging.info("Clustering results and rttm files test passed.")
        emb_scale_seq_dict['session_scale_mapping'] = session_scale_mapping_dict
        return emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict
    
    def extract_time_stamps(self, manifest_file):
        self.time_stamps = {}
        with open(manifest_file, 'r', encoding='utf-8') as manifest:
            for i, line in enumerate(manifest.readlines()):
                line = line.strip()
                dic = json.loads(line)
                uniq_name = get_uniqname_from_filepath(dic['audio_filepath'])
                if uniq_name not in self.time_stamps:
                    self.time_stamps[uniq_name] = []
                start = dic['offset']
                end = start + dic['duration']
                stamp = '{:.3f} {:.3f} '.format(start, end)
                self.time_stamps[uniq_name].append(stamp)
        return self.time_stamps

    def load_embs_and_timestamps(self, manifest_filepath, emb_scale_seq_dict, out_dir, run_clustering=False):
        for scale_idx, (window, shift) in self.clusdiar_model.multiscale_args_dict['scale_dict'].items():
            subsegments_manifest_path = os.path.join(self._cfg_msdd.test_ds.emb_dir, 'speaker_outputs', f'subsegments_scale{scale_idx}.json')
            self.embeddings = emb_scale_seq_dict[scale_idx]
            self.time_stamps = self.extract_time_stamps(subsegments_manifest_path)
            self.clusdiar_model.multiscale_embeddings_and_timestamps[scale_idx] = [self.embeddings, self.time_stamps]

        self.embs_and_timestamps = get_embs_and_timestamps(
            self.clusdiar_model.multiscale_embeddings_and_timestamps, self.clusdiar_model.multiscale_args_dict
        )


    def run_multiscale_clustering(self, manifest_filepath, emb_scale_seq_dict, emb_dir, run_clustering=False):
        self.cfg_base.diarizer.out_dir = emb_dir
        if not self.clusdiar_model:
            self.clusdiar_model = ClusteringDiarizer(cfg=self.cfg_base)
        self.clusdiar_model.AUDIO_RTTM_MAP = audio_rttm_map(manifest_filepath)
        
        if self.multiscale_weights_list:
            self.clusdiar_model.multiscale_args_dict['multiscale_weights'] = self.multiscale_weights_list
        
        self.clusdiar_model._out_dir = emb_dir
        out_rttm_dir = os.path.join(emb_dir, 'pred_rttms')

        # Segmentation
        for scale_idx, (window, shift) in self.clusdiar_model.multiscale_args_dict['scale_dict'].items():
            subsegments_manifest_path = os.path.join(emb_dir, 'speaker_outputs', f'subsegments_scale{scale_idx}.json')
            self.embeddings = emb_scale_seq_dict[scale_idx]
            self.time_stamps = self.extract_time_stamps(subsegments_manifest_path)
            self.clusdiar_model.multiscale_embeddings_and_timestamps[scale_idx] = [self.embeddings, self.time_stamps]
        self.clusdiar_model.embs_and_timestamps = get_embs_and_timestamps(
            self.clusdiar_model.multiscale_embeddings_and_timestamps, self.clusdiar_model.multiscale_args_dict
        )
        
        if run_clustering:
            # Clustering
            print("======== Custering params: ", self.clusdiar_model._cluster_params)
            all_reference, all_hypothesis = perform_clustering(
                embs_and_timestamps=self.clusdiar_model.embs_and_timestamps,
                AUDIO_RTTM_MAP=self.clusdiar_model.AUDIO_RTTM_MAP,
                out_rttm_dir=out_rttm_dir,
                clustering_params=self.clusdiar_model._cluster_params,
            )
        else:
            all_hypothesis, all_reference, lines_cluster_labels = [], [], []
            no_references = False
            for uniq_id, value in tqdm(self.clusdiar_model.AUDIO_RTTM_MAP.items()):
                with open(f"{out_rttm_dir}/{uniq_id}.rttm") as f:
                    rttm_lines = f.readlines()
                labels = self.convert_rttm_to_labels(rttm_lines)

                hypothesis = labels_to_pyannote_object(labels, uniq_name=uniq_id)
                all_hypothesis.append([uniq_id, hypothesis])
                base_scale_idx = max(self.clusdiar_model.embs_and_timestamps[uniq_id]['scale_dict'].keys())
                rttm_file = value.get('rttm_filepath', None)
                if rttm_file is not None and os.path.exists(rttm_file) and not no_references:
                    ref_labels = rttm_to_labels(rttm_file)
                    reference = labels_to_pyannote_object(ref_labels, uniq_name=uniq_id)
                    all_reference.append([uniq_id, reference])
                else:
                    no_references = True
                    all_reference = []

        self.clusdiar_model._diarizer_params.collar = 0.0
        self.clusdiar_model._diarizer_params.ignore_overlap = False
        
        # Scoring
        self.score = score_labels(
            self.clusdiar_model.AUDIO_RTTM_MAP,
            all_reference,
            all_hypothesis,
            collar=self.clusdiar_model._diarizer_params.collar,
            ignore_overlap=self.clusdiar_model._diarizer_params.ignore_overlap,
        )
        return self.score
        
    def convert_rttm_to_labels(self, rttm_lines):
        labels = []
        for line in rttm_lines:
            ls = line.split()
            start = ls[3]
            end = str(float(ls[3])+float(ls[4]))
            speaker = ls[7]
            labels.append(f"{start} {end} {speaker}")
        return labels
    
    def load_dict_from_pkl(self, out_dir): 
        with open(f'{out_dir}/{self.clus_emb_path}', 'rb') as handle:
            emb_sess_avg_dict = pkl.load(handle)
        with open(f'{out_dir}/{self.scale_map_path}', 'rb') as handle:
            session_scale_mapping_dict  = pkl.load(handle)
        return emb_sess_avg_dict, session_scale_mapping_dict
    
    def load_mapping_from_pkl(self, out_dir): 
        with open(f'{out_dir}/{self.clus_map_path}', 'rb') as handle:
            speaker_mapping_dict = pkl.load(handle)
        return speaker_mapping_dict

    def save_dict_as_pkl(self, out_dir, emb_sess_avg_dict, speaker_mapping_dict, session_scale_mapping_dict):
        print(f"Saving clustering and avg_emb files: \n {out_dir}/{self.clus_emb_path}, \n {out_dir}/{self.clus_map_path} \n {out_dir}/{self.scale_map_path}")
        with open(f'{out_dir}/{self.clus_emb_path}', 'wb') as handle:
            pkl.dump(emb_sess_avg_dict, handle, protocol=pkl.HIGHEST_PROTOCOL)
        with open(f'{out_dir}/{self.clus_map_path}', 'wb') as handle:
            pkl.dump(speaker_mapping_dict, handle, protocol=pkl.HIGHEST_PROTOCOL)
        with open(f'{out_dir}/{self.scale_map_path}', 'wb') as handle:
            pkl.dump(session_scale_mapping_dict, handle, protocol=pkl.HIGHEST_PROTOCOL)
    
    def get_scale_map(self, embs_and_timestamps):
        session_scale_mapping_dict = {}
        for uniq_id, uniq_embs_and_timestamps in embs_and_timestamps.items():
            scale_mapping_dict = getMultiScaleCosAffinityMatrix(uniq_embs_and_timestamps)
            session_scale_mapping_dict[uniq_id] = scale_mapping_dict
        return session_scale_mapping_dict
    
    def load_emb_scale_seq_dict(self, out_dir):
        window_len_list = list(self.cfg_base.diarizer.speaker_embeddings.parameters.window_length_in_sec)
        emb_scale_seq_dict = {scale_index: None for scale_index in range(len(window_len_list))}
        for scale_index in range(len(window_len_list)):
            pickle_path = os.path.join(out_dir, 'speaker_outputs', 'embeddings', f'subsegments_scale{scale_index}_embeddings.pkl')
            print(f"Loading embedding pickle file of scale:{scale_index} at {pickle_path}")
            with open(pickle_path, "rb") as input_file:
                emb_dict = pkl.load(input_file)
            for key, val in emb_dict.items():
                emb_dict[key] = val
            emb_scale_seq_dict[scale_index] = emb_dict
        return emb_scale_seq_dict

    def load_embeddings_from_pickle(self, out_dir, speaker_mapping_dict, session_scale_mapping_dict):
        """
        MSDD
        Load embeddings from diarization result folder.
        """
        scale_index = 0
        emb_scale_seq_dict = self.load_emb_scale_seq_dict(out_dir)
        window_len_list = list(self.cfg_base.diarizer.speaker_embeddings.parameters.window_length_in_sec)
        base_scale_index = len(window_len_list) - 1
        clus_label_path = os.path.join(out_dir, 'speaker_outputs', f'subsegments_scale{base_scale_index}_cluster.label')
        print(f"Loading cluster label file at {clus_label_path}...")
        with open(clus_label_path) as f:
            clus_label = f.readlines()
        emb_sess_avg_dict, base_clus_label_dict = self.get_clus_emb(emb_scale_seq_dict, clus_label, speaker_mapping_dict, session_scale_mapping_dict)
        return emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict


class EncDecDiarLabelModel(ModelPT, ExportableEncDecModel, ClusterEmbedding):
    """Encoder decoder class for multiscale speaker diarization decoder.
    Model class creates training, validation methods for setting up data
    performing model forward pass.
    Expects config dict for
    * preprocessor
    * msdd_model
    """

    @classmethod
    def list_available_models(cls) -> List[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.
        Returns:
            List of available pre-trained models.
        """
        result = []
        return None

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        self.bi_ch_infer = True
        self.cfg_msdd_model = cfg
        self.cfg_msdd_model.msdd_module.num_spks = self.cfg_msdd_model.max_num_of_spks
        
        self.cfg_msdd_model.train_ds.num_spks = self.cfg_msdd_model.max_num_of_spks
        self.cfg_msdd_model.validation_ds.num_spks = self.cfg_msdd_model.max_num_of_spks
        self.cfg_msdd_model.test_ds.num_spks = self.cfg_msdd_model.max_num_of_spks
        ClusterEmbedding.__init__(self, 
                                  cfg_base=self.cfg_msdd_model.base,
                                  cfg_msdd_model=self.cfg_msdd_model)
        if trainer:
            self.load_split_emb_clus()
        super().__init__(cfg=self.cfg_msdd_model, trainer=trainer)
        self.world_size = 1
        if trainer is not None:
            self.world_size = trainer.num_nodes * trainer.num_devices
            self.bi_ch_infer = False

        self.preprocessor = EncDecDiarLabelModel.from_config_dict(self.cfg_msdd_model.preprocessor)
        self.msdd = EncDecDiarLabelModel.from_config_dict(self.cfg_msdd_model.msdd_module)
        self.loss = BCELoss(weight=None)
        self.task = None
        self._accuracy_test = MultiBinaryAccuracy()
        self._accuracy_train = MultiBinaryAccuracy()
        self._accuracy_valid = MultiBinaryAccuracy()
        self.labels = None

    def get_emb_clus_infer(self, emb_clus):
        self.emb_sess_test_dict = emb_clus.emb_sess_test_dict
        self.clus_test_label_dict = emb_clus.clus_test_label_dict
        self.emb_seq_test = emb_clus.emb_seq_test

    def load_test_emb_clus_infer(self):
        self.emb_sess_test_dict, self.emb_seq_test, self.clus_test_label_dict = self.load_clustering_results(self.cfg_msdd_model.test_ds.manifest_filepath,
                                                               self.cfg_msdd_model.test_ds.emb_dir)
    
    def load_split_emb_clus(self):
        self.emb_sess_train_dict, self.emb_seq_train, self.clus_train_label_dict = self.load_clustering_results(self.cfg_msdd_model.train_ds.manifest_filepath,
                                                                self.cfg_msdd_model.train_ds.emb_dir)
        
        self.emb_sess_dev_dict, self.emb_seq_dev, self.clus_dev_label_dict = self.load_clustering_results(self.cfg_msdd_model.validation_ds.manifest_filepath,
                                                              self.cfg_msdd_model.validation_ds.emb_dir)

        self.emb_sess_test_dict, self.emb_seq_test, self.clus_test_label_dict = self.load_clustering_results(self.cfg_msdd_model.test_ds.manifest_filepath,
                                                               self.cfg_msdd_model.test_ds.emb_dir)

    def get_emb_clus(self, emb_clus):
        self.emb_sess_train_dict = emb_clus.emb_sess_train_dict
        self.emb_sess_dev_dict = emb_clus.emb_sess_dev_dict
        self.emb_sess_test_dict = emb_clus.emb_sess_test_dict
        self.clus_train_label_dict = emb_clus.clus_train_label_dict
        self.clus_dev_label_dict = emb_clus.clus_dev_label_dict
        self.clus_test_label_dict = emb_clus.clus_test_label_dict
        self.emb_seq_train = emb_clus.emb_seq_train
        self.emb_seq_dev = emb_clus.emb_seq_dev
        self.emb_seq_test = emb_clus.emb_seq_test
    
    def load_clustering_results(self, manifest_filepath, out_dir):
        """
        MSDD
        Run clustering diarizer to get initial clustering results.
        """
        isEmbReady = True
        if os.path.exists(f'{out_dir}/speaker_outputs/embeddings'):
            print(f"-- Embedding path exists {out_dir}/speaker_outputs/embeddings")
            emb_sess_avg_dict, session_scale_mapping_dict = self.load_dict_from_pkl(out_dir) 
            uniq_id_list, _ = self.get_manifest_uniq_ids(manifest_filepath)
            base_scale_index = max(emb_sess_avg_dict.keys())
            condA = set(uniq_id_list).issubset(set(emb_sess_avg_dict[base_scale_index].keys()))
            condB = set(uniq_id_list).issubset(set(session_scale_mapping_dict.keys()))
            isEmbReady = condA and condB
        else:
            isEmbReady = False
        
        if isEmbReady:    
            print(f"--- Embedding-EmbReady: {isEmbReady}")
            print(f"--- Loading mapping file: {out_dir}")
            speaker_mapping_dict = self.load_mapping_from_pkl(out_dir) 
            print(f"--- Loading embeddings file from: {out_dir}")
            emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict = self.load_embeddings_from_pickle(out_dir, 
                                                                                                      speaker_mapping_dict, 
                                                                                                      session_scale_mapping_dict)
        else:
            raise ValueError('Embeddings are not extracted properly. Check the following manifest filepath: {manifest_filepath} and embedding directory: {out_dir}')
        logging.info("Checking clustering results and rttm files.")
        emb_sess_avg_dict = self.check_embedding_and_RTTM(emb_sess_avg_dict, manifest_filepath)
        logging.info("Clustering results and rttm files test passed.")
        emb_scale_seq_dict['session_scale_mapping'] = session_scale_mapping_dict
        return emb_sess_avg_dict, emb_scale_seq_dict, base_clus_label_dict


    # @staticmethod
    # def extract_labels(data_layer_config):
        # labels = set()
        # manifest_filepath = data_layer_config.get('manifest_filepath', None)
        # if manifest_filepath is None:
            # logging.warning("No manifest_filepath was provided, no labels got extracted!")
            # return None
        # manifest_filepaths = convert_to_config_list(data_layer_config['manifest_filepath'])

        # for manifest_filepath in itertools.chain.from_iterable(manifest_filepaths):
            # collection = ASRSpeechLabel(
                # manifests_files=manifest_filepath,
                # min_duration=data_layer_config.get("min_duration", None),
                # max_duration=data_layer_config.get("max_duration", None),
                # index_by_file_id=True,  # Must set this so the manifest lines can be indexed by file ID
            # )
            # labels.update(collection.uniq_labels)
        # labels = list(labels)
        # logging.warning(f"Total number of {len(labels)} found in all the manifest files.")
        # return labels

    def replace_with_inferred_rttm(self, config):
        json_path = config['manifest_filepath']
        dict_list = []
        with open(json_path, 'r', encoding='utf-8') as manifest:
            for i, line in enumerate(manifest.readlines()):
                line = line.strip()
                dic = json.loads(line)
                pred_rttms_base_path = os.path.join(self.cfg.test_ds.emb_dir, 'pred_rttms')
                uniq_id = self.get_uniq_id(dic['rttm_filepath'])
                dic['rttm_filepath'] = os.path.join(pred_rttms_base_path, f"{uniq_id}.rttm")
                assert os.path.exists(dic['rttm_filepath']) == True
                dict_list.append(dic)
        new_json_path = json_path.replace('.json', '.infer.json')

        with open(new_json_path, 'w') as outfile:
            for dic in dict_list:
                json.dump(dic, outfile)
                outfile.write('\n')
        return new_json_path

    def __setup_dataloader_from_config(self, config: Optional[Dict], emb_dict: Dict, emb_seq: Dict, clus_label_dict: Dict, bi_ch_infer=False):
        featurizer = WaveformFeaturizer(
            sample_rate=config['sample_rate'], int_values=config.get('int_values', False), augmentor=None
        )
        shuffle = config.get('shuffle', False)

        if 'manifest_filepath' in config and config['manifest_filepath'] is None:
            logging.warning(f"Could not load dataset as `manifest_filepath` was None. Provided config : {config}")
            return None
        
        dataset = AudioToSpeechMSDDDataset(
            manifest_filepath=config['manifest_filepath'],
            emb_dir_path=config['emb_dir'],
            emb_dict=emb_dict,
            clus_label_dict=clus_label_dict,
            emb_seq=emb_seq,
            soft_label_thres=config.soft_label_thres,
            featurizer=featurizer,
            max_spks=config.num_spks,
            bi_ch_infer=bi_ch_infer,
        )
        self.data_collection = dataset.collection
        collate_ds = dataset
        collate_fn = collate_ds.msdd_collate_fn
        batch_size = config['batch_size']
        return torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=batch_size,
            collate_fn=collate_fn,
            drop_last=config.get('drop_last', False),
            shuffle=shuffle,
            num_workers=config.get('num_workers', 0),
            pin_memory=config.get('pin_memory', False),
        )

    def setup_training_data(self, train_data_config: Optional[Union[DictConfig, Dict]]):
        self._train_dl = self.__setup_dataloader_from_config(config=train_data_config, 
                                                             emb_dict=self.emb_sess_train_dict, 
                                                             emb_seq=self.emb_seq_train,
                                                             clus_label_dict=self.clus_train_label_dict)

    def setup_validation_data(self, val_data_layer_config: Optional[Union[DictConfig, Dict]]):
        self._validation_dl = self.__setup_dataloader_from_config(config=val_data_layer_config, 
                                                                  emb_dict=self.emb_sess_dev_dict, 
                                                                  emb_seq=self.emb_seq_dev,
                                                                  clus_label_dict=self.clus_dev_label_dict)

    def setup_test_data(self, test_data_config: Optional[Union[DictConfig, Dict]]):
        self._test_dl = self.__setup_dataloader_from_config(config=test_data_config, 
                                                            emb_dict=self.emb_sess_test_dict, 
                                                            emb_seq=self.emb_seq_test,
                                                            clus_label_dict=self.clus_test_label_dict,
                                                            bi_ch_infer=self.bi_ch_infer)

    def test_dataloader(self):
        if self._test_dl is not None:
            return self._test_dl

    @property
    def input_types(self) -> Optional[Dict[str, NeuralType]]:
        if hasattr(self.preprocessor, '_sample_rate'):
            audio_eltype = AudioSignal(freq=self.preprocessor._sample_rate)
        else:
            audio_eltype = AudioSignal()
        return {
            "input_signal": NeuralType(('B', 'T', 'C', 'D'), audio_eltype),
            "input_signal_length": NeuralType(tuple('B'), LengthsType()),
            "emb_vectors": NeuralType(('B', 'C', 'D', 'C'), EncodedRepresentation()),
            "targets": NeuralType(('B', 'T', 'C'), ProbsType()),
        }

    @property
    def output_types(self):
        return OrderedDict(
                {
                "probs": NeuralType(('B', 'T', 'C'), ProbsType()),
                "scale_weights": NeuralType(('B', 'T', 'C'), ProbsType())
                    }
                )
    
    @typecheck()
    def forward(self, input_signal, input_signal_length, emb_vectors, targets):
        preds, scale_weights = self.msdd(ms_emb_seq=input_signal, length=input_signal_length, ms_avg_embs=emb_vectors, targets=targets)
        return preds, scale_weights

    def training_step(self, batch, batch_idx):
        signals, signal_lengths, targets, emb_vectors = batch
        preds, _ = self.forward(input_signal=signals, 
                             input_signal_length=signal_lengths, 
                             emb_vectors=emb_vectors,
                             targets=targets)
        loss = self.loss(probs=preds, 
                         labels=targets, 
                         signal_lengths=signal_lengths)
        self._accuracy_train(preds, targets, signal_lengths)
        f1_acc = self._accuracy_train.compute()
        self.log('loss', loss)
        self.log('learning_rate', self._optimizer.param_groups[0]['lr'])
        self.log('train_f1_acc', f1_acc)
        self._accuracy_train.reset()
        return {'loss': loss}

    def validation_step(self, batch, batch_idx, dataloader_idx: int = 0):
        signals, signal_lengths, targets, emb_vectors = batch
        preds, _ = self.forward(input_signal=signals, 
                             input_signal_length=signal_lengths, 
                             emb_vectors=emb_vectors,
                             targets=targets)
        loss = self.loss(probs=preds, 
                         labels=targets, 
                         signal_lengths=signal_lengths)
        self._accuracy_valid(preds, targets, signal_lengths)
        f1_acc = self._accuracy_valid.compute()
        return {
            'val_loss': loss,
            'val_f1_acc': f1_acc,
        }

    def multi_validation_epoch_end(self, outputs, dataloader_idx: int = 0):
        val_loss_mean = torch.stack([x['val_loss'] for x in outputs]).mean()
        f1_acc = self._accuracy_valid.compute()
        self._accuracy_valid.reset()

        self.log('val_loss', val_loss_mean)
        self.log('val_f1_acc', f1_acc)
        return {
            'val_loss': val_loss_mean,
            'val_f1_acc': f1_acc,
        }

    def test_step(self, batch, batch_idx, dataloader_idx: int = 0):
        signals, signal_lengths, targets, emb_vectors = batch
        preds, _ = self.forward(input_signal=signals, 
                             input_signal_length=signal_lengths, 
                             emb_vectors=emb_vectors,
                             targets=targets)
        loss = self.loss(probs=preds, 
                         labels=targets, 
                         signal_lengths=signal_lengths)
        self._accuracy_test(preds, targets, signal_lengths)
        f1_acc = self._accuracy_test.compute()
        return {
            'test_loss': loss,
            'test_f1_acc': f1_acc,
        }

    def multi_test_epoch_end(self, outputs, dataloader_idx: int = 0):
        test_loss_mean = torch.stack([x['test_loss'] for x in outputs]).mean()
        f1_acc = self._accuracy_test.compute()
        self._accuracy_test.reset()
        self.log('val_f1_acc', f1_acc)
        return {
            'test_loss': test_loss_mean,
            'test_f1_acc': f1_acc,
        }
