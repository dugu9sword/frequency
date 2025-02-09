from typing import Dict

import torch

from allennlp.data import TextFieldTensors, Vocabulary
from allennlp.models.model import Model
from allennlp.modules import FeedForward, InputVariationalDropout
from allennlp.nn.util import (
    get_text_field_mask,
    masked_softmax,
    weighted_sum,
    masked_max,
)
from allennlp.training.metrics import CategoricalAccuracy
from allennlp.modules.matrix_attention.dot_product_matrix_attention import DotProductMatrixAttention
from allennlp.modules.token_embedders import TokenEmbedder
from allennlp.modules.text_field_embedders import BasicTextFieldEmbedder
from allennlp.training.optimizers import DenseSparseAdam
from allennlp.modules.seq2seq_encoders.pytorch_seq2seq_wrapper import LstmSeq2SeqEncoder
from allennlpx.modules.seq2vec_encoders.boe_encoder import BagOfEmbeddingsEncoder
from allennlpx.training import adv_utils
from luna import ram_read


class PassThrough(torch.nn.Module):
    def forward(self, x):
        return x

class BiBOE(Model):
    def __init__(
        self,
        vocab: Vocabulary,
        token_embedder: TokenEmbedder,
        num_labels: int,
        pool: str = 'sum',
    ) -> None:
        super().__init__(vocab)

        self.word_embedders = BasicTextFieldEmbedder(
            {"tokens": token_embedder}
        )
        dim = token_embedder.get_output_dim()
        self.rotation = FeedForward(dim, 1, dim, torch.nn.ReLU())

        self.encoder = BagOfEmbeddingsEncoder(
            embedding_dim=dim, 
            pool=pool,
            dropout=0.1
        )

        self.feedforward = FeedForward(dim * 2, 2, dim * 2, torch.nn.ReLU(), 0.1)
        self.output_logit = FeedForward(dim * 2, 1, num_labels, PassThrough())

        self._num_labels = num_labels

        self._accuracy = CategoricalAccuracy()
        self._loss = torch.nn.CrossEntropyLoss()

    def forward(  # type: ignore
        self,
        sent1: TextFieldTensors,
        sent2: TextFieldTensors,
        label: torch.IntTensor = None,
    ) -> Dict[str, torch.Tensor]:
        with adv_utils.forward_context("sent1"):
            encoded_sent1 = self.encoder(
                self.rotation(self.word_embedders(sent1)), 
                get_text_field_mask(sent1)
            )
        with adv_utils.forward_context("sent2"):
            encoded_sent2 = self.encoder(
                self.rotation(self.word_embedders(sent2)), 
                get_text_field_mask(sent2)
            )

        encoded = torch.cat([encoded_sent1, encoded_sent2], dim=1)

        output_hidden = self.feedforward(encoded)
        label_logits = self.output_logit(output_hidden)
        label_probs = torch.nn.functional.softmax(label_logits, dim=-1)

        output_dict = {"logits": label_logits, "probs": label_probs}

        if label is not None:
            loss = self._loss(label_logits, label.long().view(-1))
            self._accuracy(label_logits, label)
            output_dict["loss"] = loss

        return output_dict

    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        return {"accuracy": self._accuracy.get_metric(reset)}

    def get_optimizer(self):
        # return AdamWOptimizer(self.named_parameters(), lr=5e-4, weight_decay=0.0001)
        return DenseSparseAdam(self.named_parameters(), lr=5e-4)