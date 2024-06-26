import torch
from torch import nn
from torch.nn import functional as F

from scales.nets.selfies import TransformerDecoder
from scales.utils.utils import array_to_tensor


def _get_p_s_m_list_mnist(X, n_models, decoder):
    # y_s = torch.softmax(decoder(X), dim=-1)[..., 0]
    y_s = torch.unsqueeze(torch.sigmoid(decoder(X)[..., 0]), dim=1)
    y_s = (y_s > 0.5).float()
    # get the argmax of the probabilities
    p_s_m_list = []
    for m in range(n_models):
        y_s_m = torch.unsqueeze(torch.sigmoid(decoder(X)[..., 0]), dim=1)
        log_lik = -1 * torch.squeeze(nn.functional.binary_cross_entropy(y_s_m, y_s, reduction='none', reduce=0)).mean(
            axis=1)
        p_s_m_list.append(log_lik)
    return p_s_m_list


def _get_p_s_m_list_expr(X, n_models, decoder):
    if len(X.shape) == 1:
        X = X.unsqueeze(0)

    if isinstance(decoder, TransformerDecoder):
        pre_softmax = decoder(X, eval=False)
    else:
        pre_softmax = decoder(X)
    vocab_size = pre_softmax.shape[2]
    logits_s = torch.softmax(pre_softmax, dim=2).argmax(dim=2)
    y_s = torch.squeeze(F.one_hot(logits_s, num_classes=vocab_size).transpose(1, 2))
    p_s_m_list = []
    X_m_reps = torch.cat([X] * n_models, dim=0)
    if isinstance(decoder, TransformerDecoder):
        logits_all = decoder(X_m_reps, eval=False).transpose(1, 2)
    else:
        logits_all = decoder(X_m_reps).transpose(1, 2)
    for m in range(n_models):
        logits = logits_all[m * X.shape[0]: (m + 1) * X.shape[0]]
        loglik = (torch.log_softmax(logits, dim=1) * y_s).sum(axis=(1, 2))
        p_s_m_list.append(loglik)
    return p_s_m_list


def lde(log_ai, log_bi):
    max_log_p = torch.max(log_ai, log_bi)
    min_log_p = torch.min(log_ai, log_bi)
    return (max_log_p + torch.log(1 - torch.exp(min_log_p - max_log_p)))


def calculate_uncertainty(X, model, n_models=10, n_preds=10, dataset="expressions"):
    model.train()
    if hasattr(model, "decoder"):
        decoder = model.decoder
    else:
        decoder = model
    decoder.train()
    if hasattr(decoder, "trained_vae"):
        decoder.trained_vae.train()
    X = array_to_tensor(X, model.device)
    with torch.no_grad():
        mi_list = []
        for pred in range(n_preds):
            p_s_m_func = _get_p_s_m_list_mnist if dataset == "mnist" else _get_p_s_m_list_expr
            p_s_m_list = p_s_m_func(X, n_models, decoder)  # log likelihood of the data under each model
            # print("p_s_m_list", p_s_m_list)
            log_p_s_m = torch.stack(p_s_m_list, dim=1).to(dtype=model.dtype, device=model.device)  # [n_preds, n_models]
            minus_log_n_models = -1 * torch.log(torch.tensor(n_models).float())
            log_p_s = minus_log_n_models + log_p_s_m.logsumexp(dim=1)  # log_p_tot_j - log average across models
            ln_b = log_p_s + torch.log(-log_p_s)
            pj_logpj = log_p_s_m + torch.log(-log_p_s_m)
            ln_a = pj_logpj.logsumexp(dim=1) + minus_log_n_models
            mi = lde(ln_a, ln_b) - log_p_s
            mi_list.append(mi)

        return - torch.log(torch.tensor(n_preds).float()) + torch.stack(mi_list, dim=1).logsumexp(dim=1)
