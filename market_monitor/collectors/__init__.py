"""Collectors. Importing this package registers every built-in collector."""

from market_monitor.collectors.base import (  # noqa: F401
    REGISTRY,
    CollectContext,
    CollectResult,
    Collector,
    CollectorError,
    build_collectors,
)
from market_monitor.collectors import rss  # noqa: F401  (registers itself)
