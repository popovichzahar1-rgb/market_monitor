"""Common collector interface.

Every source lives in its own module `collectors/<name>.py`, subclasses `Collector`,
sets `name` (equal to its key under `sources:` in config.yaml) and is decorated
with `@register`. A collector returns normalised `Item`s; it may report partial
failures in `CollectResult.errors` and should raise `CollectorError` only when it
produced nothing useful at all. The pipeline isolates each collector, so one
failing source never breaks the run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Protocol

from market_monitor.config import Config, Secrets, SourceConfig
from market_monitor.http import HttpClient
from market_monitor.models import Item


class StateStore(Protocol):
    def kv_get(self, key: str, default: str | None = None) -> str | None: ...
    def kv_set(self, key: str, value: str) -> None: ...


class CollectorError(Exception):
    pass


@dataclass
class CollectContext:
    http: HttpClient
    config: Config
    secrets: Secrets
    state: StateStore
    now: datetime


@dataclass
class CollectResult:
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Collector(ABC):
    name: ClassVar[str]

    def __init__(self, source_config: SourceConfig) -> None:
        self.source_config = source_config

    @abstractmethod
    def collect(self, ctx: CollectContext) -> CollectResult: ...


REGISTRY: dict[str, type[Collector]] = {}


def register(cls: type[Collector]) -> type[Collector]:
    if cls.name in REGISTRY:
        raise ValueError(f"duplicate collector name {cls.name!r}")
    REGISTRY[cls.name] = cls
    return cls


def build_collectors(config: Config, only: list[str] | None = None) -> list[Collector]:
    collectors: list[Collector] = []
    for name, cls in REGISTRY.items():
        source_config = getattr(config.sources, name, None)
        if source_config is None or not source_config.enabled:
            continue
        if only and name not in only:
            continue
        collectors.append(cls(source_config))
    return collectors
