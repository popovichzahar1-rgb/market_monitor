"""Link items to watchlist companies.

Direct match (the company itself) -> ticker added to `item.entities`:
- name and aliases: whole words, any case ("Apple", "iPhone maker");
- ticker: case-sensitive whole word ("AAPL"); tickers that are short or common
  words ("A", "ON", "NOW", "KEY") only in strong forms: "$NOW", "(NYSE: NOW)",
  "NYSE:NOW", so ordinary text doesn't trigger them.
Related match -> `item.meta["related"][ticker] = ["peer:MSFT", "supplier:TSMC", "sector"]`:
peers / suppliers (tickers or names, same rules) and `sector_keywords`.
Related matches never set the direct entity; they only add context and a small bonus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from market_monitor.config import WatchItem
from market_monitor.models import Item
from market_monitor.scoring.keywords import PhraseMatcher

TICKER_LIKE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
COMMON_WORD_TICKERS = {
    "A", "AI", "ALL", "ARE", "BE", "BIG", "CAN", "CAR", "CEO", "CPI", "EU", "ETF", "FAST",
    "FOR", "GDP", "GO", "HAS", "IPO", "IT", "KEY", "LOW", "MAN", "NEW", "NOW", "ON", "ONE",
    "OPEN", "OUT", "PAY", "REAL", "RUN", "SAFE", "SEE", "SO", "TRUE", "TWO", "UK", "US",
    "WELL", "YOU", "USA", "CAT", "DOW", "FUN", "HOG", "LIFE", "LOVE", "PLAY", "SHOP",
}
_EXCHANGES = r"(?:NASDAQ|Nasdaq|NYSE|NYSE American|NYSEARCA|AMEX|LSE|XETRA|Euronext|OTC|TSX|HKEX)"


def _ticker_pattern(ticker: str) -> re.Pattern:
    t = re.escape(ticker)
    strong = [rf"\${t}(?![\w.])", rf"\(\s*(?:{_EXCHANGES}\s*:\s*)?{t}\s*\)",
              rf"\b{_EXCHANGES}\s*:\s*{t}(?![\w.])"]
    if len(ticker) >= 3 and ticker not in COMMON_WORD_TICKERS:
        strong.append(rf"(?<![\w$]){t}(?![\w.])")
    return re.compile("|".join(strong))


class _Target:
    """Matches one company/ticker/name reference in text."""

    def __init__(self, label: str, names: list[str], ticker: str | None) -> None:
        self.label = label
        self.ticker_re = _ticker_pattern(ticker) if ticker else None
        # lowercase phrases match any case (see scoring.keywords)
        phrases = [n.lower() for n in names if len(n.strip()) >= 3]
        self.names = PhraseMatcher(phrases)

    def found(self, text: str) -> bool:
        if self.ticker_re is not None and self.ticker_re.search(text):
            return True
        return bool(self.names) and self.names.search(text) is not None


def _reference(entry: str) -> _Target:
    """Peer/supplier entry: 'MSFT' is a ticker, 'Foxconn' is a name."""
    entry = entry.strip()
    if TICKER_LIKE.match(entry) and entry.isupper():
        return _Target(entry, [], entry)
    return _Target(entry, [entry], None)


@dataclass
class _Company:
    watch: WatchItem
    direct: _Target
    related: list[tuple[str, _Target]] = field(default_factory=list)  # (kind, target)
    sector: PhraseMatcher | None = None


@dataclass
class Match:
    direct: list[str] = field(default_factory=list)
    related: dict[str, list[str]] = field(default_factory=dict)


class WatchlistMatcher:
    def __init__(self, watchlist: list[WatchItem]) -> None:
        self.companies: list[_Company] = []
        for w in watchlist:
            company = _Company(w, _Target(w.ticker, [w.name, *w.aliases], w.ticker))
            company.related += [("peer", _reference(p)) for p in w.peers]
            company.related += [("supplier", _reference(s)) for s in w.suppliers]
            if w.sector_keywords:
                company.sector = PhraseMatcher([k.lower() for k in w.sector_keywords])
            self.companies.append(company)

    def __bool__(self) -> bool:
        return bool(self.companies)

    def match(self, title: str, text: str = "", entities: list[str] | None = None) -> Match:
        full = f"{title}\n{text}"
        result = Match()
        for company in self.companies:
            ticker = company.watch.ticker
            if (entities and ticker in entities) or company.direct.found(full):
                result.direct.append(ticker)
                continue
            kinds = [f"{kind}:{target.label}" for kind, target in company.related if target.found(full)]
            if company.sector is not None and company.sector.search(full):
                kinds.append("sector")
            if kinds:
                result.related[ticker] = kinds
        return result

    def enrich(self, item: Item) -> Item:
        """Add direct tickers to entities and related links to meta (in place)."""
        if not self.companies:
            return item
        m = self.match(item.title, item.text, item.entities)
        if m.direct:
            item.entities = list(dict.fromkeys([*item.entities, *m.direct]))
        if m.related:
            item.meta = {**item.meta, "related": m.related}
        return item
