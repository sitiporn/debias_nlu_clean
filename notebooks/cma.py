import os
from typing import Callable, List, Union

import pandas as pd
import numpy as np
from scipy.special import softmax
from scipy.special import expit
import fuse
import causal_utils 
import glob
from kl_general import sharpness_correction
import pickle


PROB_T = Union[List[float], List[List[float]]]


def format_label(label):
    if label == "entailment":
        return "entailment"
    else:
        return "non-entailment"
    
def get_ans(ans: int , test_set: str) -> str:
    if test_set =='mnli_hans':
        if ans == 0:
            return 'entailment'
        else:
            return 'non-entailment' 
    if test_set =='mnli_test':
        gt_key = {0:"entailment",1:"contradiction",2:"neutral"}
        return gt_key[ans]    
    elif test_set == 'fever':
        if ans == 2: # Ref from dictionary in result dir
            return 'contradiction' # since we mapped it in the Allen's reader
        return 'non-contradiction'
    elif test_set == 'qqp': 
        if ans == 1: # Ref from dictionary in result dirs
            return 'paraphrase'
        return 'non-paraphrase'
    else:
        raise NotImplementedError("Does not support test_set: %s"%test_set)
        
    
# model_path='/raid/can/nli_models/reweight_utama_github/'
# task='nli'
# data_path='/ist/users/canu/debias_nlu/data/' + task + '/'
# fusion = fuse.sum_fuse
# test_set='hans'


def get_c(
    data_path: str ,
    model_path: str ,
    fusion: Callable[[PROB_T], PROB_T],
    x0: PROB_T
) -> List[float]:
    df_bias_dev = pd.read_json(
    data_path+'dev_prob_korn_lr_overlapping_sample_weight_3class.jsonl', lines=True)
    bias_dev_score = [b for b in df_bias_dev['bias_probs']]
    bias_dev_score = np.array(bias_dev_score)
    ya1x0_dev = fusion(bias_dev_score,x0)
    df_bert_dev = pd.read_json(model_path+'raw_m.jsonl', lines=True)
    ya1x1prob_dev = []
    for p, h in zip(df_bert_dev['probs'], ya1x0_dev):
        new_ya1x1 = fusion(np.array(p), h)
        ya1x1prob_dev.append(new_ya1x1)
    c = sharpness_correction(bias_dev_score, ya1x1prob_dev) 
    return c


BIAS_MODEL_DICT = {
    'mnli_train':'nli/train_prob_korn_lr_overlapping_sample_weight_3class.jsonl',
    'mnli_dev_mm':'nli/test_prob_korn_lr_overlapping_sample_weight_3class.jsonl',
    'mnli_hans':'nli/hans_prob_korn_lr_overlapping_sample_weight_3class.jsonl',
    
    'fever_train': 'fact_verification/fever.train.jsonl',
    'fever_dev': 'fact_verification/fever.dev.jsonl',
    'fever_sym1': 'fact_verification/fever_symmetric_v0.1.test.jsonl',
    'fever_sym2': 'fact_verification/fever_symmetric_v0.2.test.jsonl',

    'qqp_train': 'paraphrase_identification/qqp.train.jsonl',
    'qqp_dev': 'paraphrase_identification/qqp.dev.jsonl',
    'qqp_paws': 'paraphrase_identification/paws.dev_and_test.jsonl',
}

BERT_MODEL_RESULT_DICT = {
    'mnli_train':'raw_train.jsonl',
    'mnli_dev_mm':'raw_mm.jsonl',
    'mnli_hans':'normal/hans_result.jsonl',
    
    'fever_train': 'raw_fever.train.jsonl',
    'fever_dev': 'raw_fever.dev.jsonl',
    'fever_sym1': 'raw_fever_symmetric_v0.1.test.jsonl',
    'fever_sym2': 'raw_fever_symmetric_v0.2.test.jsonl',

    'qqp_train': 'raw_qqp.train.jsonl',
    'qqp_dev': 'raw_qqp.dev.jsonl',
    'qqp_paws': 'raw_paws.dev_and_test.jsonl',
}


def _default_model_pred(
    _model_name: str = 'mnli_lr_model.sav',
    _input: List[float] = np.array([[0,0,0.41997876976119086]]) #   for NLI
) -> List[float]:
    loaded_model = pickle.load(open(_model_name, 'rb'))
    return loaded_model.predict_proba(_input)


def report_CMA(
    model_path: str,
    task: str,
    data_path: str,
    test_set: str,
    fusion: Callable[[PROB_T], PROB_T] = fuse.sum_fuse,
    input_a0: List[float] = [0,0,0.41997876976119086],

    correction: bool = False,
    bias_probs_key: str = 'bias_probs',
    ground_truth_key: str = 'gold_label',
    model_pred_method: Callable[[List[float]], List[float]] = _default_model_pred,
    TIE_ratio_threshold: float = 9999
) -> None:
    # load predictions from bias model (e.g., logistic regression)
    assert test_set in BIAS_MODEL_DICT.keys()
    df_bias_model = pd.read_json(
        os.path.join(data_path, BIAS_MODEL_DICT[test_set]),
        lines=True
    )
    a1=[b for b in df_bias_model[bias_probs_key]] # prob for all classes
    a1=np.array(a1)

    # get a list of all seed dir
    to_glob = model_path + task + '/*/'
    seed_path = glob.glob(to_glob) # list of model dir for all seeds

    # init list to store results
    TE_explain = []
    TIE_explain = []
    factual_scores = []
    TIE_scores = []
    NIE_explain = []
    NIE_scores = []
    INTmed_explain = []
    INTmed_scores = []
    my_causal_query = []

    # get avg score
    for seed_idx in range(len(seed_path)):
        df_train = pd.read_json(
            os.path.join(seed_path[seed_idx], BERT_MODEL_RESULT_DICT[test_set]),
            lines=True
        )
        list_probs = []
        for i in df_train['probs']:
            list_probs.extend(i)
        train_pred_results = np.array(list_probs)
        x0 = np.average(train_pred_results,axis=0)
        if correction:
            x0 = get_c(
                data_path + task + '/',
                seed_path[seed_idx],
                fusion, x0
            )
        # fusion to create ya1x0
        ya1x0prob = fusion(a1,x0)

        # get score of the model on a challenge set
        df_bert = pd.read_json(
            os.path.join(seed_path[seed_idx], BERT_MODEL_RESULT_DICT[test_set]),
            lines=True
        )
         
        # ya1x1
        ya1x1prob = []
        x1 = df_bert['probs']
        for b,p in zip(a1,x1):
            new_ya1x1 = fusion(np.array(b),p)
            ya1x1prob.append(new_ya1x1)

        debias_scores = []
        for factual, counterfactual in zip(ya1x1prob,ya1x0prob):
            debias_scores.append(factual-counterfactual)    

        # {0:"entailment",1:"contradiction",2:"neutral"}
        labels = df_bias_model[ground_truth_key]
        
        # to offset samples with no ground truth from accuracy calculation
        offset = 0 
        if '-' in df_bias_model[ground_truth_key].value_counts():
            # no ground truth
            offset = df_bias_model[ground_truth_key].value_counts()['-'] 
        # CMA
        a0 = model_pred_method() #input_a0?
        ya0x0 = fusion(a0,x0)
        # to measure accuracy
        factual_pred_correct = []
        TIE_pred_correct = []
        NIE_pred_correct = []
        INTmed_pred_correct = []
        pred_correct = []
        # for mediation analysis
        all_TE = []
        all_TIE = []
        all_NIE = []
        all_NDE = []
        all_INTmed = []
        for i in range(len(labels)): 
            ya1x1 = ya1x1prob[i]
            ya1x0 = ya1x0prob[i]
            TE = ya1x1 - ya0x0
            NDE = ya1x1 - ya0x0
            ya0x1= fusion(a0,np.array(x1[i]) )
            TIE = ya1x1 - ya1x0
            NIE = ya0x1 - ya0x0
            INTmed = TIE - NIE
            # factual
            factual_ans = np.argmax(x1[i])
            factual_ans = get_ans(factual_ans,test_set)
            factual_correct = factual_ans==labels[i]   
            factual_pred_correct.append(factual_correct)
            # TIE
            TIE_ans = np.argmax(TIE)
            TIE_ans = get_ans(TIE_ans,test_set)
            TIE_correct = TIE_ans==labels[i]  
            TIE_pred_correct.append(TIE_correct)
            # INTmed
            INTmed_ans = np.argmax(INTmed[0])
            INTmed_ans = get_ans(INTmed_ans,test_set)
            INTmed_correct = INTmed_ans==labels[i]  
            INTmed_pred_correct.append(INTmed_correct)
            # NIE
            NIE_ans = np.argmax(NIE[0])
            NIE_ans = get_ans(NIE_ans,test_set)
            NIE_correct = NIE_ans==labels[i]  
            NIE_pred_correct.append(NIE_correct)    

            # save
            all_NDE.append(NDE[0][0])
            all_NIE.append(NIE[0][0])
            all_TIE.append(TIE[0])
            all_TE.append(TE[0][0])
            all_INTmed.append((INTmed[0][0]))
            if  (TIE[0]/TE[0][0])<TIE_ratio_threshold:
                cf_ans = np.argmax(np.array(x1[i]-a1[i]))
                cf_ans = get_ans(cf_ans,test_set)  
                cf_correct = cf_ans==labels[i]
            else:
        #         print(cf_ans)
                cf_correct = factual_ans ==labels[i]
            pred_correct.append(cf_correct)

        #     np.array(x1[i]-ya1x0prob)
        #     labels[i]
        total_sample = len(labels)- offset
        factual_scores.append(sum(factual_pred_correct)/total_sample)
        TE_explain.append(np.array(all_TE).mean())
        TIE_explain.append(np.array(all_TIE).mean())
        TIE_scores.append(sum(TIE_pred_correct)/total_sample)
        NIE_explain.append(np.array(all_NIE).mean())
        NIE_scores.append(sum(NIE_pred_correct)/total_sample)    
        INTmed_explain.append(np.array(all_INTmed).mean())
        INTmed_scores.append(sum(INTmed_pred_correct)/total_sample)      
        my_causal_query.append(sum(pred_correct)/total_sample)
        print(np.array(all_TIE).mean(),(np.array(all_TIE).std()))
        print(np.array(all_INTmed).mean(),(np.array(all_INTmed).std()))
        print(np.array(all_NIE).mean(),(np.array(all_NIE).std()))

    print('factual score:')
    print(factual_scores,np.array(factual_scores).mean(),np.array(factual_scores).std()) 
    print("TE:")
    print(TE_explain,np.array(TE_explain).mean(),np.array(TE_explain).std()) 
    print("TIE:")
    print(TIE_explain,np.array(TIE_explain).mean(),np.array(TIE_explain).std())
    print("TIE acc:")
    print(TIE_scores,np.array(TIE_scores).mean(),np.array(TIE_scores).std())
    print("NIE:")
    print(NIE_explain,np.array(NIE_explain).mean(),np.array(NIE_explain).std())
    print("NIE acc:")
    print(NIE_scores,np.array(NIE_scores).mean(),np.array(NIE_scores).std())
    print("INTmed:")
    print(INTmed_explain,np.array(INTmed_explain).mean(),np.array(INTmed_explain).std())
    print("INTmed acc:")
    print(INTmed_scores,np.array(INTmed_scores).mean(),np.array(INTmed_scores).std())
    print("my query:")
    print(my_causal_query,np.array(my_causal_query).mean(),np.array(my_causal_query).std())