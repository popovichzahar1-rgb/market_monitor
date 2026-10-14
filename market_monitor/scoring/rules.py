"""Rule-based part of the score: keywords, source weights, data magnitudes, bonuses.

All weights live in config.yaml (`scoring:`). The output is explainable: every
point added is recorded in `reasons`, so `python -m market_monitor explain` and
the 👍/👎 feedback (stage 7) can show why something was (not) alerted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from market_monitor.config import ScoringConfig
from market_monitor.models import Item
from market_monitor.scoring.keywords import matcher

# Diminishing returns when several rules match: 1st full weight, 2nd half, ...
RULE_DECAY = 0.5


@dataclass
class RuleResult:
    points: float = 0.0
    topic: str | None = None
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def add(self, points: float, reason: str) -> None:
        if points:
            self.points += points
            self.reasons.append(f"{reason} {points:+.0f}")


def _source_weight(source: str, weights: dict[str, float]) -> tuple[str, float] | None:
    if source in weights:
        return source, weights[source]
    best = None
    for key, value in weights.items():
        if source.startswith(key + ":") and (best is None or len(key) > len(best[0])):
            best = (key, value)
    return best


def data_points(item: Item, cfg: ScoringConfig, result: RuleResult) -> None:
    """Scores for items generated from numbers, based on their magnitude."""
    d, meta, tags = cfg.data, item.meta, set(item.tags)
    if "price_move" in tags and meta.get("change_24h_pct") is not None:
        change = abs(float(meta["change_24h_pct"]))
        threshold = 5.0 if "market_wide" in tags else 7.0
        result.add(d.price_move_base + max(0.0, change - threshold) * d.price_move_per_pct,
                   f"price move {change:.1f}%")
        result.flags.append("crypto_price_move")
        result.topic = result.topic or "crypto"
    elif item.source == "fred":
        if meta.get("mode") == "move":
            result.add(d.fred_move_base, f"FRED move {meta.get('series')}")
            result.flags.append("macro_move")
        else:
            result.add(d.fred_release_base, f"FRED release {meta.get('series')}")
            if meta.get("series") in d.fred_major_series:
                result.add(d.fred_major_bonus, "major macro release")
                result.flags.append("macro_release")
    elif "calendar" in tags:
        result.add(d.calendar_base, "calendar")
    elif "earthquake" in tags and meta.get("magnitude") is not None:
        mag = float(meta["magnitude"])
        result.add(d.quake_base + max(0.0, mag - 6.5) * d.quake_per_magnitude, f"quake M{mag}")
        alert = meta.get("alert")
        if alert in d.quake_alert_bonus:
            result.add(d.quake_alert_bonus[alert], f"PAGER {alert}")
            result.flags.append("major_quake")
        if meta.get("tsunami"):
            result.add(d.quake_tsunami_bonus, "tsunami flag")
    elif item.source == "sec_edgar":
        form = meta.get("form", "")
        if form == "8-K":
            result.add(d.sec_8k_base, "SEC 8-K")
            result.flags.append("sec_8k")
            codes = {c.strip() for c in str(meta.get("items", "")).split(",")}
            major = sorted(codes & set(d.sec_major_8k_items))
            if major:
                result.add(d.sec_major_bonus, f"8-K items {','.join(major)}")
        else:
            result.add(d.sec_periodic_base, f"SEC {form}")
        result.topic = result.topic or "equities"


def rule_score(item: Item, cfg: ScoringConfig, watch_tickers: set[str],
               mentions: int = 1, regions: set[str] | None = None) -> RuleResult:
    """`regions`: active regions from config.regions (None = all configured keywords)."""
    result = RuleResult()
    title = item.title
    full = f"{item.title}\n{item.text}"
    tags = set(item.tags)

    matched: list[tuple[float, str, str | None, bool]] = []
    for rule in cfg.rules:
        if rule.tags_any and not tags.intersection(rule.tags_any):
            continue
        text = title if rule.title_only else full
        hit = matcher(tuple(rule.any)).search(text)
        if hit is None:
            continue
        if rule.also_any and matcher(tuple(rule.also_any)).search(text) is None:
            continue
        matched.append((rule.weight, rule.name, rule.topic, rule.alert))
    # Positive rules with diminishing returns; negative (noise) rules apply in full.
    matched.sort(key=lambda r: r[0], reverse=True)
    rank = 0
    for weight, name, topic, alert in matched:
        decay = 1.0
        if weight > 0:
            decay, rank = RULE_DECAY**rank, rank + 1
        result.add(weight * decay, f"rule:{name}")
        if topic and result.topic is None:
            result.topic = topic
        if alert:
            result.flags.append(name)

    data_points(item, cfg, result)

    if (sw := _source_weight(item.source, cfg.source_weights)) is not None:
        result.add(sw[1], f"source:{sw[0]}")

    if mentions > 1:
        result.add(min(cfg.mentions_bonus_max, (mentions - 1) * cfg.mentions_bonus_per_source),
                   f"{mentions} sources")

    assets = [a for a, words in cfg.asset_keywords.items()
              if a in item.entities or matcher(tuple(words)).search(full)]
    if assets:
        result.add(cfg.asset_bonus, f"assets:{','.join(assets)}")
        result.flags.extend(f"asset:{a}" for a in assets)

    regions_hit = [r for r, words in cfg.region_keywords.items()
                   if (regions is None or r in regions)
                   and (r in tags or matcher(tuple(words)).search(full))]
    if regions_hit:
        result.add(cfg.region_bonus, f"regions:{','.join(regions_hit)}")

    watched = sorted(watch_tickers.intersection(item.entities))
    if watched:
        result.add(cfg.watchlist_bonus, f"watchlist:{','.join(watched)}")
        result.flags.append("watchlist")
    else:
        related = {t: k for t, k in (item.meta.get("related") or {}).items() if t in watch_tickers}
        if related:
            label = ",".join(f"{t}({'/'.join(k)})" for t, k in sorted(related.items()))
            result.add(cfg.watchlist_related_bonus, f"related:{label}")

    if result.topic is None:
        result.topic = detect_topic(item, cfg)
    return result


def detect_topic(item: Item, cfg: ScoringConfig) -> str:
    """Topic by tags first, then by keyword hits; ties go to the first configured topic."""
    tags = set(item.tags)
    for name, topic in cfg.topics.items():
        if tags.intersection(topic.tags):
            return name
    full = f"{item.title}\n{item.text}"
    best, best_hits = cfg.default_topic, 0
    for name, topic in cfg.topics.items():
        hits = len(matcher(tuple(topic.keywords)).matches(full))
        if hits > best_hits:
            best, best_hits = name, hits
    return best
