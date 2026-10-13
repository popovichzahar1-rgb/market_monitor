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
