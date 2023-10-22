import torch
import torch.nn.functional as F
from torch import nn
from torch.autograd import Variable
from torch_geometric.utils import softmax
from torch_scatter import scatter_add


def pad(tensor, length, cuda_flag):
    if isinstance(tensor, Variable):
        var = tensor
        if length > var.size(0):
            if cuda_flag:
                return torch.cat([var, torch.zeros(length - var.size(0), *var.size()[1:]).cuda()])
            else:
                return torch.cat([var, torch.zeros(length - var.size(0), *var.size()[1:])])
        else:
            return var
    else:
        if length > tensor.size(0):
            if cuda_flag:
                return torch.cat([tensor, torch.zeros(length - tensor.size(0), *tensor.size()[1:]).cuda()])
            else:
                return torch.cat([tensor, torch.zeros(length - tensor.size(0), *tensor.size()[1:])])
        else:
            return tensor


def feature_transfer(bank_s_, bank_p_, bank_sp_, seq_lengths, cuda_flag=False):
    input_conversation_length = torch.tensor(seq_lengths)
    start_zero = input_conversation_length.data.new(1).zero_()
    if cuda_flag:
        input_conversation_length = input_conversation_length.cuda()
        start_zero = start_zero.cuda()

    max_len = max(seq_lengths)
    start = torch.cumsum(torch.cat((start_zero, input_conversation_length[:-1])), 0)
    # (l,b,h)
    bank_s = torch.stack(
        [pad(bank_s_.narrow(0, s, l), max_len, cuda_flag) for s, l in zip(start.data.tolist(), input_conversation_length.data.tolist())], 0
    ).transpose(0, 1) if bank_s_ is not None else None
    bank_p = torch.stack(
        [pad(bank_p_.narrow(0, s, l), max_len, cuda_flag) for s, l in zip(start.data.tolist(), input_conversation_length.data.tolist())], 0
    ).transpose(0, 1) if bank_p_ is not None else None
    bank_sp = torch.stack(
        [pad(bank_sp_.narrow(0, s, l), max_len, cuda_flag) for s, l in zip(start.data.tolist(), input_conversation_length.data.tolist())], 0
    ).transpose(0, 1) if bank_sp_ is not None else None
    return bank_s, bank_p, bank_sp


class ReasonModule(nn.Module):
    def __init__(self, in_channels=200, processing_steps=0, num_layers=1):
        """
        Reasoning Module
        """
        super(ReasonModule, self).__init__()

        self.in_channels = in_channels
        self.out_channels = 2 * in_channels
        # self.out_channels = 1 * in_channels # base
        self.processing_steps = processing_steps
        self.num_layers = num_layers
        if processing_steps > 0:
            self.lstm = nn.LSTM(self.out_channels, self.in_channels, num_layers)  # 400,200,1
            self.lstm.reset_parameters()
        print(self)

    def forward(self, x, bank_kg, batch, q_star):
        if self.processing_steps <= 0: return q_star

        batch_size = batch.max().item() + 1
        h = (x.new_zeros((self.num_layers, batch_size, self.in_channels)),
             x.new_zeros((self.num_layers, batch_size, self.in_channels)))
        for i in range(self.processing_steps):
            q, h = self.lstm(q_star.unsqueeze(0), h)
            q = q.view(batch_size, self.in_channels)
            e = (x * q[batch]).sum(dim=-1, keepdim=True)
            a = softmax(e, batch, num_nodes=batch_size)
            r = scatter_add(a * x, batch, dim=0, dim_size=batch_size)
            q_star = torch.cat([q, r], dim=-1)
            # q_star = q+r # base

        return q_star

    def __repr__(self):
        return '{}({}, {})'.format(self.__class__.__name__, self.in_channels, self.out_channels)


class CognitionNetwork(nn.Module):
    def __init__(self, n_features=200, n_classes=7, dropout=0.2, cuda_flag=False, reason_steps=None):
        """
        Multi-turn Reasoning Modules
        """
        super(CognitionNetwork, self).__init__()
        self.cuda_flag = cuda_flag
        self.reason_flag = False  # reason_flag is False if embedding backbone is transformer,  is True if backbone is word2vec/glove

        if self.reason_flag:
            # Reason Modules in DialogueCRN
            self.steps = reason_steps if reason_steps is not None else [0, 0]
            self.fc = nn.Linear(n_features, n_features * 2)
            self.reason_modules = nn.ModuleList([
                ReasonModule(in_channels=n_features, processing_steps=self.steps[0], num_layers=1),
                ReasonModule(in_channels=n_features, processing_steps=self.steps[1], num_layers=1)
            ])

        # self.scl_flag = scl_flag
        self.n_features = n_features
        if not self.reason_flag:
            self.smax_fc = nn.Linear(n_features * 2, n_classes)
        else:
            self.smax_fc = nn.Linear(n_features * 4, n_classes)  # Reason Modules in DialogueCRN

        self.dropout = nn.Dropout(dropout)

    def forward(self, U_s, U_p, seq_lengths):

        # (b) <== (l,b,h)
        batch_size = U_s.size(1)
        batch_index, context_s_, context_p_ = [], [], []

        for j in range(batch_size):
            if self.reason_flag: batch_index.extend([j] * seq_lengths[j])
            context_s_.append(U_s[:seq_lengths[j], j, :])
            context_p_.append(U_p[:seq_lengths[j], j, :])

        if self.reason_flag: batch_index = torch.tensor(batch_index)
        bank_s_ = torch.cat(context_s_, dim=0)
        bank_p_ = torch.cat(context_p_, dim=0)
        if self.cuda_flag:
            if self.reason_flag: batch_index = batch_index.cuda()
            bank_s_ = bank_s_.cuda()
            bank_p_ = bank_p_.cuda()

        # (l,b,h) << (l*b,h) bank_s
        bank_s, bank_p, _ = feature_transfer(bank_s_, bank_p_, None, seq_lengths, self.cuda_flag)
        feature_s, feature_p = bank_s, bank_p

        if self.reason_flag:
            # reason_flag is False in Dual-LSTM
            feature_ = []
            for t in range(bank_s.size(0)):
                q_star = self.fc(bank_s[t])
                q_situ = self.reason_modules[0](bank_s_, None, batch_index, q_star)
                feature_.append(q_situ.unsqueeze(0))
            feature_s = torch.cat(feature_, dim=0)

            feature_ = []
            for t in range(bank_p.size(0)):
                q_star = self.fc(bank_p[t])
                q_party = self.reason_modules[1](bank_p_, None, batch_index, q_star)
                feature_.append(q_party.unsqueeze(0))
            feature_p = torch.cat(feature_, dim=0)

        hidden = torch.cat([feature_s, feature_p], dim=-1)

        hidden0 = self.smax_fc(self.dropout(F.relu(hidden)))
        log_prob = F.log_softmax(hidden0, 2)
        log_prob = torch.cat([log_prob[:, j, :][:seq_lengths[j]] for j in range(len(seq_lengths))])

        hidden = torch.cat([hidden[:, j, :][:seq_lengths[j]] for j in range(len(seq_lengths))])
        return log_prob, hidden


class DialogueCRN(nn.Module):
    def __init__(self, base_model='LSTM', base_layer=2, input_size=None, hidden_size=None, n_speakers=2,
                 n_classes=7, dropout=0.2, cuda_flag=False, reason_steps=None):
        """
        Contextual Reasoning Network
        """

        super(DialogueCRN, self).__init__()
        self.base_model = base_model
        self.n_speakers = n_speakers
        self.base_layer = base_layer
        if self.base_model == 'LSTM':
            self.rnn = nn.LSTM(input_size=input_size + 768 * 0 + 128 * 0, hidden_size=hidden_size, num_layers=base_layer, bidirectional=True, dropout=dropout)
            self.rnn_parties = nn.LSTM(input_size=input_size + 768 * 0 + 128 * 0, hidden_size=hidden_size, num_layers=base_layer, bidirectional=True,
                                       dropout=dropout)

        elif self.base_model == 'GRU':
            self.rnn = nn.GRU(input_size=input_size, hidden_size=hidden_size, num_layers=base_layer, bidirectional=True, dropout=dropout)
            self.rnn_parties = nn.GRU(input_size=input_size, hidden_size=hidden_size, num_layers=base_layer, bidirectional=True, dropout=dropout)
        elif self.base_model == 'Linear':
            self.base_linear = nn.Linear(input_size, hidden_size)
            self.dropout = nn.Dropout(dropout)
            self.smax_fc = nn.Linear(hidden_size, n_classes)

        else:
            print('Base model must be one of LSTM/GRU/Linear')
            raise NotImplementedError
        self.hidden_size = hidden_size
        if self.base_model != 'Linear':
            self.cognition_net = CognitionNetwork(n_features=2 * hidden_size, n_classes=n_classes, dropout=dropout, cuda_flag=cuda_flag,
                                                  reason_steps=reason_steps)

        print(self)

    def init_hidden(self, num_directs, num_layers, batch_size, d_model):
        return Variable(torch.zeros(num_directs * num_layers, batch_size, d_model))

    def forward(self, r1, qmask, seq_lengths):

        U = r1
        U2 = r1
        U_s, U_p = None, None
        if self.base_model == 'LSTM':
            # (b,l,h), (b,l,p)
            U_, qmask_ = U.transpose(0, 1), qmask.transpose(0, 1)
            U_p_ = torch.zeros(U_.size()[0], U_.size()[1], self.hidden_size * 2).type(U.type())
            U_parties_ = [torch.zeros_like(U_).type(U_.type()) for _ in range(self.n_speakers)]
            pb_flag = torch.zeros(self.n_speakers, U_.size(0))
            pl_min = 2  # 1 in DialogueCRN, 2 in Dual-LSTM/SACL-LSTM
            for b in range(U_.size(0)):
                for p in range(len(U_parties_)):
                    index_i = torch.nonzero(qmask_[b][:, p]).squeeze(-1)

                    if index_i.size(0) >= pl_min:
                        pb_flag[p][b] = 1
                        U_parties_[p][b][:index_i.size(0)] = U_[b][index_i]

            for p in range(len(U_parties_)):
                # U_parties_[p]: (b,seq,h)
                index_b = torch.nonzero(pb_flag[p]).squeeze(-1)
                # (b,l,h)
                temp_ = U_parties_[p][index_b]
                h_temp = torch.zeros_like(U_p_).type(U_p_.type())
                h_temp[index_b] = self.rnn_parties(temp_.transpose(0, 1))[0].transpose(0, 1)  # temp_

                for b in range(U_p_.size(0)):
                    index_i = torch.nonzero(qmask_[b][:, p]).squeeze(-1)
                    if index_i.size(0) >= pl_min:
                        U_p_[b][index_i] = h_temp[b][:index_i.size(0)]

            U_p = U_p_.transpose(0, 1)
            U_s, hidden2 = self.rnn(U2)

        elif self.base_model == 'GRU':
            U_, qmask_ = U.transpose(0, 1), qmask.transpose(0, 1)
            U_p_ = torch.zeros(U_.size()[0], U_.size()[1], 200).type(U.type())
            U_parties_ = [torch.zeros_like(U_).type(U_.type()) for _ in range(self.n_speakers)]  # default 2
            for b in range(U_.size(0)):
                for p in range(len(U_parties_)):
                    index_i = torch.nonzero(qmask_[b][:, p]).squeeze(-1)
                    if index_i.size(0) > 0:
                        U_parties_[p][b][:index_i.size(0)] = U_[b][index_i]
            E_parties_ = [self.rnn_parties(U_parties_[p].transpose(0, 1))[0].transpose(0, 1) for p in range(len(U_parties_))]

            for b in range(U_p_.size(0)):
                for p in range(len(U_parties_)):
                    index_i = torch.nonzero(qmask_[b][:, p]).squeeze(-1)
                    if index_i.size(0) > 0: U_p_[b][index_i] = E_parties_[p][b][:index_i.size(0)]
            U_p = U_p_.transpose(0, 1)
            U_s, hidden2 = self.rnn(U)
        elif self.base_model == 'Linear':
            U = self.base_linear(U)
            U = self.dropout(F.relu(U))
            hidden = self.smax_fc(U)
            log_prob = F.log_softmax(hidden, 2)
            logits = torch.cat([log_prob[:, j, :][:seq_lengths[j]] for j in range(len(seq_lengths))])
            logits2 = torch.cat([U[:, j, :][:seq_lengths[j]] for j in range(len(seq_lengths))])
            return logits, logits2

        logits, logits2 = self.cognition_net(U_s, U_p, seq_lengths)

        return logits, logits2
