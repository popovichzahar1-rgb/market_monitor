"""Text tone: FinBERT (ONNX, int8) with a dependency-free lexicon fallback.

FinBERT runs only for items that passed the rule prefilter, in batches, and the
model is loaded lazily on first use, so runs with nothing interesting skip the
~1 s model load entirely. Export the model once with scripts/export_finbert.py.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from market_monitor.config import SentimentConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sentiment:
    label: str        # positive | negative | neutral
    value: float      # P(positive) - P(negative), in [-1, 1]
    confidence: float  # probability of the predicted label


class SentimentModel(Protocol):
    name: str

    def predict(self, texts: list[str]) -> list[Sentiment]: ...


def _softmax(row: list[float]) -> list[float]:
    peak = max(row)
    exps = [math.exp(v - peak) for v in row]
    total = sum(exps)
    return [e / total for e in exps]


class OnnxFinbert:
    """ProsusAI/finbert exported to ONNX (see scripts/export_finbert.py)."""

    name = "finbert-onnx"

    def __init__(self, cfg: SentimentConfig) -> None:
        self.cfg = cfg
        self.model_dir = Path(cfg.model_dir)
        self._session = None
        self._tokenizer = None
        self._labels: list[str] = []

    @staticmethod
    def available(cfg: SentimentConfig) -> bool:
        model_dir = Path(cfg.model_dir)
        if not all((model_dir / f).exists() for f in ("model.onnx", "tokenizer.json", "config.json")):
            return False
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        except ImportError:
            return False
        return True

    def _load(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = self.cfg.threads
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(self.model_dir / "model.onnx"), options, providers=["CPUExecutionProvider"])
        tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=self.cfg.max_length)
        pad_id = tokenizer.token_to_id("[PAD]") or 0
        tokenizer.enable_padding(pad_id=pad_id, pad_token="[PAD]")
        self._tokenizer = tokenizer
        id2label = json.loads((self.model_dir / "config.json").read_text())["id2label"]
        self._labels = [id2label[str(i)].lower() for i in range(len(id2label))]
        log.info("loaded FinBERT ONNX model from %s", self.model_dir)

    def predict(self, texts: list[str]) -> list[Sentiment]:
        if not texts:
            return []
        if self._session is None:
            self._load()
        import numpy as np

        input_names = {i.name for i in self._session.get_inputs()}
        results: list[Sentiment] = []
        for start in range(0, len(texts), self.cfg.batch_size):
            batch = self._tokenizer.encode_batch(texts[start:start + self.cfg.batch_size])
            feed = {
                "input_ids": np.array([e.ids for e in batch], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in batch], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in batch], dtype=np.int64),
            }
            logits = self._session.run(None, {k: v for k, v in feed.items() if k in input_names})[0]
            for row in logits.tolist():
                probs = dict(zip(self._labels, _softmax(row)))
                label = max(probs, key=probs.get)
                results.append(Sentiment(
                    label=label,
                    value=probs.get("positive", 0.0) - probs.get("negative", 0.0),
                    confidence=probs[label],
                ))
        return results


# Small finance-oriented lexicon (inspired by Loughran-McDonald). Deliberately
# excludes ambiguous words such as "cut" (rate cuts vs job cuts).
_POSITIVE = """surge surges surged soar soars soared rally rallies rallied jump jumps jumped
gain gains gained rise rises rose record beat beats upgrade upgraded growth profit profits
strong stronger boost boosts boosted rebound rebounds rebounded optimism approve approves
approved approval breakthrough recovery recovers ceasefire deal agreement""".split()
_NEGATIVE = """plunge plunges plunged slump slumps slumped crash crashes crashed tumble tumbles
tumbled fall falls fell drop drops dropped sink sinks sank loss losses miss misses missed
downgrade downgraded default defaults bankruptcy bankrupt fraud hack hacked exploit
exploited sanction sanctions war attack attacks attacked layoffs recession warning warns
probe lawsuit halt halts halted collapse collapses collapsed weak weaker fear fears crisis
seize seizes seized seizure shortage disruption outage explosion delay delays delayed
reject rejects rejected ban bans banned investigation charged""".split()
_WORD_RE = re.compile(r"[a-z']+")


class LexiconSentiment:
    name = "lexicon"

    def predict(self, texts: list[str]) -> list[Sentiment]:
        positive, negative = set(_POSITIVE), set(_NEGATIVE)
        out = []
        for text in texts:
            words = _WORD_RE.findall(text.lower())
            pos = sum(w in positive for w in words)
            neg = sum(w in negative for w in words)
            total = pos + neg
            if total == 0:
                out.append(Sentiment("neutral", 0.0, 1.0))
                continue
            # Polarity scaled by evidence: 1 hit -> at most 0.5, 3+ hits -> full.
            value = (pos - neg) / total * min(1.0, total / 3 + 1 / 6)
            label = "positive" if value > 0.15 else "negative" if value < -0.15 else "neutral"
            out.append(Sentiment(label, round(value, 3), min(1.0, 0.5 + abs(value) / 2)))
        return out


def load_sentiment(cfg: SentimentConfig) -> SentimentModel | None:
    if cfg.backend == "none":
        return None
    if cfg.backend in ("auto", "onnx") and OnnxFinbert.available(cfg):
        return OnnxFinbert(cfg)
    if cfg.backend == "onnx":
        log.warning("FinBERT ONNX model not found in %s; falling back to lexicon", cfg.model_dir)
    return LexiconSentiment()
