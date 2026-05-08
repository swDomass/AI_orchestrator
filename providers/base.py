"""Base class for all CLI providers."""

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from config import PROVIDER_COOLDOWN_SEC, TASK_TIMEOUT_SEC


@dataclass
class RunResult:
    success: bool
    output: str = ""
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    # Anthropic-style cache token fields (Claude only fills these; others stay 0).
    # Used for billing analytics + cache-hit-rate observability — NOT for 5h/7d
    # quota estimation (cache_creation/read are not counted against rate-limit
    # quota, see limits.py).
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class BaseProvider(ABC):
    name: str = "base"
    # Whether this provider supports CLI-level conversation sessions
    # (--session-id / --resume on Claude). Tools that want to share
    # conversation history across phases check this before generating
    # a session UUID. Codex and Gemini have CLI-level resume too, but
    # we don't currently exploit them — keeping the flag False until we
    # have empirical evidence of meaningful token savings there.
    supports_sessions: bool = False

    def __init__(self) -> None:
        self._cooldown_until: float = 0.0
        self._lock = threading.Lock()
        # Per-thread runtime context (providers are shared singleton instances).
        self._thread_ctx = threading.local()

    @property
    def _forced_model(self) -> str | None:
        return getattr(self._thread_ctx, "forced_model", None)

    @_forced_model.setter
    def _forced_model(self, value: str | None) -> None:
        self._thread_ctx.forced_model = value

    def is_cooling_down(self) -> bool:
        with self._lock:
            return time.time() < self._cooldown_until

    def set_cooldown(self, seconds: int = PROVIDER_COOLDOWN_SEC) -> None:
        with self._lock:
            self._cooldown_until = time.time() + seconds
        remaining_min = seconds // 60
        print(f"  [{self.name}] Cooldown für {remaining_min} Min gesetzt.")

    def cooldown_remaining_str(self) -> str:
        with self._lock:
            until = self._cooldown_until
        remaining = max(0, until - time.time())
        m, s = divmod(int(remaining), 60)
        return f"{m}m {s}s"

    def cooldown_remaining(self) -> float:
        """Return remaining seconds in cooldown."""
        with self._lock:
            until = self._cooldown_until
        return max(0.0, until - time.time())


    @abstractmethod
    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = TASK_TIMEOUT_SEC,
        read_only: bool = False,
        session_id: str | None = None,
        resume: bool = False,
    ) -> RunResult:
        """Execute task via CLI and return result.

        Providers should deny write-capable tools when ``read_only`` is set.

        ``session_id`` + ``resume`` enable cross-call conversation reuse on
        providers with ``supports_sessions = True`` (today: only Claude).
        Other providers MUST accept these parameters but may ignore them.
        Caller should check ``provider.supports_sessions`` before allocating
        a UUID to avoid wasted work.
        """
        ...
