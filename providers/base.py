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


# RunResult.error is an unconstrained string: providers put stable codes there
# ("rate_limit", "hang", ...) but ALSO raw stderr, exception text and prefixed
# details ("rate_limit: <detail>" from OpenRouter, "http_429: ..." from Gemini).
# Tools must therefore classify it, not compare it verbatim.
#
# Single source of truth: orchestrator.py imports this for its in-run backoff
# bail-out instead of repeating the tuple. A test pins the identity, so
# reintroducing a local copy there fails the suite.
TRANSIENT_ERRORS = ("rate_limit", "unreachable", "timeout", "hang", "stdin_incomplete")

# Codes a provider may emit with a ": <detail>" suffix instead of bare.
# "model_refusal" is a taxonomy category of its own — without it here, a Gemini
# refusal loses its code and is booked as a generic tool error.
# "http_<status>" is deliberately absent: it is not part of the stable taxonomy.
# "policy_block" is opencode's: the executing provider refused on data-protection
# grounds ("No endpoints found matching your data policy"). Without it here,
# error_code_of() returns "" for it and the code evaporates in analytics and the
# taxonomy — the resulting behaviour (not transient, so terminal) would still be
# right, but by accident rather than because the code was recognised.
_KNOWN_ERROR_CODES = TRANSIENT_ERRORS + (
    "session_missing", "auth_error", "network", "parse_error", "api_error", "model_refusal",
    "policy_block",
)


def error_code_of(error: str) -> str:
    """Extract the stable taxonomy code from a RunResult.error string.

    Returns "" for free-form prose (raw stderr, exception text) so the taxonomy
    falls through to its own classification instead of ingesting a whole
    stderr dump as an error_code.
    """
    if not error:
        return ""
    head = error.split(":", 1)[0].strip()
    return head if head in _KNOWN_ERROR_CODES else ""


def is_transient(error: str) -> bool:
    """True if the provider error is worth retrying.

    Matches bare codes and the "code: detail" form alike.
    """
    return error_code_of(error) in TRANSIENT_ERRORS


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

    @property
    def _forced_effort(self) -> str | None:
        """Reasoning-effort level for this thread's run, or None for the CLI's own
        session default. Set by the same callers that set `_forced_model`, with the
        same finally-restore discipline.

        Claude-only by construction: only providers/claude.py reads this, so every
        other provider ignores an `#effort:` tag without needing a capability check.
        Keeping it on BaseProvider (rather than on ClaudeProvider) is what lets the
        call sites stay provider-agnostic — exactly as with `_forced_model`, where
        model_id_for_provider() returns None for a non-owning provider.

        KNOWN GAP (do not mistake this for intended behaviour): the second-opinion pass in
        tools/review_loop.py and the pass-2 provider in tools/critical_review.py override
        only `_forced_model` for their secondary provider. When that secondary provider is
        the *same* Claude singleton as the primary, the outer `_forced_effort` is still in
        effect, so a `#effort:low` meant for a bulk task also lowers the effort of the
        review that checks it. Arguably it should be snapshotted and cleared around those
        calls — a review pass exists to add an independent perspective, not to inherit the
        primary task's cost tuning — but that is a behaviour change in two more tools and
        is deliberately NOT made here. Decide it separately; until then this docstring
        describes what the code does, not what it should do.
        """
        return getattr(self._thread_ctx, "forced_effort", None)

    @_forced_effort.setter
    def _forced_effort(self, value: str | None) -> None:
        self._thread_ctx.forced_effort = value

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
