from unittest import TestCase
import numpy as np
from keras_transformer_xl.backend import keras
from keras_transformer_xl.backend import backend as K
from keras_transformer_xl import RelativeBias, RelativePartialMultiHeadSelfAttention


import torch
import torch.nn as nn
import torch.nn.functional as F


class RelMultiHeadAttn(nn.Module):
    def __init__(self, n_head, d_model, d_head, dropout, dropatt=0,
                 tgt_len=None, ext_len=None, mem_len=None, pre_lnorm=False):
        super(RelMultiHeadAttn, self).__init__()

        self.n_head = n_head
        self.d_model = d_model
        self.d_head = d_head
        self.dropout = dropout

        self.qkv_net = nn.Linear(d_model, 3 * n_head * d_head, bias=False)

        self.drop = nn.Dropout(dropout)
        self.dropatt = nn.Dropout(dropatt)
        self.o_net = nn.Linear(n_head * d_head, d_model, bias=False)

        self.layer_norm = nn.LayerNorm(d_model)

        self.scale = 1 / (d_head ** 0.5)

        self.pre_lnorm = pre_lnorm

    def _parallelogram_mask(self, h, w, left=False):
        mask = torch.ones((h, w)).byte()
        m = min(h, w)
        mask[:m,:m] = torch.triu(mask[:m,:m])
        mask[-m:,-m:] = torch.tril(mask[-m:,-m:])

        if left:
            return mask
        else:
            return mask.flip(0)

    def _shift(self, x, qlen, klen, mask, left=False):
        if qlen > 1:
            zero_pad = torch.zeros((x.size(0), qlen-1, x.size(2), x.size(3)),
                                    device=x.device, dtype=x.dtype)
        else:
            zero_pad = torch.zeros(0, device=x.device, dtype=x.dtype)

        if left:
            mask = mask.flip(1)
            x_padded = torch.cat([zero_pad, x], dim=1).expand(qlen, -1, -1, -1)
        else:
            x_padded = torch.cat([x, zero_pad], dim=1).expand(qlen, -1, -1, -1)

        x = x_padded.masked_select(mask[:,:,None,None]) \
                    .view(qlen, klen, x.size(2), x.size(3))

        return x

    def _rel_shift(self, x, zero_triu=False):
        zero_pad = torch.zeros((x.size(0), 1, *x.size()[2:]),
                               device=x.device, dtype=x.dtype)
        x_padded = torch.cat([zero_pad, x], dim=1)

        x_padded = x_padded.view(x.size(1) + 1, x.size(0), *x.size()[2:])

        x = x_padded[1:].view_as(x)

        if zero_triu:
            ones = torch.ones((x.size(0), x.size(1)))
            x = x * torch.tril(ones, x.size(1) - x.size(0))[:,:,None,None]

        return x

    def forward(self, w, r, attn_mask=None, mems=None):
        raise NotImplementedError

class RelPartialLearnableMultiHeadAttn(RelMultiHeadAttn):
    def __init__(self, *args, **kwargs):
        super(RelPartialLearnableMultiHeadAttn, self).__init__(*args, **kwargs)

        self.r_net = nn.Linear(self.d_model, self.n_head * self.d_head, bias=False)

    def forward(self, w, r, r_w_bias, r_r_bias, attn_mask=None, mems=None):
        qlen, rlen, bsz = w.size(0), r.size(0), w.size(1)

        if mems is not None:
            cat = torch.cat([mems, w], 0)
            if self.pre_lnorm:
                w_heads = self.qkv_net(self.layer_norm(cat))
            else:
                w_heads = self.qkv_net(cat)
            r_head_k = self.r_net(r)

            w_head_q, w_head_k, w_head_v = torch.chunk(w_heads, 3, dim=-1)
            w_head_q = w_head_q[-qlen:]
        else:
            if self.pre_lnorm:
                w_heads = self.qkv_net(self.layer_norm(w))
            else:
                w_heads = self.qkv_net(w)
            r_head_k = self.r_net(r)

            w_head_q, w_head_k, w_head_v = torch.chunk(w_heads, 3, dim=-1)

        klen = w_head_k.size(0)

        w_head_q = w_head_q.view(qlen, bsz, self.n_head, self.d_head)           # qlen x bsz x n_head x d_head
        w_head_k = w_head_k.view(klen, bsz, self.n_head, self.d_head)           # qlen x bsz x n_head x d_head
        w_head_v = w_head_v.view(klen, bsz, self.n_head, self.d_head)           # qlen x bsz x n_head x d_head

        r_head_k = r_head_k.view(rlen, self.n_head, self.d_head)                # qlen x n_head x d_head

        #### compute attention score                                      # qlen x bsz x n_head x d_head
        rw_head_q = w_head_q + r_w_bias
        AC = torch.einsum('ibnd,jbnd->ijbn', (rw_head_q, w_head_k))             # qlen x klen x bsz x n_head

        rr_head_q = w_head_q + r_r_bias
        BD = torch.einsum('ibnd,jnd->ijbn', (rr_head_q, r_head_k))              # qlen x klen x bsz x n_head
        BD = self._rel_shift(BD)

        attn_score = AC + BD
        attn_score.mul_(self.scale)

        #### compute attention probability
        if attn_mask is not None and attn_mask.any().item():
            if attn_mask.dim() == 2:
                attn_score = attn_score.float().masked_fill(
                    attn_mask[None,:,:,None], -float('inf')).type_as(attn_score)
            elif attn_mask.dim() == 3:
                attn_score = attn_score.float().masked_fill(
                    attn_mask[:,:,:,None], -float('inf')).type_as(attn_score)

        # [qlen x klen x bsz x n_head]
        attn_prob = F.softmax(attn_score, dim=1)
        attn_prob = self.dropatt(attn_prob)

        #### compute attention vector
        attn_vec = torch.einsum('ijbn,jbnd->ibnd', (attn_prob, w_head_v))

        # [qlen x bsz x n_head x d_head]
        attn_vec = attn_vec.contiguous().view(
            attn_vec.size(0), attn_vec.size(1), self.n_head * self.d_head)

        ##### linear projection
        attn_out = self.o_net(attn_vec)
        return attn_out
        attn_out = self.drop(attn_out)


        if self.pre_lnorm:
            ##### residual connection
            output = w + attn_out
        else:
            ##### residual connection + layer normalization
            output = self.layer_norm(w + attn_out)

        return output


class TestRelMultiHead(TestCase):

    def test_torch(self):
        np.random.seed(0xcafe)
        w = torch.Tensor(np.random.standard_normal((3, 2, 4)))
        r = torch.Tensor(np.random.standard_normal((3, 4)))
        net = RelPartialLearnableMultiHeadAttn(2, 4, 2, 0.0)

        w_qkv = np.random.standard_normal((12, 4))
        w_o = np.random.standard_normal((4, 4))
        w_r = np.random.standard_normal((4, 4))

        mem = np.random.standard_normal((2, 2, 4))

        r_w_bias = np.random.standard_normal((2, 2))
        r_r_bias = np.random.standard_normal((2, 2))

        new_r = torch.Tensor(np.random.standard_normal((5, 4)))

        net.qkv_net.weight.data = torch.Tensor(w_qkv)
        net.o_net.weight.data = torch.Tensor(w_o)
        net.r_net.weight.data = torch.Tensor(w_r)
        mask = (torch.triu(torch.ones((3, 5)), 1+2)).byte()[:, :, None]
        y = net(w, new_r, torch.Tensor(r_w_bias), torch.Tensor(r_r_bias), attn_mask=mask, mems=torch.Tensor(mem))
        print(y.shape)
        # y = y.permute(2, 0, 1, 3).reshape(2, 3, 10)
        y = y.permute(1, 0, 2).reshape(2, 3, 4)
        print(y.shape)
        print(y.tolist())

    def test_sample(self):
        inputs = np.array([
            [
                [0.7562695145606995, -0.7532438039779663, -0.2882295846939087, -1.6990371942520142],
                [-0.36805298924446106, 1.1673600673675537, -0.6914459466934204, -0.764503002166748],
                [-0.8440324068069458, 0.05585795268416405, -0.5827732086181641, 1.5028537511825562],
            ],
            [
                [-0.09864164888858795, -0.5235034227371216, -1.6001530885696411, 0.034417327493429184],
                [2.043482780456543, -0.27436429262161255, 0.04834289103746414, -1.0368596315383911],
                [-0.09311037510633469, 1.366316556930542, -0.38340920209884644, -1.2647643089294434],
            ],
        ])
        relatives = np.array([
            [
                [-1.02835214138031, 2.72334361076355, -0.2137114256620407, 0.032745007425546646],
                [-1.0028023719787598, -0.5825445652008057, 0.8192897439002991, -2.456073045730591],
                [-0.19838766753673553, 0.5683724284172058, 0.30482932925224304, 0.6687977313995361],
                [0.21221022307872772, 0.4338925778865814, 0.07372605055570602, -0.05447446182370186],
                [1.0132160186767578, 2.4036381244659424, 1.5104252099990845, 0.4218626022338867],
            ],
        ] * 2)
        memories = np.array([
            [
                [0.8704222505796531, 0.9361117741463981, 0.7442665348863866, 0.91392694614948],
                [-0.10018465175352317, -0.09182475290216006, -1.246047485363712, 1.6404603895987184],
            ],
            [
                [1.2712292341556446, 1.009655780936284, 0.4420362222435132, 1.5186087787070979],
                [1.4427767754835976, 1.2102150762070925, 0.8285545743394414, 0.7111875779008273],
            ],
        ])

        kernel_q = np.array([
            [0.32681036318004547, -1.1363779747587972, 1.2424950830966563, 0.613169803410901],
            [0.19156716698736181, -0.15233131547247872, -0.16130616338419873, -1.5391239758406403],
            [0.8386004334587568, 0.158423477487577, -1.6298737099566283, -1.2476893436624792],
            [-1.8390076172747616, -0.6984487776859649, 1.7229575808498785, -0.05514513857644962],
        ])
        kernel_k = np.array([
            [-0.5537408608863357, -0.4086071455923046, -0.13042129128885002, 0.7326026846217363],
            [-0.9965187549427492, -0.7286151488450243, -1.4269400640112133, 0.12752591749386982],
            [-0.6842234254089083, 1.2938629380821804, -0.713571658756806, 0.7387086112901271],
            [-1.2420165307152238, 0.7450912596769113, -0.5036154349645774, -1.4161019970745967],
        ])
        kernel_v = np.array([
            [-0.6396944907214142, 1.22301664512685, -0.9673069099782774, 0.6593494357199338],
            [-2.0010110577965783, -0.024541032664251092, 0.6614265651081772, -0.06233478795012013],
            [0.5843029435066284, -0.27128167541306714, -1.165650716838653, 0.3394579881849406],
            [-0.4033331631189724, 1.910258142043872, -0.5085143504562967, 0.05894554241531747],
        ])
        kernel_kv = np.concatenate([kernel_k, kernel_v], axis=-1)
        kernel_o = np.array([
            [1.0015451559801243, -0.41965070720383035, -0.6800689195006436, -1.3119449289237803],
            [0.7487130375684998, -0.2875756074838825, -0.39454047242128376, 1.5645034642903253],
            [-0.4244371286817957, 1.8712603426351773, 0.5442439581019961, 1.3203132828621442],
            [-0.45182923128222996, 2.531083895835167, -0.21672610899025968, 1.7673879116655695],
        ])
        kernel_r = np.array([
            [-0.8817194029613362, -0.47497798682688624, -0.531267085230172, 0.43338928943049837],
            [-0.6655645822150862, 1.0109350638555383, 0.12862169808408846, 0.2660771849859784],
            [0.2341787847442309, -0.5514102704837403, 0.18339345878624577, 1.4227633495535283],
            [-0.7641095447924122, -0.1450007600387442, 1.5279135983387981, -0.5072818940455809],
        ])

        bias_context = np.array([0.35799413043562894, -0.15005629449852656, 0.6263946579941496, 0.3409731658714878])
        bias_relative = np.array([-0.3082491589087075, -0.3751562822576601, 0.26067868083146517, 1.1346146882950412])

        input_layers = [
            keras.layers.Input(shape=(3, 4), name='Inputs'),
            keras.layers.Input(shape=(5, 4), name='Relatives'),
            keras.layers.Input(shape=(None, 4), name='Memories'),
        ]
        bias_layer = RelativeBias(
            4,
            name='Bias')
        att_layer = RelativePartialMultiHeadSelfAttention(
            4, 2,
            use_bias=False,
            name='Attention')
        outputs = [att_layer(input_layers + bias_layer(inputs[0]))]
        bias_layer.set_weights([bias_context, bias_relative])
        att_layer.set_weights([kernel_q, kernel_kv, kernel_o, kernel_r])
        model = K.function(input_layers, outputs)
        predicted = model([inputs, relatives, memories])[0]
        expected = np.array([
            [
                [-0.25255799293518066, 0.8124626278877258, -0.2652933895587921, -4.8075032234191895],
                [-0.15575078129768372, -0.09361740946769714, -0.34058552980422974, -4.246937274932861],
                [0.27790772914886475, 0.7010535001754761, -0.12163381278514862, 5.68528413772583],
            ],
            [
                [-0.6991382837295532, 1.746666431427002, 1.5876357555389404, 10.488723754882812],
                [0.5747594833374023, 0.002886146306991577, -1.46820068359375, 0.5143530964851379],
                [0.6013575792312622, -0.7014327645301819, -1.4661258459091187, 0.3123272657394409],
            ],
        ])
        self.assertEqual((2, 3, 4), predicted.shape)
        self.assertTrue(np.all(np.abs(predicted - expected) < 1e-4), predicted)
