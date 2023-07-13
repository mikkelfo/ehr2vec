"""Create tokenized features from formatted data. config template: data.yaml"""
import os
from os.path import join

import torch

from common.config import load_config
from common.setup import prepare_directory
from data.concept_loader import ConceptLoader
from data.featuremaker import FeatureMaker
from data.split import Splitter
from data.tokenizer import EHRTokenizer
from data_fixes.exclude import Excluder
from data_fixes.handle import Handler

config_path = join('configs', 'data_pretrain.yaml')

def main_data(config_path):
    """
        Loads data
        Finds outcomes
        Creates features
        Handles wrong data
        Excludes patients with <k concepts
        Splits data
        Tokenizes
        Saves
    """
    cfg = load_config(config_path)
    logger = prepare_directory(config_path, cfg)  
    
    logger.info('Starting data processing')
    concepts, patients_info = ConceptLoader()(**cfg.loader)
   
    logger.info("Creating feature sequences")
    features = FeatureMaker(cfg.features)(concepts, patients_info)
    features = Handler(**cfg.handler)(features)
    logger.info("Exclude patients with <k concepts")
    features, _, pids = Excluder(**cfg.excluder)(features)
    torch.save(features, join(cfg.output_dir, 'features', f'features.pt'))
    torch.save(pids, join(cfg.output_dir, 'features', f'pids_features.pt'))
    
    print("Splitting data")
    splitter = Splitter(ratios=cfg.split_ratios)
    features_split, pids_split = splitter(features, pids)

    if not os.path.exists(cfg.output_dir):
        os.makedirs(cfg.output_dir)
    splitter.save(cfg.output_dir)
    train, test, val = features_split['train'], features_split['test'], features_split['val']

    torch.save(pids_split['train'], join(cfg.output_dir, 'train_pids.pt'))
    torch.save(pids_split['test'], join(cfg.output_dir, 'test_pids.pt'))
    torch.save(pids_split['val'], join(cfg.output_dir, 'val_pids.pt'))
    torch.save(train, join(cfg.output_dir, 'train.pt'))
    torch.save(test, join(cfg.output_dir, 'test.pt'))
    torch.save(val, join(cfg.output_dir, 'val.pt'))
    
    print("Tokenizing")
    tokenizer = EHRTokenizer(config=cfg.tokenizer)
    train_encoded = tokenizer(train)
    tokenizer.freeze_vocabulary()
    tokenizer.save_vocab(join(cfg.output_dir, 'vocabulary.pt'))
    test_encoded = tokenizer(test)
    val_encoded = tokenizer(val)
    torch.save(train_encoded, join(cfg.output_dir,'tokenized','train_encoded.pt'))
    torch.save(val_encoded, join(cfg.output_dir, 'tokenized','val_encoded.pt'))
    torch.save(test_encoded, join(cfg.output_dir, 'tokenized','test_encoded.pt'))
    torch.save(tokenizer.vocabulary, join(cfg.output_dir, 'vocabulary.pt'))
    
    logger.info('Finished data processing')

if __name__ == '__main__':
    main_data(config_path)

