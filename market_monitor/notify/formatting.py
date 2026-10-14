"""All user-facing texts (Ukrainian), Telegram HTML parse mode.

Rules: escape every dynamic value with `escape`; keep each HTML tag closed within
one line (messages are split on line boundaries).
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import TYPE_CHECKING

from market_monitor.config import Config
from market_monitor.textutil import truncate

if TYPE_CHECKING:
    from market_monitor.alerts import AlertView
    from market_monitor.digest import DigestData
    from market_monitor.storage import SourceHealth

DISCLAIMER = "ℹ️ Лише інформація, не є фінансовою порадою. Рішення ухвалюєте ви."

TOPIC_ICONS = {"macro": "🏦", "crypto": "🪙", "geopolitics": "🌍", "equities": "🏢", "other": "⚡"}

FLAG_LABELS = {
    "central_bank_decision": "рішення центробанку",
    "central_bank_shock": "екстрені дії центробанку",
    "exchange_hack": "злам / зупинка виведення в крипті",
    "crypto_etf_decision": "рішення щодо крипто-ETF",
    "crypto_regulation": "регуляція крипти",
    "sanctions_major": "санкції",
    "energy_supply_shock": "удар по енергетиці / постачанню",
    "opec_decision": "рішення OPEC+",
    "crypto_price_move": "різкий рух ціни",
    "macro_release": "важливий макрореліз",
    "macro_move": "різкий рух показника",
    "major_quake": "сильний землетрус",
    "sec_8k": "новий 8-K",
    "watchlist": "компанія зі списку",
}


def plural(n: int, one: str, few: str, many: str) -> str:
    """Ukrainian plural: 1 джерело, 2 джерела, 5 джерел, 21 джерело."""
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def sources_count(n: int) -> str:
    return plural(n, "джерело", "джерела", "джерел")


def tone_icon(label: str | None) -> str:
    return {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(label or "", "")


def topic_label(config: Config, topic: str) -> str:
    t = config.scoring.topics.get(topic)
    return t.label if t else "Інше"


def topic_title(config: Config, topic: str) -> str:
    return f"{TOPIC_ICONS.get(topic, '•')} {topic_label(config, topic)}"


def flag_labels(flags: list[str]) -> list[str]:
    out = []
    for flag in flags:
        if flag.startswith("asset:"):
            out.append(flag.split(":", 1)[1])
        elif flag in FLAG_LABELS:
            out.append(FLAG_LABELS[flag])
    return list(dict.fromkeys(out))


def source_name(source: str) -> str:
    return source.split(":", 1)[1] if ":" in source else source


def local_time(config: Config, dt: datetime, fmt: str = "%H:%M") -> str:
    return dt.astimezone(config.tz).strftime(fmt)


# ---- alerts --------------------------------------------------------------------

def format_alert(view: "AlertView", config: Config) -> str:
    lines = [
        f"🚨 <b>{escape(topic_title(config, view.topic))}</b> · {view.score:.0f}/100 "
        f"{tone_icon(view.sentiment_label)}".rstrip(),
        f"<b><a href=\"{escape(view.url)}\">{escape(view.title)}</a></b>",
    ]
    if view.text and view.text != view.title:
        lines.append(escape(truncate(view.text, 280)))
    why = flag_labels(view.flags)
    if view.mentions > 1:
        why.append(sources_count(view.mentions))
    if why:
        lines.append(f"<i>Чому: {escape(' · '.join(why))}</i>")
    lines.append(f"{escape(source_name(view.source))} · {local_time(config, view.published_at)}")
    return "\n".join(lines)


def format_quiet_batch(views: list["AlertView"], config: Config) -> str:
    limit = config.alerts.quiet_batch_max
    lines = [f"🌙 <b>Алерти за тихі години ({len(views)})</b>", ""]
    for n, view in enumerate(views[:limit], start=1):
        icon = TOPIC_ICONS.get(view.topic, "•")
        lines.append(f"{n}. {icon} <b>{view.score:.0f}</b> "
                     f"<a href=\"{escape(view.url)}\">{escape(view.title)}</a>")
        why = flag_labels(view.flags)
        if why:
            lines.append(f"    <i>{escape(' · '.join(why))}</i>")
    if len(views) > limit:
        lines.append(f"… і ще {len(views) - limit} (див. дайджест)")
    return "\n".join(lines)


# ---- digest --------------------------------------------------------------------

def format_sources_health(health: list["SourceHealth"]) -> str:
    if not health:
        return "🛰 Джерела: ще не запускались"
    ok = [h for h in health if h.ok]
    line = f"🛰 Джерела: {len(ok)}/{len(health)} працюють"
    failed = [h for h in health if not h.ok]
    if failed:
        line += " · ❗ " + ", ".join(escape(h.source) for h in failed)
    partial = [h for h in ok if h.last_error]
    if partial:
        line += " · ⚠️ частково: " + ", ".join(escape(h.source) for h in partial)
    return line


def format_digest(data: "DigestData", config: Config) -> list[str]:
    """Digest as a list of message parts (each sent as its own message)."""
    date = local_time(config, data.now, "%d.%m.%Y")
    total = sum(len(v) for v in data.items_by_topic.values())
    header = [
        f"📊 <b>Щоденний дайджест · {date}</b>",
        f"<i>З {local_time(config, data.since, '%d.%m %H:%M')}: зібрано "
        f"{plural(data.collected, 'запис', 'записи', 'записів')}, у дайджесті {total}, "
        f"алертів: {data.alerts_sent}</i>",
    ]
    if data.market:
        header += ["", "📈 <b>Ринок</b>", " · ".join(escape(m) for m in data.market)]
    if data.calendar:
        header += ["", "📅 <b>Найближчі події</b>"]
        header += [f"• {escape(d)} — {escape(n)}" for d, n in data.calendar]

    body: list[str] = []
    if not total:
        body += ["", "Значущих подій за період не знайдено."]
    for topic, views in data.items_by_topic.items():
        if not views:
            continue
        body += ["", f"<b>{escape(topic_title(config, topic))}</b>"]
        for view in views:
            mark = "🚨" if view.item_id in data.alerted_ids else "•"
            extra = f" · {sources_count(view.mentions)}" if view.mentions > 1 else ""
            body.append(f"{mark} <b>{view.score:.0f}</b> {tone_icon(view.sentiment_label)} "
                        f"<a href=\"{escape(view.url)}\">{escape(view.title)}</a>"
                        f" <i>({escape(source_name(view.source))}{extra})</i>")

    watch: list[str] = []
    if data.watchlist is not None:
        watch += ["", "💼 <b>Мій список відстеження</b>"]
        for ticker, views in data.watchlist.items():
            watch.append(f"<b>{escape(ticker)}</b>")
            if not views:
                watch.append("    нічого нового")
            for view in views:
                watch.append(f"    • <b>{view.score:.0f}</b> "
                             f"<a href=\"{escape(view.url)}\">{escape(view.title)}</a>")

    footer = ["", format_sources_health(data.sources)]
    if data.rate_limited:
        footer.append(f"⏱ Через ліміт алертів не надіслано окремо: {data.rate_limited}")
    if config.muted_topics:
        footer.append("🔕 Вимкнені теми: " + ", ".join(
            escape(topic_label(config, t)) for t in config.muted_topics))
    footer += ["", DISCLAIMER]
    return ["\n".join(header + body + watch + footer)]


# ---- bot command replies --------------------------------------------------------

HELP = """<b>Market Monitor</b> — збирає й фільтрує новини, що впливають на ринки.
Лише інформація, жодної торгівлі.

/list — мій список відстеження
/watch TICKER — додати компанію (напр. /watch AAPL)
/unwatch TICKER — прибрати компанію
/topics — теми; /topics off crypto · /topics on crypto
/threshold — пороги алертів; /threshold 75 · /threshold urgent 60 · /threshold priority 65
/digest_time — час дайджесту; /digest_time 19:30
/status — стан джерел і алертів

Команди обробляються під час найближчого запуску (до ~30 хв)."""


def format_watchlist(config: Config) -> str:
    if not config.watchlist:
        return "Список відстеження порожній. Додайте компанію: /watch AAPL"
    lines = ["💼 <b>Список відстеження</b>"]
    for w in config.watchlist:
        name = f" — {escape(w.name)}" if w.name else ""
        lines.append(f"• <b>{escape(w.ticker)}</b>{name}")
    return "\n".join(lines)


def format_topics(config: Config) -> str:
    lines = ["<b>Теми</b> (вимкнені не потрапляють в алерти й дайджест)"]
    for key in config.scoring.topics:
        state = "🔕 вимкнено" if key in config.muted_topics else "🔔 увімкнено"
        lines.append(f"{TOPIC_ICONS.get(key, '•')} <code>{escape(key)}</code> — "
                     f"{escape(topic_label(config, key))}: {state}")
    lines.append("\nЗмінити: /topics off crypto або /topics on crypto")
    return "\n".join(lines)


def format_thresholds(config: Config) -> str:
    a = config.alerts
    quiet = f"{a.quiet_hours.start}–{a.quiet_hours.end}" if a.quiet_hours else "вимкнено"
    return "\n".join([
        "<b>Пороги алертів</b> (оцінка 0–100)",
        f"• загальний: <b>{a.threshold:.0f}</b> — /threshold 75",
        f"• термінові категорії: <b>{a.urgent_threshold:.0f}</b> — /threshold urgent 60",
        f"• мої компанії, BTC, ETH: <b>{a.priority_threshold:.0f}</b> — /threshold priority 65",
        f"• ліміт: {a.max_per_hour} на годину · тихі години: {quiet}",
    ])


def format_status(config: Config, health: list["SourceHealth"], stats: dict, now: datetime) -> str:
    lines = [f"🛠 <b>Стан</b> · {local_time(config, now, '%d.%m %H:%M')}",
             format_sources_health(health)]
    for h in health:
        if not h.ok or h.last_error:
            icon = "❗" if not h.ok else "⚠️"
            lines.append(f"{icon} <code>{escape(h.source)}</code>: "
                         f"{escape(truncate(h.last_error or '', 150))}")
    lines += [
        f"📥 Записів за 24 год: {stats.get('items_24h', 0)}",
        f"🚨 Алертів за 24 год: {stats.get('sent_24h', 0)} · у черзі: {stats.get('queued', 0)}"
        f" · понад ліміт: {stats.get('rate_limited_24h', 0)}",
        f"📊 Дайджест о {config.digest_time} ({escape(config.timezone)})"
        + (f", останній: {escape(stats['last_digest'])}" if stats.get("last_digest") else ""),
        f"🧠 Тон тексту: {escape(stats.get('sentiment', '?'))}",
        f"💼 Компаній у списку: {len(config.watchlist)}",
    ]
    return "\n".join(lines)
