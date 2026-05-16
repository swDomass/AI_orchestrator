"""Pre-Registration / Investigation Telegram-approval manager.

Mirrors the PolicyEngine pattern (``threading.Event`` + Telegram round-trip)
but is keyed by ``(run_id, criterion_id)`` so multiple thresholds within one
investigation can wait independently.

Also handles the Phase 8 final investigation-level approval, where
``criterion_id`` is the sentinel ``__investigation__``.

The TelegramListener (see ``telegram_listener.py``) calls ``respond()`` when
a ``/approve`` or ``/reject`` arrives; the tool's Phase 0.5 loop calls
``request_threshold_approval()`` and blocks on the event.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

# Sentinel criterion_id used for the Phase 8 final approval.
INVESTIGATION_CRITERION = "__investigation__"

ResponseKind = Literal["approved", "rejected", "skipped", "timeout", ""]


@dataclass
class _Pending:
    event: threading.Event
    response: ResponseKind = ""
    telegram_msg_id: str = ""
    approver: str = ""
    reason: str = ""


@dataclass
class PreRegApprovalManager:
    """Singleton-style manager. Use ``get_manager()`` rather than instantiating."""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pending: dict[tuple[str, str], _Pending] = field(default_factory=dict)

    def request_threshold_approval(
        self,
        *,
        run_id: str,
        criterion_id: str,
        timeout_sec: float,
    ) -> tuple[ResponseKind, str, str, str]:
        """Block until the user responds via Telegram or the timeout fires.

        Returns ``(response, telegram_msg_id, approver, reason)``. ``response``
        is one of ``"approved" | "rejected" | "skipped" | "timeout"``.

        Caller is expected to have ALREADY sent the Telegram approval request
        (via ``notifier``) before calling this method — this manager only
        coordinates the wait/response side.
        """
        key = (run_id, criterion_id)
        pending = _Pending(event=threading.Event())
        with self._lock:
            # If a duplicate request arrives while one is pending, replace it
            # with the new event so a future respond() matches the latest waiter.
            self._pending[key] = pending

        responded = pending.event.wait(timeout=timeout_sec)

        with self._lock:
            stored = self._pending.pop(key, None)

        if not responded:
            logger.info(
                "scientific-investigation: approval TIMEOUT for run=%s criterion=%s",
                run_id, criterion_id,
            )
            return "timeout", "", "", ""

        # Use the data set by respond(); ``stored`` should be the same object
        # but defend against races by reading both sources defensively.
        result = stored if stored is not None else pending
        logger.info(
            "scientific-investigation: approval %s for run=%s criterion=%s",
            result.response, run_id, criterion_id,
        )
        return (
            result.response or "timeout",
            result.telegram_msg_id,
            result.approver,
            result.reason,
        )

    def respond(
        self,
        *,
        run_id: str,
        criterion_id: str,
        response: ResponseKind,
        telegram_msg_id: str = "",
        approver: str = "",
        reason: str = "",
    ) -> bool:
        """Record a Telegram response. Returns True if a waiter was unblocked.

        Called by ``telegram_listener._handle_message`` when the user types
        ``/approve <run_id> [criterion_id]`` etc.
        """
        if response not in ("approved", "rejected", "skipped"):
            logger.warning("approval response invalid: %s", response)
            return False
        key = (run_id, criterion_id)
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                return False
            pending.response = response
            pending.telegram_msg_id = telegram_msg_id
            pending.approver = approver
            pending.reason = reason
        pending.event.set()
        return True

    def has_pending(self, run_id: str, criterion_id: str) -> bool:
        with self._lock:
            return (run_id, criterion_id) in self._pending

    def cancel_all(self) -> None:
        """Wake all pending waiters with a "skipped" response — used at shutdown."""
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()
        for _, pending in items:
            pending.response = "skipped"
            pending.event.set()


# ── Module-level singleton ──────────────────────────────────────────────────

_manager: PreRegApprovalManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> PreRegApprovalManager:
    """Lazy-init module-level singleton."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = PreRegApprovalManager()
    return _manager


def reset_manager_for_tests() -> None:
    """Test helper — drops the singleton so each test starts fresh."""
    global _manager
    with _manager_lock:
        _manager = None
