"""Polite HTTP client: retries with backoff, per-host rate limiting, robots.txt."""

from __future__ import annotations

import logging
import re
import threading
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
        self.response: httpx.Response | None = None


class RobotsDisallowed(HttpError):
    pass


class BudgetExceeded(HttpError):
    """The calling collector ran out of its time budget; no more requests or waits."""


_BOT_TOKEN_PATH_RE = re.compile(r"/bot[^/]+/")


def _safe_url(url: str) -> str:
    """URL without query string or Telegram bot token, for logs and error messages."""
    parts = urlsplit(url)
    path = _BOT_TOKEN_PATH_RE.sub("/bot***/", parts.path)
    return f"{parts.scheme}://{parts.netloc}{path}"


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
        self._host_headers: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()  # collectors run in parallel threads
        self._local = threading.local()  # per-thread (= per-collector) time budget
        self._no_retry_429: set[str] = set()
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
        """Minimum delay between requests to a host (suffix match), e.g. GDELT: 1 req / 5 s."""
        self._host_interval[host.lower()] = seconds

    def no_retry_on_429(self, host: str) -> None:
        """Fail fast on 429 for hosts whose limits are long (retrying only burns time)."""
        self._no_retry_429.add(host.lower())

    def set_budget(self, seconds: float | None) -> None:
        """Time budget for requests made by the current thread (None = unlimited)."""
        self._local.deadline = None if seconds is None else self._clock() + seconds

    def remaining_budget(self) -> float | None:
        return self._remaining()

    def _remaining(self) -> float | None:
        deadline = getattr(self._local, "deadline", None)
        return None if deadline is None else deadline - self._clock()

    def _wait(self, seconds: float) -> None:
        """Sleep, but never past the budget: raise instead of waiting in vain."""
        remaining = self._remaining()
        if remaining is not None and seconds >= remaining:
            raise BudgetExceeded("time budget exceeded")
        if seconds > 0:
            self._sleep(seconds)

    def set_host_headers(self, host_suffix: str, headers: dict[str, str]) -> None:
        """Extra headers for every request to a host (suffix match), e.g. SEC's User-Agent."""
        self._host_headers[host_suffix.lower()] = headers

    def _headers_for(self, host: str, headers: dict | None) -> dict:
        merged: dict = {}
        for suffix, extra in self._host_headers.items():
            if host == suffix or host.endswith("." + suffix):
                merged.update(extra)
        merged.update(headers or {})
        return merged

    def _host_min_interval(self, host: str) -> float:
        for suffix, seconds in self._host_interval.items():
            if host == suffix or host.endswith("." + suffix):
                return seconds
        return self.config.per_host_min_interval_s

    def _throttle(self, host: str) -> None:
        """Reserve the next request slot for `host` under a lock, then sleep outside it,
        so parallel collectors never exceed a host's rate limit."""
        interval = self._host_min_interval(host)
        with self._lock:
            now = self._clock()
            last = self._last_request.get(host)
            slot = now if last is None else max(now, last + interval)
            self._last_request[host] = slot
        if slot > now:
            self._wait(slot - now)

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.config.respect_robots_txt:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: RobotFileParser | None = None
            try:
                host = parts.netloc.lower()
                self._throttle(host)
                resp = self._client.get(f"{origin}/robots.txt", headers=self._headers_for(host, None))
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                # 4xx/5xx or missing robots.txt: treat as "allowed", like major crawlers do.
            except httpx.HTTPError as exc:
                log.debug("robots.txt unavailable for %s: %s", origin, exc)
            self._robots[origin] = parser
        parser = self._robots[origin]
        if parser is None:
            return True
        agent = self._headers_for(parts.netloc.lower(), None).get("User-Agent", self.user_agent)
        return parser.can_fetch(agent, url)

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        check_robots: bool = True,
    ) -> httpx.Response:
        """GET with retries. Returns 2xx/304 responses; raises HttpError otherwise."""
        return self.request("GET", url, params=params, headers=headers, check_robots=check_robots)

    def post(self, url: str, *, json: dict | None = None, headers: dict | None = None,
             retry: bool = True) -> httpx.Response:
        """POST to an API (no robots.txt check). `retry=False` for non-idempotent calls."""
        return self.request("POST", url, json=json, headers=headers, check_robots=False,
                            retry=retry)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json: dict | None = None,
        check_robots: bool = True,
        retry: bool = True,
    ) -> httpx.Response:
        if check_robots and not self._allowed_by_robots(url):
            raise RobotsDisallowed(f"robots.txt disallows {_safe_url(url)}")
        host = urlsplit(url).netloc.lower()
        if (remaining := self._remaining()) is not None and remaining <= 0:
            raise BudgetExceeded(f"time budget exceeded before {_safe_url(url)}")
        attempts = self.config.retries + 1 if retry else 1
        last_error = ""
        for attempt in range(attempts):
            self._throttle(host)
            delay = self.config.backoff_s * (2**attempt)
            try:
                timeout = self.config.timeout_s
                if (remaining := self._remaining()) is not None:
                    timeout = max(1.0, min(timeout, remaining))
                resp = self._client.request(method, url, params=params, json=json,
                                            headers=self._headers_for(host, headers),
                                            timeout=timeout)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 300 or resp.status_code == 304:
                    return resp
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code == 429 and host in self._no_retry_429:
                    raise HttpError(f"{last_error} (rate limited) for {_safe_url(url)}", 429)
                if resp.status_code not in RETRY_STATUSES:
                    error = HttpError(f"{last_error} for {_safe_url(url)}", resp.status_code)
                    error.response = resp
                    raise error
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            if attempt < attempts - 1:
                delay = min(delay, self.config.max_retry_after_s)
                log.info("retrying %s in %.1fs (%s)", _safe_url(url), delay, last_error)
                try:
                    self._wait(delay)
                except BudgetExceeded:
                    raise BudgetExceeded(f"{last_error} for {_safe_url(url)}; time budget exceeded") from None
        raise HttpError(f"{last_error} for {_safe_url(url)} after {attempts} attempts")


def json_or_error(resp: httpx.Response) -> object:
    """Parse JSON, turning HTML/text error pages (common on rate limiting) into HttpError."""
    try:
        return resp.json()
    except ValueError as exc:
        snippet = resp.text[:120].replace("\n", " ")
        raise HttpError(f"invalid JSON from {_safe_url(str(resp.request.url))}: {snippet!r}") from exc
