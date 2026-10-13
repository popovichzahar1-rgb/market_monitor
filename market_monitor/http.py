"""Polite HTTP client: retries with backoff, per-host rate limiting, robots.txt."""

from __future__ import annotations

import logging
import time
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from market_monitor.config import HttpConfig

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpError(Exception):
    """Request failed permanently (after retries, or a non-retryable status)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RobotsDisallowed(HttpError):
    pass


def _safe_url(url: str) -> str:
    """URL without query string, for log and error messages (queries may hold keys)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


class HttpClient:
    def __init__(
        self,
        config: HttpConfig,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.user_agent = user_agent or config.user_agent
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._client = httpx.Client(
            timeout=config.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            transport=transport,
        )

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def set_host_interval(self, host: str, seconds: float) -> None:
        """Override the minimum delay between requests to one host (e.g. SEC: 10 req/s max)."""
        self._host_interval[host.lower()] = seconds

    def _throttle(self, host: str) -> None:
        interval = self._host_interval.get(host, self.config.per_host_min_interval_s)
        last = self._last_request.get(host)
        if last is not None:
            wait = interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.config.respect_robots_txt:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: RobotFileParser | None = None
            try:
                self._throttle(parts.netloc.lower())
                resp = self._client.get(f"{origin}/robots.txt")
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                # 4xx/5xx or missing robots.txt: treat as "allowed", like major crawlers do.
            except httpx.HTTPError as exc:
                log.debug("robots.txt unavailable for %s: %s", origin, exc)
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(self.user_agent, url)

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        check_robots: bool = True,
    ) -> httpx.Response:
        """GET with retries. Returns 2xx/304 responses; raises HttpError otherwise."""
        if check_robots and not self._allowed_by_robots(url):
            raise RobotsDisallowed(f"robots.txt disallows {_safe_url(url)}")
        host = urlsplit(url).netloc.lower()
        attempts = self.config.retries + 1
        last_error = ""
        for attempt in range(attempts):
            self._throttle(host)
            delay = self.config.backoff_s * (2**attempt)
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 300 or resp.status_code == 304:
                    return resp
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRY_STATUSES:
                    raise HttpError(f"{last_error} for {_safe_url(url)}", resp.status_code)
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            if attempt < attempts - 1:
                delay = min(delay, self.config.max_retry_after_s)
                log.info("retrying %s in %.1fs (%s)", _safe_url(url), delay, last_error)
                self._sleep(delay)
        raise HttpError(f"{last_error} for {_safe_url(url)} after {attempts} attempts")
