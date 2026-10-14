# Market Monitor: project conventions

Personal pipeline: collect market-moving news → dedup → score → match watchlist →
Telegram alerts and daily digest. Information only, **never** trading logic.

## Hard rules
- Code, comments, identifiers, logs: English. User-facing bot texts: Ukrainian
  (keep them in `market_monitor/notify/formatting.py`). Every digest ends with
  the "не є фінансовою порадою" line.
- No LLM API calls at runtime. Everything must stay free (GitHub Actions, free APIs).
- Only legal sources: official APIs, RSS, open datasets. Respect robots.txt
  (`HttpClient` checks it), ToS and rate limits. No scraping of sites that forbid it.
- Secrets only from env (`Secrets.from_env`); never in code, config.yaml or logs.
  Logging goes through `RedactingFilter`; never log full URLs with query strings
  (use `http._safe_url`).
- One source failing must never break a run: collectors raise `CollectorError`
  only on total failure; the pipeline isolates each collector.

## Layout
- `market_monitor/collectors/<name>.py`: one module per source, subclass
  `Collector`, `@register`, `name` == key under `sources:` in config.yaml, and
  add a config model in `config.py`. Import the module in `collectors/__init__.py`.
- `models.Item` is the single normalised schema (id, source, published_at, title,
  text, url, tags, entities) plus `meta` for structured extras (price change,
  filing form, event date). `id` = sha1(normalised URL)[:16].
- Items generated from numbers (prices, FRED, USGS) carry the `data` tag: their
  titles are templated, so fuzzy title dedup is skipped for them. Repeated alerts
  for the same condition are suppressed by putting a time-window bucket in the URL.
- Data collectors keep a baseline in kv (e.g. `fred:last:<id>`): the first sighting
  never produces an item, so a fresh database doesn't flood the user.
- `dedup.py`: URL (incl. aliases) → body text hash → fuzzy headline (rapidfuzz)
  guarded by `distinguishing_tokens` (numbers/acronyms/names must match).
  Duplicates increment `mentions` of the canonical item instead of being stored.
- Per-host politeness: `http.set_host_interval` (GDELT 5.5 s, SEC 0.15 s),
  `http.set_host_headers` (SEC User-Agent with contact e-mail).
- `scoring/`: `rules.py` (keyword rules, source weights, data magnitudes, bonuses;
  every point recorded in `reasons`), `sentiment.py` (ONNX FinBERT, lazy-loaded,
  only for items with rule score >= `prefilter_min` and not `data`; lexicon
  fallback; a model failure degrades to lexicon, never fails the run),
  `keywords.py` (lowercase phrase = any case, uppercase letter = case-sensitive,
  `*` suffix wildcard). All weights live in config.yaml, not in code.
  Rules with `alert: true` put their name into `flags` (urgent categories).
- Re-scoring: when duplicates raise an older item's `mentions`, it is re-scored
  (stored sentiment reused). The same duplicate URL never counts twice.
- FinBERT model: `scripts/export_finbert.py` → `models/finbert-onnx/` (gitignored,
  cached in CI). Runtime deps are the optional `[ml]` extra (onnxruntime, tokenizers).
- `storage.py`: SQLite, schema via append-only `MIGRATIONS` list (`PRAGMA user_version`).
  Never edit an existing migration. Journal mode DELETE (single file, CI-friendly).
- `config.py`: pydantic models with `extra="forbid"`; config.yaml typos must fail.

## Testing
- `python -m pytest`, offline only. HTTP via `httpx.MockTransport` (`make_http`,
  `route` in tests/conftest.py); fixtures in `tests/fixtures/`.
- Every collector and scoring rule gets fixture-based tests.
- `python -m market_monitor run --dry-run` prints Telegram messages instead of sending.
- `python -m market_monitor explain "headline" [--source rss:fed] [--tags crypto]`
  shows score breakdown; add a parametrized case to tests/test_scoring.py when
  changing rules.
