"""Bot commands, processed via getUpdates at the start of every run.

Only messages from TELEGRAM_CHAT_ID are handled (allowlist); everything else is
ignored without a reply. The update offset is stored in the database after each
update, so a crash never replays already handled commands.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from market_monitor.config import WatchItem
from market_monitor.notify import formatting as fmt
from market_monitor.notify.base import Notifier
from market_monitor.notify.telegram import TelegramClient, TelegramError
from market_monitor.settings import THRESHOLD_KINDS, Settings

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram:offset"
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")

# ticker -> (company name, CIK) or None when unknown
Lookup = Callable[[str], "tuple[str, int | None] | None"]
CallbackHandler = Callable[[dict], str]


class CommandProcessor:
    def __init__(self, client: TelegramClient, chat_id: str, settings: Settings,
                 notifier: Notifier, status: Callable[[], str],
                 lookup: Lookup | None = None, on_callback: CallbackHandler | None = None,
                 save_offset: bool = True) -> None:
        self.client = client
        self.chat_id = str(chat_id)
        self.settings = settings
        self.notifier = notifier
        self.status = status
        self.lookup = lookup
        self.on_callback = on_callback
        self.save_offset = save_offset

    def process(self) -> int:
        """Handle pending updates; returns the number of handled commands."""
        storage = self.settings.storage
        raw = storage.kv_get(OFFSET_KEY)
        try:
            updates = self.client.get_updates(int(raw) if raw else None)
        except TelegramError as exc:
            log.error("getUpdates failed: %s", exc)
            return 0
        handled = 0
        for update in updates:
            try:
                handled += self._handle_update(update)
            except Exception:
                log.exception("failed to handle update %s", update.get("update_id"))
            if self.save_offset:
                storage.kv_set(OFFSET_KEY, str(update["update_id"] + 1))
        return handled

    def _authorized(self, chat: dict | None) -> bool:
        return chat is not None and str(chat.get("id")) == self.chat_id

    def _handle_update(self, update: dict) -> int:
        if (message := update.get("message")) is not None:
            if not self._authorized(message.get("chat")):
                log.warning("ignored update from a chat that is not allowlisted")
                return 0
            text = (message.get("text") or "").strip()
            reply = self.handle(text) if text.startswith("/") else \
                "Я розумію лише команди. /start — список команд."
            self.notifier.send(reply)
            return 1
        if (callback := update.get("callback_query")) is not None:
            if not self._authorized((callback.get("message") or {}).get("chat")):
                log.warning("ignored callback from a chat that is not allowlisted")
                return 0
            answer = self.on_callback(callback) if self.on_callback else ""
            try:
                self.client.answer_callback(callback["id"], answer)
            except TelegramError as exc:
                log.warning("answerCallbackQuery failed: %s", exc)
            return 1
        return 0

    def handle(self, text: str) -> str:
        command, *args = text.split()
        command = command.split("@", 1)[0].lower()
        handler = {
            "/start": self.cmd_help, "/help": self.cmd_help, "/list": self.cmd_list,
            "/watch": self.cmd_watch, "/unwatch": self.cmd_unwatch, "/topics": self.cmd_topics,
            "/threshold": self.cmd_threshold, "/digest_time": self.cmd_digest_time,
            "/status": self.cmd_status,
            "/peers": self.cmd_peers, "/suppliers": self.cmd_suppliers,
            "/sector": self.cmd_sector, "/alias": self.cmd_alias,
        }.get(command)
        if handler is None:
            return "Невідома команда. /start — список команд."
        try:
            return handler(args)
        except Exception:
            log.exception("command %s failed", command)
            return "⚠️ Не вдалося виконати команду. Деталі в логах запуску."

    # ---- commands ------------------------------------------------------------

    def cmd_help(self, args: list[str]) -> str:
        return fmt.HELP

    def cmd_list(self, args: list[str]) -> str:
        return fmt.format_watchlist(self.settings.effective())

    def cmd_watch(self, args: list[str]) -> str:
        if not args:
            return "Вкажіть тикер: /watch AAPL"
        ticker = args[0].upper()
        if not TICKER_RE.match(ticker):
            return f"«{fmt.escape(args[0])}» не схоже на тикер (латиниця, цифри, крапка, дефіс; до 10 символів)."
        found = None
        if self.lookup is not None:
            try:
                found = self.lookup(ticker)
            except Exception as exc:
                log.warning("ticker lookup failed: %s", exc)
        name, cik = found if found else ("", None)
        extra_aliases = split_list(" ".join(args[1:]), spaces=False)
        aliases = [a for a in [short_company_name(name), *extra_aliases] if a]
        item = WatchItem(ticker=ticker, name=name, cik=str(cik) if cik else None,
                         aliases=list(dict.fromkeys(aliases)))
        if not self.settings.add_watch(item):
            return f"{ticker} уже є у списку. /list — переглянути."
        label = f"{ticker} ({fmt.escape(name)})" if name else ticker
        note = "" if found else (
            "\nℹ️ Тикер не знайдено в базі SEC (можливо, компанія не з США): "
            "новини шукатимуться за тикером і назвами, документів SEC не буде. "
            f"Додайте назву: /alias {ticker} Назва компанії")
        if item.aliases:
            note += f"\nШукаю в новинах: {fmt.escape(', '.join([ticker, *item.aliases]))}"
        return (f"✅ Додано {label}. Новини про компанію з'являтимуться в алертах і дайджесті.{note}"
                f"\n\nЩоб бачити пов'язані події: /peers {ticker} …, /suppliers {ticker} …, "
                f"/sector {ticker} …")

    def cmd_unwatch(self, args: list[str]) -> str:
        if not args:
            return "Вкажіть тикер: /unwatch AAPL"
        ticker = args[0].upper()
        if self.settings.remove_watch(ticker):
            return f"🗑 {fmt.escape(ticker)} прибрано зі списку."
        return f"{fmt.escape(ticker)} немає у списку. /list — переглянути."

    def cmd_topics(self, args: list[str]) -> str:
        config = self.settings.effective()
        if len(args) >= 2 and args[0].lower() in ("on", "off"):
            topic = args[1].lower()
            if topic not in config.scoring.topics:
                return f"Невідома тема «{fmt.escape(topic)}».\n\n" + fmt.format_topics(config)
            self.settings.set_topic_muted(topic, args[0].lower() == "off")
            config = self.settings.effective()
        elif args:
            return "Формат: /topics off crypto або /topics on crypto"
        return fmt.format_topics(config)

    def cmd_threshold(self, args: list[str]) -> str:
        if args:
            kind, raw = ("general", args[0]) if len(args) == 1 else (args[0].lower(), args[1])
            if kind not in THRESHOLD_KINDS:
                return "Тип порогу: urgent або priority (або без типу для загального)."
            try:
                self.settings.set_threshold(kind, float(raw.replace(",", ".")))
            except ValueError:
                return "Поріг — число від 0 до 100, напр. /threshold 75"
        return fmt.format_thresholds(self.settings.effective())

    def cmd_digest_time(self, args: list[str]) -> str:
        if args:
            try:
                value = self.settings.set_digest_time(args[0])
            except ValueError:
                return "Формат часу: ГГ:ХХ, напр. /digest_time 19:30"
            config = self.settings.effective()
            return (f"🕗 Дайджест щодня о {value} ({fmt.escape(config.timezone)}). "
                    "Зважте: запуски за розкладом можуть запізнюватися на кілька хвилин.")
        config = self.settings.effective()
        return f"🕗 Дайджест щодня о {config.digest_time} ({fmt.escape(config.timezone)}). Змінити: /digest_time 19:30"

    def cmd_status(self, args: list[str]) -> str:
        return self.status()

    def _set_list(self, args: list[str], field: str, label: str, example: str) -> str:
        if not args:
            return f"Формат: /{example}\nОчистити: /{example.split()[0]} {example.split()[1]} -"
        ticker = args[0].upper()
        raw = " ".join(args[1:]).strip()
        # Kept as typed: "MSFT" (uppercase) is matched as a ticker, "Foxconn" as a name.
        values = [] if raw in ("", "-") else split_list(raw, spaces=field in ("peers", "suppliers"))
        updated = self.settings.update_watch(ticker, **{field: values})
        if updated is None:
            return f"{fmt.escape(ticker)} немає у списку. Спершу: /watch {fmt.escape(ticker)}"
        shown = ", ".join(values) if values else "—"
        return f"✅ {fmt.escape(ticker)}: {label} — {fmt.escape(shown)}"

    def cmd_peers(self, args: list[str]) -> str:
        return self._set_list(args, "peers", "конкуренти", "peers AAPL MSFT, GOOGL, Samsung")

    def cmd_suppliers(self, args: list[str]) -> str:
        return self._set_list(args, "suppliers", "постачальники", "suppliers AAPL TSM, Foxconn")

    def cmd_sector(self, args: list[str]) -> str:
        return self._set_list(args, "sector_keywords", "ключові слова сектору",
                              "sector XOM oil price, refining, OPEC")

    def cmd_alias(self, args: list[str]) -> str:
        return self._set_list(args, "aliases", "назви для пошуку", "alias AAPL Apple, iPhone maker")


def split_list(raw: str, spaces: bool = True) -> list[str]:
    """Split by commas if present; otherwise by spaces (tickers: 'MSFT GOOGL') or,
    with spaces=False, keep the whole text as one phrase ('iPhone maker')."""
    if "," in raw:
        parts = raw.split(",")
    else:
        parts = raw.split() if spaces else [raw]
    return list(dict.fromkeys(p.strip() for p in parts if p.strip()))


_SUFFIX_RE = re.compile(r"[,.]?\s+(inc|corp|corporation|co|ltd|plc|nv|n\.v|sa|ag|se|holdings?|group|company)\.?$",
                        re.IGNORECASE)


def short_company_name(name: str) -> str:
    """'Apple Inc.' -> 'Apple'; 'MICROSOFT CORP' -> 'Microsoft'. Used as a news alias."""
    if not name:
        return ""
    short = name.strip()
    for _ in range(2):
        short = _SUFFIX_RE.sub("", short).strip(" ,.")
    if short.isupper() and (" " in short or len(short) > 5):  # keep acronyms like ASML
        short = short.title()
    return short if len(short) >= 3 else ""
