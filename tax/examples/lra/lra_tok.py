"""Tokenizers for long range arena"""

import abc
from typing import Dict, List, Any, Union
import logging
import json
import numpy as np
import os
from tqdm import tqdm
from collections import Counter, OrderedDict
from transformers import (
    BatchEncoding,
    GPT2Tokenizer,
    GPT2TokenizerFast,
)
from transformers import AutoTokenizer

LOG = logging.getLogger(__name__)


class Tokenizer(abc.ABC):
    def __init__(self) -> None:
        self.special_tokens = {
            "<unk>": "<unk>",
            "<pad>": "<pad>",
            "<bos>": "<bos>",
            "<eos>": "<eos>",
        }
        self.mapping = {
            tok: id_
            for tok, id_ in zip(range(len(self.special_tokens)), self.special_tokens)
        }

    @abc.abstractmethod
    def __call__(self):
        pass

    @property
    def pad_token_id(self):
        return self.mapping["<pad>"]

    @property
    def unk_token_id(self):
        return self.mapping["<unk>"]

    @property
    def bos_token_id(self):
        return self.mapping["<bos>"]

    @property
    def eos_token_id(self):
        return self.mapping["<eos>"]

    @property
    def vocab_size(self):
        return len(self.mapping)


class CharTokenizer(Tokenizer):
    """
    Given a full corpus do a char level tokenization.
    Vocaublary size is infered from thhe corpus.
    Not a very efficient tokenization on large datasets.
        Needs all data in memory
    """

    def __init__(self, data) -> None:
        super().__init__()
        self._data = data
        self._tokens = sorted(list(set(data)))

        self.mapping = {}
        for i, t in enumerate(self._tokens):
            self.mapping[t] = i

        k = len(self._tokens)
        for i, t in enumerate(self.special_tokens.values()):
            self.mapping[t] = k + i
        self.reverse_mapping = {v: k for k, v in self.mapping.items()}

    def _elem_call(self, element, add_special_tokens=False, **kwargs):
        input_ids = [0] * len(element)
        for i, t in enumerate(element):
            input_ids[i] = self.mapping.get(t, self.unk_token_id)
        if add_special_tokens:
            input_ids = [self.bos_token_id] + input_ids + [self.eos_token_id]
        return input_ids

    def __call__(self, elements, add_special_tokens=False, **kwargs):
        if isinstance(elements, list):
            input_ids = []
            for elem in elements:
                input_ids.append(
                    self._elem_call(
                        elem, add_special_tokens=add_special_tokens, **kwargs
                    )
                )
        else:
            input_ids = [self._elem_call(elements, **kwargs)]

        return BatchEncoding({"input_ids": input_ids})

    def decode(self, sentence, add_special_tokens=False, **kwargs):
        sent = ["a"] * len(sentence)
        k = 0
        for i, tok in enumerate(sentence):
            if not add_special_tokens:
                if self.reverse_mapping[tok] in self.special_tokens:
                    continue
            sent[k] = self.reverse_mapping[tok]
            k += 1

        return "".join(sent)

    def batch_decode(self, sentences, add_special_tokens=False, **kwargs):
        res = [None] * len(sentences)
        for i, s in enumerate(sentences):
            res[i] = self.decode(s, add_special_tokens=add_special_tokens)
        return res


class FileSimpleTokenizer(Tokenizer):
    """
    Similar to CharTokenizer, but builds vocab from disk file and
        tokens=words if delimenter is ""
    Modified from: https://github.com/kimiyoung/transformer-xl/blob/master/pytorch/utils/vocabulary.py
    """

    def __init__(
        self, min_freq=0, max_size=None, lower_case=True, delimiter=None
    ) -> None:
        super().__init__()
        self.reverse_special_tokens = {v: k for k, v in self.special_tokens.items()}
        # self.mapping contains special tokens
        self.counter = Counter()
        self.min_freq = min_freq
        self.max_size = max_size
        self.lower_case = lower_case
        self.delimiter = delimiter  # None -> whitespace

    def tokenize(self, line, add_eos=False, add_bos=False):
        line = line.strip()
        # convert to lower case
        if self.lower_case:
            line = line.lower()

        # empty delimiter '' will evaluate False
        if self.delimiter == "":
            symbols = line
        else:
            symbols = line.split(self.delimiter)

        if add_bos:
            return [self.special_tokens["<bos>"]] + symbols
        if add_eos:
            return symbols + [self.special_tokens["<eos>"]]
        return symbols

    def encode_one(self, element, add_eos=True, add_bos=True, **kwargs):
        element = self.tokenize(element, add_eos=add_eos, add_bos=add_bos)
        encoded = [0] * len(element)
        for i, t in enumerate(element):
            encoded[i] = self.mapping[t]
        return {"input_ids": encoded}

    def __call__(self, element, add_eos=True, add_bos=True, **kwargs):
        if isinstance(element, list):
            res = []
            for e in element:
                res.append(self.encode_one(e, add_bos=add_bos, add_eos=add_eos))
            return res
        return self.encode_one(element, add_bos=add_bos, add_eos=add_eos)

    def count_file(self, path, add_eos=False, add_bos=False):
        assert os.path.exists(path)
        sents = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                symbols = self.tokenize(line, add_eos=add_eos, add_bos=add_bos)
                self.counter.update(symbols)
                sents.append(symbols)

        return sents

    def build_vocab(self):
        LOG.info(
            "building vocab with min_freq=%s, max_size=%s", self.min_freq, self.max_size
        )
        self.mapping = OrderedDict()
        self.reverse_mapping = OrderedDict()

        for sym in self.special_tokens.values():
            if sym not in self.mapping:
                self.mapping[sym] = len(self.mapping)

        for sym, cnt in self.counter.most_common(self.max_size):
            if cnt < self.min_freq:
                break
            if sym not in self.mapping:
                self.mapping[sym] = len(self.mapping)

        self.reverse_mapping = {v: k for k, v in self.mapping.items()}
        LOG.info(
            "final vocab size %s from %s unique tokens",
            self.vocab_size,
            len(self.counter),
        )
        print(self.mapping)
        print(self.reverse_mapping)

    def encode_file(self, path, verbose=False, add_eos=True, add_bos=False):
        if verbose:
            LOG.info("encoding file %s ...", path)
        assert os.path.exists(path)
        encoded = []
        with open(path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if verbose and idx > 0 and idx % 500000 == 0:
                    LOG.info("    line %s", idx)
                symbols = self.__call__(line, add_eos=add_eos, add_bos=add_bos)[
                    "input_ids"
                ]
                encoded.append(np.array(symbols, dtype=np.int16))
        encoded = np.concatenate(encoded)
        return encoded

    def decode(self, sentence, decode_tok=False, **kwargs):
        sent = []
        for c in sentence:
            c = self.reverse_mapping[c]
            if decode_tok:
                # special toks like <bos>
                if not c in self.reverse_special_tokens:
                    c = chr(int(c))
            sent.append(c)
        if decode_tok:
            sent = "".join(sent)
        return sent

    def batch_decode(self, batch, decode_tok=False, **kwargs):
        """
        Args:
            batch: List[List] - batch of data
            decode_tok: bool - used if the train/valid were already trokenized and encoded.
                Relevant for enwik8 datase
        """
        res = []
        for s in batch:
            sent = self.decode(s, decode_tok=decode_tok, **kwargs)
            res.append(sent)
        return res


class ListOpsTokenizer(Tokenizer):
    """
    Possible operations: Max, Min, Median, Sum_mod.
    Brackets (, [
    Operators 0 9
    Results 0-9 (10 way classification problem)
    """

    def __init__(self, mapping, special_tokens=None):
        super().__init__()
        self.mapping = mapping
        if not special_tokens is None:
            for k, v in special_tokens.items():
                self.special_tokens[k] = v
        self.reverse_mapping = {v: k for k, v in self.mapping.items()}

    @classmethod
    def from_pretrained(cls, path):
        with open(path, "r+", encoding="utf-8") as f:
            tmp = json.load(f)
            mapping = tmp["tokens"]
            special_tokens = tmp["special_tokens"]
        tokenizer = cls(mapping, special_tokens)
        return tokenizer

    @staticmethod
    def train_it(dataset, special_tokens: Dict, save_path=None):
        """Map a symbol to each character in the set"""
        vocab = set()
        for inp in dataset["train"]["Source"]:
            inp = inp.split()
            vocab.update(inp)
        values = range(len(vocab))
        mapping = {k: v for k, v in zip(vocab, values)}

        for i, s in enumerate(special_tokens.values()):
            mapping[s] = len(vocab) + i

        if not save_path is None:
            with open(save_path, "w+", encoding="utf-8") as f:
                state = {"special_tokens": special_tokens, "tokens": mapping}
                json.dump(state, f)
        return mapping

    def pad(self, max_length, element):
        pad_length = max_length - len(element["input_ids"])
        if pad_length > 0:
            element["input_ids"] += [self.pad_token_id for _ in range(pad_length)]
            element["pad_mask"] += [1] * pad_length
        return element

    def __call__(self, max_length, element, **kwargs):
        toks = element["Source"].split()
        input_ids = [0] * len(toks)
        for i, t in enumerate(toks):
            input_ids[i] = self.mapping.get(t, self.unk_token_id)

        input_ids = input_ids[0:max_length]
        result = {
            "input_ids": input_ids,
            "labels": element["Target"],
            "pad_mask": [0] * len(input_ids),
        }
        return BatchEncoding(result)

    def decode(self, sentence: List):
        sent = ["a"] * len(sentence)
        for i, tok in enumerate(sentence):
            sent[i] = self.reverse_mapping[tok]
        return " ".join(sent)

    def decode_batch(self, sentences):
        res = [None] * len(sentences)
        for i, s in enumerate(sentences):
            res[i] = self.decode(s)
        return res


class ByteLevelTokenizer(Tokenizer):
    """
    Tokenizes using 256 ascii encodings
    """

    NUM_BYTES = 2**8

    def __init__(self, use_bos=False, use_eos=True) -> None:
        super().__init__()
        self.mapping = {
            "<pad>": self.NUM_BYTES,
            "<unk>": self.NUM_BYTES + 1,
            "<bos>": self.NUM_BYTES + 2,
            "<eos>": self.NUM_BYTES + 3,
        }
        self.reverse_special_toks = {v: k for k, v in self.mapping.items()}
        self.use_bos = use_bos
        self.use_eos = use_eos

    @property
    def vocab_size(self):
        return len(self.special_tokens) + self.NUM_BYTES

    def pad(self, max_length, element):
        pad_length = max_length - len(element["input_ids"])
        if pad_length > 0:
            element["input_ids"] += [self.pad_token_id for _ in range(pad_length)]
            element["pad_mask"] += [1] * pad_length
        return element

    def pad_pair(self, max_length, element):
        for i in [0, 1]:
            pad_length = max_length - len(element["input_ids"][i])
            if pad_length > 0:
                element["input_ids"][i] += [
                    self.pad_token_id for _ in range(pad_length)
                ]
                element["pad_mask"][i] += [1] * pad_length
        return element

    def __call__(self, max_length, element, **kwargs):
        # no need for encoding
        input_ids = list(bytearray(element["Source"].encode(encoding="utf-8")))
        max_length = max_length - int(self.use_bos) - int(self.use_eos)
        input_ids = input_ids[0:max_length]
        if self.use_bos:
            input_ids = [self.bos_token_id] + input_ids
        if self.use_eos:
            input_ids = input_ids + [self.eos_token_id]
        result = {
            "input_ids": input_ids,
            "labels": element["Target"],
            "pad_mask": [0] * len(input_ids),
        }
        return BatchEncoding(result)

    def tokenize_pair(self, max_length, element):
        input_ids = list(bytearray(element["text1"].encode(encoding="utf-8")))
        input_ids2 = list(bytearray(element["text2"].encode(encoding="utf-8")))
        max_length = max_length - int(self.use_bos) - int(self.use_eos)
        input_ids = input_ids[0:max_length]
        input_ids2 = input_ids2[0:max_length]

        if self.use_bos:
            input_ids = [self.bos_token_id] + input_ids
            input_ids2 = [self.bos_token_id] + input_ids2
        if self.use_eos:
            input_ids = input_ids + [self.eos_token_id]
            input_ids2 = input_ids2 + [self.eos_token_id]
        result = {
            "input_ids": [input_ids, input_ids2],
            "labels": element["Target"],
            "pad_mask": [[0] * len(input_ids), [0] * len(input_ids2)],
        }
        return BatchEncoding(result)

    def decode(self, sentence: List):
        sent = ["a"] * len(sentence)
        for i, tok in enumerate(sentence):
            if tok in self.reverse_special_toks:
                sent[i] = self.reverse_special_toks[tok]
            else:
                sent[i] = chr(tok)
        return " ".join(sent)

    def decode_batch(self, sentences):
        res = [None] * len(sentences)
        for i, s in enumerate(sentences):
            res[i] = self.decode(s)
        return res


class ImageTokenizer(Tokenizer):
    """
    Dummy tokenizer used only for consistency.
    If vocab_size passed all the models will assume integers as inputs and will tokenizer as nlp layers
    """

    def __init__(self, vocab_size=None) -> None:
        # No need to call super().__init__()
        self._vocab_size = vocab_size
        self.special_tokens = None
        self.mapping = None

    def __call__(self):
        pass

    @property
    def pad_token_id(self):
        return -100

    @property
    def unk_token_id(self):
        return -100

    @property
    def bos_token_id(self):
        return -100

    @property
    def eos_token_id(self):
        return -100

    @property
    def vocab_size(self):
        return self._vocab_size


# Wrapper around Hggingface tokemizers to make sure that special tokens like pad are added
class SpecialToksGPT2Tokenizer(GPT2Tokenizer):
    @classmethod
    def from_pretrained(
        cls,
        pretrained_model_name_or_path: Union[str, os.PathLike],
        *init_inputs,
        **kwargs,
    ):
        tokenizer = super().from_pretrained(
            pretrained_model_name_or_path, *init_inputs, **kwargs
        )
        special_tokens_dict = {
            "pad_token": "<|endoftext|>",
            "sep_token": "<|endoftext|>",
            "cls_token": "<|endoftext|>",
        }
        tokenizer.add_special_tokens(special_tokens_dict)
        return tokenizer


class SpecialToksGPT2TokenizerFast(GPT2TokenizerFast):
    slow_tokenizer_class = SpecialToksGPT2Tokenizer

    @classmethod
    def from_pretrained(
        cls,
        pretrained_model_name_or_path: Union[str, os.PathLike],
        *init_inputs,
        **kwargs,
    ):
        tokenizer = super().from_pretrained(
            pretrained_model_name_or_path, *init_inputs, **kwargs
        )
        special_tokens_dict = {
            "pad_token": "<|endoftext|>",
            "sep_token": "<|endoftext|>",
            "cls_token": "<|endoftext|>",
        }
        tokenizer.add_special_tokens(special_tokens_dict)
        return tokenizer


def train_tokenizer_from_pretrained(
    raw_datasets,
    tokenizer_name="bert-base-cased",
    col_name="text",
    save_path=None,
    repo_id=None,
):
    """
    Args:
        raw_datasets: Dataset - raw text data
    """
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)

    def batch_iterator(batch_size=10000):
        for i in tqdm(range(0, len(raw_datasets), batch_size)):
            yield raw_datasets[i : i + batch_size][col_name]

    new_tokenizer = tokenizer.train_new_from_iterator(
        text_iterator=batch_iterator(), vocab_size=tokenizer.vocab_size
    )
    if not save_path is None:
        new_tokenizer.save_pretrained(save_path)
    if not repo_id is None:
        new_tokenizer.push_to_hub(repo_id)

    return new_tokenizer


def train_listops():
    """Example sript on how to train the tokenizer for Listops"""
    from tax.examples.lra.lra_dp import ListOpsDP

    special_tokens = {
        "<unk>": "<unk>",
        "<pad>": "<pad>",
        "<bos>": "<bos>",
        "<eos>": "<eos>",
    }
    save_path = "/home/rares/latte_trans/data/tokenizers/tok_list_ops.json"
    tokenizer = None
    dp = ListOpsDP(
        tokenizer=tokenizer, cache_dir="/data_rares/data/input/lra_data"
    )  # "/mnt/data/input/lra_data")
    ds = dp.get_raw_data()
    ListOpsTokenizer.train_it(
        dataset=ds, special_tokens=special_tokens, save_path=save_path
    )


if __name__ == "__main__":
    train_listops()
