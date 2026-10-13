"""Unified item schema that every collector normalises into."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from market_monitor.textutil import collapse_ws, sha1

# Query parameters that only carry tracking info and must not affect dedup.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "cmpid", "ncid", "taid", "yptr",
    "guccounter", "guce_referrer", "guce_referrer_sig", "mod", "rss", "feed",
}


def normalize_url(url: str) -> str:
    """Canonical form of a URL for dedup: no tracking params, fragment or www."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))


class Item(BaseModel):
    id: str = ""
    source: str
    published_at: datetime
    title: str
    text: str = ""
    url: str
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    # Structured extras from data sources (price change, filing form, event date...).
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("published_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0)

    @field_validator("title", "text")
    @classmethod
    def _clean(cls, value: str) -> str:
        return collapse_ws(value)

    @model_validator(mode="after")
    def _fill_id(self) -> "Item":
        if not self.id:
            self.id = sha1(self.url_norm)[:16]
        # Keep tags/entities unique and ordered for stable output.
        self.tags = list(dict.fromkeys(self.tags))
        self.entities = list(dict.fromkeys(self.entities))
        return self

    @property
    def url_norm(self) -> str:
        return normalize_url(self.url)

    @property
    def is_data(self) -> bool:
        """Generated from numeric data (prices, FRED): templated titles, no fuzzy dedup."""
        return DATA_TAG in self.tags


DATA_TAG = "data"
