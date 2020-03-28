import csv
import sys
from collections import Counter
from functools import partial

import numpy as np
import pandas
import torch
from typing import Dict, List, Any
from allennlp.data.dataloader import DataLoader, allennlp_collate
from allennlp.data.samplers import BucketBatchSampler
from allennlp.data.vocabulary import Vocabulary
from allennlp.modules.text_field_embedders import TextFieldEmbedder
from allennlpx.training.adv_trainer import AdvTrainer, EpochCallback, BatchCallback
from allennlp.training.util import evaluate
from nltk.corpus import stopwords
from tqdm import tqdm

from allennlpx import allenutil
from allennlpx.data.dataset_readers.berty_tsv import BertyTSVReader
from allennlpx.data.dataset_readers.spacy_tsv import SpacyTSVReader
from allennlpx.interpret.attackers.attacker import DEFAULT_IGNORE_TOKENS
from allennlpx.interpret.attackers.bruteforce import BruteForce
from allennlpx.interpret.attackers.genetic import Genetic
from allennlpx.interpret.attackers.hotflip import HotFlip
from allennlpx.interpret.attackers.pgd import PGD
from allennlpx.interpret.attackers.policies import (CandidatePolicy,
                                                    EmbeddingPolicy,
                                                    SpecifiedPolicy,
                                                    SynonymPolicy,
                                                    UnconstrainedPolicy)
from allennlpx.interpret.attackers.pwws import PWWS
from allennlpx.modules.knn_utils import H5pyCollector, build_faiss_index
from allennlpx.modules.token_embedders.embedding import \
    _read_pretrained_embeddings_file
from allennlpx.modules.token_embedders.graph_funcs import MeanAggregator, PoolingAggregator
from allennlpx.predictors.text_classifier import TextClassifierPredictor
from awesome_glue.config import Config
from awesome_glue.vanilla_classifier import Classifier
from awesome_glue import embed_util
from awesome_glue.bert_classifier import BertClassifier
from awesome_glue.task_specs import TASK_SPECS
from awesome_glue.transforms import (BackTrans, DAE, BertAug, Crop, EmbedAug,
                                     Identity, RandDrop, SynAug,
                                     transform_collate,
                                     parse_transform_fn_from_args)
from awesome_glue.utils import (EMBED_DIM, WORD2VECS, AttackMetric, FreqUtil,
                                set_environments, text_diff, get_neighbours,
                                AnnealingTemperature)
from luna import flt2str, ram_write, ram_read
from luna.logging import log
from luna.public import Aggregator, auto_create
from luna.pytorch import set_seed
from allennlp.training.metrics.categorical_accuracy import CategoricalAccuracy
from allennlpx.training import adv_utils
from allennlpx.interpret.attackers.embedding_searcher import EmbeddingSearcher
import logging
from awesome_glue.data_loader import load_data, load_banned_words

logger = logging.getLogger(__name__)

set_environments()


class Task:
    def __init__(self, config: Config):
        super().__init__()
        self.config = config

        loaded_data = load_data(config.task_id, config.tokenizer)
        self.reader = loaded_data['reader']
        self.train_data, self.dev_data, self.test_data = loaded_data['data']
        self.vocab: Vocabulary = loaded_data['vocab']

        # Build the model
        # embed_args = {
        #     "vocab": self.vocab,
        #     "pretrain": config.pretrain,
        #     "cache_embed_path": f"{config.task_id}-{config.pretrain}.vec"
        # }
        # if config.arch in ['lstm', 'cnn', 'boe']:
        #     token_embedder = embed_util.build_embedding(**embed_args)
        # elif config.arch in ['dlstm', 'dcnn', 'dboe']:
        #     _, spacy_vec = self.get_spacy_vocab_and_vec()
        #     neighbours, nbr_mask = get_neighbours(spacy_vec,
        #                                           return_edges=False)
        #     token_embedder = embed_util.build_dirichlet_embedding(
        #         **embed_args,
        #         temperature=config.dir_temp,
        #         neighbours=neighbours.cuda(),
        #         nbr_mask=nbr_mask.cuda())
        # elif config.arch in ['glstm', 'gcnn', 'gboe']:
        #     _, spacy_vec = self.get_spacy_vocab_and_vec()
        #     edges = get_neighbours(spacy_vec, return_edges=True)
        #     token_embedder = embed_util.build_graph_embedding(
        #         **embed_args,
        #         gnn={
        #             "mean": MeanAggregator(300),
        #             "pool": PoolingAggregator(300)
        #         }[config.gnn_type],
        #         edges=edges.cuda(),
        #         hop=config.gnn_hop)
        # self.model = Classifier(
        #     vocab=self.vocab,
        #     token_embedder=token_embedder,
        #     arch=config.arch,
        #     num_labels=TASK_SPECS[config.task_id]['num_labels'])
        self.model = BertClassifier(
            self.vocab
        )
        self.model.cuda()

        # The predictor is a wrapper of the model.
        # It is slightly different from the predictor provided by AllenNLP.
        # With the predictor, we can do some tricky things before/after feeding
        # instances into a model, such as:
        # - do some input transformation (random drop, word augmentation, etc.)
        # - ensemble several models
        # Note that the attacker receives a predictor as the proxy of the model,
        # which allows for test-time attacks.
        self.predictor = TextClassifierPredictor(
            self.model,
            self.reader,
            key='sent' if self.config.arch != 'bert' else 'berty_tokens')

        # list[str] -> list[str]
        transform_fn = parse_transform_fn_from_args(
            self.config.pred_transform, self.config.pred_transform_args)
        # list[instance] -> list[instance]
        transform_fn = partial(self.reader.transform_instances, transform_fn)

        self.predictor.set_ensemble_num(self.config.pred_ensemble)
        self.predictor.set_transform_fn(transform_fn)

    def train(self):
        num_epochs = 16
        pseudo_batch_size = 32

        # Maybe we will do some data augmentation here.
        if self.config.aug_data != '':
            log(f'Augment data from {self.config.aug_data}')
            aug_data = auto_create(
                f"{self.config.task_id}.{self.config.arch}.aug",
                lambda: self.reader.read(self.config.aug_data),
                cache=True)
            self.train_data.instances.extend(aug_data.instances)

        # Set up the adversarial training policy
        if self.config.adv_constraint:
            # VERY IMPORTANT!
            # we use the spacy_weight here since during attacks we use an external weight.
            # but it is also possible to use the model's internal weight.
            # one thing is important: the weight must be corresponding to the vocab!
            _, spacy_weight = self.get_spacy_vocab_and_vec()
            searcher = EmbeddingSearcher(
                embed=spacy_weight,
                idx2word=self.vocab.get_token_from_index,
                word2idx=self.vocab.get_token_index)
            searcher.pre_search('euc', 10)
        else:
            searcher = None
        policy_args = {
            "adv_iteration": self.config.adv_iter,
            "replace_num": self.config.adv_replace_num,
            "searcher": searcher,
        }
        if self.config.adv_policy == 'hot':
            adv_policy = adv_utils.HotFlipPolicy(**policy_args)
        elif self.config.adv_policy == 'rad':
            adv_policy = adv_utils.RandomNeighbourPolicy(**policy_args)
        else:
            adv_policy = None

        # Set callbacks
        epoch_callbacks = []
        if self.config.arch in ['dlstm', 'dcnn', 'dboe']:
            epoch_callbacks.append(AnnealingTemperature())

        # A collate_fn will do some transformation an instance before
        # fed into a model. If we want to train a model with some transformations
        # such as cropping/DAE, we can modify code here. e.g.,
        # collate_fn = partial(transform_collate, self.vocab, self.reader, Crop(0.3))
        collate_fn = allennlp_collate
        trainer = AdvTrainer(
            model=self.model,
            optimizer=self.model.get_optimizer(),
            validation_metric='+accuracy',
            adv_policy=adv_policy,
            data_loader=DataLoader(
                self.train_data,
                batch_sampler=BucketBatchSampler(
                    data_source=self.train_data,
                    batch_size=pseudo_batch_size,
                ),
                collate_fn=collate_fn,
            ),
            validation_data_loader=DataLoader(
                self.dev_data,
                batch_size=pseudo_batch_size,
            ),
            num_epochs=num_epochs,
            patience=None,
            grad_clipping=1.,
            cuda_device=0,
            epoch_callbacks=epoch_callbacks,
            batch_callbacks=[],
            serialization_dir=f'saved/models/{self.config.model_name}'
            if self.config.model_name != 'off' else None,
            num_serialized_models_to_keep=3)
        trainer.train()

    def from_pretrained(self):
        model_path = f'saved/models/{self.config.model_name}/best.th'
        print(f'Load model from {model_path}')
        self.model.load_state_dict(torch.load(model_path))
        self.model.eval()

    @torch.no_grad()
    def evaluate_model(self):
        self.from_pretrained()
        print(evaluate(self.model, DataLoader(self.dev_data, 32), 0, None))

    @torch.no_grad()
    def evaluate_predictor(self):
        self.from_pretrained()
        metric = CategoricalAccuracy()
        batch_size = 32
        total_size = len(self.dev_data)
        for bid in tqdm(range(0, total_size, batch_size)):
            instances = [
                self.dev_data[i]
                for i in range(bid, min(bid + batch_size, total_size))
            ]
            outputs = self.predictor.predict_batch_instance(instances)
            preds, labels = [], []
            for inst, outp in zip(instances, outputs):
                preds.append([outp['probs']])
                labels.append([inst.fields['label'].label])
                metric(predictions=torch.tensor(preds),
                       gold_labels=torch.tensor(labels))
        print(metric.get_metric())

    @torch.no_grad()
    def transfer_attack(self):
        self.from_pretrained()
        set_seed(11221)
        df = pandas.read_csv(self.config.adv_data,
                             sep='\t',
                             quoting=csv.QUOTE_NONE)
        attack_metric = AttackMetric()

        for rid in tqdm(range(df.shape[0])):
            raw = df.iloc[rid]['raw']
            adv = df.iloc[rid]['adv']

            results = self.predictor.predict_batch_instance([
                self.reader.text_to_instance(raw),
                self.reader.text_to_instance(adv)
            ])

            raw_pred = np.argmax(results[0]['probs'])
            adv_pred = np.argmax(results[1]['probs'])

            label = df.iloc[rid]['label']

            if raw_pred == label:
                if adv_pred != raw_pred:
                    attack_metric.succeed()
                else:
                    attack_metric.fail()
            else:
                attack_metric.escape()
            print('Agg metric', attack_metric)
        print(Counter(df["label"].tolist()))
        print(attack_metric)

    def attack(self):
        self.from_pretrained()

        spacy_vocab, spacy_weight = self.get_spacy_vocab_and_vec()

        if self.config.attack_gen_adv:
            f_adv = open(
                f"nogit/{self.config.model_name}.{self.config.attack_method}.adv.tsv",
                'w')
            f_adv.write("raw\tadv\tlabel\n")

        for module in self.model.modules():
            if isinstance(module, TextFieldEmbedder):
                for embed in module._token_embedders.keys():
                    module._token_embedders[embed].weight.requires_grad = True

        forbidden_words = load_banned_words(self.config.task_id)

        forbidden_words += stopwords.words("english")
        #         STOP_WORDS = stopwords.words("english")
        #         for ele in ['nor', 'above']:
        #             STOP_WORDS.remove(ele)
        #         for ele in STOP_WORDS:
        #             if "'" in ele:
        #                 STOP_WORDS.remove(ele)
        #         FreqUtil.topk_frequency(self.vocab, 100, 'least', forbidden_words)
        general_kwargs = {
            "ignore_tokens": forbidden_words,
            "forbidden_tokens": forbidden_words,
            "max_change_num_or_ratio": 0.15
        }
        blackbox_kwargs = {
            "vocab": spacy_vocab,
            "token_embedding": spacy_weight
        }
        if self.config.attack_method == 'pgd':
            attacker = PGD(self.predictor,
                           step_size=100.,
                           max_step=20,
                           iter_change_num=1,
                           **general_kwargs)
        elif self.config.attack_method == 'hotflip':
            attacker = HotFlip(
                self.predictor,
                policy=EmbeddingPolicy(measure='euc', topk=10, rho=None),
                #                                policy=UnconstrainedPolicy(),
                **general_kwargs)
        elif self.config.attack_method == 'bruteforce':
            attacker = BruteForce(self.predictor,
                                  policy=EmbeddingPolicy(measure='euc',
                                                         topk=10,
                                                         rho=None),
                                  **general_kwargs,
                                  **blackbox_kwargs)
        elif self.config.attack_method == 'pwws':
            attacker = PWWS(
                self.predictor,
                #                 policy=SpecifiedPolicy(words=STOP_WORDS),
                policy=EmbeddingPolicy(measure='euc', topk=10, rho=None),
                #                             policy=SynonymPolicy(),
                **general_kwargs,
                **blackbox_kwargs)
        elif self.config.attack_method == 'genetic':
            attacker = Genetic(self.predictor,
                               num_generation=10,
                               num_population=20,
                               policy=EmbeddingPolicy(measure='euc',
                                                      topk=10,
                                                      rho=None),
                               lm_topk=4,
                               **general_kwargs,
                               **blackbox_kwargs)
        else:
            raise Exception()

        # For SST, we do not attack all sentences in train set.
        # We attack sentences whose length is larger than 20 instead.
        if self.config.attack_data_split == 'train':
            data_to_attack = self.train_data
            if self.config.task_id == 'SST':
                if self.config.arch == 'bert':
                    field_name = 'berty_tokens'
                else:
                    field_name = 'sent'
                data_to_attack = list(
                    filter(lambda x: len(x[field_name].tokens) > 20,
                           data_to_attack))
        elif self.config.attack_data_split == 'dev':
            data_to_attack = self.dev_data

        if self.config.arch == 'bert':
            field_to_change = 'berty_tokens'
        else:
            field_to_change = 'sent'
        data_to_attack = list(
            filter(lambda x: len(x[field_to_change].tokens) < 300,
                   data_to_attack))

        if self.config.attack_size == -1:
            adv_number = len(data_to_attack)
        else:
            adv_number = self.config.attack_size
        data_to_attack = data_to_attack[:adv_number]

        strict_metric = AttackMetric()
        loose_metric = AttackMetric()
        agg = Aggregator()
        raw_counter = Counter()
        adv_counter = Counter()
        for i in tqdm(range(adv_number)):
            raw_text = allenutil.as_sentence(data_to_attack[i])
            adv_text = None

            raw_probs = self.predictor.predict_instance(
                data_to_attack[i])['probs']
            raw_pred = np.argmax(raw_probs)
            raw_label = data_to_attack[i]['label'].label
            # Only attack correct instance
            if raw_pred == raw_label:
                result = attacker.attack_from_json(
                    {field_to_change: raw_text},
                    field_to_change=field_to_change)
                adv_text = allenutil.as_sentence(result['adv'])
                adv_probs = self.predictor.predict(adv_text)['probs']
                adv_pred = np.argmax(adv_probs)

                if result['success']:
                    strict_metric.succeed()
                else:
                    strict_metric.fail()

                # yapf:disable
                if raw_text != adv_text and raw_pred != adv_pred:
                    loose_metric.succeed()
                    diff = text_diff(result['raw'], result['adv'])
                    raw_counter.update(diff['a_changes'])
                    adv_counter.update(diff['b_changes'])
                    to_aggregate = [('change_num', diff['change_num']),
                                    ('change_ratio', diff['change_ratio'])]
                    if "generation" in result:
                        to_aggregate.append(
                            ('generation', result['generation']))
                    agg.aggregate(*to_aggregate)
                    adv_text = allenutil.as_sentence(result['adv'])
                    log("[raw]", raw_text, "\n[prob]", flt2str(raw_probs, cat=', '))
                    log("[adv]", adv_text, '\n[prob]', flt2str(adv_probs, cat=', '))
                    if "changed" in result:
                        log("[changed]", result['changed'])
                    log()

                    log("Avg.change#", round(agg.mean("change_num"), 2),
                        "Avg.change%", round(100 * agg.mean("change_ratio"), 2))
                    if "generation" in result:
                        log("Aggregated generation", agg.mean("generation"))
                    log(f"Aggregated metric: [loose] {loose_metric} [strict] {strict_metric}")
                else:
                    loose_metric.fail()
                # yapf:enable
            else:
                loose_metric.escape()
                strict_metric.escape()

            if adv_text is None:
                adv_text = raw_text

            if self.config.attack_gen_adv:
                f_adv.write(f"{raw_text}\t{adv_text}\t{raw_label}\n")
            sys.stdout.flush()

        if self.config.attack_gen_adv:
            f_adv.close()

        # yapf:disable
        print("Statistics of changed words:")
        print(">> [raw] ", raw_counter.most_common())
        print(">> [adv] ", adv_counter.most_common())
        print("Overall:")
        print("Avg.change#:", round(agg.mean("change_num"), 2) if agg.has_key("change_num") else '-',
              "Avg.change%:", round(100 * agg.mean("change_ratio"), 2) if agg.has_key("change_ratio") else '-',
              "[loose] Accu.before%:", round(loose_metric.accuracy_before_attack, 2),
              "Accu.after%:", round(loose_metric.accuracy_after_attack, 2),
              "[strict] Accu.before%:", round(strict_metric.accuracy_before_attack, 2),
              "Accu.after%:", round(strict_metric.accuracy_after_attack, 2))
        # yapf:enable

    def get_spacy_vocab_and_vec(self):
        if self.config.tokenizer != 'spacy':
            spacy_data = load_data(self.config.task_id, "spacy")
            spacy_vocab: Vocabulary = spacy_data['vocab']
        else:
            spacy_vocab = self.vocab
        spacy_weight = embed_util.read_weight(
            spacy_vocab, self.config.attack_vectors,
            f"{self.config.task_id}-{self.config.attack_vectors}.vec")
        return spacy_vocab, spacy_weight


#     def knn_build_index(self):
#         self.from_pretrained()
#         iterator = BasicIterator(batch_size=32)
#         iterator.index_with(self.vocab)

#         ram_write("knn_flag", "collect")
#         filtered = list(filter(lambda x: len(x.fields['berty_tokens'].tokens) > 10,
#                                self.train_data))
#         evaluate(self.model, filtered, iterator, 0, None)

#     def knn_evaluate(self):
#         ram_write("knn_flag", "infer")
#         self.evaluate()

#     def knn_attack(self):
#         ram_write("knn_flag", "infer")
#         self.attack()

#     def build_manifold(self):
#         spacy_data = load_data(self.config.task_id, "spacy")
#         train_data, dev_data, _ = spacy_data['data']
#         if self.config.task_id == 'SST':
#             train_data = list(filter(lambda x: len(x["sent"].tokens) > 15, train_data))
#         spacy_vocab: Vocabulary = spacy_data['vocab']

#         embedder = SentenceTransformer('bert-base-nli-stsb-mean-tokens')

#         collector = H5pyCollector(f'{self.config.task_id}.train.h5py', 768)

#         batch_size = 32
#         total_size = len(train_data)
#         for i in range(0, total_size, batch_size):
#             sents = []
#             for j in range(i, min(i + batch_size, total_size)):
#                 sents.append(allenutil.as_sentence(train_data[j]))
#             collector.collect(np.array(embedder.encode(sents)))
#         collector.close()

#     def test_distance(self):
#         embedder = SentenceTransformer('bert-base-nli-stsb-mean-tokens')
#         index = build_faiss_index(f'{self.config.task_id}.train.h5py')

#         df = pandas.read_csv(self.config.adv_data, sep='\t', quoting=csv.QUOTE_NONE)
#         agg_D = []
#         for rid in tqdm(range(df.shape[0])):
#             raw = df.iloc[rid]['raw']
#             adv = df.iloc[rid]['adv']
#             if raw != adv:
#                 sent_embed = embedder.encode([raw, adv])
#                 D, _ = index.search(np.array(sent_embed), 3)
#                 agg_D.append(D.mean(axis=1))
#         agg_D = np.array(agg_D)
#         print(agg_D.mean(axis=0), agg_D.std(axis=0))
#         print(sum(agg_D[:, 0] < agg_D[:, 1]), 'of', agg_D.shape[0])

#     def test_ppl(self):
#         en_lm = torch.hub.load('pytorch/fairseq',
#                                'transformer_lm.wmt19.en',
#                                tokenizer='moses',
#                                bpe='fastbpe')
#         en_lm.eval()
#         en_lm.cuda()

#         df = pandas.read_csv(self.config.adv_data, sep='\t', quoting=csv.QUOTE_NONE)
#         agg_ppls = []
#         for rid in tqdm(range(df.shape[0])):
#             raw = df.iloc[rid]['raw']
#             adv = df.iloc[rid]['adv']
#             if raw != adv:
#                 scores = en_lm.score([raw, adv])
#                 ppls = np.array(
#                     [ele['positional_scores'].mean().neg().exp().item() for ele in scores])
#                 agg_ppls.append(ppls)
#         agg_ppls = np.array(agg_ppls)
#         print(agg_ppls.mean(axis=0), agg_ppls.std(axis=0))
#         print(sum(agg_ppls[:, 0] < agg_ppls[:, 1]), 'of', agg_ppls.shape[0])
