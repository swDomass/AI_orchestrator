"""
Parallel sub-agent runner for the AI Orchestrator.

Queue syntax:
    - [ ] Review, test, and document project X #parallel
      - review code #claude #tool:review-loop cwd:/d/proj
      - run tests #codex #tool:test-loop cwd:/d/proj
      - update README #gemini cwd:/d/proj2

Subtasks that share the same CWD run sequentially within that group.
Different CWD groups run in parallel threads.
"""

import logging
import threading
from dataclasses import dataclass, field

from limits import AllLimits

logger = logging.getLogger(__name__)


@dataclass
class SubTask:
    text: str
    provider_forced: str | None
    cwd: str | None
    tool_name: str | None
    timeout: int


@dataclass
class SubTaskResult:
    text: str
    provider_name: str
    success: bool
    output: str
    error: str = ""


def _parse_subtask(text: str) -> SubTask:
    """Extract metadata from a subtask line."""
    from queue_manager import extract_cwd, extract_timeout
    from config import TASK_TIMEOUT_SEC

    # Detect forced provider from #claude / #gemini / #codex tags
    from dispatcher import _TAG_MAP, _TAG_RE_BY_PROVIDER

    provider_forced: str | None = None
    text_lower = text.lower()
    for tag, name in _TAG_MAP.items():
        if _TAG_RE_BY_PROVIDER[tag].search(text_lower):
            provider_forced = name
            break

    # Extract tool, cwd, timeout
    from tools import extract_tool_tag
    tool_name = extract_tool_tag(text)
    cwd = extract_cwd(text)
    timeout = extract_timeout(text, default=TASK_TIMEOUT_SEC)

    return SubTask(
        text=text,
        provider_forced=provider_forced,
        cwd=cwd,
        tool_name=tool_name,
        timeout=timeout,
    )


def _run_single_subtask(
    subtask: SubTask,
    idx: int,
    limits: AllLimits,
    memory_context: str,
    pause_event: threading.Event | None,
    profile=None,  # ProfileConfig | None
) -> SubTaskResult:
    """Execute a single subtask and return its result."""
    from dispatcher import select_provider
    from queue_manager import strip_metadata_tags
    from orchestrator import _build_prompt, _run_with_retry, _execute_tool_task

    if pause_event and pause_event.is_set():
        return SubTaskResult(
            text=subtask.text,
            provider_name="paused",
            success=False,
            output="",
            error="paused",
        )

    clean_text = strip_metadata_tags(subtask.text)

    # Force provider if tag present
    exclude: set[str] = set()
    provider = select_provider(
        subtask.text, limits, exclude=exclude, profile=profile,
        force_name=subtask.provider_forced
    )
    if provider is None:
        return SubTaskResult(
            text=subtask.text,
            provider_name="none",
            success=False,
            output="",
            error="no_provider",
        )

    logger.debug("parallel: subtask %d → provider %s, tool %s", idx, provider.name, subtask.tool_name)

    # Tool-based subtask
    if subtask.tool_name:
        from orchestrator import ToolTaskExecutionOutcome
        outcome = _execute_tool_task(
            subtask.text,
            subtask.tool_name,
            provider,
            subtask.cwd,
            timeout=subtask.timeout,
            queue_line_no=None,
            memory_context=memory_context,
            skip_queue=True,      # parent handles finalization
        )
        return SubTaskResult(
            text=subtask.text,
            provider_name=f"{provider.name}+{subtask.tool_name}",
            success=outcome.success,
            output=outcome.error if not outcome.success else "done",
            error=outcome.error if not outcome.success else "",
        )

    # Plain single-shot subtask
    prompt = _build_prompt(subtask.text, provider.name, memory_context=memory_context)
    result, _ = _run_with_retry(
        provider, subtask.text, prompt, subtask.cwd, subtask.timeout,
        pause_event=pause_event,
    )
    return SubTaskResult(
        text=subtask.text,
        provider_name=provider.name,
        success=result.success,
        output=result.output if result.success else "",
        error=result.error if not result.success else "",
    )


def run_parallel(
    parent_task: str,
    subtask_texts: tuple[str, ...],
    limits: AllLimits,
    memory_context: str = "",
    pause_event: threading.Event | None = None,
    profile=None,  # ProfileConfig | None
) -> list[SubTaskResult]:
    """Run subtasks with parallelism across different CWDs.

    Subtasks sharing the same CWD run sequentially within that group.
    Different CWD groups run in parallel threads.
    """
    if not subtask_texts:
        return []

    subtasks = [_parse_subtask(t) for t in subtask_texts]

    # Group by CWD (None = parent CWD group)
    cwd_groups: dict[str | None, list[tuple[int, SubTask]]] = {}
    for i, st in enumerate(subtasks):
        key = st.cwd
        cwd_groups.setdefault(key, []).append((i, st))

    all_results: list[SubTaskResult | None] = [None] * len(subtasks)
    threads: list[threading.Thread] = []
    lock = threading.Lock()

    def _run_group(group: list[tuple[int, SubTask]]) -> None:
        for idx, st in group:
            result = _run_single_subtask(st, idx, limits, memory_context, pause_event, profile=profile)
            with lock:
                all_results[idx] = result

    for _cwd, group in cwd_groups.items():
        t = threading.Thread(
            target=_run_group,
            args=(group,),
            daemon=True,
            name=f"parallel-{_cwd or 'default'}",
        )
        threads.append(t)
        t.start()

    # Compute a generous join timeout: max subtask timeout + buffer
    max_subtask_timeout = max(st.timeout for st in subtasks) if subtasks else 600
    join_timeout = max_subtask_timeout + 120  # extra 2 min buffer

    for t in threads:
        t.join(timeout=join_timeout)
        if t.is_alive():
            logger.warning("parallel: thread %s still alive after %ds timeout", t.name, join_timeout)

    # Replace any None slots (thread timed out or internal error) with error results
    final: list[SubTaskResult] = []
    for i, r in enumerate(all_results):
        if r is None:
            final.append(SubTaskResult(
                text=subtasks[i].text,
                provider_name="unknown",
                success=False,
                output="",
                error="internal_error",
            ))
        else:
            final.append(r)

    return final


def format_parallel_result(results: list[SubTaskResult]) -> str:
    """Format parallel subtask results into a single output string."""
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        status = "PASS" if r.success else "FAIL"
        provider_safe = r.provider_name or "unknown"
        task_preview = r.text[:60] + ("..." if len(r.text) > 60 else "")
        detail = r.output[:200] if r.success else (r.error or "unknown error")
        lines.append(f"**Subtask {i}** ({provider_safe}): {status} — {task_preview}\n{detail}")
    return "\n\n".join(lines)
