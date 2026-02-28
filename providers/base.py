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


class BaseProvider(ABC):
    name: str = "base"

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
    def run(self, task: str, cwd: str | None = None, timeout: int = TASK_TIMEOUT_SEC) -> RunResult:
        """Execute task via CLI and return result."""
        ...
