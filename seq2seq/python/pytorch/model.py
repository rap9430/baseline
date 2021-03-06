import torch
import torch.nn as nn
from torch.autograd import Variable
import numpy as np
from utils import *

class SequenceCriterion(nn.Module):

    def __init__(self, nc):
        super(SequenceCriterion, self).__init__()
        # Assume pad is zero element for now
        weight = torch.ones(nc)
        weight[0] = 0
        self.crit = nn.NLLLoss(weight, size_average=False)
    
    def forward(self, inputs, targets):
        # This is BxT, which is what we want!
        total_sz = targets.nelement()
        loss = self.crit(inputs.view(total_sz, -1), targets.view(total_sz))
        return loss

def _rnn(insz, hsz, rnntype, nlayers):

    if rnntype == 'gru':
        rnn = torch.nn.GRU(insz, hsz, nlayers)
    else:
        rnn = torch.nn.LSTM(insz, hsz, nlayers)
    return rnn


class StackedLSTMCell(nn.Module):
    def __init__(self, num_layers, input_size, rnn_size, dropout):
        super(StackedLSTMCell, self).__init__()
        self.dropout = nn.Dropout(dropout)
        self.num_layers = num_layers
        self.layers = nn.ModuleList()

        for i in range(num_layers):
            self.layers.append(nn.LSTMCell(input_size=input_size, hidden_size=rnn_size, bias=False))
            input_size = rnn_size

    def forward(self, input, hidden):
        h_0, c_0 = hidden
        hs, cs = [], []
        for i, layer in enumerate(self.layers):
            h_i, c_i = layer(input, (h_0[i], c_0[i]))
            input = h_i
            if i != self.num_layers:
                input = self.dropout(input)
            hs += [h_i]
            cs += [c_i]

        hs = torch.stack(hs)
        cs = torch.stack(cs)

        return input, (hs, cs)


class StackedGRUCell(nn.Module):
    def __init__(self, num_layers, input_size, rnn_size, dropout):
        super(StackedGRUCell, self).__init__()
        self.dropout = nn.Dropout(dropout)
        self.num_layers = num_layers
        self.layers = nn.ModuleList()

        for i in range(num_layers):
            self.layers.append(nn.GRUCell(input_size=input_size, hidden_size=rnn_size))
            input_size = rnn_size

    def forward(self, input, hidden):
        h_0 = hidden
        hs = []
        for i, layer in enumerate(self.layers):
            h_i = layer(input, (h_0[i]))
            input = h_i
            if i != self.num_layers:
                input = self.dropout(input)
            hs += [h_i]

        hs = torch.stack(hs)

        return input, hs

def _rnn_cell(insz, hsz, rnntype, nlayers, dropout):

    if rnntype == 'gru':
        rnn = StackedGRUCell(nlayers, insz, hsz, dropout)
    else:
        rnn = StackedLSTMCell(nlayers, insz, hsz, dropout)
    print(rnn)
    return rnn



def _embedding(x2vec, finetune=True):
    dsz = x2vec.dsz
    lut = nn.Embedding(x2vec.vsz + 1, dsz, padding_idx=0)
    del lut.weight
    lut.weight = nn.Parameter(torch.FloatTensor(x2vec.weights),
                              requires_grad=finetune)
    return lut

def _append2seq(seq, modules):
    for module in modules:
        seq.add_module(str(module), module)

class Seq2SeqModel(nn.Module):

    def save(self, outdir, base):
        outname = '%s/%s.model' % (outdir, base)
        torch.save(self, outname)

    def create_loss(self):
        return SequenceCriterion(self.nc)

    @staticmethod
    def load(dirname, base):
        name = '%s/%s.model' % (dirname, base)
        return torch.load(name)

    # TODO: Add more dropout, BN
    def __init__(self, embed1, embed2, mxlen, hsz, nlayers, rnntype, batchfirst=True):
        super(Seq2SeqModel, self).__init__()
        dsz = embed1.dsz

        self.embed_in = _embedding(embed1)
        self.embed_out = _embedding(embed2)
        self.nc = embed2.vsz + 1            
        self.encoder_rnn = _rnn(dsz, hsz, rnntype, nlayers)
        self.decoder_rnn = _rnn(hsz, hsz, rnntype, nlayers)
        self.preds = nn.Linear(hsz, self.nc)
        self.batchfirst = batchfirst
        self.probs = nn.LogSoftmax()

    # Input better be xch, x
    def forward(self, input):
        rnn_enc_seq, final_encoder_state = self.encode(input[0])
        return self.decode(rnn_enc_seq, final_encoder_state, input[1])

    def decode(self, rnn_enc_seq, final_encoder_state, dst):
        if self.batchfirst is True:
            dst = dst.transpose(0, 1).contiguous()
        embed_out_seq = self.embed_out(dst)
        output, _ = self.decoder_rnn(embed_out_seq, final_encoder_state)

        # Reform batch as (T x B, D)
        pred = self.probs(self.preds(output.view(output.size(0)*output.size(1),
                                                 -1)))
        # back to T x B x H -> B x T x H
        pred = pred.view(output.size(0), output.size(1), -1)
        return pred.transpose(0, 1).contiguous() if self.batchfirst else pred

    def encode(self, src):
        if self.batchfirst is True:
            src = src.transpose(0, 1).contiguous()

        embed_in_seq = self.embed_in(src)
        return self.encoder_rnn(embed_in_seq)

class Seq2SeqAttnModel(nn.Module):

    def save(self, outdir, base):
        outname = '%s/%s.model' % (outdir, base)
        torch.save(self, outname)

    def create_loss(self):
        return SequenceCriterion(self.nc)

    @staticmethod
    def load(dirname, base):
        name = '%s/%s.model' % (dirname, base)
        return torch.load(name)

    # TODO: Add more dropout, BN
    def __init__(self, embed1, embed2, mxlen, hsz, nlayers, rnntype, batchfirst=True):
        super(Seq2SeqAttnModel, self).__init__()
        dsz = embed1.dsz
        self.embed_in = _embedding(embed1)
        self.embed_out = _embedding(embed2)
        self.nc = embed2.vsz + 1            
        self.encoder_rnn = _rnn(dsz, hsz, rnntype, nlayers)
        self.dropout = nn.Dropout(0.5)
        self.decoder_rnn = _rnn_cell(hsz + dsz, hsz, rnntype, nlayers, 0.5)
        self.preds = nn.Linear(hsz, self.nc)
        self.batchfirst = batchfirst
        self.probs = nn.LogSoftmax()
        self.output_to_attn = nn.Linear(hsz, hsz, bias=False)
        self.attn_softmax = nn.Softmax()
        self.attn_out = nn.Linear(2*hsz, hsz, bias=False)
        self.attn_tanh = nn.Tanh()
        self.nlayers = nlayers
        self.hsz = hsz

    def attn(self, output_t, context):
        # Output(t) = B x H x 1
        # Context = B x T x H
        # a = B x T x 1
        a = torch.bmm(context, self.output_to_attn(output_t).unsqueeze(2))
        a = self.attn_softmax(a.squeeze(2))
        # a = B x T
        # Want to apply over context, scaled by a
        # (B x 1 x T) (B x T x H) = (B x 1 x H)
        a = a.view(a.size(0), 1, a.size(1))
        combined = torch.bmm(a, context).squeeze(1)
        combined = torch.cat([combined, output_t], 1)
        combined = self.attn_tanh(self.attn_out(combined))
        return combined

    # Input better be xch, x
    def forward(self, input):
        rnn_enc_seq, final_encoder_state = self.encode(input[0])
        return self.decode(rnn_enc_seq, final_encoder_state, input[1])

    def output_0(self, context):
        batch_size = context.size(1)
        h_size = (batch_size, self.hsz)
        return Variable(context.data.new(*h_size).zero_(), requires_grad=False)

    def decode(self, context, final_encoder_state, dst):
        if self.batchfirst is True:
            dst = dst.transpose(0, 1).contiguous()
        embed_out_seq = self.embed_out(dst)
        context_transpose = context.t()
        h_i = final_encoder_state
        init_output = self.output_0(context)
        outputs = []
        output_i = init_output
        for i, embed_i in enumerate(embed_out_seq.split(1)):
            # 1 x B x D -> B x D
            embed_i = embed_i.squeeze(0)
            # Luong paper says to do this "input feeding", Kim confirms always
            # works better
            # B x (D + H)
            embed_i = torch.cat([embed_i, output_i], 1)
            output_i, h_i = self.decoder_rnn(embed_i, h_i)
            output_i = self.attn(output_i, context_transpose)
            output_i = self.dropout(output_i)
            outputs += [output_i]

        output = torch.stack(outputs)

        # Reform batch as (T x B, D)
        pred = self.probs(self.preds(output.view(output.size(0)*output.size(1),
                                                 -1)))
        # back to T x B x H -> B x T x H
        pred = pred.view(output.size(0), output.size(1), -1)
        return pred.transpose(0, 1).contiguous() if self.batchfirst else pred

    def encode(self, src):
        if self.batchfirst is True:
            src = src.transpose(0, 1).contiguous()

        embed_in_seq = self.embed_in(src)
        return self.encoder_rnn(embed_in_seq)
