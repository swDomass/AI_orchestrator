"""Claude Code CLI provider.

Runs claude with full tool access (Read, Write, Edit, Bash, Glob, Grep) in non-interactive mode.
Billing: subscription login by default; with ORCH_CLAUDE_API_KEY the run is paid from API
credits instead (see claude_billing, docs/adr/005-claude-api-credits-abrechnungsweg.md).
Uses --output-format stream-json --verbose to capture incremental NDJSON events
(liveness signal for the hang watchdog) and actual token usage for capacity
estimation. The final type=="result" event carries .result + .usage.
"""

import contextlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import TypedDict

import claude_billing
from config import TASK_IDLE_TIMEOUT_SEC, TASK_TIMEOUT_SEC
from providers.base import BaseProvider, RunResult
from providers.process_runner import run_with_watchdog

logger = logging.getLogger(__name__)

_CLAUDE_CMD = shutil.which("claude") or "claude"


class _Tokens(TypedDict):
    """The four token fields of RunResult (typed so ``**tokens`` is checkable)."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


@dataclass
class _StreamMeta:
    """Billing facts read from one stream-json run (see ClaudeProvider._scan_stream)."""

    billing: str = ""  # "abo" | "api" | "" (no init event)
    session_id: str = ""
    message_ids: list[str] = field(default_factory=list)
    assistant_errors: list[str] = field(default_factory=list)
    error_text: str = ""  # text of assistant events that carry an error
    cost_usd: float | None = None
    is_error: bool = False
    api_error_status: int | None = None
    result_text: str = ""


class ClaudeProvider(BaseProvider):
    name = "claude"
    supports_sessions = True

    @staticmethod
    def _build_command(
        read_only: bool,
        session_id: str | None = None,
        resume: bool = False,
    ) -> list[str]:
        cmd = [
            _CLAUDE_CMD,
            "--print",
            # stream-json emits one NDJSON event per line (init/assistant/tool_use/
            # result) → incremental liveness signal for the hang watchdog. --verbose
            # is required for stream-json in --print mode.
            "--output-format", "stream-json",
            "--verbose",
            # Move per-machine sections (cwd, env, git status) from system-prompt
            # into the first user message → static system-prompt → Anthropic prompt
            # cache hits across sequential subprocess calls (1h TTL).
            "--exclude-dynamic-system-prompt-sections",
        ]
        # Session flags: --session-id starts a NEW session with the given UUID;
        # --resume continues an EXISTING session. Caller must track state.
        # If CLAUDE_SESSION_ENABLED is False, session_id is silently ignored.
        from config import CLAUDE_SESSION_ENABLED
        if session_id and CLAUDE_SESSION_ENABLED:
            if resume:
                cmd.extend(["--resume", session_id])
            else:
                cmd.extend(["--session-id", session_id])
        if read_only:
            # Agent is included so read-only multi-agent flows (deep-security-audit
            # _run_subagent_mode style) can fan out to subagents even without
            # write permissions. Subagents inherit the parent's tool scope.
            #
            # The tool is named `Agent`, NOT `Task` (verified 2026-07-30 against the
            # tools reference: every `Task*` name is a task-LIST tool — TaskCreate,
            # TaskGet, TaskList, TaskOutput, TaskStop, TaskUpdate — none of them
            # spawns subagents). An unresolvable name in --allowedTools simply never
            # matches, so `Task` left the Agent tool un-allowlisted here: headless
            # runs cannot answer a permission prompt, so the fan-out was denied and
            # degraded to exactly the monolithic single-perspective run this
            # allowlist exists to prevent — silently, with every check green.
            cmd.extend(["--allowedTools", "Read,Glob,Grep,Agent"])
        else:
            cmd.extend([
                "--dangerously-skip-permissions",
                # Agent is required for tools that orchestrate via Claude's
                # internal subagent system (deep-security-audit subagent-mode).
                # Without it, the master prompt's "spawn 6 subagents in parallel"
                # silently degrades to monolithic single-perspective.
                # (Here --dangerously-skip-permissions already bypasses the
                # allowlist; the correct name still matters so this path doesn't
                # re-seed the wrong one when the bypass is ever dropped.)
                "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,Agent",
            ])
        return cmd

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = TASK_TIMEOUT_SEC,
        read_only: bool = False,
        session_id: str | None = None,
        resume: bool = False,
    ) -> RunResult:
        model_label = self._forced_model
        if model_label:
            print(f"  [claude → {model_label}] Führe Task aus...")
        else:
            print("  [claude] Führe Task aus...")
        cmd = self._build_command(read_only=read_only, session_id=session_id, resume=resume)
        if self._forced_model:
            cmd.extend(["--model", self._forced_model])
        # `--effort <level>` (low|medium|high|xhigh|max, see config.CLAUDE_EFFORT_LEVELS)
        # — set via the #effort: queue tag. Appended ONLY when explicitly requested:
        # without a tag the CLI keeps its own session default, which must not be
        # silently overridden from here.
        if self._forced_effort:
            cmd.extend(["--effort", self._forced_effort])

        use_api = claude_billing.api_active()
        env = claude_billing.child_env() if use_api else None
        # Resuming: the transcript already holds earlier (possibly subscription)
        # messages - only IDs that are new in this run belong in the ledger.
        prior_ids = (
            set(claude_billing.transcript_message_ids(session_id))
            if use_api and resume and session_id else set()
        )
        # Ledger rows appear only after the run: the in-flight counter covers the
        # API attempt up to its ledger entry (calibration samples are skipped
        # meanwhile, see quota_calibration.log_calibration_sample). Not the retry.
        with claude_billing.api_run_in_flight() if use_api else contextlib.nullcontext():
            res, meta, failure = self._attempt(
                cmd, task=task, cwd=cwd, timeout=timeout, env=env, use_api=use_api,
            )
            if res.billing == "api":
                # Tokens were spent on credits (also of a failed attempt, e.g. a
                # 429 mid-run) → keep them out of the subscription calibration.
                self._record_api_run(meta, exclude=prior_ids)
        if failure:
            # Credit/auth/rate failure on the API path: block it (one Telegram notice
            # per episode) and repeat the SAME run exactly once on the subscription.
            claude_billing.trip(failure)
            logger.warning("Claude API run failed (%s) - repeating once on subscription", failure)
            print(f"  [claude] API-Abrechnung fehlgeschlagen ({failure}) → Wiederholung über Abo.")
            # `--session-id X` of the failed attempt may already exist ("already in
            # use") → continue that session instead (copy, cmd stays untouched).
            retry_cmd = ["--resume" if a == "--session-id" else a for a in cmd]
            res, meta, _ = self._attempt(
                retry_cmd, task=task, cwd=cwd, timeout=timeout, env=None, use_api=False,
            )
            return res
        if res.billing == "api" and res.success and use_api:
            claude_billing.clear()
        return res

    @staticmethod
    def _record_api_run(meta: "_StreamMeta", exclude: "set[str] | frozenset[str]" = frozenset()) -> None:
        """Ledger entry: stream IDs plus the transcript IDs (incl. subagents),
        minus ``exclude`` (IDs that predate a resumed run)."""
        ids = list(dict.fromkeys(
            i for i in [*meta.message_ids, *claude_billing.transcript_message_ids(meta.session_id)]
            if i not in exclude
        ))
        claude_billing.record_api_run(meta.session_id, ids)

    def _attempt(
        self, cmd: list[str], *, task: str, cwd: str | None, timeout: int,
        env: dict[str, str] | None, use_api: bool,
    ) -> "tuple[RunResult, _StreamMeta, str | None]":
        """One CLI run + billing facts. Third item: ``"credit"``/``"auth"``/``"rate"``
        when the run failed on the API path because of billing/authentication/a 429
        rate limit, else None."""
        sink: dict[str, str] = {}
        res = self._run_once(cmd, task=task, cwd=cwd, timeout=timeout, env=env, sink=sink)
        meta = self._scan_stream(sink.get("stdout", ""))
        res.billing = meta.billing or ("api" if use_api else "abo")
        res.cost_usd = meta.cost_usd
        failure = None
        if use_api and res.billing == "api" and (not res.success or meta.is_error):
            failure = self._billing_failure(res, meta, sink.get("stderr", ""))
        return res, meta, failure

    def _run_once(
        self, cmd: list[str], *, task: str, cwd: str | None, timeout: int,
        env: dict[str, str] | None, sink: dict[str, str],
    ) -> RunResult:
        try:
            result = run_with_watchdog(
                cmd,
                input_text=task,
                cwd=cwd,
                idle_timeout=TASK_IDLE_TIMEOUT_SEC,
                hard_timeout=timeout,
                # claude resolves to a native executable, so no shell is needed to
                # launch it. shell=False removes the Windows cmd.exe layer whose
                # forwarding of a >64 KB stdin stream was a source of the silent
                # prompt-tail loss. stdin_via_file then delivers the (large) prompt
                # as a real file with a deterministic EOF instead of a raced pipe
                # feed, so the task text at the prompt's END can no longer be lost.
                shell=False,
                liveness_lines=True,  # NDJSON-event-aware: tool_use pauses idle timer
                stdin_via_file=True,
                env=env,
            )

            stdout = (result.stdout or "").strip()
            stderr = self._strip_connector_notice((result.stderr or "").strip())
            sink["stdout"], sink["stderr"] = stdout, stderr

            # Parse the final NDJSON result event ONCE; derive output + tokens.
            result_event = self._extract_result_event(stdout)
            output = self._output_from_event(result_event, fallback=stdout)
            tokens = self._tokens_from_event(result_event)

            # An incompletely delivered prompt produces a PERFECTLY well-formed
            # run: the CLI answers the truncated prompt, exits 0 and emits a
            # subtype=="success" result event. Nothing downstream can tell the
            # task text was missing, so it must be caught before the success
            # evaluation. getattr(): provider tests fake the watchdog result
            # with a three-field SimpleNamespace.
            #
            # Bare code, no suffix: the orchestrator matches error codes by
            # exact equality (see the note in providers/gemini.py), and a
            # variable suffix would both miss those branches and make every
            # incident a unique error_code in analytics. Detail goes to output
            # and to the _log.error in process_runner._feed_stdin.
            # Deliberately NOT checked here at the top: a child that dies early
            # (rate limit, missing session) ALSO breaks the stdin pipe, so an
            # up-front check masks those better-informed classifications. It is
            # applied where it is the ONLY explanation — inside the success
            # branch, and as a last resort before the generic fallback.
            stdin_error = getattr(result, "stdin_error", None)

            # A clean run MUST carry a top-level type=="result" event. With
            # --output-format stream-json the absence of a result line is a real
            # failure mode (truncated/partial stream, --verbose noise without a
            # result, stream-json error). Without this guard, rc=0 + raw NDJSON
            # falling back into `output` would be finalized as success=True and
            # falsely satisfy #needs: deps. Require result_event explicitly.
            if (
                result.returncode == 0
                and result_event is not None
                and output
                and self._is_success_output(
                    output=output,
                    json_payload=result_event,
                )
            ):
                # The run looks clean — but a truncated prompt produces exactly
                # this shape: the CLI happily answers the context-only remainder
                # and reports subtype=="success". This is the one place where an
                # incomplete delivery is the ONLY possible explanation, so it
                # wins here and nowhere earlier.
                if stdin_error:
                    return RunResult(
                        success=False, error="stdin_incomplete",
                        output=stdin_error, **tokens,
                    )
                return RunResult(success=True, output=output, **tokens)

            # Keyword detection for rate_limit/session_missing must NOT scan the
            # assistant's answer text — a SUCCESS result's prose mentioning "rate
            # limit"/"quota" would false-positive a bogus cooldown. Scan only
            # signal-bearing surfaces: stderr, dedicated rate_limit_event / error
            # NDJSON lines, the result event's subtype, and the result text ONLY
            # when it is an error result.
            scan_parts = [stderr]
            for line in stdout.splitlines():
                # Both spacings: the CLI currently emits compact JSON, but anything
                # produced by a plain json.dumps writes `"type": "error"` with a
                # space, and the substring test silently missed those — measured by
                # an external review, which fed such an event and got the raw JSON
                # back instead of `rate_limit`. Still a substring test rather than a
                # per-line json.loads: this runs over every line of a possibly huge
                # NDJSON stream, and the two spellings are what real emitters produce.
                if ("rate_limit_event" in line
                        or '"type":"error"' in line
                        or '"type": "error"' in line):
                    scan_parts.append(line)
            if result_event is not None:
                subtype = result_event.get("subtype")
                if isinstance(subtype, str):
                    scan_parts.append(subtype)
                if subtype != "success":
                    err_text = result_event.get("result")
                    if isinstance(err_text, str):
                        scan_parts.append(err_text)
            combined = " ".join(scan_parts).lower()
            # Typed error: --resume against a non-existent UUID errors with this
            # exact phrase. Tools should fall back to a fresh session + state inject.
            if "no conversation found with session id" in combined:
                return RunResult(success=False, error="session_missing", **tokens)
            # OAuth login expired (measured 2026-09-09 in logs/runs.jsonl, three
            # consecutive task runs 09:25-10:36 all carrying this exact stderr text
            # verbatim as the raw, unclassified error_code): "Failed to authenticate:
            # OAuth session expired and could not be refreshed". Distinct from
            # "auth_error" (the code the HTTP-based providers use for a missing/
            # rejected API key) — this is the interactive CLI login itself, which
            # only a human `claude login` fixes. Matched narrowly on the literal
            # observed phrase rather than the broader "auth"/"token"/"expired"
            # keywords heartbeat._PROBE_TRANSIENT_KEYWORDS uses for its low-stakes
            # model-alive probe: those three words individually are common enough in
            # ordinary coding-task prose (a task ABOUT implementing OAuth) that a
            # broad match risks reclassifying an unrelated failure. The narrow phrase
            # is still safe against a SUCCESS answer discussing OAuth — `combined`
            # above already excludes success-result prose (only stderr, the
            # rate_limit_event/error NDJSON lines, and a non-success result's own
            # text are scanned).
            if "oauth session expired" in combined:
                return RunResult(success=False, error="auth_expired", **tokens)
            # "session limit" was added 2026-09-10 after it cost a finished two-hour
            # dev-loop. The CLI says "You've hit your session limit · resets 1:30am
            # (Europe/Vienna)" — none of the four older keywords match it, so the raw
            # prose escaped as RunResult.error, error_code_of() returned "",
            # is_transient() returned False, and orchestrator.py:1733 stamped the
            # queue line terminally failed. Measured over logs/ plus all four
            # runs-archive months: exactly two limit phrasings occur, "rate limit: {"
            # (already covered) and this one, 5x each. Deliberately NOT a bare
            # "limit" — that would match ordinary prose. This tuple is safe to widen
            # only because `combined` above is restricted to signal-bearing surfaces;
            # a SUCCESS result whose answer text discusses session limits never
            # reaches here.
            if any(kw in combined for kw in
                   ("rate limit", "usage limit", "session limit", "quota", "overloaded")):
                return RunResult(success=False, error="rate_limit", **tokens)

            # No better classification matched — but ONLY trust this at rc == 0.
            # At rc != 0 the broken pipe is almost always a SYMPTOM: any CLI
            # dying early (not logged in, model not found, a panic) also breaks
            # the feeder, because the orchestrator prompt (~100 KB) far exceeds
            # the OS pipe buffer (4-64 KB). Booking those as stdin_incomplete
            # would bury the real cause, so below it is appended, not swapped in.
            if stdin_error and result.returncode == 0:
                return RunResult(
                    success=False, error="stdin_incomplete",
                    output=stdin_error, **tokens,
                )

            # rc==0 but no result event → incomplete/partial stream. Surface a
            # clear error instead of passing the raw NDJSON blob through as if
            # it were the answer.
            if result.returncode == 0 and result_event is None:
                return RunResult(
                    success=False,
                    error=stderr or "incomplete stream-json output (no result event)",
                    **tokens,
                )

            return RunResult(
                success=False,
                error=stderr or output or "empty output",
                # Keep the real CLI message as the classification, carry the
                # delivery diagnosis alongside it instead of losing it.
                output=(f"{output}\n[stdin] {stdin_error}" if stdin_error else output),
                **tokens,
            )

        except subprocess.TimeoutExpired as exc:
            # Partial output of the killed run: session_id/message_ids still reach
            # the ledger (credits were spent) via _scan_stream.
            sink["stdout"] = self._as_text(exc.output).strip()
            sink["stderr"] = self._strip_connector_notice(self._as_text(exc.stderr).strip())
            kind = getattr(exc, "timeout_kind", "hard")
            return RunResult(success=False, error="hang" if kind == "idle" else "timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="claude CLI not found")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e))

    # Billing-failure signals (see docs/adr/005-claude-api-credits-abrechnungsweg.md).
    # Deliberately generous: a false alarm locks the credit path for up to 24 h (all
    # Claude runs then go via the subscription, i.e. the state before this change)
    # and sends a false credit/key alert via Telegram; a blind spot would instead let
    # every run fail on the API path with no fallback (permanent failed attempts).
    _BILLING_TEXT_RE = re.compile(
        r"credit balance|usage limits? (reached|exceeded)|reached your specified"
        r"|invalid api key|invalid x-api-key|billing",
        re.IGNORECASE,
    )
    _AUTH_TEXT_RE = re.compile(r"invalid api key|invalid x-api-key", re.IGNORECASE)
    # Real rate-limit vocabulary; "overloaded"/"quota" alone are not (see _billing_failure).
    _RATE_TEXT_RE = re.compile(r"rate[ _-]?limit|\b429\b", re.IGNORECASE)

    @staticmethod
    def _as_text(data: "str | bytes | None") -> str:
        if data is None:
            return ""
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else data

    @staticmethod
    def _strip_connector_notice(stderr: str) -> str:
        """Drop the CLI's per-run "claude.ai connectors are disabled because
        ANTHROPIC_API_KEY ..." warning (API mode): it is no failure signal and would
        otherwise become RunResult.error / feed the auth classification."""
        return "\n".join(
            ln for ln in stderr.splitlines()
            if "claude.ai connectors are disabled because" not in ln
        ).strip()

    @staticmethod
    def _scan_assistant(meta: "_StreamMeta", evt: dict, seen: set[str]) -> None:
        """message.id of every assistant event (also subagent ones), plus the
        ``error`` field and the text of events that carry one."""
        msg = evt.get("message")
        msg = msg if isinstance(msg, dict) else {}
        mid = msg.get("id")
        if isinstance(mid, str) and mid and mid not in seen:
            seen.add(mid)
            meta.message_ids.append(mid)
        err = evt.get("error")
        if isinstance(err, str) and err:
            meta.assistant_errors.append(err)
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    meta.error_text += " " + block["text"]

    @staticmethod
    def _scan_stream(stdout: str) -> "_StreamMeta":
        """Collect billing facts from all NDJSON events (init/assistant/result)."""
        meta = _StreamMeta()
        seen: set[str] = set()
        for raw in (stdout or "").splitlines():
            line = raw.strip()
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            etype = evt.get("type")
            if etype == "system" and evt.get("subtype") == "init":
                src = evt.get("apiKeySource")
                if isinstance(src, str):
                    meta.billing = "abo" if src == "none" else "api"
                if isinstance(evt.get("session_id"), str):
                    meta.session_id = evt["session_id"]
            elif etype == "assistant":
                ClaudeProvider._scan_assistant(meta, evt, seen)
            elif etype == "result":
                cost = evt.get("total_cost_usd")
                meta.cost_usd = float(cost) if isinstance(cost, (int, float)) else None
                if isinstance(evt.get("session_id"), str):
                    meta.session_id = evt["session_id"]
                meta.is_error = evt.get("is_error") is True
                status = evt.get("api_error_status")
                meta.api_error_status = status if isinstance(status, int) else None
                text = evt.get("result")
                meta.result_text = text if isinstance(text, str) else ""
        return meta

    @classmethod
    def _billing_failure(cls, res: RunResult, meta: "_StreamMeta", stderr: str) -> str | None:
        """``"auth"``/``"credit"`` for a billing/authentication failure, ``"rate"``
        for a 429 (rate limit of the Console organisation), else None.

        Only called on the API path (caller checks). ``"rate"`` lets ``run()`` repeat
        once on the subscription with a short block instead of a provider cooldown.
        The ``rate_limit`` assistant code counts only without an explicit non-429
        status (same rule as the keyword path below).
        Not billing: server errors, timeouts/hangs (classified elsewhere); overload/5xx
        (529, "overloaded") is no ``rate`` either and takes the normal cooldown path.
        """
        if res.error in ("hang", "timeout"):
            return None
        if meta.api_error_status == 429 or (
            "rate_limit" in meta.assistant_errors and meta.api_error_status is None
        ):
            return "rate"
        errors = set(meta.assistant_errors)
        by_code = bool(errors & {"billing_error", "authentication_failed"}) or (
            meta.api_error_status in (401, 402)
        )
        text = " ".join(
            [stderr, meta.error_text, meta.result_text if meta.is_error else ""]
        )
        by_text = cls._BILLING_TEXT_RE.search(text) is not None
        if not (by_code or by_text):
            # Classified as rate_limit by keyword without a 429: only real rate-limit
            # text counts, and no explicit non-429 status (529 overload is not a rate).
            if (res.error == "rate_limit"
                    and meta.api_error_status in (None, 429)
                    and cls._RATE_TEXT_RE.search(text)):
                return "rate"
            return None
        if (meta.api_error_status == 401 or "authentication_failed" in errors
                or cls._AUTH_TEXT_RE.search(text)):
            return "auth"
        return "credit"

    @staticmethod
    def _extract_result_event(stdout: str) -> dict | None:
        """Return the LAST NDJSON event with type=='result', or None.

        stream-json emits one JSON object per line (init, assistant, tool_use,
        result). The final result event carries .result (text) and .usage. A
        single-object json payload on one line is parsed as one line too, so the
        legacy json-mode tests still pass through unchanged.

        ⚠️ "LAST result wins" is only safe because subagent events never enter
        this stream. Forwarding is opt-in via `--forward-subagent-text` (verified
        against `claude --help`, 2026-07-30) and `_build_command` does NOT pass
        it. Do NOT add that flag for a better liveness signal without first
        filtering by session/agent here: a forwarded SUBAGENT result event would
        be the last `type=="result"` line and silently become the task's answer —
        a well-formed, subtype=="success" run carrying the wrong output, i.e. the
        same failure class as the 2026-07-20/24/25 prompt-tail incidents.
        """
        if not stdout:
            return None
        last_result = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict) and evt.get("type") == "result":
                last_result = evt
        return last_result

    @staticmethod
    def _is_success_output(output: str, json_payload: dict | None) -> bool:
        if not output:
            return False
        if json_payload is None:
            return True

        subtype = json_payload.get("subtype")
        if isinstance(subtype, str):
            return subtype == "success"

        result = json_payload.get("result")
        return isinstance(result, str) and bool(result.strip())

    _ZERO_TOKENS: _Tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    @staticmethod
    def _output_from_event(result_event: dict | None, fallback: str) -> str:
        """Output text from a result event: its ``.result`` string, else the
        re-serialized event (so structured errors don't re-include CLI noise),
        else ``fallback`` (raw stdout) when there is no result event."""
        if result_event is None:
            return fallback
        result = result_event.get("result")
        return result if isinstance(result, str) else json.dumps(result_event)

    @staticmethod
    def _tokens_from_event(result_event: dict | None) -> _Tokens:
        """The four RunResult token fields from a result event's ``.usage``
        (all-zero when absent). Field parity feeds quota calibration/analytics."""
        if result_event is None:
            return _Tokens(**ClaudeProvider._ZERO_TOKENS)
        usage = result_event.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        def _n(key: str) -> int:
            return int(usage.get(key, 0) or 0)

        return _Tokens(
            input_tokens=_n("input_tokens"),
            output_tokens=_n("output_tokens"),
            cache_creation_input_tokens=_n("cache_creation_input_tokens"),
            cache_read_input_tokens=_n("cache_read_input_tokens"),
        )

    @staticmethod
    def _parse_json_response(stdout: str) -> tuple[str, _Tokens]:
        """Parse Claude CLI JSON/NDJSON response → (output_text, token_dict).

        Falls back to (stdout, all-zero dict) when no result event is present.
        Kept as a single entry point for direct callers/tests; ``run()`` derives
        output + tokens from a once-extracted event via the helpers above.
        """
        if not stdout:
            return stdout, _Tokens(**ClaudeProvider._ZERO_TOKENS)
        event = ClaudeProvider._extract_result_event(stdout)
        return (
            ClaudeProvider._output_from_event(event, fallback=stdout),
            ClaudeProvider._tokens_from_event(event),
        )
