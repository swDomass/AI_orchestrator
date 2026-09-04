"""Base class for orchestrator tools.

Tools are multi-step workflows that go beyond single CLI calls.
They run iterative loops (review→fix→recheck) and report progress.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

from config import MEMORY_HISTORY_HEADING, get_system_prompt
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
    cwd: str | None = None  # captured for ActiveRunRegistry mirror

    @classmethod
    def create(cls, tool_name: str, cwd: str | None) -> "ToolTracer":
        """Build a tracer for a tool run. Allocates a UUID and the trace file
        path. If cwd is missing or the directory cannot be created, the JSONL
        trace becomes a silent no-op — but the central ActiveRunRegistry
        mirror still runs.
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
        return cls(tool_name=tool_name, run_id=run_id, trace_file=trace_file, cwd=cwd)

    def emit(self, action: str, **details) -> None:
        """Append one JSON line to the trace file. Never raises.

        In addition, mirrors selected lifecycle events (run_start /
        iteration_start / iteration_end / phase_start / subprocess_result /
        run_end) into the central ``ActiveRunRegistry`` so the dashboard can
        show live progress without scanning per-cwd trace files.
        """
        if self.trace_file:
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

        # Mirror to central active-runs index (best-effort, never raises).
        try:
            self._mirror_to_active_runs(action, details)
        except Exception as exc:  # pragma: no cover — last-resort safety net
            logger.warning("ActiveRunRegistry mirror failed: %s", exc)

    def _mirror_to_active_runs(self, action: str, details: dict) -> None:
        """Translate trace events into ActiveRunRegistry updates."""
        if action == "run_start":
            ActiveRunRegistry.start(
                run_id=self.run_id,
                tool=self.tool_name,
                task=str(details.get("task", "")),
                # Prefer the tracer's captured cwd; emit-details may omit it.
                cwd=details.get("cwd") or self.cwd,
                provider=str(details.get("provider", "")),
                started_at=self.start_time,
            )
            # max_iterations is commonly emitted alongside run_start
            if "max_iterations" in details:
                ActiveRunRegistry.update(
                    self.run_id, iteration_max=int(details["max_iterations"])
                )
        elif action == "run_end":
            ActiveRunRegistry.end(self.run_id)
        elif action in ("iteration_start", "iteration_end", "phase_start",
                        "subprocess_result"):
            fields: dict = {}
            if "iteration" in details:
                fields["iteration_current"] = int(details["iteration"])
            if "max_iterations" in details:
                fields["iteration_max"] = int(details["max_iterations"])
            if "phase" in details:
                fields["phase"] = str(details["phase"])
            delta: dict = {}
            for src, dst in (
                ("input_tokens", "input"),
                ("output_tokens", "output"),
                ("cache_creation_input_tokens", "cache_creation"),
                ("cache_read_input_tokens", "cache_read"),
            ):
                if src in details and details[src]:
                    delta[dst] = details[src]
            if delta:
                fields["tokens_delta"] = delta
            if fields:
                ActiveRunRegistry.update(self.run_id, **fields)


# ── ActiveRunRegistry ────────────────────────────────────────────────────────

ACTIVE_RUNS_DIR = Path(__file__).resolve().parent.parent / "logs" / "active_runs"
_ACTIVE_STALE_SEC = 6 * 3600       # records without update older than this → "stale"
_ACTIVE_CLEANUP_SEC = 24 * 3600    # records older than this → physically deleted


class ActiveRunRegistry:
    """Central index of currently-running tool runs (file-per-run JSON).

    Files live in ``logs/active_runs/<run_id>.json`` at orchestrator root.
    Writes are atomic (tempfile + os.replace). Reads from the dashboard never
    see partial JSON. All operations are best-effort — exceptions are swallowed
    because tracing must never break a tool run.
    """

    @staticmethod
    def _path(run_id: str) -> Path:
        return ACTIVE_RUNS_DIR / f"{run_id}.json"

    @staticmethod
    def _ensure_dir() -> bool:
        try:
            ACTIVE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            logger.warning("ActiveRunRegistry dir setup failed: %s", exc)
            return False

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        """tempfile + os.replace so concurrent readers never see partial JSON."""
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except OSError as exc:
            logger.warning("ActiveRunRegistry write failed for %s: %s", path.name, exc)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @classmethod
    def start(
        cls,
        run_id: str,
        tool: str,
        task: str,
        cwd: str | None,
        provider: str,
        started_at: float | None = None,
    ) -> None:
        """Create a new active-run record. Idempotent (overwrites on collision)."""
        if not cls._ensure_dir():
            return
        ts = started_at or time.time()
        record = {
            "run_id": run_id,
            "tool": tool,
            "task": task[:500],
            "cwd": cwd or "",
            "provider": provider,
            "started_at": ts,
            "last_update": ts,
            "elapsed_sec": 0.0,
            "iteration_current": 0,
            "iteration_max": 0,
            "phase": "",
            "tokens": {
                "input": 0,
                "output": 0,
                "cache_creation": 0,
                "cache_read": 0,
            },
            "status": "running",
        }
        cls._atomic_write(cls._path(run_id), record)

    @classmethod
    def update(cls, run_id: str, **fields) -> None:
        """Merge fields into the existing record.

        Tokens are accumulated via ``tokens_delta`` (a dict with input/output/
        cache_creation/cache_read int deltas). Other fields overwrite.
        """
        path = cls._path(run_id)
        try:
            with path.open(encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError):
            return  # no record → nothing to update (start may not have run)

        now = time.time()
        record["last_update"] = now
        record["elapsed_sec"] = round(now - record.get("started_at", now), 3)

        delta = fields.pop("tokens_delta", None)
        if isinstance(delta, dict):
            tokens = record.setdefault("tokens", {"input": 0, "output": 0,
                                                  "cache_creation": 0, "cache_read": 0})
            for key in ("input", "output", "cache_creation", "cache_read"):
                if key in delta:
                    tokens[key] = int(tokens.get(key, 0)) + int(delta[key])

        for key, value in fields.items():
            record[key] = value

        cls._atomic_write(path, record)

    @classmethod
    def end(cls, run_id: str) -> None:
        """Delete the active-run record. Idempotent."""
        try:
            cls._path(run_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("ActiveRunRegistry end failed for %s: %s", run_id, exc)

    @classmethod
    def list_active(
        cls,
        stale_after_sec: int = _ACTIVE_STALE_SEC,
        cleanup_after_sec: int = _ACTIVE_CLEANUP_SEC,
    ) -> list[dict]:
        """Return all active-run records.

        Records without update for >``cleanup_after_sec`` are physically deleted.
        Records without update for >``stale_after_sec`` are marked
        ``status='stale'`` so the dashboard can render them differently.
        Returned list is sorted by ``started_at`` (oldest first).
        """
        if not ACTIVE_RUNS_DIR.exists():
            return []

        now = time.time()
        results: list[dict] = []
        for entry in ACTIVE_RUNS_DIR.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                with entry.open(encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue

            last_update = record.get("last_update", record.get("started_at", now))
            age = now - last_update
            if age > cleanup_after_sec:
                try:
                    entry.unlink()
                except OSError:
                    pass
                continue
            if age > stale_after_sec:
                record["status"] = "stale"
            # Refresh elapsed for the dashboard (process may still be running)
            record["elapsed_sec"] = round(now - record.get("started_at", now), 3)
            results.append(record)

        results.sort(key=lambda r: r.get("started_at", 0))
        return results


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

    # Progressive skill index (#37) — always-present, cheap to keep cache-warm.
    # Goes right after the static system prompt so the longest stable prefix
    # stays cache-stable across tool runs.
    try:
        from skills import build_index
        from config import VAULT_PATH as _VAULT_PATH
        index_block = build_index(vault_path=_VAULT_PATH)
        if index_block:
            prompt += f"\n\n{index_block}"
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("Skill index injection skipped: %s", exc)

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
        prompt += f"\n\n{MEMORY_HISTORY_HEADING}\n{memory_context}"

    # Preflight (#35) — deterministic per-tool context injection. Cached on
    # disk per (tool, cwd, day) so the LLM doesn't pay re-discovery costs on
    # every iteration. Silently skips when tool_name has no preflight hook.
    if tool_name and cwd:
        try:
            import preflight as _preflight
            block = _preflight.collect_cached(tool_name, cwd)
            if block:
                prompt += f"\n\n{block}"
        except Exception as exc:  # noqa: BLE001 — preflight is best-effort
            logger.debug("Preflight injection skipped for %s: %s", tool_name, exc)

    return prompt


class BaseTool(ABC):
    name: str = "base"
    description: str = ""
    read_only: bool = False

    # The tool PRODUCES the working-tree diff that its own reviewers then judge,
    # so it must start from a clean tree — leftover changes from an earlier task
    # are not noise, they corrupt the object under review. The orchestrator refuses
    # to start such a task in a dirty repo (error_code "worktree_dirty"); a task
    # line can waive it with `#allow-dirty`.
    #
    # Deliberately False for review-loop and friends: review-loop CONSUMES an
    # existing diff, so demanding a clean tree there would be the opposite of what
    # the tool is for.
    requires_clean_worktree: bool = False

    def _runtime_deadline(self) -> float:
        """Monotonic wall-clock deadline for the whole tool run.

        Bounds the SUM of all phases/iterations via the tool's
        ToolContract.max_runtime_sec (falls back to TOOL_DEFAULT_MAX_RUNTIME_SEC).
        Prevents 20 iterations × multiple long phases from binding 10+ hours of
        wall-clock when a high #timeout: hard backstop is set per call.
        """
        from config import TOOL_DEFAULT_MAX_RUNTIME_SEC
        max_runtime = TOOL_DEFAULT_MAX_RUNTIME_SEC
        try:
            import policy
            contract = policy.get_engine().get_tool_contract(self.name)
            if contract and contract.max_runtime_sec:
                max_runtime = contract.max_runtime_sec
        except Exception:
            pass
        return time.monotonic() + max_runtime

    @staticmethod
    def _phase_cap(task_timeout: int | None, phase_default: int) -> int:
        """Per-phase timeout cap.

        A high task #timeout: hard backstop must NOT raise a phase above its
        established TOOL_*_TIMEOUT_SEC constant — it only acts as an upper deckel.
        """
        if not task_timeout:
            return phase_default
        return min(task_timeout, phase_default)

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
