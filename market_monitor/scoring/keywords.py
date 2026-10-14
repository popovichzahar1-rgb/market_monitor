"""Whole-word phrase matching with a `*` suffix wildcard.

Case rule (keeps config readable and avoids false hits):
- all-lowercase phrase  -> matches any case   ("holds rates" ~ "Holds Rates")
- any uppercase letter  -> case-sensitive     ("Fed" != "fed up", "US" != "us")
"""

from __future__ import annotations

import re
from functools import lru_cache


def _phrase_pattern(phrase: str) -> str:
    parts = [re.escape(p) for p in phrase.strip().split()]
    body = r"\s+".join(parts).replace(r"\*", r"\w*")
    if phrase == phrase.lower():
        body = f"(?i:{body})"
    return rf"(?<!\w){body}(?!\w)"


class PhraseMatcher:
    def __init__(self, phrases: list[str]) -> None:
        self.phrases = [p for p in phrases if p.strip()]
        self._regex = (
            re.compile("|".join(f"({_phrase_pattern(p)})" for p in self.phrases))
            if self.phrases else None
        )

    def __bool__(self) -> bool:
        return self._regex is not None

    def search(self, text: str) -> str | None:
        """The configured phrase that matched first, or None."""
        if self._regex is None:
            return None
        match = self._regex.search(text)
        if match is None:
            return None
        return self.phrases[match.lastindex - 1]

    def matches(self, text: str) -> list[str]:
        if self._regex is None:
            return []
        found = []
        for match in self._regex.finditer(text):
            phrase = self.phrases[match.lastindex - 1]
            if phrase not in found:
                found.append(phrase)
        return found


@lru_cache(maxsize=512)
def matcher(phrases: tuple[str, ...]) -> PhraseMatcher:
    return PhraseMatcher(list(phrases))
