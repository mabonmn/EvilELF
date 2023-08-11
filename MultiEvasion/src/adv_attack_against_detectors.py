"""
produce generic adversarial malware that evade multiple malware detectors,
or perform adversarial attack against multiple malware detectors at same time,
different adversarial attacks can be selected (e.g., FGSM, PGD, DeepFool, etc)

200 malware test samples
label: 0 --> benign; 1 --> malware

index to perturbate:
1. DOS Header Attack: all bytes except two magic numbers "MZ" [2,0x3c) and 4 bytes values
    at [0x3c,0x40] (about 243 bytes for Full DOS)
2. Extended DOS Header Attack: bytes from DOS Header Attacks, plus new extended space of DOS Header
3. Content Shift Attack: new created space between PE header and first section

steps:
1. obtain index to perturb
2. initiate obtained index_to_perturb: either with bytes from benign samples or random bytes
3. applied with adversarial example generation method on the index_to_perturb
4. obtained best adversarial malware that can evade detectors or terminate until max iterations
"""
import os
os.sys.path.append('..')

import torch
import time,sys,argparse
import pandas as pd
from torch.utils.data import DataLoader
from src.util import ExeDataset, get_acc_FP, get_adv_name
from src.model import MalConv_freezeEmbed,FireEye_freezeEmbed
import numpy as np

from attacks.attackbox import FGSM,PGD,FFGSM,CW
from attacks.attackbox.attack_base import Attack
from attacks.attackbox.attack_utils import get_perturb_index_and_init_x
from src.util import forward_prediction_process
from sklearn.metrics import accuracy_score


class adv_evasion_attack_sync(Attack):
    """
    perform adversarial evasion attack against multiple (e.g., two) malware detectors at same time.
    adversarial evasion attacks include FGSM, PGD, DeepFool, etc
    e.g., use FGSM based advserarial evasion attack to evade Malconv and DNN-FireEye synchronically/simultaneously
    """
    def __init__(self,
                 model1=None,
                 model2=None,
                 log_file_result_name=None):

        ## 继承父类
        Attack.__init__(self,model1,model2)

        ## 定义子类的参数
        ## initate log file for recording results globally
        ## if not assigned, then initiated in parent class (Attack)
        ## otherwise, equal to assigned file name
        if log_file_result_name:
            ## re-assign log file with new file in sub-class (子类里重新赋值给继承父类中的变量)
            self.log_file_for_result = log_file_result_name


    def mean_element_wise(self,array1,array2):
        """
        given two array, output the mean value element-wise
        array1=[1,2,3], array2=[4,5,6]
        --> [2.5,3.5,4.5]
        """
        out = []
        for i in range(len(array1)):
            out.append(np.mean([array1[i],array2[i]]))
        return np.array(out)


    def binary_acc(self,preds,y):
        "input are list, add one dimension to compare if equal"
        preds = np.array(preds)[np.newaxis:]
        y = np.array(y)[np.newaxis:]

        corrects = (preds==y)
        acc = float(sum(corrects))/len(corrects)
        FP = len(corrects) - corrects.sum()
        return acc,FP


    def get_predicted_malware(self,
                              model_1=None,
                              data_iterator=None,
                              model_2=None,
                              verbose=True):
        """
        one model: discard samples that assigned label with benign, otherwise keep for attacking
        two models: discard samples that assigned label with benign by both models, otherwise keep for attacking
        label: 1--> malware, 0--> benign

        return:
            - the group of predicted malwares (list)
            - the list of corresponding labels
        """
        forward_prediction = forward_prediction_process()

        X_mal,y_mal = [],[] ## to record samples and lables that predicted as malware by one detector at least
        y_pred_1, y_pred_2 = [],[]  ## to record the predicted labels for each model
        all_y = [] ## to record orgianl labels of all input
        for i,batch in enumerate(data_iterator):
            X,y = batch
            X, y = X.float().to(self.device), y.long().to(self.device)
            all_y.append(y.item())

            confidence_1,_ = forward_prediction._forward(X, model_1)
            y_pred_1.append(np.argmax(confidence_1))
            if model_2:
                confidence_2,_ = forward_prediction._forward(X, model_2)
                y_pred_2.append(np.argmax(confidence_2))
                if confidence_1[0] > 0.5 and confidence_2[0] > 0.5:
                    continue
                else:
                    X_mal.append(X.cpu().squeeze().numpy())
                    y_mal.append(y.item())
            else:
                if confidence_1[0] < 0.5:
                    X_mal.append(X.squeeze().numpy())
                    y_mal.append(y.item())
        print(f"Add {len(X_mal)} Malwares.")
        print(
            f'Model1 MalConv Acc: {accuracy_score(y_pred_1, all_y)}; Model2 FireEye Acc: {accuracy_score(y_pred_2, all_y)}')

        if verbose:
            with open(self.log_file_for_result, 'a') as f:
                print('-'*20,'Malware input statistics for Evasion Attacks','-'*20,file=f)
                print(f'{len(data_iterator)} malware in total.', file=f)
                print(f'{len(X_mal)} malware predicted as malware at least by one detector', file=f)
                print(f'These {len(X_mal)} malware will be used to produce adversarial malware.', file=f)
                print(f'Model1 {args.model_path_1.split("/")[-1].split(".")[0]} Acc: {accuracy_score(y_pred_1,all_y)}; Model2 {args.model_path_2.split("/")[-1].split(".")[0]} Acc: {accuracy_score(y_pred_2,all_y)}',file=f)
                print('\n',file=f)

        return X_mal,y_mal



    def test_loop(self,
                  model_1=None,
                  data_iterator=None,
                  adversary:str=None,
                  model_2=None,
                  verbose:bool=True):
        """
        for each input x, get adv_y and adv_x;
        and output the accuracy sum(adv_y==y)/len(y)
        """
        num_pert_bytes = [] # to record the number of bytes perturbed each sample
        num_samples = 0
        adv_labels = []
        labels = []
        start = time.time()

        ## filter malwares that predicted as benign ones
        ## i.e., discard predicted benign and get predicted malware
        X_mal, y_mal = self.get_predicted_malware(model_1=model_1,
                                                  data_iterator=data_iterator,
                                                  model_2=model_2)

        not_pe_file_count = 0
        num_evasion_by_benign_content = 0
        num_index_change_in_total_group = []
        num_index_change_by_gradient_group = []
        for i,(X,y) in enumerate(zip(X_mal,y_mal)):

            num_samples += 1
            print('\n',f"sample {i} under {args.adversary} attack.", '\n')
            labels.append(y)

            ## convert list of integer (range must in [0,255]) to bytearray
            ## bytearray(list of integer): --> binary file
            ## list(binary file): --> list of integer (convert back to hex by list())
            # gg = np.where(X==256)[0].size
            X_bytearray= bytearray(list(np.array(X,dtype=int))) #convert integer to bytes; e.g.,[0,1] --> [b'\x00\x01]

            ## get index of perturbation.
            ## use try and exception to filter the corrupted input files (e.g. not a PE binary file)
            try:
                index_to_perturb, x_init = get_perturb_index_and_init_x(input=X_bytearray,
                                                                        preferable_extension_amount=args.preferable_extension_amount,
                                                                        preferable_shift_amount=args.preferable_shift_amount,
                                                                        max_input_size=args.first_n_byte,
                                                                        partial_dos=args.partial_dos,
                                                                        content_shift=args.content_shift,
                                                                        slack=args.slack,
                                                                        combine_w_slack=args.combine_w_slack)
            except:
                not_pe_file_count += 1
                continue

            ## convert initiated x (applied with adversarial attacks) from bytearray to list of integer
            ## cut the size of input to fixed number since the DOS/Shift attack increase length
            X_init = np.array(list(x_init))[:args.first_n_byte]

            if model_2:
                (adv_x_preds_1, adv_y_1), (adv_x_preds_2, adv_y_2), pert_size,num_evasion_by_benign_content,num_index_change_in_total_group,\
                num_index_change_by_gradient_group = adversary.perturbation(inputs=X_init,
                                                                            index_to_perturb=index_to_perturb,
                                                                            num_evasion_by_benign_content=num_evasion_by_benign_content,
                                                                            num_index_change_in_total_group=num_index_change_in_total_group,
                                                                            num_index_change_by_gradient_group=num_index_change_by_gradient_group)
                adv_labels.append((adv_y_1,adv_y_2))
            else:
                adv_x_preds_1, adv_y_1, pert_size,num_evasion_by_benign_content,num_index_change_in_total_group,\
                num_index_change_by_gradient_group = adversary.perturbation(X_init,
                                                                            index_to_perturb=index_to_perturb,
                                                                            num_evasion_by_benign_content=num_evasion_by_benign_content,
                                                                            num_index_change_in_total_group=num_index_change_in_total_group,
                                                                            num_index_change_by_gradient_group=num_index_change_by_gradient_group)
                adv_labels.append(adv_y_1)

            num_pert_bytes.append(pert_size)

        end = time.time()

        single_model = (False if model_2 else True)
        acc,FP,(acc1,FP1),(acc2,FP2) = get_acc_FP(adv_labels,single_model=single_model)

        print('------------------------------------------------')
        print(f"Evasion attack: {evasion_attack_name}; Feature optimization: {args.adversary}; Only Partial DOS attack: {args.partial_dos}; Only Content Shift attack: {args.content_shift};"
              f"Only Slack attack: {args.slack}")
        print(f'Successful Evasion Rate {1 - acc}')
        print(f'Two Models {not single_model}, Acc against adv_x (at least one detector predicted as malware): {acc}')
        print(f'False Positive: {FP}, which evade detectors successfully in the end. ({len(adv_labels)} test samples in total)')
        print(f'False Positive just by [benign_content:{args.pert_init_with_benign}, False means random_content]: {num_evasion_by_benign_content}, which evade successfully just by benign content or random bytes initiation. '
            f'Corresponding Evasion Rate {num_evasion_by_benign_content / len(adv_labels)}')
        print(f'False Positive just by {args.adversary}: {FP - num_evasion_by_benign_content}. '
            f'Corresponding Evasion Rate {(FP - num_evasion_by_benign_content) / len(adv_labels)}')
        print(f'Model1 {args.model_path_1.split("/")[-1].split(".")[0]}: {acc1} accuracy for detecting adversarial malware. FP: {FP1} number of adv_x evade successfully.')
        print(f'Model2 {args.model_path_2.split("/")[-1].split(".")[0]}: {acc2} accuracy for detecting adversarial malware. FP: {FP2} number of adv_x evade successfully.')
        print(f'Average number of index crafted for each file: {np.mean(num_index_change_in_total_group)}')
        print(f'Average number of index crafted just by gradient optimization for each file: {np.mean(num_index_change_by_gradient_group)}')
        # print(f'average perturbation overhead: {len(num_pert_bytes)/num_samples} bytes')
        print(f'{len(adv_labels)} out of {i + 1} PE files used as testing files (to evade detectors)')
        print(f'{not_pe_file_count} files that can not processed/identified as PE binary. Filter out!')
        print(f'test time: {int((end - start)/3600)}h {int((end - start) % 3600 / 60)}m {int((end - start) % 60)}s.')

        if verbose:
            with open(self.log_file_for_result, 'a') as f:
                print('-' * 20, 'Evasion results', '-' * 20,file=f)
                print(f"Evasion attack: {evasion_attack_name}; Feature optimization: {args.adversary}; Only Partial DOS attack: {args.partial_dos}; Only Content Shift attack: {args.content_shift}; "
                      f"Only Slack attack: {args.slack}",file=f)
                print(f'Successful Evasion Rate {1 - acc}',file=f)
                print(f'Two Models {not single_model}, Acc against adv_x (at least one detector predicted as malware): {acc}',file=f)
                print(f'False Positive: {FP}, which evade detectors successfully in the end. ({len(adv_labels)} test samples in total)',file=f)
                print(f'False Positive just by [benign_content:{args.pert_init_with_benign}, False means random_content]: {num_evasion_by_benign_content}, which evade successfully just by benign content or random bytes initiation. '
                      f'Corresponding Evasion Rate {num_evasion_by_benign_content/len(adv_labels)}',file=f)
                print(f'False Positive just by {args.adversary}: {FP-num_evasion_by_benign_content}. '
                    f'Corresponding Evasion Rate {(FP-num_evasion_by_benign_content) / len(adv_labels)}', file=f)
                print(f'Model1 {args.model_path_1.split("/")[-1].split(".")[0]}: {acc1} accuracy for detecting adversarial malware. FP: {FP1} number of adv_x evade successfully.',file=f)
                print(f'Model2 {args.model_path_2.split("/")[-1].split(".")[0]}: {acc2} accuracy for detecting adversarial malware. FP: {FP2} number of adv_x evade successfully.',file=f)
                print(f'Average number of index crafted for each file: {np.mean(num_index_change_in_total_group)}',file=f)
                print(f'Average number of index crafted by gradient optimization for each file: {np.mean(num_index_change_by_gradient_group)}',file=f)
                # print(f'average perturbation overhead: {len(num_pert_bytes) / num_samples} bytes.',file=f)
                print(f'{len(adv_labels)} out of {i + 1} PE files used as testing files (to evade detectors).',file=f)
                print(f'{not_pe_file_count} files that can not processed/identified as PE binary. Filter out!',file=f)
                print(f'test time: {int((end - start)/3600)}h {int((end - start) % 3600 / 60)}m {int((end - start) % 60)}s.', file=f)


def main_test():
    "gpu/cpu"
    device = ('cuda:0' if torch.cuda.is_available() else 'cpu')
    print('CUDA AVAIBABEL: ', torch.cuda.is_available())

    "load data"
    test_label_table = pd.read_csv(args.test_label_path, header=None, index_col=0)
    # test_label_table.index = test_label_table.index.str.upper()  # upper the string
    test_label_table = test_label_table.rename(columns={1: 'ground_truth'})

    # output the statistic of loading data
    print('Testing Set:')
    print('\tTotal', len(test_label_table), 'files')
    print('\tMalware Count :', test_label_table['ground_truth'].value_counts()[1])
    # print('\tGoodware Count:', test_label_table['ground_truth'].value_counts()[0])

    ## output testing data stat
    with open(args.log_file_for_result,'a') as f:
        print('-'*20,'Input file Statistics','-'*20,file=f)
        print('Total', len(test_label_table), 'files',file=f)
        print('Malware Count :', test_label_table['ground_truth'].value_counts()[1],file=f)
        print('\n',file=f)

    test_data_loader = DataLoader(ExeDataset(list(test_label_table.index), args.test_data_path,
                                             list(test_label_table.ground_truth), args.first_n_byte),
                                  batch_size=args.batch_size,
                                  shuffle=False, num_workers=args.use_cpu)

    ## load model
    ## model1: define as MalConv, globally effective
    ## model2: define as FireEye, globally effective
    model_1 = MalConv_freezeEmbed(max_input_size=args.first_n_byte, window_size=args.window_size).to(device)
    model_2 = FireEye_freezeEmbed(input_length=args.first_n_byte, window_size=512,vocab_size=257).to(device)

    model_1.load_state_dict(torch.load(args.model_path_1, map_location=device))
    model_2.load_state_dict(torch.load(args.model_path_2, map_location=device))

    model_1.eval()
    model_2.eval()


    "load adversary"
    if args.adversary == 'FGSM':
        adversary = FGSM(model_1=model_1,
                        model_2=model_2,
                        eps=args.eps,
                        w_1=args.w_1,
                        w_2=args.w_2,
                        random_init=False,
                        pert_init_with_benign=args.pert_init_with_benign)
    elif args.adversary == 'FFGSM':
        adversary = FFGSM(model_1=model_1,
                          model_2=model_2,
                          alpha=args.alpha,
                          eps=args.eps,
                          w_1=args.w_1,
                          w_2=args.w_2,
                          pert_init_with_benign=args.pert_init_with_benign)
    elif args.adversary == 'PGD':
        adversary = PGD(model_1=model_1,
                        model_2=model_2,
                        alpha=args.alpha,
                        eps=args.eps,
                        iter_steps=args.iter_steps,
                        w_1=args.w_1,
                        w_2=args.w_2,
                        random_init=False,
                        pert_init_with_benign=args.pert_init_with_benign)
    elif args.adversary == 'CW':
        adversary = CW(model_1=model_1,
                       model_2=model_2,
                       c=args.c,
                       kappa=args.kappa,
                       iter_steps=args.iter_steps,
                       lr=0.01,
                       w_1=args.w_1,
                       w_2=args.w_2,
                       random_init=False,
                       pert_init_with_benign=args.pert_init_with_benign)
    else:
        print('please specify an adversary')
        sys.exit()

    return model_1,model_2,adversary,test_data_loader


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Adversarial Evasion Attacks against Multi-Malware Detectors',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--log_file_for_result', default=None,type=str, help='txt file for recording results')
    parser.add_argument('--test_data_path', default=None, help='path for test data')
    parser.add_argument('--test_label_path', default=None, help='csv file for testing malware files with labels')
    parser.add_argument('--model_path_1', default=None, help='path for trained model 1')
    parser.add_argument('--model_path_2', default=None, help='path for trained model 2')
    parser.add_argument('--w_1', default=0.5, type=float, help='loss weight of model 1')
    parser.add_argument('--w_2', default=0.5, type=float, help='loss weight of model 2')
    parser.add_argument('--use_cpu', default=1, type=int,help='number of cpu cores use for data loader')
    parser.add_argument('--batch_size', default=1, type=int,
                        help='batch size for generating adv_x, default=1 (most of case used generated 1 by 1)')
    parser.add_argument('--first_n_byte', default=102400, type=int,help='the input size of malware detector model')
    parser.add_argument('--window_size', default=500,type=int,help='kernel size of the model')
    parser.add_argument('--preferable_extension_amount', default=512,type=int,help='number of bytes created by DOS extension attack')
    parser.add_argument('--preferable_shift_amount', default=512,type=int,help='number of bytes created by content shift attack')
    parser.add_argument('--pert_init_with_benign', default=True,type=str,help='True: initiation perturbation with benign content, otherwise with random content')
    parser.add_argument('--adversary', default='FGSM',type=str,help='adversary name')
    parser.add_argument('--alpha', default=0.3,type=float,help='maximum perturbation size')
    parser.add_argument('--eps', default=0.07,type=float,help='perturbation step size')
    parser.add_argument('--iter_steps', default=50,type=int,help='number of iteration applied perturbation')
    parser.add_argument('--partial_dos', default=False,type=str,help='whether only perform partial dos attack or not')
    parser.add_argument('--content_shift', default=False,type=str,help='whether only content shift attack or not')
    parser.add_argument('--slack', default=False,type=str,help='whether only slack attack or not')
    parser.add_argument('--combine_w_slack', default=False,type=str,help='combine slack attack with others together?')
    parser.add_argument('--c',default=1e-4,type=float,help='threshold for cw attack')
    parser.add_argument('--kappa',default=0,type=int,help='threshold parameter for cw attack')
    args = parser.parse_args() ## global variables

    ## get boolean value
    if args.pert_init_with_benign == 'True':
        args.pert_init_with_benign = True
    elif args.pert_init_with_benign == 'False':
        args.pert_init_with_benign = False

    print('\n', args, '\n')

    ## output parameters to file
    ## rename file by adding adversary name
    ## first time to write file, empty file before writing
    file_name_split = args.log_file_for_result.split('/')
    folder_path = ('/').join(file_name_split[:-1]) + '/'
    evasion_attack_name = get_adv_name(partial_dos=args.partial_dos,
                                       content_shift=args.content_shift,
                                       slack=args.slack,
                                       combine_w_slack=args.combine_w_slack,
                                       preferable_extension_amount=args.preferable_extension_amount,
                                       preferable_shift_amount=args.preferable_shift_amount)

    if args.adversary == 'FGSM':
        file_name = ('_' + args.adversary +'_eps'+str(args.eps) + '_extend'+str(args.preferable_extension_amount)
                     +'_shift'+str(args.preferable_shift_amount)+ '_' + evasion_attack_name
                     + '_w1_'+str(args.w_1)+ '_w2_'+str(args.w_2) + '.').join(file_name_split[-1].split('.'))
    elif args.adversary == 'FFGSM':
        file_name = ('_' + args.adversary +'_eps'+str(args.eps)+'_alpha'+str(args.alpha) + '_extend'+str(args.preferable_extension_amount)
                     +'_shift'+str(args.preferable_shift_amount)+ '_' + evasion_attack_name
                     + '_w1_'+str(args.w_1)+ '_w2_'+str(args.w_2)+ '.').join(file_name_split[-1].split('.'))
    elif args.adversary == 'PGD':
        file_name = ('_' + args.adversary +'_eps'+str(args.eps)+'_alpha'+str(args.alpha)+'_iter'
                     +str(args.iter_steps)+'_extend'+str(args.preferable_extension_amount)+'_shift'
                     +str(args.preferable_shift_amount) + '_' + evasion_attack_name
                     + '_w1_'+str(args.w_1)+ '_w2_'+str(args.w_2)+ '.').join(file_name_split[-1].split('.'))
    elif args.adversary == 'CW':
        file_name = ('_' + args.adversary + '_c' + str(args.c)+ '_iter'
                     +str(args.iter_steps) + '_extend' + str(args.preferable_extension_amount)
                     + '_shift' + str(args.preferable_shift_amount) + '_' + evasion_attack_name
                     + '_w1_' + str(args.w_1) + '_w2_' + str(args.w_2) + '.').join(file_name_split[-1].split('.'))
    args.log_file_for_result = folder_path + file_name
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
    if os.path.exists(args.log_file_for_result):
        os.remove(args.log_file_for_result)
    with open(args.log_file_for_result,'w') as f:
        print('-'*20,'Input Parameters','-'*20,file=f)
        print(args,file=f)
        print('\n',file=f)

    ## fix random seed
    torch.backends.cudnn.deterministic = True

    ## main function to get models, test data and adversary
    model_1, model_2, adversary, test_data_loader = main_test()

    ## initiate attack
    adv_evasion_attack_sync = adv_evasion_attack_sync(model1=model_1,
                                                      model2=model_2,
                                                      log_file_result_name=args.log_file_for_result)
    ## starting evading process
    adv_evasion_attack_sync.test_loop(model_1=model_1,
                                      model_2=model_2,
                                      data_iterator=test_data_loader,
                                      adversary=adversary,
                                      verbose=True)
    print(f"adversary: {args.adversary}")


