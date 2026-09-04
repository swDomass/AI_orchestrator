"""
Parallel sub-agent runner for the AI Orchestrator.

Queue syntax:
    - [ ] Review, test, and document project X #parallel
      - review code #claude #tool:review-loop cwd:/d/proj
      - run tests #codex #tool:test-loop cwd:/d/proj
      - update README #gemini cwd:/d/proj2

Subtasks that share the same CWD run sequentially within that group.
Different CWD groups run in parallel threads.

Worktree isolation (#worktree on parent task — opt-in):
    Each CWD group runs inside an isolated `git worktree` under
    <group-cwd>/.worktrees/parallel-<hash>. Cleanup is automatic on success;
    failed groups leave the worktree for inspection. Add #keep-worktree on
    the parent to skip cleanup even on success.
"""

import hashlib
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from limits import AllLimits, estimate_task_usage_pct, report_estimated_usage

logger = logging.getLogger(__name__)


@dataclass
class SubTask:
    text: str
    provider_forced: str | None
    cwd: str | None
    tool_name: str | None
    timeout: int
    # Alias key (e.g. 'claude_haiku', 'gemini_flash') — resolved to a model ID
    # per-provider at execution time via config.model_id_for_provider().
    model_tag: str | None = None
    # Reasoning-effort level from #effort:<level> (Claude-only, already validated
    # against config.CLAUDE_EFFORT_LEVELS). Inherited from the parent task when the
    # subtask carries no tag of its own — same rule as model_tag.
    effort: str | None = None


@dataclass
class SubTaskResult:
    text: str
    provider_name: str
    success: bool
    output: str
    error: str = ""
    # Summed by the caller and handed to memory.store_result. Without it the aggregate is
    # stored with output_tokens=0, and memory._is_noninformative then declines to judge —
    # which would leave #parallel as the one path where a no-op answer is still stored as
    # a success and re-injected as context.
    output_tokens: int = 0


def _parse_subtask(text: str) -> SubTask:
    """Extract metadata from a subtask line."""
    from queue_manager import extract_cwd, extract_timeout, extract_model_tag, extract_effort_tag
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
    model_tag = extract_model_tag(text)
    effort = extract_effort_tag(text)

    return SubTask(
        text=text,
        provider_forced=provider_forced,
        cwd=cwd,
        tool_name=tool_name,
        timeout=timeout,
        model_tag=model_tag,
        effort=effort,
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

    # Same clean-worktree precondition as the single-task path. run_once() exempts
    # the `#parallel` PARENT (its tool tag is not what runs), so without this the
    # subtasks — which are what actually calls _execute_tool_task — would be the
    # one route around the gate. Checked before provider selection, so a violation
    # costs no token; the subtask fails, and the parent is finalized ❌ through
    # `failed=not success_all`. Under `#worktree` the cwd has already been rewritten
    # to the freshly created worktree, which is clean by construction.
    from orchestrator import _worktree_gate_violation
    gate_msg = _worktree_gate_violation(subtask.text, subtask.tool_name, subtask.cwd)
    if gate_msg:
        logger.warning("parallel: subtask %d blocked — %s", idx, gate_msg)
        return SubTaskResult(
            text=subtask.text,
            provider_name="none",
            success=False,
            output="",
            error=f"worktree_dirty: {gate_msg}",
        )

    # Force provider if tag present
    exclude: set[str] = set()
    provider = select_provider(
        subtask.text, limits, exclude=exclude, profile=profile,
        force_name=subtask.provider_forced, tool_name=subtask.tool_name
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
    from config import model_id_for_provider
    model_id = model_id_for_provider(subtask.model_tag, provider.name)
    previous_forced_model = getattr(provider, "_forced_model", None)
    setattr(provider, "_forced_model", model_id)
    previous_forced_effort = getattr(provider, "_forced_effort", None)
    setattr(provider, "_forced_effort", subtask.effort)

    try:
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
                output=(outcome.output or "done") if outcome.success else "",
                error=outcome.error if not outcome.success else "",
                output_tokens=outcome.output_tokens,
            )

        # Plain single-shot subtask
        prompt = _build_prompt(subtask.text, provider.name, memory_context=memory_context)
        start_time = time.time()
        result, _ = _run_with_retry(
            provider, subtask.text, prompt, subtask.cwd, subtask.timeout,
            pause_event=pause_event,
        )
        duration = time.time() - start_time
        if result.error not in ("rate_limit", "unreachable", "paused"):
            report_estimated_usage(
                provider.name,
                estimate_task_usage_pct(
                    duration,
                    input_tokens=getattr(result, "input_tokens", 0),
                    output_tokens=getattr(result, "output_tokens", 0),
                    prompt_text=prompt,
                    output_text=result.output,
                    provider=provider.name,
                ),
            )
        return SubTaskResult(
            text=subtask.text,
            provider_name=provider.name,
            success=result.success,
            output=result.output if result.success else "",
            error=result.error if not result.success else "",
            output_tokens=getattr(result, "output_tokens", 0),
        )
    finally:
        setattr(provider, "_forced_model", previous_forced_model)
        setattr(provider, "_forced_effort", previous_forced_effort)


# ── Worktree isolation (P1) ──────────────────────────────────────────────────


_GIT_CHECK_TIMEOUT_SEC = 5
_GIT_WORKTREE_TIMEOUT_SEC = 30


def _is_clean_git_repo(path: Path) -> tuple[bool, str]:
    """Return (ok, reason) — True only when path is a git repo with no
    uncommitted changes. Reason is empty when ok.
    """
    try:
        repo_check = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(path), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GIT_CHECK_TIMEOUT_SEC,
        )
        if repo_check.returncode != 0:
            return False, "not a git repository"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(path), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GIT_CHECK_TIMEOUT_SEC,
        )
        if status.returncode != 0:
            return False, f"git status failed: {(status.stderr or status.stdout).strip()[:120]}"
        if status.stdout.strip():
            return False, "uncommitted changes present"
        return True, ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git check error: {exc}"


def _worktree_id(parent_task: str, group_key: str | None, idx: int) -> str:
    """Stable short ID for one worktree (8 hex chars + group index).

    Short on purpose: Windows path limit (260 chars) — combined with the parent
    CWD and the ``.worktrees`` subdir, longer IDs would trip MAX_PATH.
    """
    payload = f"{parent_task}|{group_key or ''}|{idx}".encode("utf-8", errors="replace")
    return f"parallel-{hashlib.sha1(payload).hexdigest()[:8]}"


def _create_worktree(parent_cwd: Path, worktree_id: str) -> tuple[Path | None, str]:
    """Create a git worktree at parent_cwd/.worktrees/<worktree_id>.

    Returns (path, error). path is None when creation failed; error is empty
    when path is set. Uses --detach so the worktree gets a detached HEAD —
    avoids branch-name collisions when the same parent runs concurrently.
    """
    from config import PARALLEL_WORKTREE_ROOT

    target = parent_cwd / PARALLEL_WORKTREE_ROOT / worktree_id
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["git", "worktree", "add", "--detach", str(target), "HEAD"],
            cwd=str(parent_cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GIT_WORKTREE_TIMEOUT_SEC,
        )
        if r.returncode != 0:
            return None, f"git worktree add failed: {(r.stderr or r.stdout).strip()[:200]}"
        if not target.is_dir():
            return None, f"worktree path missing after add: {target}"
        return target, ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"git worktree add error: {exc}"


def _remove_worktree(parent_cwd: Path, worktree_path: Path) -> bool:
    """Best-effort worktree removal. Logs failures, never raises."""
    try:
        r = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(parent_cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GIT_WORKTREE_TIMEOUT_SEC,
        )
        if r.returncode == 0:
            return True
        logger.warning(
            "git worktree remove failed for %s: %s",
            worktree_path, (r.stderr or r.stdout).strip()[:200],
        )
        return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git worktree remove error for %s: %s", worktree_path, exc)
        return False


def _group_join_timeout_sec(group: list[tuple[int, SubTask]]) -> int:
    """Join timeout for one CWD group (runs sequentially within a single thread)."""
    total = sum(max(0, st.timeout) for _, st in group)
    return total + 120  # extra buffer for provider/tool overhead


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
    parent_cwd: str | None = None
    worktree_enabled = False
    keep_worktree = False
    try:
        from queue_manager import (
            extract_cwd,
            has_cwd_tag,
            extract_effort_tag,
            has_effort_tag_attempt,
            extract_model_tag,
            extract_worktree_tag,
            extract_keep_worktree_tag,
        )
        parent_cwd = extract_cwd(parent_task)
        parent_model_tag = extract_model_tag(parent_task)
        parent_effort = extract_effort_tag(parent_task)
        worktree_enabled = extract_worktree_tag(parent_task)
        keep_worktree = extract_keep_worktree_tag(parent_task)
        if parent_cwd:
            subtasks = [
                replace(st, cwd=parent_cwd)
                if st.cwd is None and not has_cwd_tag(st.text)
                else st
                for st in subtasks
            ]
        if parent_model_tag:
            subtasks = [
                replace(st, model_tag=parent_model_tag)
                if st.model_tag is None
                else st
                for st in subtasks
            ]
        if parent_effort:
            # `st.effort is None` is NOT the same as "the subtask carried no tag":
            # extract_effort_tag() collapses an INVALID level to None too. Inheriting on
            # that basis would run `Child #effort:ultra` at the parent's level instead of
            # the session default — silently honouring a typo. So ask whether a tag was
            # *attempted*; any attempt, valid or not, blocks inheritance.
            #
            # has_effort_tag_attempt() rather than extract_effort_tag_raw(): the raw
            # extractor still uses the strict regex, so `#effort=high`, `#effort: high`
            # and `(#effort:high)` all returned None and inherited the parent level.
            subtasks = [
                replace(st, effort=parent_effort)
                if st.effort is None and not has_effort_tag_attempt(st.text)
                else st
                for st in subtasks
            ]
    except Exception as e:
        logger.warning("parallel: parent metadata inheritance skipped: %s", e)

    # Group by CWD (None = parent CWD group)
    cwd_groups: dict[str | None, list[tuple[int, SubTask]]] = {}
    for i, st in enumerate(subtasks):
        key = st.cwd
        cwd_groups.setdefault(key, []).append((i, st))

    all_results: list[SubTaskResult | None] = [None] * len(subtasks)

    # Worktree setup (opt-in via #worktree on parent). One worktree per CWD group;
    # each group's subtasks get their cwd rewritten to point inside it.
    worktree_map: dict[str | None, Path] = {}   # group_key → worktree path
    worktree_base: dict[str | None, Path] = {}  # group_key → base cwd (for `git worktree remove`)
    worktree_errors: dict[str | None, str] = {}  # group_key → setup error

    if worktree_enabled:
        for group_idx, (group_key, group) in enumerate(cwd_groups.items()):
            base_cwd_str = group_key or parent_cwd
            if not base_cwd_str:
                worktree_errors[group_key] = "worktree requires cwd on parent or subtask"
                continue
            base_cwd = Path(base_cwd_str)
            ok, reason = _is_clean_git_repo(base_cwd)
            if not ok:
                worktree_errors[group_key] = f"worktree precheck failed: {reason}"
                continue
            wt_id = _worktree_id(parent_task, group_key, group_idx)
            wt_path, err = _create_worktree(base_cwd, wt_id)
            if wt_path is None:
                worktree_errors[group_key] = err
                continue
            worktree_map[group_key] = wt_path
            worktree_base[group_key] = base_cwd
            wt_cwd_str = str(wt_path)
            for pos, (orig_idx, st) in enumerate(group):
                new_st = replace(st, cwd=wt_cwd_str)
                group[pos] = (orig_idx, new_st)
                subtasks[orig_idx] = new_st

    # Groups that hit a worktree setup error: short-circuit all their subtasks
    # to a failed result so they show up clearly in the parent's final output.
    for group_key, err in worktree_errors.items():
        for orig_idx, st in cwd_groups[group_key]:
            all_results[orig_idx] = SubTaskResult(
                text=st.text,
                provider_name="worktree",
                success=False,
                output="",
                error=err,
            )

    threads: list[threading.Thread] = []
    thread_timeouts: dict[threading.Thread, int] = {}
    lock = threading.Lock()

    def _run_group(group: list[tuple[int, SubTask]]) -> None:
        for idx, st in group:
            try:
                result = _run_single_subtask(st, idx, limits, memory_context, pause_event, profile=profile)
            except Exception as e:
                logger.exception("parallel: subtask %d crashed", idx)
                result = SubTaskResult(
                    text=st.text,
                    provider_name="internal",
                    success=False,
                    output="",
                    error=f"subtask_crash: {e}",
                )
            with lock:
                all_results[idx] = result

    for _cwd, group in cwd_groups.items():
        if _cwd in worktree_errors:
            continue   # already short-circuited above
        t = threading.Thread(
            target=_run_group,
            args=(group,),
            daemon=True,
            name=f"parallel-{_cwd or 'default'}",
        )
        threads.append(t)
        thread_timeouts[t] = _group_join_timeout_sec(group)
        t.start()

    for t in threads:
        join_timeout = thread_timeouts.get(t, 720)
        t.join(timeout=join_timeout)
        if t.is_alive():
            logger.warning("parallel: thread %s still alive after %ds timeout", t.name, join_timeout)

    # Worktree cleanup: remove on success, retain on failure (so the user can inspect).
    # The retained path is appended to each failed subtask's error string.
    for group_key, wt_path in worktree_map.items():
        group_idxs = [orig_idx for orig_idx, _ in cwd_groups[group_key]]
        group_results = [all_results[idx] for idx in group_idxs]
        any_failed = any(r is None or not r.success for r in group_results)
        if any_failed:
            logger.warning("parallel: leaving worktree intact at %s (subtask failed/missing)", wt_path)
            for idx in group_idxs:
                r = all_results[idx]
                if r is not None and not r.success:
                    all_results[idx] = SubTaskResult(
                        text=r.text,
                        provider_name=r.provider_name,
                        success=False,
                        output=r.output,
                        error=f"{r.error} [worktree retained: {wt_path}]".strip(),
                    )
            continue
        if keep_worktree:
            logger.info("parallel: keeping worktree at %s (#keep-worktree)", wt_path)
            continue
        base = worktree_base.get(group_key)
        if base is not None:
            _remove_worktree(base, wt_path)

    # Replace any remaining None slots (thread timed out or internal error)
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
