import logging
import os
from os.path import join
from typing import Dict, List, Tuple, Union

import numpy as np
import pandas as pd
import torch
from common.utils import Data
from data.dataset import (BinaryOutcomeDataset, HierarchicalMLMDataset,
                          MLMDataset)
from data_fixes.adapt import BehrtAdapter
from data_fixes.censor import Censorer
from data_fixes.exclude import Excluder
from data_fixes.handle import Handler
from data_fixes.truncate import Truncator
from transformers import BertConfig

logger = logging.getLogger(__name__)  # Get the logger for this module

PID_KEY = 'PID'
VOCABULARY_FILE = 'vocabulary.pt'
BG_GENDER_KEYS = {
    'male': ['M', 'Kvinde', 'male', 'Male', 'man', 'MAN', '1'],
    'female': ['W', 'Mand', 'F', 'female', 'woman', 'WOMAN', '0']
}
MIN_POSITIVES = {'train': 10, 'val': 5}


def load_model(model_class, cfg, add_config={}):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    checkpoint_path = join(cfg.paths.model_path, 'checkpoints' ,f'checkpoint_epoch{cfg.paths.checkpoint_epoch}_end.pt')
    # Load the config from file
    config = BertConfig.from_pretrained(cfg.paths.model_path) 
    config.update(add_config)
    model = model_class(config)
    load_result = model.load_state_dict(torch.load(checkpoint_path, map_location=device)['model_state_dict'], strict=False)
    missing_keys = load_result.missing_keys

    if len([k for k in missing_keys if k.startswith('embeddings')])>0:
        pretrained_model_embeddings = model.embeddings.__class__.__name__
        raise ValueError(f"Embeddings not loaded. Ensure that model.behrt_embeddings is compatible with pretrained model embeddings {pretrained_model_embeddings}.")
    logger.warning("missing state dict keys: %s", missing_keys)
    return model

def create_binary_outcome_datasets(all_outcomes, cfg):
    """
    This function is used to create outcome datasets based on the configuration provided.
    """
    outcomes, censor_outcomes, pids = DatasetPreparer._retrieve_outcomes(all_outcomes, cfg)
    if cfg.get("encode_pos_only", False):
        outcomes, censor_outcomes, pids = select_positives(outcomes, censor_outcomes, pids)
        cfg.train_data.num_patients = None
        cfg.val_data.num_patients = None
    if cfg.train_data.num_patients == 0:
        train_dataset = None
    else:
        train_dataset = BinaryOutcomeDataset(cfg.paths.data_path, 'train', outcomes, 
                                    censor_outcomes=censor_outcomes, 
                                    outcome_pids=pids,
                                    num_patients=cfg.train_data.num_patients,
                                    pids=pids if cfg.get("encode_pos_only", False) else None,
                                    n_hours=cfg.outcome.n_hours,
                                    n_procs=cfg.train_data.get('n_procs', None))

    if cfg.val_data.num_patients == 0:
        val_dataset = None
    else:
        val_dataset = BinaryOutcomeDataset(cfg.paths.data_path, 'val',  outcomes, 
                                    censor_outcomes=censor_outcomes, 
                                    outcome_pids=pids,
                                    num_patients=cfg.val_data.num_patients,
                                    pids=pids if cfg.get("encode_pos_only", False) else None, 
                                    n_hours=cfg.outcome.n_hours,
                                    n_procs=cfg.val_data.get('n_procs', None),
                                    )
    
    return train_dataset, val_dataset, outcomes

def select_positives(outcomes, censor_outcomes, pids):
    """Select only positive outcomes."""
    logger.info("Selecting only positive outcomes")
    select_indices = set([i for i, outcome in enumerate(outcomes) if pd.notna(outcome)])
    outcomes = [outcomes[i] for i in select_indices]
    censor_outcomes = [censor_outcomes[i] for i in select_indices]
    pids = [pids[i] for i in select_indices]
    return outcomes, censor_outcomes, pids

# TODO: Add option to load test set only!

class DatasetPreparer:
    SPECIAL_CODES = ['[', 'BG_', 'BG']
    def __init__(self, cfg):
        self.cfg = cfg
        self.run_folder = join(self.cfg.paths.output_path, self.cfg.paths.run_name)
        os.makedirs(self.run_folder, exist_ok=True)

    def prepare_mlm_dataset(self):
        """Load data, truncate, adapt features, create dataset"""
        train_features, val_features, vocabulary = self._prepare_mlm_features()

        train_dataset = MLMDataset(train_features, vocabulary, **self.cfg.data.dataset)
        val_dataset = MLMDataset(val_features, vocabulary, **self.cfg.data.dataset)
        
        return train_dataset, val_dataset

    def prepare_hmlm_dataset(self):
        train_features, val_features, vocabulary = self._prepare_mlm_features()

        tree, tree_matrix, h_vocabulary = self._load_tree()
        
        torch.save(h_vocabulary, join(self.run_folder, 'h_vocabulary.pt'))

        train_dataset = HierarchicalMLMDataset(train_features, vocabulary, 
                                            h_vocabulary, tree, tree_matrix, 
                                            **self.cfg.data.dataset)
        val_dataset = HierarchicalMLMDataset(val_features, vocabulary, 
                                            h_vocabulary, tree, tree_matrix, 
                                            **self.cfg.data.dataset)
        return train_dataset, val_dataset

    def prepare_finetune_features(self)->Tuple[dict]:
        """
        Prepare the features for fine-tuning. 
        The process includes:
        1. Loading tokenized data
        2. Excluding pretrain patients
        3. Optional select random subset
        4. Optional: Select male/female
        5. Loading and processing outcomes
        6. Optional: Select only patients with a censoring outcome
        7. Filter patients with outcome before censoring
        8. Optional: Filter code types (e.g. only diagnoses)
        9. Data censoring
        10. Exclude patients with less than k concepts
        11. Optional: Select patients by age
        12. Optional: Remove background tokens
        13. Truncation
        14. Normalize segments
        
        """
        data_cfg = self.cfg.data
        paths_cfg = self.cfg.paths

        # 1. Loading tokenized data
        features, pids, vocabulary = self._load_tokenized_finetune_data()
        data = Data(features, pids, vocabulary=vocabulary, mode='val')
        datasets = {'val': data}
        # 2. Excluding pretrain patients
        datasets = self._process_datasets(datasets, self._exclude_pretrain_patients) 
        # 3. Patient selection
        if data_cfg.get('num_patients', False):
            datasets = self._process_datasets(datasets, self._select_random_subset, 
                                              {'val': {'num_patients':data_cfg.num_patients}})
        # 4. Optinally select gender group
        if data_cfg.get('gender', False):
            datasets = self._process_datasets(datasets, self._select_by_gender)
        # 5. Loading and processing outcomes
        logger.info(f'Load outcomes from {paths_cfg.outcome} and censoring outcomes from {paths_cfg.censor if paths_cfg.get("censor", False) else paths_cfg.outcome}')
        outcomes = torch.load(paths_cfg.outcome)
        censor_outcomes = torch.load(paths_cfg.censor) if paths_cfg.get('censor', False) else outcomes   
        logger.info("Assigning outcomes to data")
        datasets = self._process_datasets(datasets, self._retrieve_and_assign_outcomes, 
                                           {'val': {'outcomes': outcomes, 'censor_outcomes': censor_outcomes}})
        self._log_pos_patients_num(datasets)
        # 6. Optionally select patients of interest
        if data_cfg.get("select_censored", False):
            datasets = self._process_datasets(datasets, self._select_censored)
            self._log_pos_patients_num(datasets)
        # 7. Filter patients with outcome before censoring
        if self.cfg.outcome.type != self.cfg.outcome.get('censor_type', None):
            datasets = self._process_datasets(datasets, self._filter_outcome_before_censor) # !Timeframe (earlier instance of outcome)
            self._log_pos_patients_num(datasets)
        
        # 8. Filter code types
        if data_cfg.get('code_types', False):
            datasets = self._process_datasets(datasets, self._filter_code_types)
            self._log_pos_patients_num(datasets)
        # 9. Data censoring
        datasets = self._process_datasets(datasets, self._censor_data)
        self._log_pos_patients_num(datasets)

        # 10. Exclude patients with less than k concepts
        datasets = self._process_datasets(datasets, self._exclude_short_sequences)

        # 11. Select Patients By Age
        if data_cfg.get('min_age', False) or data_cfg.get('max_age', False):
            datasets = self._process_datasets(datasets, self._select_by_age)
            self._log_pos_patients_num(datasets)

        # 12. Optionally Remove Background Tokens
        if data_cfg.get("remove_background", False):
            datasets = self._process_datasets(datasets, self._remove_background)

        # 13. Truncation
        logger.info('Truncating')
        datasets = self._process_datasets(datasets, self._truncate)

        # 14. Normalize Segments
        logger.info('Normalizing segments')
        datasets = self._process_datasets(datasets, self._normalize_segments)

        datasets['val'].check_lengths()

        self._log_pos_patients_num(datasets)
        datasets = self._process_datasets(datasets, self._save_sequence_lengths)
        data = datasets['val']

        if self.cfg.model.get('behrt_embeddings', False):
            data.features = BehrtAdapter().adapt_features(data.features)
        torch.save(data.features, join(self.run_folder, 'features.pt'))
        torch.save(data.pids, join(self.run_folder, 'pids.pt'))

        return data

    def prepare_onehot_features(self)->Tuple[np.ndarray]:
        """Use ft features and map them onto one hot vectors with binary outcomes"""
        train_data = self._load_finetune_data(mode='train')
        val_data = self._load_finetune_data(mode='val')
        token2index, new_vocab = self._get_token_to_index_map(train_data.vocabulary)
        X_train, y_train = self._encode_to_onehot(train_data, token2index)
        X_val, y_val = self._encode_to_onehot(val_data, token2index)
        return X_train, y_train, X_val, y_val, new_vocab
    
    def _encode_to_onehot(self, data:Data, token2index: dict)->Tuple[np.ndarray]:
        """Encode features to one hot and age at the time of last event"""
        AGE_INDEX = 0
         # Initialize arrays
        num_samples = len(data)
        num_features = len(token2index) + 1 # +1 for age
        X = np.zeros((num_samples, num_features), dtype=np.int16)
        y = np.zeros(num_samples, dtype=np.int16)
        # Create an array of keys for faster lookup
        keys_array = np.array(list(token2index.keys())) 
        
        # Vectorized function to map tokens to indices
        token2index_map = np.vectorize(token2index.get)

        for i, (concepts, outcome) in enumerate(zip(data.features['concept'], data.outcomes)):
            y[i] = int(not pd.isna(outcome))
            age = data.features['age'][i][-1]    
            X[i, AGE_INDEX] = age
            
            # Filter and encode unique concepts
            concepts = np.array(concepts)
            unique_concepts = np.unique(concepts)
            valid_concepts_mask = np.isin(unique_concepts, keys_array) # Only keep concepts that are in the token2index map
            filtered_concepts = unique_concepts[valid_concepts_mask]
            concept_indices = token2index_map(filtered_concepts) + 1
            X[i, concept_indices] = 1
        return X, y
                
    # ! Potentially map gender onto one index?
    def _get_token_to_index_map(self, vocabulary:dict)->Tuple[dict]:
        """
        Creates a new mapping from vocbulary values to new integers excluding special tokens
        """
        filtered_tokens = set([v for k, v in vocabulary.items() if not k.startswith('[')])
        token2index = {token: i for i, token in enumerate(filtered_tokens)}        
        new_vocab = {k: token2index[v] for k, v in vocabulary.items() if v in token2index}
        return token2index, new_vocab

    def _load_finetune_data(self, mode: str)->Data:
        """Load features for finetuning"""
        dir_ = self.cfg.paths.finetune_features_path
        train_features = torch.load(join(dir_, f'features_{mode}.pt'))
        outcomes = torch.load(join(dir_, f'outcomes_{mode}.pt'))
        pids = torch.load(join(dir_, f'pids_{mode}.pt'))
        vocabulary = torch.load(join(dir_, 'vocabulary.pt'))
        return Data(train_features, pids, outcomes, vocabulary=vocabulary, mode=mode)

    def save_patient_nums(self, train_data: Data=None, val_data: Data=None, folder:str=None)->None:
        """Save patient numbers for train val including the number of positive patients to a csv file"""
        train_df = pd.DataFrame({'train': [len(train_data), len([t for t in train_data.outcomes if not pd.isna(t)])]}, 
                                index=['total', 'positive'])
        val_df = pd.DataFrame({'val': [len(val_data), len([t for t in val_data.outcomes if not pd.isna(t)])]},
                              index=['total', 'positive'])
        patient_nums = pd.concat([train_df, val_df], axis=1)
        patient_nums.to_csv(join(
            self.run_folder if folder is None else folder, 'patient_nums.csv'), 
                            index_label='Patient Group')

    def _log_pos_patients_num(self, datasets: Dict)->None:
        for mode, data in datasets.items():
            num_positive_patiens = len([t for t in data.outcomes if not pd.isna(t)])
            if num_positive_patiens < MIN_POSITIVES[mode]:
                raise ValueError(f"Number of positive patients is less than 10: {num_positive_patiens}")
            logger.info(f"Positive {mode} patients: {num_positive_patiens}")

    def _process_datasets(self, datasets: Dict, func: callable, args_for_func: Dict=None)->Dict:
        """Apply a function to all datasets in a dictionary"""
        if args_for_func is None:
            args_for_func = {}
        for split, data in datasets.items():
            # Get mode-specific arguments, or an empty dictionary if they don't exist
            mode_args = args_for_func.get(split, {})
            datasets[split] = func(data, **mode_args)
        self._log_patient_nums(func.__name__, datasets)
        return datasets

    def _exclude_short_sequences(self, data: Data)->Data:
        """Exclude patients with less than k concepts"""
        excluder = Excluder(min_len = self.cfg.data.get('min_len', 3),
                            vocabulary=data.vocabulary)
        kept_indices = excluder._exclude(data.features)
        return self._select_entries(data, kept_indices)

    def _select_censored(self, data: Data)->Data:
        """Select only censored patients"""
        kept_indices = [i for i, censor in enumerate(data.censor_outcomes) if not pd.isna(censor)]
        return self._select_entries(data, kept_indices)

    def _censor_data(self, data: Data)->Data:
        """Censors data and removes patients with no data left"""
        censorer = Censorer(self.cfg.outcome.n_hours, vocabulary=data.vocabulary)
        data.features = censorer(data.features, data.censor_outcomes, exclude=False)
        return data
    
    def _filter_code_types(self, data: Data)->Data:
        """Filter code types, e.g. keep only diagnoses. Remove patients with not sufficient data left."""
        keep_codes = self._get_keep_codes()
        special_tokens = set([token for code, token in data.vocabulary.items() if self.code_starts_with(code, self.SPECIAL_CODES)])
        keep_tokens = set([token for code, token in data.vocabulary.items() if self.code_starts_with(code, keep_codes)])

        logger.info(f"Keep only codes starting with: {keep_codes}")
        keep_indices = []
        for patient_index, patient in enumerate(self._iter_patients(data.features)):
            concepts = patient['concept']
            non_special_entries = set([token for token in concepts if token not in special_tokens])
            if len(non_special_entries)>self.cfg.data.get('min_len', 2):
                keep_indices.append(patient_index)
            self._filter_patient(data, patient, keep_tokens, patient_index)
            
        return self._select_entries(data, keep_indices)

    def _filter_patient(self, data: Data, patient: dict, keep_tokens: set, patient_index: int)->None:
        concepts = patient['concept']
        keep_entries = {i:token for i, token in enumerate(concepts) if \
                            token in keep_tokens}
        for k, v in patient.items():
            filtered_list = [v[i] for i in keep_entries]
            data.features[k][patient_index] = filtered_list

    def _get_keep_codes(self)->set:
        """Return a set of codes to keep."""
        return set(self.SPECIAL_CODES + self.cfg.data.code_types)

    @staticmethod
    def code_starts_with(code: int, prefixes: set)->bool:
        """Check if the code starts with any of the given prefixes."""
        return any(code.startswith(prefix) for prefix in prefixes)

    @staticmethod
    def _iter_patients(features: dict) -> dict:
        for i in range(len(features["concept"])):
            yield {key: values[i] for key, values in features.items()}

    def _retrieve_and_assign_outcomes(self, data: Data, outcomes: Dict, censor_outcomes: Dict)->Data:
        """Retrieve outcomes and assign them to the data instance"""
        data.outcomes = self._select_and_order_outcomes_for_patients(outcomes, data.pids, self.cfg.outcome.type)
        if self.cfg.outcome.get('censor_type', None) is not None:
            data.censor_outcomes = self._select_and_order_outcomes_for_patients(censor_outcomes, data.pids, self.cfg.outcome.censor_type)
        else:
            data.censor_outcomes = [None]*len(outcomes)
        return data
        
    def _prepare_mlm_features(self)->Tuple[dict, dict, dict]:   
        """
        1. Load tokenized data
        2. Optional: Select random subset
        3. Truncation
        4. Optional: Remove background tokens        
        """
        train_features, train_pids, val_features, val_pids, vocabulary = self._load_tokenized_data()
        torch.save(vocabulary, join(self.run_folder, VOCABULARY_FILE))
        datasets = {'train': Data(train_features, train_pids, vocabulary=vocabulary, mode='train'),
                    'val': Data(val_features, val_pids, vocabulary=vocabulary, mode='val')}
        # Patient Selection
        if self.cfg.data.get('num_train_patients', False) or self.cfg.data.get('num_val_patients', False):
            datasets = self._process_datasets(datasets, self._select_random_subset, 
                                              {'train': {'num_patients':self.cfg.data.num_train_patients},
                                               'val': {'num_patients':self.cfg.data.num_val_patients}})
            
        if self.cfg.data.get("remove_background", False):
            logger.info("Removing background tokens")
            datasets = self._process_datasets(datasets, self._remove_background)
       
        logger.info(f"Truncating data to {self.cfg.data.truncation_len} tokens")
        datasets = self._process_datasets(datasets, self._truncate)
        logger.info("Normalizing segments")
        datasets = self._process_datasets(datasets, self._normalize_segments)

        datasets['train'].check_lengths()
        datasets['val'].check_lengths()
        self._save_pids(datasets['train'].pids, datasets['val'].pids)
        datasets = self._process_datasets(datasets, self._save_sequence_lengths)
        self._check_max_segment(datasets['train'])
        self._check_max_segment(datasets['val'])
        
        if self.cfg.model.get('behrt_embeddings', False):
            datasets['train'].features = BehrtAdapter().adapt_features(datasets['train'].features)
            datasets['val'].features = BehrtAdapter().adapt_features(datasets['val'].features)
        
        return datasets['train'].features, datasets['val'].features, datasets['train'].vocabulary

    def _truncate(self, data: Data)->Data:
        truncator = Truncator(max_len=self.cfg.data.truncation_len, 
                              vocabulary=data.vocabulary)
        data.features = truncator(data.features)
        return data

    def _normalize_segments(self, data: Data)->Data:
        """Normalize segments after truncation to start with 1 and increase by 1
        or if position_ids present (org. BEHRT version) then normalize those."""
        segments_key = 'position_ids' if 'position_ids' in data.features.keys() else 'segment'
        
        segments = []
        for segment in data.features[segments_key]:
            segments.append(Handler.normalize_segments(segment))
        
        data.features[segments_key] = segments
        return data

    def _remove_background(self, data: Data)->Data:
        """Remove background tokens from features and the first sep token following it"""
        background_indices = self._get_background_indices(data)
        first_index = min(background_indices)
        last_index = max(background_indices)
        for k, token_lists in data.features.items():
            new_tokens_lists = []
            for idx, tokens in enumerate(token_lists):
                new_tokens = [token for j, token in enumerate(tokens) if (j < first_index) or (j > last_index)]
                new_tokens_lists.append(new_tokens)
            data.features[k] = new_tokens_lists 
        return data

    def _get_background_indices(self, data: Data)->List[int]:
        """Get the length of the background sentence"""
        background_tokens = set([v for k, v in data.vocabulary.items() if k.startswith('BG_')])
        example_concepts = data.features['concept'][0] # Assume that all patients have the same background length
        background_indices = [i for i, concept in enumerate(example_concepts) if concept in background_tokens]
        if data.vocabulary['[SEP]'] in example_concepts:
            background_indices.append(max(background_indices)+1)
        return background_indices

    def _load_tokenized_data(self):
        tokenized_dir = self.cfg.paths.get('tokenized_dir', 'tokenized')
        logger.info('Loading tokenized data from %s', tokenized_dir)
        tokenized_data_path = join(self.cfg.paths.data_path, tokenized_dir)
        logger.info("Loading tokenized data train")
        train_features  = torch.load(join(tokenized_data_path, 'tokenized_train.pt'))
        train_pids = torch.load(join(tokenized_data_path,  'pids_train.pt'))
        logger.info("Loading tokenized data val")
        val_features = torch.load(join(tokenized_data_path, 'tokenized_val.pt'))
        val_pids = torch.load(join(tokenized_data_path, 'pids_val.pt'))
        logger.info("Loading vocabulary")
        try:
            vocabulary = torch.load(join(tokenized_data_path, VOCABULARY_FILE))
        except:
            vocabulary = torch.load(join(self.cfg.paths.data_path, VOCABULARY_FILE))
        return train_features, train_pids, val_features, val_pids, vocabulary

    def _load_tokenized_finetune_data(self):
        tokenized_dir = self.cfg.paths.get('tokenized_dir', 'tokenized')
        tokenized_file = self.cfg.paths.get('tokenized_file', 'tokenized_val.pt')
        tokenized_pids = self.cfg.paths.get('tokenized_pids', 'pids_val.pt')
        tokenized_data_path = join(self.cfg.paths.data_path, tokenized_dir)
        logger.info(f"Loading tokenized data from {tokenized_data_path}")
        features  = torch.load(join(tokenized_data_path, tokenized_file))
        pids = torch.load(join(tokenized_data_path,  tokenized_pids))
        logger.info("Loading vocabulary")
        try:
            vocabulary = torch.load(join(tokenized_data_path, VOCABULARY_FILE))
        except:
            vocabulary = torch.load(join(self.cfg.paths.data_path, VOCABULARY_FILE))
        return features, pids, vocabulary

    def _load_tree(self):
        hierarchical_path = join(self.cfg.paths.data_path, 
                                 self.cfg.paths.hierarchical_dir)
        tree = torch.load(join(hierarchical_path, 'tree.pt'))
        tree_matrix = torch.load(join(hierarchical_path, 'tree_matrix.pt'))
        h_vocabulary = torch.load(join(hierarchical_path, VOCABULARY_FILE))
        return tree, tree_matrix, h_vocabulary 

    def _exclude_pretrain_patients(self, data: Data):
        pretrain_pids = set(torch.load(join(self.cfg.paths.model_path, f'pids_{data.mode}.pt')))
        kept_indices = [i for i, pid in enumerate(data.pids) if pid not in pretrain_pids]
        return self._select_entries(data, kept_indices)
    
    def _retrieve_outcomes(self, all_outcomes: Dict, all_censor_outcomes: Dict)->Union[List, List]:
        """From the configuration, load the outcomes and censor outcomes."""
        outcomes = all_outcomes.get(self.cfg.outcome.type, [None]*len(all_outcomes[PID_KEY]))
        censor_outcomes = all_censor_outcomes.get(self.cfg.outcome.get('censor_type', None), [None]*len(outcomes))
        return outcomes, censor_outcomes

    def _select_random_subset(self, data, num_patients, seed=0):
        """Select a num_patients random patients"""
        np.random.seed(seed)
        indices = np.arange(len(data.pids))
        np.random.shuffle(indices)
        indices = indices[:num_patients]
        return self._select_entries(data, indices)

    def _select_by_age(self, data: Data)->Data:
        """
        Assuming that age is given in days. 
        We retrieve the last age of each patient and check whether it's within the range.
        """
        kept_indices = []
        min_age = self.cfg.data.get('min_age', 0)
        max_age = self.cfg.data.get('max_age', 120)
        kept_indices = [i for i, ages in enumerate(data.features['age']) 
                if min_age <= ages[-1] <= max_age]
        return self._select_entries(data, kept_indices)

    def _select_by_gender(self, data):
        """Select only patients of a certain gender"""
        gender_token = self._get_gender_token(data.vocabulary, self.cfg.data.gender)
        kept_indices = [i for i, concepts in enumerate(data.features['concept']) if gender_token in set(concepts)]
        return self._select_entries(data, kept_indices)
    
    def _get_gender_token(self, vocabulary, key):
        """Get the token from the vocabulary corresponding to the gender provided in the config"""
        
        # Determine the gender category
        gender_category = None
        for category, keys in BG_GENDER_KEYS.items():
            if key in keys:
                gender_category = category
                break

        if gender_category is None:
            raise ValueError(f"Unknown gender {key}, please select one of {BG_GENDER_KEYS}")

        # Check the vocabulary for a matching token
        for possible_key in BG_GENDER_KEYS[gender_category]:
            gender_token = vocabulary.get('BG_GENDER_' + possible_key, None)
            if gender_token is not None:
                return gender_token
    
        raise ValueError(f"None of BG_GENDER_+{BG_GENDER_KEYS[gender_category]} found in vocabulary.")
    
    def _filter_outcome_before_censor(self, data: Data)->Data:
        """Filter patients with outcome before censoring and missing censoring when outcome present."""
        kept_indices = []
        for i, (outcome, censor) in enumerate(zip(data.outcomes, data.censor_outcomes)):
            if pd.isna(censor):
                if pd.isna(outcome):
                    kept_indices.append(i)
            elif pd.isna(outcome) or outcome >= (censor + self.cfg.outcome.n_hours):
                kept_indices.append(i)
        return self._select_entries(data, kept_indices)

    @staticmethod
    def _select_entries(data:Data, indices:List)->Data:
        """
        Select entries based on indices. 
        Optionally for outcomes and censor outcomes, if present returns dict of results.
        """
        indices = set(indices)
        data.features = {k: [v[i] for i in indices] for k, v in data.features.items()}
        data.pids = [data.pids[i] for i in indices]
        if data.outcomes is not None:
            data.outcomes = [data.outcomes[i] for i in indices]
        if data.censor_outcomes is not None:
            data.censor_outcomes = [data.censor_outcomes[i] for i in indices]
        return data

    @staticmethod
    def _select_and_order_outcomes_for_patients(all_outcomes: Dict, pids: List, outcome: str) -> List:
        """Select outcomes for patients and order them based on the order of pids"""
        outcome_pids = all_outcomes[PID_KEY]
        outcome_group = all_outcomes[outcome]
        assert len(outcome_pids) == len(outcome_group), "Mismatch between PID_KEY length and outcome_group length"

        # Create a dictionary of positions for each PID for quick lookup
        pid_to_index = {pid: idx for idx, pid in enumerate(outcome_pids)}
        
        outcome_pids = set(outcome_pids)
        if not set(pids).issubset(outcome_pids):
            logger.warn(f"PIDs is not a subset of outcome PIDs, there is a mismatch of {len(set(pids).difference(outcome_pids))} patients") 
        
        outcomes = [outcome_group[pid_to_index[pid]] if pid in outcome_pids else None for pid in pids]

        return outcomes

    def _save_pids(self, train_pids: list, val_pids: list)->None:
        torch.save(train_pids, join(self.run_folder, 'pids_train.pt'))
        torch.save(val_pids, join(self.run_folder, 'pids_val.pt'))

    @staticmethod
    def _log_patient_nums(operation:str, datasets: Dict):
        logger.info(f"After applying {operation}:")
        for split, data in datasets.items():
            logger.info(f"{split}: {len(data.pids)} patients")

    def _save_sequence_lengths(self, data):
        if not data.outcomes:
            sequence_lens = torch.tensor([len(concepts) for concepts in data.features['concept']])
            torch.save(sequence_lens, join(self.run_folder, f'sequences_lengths_{data.mode}.pt'))
            return data
        else:
            pos_indices = set([i for i, outcome in enumerate(data.outcomes) if not pd.isna(outcome)])
            sequence_lens_neg = torch.tensor([len(concepts) for i, concepts in enumerate(data.features['concept']) if i in pos_indices])
            sequence_lens_pos = torch.tensor([len(concepts) for i, concepts in enumerate(data.features['concept']) if i not in pos_indices])
            torch.save(sequence_lens_neg, join(self.run_folder, f'sequences_lengths_{data.mode}_neg.pt'))
            torch.save(sequence_lens_pos, join(self.run_folder, f'sequences_lengths_{data.mode}_pos.pt'))
            return data
    
    def _check_max_segment(self, data: Data)->None:
        """Check max segment"""
        max_segment = max([max(seg) for seg in data.features['segment']])
        if max_segment>=self.cfg.model.type_vocab_size:
            raise ValueError(f"Max segment {max_segment} is larger or equal to type_vocab_size {self.cfg.model.type_vocab_size}\
                             Please adjust type_vocab_size in the config.")
        

