"""Logging with secret redaction. Secrets must never reach logs."""

from __future__ import annotations

import logging
import re
import sys

_PATTERNS = [
    # Telegram bot token, also inside api.telegram.org/bot<token>/ URLs
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"),
    # api_key=..., token=... in query strings
    re.compile(r"(?i)((?:api_key|apikey|token|key)=)[^&\s\"']+"),
]


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 4]

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        for pattern in _PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and record.exc_info[1] is not None:
            # Render the traceback now so we can redact it, then drop exc_info.
            formatted = logging.Formatter().formatException(record.exc_info)
            record.msg = f"{record.msg}\n{self.redact(formatted)}"
            record.args, record.exc_info, record.exc_text = None, None, None
        return True


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full request URLs at INFO (which may contain the bot token or API keys).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
