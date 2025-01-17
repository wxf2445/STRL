import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import transformer.Constants as Constants
import numpy as np

from transformer.Models import get_non_pad_mask


def compute_event(event, non_pad_mask):
    """ Log-likelihood of events. """

    # add 1e-9 in case some events have 0 likelihood
    event += math.pow(10, -9)
    event.masked_fill_(~non_pad_mask.bool(), 1.0)

    result = torch.log(event)
    return result


def compute_integral_biased(all_lambda, time, non_pad_mask):
    """ Log-likelihood of non-events, using linear interpolation. """

    diff_time = (time[:, 1:] - time[:, :-1]) * non_pad_mask[:, 1:]
    diff_lambda = (all_lambda[:, 1:] + all_lambda[:, :-1]) * non_pad_mask[:, 1:]

    biased_integral = diff_lambda * diff_time
    result = 0.5 * biased_integral
    return result


def compute_integral_unbiased(model, data, time, non_pad_mask, type_mask):
    """ Log-likelihood of non-events, using Monte Carlo integration. """

    num_samples = 100

    # time [16, L]
    diff_time = (time[:, 1:] - time[:, :-1]) * non_pad_mask[:, 1:]  # divide interval
    # diff_time [16, L-1]

    # diff_time.unsqueeze(2) [16, L-1, 1]
    temp_time = diff_time.unsqueeze(2) * \
                torch.rand([*diff_time.size(), num_samples], device=data.device)  # rand sampling 100
    # temp_time [16, L-1, 100]

    # time[:, :-1] + 1  [16, L-1]

    temp_time /= (time[:, :-1] + 1).unsqueeze(2)  # [16, L-1, 100] / [16, L-1, 1] -> [16, L-1, 100]
    # temp_time [16, L-1, 100]

    # data [16, L, 512]
    temp_hid = model.linear(data)[:, 1:, :]  # Linear(512, 22)  w^{T}_{k,v} * h(t)
    # temp_hid [16, L-1, 22]

    # type_mask [16, L, 22], type_mask[:, 1:, :] [16, L-1, 22]
    temp_hid = torch.sum(temp_hid * type_mask[:, 1:, :], dim=2, keepdim=True)  # sum(1) [16, L-1, 22] * [16, L-1, 22]
    # temp_hid [16, L-1, 1]

    all_lambda = F.softplus(temp_hid + model.alpha * temp_time, threshold=10)
    # all_lambda [16, L-1, 100]

    all_lambda = torch.sum(all_lambda, dim=2) / num_samples
    # all_lambda  [16, L-1]

    unbiased_integral = all_lambda * diff_time  # [16, L-1] * [16, L-1]
    return unbiased_integral  # [16, L-1]


def log_likelihood(model, data, time, types):
    """ Log-likelihood of sequence. """
    non_pad_mask = get_non_pad_mask(types).squeeze(2)  # [16, L] ->  [16, L, 1] -> [16, L]

    # type_mask: torch.Size([16, L, 22]),
    type_mask = torch.zeros([*types.size(), model.num_types], device=data.device)

    # print(model.num_types)  # 23
    for i in range(model.num_types):
        type_mask[:, :, i] = (types == i + 1).bool().to(data.device)  # torch.Size([16, L, 22])

    all_hid = model.linear(data)  # Linear(512, 22)  [16, L, 512] -> [16, 180, 22]
    all_lambda = F.softplus(all_hid, threshold=10)  # [16, L, 22] -> [16, 180, 22]

    type_lambda = torch.sum(all_lambda * type_mask, dim=2)  # sum(2) [16, L, 22] * [16, L, 22] -> [16, L]

    # event log-likelihood
    event_ll = compute_event(type_lambda, non_pad_mask)  # remove 0, add mask, put it in log
    event_ll = torch.sum(event_ll, dim=-1)  # [16]

    # non-event log-likelihood, either numerical integration or MC integration
    # non_event_ll = compute_integral_biased(type_lambda, time, non_pad_mask)
    non_event_ll = compute_integral_unbiased(model, data, time, non_pad_mask, type_mask)   # [16, L-1]
    non_event_ll = torch.sum(non_event_ll, dim=-1)   # [16]
    return event_ll, non_event_ll

# rating_prediction, event_type, test_label, opt.smooth, Constants.TYPE_NUMBER
def rating_loss(prediction, label, test_label, loss_func, num_type):
    """ Event prediction loss, cross entropy or label smoothing. """

    # convert [1,2,3] based types to [0,1,2]; also convert padding events to -1
    loss = loss_func(prediction, label, test_label, num_type)
    loss = torch.sum(loss)

    return loss


# def time_loss(prediction, event_time):
#     """ Time prediction loss. """
#
#     prediction.squeeze_(-1)  # [16, L, 1] -> [16, L]
#
#     true = event_time[:, 1:] - event_time[:, :-1]
#     prediction = prediction[:, :-1]
#
#     # event time gap prediction
#     diff = prediction - true
#     se = torch.sum(diff * diff)
#     return se


class LabelSmoothingLoss(nn.Module):
    """
    With label smoothing,
    KL-divergence between q_{smoothed ground truth prob.}(w)
    and p_{prob. computed by model}(w) is minimized.
    """

    def log_surplus_confidence_matrix(self, B, alpha=2.0, epsilon=1e-5):
        # To construct the surplus confidence matrix, we need to operate only on the nonzero elements.
        # This is not possible: S = alpha * np.log(1 + B / epsilon)
        S = B.copy()
        S.data = alpha * np.log(1 + S.data / epsilon)
        return S

    def __init__(self, label_smoothing, tgt_vocab_size, device, coefficient, ignore_index=-100):
        assert 0.0 < label_smoothing <= 1.0
        super(LabelSmoothingLoss, self).__init__()

        self.eps = label_smoothing
        self.num_classes = tgt_vocab_size
        self.ignore_index = ignore_index
        self.device = device
        self.coefficient = coefficient

    def forward(self, output, label, test_label, num_type):
        """
        output (DoubleTensor): (batch_size) x n_classes
        target (LongTensor): batch_size
        """

        # sampling = torch.zeros(label.size(0), num_type, device='cuda:0', dtype=torch.float32)
        one_hots = torch.zeros(label.size(0), num_type, device=self.device, dtype=torch.float32)
        # weight = torch.ones(label.size(0), num_type, device=self.device, dtype=torch.float32)
        # target_ = torch.ones(label.size()[0], num_type, device='cuda', dtype=torch.double)
        for i, (t, tl) in enumerate(zip(label, test_label)):
            where_ = torch.where(t != 0)[0]
            t = t[where_] - 1
            one_hots[i][t] = 1
            # weight[i][t] = 1
            # target_[i][t] = 0.2

            where_ = torch.where(tl != 0)[0]
            tl = tl[where_] - 1
            one_hots[i][tl] = 2
            # weight[i][tl] = 2
            # target_[i][tl] = 0.2

            # t = torch.cat((tl, t), 0)
            # x = t[t!=0].cpu().numpy()
            # y = np.random.randint(1, num_type + 1, size=(50*len(x)))
            # sampling_indices = torch.tensor(np.union1d(x, y), device='cuda:0')
            #
            # sampling[i][sampling_indices-1] = 1

        # loss = (weight * (one_hots - output)**2).sum() / num_type

        one_hots = one_hots * (1 - self.eps) + (1 - one_hots) * self.eps / num_type
        log_prb = F.logsigmoid(output)  # output [16, 161, 22]   log_prb [16, 161, 22]
        # log_prb = F.log_softmax(output, dim=-1)
        loss = -(one_hots * log_prb).sum(dim=-1)  # [16, 161, 22] #  * sampling

        return loss


# def time_loss(prediction, event_time):
#     """ Time prediction loss. """
#
#     prediction.squeeze_(-1)  # [16, L, 1] -> [16, L]
#
#     true = event_time[:, 1:] - event_time[:, :-1]
#     prediction = prediction[:, :-1]
#
#     # event time gap prediction
#     diff = prediction - true
#     se = torch.sum(diff * diff)
#     return se

