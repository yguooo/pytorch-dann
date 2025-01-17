"""Train dann."""

import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
from core.test import test
from core.test_weight import test_weight
from utils.utils import save_model
import torch.backends.cudnn as cudnn
import math
cudnn.benchmark = True

def weight(ten, a=10):
    a = torch.tensor(a, device=ten.device)
    return (torch.atan(a*(ten-0.5)) +
            torch.atan(0.5*a))/(2*torch.atan(a*0.5))
def lipton_weight(ten, beta = 4):
        order = torch.argsort(ten)
        return (order < len(ten)/(1+beta)).float()

def get_quantile(ten, a = 0.5):   
    return torch.kthvalue(ten,math.floor(len(ten)*a))[0]  

def train_dann(model, params, src_data_loader, tgt_data_loader, src_data_loader_eval, tgt_data_loader_eval, num_src, num_tgt, device, logger):
    """Train dann."""
    ####################
    # 1. setup network #
    ####################

    # setup criterion and optimizer

    if not params.finetune_flag:
        print("training non-office task")
        # optimizer = optim.SGD(model.parameters(), lr=params.lr, momentum=params.momentum, weight_decay=params.weight_decay)
        optimizer = optim.Adam(model.parameters(), lr=params.lr)
    else:
        print("training office task")
        parameter_list = [{
            "params": model.features.parameters(),
            "lr": 0.001
        }, {
            "params": model.fc.parameters(),
            "lr": 0.001
        }, {
            "params": model.bottleneck.parameters()
        }, {
            "params": model.classifier.parameters()
        }, {
            "params": model.discriminator.parameters()
        }]
        optimizer = optim.SGD(parameter_list, lr=0.01, momentum=0.9)

    criterion0 = nn.CrossEntropyLoss(reduction = 'mean')
    criterion = nn.CrossEntropyLoss(reduction = 'none')
    weight_src = torch.ones(num_src).to(device)
    weight_tgt = torch.ones(num_tgt).to(device)
    ####################
    # 2. train network #
    ####################
    global_step = 0
    for epoch in range(params.num_epochs):
        # set train state for Dropout and BN layers
        model.train()
        # zip source and target data pair
        len_dataloader = min(len(src_data_loader), len(tgt_data_loader))
        data_zip = enumerate(zip(src_data_loader, tgt_data_loader))
        for step, ((images_src, class_src, idx_src), (images_tgt, _, idx_tgt)) in data_zip:

            p = float(step + epoch * len_dataloader) / \
                params.num_epochs / len_dataloader
            alpha = 2. / (1. + np.exp(-10 * p)) - 1

            # if params.lr_adjust_flag == 'simple':
            #     lr = adjust_learning_rate(optimizer, p)
            # else:
            #     lr = adjust_learning_rate_office(optimizer, p)
            # logger.add_scalar('lr', lr, global_step)

            # prepare domain label
            size_src = len(images_src)
            size_tgt = len(images_tgt)
            label_src = torch.zeros(size_src).long().to(device)  # source 0
            label_tgt = torch.ones(size_tgt).long().to(device)  # target 1

            # make images variable
            class_src = class_src.to(device)
            images_src = images_src.to(device)
            images_tgt = images_tgt.to(device)

            # zero gradients for optimizer
            optimizer.zero_grad()
            
            # train on source domain
            src_class_output, src_domain_output = model(input_data=images_src, alpha=alpha)
            src_loss_class = criterion0(src_class_output, class_src)
            if params.run_mode in [0,2]:
                src_loss_domain = criterion0(src_domain_output, label_src)
            else: 
                src_loss_domain = criterion(src_domain_output, label_src)
                prob = torch.softmax(src_domain_output.data, dim = -1)
                if params.soft: 
                    if params.quantile: 
                        weight_src[idx_src] = (torch.sort(prob[:,1])[1]).float().detach()
                    else:   
                        weight_src[idx_src] = weight(prob[:,1]).detach()
                else:
                    if params.quantile:
                        weight_src[idx_src] = (prob[:,0] < \
                        get_quantile(prob[:,0],params.threshold[0])).float().detach()
                    else:
                        weight_src[idx_src] = (prob[:,0] < params.threshold[0]).float().detach()
                src_loss_domain = torch.dot(weight_src[idx_src], src_loss_domain
                                        )/ torch.sum(weight_src[idx_src])
            #train on target domain
            _, tgt_domain_output = model(input_data=images_tgt, alpha=alpha)
            if params.run_mode in [0,1]:           
                tgt_loss_domain = criterion0(tgt_domain_output, label_tgt)
            else: 
                tgt_loss_domain = criterion(tgt_domain_output, label_tgt)
                prob = torch.softmax(tgt_domain_output.data, dim = -1)

                if params.soft: 
                    if params.quantile: 
                        weight_tgt[idx_tgt] = (torch.sort(prob[:,0])[1]).float().detach()
                    else: 
                        weight_tgt[idx_tgt] = weight(prob[:,0]).detach()
                else: 
                    if params.quantile: 
                        weight_tgt[idx_tgt] = (prob[:,1] < \
                        get_quantile(prob[:,1],params.threshold[1])).float().detach()
                    else: 
                        weight_tgt[idx_tgt] = (prob[:,1] < params.threshold[1]).float().detach()
                tgt_loss_domain = torch.dot(weight_tgt[idx_tgt], tgt_loss_domain
                                            ) / torch.sum(weight_tgt[idx_tgt])

            loss = src_loss_class + src_loss_domain + tgt_loss_domain
            if params.src_only_flag:
                loss = src_loss_class

            # optimize dann
            loss.backward()
            optimizer.step()

            global_step += 1

            # print step info
            logger.add_scalar('src_loss_class', src_loss_class.item(), global_step)
            logger.add_scalar('src_loss_domain', src_loss_domain.item(), global_step)
            logger.add_scalar('tgt_loss_domain', tgt_loss_domain.item(), global_step)
            logger.add_scalar('loss', loss.item(), global_step)

            if ((step + 1) % params.log_step == 0):
                print(
                    "Epoch [{:4d}/{}] Step [{:2d}/{}]: src_loss_class={:.6f}, src_loss_domain={:.6f}, tgt_loss_domain={:.6f}, loss={:.6f}"
                    .format(epoch + 1, params.num_epochs, step + 1, len_dataloader, src_loss_class.data.item(),
                            src_loss_domain.data.item(), tgt_loss_domain.data.item(), loss.data.item()))

        # eval model
        if ((epoch + 1) % params.eval_step == 0):
            src_test_loss, src_acc, src_acc_domain = test_weight(model, src_data_loader_eval, device, flag='source')
            tgt_test_loss, tgt_acc, tgt_acc_domain = test_weight(model, tgt_data_loader_eval, device, flag='target')
            logger.add_scalar('src_test_loss', src_test_loss, global_step)
            logger.add_scalar('src_acc', src_acc, global_step)
            logger.add_scalar('src_acc_domain', src_acc_domain, global_step)
            logger.add_scalar('tgt_test_loss', tgt_test_loss, global_step)
            logger.add_scalar('tgt_acc', tgt_acc, global_step)
            logger.add_scalar('tgt_acc_domain', tgt_acc_domain, global_step)


        # save model parameters
        if ((epoch + 1) % params.save_step == 0):
            save_model(model, params.model_root,
                       params.src_dataset + '-' + params.tgt_dataset + "-dann-{}.pt".format(epoch + 1))

    # save final model
    save_model(model, params.model_root, params.src_dataset + '-' + params.tgt_dataset + "-dann-final.pt")

    return model

def adjust_learning_rate(optimizer, p):
    lr_0 = 0.01
    alpha = 10
    beta = 0.75
    lr = lr_0 / (1 + alpha * p)**beta
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr

def adjust_learning_rate_office(optimizer, p):
    lr_0 = 0.001
    alpha = 10
    beta = 0.75
    lr = lr_0 / (1 + alpha * p)**beta
    for param_group in optimizer.param_groups[:2]:
        param_group['lr'] = lr
    for param_group in optimizer.param_groups[2:]:
        param_group['lr'] = 10 * lr
    return lr
