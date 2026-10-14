"""Message texts (Ukrainian). Telegram HTML parse mode."""

from __future__ import annotations

from html import escape

from market_monitor.config import Config
from market_monitor.pipeline import RunReport

DISCLAIMER = "ℹ️ Лише інформація, не є фінансовою порадою."


def tone_icon(label: str | None) -> str:
    return {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(label or "", "")


def format_run_preview(report: RunReport, config: Config, limit: int = 20) -> str:
    """Interim preview of newly scored items grouped by topic (alerts/digest in stage 4)."""
    tz = config.tz
    when = report.started_at.astimezone(tz).strftime("%d.%m %H:%M")
    lines = [f"<b>Market Monitor · нові записи ({when})</b>"]
    scored = sorted(report.scored, key=lambda p: p[1].score, reverse=True)[:limit]
    if not scored:
        lines += ["", "Нових записів немає."]
    labels = {key: t.label for key, t in config.scoring.topics.items()}
    for topic_key in [*labels, None]:
        if topic_key is None:  # topics missing from config
            group = [(i, s) for i, s in scored if s.topic not in labels]
        else:
            group = [(i, s) for i, s in scored if s.topic == topic_key]
        if not group:
            continue
        lines += ["", f"<b>{escape(labels.get(topic_key, 'Інше'))}</b>"]
        for item, sc in group:
            urgent = "🚨 " if any(not f.startswith("asset:") and f != "watchlist" for f in sc.flags) else ""
            lines.append(
                f"{urgent}<b>{sc.score:.0f}</b> {tone_icon(sc.sentiment_label)} "
                f"<a href=\"{escape(item.url)}\">{escape(item.title)}</a>"
            )
    hidden = len(report.scored) - len(scored)
    if hidden > 0:
        lines.append(f"\n… і ще {hidden} із нижчою оцінкою")
    lines += ["", format_sources_line(report), "", DISCLAIMER]
    return "\n".join(lines)


def format_sources_line(report: RunReport) -> str:
    ok = sum(1 for s in report.sources if s.ok)
    line = f"Джерела: {ok}/{len(report.sources)} працюють"
    failed = report.failed_sources
    if failed:
        line += " · ❗ збій: " + ", ".join(escape(s.source) for s in failed)
    partial = [s for s in report.sources if s.ok and s.error]
    if partial:
        line += " · ⚠️ частково: " + ", ".join(escape(s.source) for s in partial)
    return line
