"""Liveness/hang watchdog for CLI provider subprocesses.

Replaces the plain ``subprocess.run(..., timeout=...)`` call used by all three
CLI providers. ``subprocess.run`` only kills the *direct* child on timeout; on
Windows with ``shell=True`` the real ``node``/``claude`` grandchild is orphaned,
keeps the stdout pipes open and ``communicate()`` blocks until that orphan
finishes on its own (observed: nominal kill at 900 s, return at 1094 s).

This module:
  * spawns the process via ``Popen``,
  * drains stdout AND stderr on TWO reader threads simultaneously (a single
    reader would deadlock on a full OS pipe buffer of the other stream),
  * tracks a monotonic last-activity timestamp,
  * kills the *whole process tree* (Windows ``taskkill /F /T``, POSIX
    ``killpg`` SIGTERM→SIGKILL) when either the idle (hang) or the hard
    (absolute backstop) timeout fires.

Liveness model — see ``_Liveness``:
  * Gemini/Codex stream text incrementally, so byte activity == liveness.
  * Claude streams NDJSON events; a running tool call keeps stdout silent for
    the whole tool duration (verified with claude 2.1.158: an ``assistant``
    event carrying a ``tool_use`` content block, then 18.1 s of stdout silence
    for a 12 s tool call, scaling with tool duration). A naive byte watchdog
    would kill a productive run during a long pytest/build/install phase.

    There is NO top-level ``{"type":"tool_use"}`` event. The real sequence is::

        system/init → rate_limit_event → assistant[content: thinking]
        → assistant[content: tool_use] → (stdout SILENCE for the tool duration)
        → user[content: tool_result] → assistant[content: text] → result

    With ``liveness_lines=True`` an ``assistant`` event whose ``message.content``
    contains a ``tool_use`` block pauses the idle timer; a ``user`` event with a
    ``tool_result`` block (or the top-level ``result`` event) resumes it. The
    ``hard_timeout`` stays the absolute backstop.
"""

import codecs
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from config import TASK_TIMEOUT_SEC

_log = logging.getLogger(__name__)

_POSIX_TERM_GRACE_SEC = 5.0      # SIGTERM → wait → SIGKILL
_WATCHDOG_POLL_SEC = 0.5         # interval of the watchdog loop
_READER_JOIN_TIMEOUT_SEC = 5.0   # max wait on reader threads after process end/kill
_TASKKILL_TIMEOUT_SEC = 10.0     # timeout for the taskkill subprocess (Windows)
# Cap on retained captured chars PER stream. stream-json --verbose streams every
# tool_result (full file reads, Bash output) as its own event; without a cap a
# long dev-loop phase could hold hundreds of MB in memory. We keep the TAIL
# (oldest lines dropped first) so the final type=="result" event — always the
# last line — is always retained.
_MAX_CAPTURE_CHARS = 8_000_000   # ~8 MB per stream
_READ_CHUNK_BYTES = 65536        # read1() chunk size for byte-granular liveness

# The top-level ``result`` event always means the run is finishing → no tool is
# running anymore. (``assistant``/``user`` are NOT in here: their meaning depends
# on the content blocks they carry — see ``_Liveness.on_event``.)
_TOOL_DONE_EVENTS = ("result",)


@dataclass
class WatchdogResult:
    """CompletedProcess-compatible result over the three attributes we use.

    Own class instead of subprocess.CompletedProcess because the latter
    requires ``args``; the providers and tests only read returncode/stdout/
    stderr, so duck typing is enough.

    ``stdin_error`` carries a diagnostic string when the prompt did NOT fully
    reach the child — see ``_feed_stdin``. None means every byte was handed to
    the OS pipe, NOT that the child read them: a prompt below the pipe buffer
    (~64 KB) is accepted whole even by a child that never reads, so small
    prompts are a blind spot (documented in docs/architecture/components.md).
    Providers MUST read it via ``getattr(result, "stdin_error", None)``: the
    provider tests fake this object with a three-field ``SimpleNamespace``.
    """

    returncode: int
    stdout: str
    stderr: str
    stdin_error: str | None = None


class _Liveness:
    """Tracks last-activity and (for Claude) whether any tool is still running.

    ``_open_tool_ids`` is the set of ``tool_use`` block ids that have started but
    not yet returned. A tool is "running" (→ never idle-kill) as long as that set
    is non-empty OR an un-correlatable tool_use (no id) is outstanding. Using a
    SET rather than a single boolean is essential: Claude emits parallel tool_use
    blocks in one assistant message and spawns Task subagents, so the FIRST
    tool_result must not flip the timer back on while a sibling tool is still
    running. A ``Lock`` guards the multi-step set updates against the watchdog
    reader; the single-float ``_last`` write/read stays GIL-atomic.
    """

    def __init__(self, start_monotonic: float) -> None:
        self._last = [start_monotonic]
        self._open_tool_ids: set[str] = set()
        self._anon_open = 0  # tool_use blocks without an id (can't be correlated)
        self._lock = threading.Lock()

    def touch(self, now: float | None = None) -> None:
        self._last[0] = now if now is not None else time.monotonic()

    def on_event(self, evt: dict, now: float) -> None:
        """Update tool-running state from a parsed NDJSON event (Claude only).

        Claude has no top-level ``tool_use`` event. Tool activity is encoded in
        the content blocks of ``assistant`` / ``user`` events:
          * ``assistant`` with ``tool_use`` block(s) → those tools are starting,
            stdout will go silent for their whole duration → track their ids.
          * ``user`` with ``tool_result`` block(s) → those tools returned →
            drop their ids; idle only once ALL tracked tools are done.
          * top-level ``result`` → the run is finishing → clear everything.
        Any other event (init, thinking, plain text, rate_limit_event) only
        touches the activity timestamp.
        """
        evt_type = evt.get("type")
        if evt_type == "assistant":
            for block in _content_blocks(evt, "tool_use"):
                tool_id = block.get("id")
                with self._lock:
                    if tool_id:
                        self._open_tool_ids.add(tool_id)
                    else:
                        self._anon_open += 1
        elif evt_type == "user":
            for block in _content_blocks(evt, "tool_result"):
                tool_id = block.get("tool_use_id")
                with self._lock:
                    if tool_id and tool_id in self._open_tool_ids:
                        self._open_tool_ids.discard(tool_id)
                    elif self._anon_open > 0:
                        # tool_result for an id-less tool_use we couldn't track →
                        # pair it off so an anonymous tool can't pause idle forever.
                        self._anon_open -= 1
        elif evt_type in _TOOL_DONE_EVENTS:
            with self._lock:
                self._open_tool_ids.clear()
                self._anon_open = 0
        self._last[0] = now

    def idle_for(self, now: float) -> float:
        with self._lock:
            if self._open_tool_ids or self._anon_open:
                return 0.0  # a tool is still running → never idle-kill
        return now - self._last[0]


def _spawn(
    cmd: list[str] | str,
    cwd: str | None,
    shell: bool,
    encoding: str,
    errors: str,
) -> subprocess.Popen:
    kwargs: dict = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=encoding,
        errors=errors,
        cwd=cwd,
        shell=shell,
    )
    if os.name == "posix":
        # Own process group so killpg reaps the whole tree.
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _make_reader(
    stream, sink: list[str], liveness: _Liveness, parse_events: bool,
    encoding: str, errors: str,
) -> Callable:
    """Return a target that drains one stream into ``sink`` with byte-granular liveness.

    Reads available bytes via the underlying buffer's ``read1()`` (returns as
    soon as ANY data is ready) rather than iterating lines: a line iterator only
    registers activity on a newline, so a provider streaming a long newline-less
    burst (build logs, `pytest` dots) would look idle and get false-killed. An
    incremental decoder reassembles multi-byte chars split across reads. Both
    stdout and stderr get their own thread so neither blocks the other on a full
    OS pipe buffer.
    """
    raw = getattr(stream, "buffer", None)
    decoder = codecs.getincrementaldecoder(encoding)(errors)

    def _cap(retained: int) -> int:
        # Drop oldest chunks once over the cap; keep the tail (the final result
        # event is the last line). Never drop the only chunk.
        while retained > _MAX_CAPTURE_CHARS and len(sink) > 1:
            retained -= len(sink.pop(0))
        return retained

    def _absorb(text: str, line_buf: str, now: float) -> str:
        if not parse_events:
            liveness.touch(now)
            return line_buf
        line_buf += text
        *complete, line_buf = line_buf.split("\n")
        for ln in complete:
            _touch_from_event(ln, liveness, now)
        liveness.touch(now)
        return line_buf

    def _run() -> None:
        retained = 0
        line_buf = ""
        try:
            if raw is None:  # defensive: stream has no binary buffer (rare)
                for line in stream:
                    sink.append(line)
                    retained = _cap(retained + len(line))
                    line_buf = _absorb(line, line_buf, time.monotonic())
                return
            while True:
                data = raw.read1(_READ_CHUNK_BYTES)
                now = time.monotonic()
                text = decoder.decode(data, final=not data)
                if text:
                    sink.append(text)
                    retained = _cap(retained + len(text))
                    line_buf = _absorb(text, line_buf, now)
                elif data:
                    liveness.touch(now)  # bytes arrived, char still incomplete
                if not data:
                    break  # EOF
        except (ValueError, OSError):
            # Stream may be closed under us during FD cleanup after a kill.
            pass

    return _run


def _touch_from_event(line: str, liveness: _Liveness, now: float) -> None:
    """Parse one NDJSON line; update tool-active state, else just touch."""
    stripped = line.strip()
    if not stripped:
        liveness.touch(now)
        return
    try:
        evt = json.loads(stripped)
    except json.JSONDecodeError:
        liveness.touch(now)
        return
    if isinstance(evt, dict):
        liveness.on_event(evt, now)
    else:
        liveness.touch(now)


def _content_blocks(evt: dict, block_type: str) -> list[dict]:
    """Return the event's ``message.content`` blocks of ``block_type`` (maybe empty).

    Claude stream-json wraps message blocks as
    ``{"type":"assistant","message":{"content":[{"type":"tool_use","id":...}]}}``.
    Defensive against missing/oddly-shaped fields (returns []).
    """
    message = evt.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        block for block in content
        if isinstance(block, dict) and block.get("type") == block_type
    ]


@dataclass
class _StdinDelivery:
    """Outcome of the stdin feeder thread, read by the caller AFTER its join.

    Fail-CLOSED by design: ``delivered`` is set ONLY after a successful
    ``close()``. Every other way out of the feeder — a caught write/flush error,
    an exception class we do NOT catch (``MemoryError`` on a multi-MB prompt,
    ``RuntimeError`` at interpreter shutdown), or a thread that never finished —
    leaves it False and therefore counts as a failed delivery. The default state
    has to be "not delivered": the whole reason this class exists is that a
    silently lost prompt used to look like a success.
    """

    expected_chars: int = 0
    delivered: bool = False
    error: str | None = None


def _feed_stdin(
    proc: subprocess.Popen,
    input_text: str | None,
    delivery: _StdinDelivery,
) -> None:
    """Write input_text to the process stdin and close it (own thread).

    A swallowed failure here is NOT harmless. ``_spawn`` opens stdin with
    ``text=True`` → a buffered ``TextIOWrapper``, so ``write()`` only fills the
    buffer and the *tail* is flushed by ``flush()``/``close()``. If that final
    flush fails, the child receives a TRUNCATED prompt. The orchestrator builds
    its prompt as ``core → skills → memory → task`` (orchestrator._build_prompt),
    i.e. the task text sits at the very END — so a lost tail removes the
    instruction and leaves a context-only prompt. The CLI then answers "what
    would you like me to do?", exits 0 with a valid result event, and the run is
    finalized as a SUCCESS that did nothing. Observed 5×; cost 3 days of vault
    health data (2026-07-20).

    ``flush()`` is therefore called explicitly before ``close()`` so the tail
    flush is its own, attributable failure site, and the outcome is recorded in
    ``delivery`` instead of being dropped.
    """
    if input_text is None:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        delivery.delivered = True
        return

    stage = "write"
    try:
        written = proc.stdin.write(input_text)
        # Defensive: write() returns the accepted character count. Against a real
        # pipe it has always returned the full length in testing (verified to
        # 5 MB), but a short return would silently drop the tail — the exact
        # failure this function exists to catch — so treat it as one.
        if written is not None and written < len(input_text):
            raise OSError(f"short write: {written} of {len(input_text)} chars accepted")
        stage = "flush"
        proc.stdin.flush()
        stage = "close"
        proc.stdin.close()
        delivery.delivered = True  # last statement: only a clean run confirms
    except (BrokenPipeError, OSError, ValueError) as e:
        # A failing write/flush/close raises without telling us how much of the
        # buffer reached the pipe, so we report the prompt length and say plainly
        # that the delivered amount is unknown rather than implying all N failed.
        # (A short return from write() is handled above, where the count IS known.)
        delivery.error = (
            f"{stage}() failed; prompt was {len(input_text)} chars, "
            f"delivered amount unknown: {type(e).__name__}: {e}"
        )
        _log.error("[stdin] prompt delivery incomplete — %s", delivery.error)


def _tree_kill(proc: subprocess.Popen) -> None:
    """Kill the whole process tree of ``proc`` (best effort)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        _taskkill(proc)
        # Belt-and-suspenders: a grandchild could fork exactly between poll()
        # and the first tree enumeration. Only retry if the tree is still alive
        # — avoids a fixed _WATCHDOG_POLL_SEC penalty on the common case where
        # the first kill already reaped everything. The retry is idempotent.
        if proc.poll() is None:
            time.sleep(_WATCHDOG_POLL_SEC)
            _taskkill(proc)
    else:
        _killpg(proc)
    _reap(proc)


def _taskkill(proc: subprocess.Popen) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _killpg(proc: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    deadline = time.monotonic() + _POSIX_TERM_GRACE_SEC
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(_WATCHDOG_POLL_SEC)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _reap(proc: subprocess.Popen) -> None:
    """Wait for the process so no zombie remains."""
    try:
        proc.wait(timeout=_READER_JOIN_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _watchdog_loop(
    proc: subprocess.Popen,
    liveness: _Liveness,
    idle_timeout: float,
    hard_timeout: float,
    start_monotonic: float,
) -> str | None:
    """Return 'idle' or 'hard' if a timeout fired, else None on normal exit."""
    while proc.poll() is None:
        time.sleep(_WATCHDOG_POLL_SEC)
        now = time.monotonic()
        if now - start_monotonic >= hard_timeout:
            return "hard"
        if liveness.idle_for(now) >= idle_timeout:
            return "idle"
    return None


def run_with_watchdog(
    cmd: list[str] | str,
    *,
    input_text: str | None,
    cwd: str | None,
    idle_timeout: float,
    hard_timeout: float,
    shell: bool,
    liveness_lines: bool = False,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> WatchdogResult:
    """Run a CLI command with an idle (hang) + hard (backstop) watchdog.

    Raises ``subprocess.TimeoutExpired`` with an extra ``timeout_kind``
    attribute ("idle" | "hard") when the process tree is killed.

    ``liveness_lines=True`` enables NDJSON-event-aware liveness (Claude): a
    running ``tool_use`` pauses the idle timer. ``False`` = pure byte liveness
    (Gemini/Codex).
    """
    # Defensive: a non-positive hard_timeout (a 0/None leaking from a caller's
    # extract_timeout default) would make the watchdog hard-kill instantly.
    if not hard_timeout or hard_timeout <= 0:
        hard_timeout = TASK_TIMEOUT_SEC

    proc = _spawn(cmd, cwd, shell, encoding, errors)
    start = time.monotonic()
    liveness = _Liveness(start)

    stdout_sink: list[str] = []
    stderr_sink: list[str] = []
    delivery = _StdinDelivery(
        expected_chars=len(input_text) if input_text is not None else 0
    )
    threads = [
        threading.Thread(
            target=_make_reader(proc.stdout, stdout_sink, liveness, liveness_lines, encoding, errors),
            daemon=True,
        ),
        threading.Thread(
            target=_make_reader(proc.stderr, stderr_sink, liveness, False, encoding, errors),
            daemon=True,
        ),
        threading.Thread(target=_feed_stdin, args=(proc, input_text, delivery), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        kind = _watchdog_loop(proc, liveness, idle_timeout, hard_timeout, start)

        if kind is not None:
            _tree_kill(proc)
            for thread in threads:
                thread.join(_READER_JOIN_TIMEOUT_SEC)
            # Carry partial output on the exception (subprocess.run did too) so
            # logs/diagnostics can see what the process emitted before the kill.
            exc = subprocess.TimeoutExpired(
                cmd=cmd,
                timeout=hard_timeout if kind == "hard" else idle_timeout,
                output="".join(stdout_sink),
                stderr="".join(stderr_sink),
            )
            exc.timeout_kind = kind
            # A child waiting for a prompt that never arrived idles out and is
            # killed as "hang" — carry the delivery diagnosis so the real cause
            # is visible instead of a bare hang after MAX_HANG_RETRIES.
            # Fail-closed like the normal path: a feeder still blocked in
            # write() when we kill the tree never confirmed delivery, and that
            # is EXACTLY the case this diagnosis exists for — reading only
            # delivery.error would report None there.
            exc.stdin_error = None
            if input_text is not None and not delivery.delivered:
                exc.stdin_error = delivery.error or (
                    f"feeder did not confirm delivery for "
                    f"{delivery.expected_chars} chars before the {kind} timeout"
                )
                # Logged here so the diagnosis has an effect even though the
                # providers map timeouts to the bare codes hang/timeout.
                _log.error(
                    "[stdin] prompt delivery incomplete at %s timeout — %s",
                    kind, exc.stdin_error,
                )
            raise exc

        for thread in threads:
            thread.join(_READER_JOIN_TIMEOUT_SEC)
        # Read AFTER the joins (join() establishes happens-before). Fail-closed:
        # anything short of a confirmed delivery is an error — a feeder still
        # blocked in write() while the child already exited means the child
        # stopped reading early, i.e. the prompt tail never made it.
        stdin_error = None
        if input_text is not None and not delivery.delivered:
            stdin_error = delivery.error or (
                f"feeder did not confirm delivery within {_READER_JOIN_TIMEOUT_SEC}s "
                f"for {delivery.expected_chars} chars"
            )
            if delivery.error is None:  # unlogged path → log it here
                _log.error("[stdin] prompt delivery incomplete — %s", stdin_error)
        return WatchdogResult(
            returncode=proc.returncode,
            stdout="".join(stdout_sink),
            stderr="".join(stderr_sink),
            stdin_error=stdin_error,
        )
    finally:
        # Close the pipe FDs we opened. subprocess.run did this via its context
        # manager; without it every call leaks 2-3 FDs → exhaustion in --watch.
        # Runs after the joins so a reader never loses its stream mid-read.
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
