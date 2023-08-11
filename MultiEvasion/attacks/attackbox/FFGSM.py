"""
FFGSM for models with raw bytes input
"""
import copy

import torch
from torch.autograd import Variable
import numpy as np
from attacks.attackbox.attack_base import Attack



class FFGSM(Attack):
    r"""
    New FGSM proposed in 'Fast is better than free: Revisiting adversarial training'. [https://arxiv.org/abs/2001.03994]
    A new fast gradient sign method (FGSM), which combined with random initialization,
    is as effective as PGD-based training but has significantly lower cost

    """
    def __init__(self,
                 model_1=None,
                 model_2=None,
                 eps: float = 8/255,
                 alpha: float = 10 / 255,
                 w_1: float = 0.5,
                 w_2: float = 0.5,
                 index_to_perturb: list = None,
                 pert_init_with_benign: bool = True):
        super(FFGSM,self).__init__(model_1, model_2)

        if not index_to_perturb or len(index_to_perturb)==0:
            index_to_perturb = [i for i in range(2, 0x3c)]  ## partial dos length=58 ( magic numbers in DOS header "MZ"[0x00-0x02] and [0x3c-0x40] can't be modified)

        self.w_1, self.w_2 = w_1, w_2
        self.eps = eps
        self.alpha = alpha
        self.index_to_perturb = index_to_perturb
        self.pert_init_with_benign = pert_init_with_benign
        self.attack = 'FFGSM'


    def perturbation(self,
                     inputs:list=None,
                     index_to_perturb:list=None,
                     num_evasion_by_benign_content:int=0,
                     num_index_change_in_total_group:list=[],
                     num_index_change_by_gradient_group:list=[]):
        """
        inputs: the initiated inputs (with 0 initiated in perturb_index) after DOS_Extension/Content_Shift attacks
        the length of inputs should larger than original input sample

        num_evasion_w_benign_content: number of evasion times just with benign content or random bytes
        num_index_change_in_total_group: list append all num_index_change_in_total for each input malware
        num_index_change_by_gradient_group: list append with all num_byte_change_by_gradient for each input malware
        """

        ## get index_to_perturb, if exist, assign it to the global variable,
        ## otherwise no attack (i.e. this situation happenes when slack attack cannot find slack space)
        if len(index_to_perturb) > 0:
            self.index_to_perturb = index_to_perturb
        elif len(index_to_perturb) == 0:
            ## 1) no perturbatoin space found  --> return results based on original inputs
            ## 2) happens when the found indexes (e.g. [143432343,145645647]) are larger than the input size (102400),
            ## therefore, even though index_to_perturb exist, but it's still empty consider the input size
            self.index_to_perturb = index_to_perturb
            print('no available index found for perturb given the current input size, predict based on original input')

            if self.model_2:
                (adv_x_preds_1, adv_y_1), (adv_x_preds_2, adv_y_2) = \
                    self._get_print_return_results(inputs, self.model_1, self.model_2, test_x_init=True)
                pert_size = len(self.index_to_perturb)
                return (adv_x_preds_1, adv_y_1), (adv_x_preds_2, adv_y_2), pert_size, num_evasion_by_benign_content, \
                       num_index_change_in_total_group, num_index_change_by_gradient_group
            else:
                adv_x_preds_1, adv_y_1 = self._get_print_return_results(inputs, self.model_1)
                pert_size = len(self.index_to_perturb)
                return adv_x_preds_1, adv_y_1, pert_size, num_evasion_by_benign_content, \
                       num_index_change_in_total_group, num_index_change_by_gradient_group

        # initiate input with benign content or random perturbations
        if self.pert_init_with_benign:
            benign_contents = self.get_section_content_from_folder(num_sections=100, target_section='.text')
        else:
            benign_contents = None

        ## get initiated adv_x with benign content or random bytes;
        ## and get perturbations (either benign contents or random values)
        x_adv_init,pert_init = self.apply_manipulations(inputs,
                                                        self.index_to_perturb,
                                                        self.pert_init_with_benign,
                                                        benign_contents=benign_contents)

        ## ------------------- print results for initiated x before attack and ternimate if needed -----------------------------
        ## test the initiated x that either with random value or benign content in the index_to_perturb
        (adv_x_preds_1,adv_y_1),(adv_x_preds_2,adv_y_2) = self._get_print_return_results(x_adv_init,
                                                                                         self.model_1,
                                                                                         self.model_2,
                                                                                         test_x_init=True)

        ## terminate when label already changed to benign (label=0)
        if adv_y_1 == 0 and adv_y_2 == 0:
            num_evasion_by_benign_content += 1
            return (adv_x_preds_1,adv_y_1), (adv_x_preds_2,adv_y_2), len(pert_init), num_evasion_by_benign_content, \
                   num_index_change_in_total_group, num_index_change_by_gradient_group

        ## else --> start FGSM based adversarial attacks
        ## update values in index_to_perturb with optimized values by FGSM
        print('\n','-'*50)
        print(f'starting FFGSM based adversarial evasion attack...')

        ## ------------- apply gaussian noise (or others) on input before backward ----------------
        x_adv = copy.deepcopy(x_adv_init)
        pert_rand = torch.randn_like(torch.tensor(x_adv), dtype=float).uniform_(-self.alpha, self.alpha)
        x_adv = self._add_pert_in_certain_index(X=x_adv / 255,
                                                pert=pert_rand,
                                                index_to_perturb=self.index_to_perturb) * 255

        ## add dim at axis=0
        x_adv = torch.tensor(np.array(x_adv)[np.newaxis,:],device=self.device)
        x_adv = Variable(x_adv.float(), requires_grad=True).to(self.device)
        # forward process
        preds_1, embed_x_1 = self._forward(x_adv, self.model_1) # embed_x: float
        loss_1 = self.targeted* self.loss(preds_1, self.targeted_label)

        # against 1 or 2 models
        if not self.model_2:
            loss = loss_1
        else:
            preds_2, embed_x_2 = self._forward(x_adv, self.model_2)
            loss_2 = self.targeted * self.loss(preds_2, self.targeted_label)
            loss = self.w_1 * loss_1 + self.w_2 * loss_2

        # zero_grad before backward, otherwise it will accumulate gradients in mini-batches
        self.model_1.zero_grad()
        if self.model_2:
            self.model_2.zero_grad()
        loss.backward()

        # get sign and perturbation
        grad_sign_for_perturb_index_1 = self._get_grad_sign(embed_x_1,self.index_to_perturb)

        ## get adv_x by applied with FGSM based perturbations
        ## by add pert on adv_x initiated with benign contents or random bytes
        adv_x1 = self._get_advX_for_FFGSM(x=x_adv_init,
                                          embed_x=embed_x_1,
                                          sign=grad_sign_for_perturb_index_1,
                                          eps=self.eps,
                                          embed_matrix=self.embed_matrix_1,
                                          index_to_perturb=self.index_to_perturb,
                                          alpha=self.alpha)

        ## obtain final pertbation based on the either one model or two models
        ## one model: simple output pert = eps * sign
        ## two models: 1) if sign1=sign2 --> ... 2) else --> ...
        if self.model_2:
            grad_sign_for_perturb_index_2 = self._get_grad_sign(embed_x_2,self.index_to_perturb)

            ## get adv_x by applied with FGSM based perturbations
            ## by add pert on adv_x initiated with benign contents or random bytes
            adv_x2 = self._get_advX_for_FFGSM(x=x_adv_init,
                                              embed_x=embed_x_2,
                                              sign=grad_sign_for_perturb_index_2,
                                              eps=self.eps,
                                              embed_matrix=self.embed_matrix_2,
                                              index_to_perturb=self.index_to_perturb,
                                              alpha=self.alpha)

            ## get final adv_x based on two adv_x from two models
            adv_x,num_index_change_in_total,num_index_change_by_gradient = \
                self._final_adv_x_selection(x=x_adv_init,
                                            adv_x1=adv_x1,
                                            adv_x2=adv_x2,
                                            verbose=True,
                                            index_to_perturb=self.index_to_perturb)
        else:
            ## only one model need to consider
            adv_x = adv_x1
            num_index_change_in_total = len(self.index_to_perturb)
            num_index_change_by_gradient = 0 # need to re-modify later, assign 0 for now

        ## append num_index_change to list for records
        num_index_change_in_total_group.append(num_index_change_in_total)
        num_index_change_by_gradient_group.append(num_index_change_by_gradient)


        ## ------------------- print and get results for adv_x after FGSM attack -----------------------------
        ## get prediction for produced adversarial example adv_x after FGSM,
        ## print and return results (prediction score, prediction label, size of perturbation)
        if self.model_2:
            (adv_x_preds_1,adv_y_1),(adv_x_preds_2,adv_y_2) = \
                self._get_print_return_results(adv_x,self.model_1,self.model_2,test_x_init=False)
            pert_size = len(self.index_to_perturb)
            return (adv_x_preds_1,adv_y_1), (adv_x_preds_2,adv_y_2), pert_size, num_evasion_by_benign_content, \
                   num_index_change_in_total_group, num_index_change_by_gradient_group
        else:
            adv_x_preds_1, adv_y_1 = self._get_print_return_results(adv_x,self.model_1)
            pert_size = len(self.index_to_perturb)
            return adv_x_preds_1, adv_y_1, pert_size, num_evasion_by_benign_content, \
                   num_index_change_in_total_group, num_index_change_by_gradient_group



