"""opencode CLI provider — third external voice, Stufe 2 (tag-activated only).

Third non-Claude, non-Codex voice next to Mistral Vibe (a fourth training
lineage, and — via the handpicked `openrouter/zdr-review*` aliases — the only
ZDR-guaranteed review path for customer code). Registered ONLY when
`opencode.exe` resolves AND both agents this repo depends on
(``extern-review``/``extern-dev``) are configured in ``opencode.json``, and
deliberately kept OUT of the dispatcher's fallback chain (Stufe 3, not built
here): activation needs an explicit ``#opencode`` / ``#opencode_<alias>`` tag.
A tag without a registered provider parks the task rather than degrading to
another executor — see ``dispatcher._NO_FALLBACK_PROVIDERS`` (opencode is a
member for a different reason than Vibe: not blast radius, since this provider
does write, but the intent of the tag itself — see that constant's docstring).

CLI facts, all measured 2026-09-04 against opencode 1.18.27 unless noted:

* ``shutil.which("opencode")`` resolves only the npm-installed ``.CMD``/``.ps1``
  shims, never the real executable. ``subprocess`` can start a ``.cmd`` only via
  ``shell=True``, and there a quote embedded in an argument (a task prompt can
  contain any character) breaks out of Windows' cmd.exe quoting — so this
  provider resolves past the shim to the real
  ``<npm-root>/node_modules/opencode-ai/bin/opencode.exe`` instead of ever
  spawning through cmd.exe. No resolvable ``.exe`` → ``None`` → not registered.
  Registered-but-broken (a shell escape bug in production) was judged worse
  than simply absent.
* ``-f`` is a yargs ARRAY flag: anything following it that isn't itself a
  recognised flag is swallowed as a second filename. The short instruction
  message must sit BEFORE ``-f`` in argv, never after.
* stdout carries raw ANSI escapes (``\x1b[0m``, ``\x1b[91m``) — stripped before
  ``RunResult.output`` is filled, or every review would carry escape-code
  garbage.
* ``exit 1`` is ambiguous: an unknown model alias exits 1 with a generic
  ``UnknownError``, indistinguishable from an actual API-level failure by
  return code alone. Classification therefore scans the (ANSI-stripped)
  combined stdout+stderr text for keywords, not just the return code.
* ``--variant`` exists (the 2026-08-28 note that it didn't was stale) and
  tolerates undocumented values — ``--variant xhigh`` exits 0 — so `#effort:`
  values are passed through raw here, no mapping table, matching the CLI's own
  looseness.
* opencode reports no token/cost numbers (measured: `cost=0` in 142/142 real
  log lines), so ``RunResult`` stays at the dataclass default of 0 for every
  token field — the orchestrator's char/duration estimate takes over exactly
  as it does for Codex and Vibe.
* A real ``-f`` + ``--agent extern-review`` + ``--dir`` run returns 0 with the
  agent reading the attachment through its own ``read`` tool — the delivery
  path is confirmed working end-to-end, not just plausible from the CLI help.

Prompt delivery is a REAL FILE via ``-f``, never stdin: ``run_with_watchdog``
is called with ``input_text=None``. That is not an omission — it structurally
removes the whole ``stdin_incomplete`` tail-loss contract every other provider
in this repo carries (see ``providers/process_runner.py``'s ``_feed_stdin``
docstring): there is no pipe to lose the tail of, because the prompt is fully
on disk before the process is ever spawned.

Prompt cap (``config.OPENCODE_MAX_PROMPT_BYTES``, default 50 000) applies in
BOTH read_only and write modes and is enforced BEFORE any subprocess call —
above it: ``RunResult(success=False, error="prompt_too_large")``, never a
silent truncation. This is not caution for its own sake: a ``-f`` file living
outside ``--dir`` cannot be reread by either agent once attached, because
``external_directory: deny`` is the LAST matching rule in opencode's
permission list — including for the one path (``%TEMP%/opencode/*``) that
looked like an allowed exception on inspection (measured 2026-09-04: a re-read
attempt from both a scratchpad path and that path failed identically). Above
the cap the honest alternative to failing is not "the agent fetches the rest"
but "the reviewer silently judges a fraction of the material and never says
so" — exactly what happened in the 2026-08-28 measurement this cap replaces
(24 % of a 222 KB file arrived, 5 of 6 anchors missing, model began guessing
at their location instead of reporting the gap).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import config
from providers.base import BaseProvider, RunResult
from providers.process_runner import run_with_watchdog

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A picker's stdout is only trusted if it looks like a single bare model id
# token -- no whitespace, no multi-line noise, capped so a runaway/garbage
# response can't be treated as a model id.
_PICKER_TOKEN_RE = re.compile(r"^\S{1,200}$")

# --- Error classification: the two directions are NOT symmetric ---------------
#
# `exit 1` is ambiguous here (an unknown model alias yields a generic
# "UnknownError", measured 2026-09-04), so the code has to be read out of the
# output text. Scanning for bare status numbers is what vibe.py and codex.py do,
# but for opencode the cost of guessing wrong differs per direction, so the two
# checks are tightened to different degrees:
#
# * rate_limit IS in providers.base.TRANSIENT_ERRORS -> a false positive costs a
#   30-minute cooldown and a requeue. A false NEGATIVE is the expensive one: no
#   cooldown, so the task rotates on and every attempt is another PAID run. Kept
#   deliberately generous — only digit boundaries are added, so "14290" or
#   "4029" no longer match while a bare "429" still does.
# * auth_error is NOT transient -> orchestrator.py:711 bails out of the in-run
#   backoff for TRANSIENT_ERRORS only, so a NON-transient code runs the full
#   MAX_RETRIES_PER_PROVIDER (2) attempts: a false auth_error costs TWO paid
#   opencode runs where rate_limit costs one.
#
#   That inverts the reasoning this block carried until 2026-09-04, which said a
#   false auth_error "stamps the task terminally failed and no retry ever
#   happens". Measured, it does not: `grep -rn auth_error` finds no consumer
#   outside taxonomy.py's category map, and an UNclassified error reaches the
#   same non-transient outcome via error_code_of() == "". The difference between
#   auth_error and a generic failure is therefore ONLY the label in analytics --
#   the task's fate is identical either way.
#
#   And a label is a bad thing to pay a false positive for. Hence the split
#   below: the auth check reads STDERR ONLY, never the agent's answer. Measured
#   2026-09-04 across three runs: stdout carries exclusively what the agent
#   produced ("OK", the review text), stderr exclusively opencode's own
#   protocol (the "> extern-review · zdr-review" header, tool calls, its own
#   error lines). Words like "unauthorized" or "forbidden" and phrases like
#   "HTTP 403" are ordinary security-review vocabulary, so scanning the answer
#   for them buys nothing and mislabels a run whose only real problem is
#   something else. A genuine auth failure that opencode reports on stdout alone
#   degrades to a generic error -- same retries, same outcome, one less label.
#   NOT measured, named as the gap: which stream opencode uses for an actual
#   401/403 (provoking one needs the machine config, which stays untouched).
_RATE_LIMIT_TEXT = (
    "rate limit", "too many requests", "quota",
    # The 402 family. OpenRouter's own wording is "requires more credits", but
    # the cap can also be hit mid-run by a DIFFERENT consumer of the same key
    # (the HTTP provider shares it, see the module docstring), and the vendor
    # phrasing for that case is not pinned by any measurement we have. All of
    # these are multi-word on purpose: a bare "credits" or "balance" is ordinary
    # review vocabulary and would put a false rate_limit on a real answer.
    "requires more credits", "insufficient credits", "not enough credits",
    "out of credits", "insufficient balance", "payment required",
)
_RATE_LIMIT_CODE_RE = re.compile(r"(?<!\d)(?:429|402)(?!\d)")
_AUTH_TEXT = ("unauthorized", "forbidden", "invalid api key", "authentication failed")
# The status number must sit within a short distance of an HTTP-shaped word --
# "status: 401", "HTTP 403", "code=401" all match, prose like "die 403 Zeilen" does not.
_AUTH_CODE_RE = re.compile(r"(?:http|https|status|statuscode|code|err(?:or)?)\W{0,8}(?<!\d)(?:401|403)(?!\d)")


def _looks_rate_limited(text: str) -> bool:
    return any(kw in text for kw in _RATE_LIMIT_TEXT) or bool(_RATE_LIMIT_CODE_RE.search(text))


def _looks_auth_failed(text: str) -> bool:
    return any(kw in text for kw in _AUTH_TEXT) or bool(_AUTH_CODE_RE.search(text))

_REQUIRED_AGENTS = ("extern-review", "extern-dev")

# The only part of the run guaranteed to arrive uncut (see module docstring):
# everything else lives in the attached -f file.
_SHORT_MESSAGE = (
    "Lies die angehaengte Datei vollstaendig und fuehre den darin "
    "beschriebenen Auftrag aus."
)


def _resolve_exe() -> str | None:
    """Resolve the real ``opencode.exe``, never the ``.CMD``/``.ps1`` shim.

    Runs at import time (module-level ``_OPENCODE_EXE`` below) as part of
    ``providers/__init__.py``'s import chain, which the dispatcher import
    triggers regardless of whether opencode is wired into the dispatcher's
    tag map — so this must NEVER raise. Every failure mode (no PATH entry, an
    exotic filesystem error walking the candidate path) collapses to ``None``.
    """
    try:
        shim = shutil.which("opencode")
        if not shim:
            return None
        if shim.lower().endswith(".exe"):
            return shim
        candidate = Path(shim).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        return str(candidate) if candidate.is_file() else None
    except OSError:
        return None


_OPENCODE_EXE = _resolve_exe()


def _load_opencode_config() -> dict:
    """Read opencode.json. NEVER raises -- see ``_resolve_exe`` for why that
    matters here: this also runs (via ``is_available()``) on the dispatcher
    import path. A missing file, invalid JSON, or unreadable path all collapse
    to an empty dict, which ``_agents_configured`` then reads as "not set up"."""
    try:
        with open(config.OPENCODE_CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _agents_configured(cfg: dict) -> bool:
    """True if BOTH agents this repo depends on exist in opencode.json's
    ``agent`` map. Only their presence is checked here -- their permission
    shape (read_only vs write-capable) is opencode.json's job, not ours to
    police at runtime."""
    agents = cfg.get("agent")
    if not isinstance(agents, dict):
        return False
    return all(name in agents for name in _REQUIRED_AGENTS)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _write_prompt_file(task: str) -> str:
    """Write ``task`` to a fresh temp file and return its path (caller unlinks
    in a ``finally``). This is the ``-f`` attachment, NOT stdin -- see the
    module docstring for why that removes the stdin_incomplete contract."""
    fd, path = tempfile.mkstemp(prefix="orch_opencode_prompt_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(task)
    except BaseException:
        _unlink_quiet(path)
        raise
    return path


def _unlink_quiet(path: str) -> None:
    """Best-effort temp-file removal. A brief retry covers the same Windows
    handle-release lag after a force-kill that ``process_runner._unlink_quiet``
    guards against; a leaked prompt temp file is harmless either way and must
    never mask the real RunResult."""
    for attempt in range(3):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt < 2:
                time.sleep(0.05)


class OpencodeProvider(BaseProvider):
    name = "opencode"  # no underscore -- analytics.py:543 does split("_")[0]
    supports_sessions = False

    @staticmethod
    def is_available() -> bool:
        """``opencode.exe`` resolvable AND both required agents configured.

        Runs at dispatcher-import time (via ``providers/__init__.py``) -- must
        never raise, wrapped defensively even though both helpers it calls
        already are, so a future change to either can't reopen that hole.
        """
        try:
            if _resolve_exe() is None:
                return False
            return _agents_configured(_load_opencode_config())
        except Exception:
            return False

    @staticmethod
    def _build_command(
        exe: str,
        model: str,
        agent: str,
        *,
        effort: str | None,
        cwd: str | None,
        prompt_path: str,
    ) -> list[str]:
        # yargs array-flag trap: -f must be LAST, with nothing prompt-shaped
        # after it, or it swallows the next token as a second filename.
        cmd = [exe, "run", _SHORT_MESSAGE, "--model", model, "--agent", agent]
        if effort:
            cmd.extend(["--variant", effort])
        if cwd:
            cmd.extend(["--dir", cwd])
        cmd.extend(["-f", prompt_path])
        return cmd

    @staticmethod
    def _build_env() -> dict[str, str]:
        """Full merged copy of the child environment.

        Nothing opencode-specific is injected today -- auth and model config
        come from opencode's own auth.json/opencode.json, not env vars. Still
        passed as an explicit copy rather than leaving ``env=None`` (which
        would let Popen inherit the parent as-is): providers are shared
        singletons run from parallel threads, so the moment a future per-run
        variable is needed here, mutating ``os.environ`` directly would leak
        one run's setting into another's. Matches the pattern in vibe.py/
        codex.py's env handling.
        """
        return os.environ.copy()

    @staticmethod
    def _run_picker() -> str | None:
        """Invoke the (optional) external model picker; None on anything but
        a clean single-token success.

        ``config.OPENCODE_MODEL_PICKER`` defaults to "" -- the picker script
        lives in ``~/.claude/scripts`` and is simply absent on a fresh
        machine, which is the NORMAL case, not a degraded one.
        """
        picker = config.OPENCODE_MODEL_PICKER
        if not picker:
            return None
        try:
            result = subprocess.run(
                [sys.executable, "-X", "utf8", picker, "--profile", config.OPENCODE_PICKER_PROFILE],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        token = (result.stdout or "").strip()
        return token if _PICKER_TOKEN_RE.match(token) else None

    def _resolve_model(self) -> str:
        """Resolution order: forced tag (``#opencode_<alias>``, already
        resolved to a full CLI id upstream via ``config.model_id_for_provider``)
        > picker script > static default. The default is the NORMAL outcome
        for a fresh setup, not a last resort -- see ``_run_picker``."""
        if self._forced_model:
            return self._forced_model
        picked = self._run_picker()
        if picked:
            return picked
        return config.OPENCODE_DEFAULT_MODEL

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = config.TASK_TIMEOUT_SEC,
        read_only: bool = False,
        session_id: str | None = None,  # accepted but ignored (supports_sessions=False)
        resume: bool = False,            # accepted but ignored
    ) -> RunResult:
        if _OPENCODE_EXE is None:
            return RunResult(success=False, error="opencode CLI not found")

        prompt_bytes = len(task.encode("utf-8"))
        if prompt_bytes > config.OPENCODE_MAX_PROMPT_BYTES:
            # See module docstring: above the cap there is no way for either
            # agent to fetch the rest, so failing loudly beats a reviewer
            # silently judging a truncated fraction of the material.
            return RunResult(
                success=False,
                error="prompt_too_large",
                output=(
                    f"{prompt_bytes} bytes > OPENCODE_MAX_PROMPT_BYTES cap "
                    f"({config.OPENCODE_MAX_PROMPT_BYTES})"
                ),
            )

        model = self._resolve_model()
        agent = "extern-review" if read_only else "extern-dev"
        effort = self._forced_effort

        print(f"  [opencode → {agent}/{model}] Führe Task aus...")

        prompt_path = None
        try:
            prompt_path = _write_prompt_file(task)
            cmd = self._build_command(
                _OPENCODE_EXE, model, agent, effort=effort, cwd=cwd, prompt_path=prompt_path
            )
            result = run_with_watchdog(
                cmd,
                # Prompt travels via the -f file written above, NOT stdin --
                # see module docstring for why that removes the whole
                # stdin_incomplete contract for this provider by construction.
                input_text=None,
                cwd=cwd,
                idle_timeout=config.OPENCODE_IDLE_TIMEOUT_SEC,
                hard_timeout=timeout,
                shell=False,
                env=self._build_env(),
            )

            # Stripped before classification too, not just before returning:
            # an escape sequence could in principle sit inside a matched
            # phrase, and RunResult.output must be ANSI-free either way.
            output = _strip_ansi(result.stdout or "").strip()
            stderr = _strip_ansi(result.stderr or "").strip()

            if result.returncode == 0 and output:
                return RunResult(success=True, output=output)

            # exit 1 is ambiguous by itself (see module docstring) -- classify
            # from the actual content instead of trusting the return code.
            combined = f"{output} {stderr}".lower()

            if "data policy" in combined or "zero data retention" in combined:
                return RunResult(success=False, error="policy_block", output=output or stderr)
            if _looks_rate_limited(combined):
                return RunResult(success=False, error="rate_limit", output=output or stderr)
            # stderr only, deliberately narrower than the two checks above --
            # the reasoning is in the comment block over _AUTH_TEXT: this code
            # buys a label, not a different fate, so it must not be earned by
            # words the agent itself wrote.
            if _looks_auth_failed(stderr.lower()):
                return RunResult(success=False, error="auth_error", output=output or stderr)

            # The agent's answer belongs in `output`, never in `error`. The old
            # `error=stderr or output` put a whole review text into the field
            # that gets logged, notified (truncated at 3500 chars) and stored as
            # the failure reason, while RunResult.output — what the orchestrator
            # persists as the task result — stayed empty. A non-zero exit does
            # not mean nothing was produced.
            return RunResult(
                success=False,
                error=stderr or (f"exit {result.returncode} without stderr" if output else "empty output"),
                output=output or stderr,
            )

        except subprocess.TimeoutExpired as exc:
            kind = getattr(exc, "timeout_kind", "hard")
            # Kein Sweeper (siehe Auftrag) -- ob der doppelte taskkill /F /T in
            # _tree_kill hier reicht, ist ungemessen. Nur die Warnung ist der
            # gebaute Teil.
            logger.warning(
                "[opencode] %s-Kill -- moeglicherweise bleibt ein opencode.exe-Waise "
                "zurueck, der die SQLite-Session-DB offenhaelt (ungemessen, ob "
                "_tree_kill das abdeckt -- kein Sweeper darauf gebaut).",
                "idle" if kind == "idle" else "hard",
            )
            return RunResult(success=False, error="hang" if kind == "idle" else "timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="opencode CLI not found")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e))
        finally:
            if prompt_path:
                _unlink_quiet(prompt_path)
