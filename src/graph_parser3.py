import sys
import numpy as np
import yaml
import argparse
import torch.nn.functional as F

from torch import nn
from torch import optim
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from random import sample,shuffle,random
from mst import chuliu_edmonds
from lexers  import *
from deptree import *


class DependencyDataset:
    """
    A representation of the DepBank for efficient processing.
    This is a sorted dataset.
    """
    PAD_IDX            = 0
    PAD_TOKEN          = '<pad>'
    UNK_WORD           = '<unk>'

    @staticmethod
    def read_conll(filename):
        istream       = open(filename)
        treelist = []
        tree = DepGraph.read_tree(istream) 
        while tree:
            if len(tree.words) <= 150:
                treelist.append(tree)
            else:
                print('dropped tree with length',len(tree.words))
            tree = DepGraph.read_tree(istream)
        istream.close()  
        return treelist
        
    def __init__(self,treelist,lexer,char_dataset,use_labels=None,use_tags=None):
           
        self.lexer        = lexer
        self.char_dataset = char_dataset
        self.treelist     = treelist
        if use_labels:
            self.itolab = use_labels
            self.labtoi = {label:idx for idx,label in enumerate(self.itolab)}
        else:
            self.init_labels(self.treelist)
        if use_tags:
            self.itotag = use_tags
            self.tagtoi = {tag:idx for idx,tag in enumerate(self.itotag)}
        else:
            self.init_tags(self.treelist)

    def encode(self):
        self.deps, self.heads,self.labels,self.tags = [ ],[ ],[ ],[ ]
        self.words,self.mwe_ranges, self.cats = [ ], [ ], [ ]

        for tree in self.treelist:
            depword_idxes = self.lexer.tokenize(tree.words)
            deptag_idxes  = [self.tagtoi.get(tag,self.tagtoi[DependencyDataset.UNK_WORD]) for tag in tree.pos_tags]

            self.words.append(tree.words)
            self.cats.append(tree.pos_tags)
            self.tags.append(deptag_idxes)
            self.deps.append(depword_idxes)
            self.heads.append(self.oracle_governors(tree))
            # the get defaulting to 0 is a hack for labels not found in training set
            self.labels.append([self.labtoi.get(lab,0) for lab in self.oracle_labels(tree)])
            self.mwe_ranges.append(tree.mwe_ranges)

    def save_vocab(self,filename):
        out = open(filename,'w')
        print(' '.join(self.itolab),file=out)
        print(' '.join(self.itotag),file=out)
        out.close()

    @staticmethod
    def load_vocab(filename):
        reloaded = open(filename)
        itolab   = reloaded.readline().split()
        itotag   = reloaded.readline().split()
        reloaded.close()
        return itos,itolab,itotag
    
    def shuffle_data(self):
        N = len(self.deps)
        order = list(range(N))
        shuffle(order)
        self.deps       = [self.deps[i] for i in order]
        self.tags       = [self.tags[i] for i in order]
        self.heads      = [self.heads[i] for i in order]
        self.labels     = [self.labels[i] for i in order]
        self.words      = [self.words[i] for i in order]
        self.cats       = [self.cats[i] for i in order]
        self.mwe_ranges = [self.mwe_ranges[i] for i in order]

    def order_data(self):
        N           = len(self.deps)
        order       = list(range(N))        
        lengths     = map(len,self.deps)
        order       = [idx for idx, L in sorted(zip(order,lengths),key=lambda x:x[1])]
        self.deps   = [self.deps[idx] for idx in order]
        self.tags   = [self.tags[idx] for idx in order]
        self.heads  = [self.heads[idx] for idx in order]
        self.labels = [self.labels[idx] for idx in order]
        self.words  = [self.words[idx] for idx in order]
        self.mwe_ranges  = [self.mwe_ranges[idx] for idx in order]
        self.cats        = [self.cats[idx] for idx in order]
        
    def make_batches(self, batch_size,shuffle_batches=False,shuffle_data=True,order_by_length=False):
        self.encode()
        if shuffle_data:  
            self.shuffle_data()
        if order_by_length: #shuffling and ordering is relevant : it change the way ties are resolved and thus batch construction
            self.order_data()

        N = len(self.deps)
        batch_order = list(range(0,N, batch_size))
        if shuffle_batches:
            shuffle(batch_order)
            
        for i in batch_order: 
            deps   = self.pad(self.deps[i:i+batch_size])
            tags   = self.pad(self.tags[i:i+batch_size])
            heads  = self.pad(self.heads[i:i+batch_size])
            labels = self.pad(self.labels[i:i+batch_size])
            words  = self.words[i:i+batch_size]
            mwe    = self.mwe_ranges[i:i+batch_size]
            cats   = self.cats[i:i+batch_size]
            chars  = self.char_dataset.batch_chars(self.words[i:i+batch_size])
            yield (words,mwe,chars,cats,deps,tags,heads,labels)

    def pad(self,batch): 
        if type(batch[0]) == tuple and len(batch[0]) == 2:   #had hoc stuff for BERT Lexers
            sent_lengths                = [ len(seqA) for (seqA,seqB) in batch] 
            max_len                     = max(sent_lengths)
            padded_batchA,padded_batchB = [ ], [ ]
            for k, seq in zip(sent_lengths, batch):
                seqA,seqB = seq
                paddedA   = seqA + (max_len - k)*[ DependencyDataset.PAD_IDX]
                paddedB   = seqB + (max_len - k)*[ self.lexer.BERT_PAD_IDX ]  
                padded_batchA.append(paddedA)
                padded_batchB.append(paddedB)
            return  ( Variable(torch.LongTensor(padded_batchA)) , Variable(torch.LongTensor(padded_batchB)) )                
        else:
            sent_lengths = list(map(len, batch))
            max_len      = max(sent_lengths)
            padded_batch = [ ]
            for k, seq in zip(sent_lengths, batch):
                padded = seq + (max_len - k) * [ DependencyDataset.PAD_IDX]

                padded_batch.append(padded)
        return Variable( torch.LongTensor(padded_batch) )

    def init_labels(self,treelist):
        labels      = set([ lbl for tree in treelist for (gov,lbl,dep) in tree.get_all_edges()])
        self.itolab = [DependencyDataset.PAD_TOKEN] + list(labels)
        self.labtoi = {label:idx for idx,label in enumerate(self.itolab)}

    def init_tags(self,treelist):
        tagset  = set([ tag for tree in treelist for tag in tree.pos_tags])
        tagset.update([DepGraph.ROOT_TOKEN,DependencyDataset.UNK_WORD])
        self.itotag = [DependencyDataset.PAD_TOKEN] + list(tagset)
        self.tagtoi = {tag:idx for idx,tag in enumerate(self.itotag)}
            
    def __len__(self):      
        return len(self.treelist)
    
    def oracle_labels(self,depgraph):
        """
        Returns a list where each element list[i] is the label of
        the position of the governor of the word at position i.
        Returns:
        a tensor of size N.
        """
        N          = len(depgraph)
        edges      = depgraph.get_all_edges( )
        rev_labels = dict( [ (dep,label) for (gov,label,dep) in edges ] )
        return [ rev_labels.get(idx,DependencyDataset.PAD_TOKEN) for idx in range(N) ]   
        
    def oracle_governors( self,depgraph ): 
        """
        Returns a list where each element list[i] is the index of
        the position of the governor of the word at position i.
        Returns:
        a tensor of size N.
        """
        N         = len( depgraph )
        edges     = depgraph.get_all_edges( )
        rev_edges = dict( [(dep,gov) for (gov,label,dep) in edges ] )
        return [ rev_edges.get(idx,0) for idx in range(N) ]


class CharDataSet:
    """
    Namespace for simulating a char dataset
    """
    def __init__(self,charlist):
        self.i2c   = charlist
        self.c2idx = dict(  [(c,idx) for idx,c in enumerate(charlist)] )

    def __len__(self):
        return len(self.i2c)

    def word2charcodes(self,token):
        """
        Turns a string into a list of char codes.
        If the string is <pad> or <unk> returns an empty list of char codes
        """
        return [  ] if token in [DependencyDataset.PAD_TOKEN,DependencyDataset.UNK_WORD] else [ self.c2idx[c] for c in token if c in self.c2idx ]

    def batchedtokens2codes(self,toklist):
        """
        Codes a list of tokens as a batch of lists of charcodes and pads them if needed
        :param toklist:
        :return:
        """
        max_tok_len  = [ len(token) for token in toklist ]
        charcodes    = [ self.word2charcodes(token)  for token in toklist ]
        sent_lengths = list( map(len, charcodes))
        max_len      = max( sent_lengths)
        padded_codes = [ ]
        for k, seq in zip(sent_lengths, charcodes):
            padded = seq + (max_len - k) * [ DependencyDataset.PAD_IDX ]
            padded_codes.append(padded)
        return torch.tensor(padded_codes)


    def batch_chars(self,sent_batch):
        """
        This is a generator that yields batches of character codes word by word.
        Performs the padding as well.
        :param sent_batch: a batch of list of tokens (non padded)
        :yields: a batch (torch.tensor) of character idxes
        """
        sent_lengths  = list( map(len, sent_batch))
        max_sent_len  = max(sent_lengths)

        batched_sents = []
        for k, seq in zip(sent_lengths, sent_batch):
            padded = seq + (max_sent_len - k) * [ DependencyDataset.PAD_TOKEN ]
            batched_sents.append(padded)

        for idx in range(max_sent_len):
            yield self.batchedtokens2codes( [sentence[idx] for sentence in batched_sents])

    @staticmethod
    def make_vocab(wordlist):
        charset = set()
        for token in wordlist:
            charset.update(list(token))

        return CharDataSet([DependencyDataset.PAD_TOKEN] + list(charset))


class CharRNN(nn.Module):

    def __init__(self,charset_size,char_embedding_size,word_embedding_size):

        super(CharRNN, self).__init__()

        self.word_embedding_size = word_embedding_size #bc bi-lstm
        self.char_embedding      = nn.Embedding(charset_size, char_embedding_size, padding_idx=DependencyDataset.PAD_IDX)
        self.char_bilstm         = nn.LSTM(char_embedding_size,\
                                           int(self.word_embedding_size/2),1,\
                                           batch_first=True,\
                                           bidirectional=True)


    def forward(self,xinput):
        """
        Predicts the word embedding from the token characters.
        :param xinput: is a tensor of char indexes encoding a batch of tokens [batch,token_len,char_seq_idx]
        :return: a word embedding tensor
        """

        embeddings = self.char_embedding(xinput)
        outputs,( _ ,cembedding) = self.char_bilstm(embeddings)
        result = cembedding.view(-1,self.word_embedding_size)
        return result


class MLP(nn.Module):

    def __init__(self,input_size,hidden_size,output_size,dropout=0.0):
        super(MLP, self).__init__()
        self.Wdown    = nn.Linear(input_size,hidden_size)
        self.Wup      = nn.Linear(hidden_size,output_size)
        self.g        = nn.ReLU()
        self.dropout  = nn.Dropout(p=dropout)
        
    def forward(self,input):
        return self.Wup(self.dropout(self.g(self.Wdown(input))))

    
class BiAffine(nn.Module):
    """Biaffine attention layer."""
    def __init__(self, input_dim, output_dim):
        super(BiAffine, self).__init__()
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.U = nn.Parameter(torch.FloatTensor(output_dim, input_dim, input_dim)) #check init
        nn.init.xavier_uniform_(self.U)
        
    def forward(self, Rh, Rd):
        Rh = Rh.unsqueeze(1)
        Rd = Rd.unsqueeze(1)
        S = Rh @ self.U @ Rd.transpose(-1, -2)
        return S.squeeze(1)



class Tagger(nn.Module):

    def __init__(self,input_dim,tagset_size):
        super(Tagger, self).__init__()
        self.W = nn.Linear(input_dim,tagset_size)

    def forward(self,input):
        return self.W(input)

class BiAffineParser(nn.Module):
    
    """Biaffine Dependency Parser."""
    def __init__(self,
                 lexer,
                 char_rnn,
                 tagset_size,
                 encoder_dropout, #lstm dropout
                 mlp_input,
                 mlp_tag_hidden,
                 mlp_arc_hidden,
                 mlp_lab_hidden,
                 mlp_dropout,
                 num_labels,
                 device='cuda:1'):

        super(BiAffineParser, self).__init__()
        self.device            = torch.device(device) if type(device) == str else device
        self.lexer             = lexer.to(self.device)
        self.tag_rnn           = nn.LSTM(self.lexer.embedding_size+char_rnn.word_embedding_size,mlp_input,2, batch_first=True,dropout=encoder_dropout,bidirectional=True).to(self.device)
        self.dep_rnn           = nn.LSTM(tagset_size+self.lexer.embedding_size+char_rnn.word_embedding_size,mlp_input,2, batch_first=True,dropout=encoder_dropout,bidirectional=True).to(self.device)

        #POS tagger & char RNN
        #self.pos_tagger    = Tagger(mlp_input*2,tagset_size).to(self.device)
        self.pos_tagger    = MLP(mlp_input * 2,mlp_tag_hidden,tagset_size).to(self.device)
        self.char_rnn      = char_rnn.to(self.device)
        # Arc MLPs
        self.arc_mlp_h = MLP(mlp_input*2, mlp_arc_hidden, mlp_input, mlp_dropout).to(self.device)
        self.arc_mlp_d = MLP(mlp_input*2, mlp_arc_hidden, mlp_input, mlp_dropout).to(self.device)
        # Label MLPs
        self.lab_mlp_h = MLP(mlp_input*2, mlp_lab_hidden, mlp_input, mlp_dropout).to(self.device)
        self.lab_mlp_d = MLP(mlp_input*2, mlp_lab_hidden, mlp_input, mlp_dropout).to(self.device)

        # BiAffine layers
        self.arc_biaffine = BiAffine(mlp_input, 1).to(self.device)
        self.lab_biaffine = BiAffine(mlp_input, num_labels).to(self.device)

        #hyperparams for saving...
        self.tagset_size                                           = tagset_size
        self.mlp_input,self.mlp_arc_hidden,self.mlp_lab_hidden     = mlp_input,mlp_arc_hidden,mlp_lab_hidden,
        self.num_labels                                            = num_labels 
        
    def save_params(self,path):

        torch.save(self.state_dict(), path)
        
    def load_params(self,path):

        self.load_state_dict(torch.load(path))
        self.eval()

    def forward(self,xwords,xchars):

        """Computes char embeddings"""
        char_embed = torch.stack([self.char_rnn(column) for column in xchars],dim=1)
        """Computes word embeddings"""
        lex_emb    = self.lexer(xwords)

        """encodes input for tagging"""
        tag_input         = torch.cat((lex_emb,char_embed),dim=2)
        tag_embeddings,_  = self.tag_rnn(tag_input)

        """Performs POS tagging"""
        tag_scores        = self.pos_tagger(tag_embeddings)

        """Encodes parser input"""
        #dep_input         =  torch.cat((lex_emb,char_embed,F.softmax(tag_scores,dim=2)),dim=2)
        dep_input = torch.cat((lex_emb, char_embed, tag_scores), dim=2)
        dep_embeddings, _ = self.dep_rnn(dep_input)

        """Compute the score matrices for the arcs and labels."""
        arc_h      = self.arc_mlp_h(dep_embeddings)
        arc_d      = self.arc_mlp_d(dep_embeddings)
        lab_h      = self.lab_mlp_h(dep_embeddings)
        lab_d      = self.lab_mlp_d(dep_embeddings)
                
        arc_scores = self.arc_biaffine(arc_h, arc_d)
        lab_scores = self.lab_biaffine(lab_h, lab_d)

        return tag_scores, arc_scores, lab_scores


    def eval_model(self,dev_set,batch_size):

        loss_fnc   = nn.CrossEntropyLoss(reduction='sum')

        #Note: the accurracy scoring is approximative and cannot be interpreted as an UAS/LAS score !
        
        self.eval()
        self.lexer.eval_mode()

        dev_batches = dev_set.make_batches(batch_size,shuffle_batches=True,shuffle_data=True,order_by_length=True)
        tag_acc, arc_acc, lab_acc, gloss, taggerZ, arcsZ = 0, 0, 0, 0, 0, 0
        overall_size = 0
        
        with torch.no_grad():
            for batch in dev_batches:
                words,mwe,chars,cats,deps,tags,heads,labels = batch
                if type(deps)==tuple:
                    depsA,depsB   = deps
                    deps          = (depsA.to(self.device),depsB.to(self.device))
                    overall_size += (depsA.size(0)*depsA.size(1)) #bc no masking at training           
                else:
                    deps           = deps.to(self.device)
                    overall_size  += (deps.size(0)*deps.size(1)) #bc no masking at training           
                heads, labels,tags =  heads.to(self.device), labels.to(self.device),tags.to(self.device)
                chars              =  [ token.to(self.device) for token in chars ]

                #preds 
                tagger_scores, arc_scores, lab_scores = self.forward(deps,chars)
                
                #get global loss
                #ARC LOSS
                arc_scoresL  = arc_scores.transpose(-1, -2)                             # [batch, sent_len, sent_len]
                arc_scoresL  = arc_scoresL.contiguous().view(-1, arc_scoresL.size(-1))   # [batch*sent_len, sent_len]
                arc_loss     = loss_fnc(arc_scoresL, heads.view(-1))                    # [batch*sent_len]

                # TAGGER_LOSS
                tagger_scoresB = tagger_scores.contiguous().view(-1, tagger_scores.size(-1))
                tagger_loss    = loss_fnc(tagger_scoresB, tags.view(-1))

                #LABEL LOSS
                headsL       = heads.unsqueeze(1).unsqueeze(2)                          # [batch, 1, 1, sent_len]
                headsL       = headsL.expand(-1, lab_scores.size(1), -1, -1)            # [batch, n_labels, 1, sent_len]
                lab_scoresL  = torch.gather(lab_scores, 2, headsL).squeeze(2)           # [batch, n_labels, sent_len]
                lab_scoresL  = lab_scoresL.transpose(-1, -2)                            # [batch, sent_len, n_labels]
                lab_scoresL  = lab_scoresL.contiguous().view(-1, lab_scoresL.size(-1))  # [batch*sent_len, n_labels]
                labelsL      = labels.view(-1)                                          # [batch*sent_len]
                lab_loss     = loss_fnc(lab_scoresL, labelsL)
                
                loss         = tagger_loss + arc_loss + lab_loss
                gloss       += loss.item()

                #greedy arc accurracy (without parsing)
                _, pred = arc_scores.max(dim=-2)
                mask = (heads != DependencyDataset.PAD_IDX).float()
                arc_accurracy = torch.sum((pred == heads).float() * mask, dim=-1)
                arc_acc += torch.sum(arc_accurracy).item()

                #tagger accurracy
                _, tag_pred = tagger_scores.max(dim=2)
                mask = (tags != DependencyDataset.PAD_IDX).float()
                tag_accurracy = torch.sum((tag_pred == tags).float() * mask, dim=-1)
                tag_acc += torch.sum(tag_accurracy).item()
                taggerZ += torch.sum(mask).item()

                #greedy label accurracy (without parsing)
                _, pred = lab_scores.max(dim=1)
                pred = torch.gather(pred, 1, heads.unsqueeze(1)).squeeze(1)
                mask = (heads != DependencyDataset.PAD_IDX).float()
                lab_accurracy = torch.sum((pred == labels).float() * mask, dim=-1)
                lab_acc += torch.sum(lab_accurracy).item()
                arcsZ += torch.sum(mask).item()

                
        return gloss/overall_size,tag_acc/taggerZ, arc_acc/arcsZ, lab_acc/arcsZ


    def train_model(self,train_set,dev_set,epochs,batch_size,lr,modelpath='test_model.pt'):

        print('start training',flush=True)
        loss_fnc   = nn.CrossEntropyLoss(reduction='sum')

        optimizer = torch.optim.Adam(self.parameters(), betas=(0.9, 0.9), lr = lr,eps=1e-09)
        scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma = 0.95)

        for e in range(epochs):
            TRAIN_LOSS    =  0
            TRAIN_TOKS    =  0
            BEST_ARC_ACC  =  0
            self.lexer.train_mode()
            train_batches = train_set.make_batches(batch_size,shuffle_batches=True,shuffle_data=True,order_by_length=True)
            overall_size  = 0
            for batch in train_batches:
                self.train() 
                words,mwe,chars,cats,deps,tags,heads,labels = batch
                if type(deps)==tuple:
                    depsA,depsB   = deps
                    deps          = (depsA.to(self.device),depsB.to(self.device))
                    overall_size += (depsA.size(0)*depsA.size(1)) #bc no masking at training           
                else:
                    deps = deps.to(self.device)
                    overall_size += (deps.size(0)*deps.size(1)) #bc no masking at training           
                heads, labels,tags =  heads.to(self.device), labels.to(self.device),tags.to(self.device)
                chars              =  [ token.to(self.device) for token in chars ]

                #FORWARD
                tagger_scores, arc_scores, lab_scores = self.forward(deps,chars)

                #ARC LOSS
                arc_scores = arc_scores.transpose(-1, -2)                           # [batch, sent_len, sent_len]
                arc_scores = arc_scores.contiguous().view(-1, arc_scores.size(-1))  # [batch*sent_len, sent_len]
                arc_loss   = loss_fnc(arc_scores, heads.view(-1))                   # [batch*sent_len]

                #TAGGER_LOSS
                tagger_scores = tagger_scores.contiguous().view(-1, tagger_scores.size(-1))
                tagger_loss = loss_fnc(tagger_scores, tags.view(-1))

                #LABEL LOSS
                heads      = heads.unsqueeze(1).unsqueeze(2)                        # [batch, 1, 1, sent_len]
                heads      = heads.expand(-1, lab_scores.size(1), -1, -1)           # [batch, n_labels, 1, sent_len]
                lab_scores = torch.gather(lab_scores, 2, heads).squeeze(2)          # [batch, n_labels, sent_len]
                lab_scores = lab_scores.transpose(-1, -2)                           # [batch, sent_len, n_labels]
                lab_scores = lab_scores.contiguous().view(-1, lab_scores.size(-1))  # [batch*sent_len, n_labels]
                labels     = labels.view(-1)                                        # [batch*sent_len]
                lab_loss   = loss_fnc(lab_scores, labels)

                loss       = tagger_loss + arc_loss + lab_loss
    
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                TRAIN_LOSS   += loss.item()
                
            DEV_LOSS,DEV_TAG_ACC,DEV_ARC_ACC,DEV_LAB_ACC = self.eval_model(dev_set,batch_size)
            print('Epoch ',e,'train mean loss',TRAIN_LOSS/overall_size,
                             'valid mean loss',DEV_LOSS,
                             'valid tag acc',DEV_TAG_ACC,
                             'valid arc acc',DEV_ARC_ACC,
                             'valid label acc',DEV_LAB_ACC,
                             'Base LR',scheduler.get_lr()[0],flush=True)

            if DEV_ARC_ACC > BEST_ARC_ACC:
                self.save_params(modelpath)
                BEST_ARC_ACC = DEV_ARC_ACC

            #scheduler.step()
                
        self.load_params(modelpath)
        self.save_params(modelpath)

        
    def predict_batch(self,test_set,ostream,batch_size,greedy=False):

        self.lexer.eval_mode()
        test_batches = test_set.make_batches(batch_size,shuffle_batches=False,shuffle_data=False,order_by_length=False) #keep natural order here

        with torch.no_grad():
            
            softmax = nn.Softmax(dim=1)
            for batch in test_batches:
                self.eval()
                words,mwe,chars,cats,deps,tags,heads,labels = batch
                if type(deps) == tuple:
                    depsA,depsB = deps
                    deps = (depsA.to(self.device),depsB.to(self.device))
                    SLENGTHS = (depsA != DependencyDataset.PAD_IDX).long().sum(-1)
                else:
                    deps = deps.to(self.device)
                    SLENGTHS = (deps != DependencyDataset.PAD_IDX).long().sum(-1)
                heads, labels,tags =  heads.to(self.device), labels.to(self.device),tags.to(self.device)                
                chars              =  [ token.to(self.device) for token in chars ]

                #batch prediction
                tagger_scores_batch, arc_scores_batch, lab_scores_batch = self.forward(deps,chars)
                tagger_scores_batch, arc_scores_batch, lab_scores_batch = tagger_scores_batch.cpu(),arc_scores_batch.cpu(), lab_scores_batch.cpu()

                for tokens,mwe_range,length,tagger_scores,arc_scores,lab_scores in zip(words,mwe,SLENGTHS,tagger_scores_batch,arc_scores_batch,lab_scores_batch):
                    # Predict heads 
                    probs          = arc_scores.numpy().T
                    mst_heads      = np.argmax(probs,axis=1) if greedy else chuliu_edmonds(probs)

                    # Predict tags
                    tag_probs      = tagger_scores.numpy()
                    tag_idxes      = np.argmax(tag_probs,axis=1)
                    pos_tags       = [ test_set.itotag[idx] for idx in tag_idxes ]
                    # Predict labels
                    select         = torch.LongTensor(mst_heads).unsqueeze(0).expand(lab_scores.size(0), -1)
                    select         = Variable(select)
                    selected       = torch.gather(lab_scores, 1, select.unsqueeze(1)).squeeze(1)
                    _, mst_labels  = selected.max(dim=0)
                    mst_labels     = mst_labels.data.numpy()
                    edges          = [ (head,test_set.itolab[lbl],dep) for (dep,head,lbl) in zip(list(range(length)),mst_heads[:length], mst_labels[:length]) ]
                    dg             =  DepGraph(edges[1:],wordlist=tokens[1:],pos_tags=pos_tags[1:],mwe_range=mwe_range)
                    print(dg,file=ostream)
                    print(file=ostream)



class GridSearch:
    """ This generates all the possible experiments specified by a yaml config file """
    def __init__(self,yamlparams):
        
        self.HP = yamlparams

    def generate_setup(self):
        
        setuplist = [ ]                  #init
        K         = list(self.HP.keys())
        for key in K:
            value = self.HP[key]
            if type(value) is list:
                if setuplist:
                    setuplist = [ elt+[V] for elt in setuplist for V in value]
                else:
                    setuplist = [[V] for V in value]
            else:
                for elt in setuplist:
                    elt.append(value)
        print('#%d'%(len(setuplist)),'runs to be performed')
        
        for setup in setuplist:
            yield dict(zip(K,setup))

    @staticmethod
    def generate_run_name(base_filename,dict_setup):
        return base_filename + '+' + '+'.join([ k+':'+str(v)   for (k,v) in dict_setup.items() if k != 'output_path'] ) + '.conll'


if __name__ == '__main__':

    import os.path

    def savelist(strlist, filename):
        ostream = open(filename, 'w')
        ostream.write('\n'.join(strlist))
        ostream.close()


    def loadlist(filename):
        istream = open(filename)
        strlist = [line for line in istream.read().split('\n')]
        istream.close()
        return strlist

    parser = argparse.ArgumentParser(description='Graph based Attention based dependency parser/tagger ')
    parser.add_argument('config_file', metavar='CONFIG_FILE', type=str, help='the configuration file')
    parser.add_argument('--train_file', metavar='TRAIN_FILE', type=str, help='the conll training file')
    parser.add_argument('--dev_file', metavar='DEV_FILE', type=str, help='the conll development file')
    parser.add_argument('--pred_file', metavar='PRED_FILE', type=str, help='the conll file to parse')

    args = parser.parse_args()
    hp = yaml.load(open(args.config_file).read())

    CONFIG_FILE = os.path.abspath(args.config_file)
    MODEL_DIR   = os.path.dirname(CONFIG_FILE)

    if args.train_file and args.dev_file:
        #TRAIN MODE
        traintrees  = DependencyDataset.read_conll(args.train_file)
        devtrees    = DependencyDataset.read_conll(args.dev_file)

        ordered_vocab = make_vocab(traintrees,0)

        savelist(ordered_vocab,os.path.join(MODEL_DIR,hp['lexer']+"-vocab"))

        if hp['lexer'] == 'default':
            lexer = DefaultLexer(ordered_vocab,hp['word_embedding_size'],hp['word_dropout'])
        elif hp['lexer'] == 'fasttext':
            lexer = FastTextLexer(ordered_vocab,hp['word_dropout'])
        elif hp['lexer'] == 'flaubertbase':
            lexer = BertBaseLexer(ordered_vocab,hp['word_embedding_size'],hp['word_dropout'],bert_modelfile='flaubert-base-uncased',cased=False)
        elif hp['lexer'] == 'flaubertlarge':
            lexer = BertBaseLexer(ordered_vocab,hp['word_embedding_size'],hp['word_dropout'],cased=True,bert_modelfile='flaubert-large-cased',BERT_SIZE=1024)
        elif hp['lexer'] == 'mbert':
            lexer = BertBaseLexer(ordered_vocab,hp['word_embedding_size'],hp['word_dropout'],cased=True,bert_modelfile="bert-base-multilingual-cased")
        elif hp['lexer'] == 'camembert':
            lexer = BertBaseLexer(ordered_vocab,hp['word_embedding_size'],hp['word_dropout'],cased=True,bert_modelfile="camembert-base")
        else:
            print('no valid lexer specified. abort.')
            exit(1)

        ordered_charset = CharDataSet.make_vocab(ordered_vocab)
        savelist(ordered_charset.i2c,os.path.join(MODEL_DIR,hp['lexer']+"-charcodes"))
        char_rnn        = CharRNN(len(ordered_charset), hp['char_embedding_size'], hp['word_embedding_size'])

        trainset           = DependencyDataset(traintrees,lexer,ordered_charset)
        itolab,itotag      = trainset.itolab,trainset.itotag
        savelist(itolab, os.path.join(MODEL_DIR,hp['lexer']+"-labcodes"))
        savelist(itotag, os.path.join(MODEL_DIR,hp['lexer']+"-tagcodes"))
        devset             = DependencyDataset(devtrees,lexer,ordered_charset,use_labels=itolab,use_tags=itotag)

        parser             = BiAffineParser(lexer,char_rnn,len(itotag),hp['encoder_dropout'],hp['mlp_input'],hp['mlp_tag_hidden'],hp['mlp_arc_hidden'],hp['mlp_lab_hidden'],hp['mlp_dropout'],len(itolab),hp['device'])
        parser.train_model(trainset,devset,hp['epochs'],hp['batch_size'],hp['lr'],modelpath=os.path.join(MODEL_DIR,hp['lexer']+"-model.pt"))
        print('training done.',file=sys.stderr)

    if args.pred_file:
        #TEST MODE
        testtrees     = DependencyDataset.read_conll(args.pred_file)
        ordered_vocab = loadlist(os.path.join(MODEL_DIR,hp['lexer']+"-vocab"))

        if hp['lexer']   == 'default':
            lexer = DefaultLexer(ordered_vocab, hp['word_embedding_size'], hp['word_dropout'])
        elif hp['lexer'] == 'fasttext':
            lexer = FastTextLexer(ordered_vocab, hp['word_dropout'])
        elif hp['lexer'] == 'flaubertbase':
            lexer = BertBaseLexer(ordered_vocab, hp['word_embedding_size'], hp['word_dropout'],bert_modelfile='flaubert-base-uncased', cased=False)
        elif hp['lexer'] == 'flaubertlarge':
            lexer = BertBaseLexer(ordered_vocab, hp['word_embedding_size'], hp['word_dropout'], cased=True,bert_modelfile='flaubert-large-cased', BERT_SIZE=1024)
        elif hp['lexer'] == 'mbert':
            lexer = BertBaseLexer(ordered_vocab, hp['word_embedding_size'], hp['word_dropout'], cased=True,bert_modelfile="bert-base-multilingual-cased")
        elif hp['lexer'] == 'camembert':
            lexer = BertBaseLexer(ordered_vocab, hp['word_embedding_size'], hp['word_dropout'], cased=True,bert_modelfile="camembert-base")
        else:
            print('no valid lexer specified. abort.')
            exit(1)

        ordered_charset =  CharDataSet(loadlist(os.path.join(MODEL_DIR,hp['lexer']+"-charcodes")))
        char_rnn        = CharRNN(len(ordered_charset), hp['char_embedding_size'], hp['word_embedding_size'])

        itolab  = loadlist(os.path.join(MODEL_DIR,hp['lexer']+"-labcodes"))
        itotag  = loadlist(os.path.join(MODEL_DIR,hp['lexer']+"-tagcodes"))
        testset = DependencyDataset(testtrees,lexer,ordered_charset,use_labels=itolab,use_tags=itotag)
        parser  = BiAffineParser(lexer,char_rnn,len(itotag),hp['encoder_dropout'],hp['mlp_input'],hp['mlp_tag_hidden'],hp['mlp_arc_hidden'],hp['mlp_lab_hidden'],hp['mlp_dropout'],len(itolab),hp['device'])
        parser.load_params(os.path.join(MODEL_DIR,hp['lexer']+"-model.pt"))
        ostream = open(args.pred_file+'.parsed','w')
        parser.predict_batch(testset,ostream,hp['batch_size'],greedy=False)
        ostream.close()
        print('parsing done.',file=sys.stderr)

