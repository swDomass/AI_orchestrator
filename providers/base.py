"""Base class for all CLI providers."""

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
    _cooldown_until: float = 0.0

    def is_cooling_down(self) -> bool:
        return time.time() < self._cooldown_until

    def set_cooldown(self, seconds: int = PROVIDER_COOLDOWN_SEC) -> None:
        self._cooldown_until = time.time() + seconds
        remaining_min = seconds // 60
        print(f"  [{self.name}] Cooldown für {remaining_min} Min gesetzt.")

    def cooldown_remaining_str(self) -> str:
        remaining = max(0, self._cooldown_until - time.time())
        m, s = divmod(int(remaining), 60)
        return f"{m}m {s}s"

    @abstractmethod
    def run(self, task: str, cwd: str | None = None, timeout: int = TASK_TIMEOUT_SEC) -> RunResult:
        """Execute task via CLI and return result."""
        ...
