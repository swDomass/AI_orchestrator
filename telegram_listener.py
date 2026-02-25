"""
Bidirectional Telegram listener for the AI Orchestrator.

Runs as a daemon thread during --watch mode. Uses long-polling (getUpdates)
to receive messages — no webhooks, no extra dependencies.

Supported commands:
  /help    — list all commands
  /status  — queue size + per-provider limit summary
  /limits  — detailed per-provider limits (same as --check-limits output)
  /pause   — pause orchestrator task processing
  /resume  — resume orchestrator task processing

Any other text is forwarded to the best available AI provider and the
response is sent back to the same chat.
"""

import json
import logging
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_CHAT_THINKING_SEC,
    TELEGRAM_CHAT_TIMEOUT_SEC,
    TELEGRAM_ENABLED,
    TELEGRAM_MAX_TASK_LENGTH,
)
from dispatcher import select_provider
from limits import get_limits
from notifier import send_message
from queue_manager import append_task, read_queue

logger = logging.getLogger("telegram-listener")

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _fmt_time(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def _escape_telegram_markdown(text: str) -> str:
    """Escape Telegram legacy Markdown control chars in dynamic text."""
    escaped = []
    for ch in str(text):
        if ch in "\\_*`[]()":
            escaped.append("\\")
        escaped.append(ch)
    return "".join(escaped)


def _parse_command(text: str) -> tuple[str, str] | None:
    """Parse a Telegram command and optional argument string.

    Returns (command, args) with the command lowercased and any @bot suffix removed.
    For non-command text, returns None.
    """
    if not text.startswith("/"):
        return None

    head, sep, tail = text.partition(" ")
    command = head.split("@", 1)[0].lower()
    args = tail if sep else ""
    return command, args


def _api_get(method: str, params: dict) -> dict | None:
    """Call a Telegram Bot API GET method. Returns parsed JSON or None on error."""
    url = f"{_API_BASE}/{method}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=35) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("API error (%s): %s", method, e)
        return None


class _RateLimiter:
    """Simple sliding-window rate limiter (per-action)."""

    def __init__(self, max_calls: int, window_sec: float) -> None:
        self._max_calls = max_calls
        self._window = window_sec
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        import time
        now = time.time()
        with self._lock:
            self._timestamps = [t for t in self._timestamps if now - t < self._window]
            if len(self._timestamps) >= self._max_calls:
                return False
            self._timestamps.append(now)
            return True


class TelegramListener:
    """
    Long-poll Telegram for incoming messages and dispatch them.

    Thread safety:
    - pause_event is set/cleared only from the listener thread.
    - _chat_sem limits concurrent AI chat calls to 1.
    - select_provider / get_limits are safe after the base.py lock fix.
    """

    def __init__(self, pause_event: threading.Event) -> None:
        self._pause_event = pause_event
        self._stop_event = threading.Event()
        self._chat_sem = threading.Semaphore(1)
        self._thread = threading.Thread(target=self._poll_loop, name="TelegramListener", daemon=True)
        # Rate limiters: 20 commands/min, 5 AI chats/min, 10 task adds/min
        self._cmd_limiter = _RateLimiter(max_calls=20, window_sec=60)
        self._chat_limiter = _RateLimiter(max_calls=5, window_sec=60)
        self._task_limiter = _RateLimiter(max_calls=10, window_sec=60)

    def start(self) -> None:
        if not TELEGRAM_ENABLED:
            logger.info("Telegram nicht konfiguriert, Listener wird nicht gestartet.")
            return
        self._thread.start()
        logger.info("Gestartet (long-polling).")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    # ------------------------------------------------------------------
    # Internal: polling loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        offset = self._sync_to_latest_offset()
        if offset is None:
            return

        while not self._stop_event.is_set():
            resp = _api_get("getUpdates", {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": json.dumps(["message"]),
            })
            if resp is None or not resp.get("ok"):
                # Wait briefly before retrying to avoid hammering on persistent errors
                self._stop_event.wait(5)
                continue

            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if msg:
                    self._handle_message(msg)

    def _sync_to_latest_offset(self) -> int | None:
        """Drop pending backlog once on startup so only new commands are processed."""
        while not self._stop_event.is_set():
            resp = _api_get("getUpdates", {
                "offset": -1,
                "limit": 1,
                "timeout": 0,
                "allowed_updates": json.dumps(["message"]),
            })
            if resp is None or not resp.get("ok"):
                self._stop_event.wait(5)
                continue

            updates = resp.get("result", [])
            if updates:
                return updates[-1]["update_id"] + 1
            return 0
        return None

    # ------------------------------------------------------------------
    # Internal: message dispatch
    # ------------------------------------------------------------------

    def _handle_message(self, msg: dict) -> None:
        """Route an incoming message to command handler or AI chat."""
        # Security: only accept messages from the configured chat
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID):
            return

        text = (msg.get("text") or "").strip()
        if not text:
            return

        parsed_command = _parse_command(text)

        # Commands (rate-limited)
        if parsed_command:
            command, command_args = parsed_command
            if command == "/task":
                if not self._task_limiter.allow():
                    send_message("⏳ Zu viele Task-Anfragen. Bitte kurz warten.")
                    return
                if not command_args.strip():
                    send_message("ℹ️ Verwendung: `/task Beschreibung des Tasks`")
                else:
                    self._cmd_add_task(command_args)
            elif not self._cmd_limiter.allow():
                send_message("⏳ Zu viele Befehle. Bitte kurz warten.")
            elif command == "/help":
                self._cmd_help()
            elif command == "/status":
                self._cmd_status()
            elif command == "/limits":
                self._cmd_limits()
            elif command == "/pause":
                self._cmd_pause()
            elif command == "/resume":
                self._cmd_resume()
            return

        # AI chat (rate-limited, spawned in worker thread)
        if not self._chat_limiter.allow():
            send_message("⏳ Zu viele KI-Anfragen. Bitte kurz warten.")
            return
        t = threading.Thread(target=self._handle_chat, args=(text,), daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _cmd_help(self) -> None:
        send_message(
            "🤖 *AI Orchestrator – Befehle*\n\n"
            "/task \\<beschreibung\\> — Task zur Queue hinzufügen\n"
            "/status — Queue-Größe + Provider-Übersicht\n"
            "/limits — Detaillierte Provider-Limits\n"
            "/pause  — Task-Verarbeitung pausieren\n"
            "/resume — Task-Verarbeitung fortsetzen\n"
            "/help   — Diese Hilfe\n\n"
            "Beliebiger Text → sofortige KI-Antwort"
        )

    def _cmd_status(self) -> None:
        tasks = read_queue()
        limits = get_limits()

        lines = [f"📊 *Status*\n\nQueue: {len(tasks)} offene Task(s)\n"]
        for name in ("claude", "gemini", "codex"):
            lim = getattr(limits, name)
            if lim.available:
                status = f"✅ {lim.remaining_pct:.1f}%"
                reset = f" (reset ~{_fmt_time(lim.resets_in_sec)})" if lim.resets_in_sec else ""
                lines.append(f"  {name}: {status}{reset}")
            else:
                lines.append(f"  {name}: ❌ {lim.error or 'nicht verfügbar'}")

        paused = " ⏸️ PAUSIERT" if self._pause_event.is_set() else ""
        lines.append(f"\nOrchestrator: {'läuft' + paused}")
        send_message("\n".join(lines))

    def _cmd_limits(self) -> None:
        limits = get_limits()
        lines = ["📋 *Provider Limits*\n"]
        for name in ("claude", "gemini", "codex"):
            lim = getattr(limits, name)
            if lim.available:
                reset = f", reset in {_fmt_time(lim.resets_in_sec)}" if lim.resets_in_sec else ""
                lines.append(f"  {name}: {lim.remaining_pct:.1f}% remaining{reset}")
            else:
                lines.append(f"  {name}: ❌ {lim.error or 'nicht verfügbar'}")
        send_message("\n".join(lines))

    def _cmd_pause(self) -> None:
        self._pause_event.set()
        send_message("⏸️ Orchestrator *pausiert*.\nNeue Tasks werden nicht verarbeitet.\n/resume zum Fortsetzen.")
        logger.info("Orchestrator durch Telegram pausiert.")

    def _cmd_resume(self) -> None:
        self._pause_event.clear()
        send_message("▶️ Orchestrator *läuft wieder*.")
        logger.info("Orchestrator durch Telegram fortgesetzt.")

    def _cmd_add_task(self, task_text: str) -> None:
        task_text = task_text.strip()
        if not task_text:
            send_message("ℹ️ Verwendung: `/task Beschreibung des Tasks`")
            return
        if "\n" in task_text or "\r" in task_text:
            send_message("❌ `/task` unterstützt nur einzeilige Aufgaben.")
            return
        if len(task_text) > TELEGRAM_MAX_TASK_LENGTH:
            send_message(f"❌ Task zu lang ({len(task_text)} Zeichen, max {TELEGRAM_MAX_TASK_LENGTH}).")
            return
        # Reject tasks containing control characters (except normal whitespace)
        if any(ord(ch) < 32 and ch not in ("\n", "\r", "\t") for ch in task_text):
            send_message("❌ Task enthält ungültige Zeichen.")
            return
        append_task(task_text)
        safe = task_text[:100].replace("`", "'")
        send_message(f"✅ Task zur Queue hinzugefügt:\n`{safe}`")
        logger.info("Task hinzugefügt: %s", task_text[:60])

    # ------------------------------------------------------------------
    # AI chat
    # ------------------------------------------------------------------

    def _handle_chat(self, text: str) -> None:
        """Forward text to best available provider and reply. Runs in worker thread."""
        acquired = self._chat_sem.acquire(blocking=False)
        if not acquired:
            send_message("⏳ Bereits eine KI-Anfrage läuft – bitte kurz warten...")
            return

        logger.info("Chat-Anfrage erhalten: %s", text[:80])

        try:
            limits = get_limits()
            tried_providers: set[str] = set()

            while True:
                provider = select_provider(text, limits, exclude=tried_providers)

                if provider is None:
                    if not tried_providers:
                        logger.warning("Kein Provider verfügbar")
                        send_message("❌ Kein Provider verfügbar (alle voll oder im Cooldown).")
                    else:
                        logger.warning("Alle Provider fehlgeschlagen: %s", tried_providers)
                        send_message("❌ Alle Provider fehlgeschlagen.")
                    return

                # Defensive guard: avoid infinite retry loops if select_provider
                # ignores the exclude set (e.g. in tests/mocks or future regressions).
                if provider.name in tried_providers:
                    logger.error("Provider-Auswahl wiederholt sich: %s", provider.name)
                    send_message("❌ Interner Fehler: Provider-Auswahl wiederholt sich (Retry abgebrochen).")
                    return

                tried_providers.add(provider.name)
                logger.info("Versuche Provider: %s", provider.name)
                send_message(f"⏳ Frage {provider.name}...")

                # Run provider in a thread so we can send a "thinking" notification
                result_holder: list = []
                error_holder: list = []

                def _run_provider():
                    try:
                        result_holder.append(provider.run(text, timeout=TELEGRAM_CHAT_TIMEOUT_SEC))
                    except Exception as exc:
                        error_holder.append(exc)

                worker = threading.Thread(target=_run_provider, daemon=True)
                worker.start()

                # Phase 1: wait THINKING_SEC, send notification if still running
                worker.join(timeout=TELEGRAM_CHAT_THINKING_SEC)
                if worker.is_alive():
                    logger.info("%s denkt noch nach (>%ds)", provider.name, TELEGRAM_CHAT_THINKING_SEC)
                    send_message(f"⏳ {provider.name} denkt noch nach...")
                    # Phase 2: wait for remaining time
                    remaining = TELEGRAM_CHAT_TIMEOUT_SEC - TELEGRAM_CHAT_THINKING_SEC
                    worker.join(timeout=max(0, remaining))

                if worker.is_alive():
                    logger.warning("%s Timeout nach %ds", provider.name, TELEGRAM_CHAT_TIMEOUT_SEC)
                    # Thread is stuck — we can't kill it, but we move on to next provider
                    err = _escape_telegram_markdown("Timeout")
                    send_message(f"⚠️ {provider.name} fehlgeschlagen: {err} – versuche anderen Provider...")
                    continue

                if error_holder:
                    raise error_holder[0]

                result = result_holder[0]

                if result.success:
                    logger.info("Antwort von %s erhalten (%d Zeichen)", provider.name, len(result.output))
                    provider_name = _escape_telegram_markdown(provider.name)
                    reply = _escape_telegram_markdown(result.output)
                    header = f"🤖 *{provider_name}*:\n\n"
                    reply = reply[: max(0, 4096 - len(header))]
                    send_message(f"{header}{reply}")
                    return
                else:
                    logger.warning("%s fehlgeschlagen: %s", provider.name, result.error)
                    err = _escape_telegram_markdown(str(result.error))
                    send_message(f"⚠️ {provider.name} fehlgeschlagen: {err} – versuche anderen Provider...")
        except Exception as e:
            logger.error("Interner Fehler bei Chat-Verarbeitung: %s", e, exc_info=True)
            send_message(f"❌ Interner Fehler: {_escape_telegram_markdown(str(e))}")
        finally:
            self._chat_sem.release()
