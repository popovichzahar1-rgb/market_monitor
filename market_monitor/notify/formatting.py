"""Message texts (Ukrainian). Telegram HTML parse mode."""

from __future__ import annotations

from html import escape
from zoneinfo import ZoneInfo

from market_monitor.pipeline import RunReport

DISCLAIMER = "ℹ️ Лише інформація, не є фінансовою порадою."


def format_run_preview(report: RunReport, tz: ZoneInfo, limit: int = 15) -> str:
    """Stage-1 preview of newly collected items (replaced by alerts/digest later)."""
    when = report.started_at.astimezone(tz).strftime("%d.%m %H:%M")
    lines = [f"<b>Market Monitor · нові записи ({when})</b>", ""]
    items = sorted(report.new_items, key=lambda i: i.published_at, reverse=True)
    if not items:
        lines.append("Нових записів немає.")
    for item in items[:limit]:
        ts = item.published_at.astimezone(tz).strftime("%H:%M")
        tags = f" <i>[{escape(', '.join(item.tags))}]</i>" if item.tags else ""
        lines.append(f"• {ts} <a href=\"{escape(item.url)}\">{escape(item.title)}</a>{tags}")
    if len(items) > limit:
        lines.append(f"… і ще {len(items) - limit}")
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
