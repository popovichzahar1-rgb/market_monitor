"""Small text helpers shared by collectors, dedup and scoring."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def clean_html(raw: str | None) -> str:
    """Strip tags and entities from an HTML fragment, returning plain text."""
    if not raw:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    return collapse_ws(html.unescape(text))


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut + "…"


def normalize_for_match(text: str) -> str:
    """Lowercase, unicode-normalise and drop punctuation; used for hashing and matching."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = _NON_WORD_RE.sub(" ", text)
    return collapse_ws(text)


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# " - Reuters", " | CNBC", " — Bloomberg": short trailing source names in headlines.
_SOURCE_SUFFIX_RE = re.compile(r"\s+[-|–—]\s+(?:\S+\s?){1,3}$")


def title_key(title: str) -> str:
    """Normalised headline used for fuzzy duplicate detection."""
    return normalize_for_match(_SOURCE_SUFFIX_RE.sub("", title))


def content_hash(text: str, min_chars: int = 120) -> str | None:
    """Hash of the normalised body text; None when the text is too short to be distinctive."""
    norm = normalize_for_match(text)
    if len(norm) < min_chars:
        return None
    return sha1(norm[:2000])


def distinguishing_tokens(title: str) -> set[str]:
    """Tokens that make a headline about a *different* event if they differ:
    numbers, acronyms (FED, ECB, BTC) and capitalised names (except the first word).
    Title-Case headlines make most words "distinguishing", which errs on the safe
    side (a missed merge costs less than a lost story).
    """
    words = _SOURCE_SUFFIX_RE.sub("", title).split()
    out = set()
    for index, word in enumerate(words):
        core = word.strip(".,;:!?\"'()[]«»“”‘’")
        if not core:
            continue
        if any(ch.isdigit() for ch in core) or (core.isupper() and len(core) >= 2) or (
            index > 0 and core[0].isupper()
        ):
            out.update(normalize_for_match(core).split())
    return out
