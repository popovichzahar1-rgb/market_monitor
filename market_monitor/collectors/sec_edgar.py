"""SEC EDGAR: recent filings (8-K, 10-Q, 10-K, ...) for watchlist companies.

Uses the official JSON endpoints:
- https://www.sec.gov/files/company_tickers.json  (ticker -> CIK, cached daily)
- https://data.sec.gov/submissions/CIK##########.json
SEC fair-access policy requires a descriptive User-Agent with a contact e-mail
(SEC_CONTACT_EMAIL) and at most 10 requests per second.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from market_monitor.collectors.base import (
    CollectContext,
    CollectResult,
    Collector,
    CollectorError,
    StateStore,
    register,
)
from market_monitor.config import SecEdgarSourceConfig, WatchItem
from market_monitor.http import HttpClient, json_or_error
from market_monitor.models import Item

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
TICKERS_KEY = "sec:company_tickers"

# Human-readable meaning of the most market-relevant 8-K items.
FORM_8K_ITEMS = {
    "1.01": "material agreement",
    "1.02": "termination of material agreement",
    "1.03": "bankruptcy or receivership",
    "1.05": "material cybersecurity incident",
    "2.01": "acquisition or disposition of assets",
    "2.02": "results of operations (earnings)",
    "2.03": "new material financial obligation",
    "2.04": "triggering events accelerating obligations",
    "2.05": "exit or restructuring costs",
    "2.06": "material impairments",
    "3.01": "delisting notice",
    "4.01": "change of auditor",
    "4.02": "non-reliance on prior financial statements",
    "5.01": "change in control",
    "5.02": "departure/appointment of directors or officers",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
}


def sec_user_agent(contact_email: str) -> str:
    return f"MarketMonitor/0.1 personal-use {contact_email}"


def configure_http(http: HttpClient, contact_email: str, min_interval_s: float) -> None:
    for host in ("sec.gov",):
        http.set_host_headers(host, {"User-Agent": sec_user_agent(contact_email)})
        http.set_host_interval(host, min_interval_s)


def load_ticker_map(
    http: HttpClient, state: StateStore, now: datetime, cache_hours: int
) -> dict[str, dict]:
    """Ticker -> {cik, title}. Cached in the state store to avoid a 1 MB download every run."""
    cached = state.kv_get(TICKERS_KEY)
    if cached:
        data = json.loads(cached)
        fetched = datetime.fromisoformat(data["fetched_at"])
        if now - fetched < timedelta(hours=cache_hours):
            return data["map"]
    raw = json_or_error(http.get(TICKERS_URL))
    mapping = {
        str(row["ticker"]).upper(): {"cik": int(row["cik_str"]), "title": row["title"]}
        for row in (raw.values() if isinstance(raw, dict) else [])
    }
    if not mapping:
        raise CollectorError("empty SEC ticker map")
    state.kv_set(TICKERS_KEY, json.dumps({"fetched_at": now.isoformat(), "map": mapping}))
    return mapping


def describe_8k_items(items_field: str) -> list[str]:
    codes = [c.strip() for c in (items_field or "").split(",") if c.strip()]
    return [f"{c} {FORM_8K_ITEMS[c]}" for c in codes if c in FORM_8K_ITEMS]


def parse_filing_time(acceptance: str, filing_date: str) -> datetime:
    if acceptance:
        try:
            return datetime.fromisoformat(acceptance.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.fromisoformat(filing_date).replace(tzinfo=timezone.utc)


def parse_submissions(
    payload: dict, watch: WatchItem, cik: int, cfg: SecEdgarSourceConfig, now: datetime
) -> list[Item]:
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    company = payload.get("name") or watch.name or watch.ticker
    cutoff = now - timedelta(days=cfg.max_age_days)
    wanted = {f.upper() for f in cfg.forms}
    items: list[Item] = []
    for i, form in enumerate(forms):
        if form.upper() not in wanted:
            continue

        def col(name: str, default: str = "") -> str:
            values = recent.get(name) or []
            return values[i] if i < len(values) and values[i] is not None else default

        published = parse_filing_time(col("acceptanceDateTime"), col("filingDate"))
        if published < cutoff:
            continue
        accession = col("accessionNumber")
        document = col("primaryDocument")
        if not accession:
            continue
        details = describe_8k_items(col("items")) if form.upper() == "8-K" else []
        description = col("primaryDocDescription")
        title = f"{watch.ticker}: SEC {form} filed"
        if details:
            title += " — " + "; ".join(details)
        elif description and description.upper() != form.upper():
            title += f" — {description}"
        url = ARCHIVE_URL.format(cik=cik, accession=accession.replace("-", ""), document=document)
        items.append(
            Item(
                source="sec_edgar",
                published_at=min(published, now),
                title=title,
                text=f"{company} filed form {form} on {col('filingDate')}. "
                     + (f"Items: {', '.join(details)}." if details else ""),
                url=url,
                tags=["sec", "filing", f"form:{form.upper()}", "watchlist"],
                entities=[watch.ticker],
                meta={"form": form.upper(), "cik": cik, "accession": accession,
                      "items": col("items")},
            )
        )
    return items


@register
class SecEdgarCollector(Collector):
    name = "sec_edgar"
    source_config: SecEdgarSourceConfig

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        watchlist = ctx.config.watchlist
        if not watchlist:
            return result  # nothing to watch yet; not an error
        if not ctx.secrets.sec_contact_email:
            raise CollectorError("SEC_CONTACT_EMAIL is not set (required by SEC fair-access policy)")
        cfg = self.source_config
        configure_http(ctx.http, ctx.secrets.sec_contact_email, cfg.min_interval_s)
        ticker_map: dict[str, dict] | None = None
        for watch in watchlist:
            try:
                cik = int(watch.cik) if watch.cik else None
                if cik is None:
                    if ticker_map is None:
                        ticker_map = load_ticker_map(ctx.http, ctx.state, ctx.now, cfg.tickers_cache_hours)
                    entry = ticker_map.get(watch.ticker)
                    if not entry:
                        # e.g. EU-only listings: they simply have no SEC filings.
                        log.info("sec %s: not in SEC ticker list, skipped", watch.ticker)
                        continue
                    cik = entry["cik"]
                payload = json_or_error(ctx.http.get(SUBMISSIONS_URL.format(cik=f"{cik:010d}")))
                items = parse_submissions(payload, watch, cik, cfg, ctx.now)
                log.info("sec %s: %d recent filings", watch.ticker, len(items))
                result.items.extend(items)
            except Exception as exc:
                log.warning("sec %s failed: %s", watch.ticker, exc)
                result.errors.append(f"{watch.ticker}: {exc}")
        if len(result.errors) == len(watchlist):
            raise CollectorError("; ".join(result.errors[:3]))
        return result
