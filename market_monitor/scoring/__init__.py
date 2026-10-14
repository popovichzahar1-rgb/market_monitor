"""Importance scoring: rules (always) + sentiment (only for prefiltered items).

final = clamp(rule points + sentiment points, 0, 100)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from market_monitor.config import Config
from market_monitor.models import Item
from market_monitor.scoring.rules import rule_score
from market_monitor.scoring.sentiment import (
    LexiconSentiment,
    Sentiment,
    SentimentModel,
    load_sentiment,
)

log = logging.getLogger(__name__)


@dataclass
class Score:
    score: float
    topic: str
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    sentiment: float | None = None
    sentiment_label: str | None = None
    sentiment_model: str | None = None


def clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def sentiment_points(s: Sentiment, weight: float, negative_extra: float) -> float:
    points = weight * abs(s.value)
    if s.value <= -0.5:
        points += negative_extra
    return points


def sentiment_text(item: Item) -> str:
    return f"{item.title}. {item.text[:400]}" if item.text else item.title


class Scorer:
    def __init__(self, config: Config, model: SentimentModel | None = None,
                 use_default_model: bool = True) -> None:
        self.config = config
        self.cfg = config.scoring
        self.model = model if model is not None or not use_default_model \
            else load_sentiment(self.cfg.sentiment)
        self.watch_tickers = {w.ticker for w in config.watchlist}
        self.regions = set(config.regions)
        self.last_model_seconds = 0.0

    def score(self, items: list[Item], mentions: dict[str, int] | None = None,
              known_sentiment: dict[str, Sentiment] | None = None) -> list[Score]:
        mentions = mentions or {}
        known_sentiment = known_sentiment or {}
        rules = [rule_score(i, self.cfg, self.watch_tickers, mentions.get(i.id, 1), self.regions) for i in items]
        scores = [Score(score=r.points, topic=r.topic or self.cfg.default_topic,
                        flags=r.flags, reasons=r.reasons) for r in rules]

        # Prefilter: the model only sees items that already look relevant. Data items
        # (numbers) have no meaningful tone.
        todo = [i for i, (item, s) in enumerate(zip(items, scores))
                if s.score >= self.cfg.prefilter_min and not item.is_data
                and item.id not in known_sentiment]
        predicted = dict(self._predict([items[i] for i in todo], todo))
        for index, (item, s) in enumerate(zip(items, scores)):
            sent = known_sentiment.get(item.id) or predicted.get(index)
            if sent is not None:
                cfg = self.cfg.sentiment
                points = sentiment_points(sent, cfg.weight, cfg.negative_extra)
                if points >= 0.5:
                    s.reasons.append(f"tone:{sent.label} {points:+.0f}")
                s.score += points
                s.sentiment, s.sentiment_label = sent.value, sent.label
                s.sentiment_model = self.model.name if self.model and index in predicted else None
            s.score = clamp(s.score)
        return scores

    def _predict(self, items: list[Item], indices: list[int]) -> list[tuple[int, Sentiment]]:
        if not items or self.model is None:
            return []
        start = time.monotonic()
        texts = [sentiment_text(i) for i in items]
        try:
            results = self.model.predict(texts)
        except Exception as exc:  # broken model file etc.: degrade, don't fail the run
            log.warning("sentiment model %s failed (%s); using lexicon", self.model.name, exc)
            self.model = LexiconSentiment()
            results = self.model.predict(texts)
        self.last_model_seconds = time.monotonic() - start
        log.info("sentiment (%s): %d items in %.2fs", self.model.name, len(items),
                 self.last_model_seconds)
        return list(zip(indices, results))
