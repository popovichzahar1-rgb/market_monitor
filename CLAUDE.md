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
- Run order (`cli.cmd_run`): bot commands → collect → score → alerts → digest if due.
- `settings.py`: bot-command overrides in kv (`override:*`) layered over config.yaml;
  always read config via `Settings.effective()` / `App.config`, never the raw base.
- `alerts.py`: decision order documented in its docstring; `alerts.item_id` is
  UNIQUE (never alert twice); quiet hours → `queued` → one batch message;
  over `max_per_hour` → `rate_limited` (digest only); send failure → re-queued.
  Dry-run records `dry_run` (not counted in the hourly limit).
- `digest.py`: `digest_due` = first run after local `digest_time` each day
  (robust to late cron). Watchlist block only when the watchlist is non-empty.
- `notify/telegram.py`: Bot API over `HttpClient.post`; messages split on line
  boundaries (each HTML tag must open and close within one line).
  `notify/commands.py`: getUpdates, allowlist = TELEGRAM_CHAT_ID only, offset saved
  per update; dry-run never saves the offset.
- `watchlist.py`: `WatchlistMatcher` (names/aliases any case; tickers case-sensitive,
  common-word/short tickers only as `$T`, `(NYSE: T)`, `NYSE:T`). Direct match →
  `item.entities`; peers/suppliers/sector_keywords → `item.meta["related"]`
  (bonus `watchlist_related_bonus`, never the `watchlist` flag). Runs as the
  `enrich` hook in `pipeline.collect` and again live in `digest.company_blocks`.
- SEC collector also stores `sec:latest:<T>` (latest filing per form, any age) and
  `sec:profile:<T>` (name, SIC industry) for the digest company block.
- `feedback.py`: alert messages carry 👍/👎 inline buttons (`fb:u|d:<item_id>`);
  `FeedbackHandler` (CommandProcessor `on_callback`) stores the vote in
  `alerts.feedback` and marks the chosen button (votes can be changed).
  `build_report` aggregates votes by rule/flag, source, topic and alert reason and
  produces *suggestions* only; never auto-tune config. Items with a vote survive
  `prune` (tuning history).
- Ukrainian plurals via `formatting.plural` (1 джерело, 2 джерела, 5 джерел).
- `pipeline.collect` runs collectors in a thread pool (`collect_workers`); collectors
  only touch state through `BufferedState` (flushed by the main thread), and all
  dedup/storage happens in the main thread under `storage.lock`, in collector order.
  `HttpClient._throttle` reserves per-host slots under a lock.
- CI (`.github/`): `monitor.yml` (30 min), `digest.yml`, `tests.yml`; composite
  actions `prepare` (deps, FinBERT cache/export, restore) and `persist` (pack, push).
  State = `state.py` (gzip + Fernet/scrypt) on the `data` branch as ONE commit,
  pushed with `--force-with-lease`. Persist only runs when restore succeeded
  (`STATE_RESTORED`) and never in dry-run. Never log or print decrypted state.
- `storage.py`: SQLite, schema via append-only `MIGRATIONS` list (`PRAGMA user_version`).
  Never edit an existing migration. Journal mode DELETE (single file, CI-friendly).
- `config.py`: pydantic models with `extra="forbid"`; config.yaml typos must fail.

## Testing
- `python -m pytest`, offline only (~4 s). Coverage: `python -m pytest --cov=market_monitor`
  (keep ≥ 90 %). HTTP via `httpx.MockTransport` (`make_http`,
  `route` in tests/conftest.py); fixtures in `tests/fixtures/`.
- Every collector and scoring rule gets fixture-based tests.
- `python -m market_monitor run --dry-run` prints Telegram messages instead of sending.
  Telegram in tests: `FakeTelegram` + `telegram_env` fixture (tests/conftest.py).
- `python -m market_monitor explain "headline" [--source rss:fed] [--tags crypto]`
  shows score breakdown and the alert decision; add a parametrized case to
  tests/test_scoring.py when changing rules (real headlines that misfired live
  belong in `test_live_false_positives_stay_below_alert`).
