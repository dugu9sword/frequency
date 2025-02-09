import csv
import sys
from collections import Counter
from functools import partial
import json

import numpy as np
import pandas
import torch
from allennlp.data.dataloader import PyTorchDataLoader as DataLoader
from allennlp.data.dataloader import allennlp_collate
from allennlp.data.samplers import BucketBatchSampler
from allennlp.data.vocabulary import Vocabulary
from allennlpx.training.adv_trainer import AdvTrainer
from allennlp.training.util import evaluate
from tqdm import tqdm
import pathlib
import shutil

from allennlpx import allenutil
from allennlpx.interpret.attackers import BruteForce, Genetic, HotFlip, PWWS
from allennlpx.interpret.attackers.searchers import EmbeddingSearcher, CachedWordSearcher, EmbeddingNbrUtil, WordIndexSearcher
from allennlpx.predictors import TextClassifierPredictor, BiTextClassifierPredictor
from awesome_glue.config import Config
from awesome_glue.vanilla_classifier import Classifier
from awesome_glue.esim import ESIM
from awesome_glue import embed_util
from awesome_glue.bert_classifier import BertClassifier
from awesome_glue.task_specs import TASK_SPECS, is_sentence_pair
from awesome_glue.transforms import (transform_collate,
                                     parse_transform_fn_from_args)
from awesome_glue.utils import (AttackMetric, set_environments, WarmupCallback,
                                text_diff, read_hyper,
                                allen_instances_for_attack)
from luna import flt2str, ram_write, ram_set_flag, ram_reset_flag
from luna.logging import log
from luna.public import Aggregator, auto_create, numpy_seed
from luna.pytorch import set_seed
from allennlp.training.metrics.categorical_accuracy import CategoricalAccuracy
from allennlpx.training.checkpointer import CheckpointerX
from allennlpx.training import adv_utils
import logging
from awesome_glue.data_loader import load_banned_words, load_data
from awesome_glue.weighted_util import WeightedHull, SameAlphaHull, DecayAlphaHull
from awesome_glue.biboe import BiBOE
from awesome_glue.decom_att import DecomposableAttention
from awesome_glue.weighted_embedding import WeightedEmbedding
from typing import Dict, Any
from allennlp.training.learning_rate_schedulers.slanted_triangular import SlantedTriangular

logging.getLogger('transformers').setLevel(logging.CRITICAL)

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

        if self.config.arch == "bert":
            bert_vocab = embed_util.get_bert_vocab()

        # Build the model
        embed_args = {
            "vocab": self.vocab,
            "pretrain": config.pretrain,
            "finetune": config.finetune,
            "cache_embed_path": f"{config.task_id}-{config.pretrain}.vec"
        }
        # import pdb; pdb.set_trace()

        if not config.weighted_embed:
            if self.config.arch != "bert":
                token_embedder = embed_util.build_embedding(**embed_args)
        else:
            hull_args = {
                "alpha": config.dir_alpha,
                "nbr_file": "external_data/ibp-nbrs.json" if not config.big_nbrs else "external_data/euc-top8.json",
                "vocab": self.vocab if self.config.arch != 'bert' else bert_vocab,
                "nbr_num": config.nbr_num,
                "second_order": config.second_order
            }
            if config.second_order and 0.0 < config.dir_decay <= 1.0:
                hull = DecayAlphaHull.build(
                    **hull_args,
                    decay=config.dir_decay,
                )
            else:
                hull = SameAlphaHull.build(**hull_args)
            print(
                f'Using {type(hull)} with second_order={config.second_order}')

            if self.config.arch != "bert":
                token_embedder = embed_util.build_weighted_embedding(
                    **embed_args, hull=hull)

        if config.arch != 'bert':
            arch_args = {
                "vocab": self.vocab,
                "token_embedder": token_embedder,
                "num_labels": TASK_SPECS[config.task_id]['num_labels']
            }
            if config.arch in ['boe', 'cnn', 'lstm']:
                self.model = Classifier(arch=config.arch,
                                        pool=config.pool,
                                        **arch_args)
            elif config.arch == 'biboe':
                self.model = BiBOE(**arch_args, pool=config.pool)
            elif config.arch == 'datt':
                self.model = DecomposableAttention(**arch_args)
            elif config.arch == 'esim':
                self.model = ESIM(**arch_args)
        else:
            self.model = BertClassifier(
                bert_vocab,
                num_labels=TASK_SPECS[config.task_id]['num_labels'])
            if config.weighted_embed:
                bert_embeddings = self.model.bert_embedder.transformer_model.embeddings
                bert_embeddings.word_embeddings = WeightedEmbedding(
                    num_embeddings=bert_vocab.get_vocab_size('tokens'),
                    embedding_dim=768,
                    weight=bert_embeddings.word_embeddings.weight,
                    hull=hull,
                    sparse=False,
                    trainable=True)
        self.model.cuda()

        # The predictor is a wrapper of the model.
        # It is slightly different from the predictor provided by AllenNLP.
        # With the predictor, we can do some tricky things before/after feeding
        # instances into a model, such as:
        # - do some input transformation (random drop, word augmentation, etc.)
        # - ensemble several models
        # Note that the attacker receives a predictor as the proxy of the model,
        # which allows for test-time attacks.
        if is_sentence_pair(self.config.task_id):
            self.predictor = BiTextClassifierPredictor(self.model,
                                                       self.reader,
                                                       key1='sent1',
                                                       key2='sent2')
        else:
            self.predictor = TextClassifierPredictor(self.model,
                                                     self.reader,
                                                     key='sent')

        # list[str] -> list[str]
        transform_fn = parse_transform_fn_from_args(
            self.config.pred_transform, self.config.pred_transform_args)

        self.predictor.set_ensemble_num(self.config.pred_ensemble)
        if self.config.hard_prob:
            self.predictor.set_ensemble_p(-1)
        else:
            self.predictor.set_ensemble_p(3)
        self.predictor.set_transform_fn(transform_fn)
        if is_sentence_pair(self.config.task_id):
            self.predictor.set_transform_field("sent2")
        else:
            self.predictor.set_transform_field("sent")

        if self.config.task_id == 'SST':
            self.train_data = self.dev_data
            self.config.attack_data_split = 'dev'
        if self.config.weighted_off:
            ram_set_flag("weighted_off")

    def train(self):
        if self.config.adjust_point:
            ram_set_flag("adjust_point")
        # ram_write('dist_reg', self.config.dist_reg)
        read_hyper_ = partial(read_hyper, self.config.task_id,
                              self.config.arch)
        num_epochs = int(read_hyper_("num_epochs"))
        batch_size = int(read_hyper_("batch_size"))
        logger.info(f"num_epochs: {num_epochs}, batch_size: {batch_size}")

        if self.config.model_name == 'tmp':
            p = pathlib.Path('saved/models/tmp')
            if p.exists():
                shutil.rmtree(p)

        # Maybe we will do some data augmentation here.
        if self.config.aug_data != '':
            log(f'Augment data from {self.config.aug_data}')
            aug_data = auto_create(
                f"{self.config.task_id}.{self.config.arch}.aug",
                lambda: self.reader.read(self.config.aug_data),
                cache=True)
            self.train_data.instances.extend(aug_data.instances)

        # Set up the adversarial training policy
        if self.config.arch == 'bert':
            model_vocab = embed_util.get_bert_vocab()
        else:
            model_vocab = self.vocab
        # yapf: disable
        adv_field = 'sent2' if is_sentence_pair(self.config.task_id) and self.config.arch != 'bert' else 'sent'
        policy_args = {
            "adv_iteration": self.config.adv_iter,
            "replace_num": self.config.adv_replace_num,
            "searcher": WordIndexSearcher(
                CachedWordSearcher(
                    "external_data/ibp-nbrs.json" if not self.config.big_nbrs else "external_data/euc-top8.json",
                    model_vocab.get_token_to_index_vocabulary("tokens"),
                    second_order=False
                ),
                word2idx=model_vocab.get_token_index,
                idx2word=model_vocab.get_token_from_index,
            ),
            'adv_field': adv_field
        }
        # yapf: enable
        if self.config.adv_policy == 'hot':
            if is_sentence_pair(
                    self.config.task_id) and self.config.arch != 'bert':
                policy_args['forward_order'] = 1
            adv_policy = adv_utils.HotFlipPolicy(**policy_args)
        elif self.config.adv_policy == 'rdm':
            adv_policy = adv_utils.RandomNeighbourPolicy(**policy_args)
        elif self.config.adv_policy == 'diy':
            adv_policy = adv_utils.DoItYourselfPolicy(self.config.adv_iter,
                                                      adv_field,
                                                      self.config.adv_step)
        else:
            adv_policy = adv_utils.NoPolicy

        # A collate_fn will do some transformation an instance before
        # fed into a model. If we want to train a model with some transformations
        # such as cropping/DAE, we can modify code here. e.g.,
        # collate_fn = partial(transform_collate, self.vocab, self.reader, Crop(0.3))
        collate_fn = allennlp_collate
        train_data_sampler = BucketBatchSampler(
            data_source=self.train_data,
            batch_size=batch_size,
        )
        # Set callbacks

        if self.config.task_id == 'SNLI' and self.config.arch != 'bert':
            epoch_callbacks = []
            if self.config.model_pretrain != "":
                epoch_callbacks = [WarmupCallback(2)]
                if self.config.model_pretrain == 'auto':
                    self.config.model_pretrain = {
                        "biboe": "SNLI-fix-biboe-sum",
                        "datt": "SNLI-fix-datt"
                    }[self.config.arch]
                logger.warning(
                    f"Try loading weights from pretrained model {self.config.model_pretrain}"
                )
                pretrain_ckpter = CheckpointerX(
                    f"saved/models/{self.config.model_pretrain}")
                self.model.load_state_dict(pretrain_ckpter.best_model_state())
        else:
            epoch_callbacks = []
        # epoch_callbacks = []
        batch_callbacks = []

        opt = self.model.get_optimizer()
        if self.config.arch == 'bert':
            scl = SlantedTriangular(opt, num_epochs,
                                    len(self.train_data) // batch_size)
        else:
            scl = None

        trainer = AdvTrainer(
            model=self.model,
            optimizer=opt,
            learning_rate_scheduler=scl,
            validation_metric='+accuracy',
            adv_policy=adv_policy,
            data_loader=DataLoader(
                self.train_data,
                batch_sampler=train_data_sampler,
                collate_fn=collate_fn,
            ),
            validation_data_loader=DataLoader(
                self.dev_data,
                batch_size=batch_size,
            ),
            num_epochs=num_epochs,
            patience=None,
            grad_clipping=1.,
            cuda_device=0,
            epoch_callbacks=epoch_callbacks,
            batch_callbacks=batch_callbacks,
            serialization_dir=f'saved/models/{self.config.model_name}',
            num_serialized_models_to_keep=20)
        trainer.train()

    def from_pretrained(self):
        ckpt_path = f'saved/models/{self.config.model_name}'
        if self.config.load_ckpt < 0:
            ckpter = CheckpointerX(ckpt_path)
            # latest_epoch = 3 if self.config.arch == 'bert' else 10
            latest_epoch = -self.config.load_ckpt
            model_path, _, load_epoch = ckpter.find_latest_best_checkpoint(
                latest_epoch, 'validation_accuracy')
        else:
            model_path = f'{ckpt_path}/model_state_epoch_{self.config.load_ckpt}.th'
            load_epoch = self.config.load_ckpt
        print(f'Load model from {model_path}')
        metric_path = f'{ckpt_path}/metrics_epoch_{load_epoch}.json'
        metric = json.load(open(metric_path))
        metric = list(filter(lambda item: "accu" in item[0], metric.items()))
        print(f'The metric at epoch {load_epoch} is: {metric}')
        sys.stdout.flush()
        self.model.load_state_dict(torch.load(model_path))
        self.model.eval()

    @torch.no_grad()
    def evaluate_model(self):
        self.from_pretrained()
        print(evaluate(self.model, DataLoader(self.dev_data, 32), 0, None))

    @torch.no_grad()
    def evaluate_predictor(self):
        self.from_pretrained()

        eval_data = self.downsample()
        metric = CategoricalAccuracy()
        batch_size = 32

        bar = tqdm(range(0, len(eval_data), batch_size))
        for bid in bar:
            instances = [
                eval_data[i]
                for i in range(bid, min(bid + batch_size, len(eval_data)))
            ]
            outputs = self.predictor.predict_batch_instance(instances)
            preds, labels = [], []
            for inst, outp in zip(instances, outputs):
                preds.append([outp['probs']])
                label_idx = inst.fields['label'].label
                if isinstance(inst.fields['label'].label, str):
                    label_idx = self.vocab.get_token_index(label_idx, 'labels')
                labels.append([label_idx])
            metric(predictions=torch.tensor(preds),
                   gold_labels=torch.tensor(labels))
            bar.set_description("{:5.2f}".format(metric.get_metric()))
        print(f"Evaluate on {self.config.data_split}, the result is ",
              metric.get_metric())

    def downsample(self, tokenizer=None):
        # Set up the data to attack
        if tokenizer is None:
            data_down = {
                "train": self.train_data,
                "dev": self.dev_data,
                "test": self.test_data
            }[self.config.data_split]
        else:
            train_data, dev_data, test_data = load_data(
                self.config.task_id, tokenizer)['data']
            data_down = {
                "train": train_data,
                "dev": dev_data,
                "test": test_data
            }[self.config.data_split]

        if 'sent' in data_down[0].fields:
            main_field = 'sent'
        else:
            main_field = 'sent2'
        data_down = list(
            filter(lambda x: len(x[main_field].tokens) < 300, data_down))

        if self.config.data_random:
            with numpy_seed(19491001):
                idxes = np.random.permutation(len(data_down))
                data_down = [data_down[i] for i in idxes]

        if self.config.data_downsample != -1:
            # self.config.data_downsample = 1000
            start = self.config.data_shard * self.config.data_downsample
            end = start + self.config.data_downsample
            data_down = data_down[start:end]

        print(
            f'Downsample {self.config.data_downsample} samples at shard {self.config.data_shard} on {self.config.data_split} set'
        )
        return data_down

    def attack(self):
        print('Firstly, evaluate the model:')
        self.evaluate_predictor()
        #         self.from_pretrained()

        data_to_attack = self.downsample(tokenizer='spacy')
        if is_sentence_pair(self.config.task_id):
            field_to_change = 'sent2'
        else:
            field_to_change = 'sent'

        # Speed up the predictor
        if self.config.arch != 'bert':
            self.predictor.set_max_tokens(360000)
            if self.config.nbr_2nd[1] == '2':
                if self.config.nbr_num <= 12:
                    self.predictor.set_max_tokens(360000)
                elif self.config.nbr_num <= 24:
                    self.predictor.set_max_tokens(120000)
                else:
                    self.predictor.set_max_tokens(90000)
        else:
            self.predictor.set_max_tokens(60000)

        # Set up the attacker
        # Whatever bert/non-bert model, we use the spacy vocab
        spacy_vocab = load_data(self.config.task_id, "spacy")['vocab']
        searcher = CachedWordSearcher(
            "external_data/ibp-nbrs.json"
            if not self.config.big_nbrs else "external_data/euc-top8.json",
            spacy_vocab.get_token_to_index_vocabulary("tokens"))
        forbidden_words = load_banned_words(self.config.task_id)
        # forbidden_words += stopwords.words("english")
        general_kwargs = {
            "ignore_tokens": forbidden_words,
            "forbidden_tokens": forbidden_words,
            "max_change_num_or_ratio": 0.15,
            "field_to_change": field_to_change,
            "field_to_attack": 'label',
            "use_bert": self.config.arch == 'bert',
            "searcher": searcher
        }
        if self.config.attack_method == 'hotflip':
            attacker = HotFlip(self.predictor, **general_kwargs)
        elif self.config.attack_method == 'bruteforce':
            attacker = BruteForce(self.predictor, **general_kwargs)
        elif self.config.attack_method == 'PWWS':
            attacker = PWWS(self.predictor, **general_kwargs)
        elif self.config.attack_method in ['GA-LM', 'GA']:
            if self.config.attack_method == "GA-LM":
                lm_constraints = json.load(
                    open(
                        f"external_data/ibp-nbrs.{self.config.task_id}.{self.config.data_split}.lm.json"
                    ))
            else:
                lm_constraints = None
            attacker = Genetic(self.predictor,
                               num_generation=40,
                               num_population=60,
                               lm_topk=-1,
                               lm_constraints=lm_constraints,
                               **general_kwargs)
        else:
            raise Exception()

        # Start attacking
        if self.config.attack_gen_adv:
            f_adv = open(
                f"nogit/{self.config.model_name}.{self.config.attack_method}.adv.tsv",
                'w')
            f_adv.write("raw\tadv\tlabel\n")
        metric = AttackMetric()
        agg = Aggregator()
        raw_counter = Counter()
        adv_counter = Counter()
        for i in tqdm(range(len(data_to_attack))):
            log(f"Attacking instance {i}...")
            #             if self.config.arch == 'bert':
            #                 raw_json = allenutil.bert_instance_as_json(data_to_attack[i])
            #             else:
            #                 raw_json = allenutil.as_json(data_to_attack[i])
            raw_json = allenutil.as_json(data_to_attack[i])
            adv_json = raw_json.copy()

            raw_probs = self.predictor.predict_json(raw_json)['probs']
            raw_pred = np.argmax(raw_probs)
            raw_label = data_to_attack[i]['label'].label
            if isinstance(raw_label, str):
                raw_label = self.vocab.get_token_index(raw_label, 'labels')

            # Only attack correct instance
            if raw_pred == raw_label:
                # yapf:disable
                result = attacker.attack_from_json(raw_json)
                adv_json[field_to_change] = allenutil.as_sentence(result['adv'])

                if "generation" in result:
                    print("stop at generation", result['generation'])
                # sanity check: in case of failure, the changed num should be close to
                # the max change num.
                if not result['success']:
                    diff = text_diff(result['raw'], result['adv'])
                    print('[Fail statistics]', diff)

                # Count
                if result['success']:
                    diff = text_diff(result['raw'], result['adv'])
                    raw_counter.update(diff['a_changes'])
                    adv_counter.update(diff['b_changes'])
                    to_aggregate = [('change_num', diff['change_num']),
                                    ('change_ratio', diff['change_ratio'])]
                    if "generation" in result:
                        to_aggregate.append(('generation', result['generation']))
                    agg.aggregate(*to_aggregate)
                    log("[raw]", raw_json, "\n[prob]", flt2str(raw_probs, cat=', '))
                    log("[adv]", adv_json, '\n[prob]', flt2str(result['outputs']['probs'], cat=', '))
                    if "changed" in result:
                        log("[changed]", result['changed'])
                    log()

                    log("Avg.change#", "{:5.2f}".format(agg.mean("change_num")),
                        "Avg.change%", "{:5.2f}".format(100 * agg.mean("change_ratio")))
                    if "generation" in result:
                        log("Avg.gen#", agg.mean("generation"))

                if result['success']:
                    metric.succeed()
                else:
                    metric.fail()


                log(f"Aggregated metric: {metric}")
                # yapf:enable
            else:
                log("Skipping the current instance since the predicted label is wrong."
                    )
                metric.escape()

            if self.config.attack_gen_adv:
                f_adv.write(
                    f"{raw_json[field_to_change]}\t{adv_json[field_to_change]}\t{raw_label}\n"
                )
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
              "Accu.before%:", round(metric.accuracy_before_attack, 2),
              "Accu.after%:", round(metric.accuracy_after_attack, 2))
        # yapf:enable

    def attack_pro(self):
        print('Firstly, evaluate the model:')
        self.evaluate_predictor()

        from textattack.attack_results import SuccessfulAttackResult, SkippedAttackResult, FailedAttackResult
        from textattack.models.wrappers import ModelWrapper
        from textattack.attack_recipes import TextFoolerJin2019
        from textattack.attack_recipes import PWWSRen2019

        dataset = allen_instances_for_attack(
            self.downsample(tokenizer='spacy'))

        class AllenModel(ModelWrapper):
            def __init__(self, predictor):
                self.predictor = predictor
                # self.predictor._model.cuda()

            def __call__(self, text_input_list):
                with torch.no_grad():
                    outputs = self.predictor.predict_batch_json([{
                        "sent":
                        text_input
                    } for text_input in text_input_list])
                outputs = [output['probs'] for output in outputs]
                return outputs

        model = AllenModel(self.predictor)

        attack_cls = {
            "pwws": PWWSRen2019,
            "textfooler": TextFoolerJin2019
        }[self.config.attack_pro_method]
        attack = attack_cls.build(model)
        results = attack.attack_dataset(dataset)

        metric = AttackMetric()
        bar = tqdm(results)
        for i, result in enumerate(bar):
            print(f'[sentence] >>> {i}')
            sys.stdout.flush()
            if isinstance(result, SuccessfulAttackResult):
                metric.succeed()
            elif isinstance(result, SkippedAttackResult):
                metric.escape()
            else:
                metric.fail()
            bar.set_description(f'{metric}')
        print(metric)
