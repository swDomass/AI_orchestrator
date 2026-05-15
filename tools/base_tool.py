"""Base class for orchestrator tools.

Tools are multi-step workflows that go beyond single CLI calls.
They run iterative loops (review→fix→recheck) and report progress.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import time
import uuid
from pathlib import Path

from config import get_system_prompt
from providers.base import BaseProvider

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    success: bool
    output: str = ""
    iterations: int = 0
    error: str = ""
    error_code: str = ""
    retryable: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    # Anthropic prompt-cache fields (Claude only — others stay 0).
    # Aggregated across all phases/iterations within a tool run.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class SessionContext:
    """Manages CLI session lifecycle for tools that share conversation history
    across phases (Claude --session-id / --resume).

    Usage:
        sess = SessionContext.create(provider, tool_name="dev-loop", cwd=cwd)
        # First call of a (sub-)session: starts a new conversation
        result = provider.run(prompt, **sess.first_call_kwargs())
        # Subsequent calls: resume the same conversation
        result = provider.run(prompt2, **sess.resume_kwargs())
        sess.bump()  # increment iteration counter
        if sess.needs_rollover():
            handover = sess.handover_summary()
            sess.rollover(tool_name="dev-loop", cwd=cwd)
            # Use handover as first user message in the new session

    When provider.supports_sessions is False or CLAUDE_SESSION_ENABLED is off,
    all helpers return empty dicts → caller falls back to today's stateless
    subprocess pattern transparently.
    """
    enabled: bool = False
    uuid: str | None = None
    iteration_count: int = 0
    cap: int = 5  # max iterations per session before rollover

    @classmethod
    def create(
        cls,
        provider: object,
        tool_name: str,
        cwd: str | None,
        cap: int = 5,
    ) -> "SessionContext":
        """Build a session context, allocating a UUID if the provider supports it
        and the global feature flag is enabled. Registers the UUID in the
        sidecar so heartbeat-cleanup can recognize it as orchestrator-created.
        """
        from config import CLAUDE_SESSION_ENABLED
        supports = bool(getattr(provider, "supports_sessions", False))
        if not (supports and CLAUDE_SESSION_ENABLED):
            return cls(enabled=False, cap=cap)
        import uuid as _uuid
        sid = str(_uuid.uuid4())
        try:
            from session_registry import register_session
            register_session(sid, tool_name, cwd or "")
        except (ImportError, OSError) as exc:  # pragma: no cover
            logger.warning("Session registry unavailable: %s", exc)
        return cls(enabled=True, uuid=sid, cap=cap)

    def first_call_kwargs(self) -> dict:
        """kwargs for provider.run() that STARTS a new session."""
        if self.enabled and self.uuid:
            return {"session_id": self.uuid, "resume": False}
        return {}

    def resume_kwargs(self) -> dict:
        """kwargs for provider.run() that CONTINUES the current session."""
        if self.enabled and self.uuid:
            return {"session_id": self.uuid, "resume": True}
        return {}

    def bump(self) -> None:
        self.iteration_count += 1

    def needs_rollover(self) -> bool:
        """True when the cap is reached and a fresh session should be started."""
        return self.enabled and self.cap > 0 and self.iteration_count >= self.cap

    def rollover(self, tool_name: str, cwd: str | None) -> None:
        """Allocate a new UUID and reset the iteration counter. Old session's
        registry entry stays for heartbeat-cleanup; new UUID is registered."""
        if not self.enabled:
            return
        import uuid as _uuid
        new_uuid = str(_uuid.uuid4())
        try:
            from session_registry import register_session
            register_session(new_uuid, tool_name, cwd or "")
        except (ImportError, OSError):  # pragma: no cover
            pass
        self.uuid = new_uuid
        self.iteration_count = 0


@dataclass
class TokenCounter:
    """Aggregates token counts (input/output + Anthropic cache fields) across
    tool phases. Use ``.add(result)`` after each ``provider.run()`` and pass
    ``**counter.as_kwargs()`` when constructing the final ToolResult.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, result: object) -> None:
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            setattr(self, field, getattr(self, field) + getattr(result, field, 0))

    def as_kwargs(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


@dataclass
class ToolTracer:
    """Structured JSONL action trace for a single tool run.

    One line per event in {cwd}/.{tool_name}/traces/{run_id}.jsonl. Disabled
    silently when cwd is None or directory creation fails — tools must never
    break because tracing is unavailable.

    Usage:
        tracer = ToolTracer.create(self.name, cwd)
        tracer.emit("run_start", task=task[:200], provider=provider.name)
        # ... at each phase / subprocess boundary:
        tracer.emit("subprocess_call", phase="agent_pentester", prompt_chars=len(p))
        result = provider.run(...)
        tracer.emit("subprocess_result", phase="agent_pentester",
                    success=not result.error,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens)
        tracer.emit("run_end", success=tool_result.success)

    Suggested action vocabulary (open-ended — `details` accepts any kwargs):
        run_start / run_end
        phase_start / phase_end                         (details: phase=...)
        iteration_start / iteration_end                 (details: iteration=N)
        subprocess_call / subprocess_result             (details: phase, tokens)
        session_rollover                                (details: old_uuid, new_uuid)
        capacity_exhausted                              (details: phase, agent)
        roundtable_start / roundtable_persona_*         (deep-security-audit)
    """
    tool_name: str
    run_id: str
    trace_file: Path | None = None  # None when disabled
    start_time: float = field(default_factory=time.time)

    @classmethod
    def create(cls, tool_name: str, cwd: str | None) -> "ToolTracer":
        """Build a tracer for a tool run. Allocates a UUID and the trace file
        path. If cwd is missing or the directory cannot be created, the tracer
        becomes a silent no-op (emit() returns immediately).
        """
        run_id = str(uuid.uuid4())
        trace_file: Path | None = None
        if cwd:
            try:
                trace_dir = Path(cwd) / f".{tool_name}" / "traces"
                trace_dir.mkdir(parents=True, exist_ok=True)
                trace_file = trace_dir / f"{run_id}.jsonl"
            except OSError as exc:
                logger.warning("Tool trace setup failed for %s: %s", tool_name, exc)
        return cls(tool_name=tool_name, run_id=run_id, trace_file=trace_file)

    def emit(self, action: str, **details) -> None:
        """Append one JSON line to the trace file. Never raises."""
        if not self.trace_file:
            return
        entry = {
            "ts": datetime.now().isoformat(),
            "elapsed_sec": round(time.time() - self.start_time, 3),
            "run_id": self.run_id,
            "tool": self.tool_name,
            "action": action,
            "details": details,
        }
        try:
            with self.trace_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("Tool trace write failed for %s: %s", self.tool_name, exc)


def _make_capacity_exhausted_result(
    msg: str,
    output: str,
    iterations: int,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> ToolResult:
    """Return a ToolResult signalling capacity exhaustion (retryable)."""
    return ToolResult(
        success=False,
        output=output,
        iterations=iterations,
        error=msg,
        error_code="capacity_exhausted",
        retryable=True,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


def _write_tool_file(output_dir: Path, filename: str, content: str) -> None:
    """Write a file into a tool output directory, creating it if needed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / filename).write_text(content, encoding="utf-8")


def _make_report_header(title: str, timestamp: str, task: str, provider_name: str, cwd_path: Path) -> str:
    """Return a standard Markdown report header for tool output files."""
    return (
        f"# {title} — {timestamp}\n\n"
        f"**Task:** {task}  \n"
        f"**Provider:** {provider_name}  \n"
        f"**CWD:** {cwd_path}\n\n"
        "---\n\n"
    )


def _build_system_prompt(
    provider_name: str,
    memory_context: str = "",
    tool_name: str | None = None,
    cwd: str | None = None,
) -> str:
    """Assemble system prompt with layered memory context for tool workflows.

    Layer order is chosen to maximize Anthropic prompt-cache hit rate. Cache
    matches the longest IDENTICAL prefix across calls, so we put the most
    static layers first and the most volatile ones last:

      1. Provider system prompt (SOUL.md, SAFETY)        — static across all tasks
      2. Curated MEMORY.md                                — user-edited, rarely changes
      3. Lessons (cwd-filtered)                           — stable per tool+cwd
      4. Daily log (today + yesterday)                    — grows during the day
      5. Task-specific TF-IDF memory_context              — changes every task

    Reordering Lessons before Daily is the key change vs. the prior version
    (Daily was layer 2, breaking cache for tool+cwd reruns within the same day).
    """
    prompt = get_system_prompt(provider_name)

    try:
        import memory as memory_module
    except (ImportError, OSError) as exc:
        logger.warning("Tool prompt memory import failed: %s", exc)
        memory_module = None

    if memory_module is not None:
        try:
            curated = memory_module.get_curated_memory()
            if curated:
                prompt += f"\n\n## Langzeit-Kontext\n{curated}"
        except (OSError, ValueError) as exc:
            logger.warning("Tool prompt curated memory load failed: %s", exc)

        # Lessons before Daily: lessons are stable per (tool, cwd); daily grows
        # over the day → moving daily to the end keeps the prefix cache warm
        # for repeated tool runs in the same project.
        try:
            lessons = memory_module.get_lessons_context(tool_name=tool_name, cwd=cwd)
            if lessons:
                prompt += f"\n\n## Gelernte Lektionen (Best Practices)\n{lessons}"
        except (OSError, ValueError) as exc:
            logger.warning("Tool prompt lessons memory load failed: %s", exc)

        try:
            daily = memory_module.get_daily_context()
            if daily:
                prompt += f"\n\n## Heutiger Verlauf\n{daily}"
        except (OSError, ValueError) as exc:
            logger.warning("Tool prompt daily memory load failed: %s", exc)

    if memory_context:
        prompt += f"\n\n## Relevanter vergangener Kontext\n{memory_context}"
    return prompt


class BaseTool(ABC):
    name: str = "base"
    description: str = ""
    read_only: bool = False

    @abstractmethod
    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        **kwargs,
    ) -> ToolResult:
        """Execute the tool workflow. Returns a ToolResult."""
        ...
