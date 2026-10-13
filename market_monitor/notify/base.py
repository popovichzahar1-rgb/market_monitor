"""Notifier interface. Dry-run prints messages instead of sending them."""

from __future__ import annotations

import sys
from typing import Protocol, TextIO


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class ConsoleNotifier:
    """Used with --dry-run: prints exactly what would be sent to Telegram."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)
        print("─" * 20 + " [dry-run] Telegram message " + "─" * 20, file=self.stream)
        print(text, file=self.stream)
        print("─" * 68, file=self.stream)
