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
  ``<npm-root>/node_modules/@opencode/cli/bin/opencode.exe`` (opencode 1.x:
  ``opencode-ai``) instead of ever
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
* opencode 2.2 (measured 2026-10-28) has neither ``--dir`` nor ``--variant``.
  The radius is ``PWD`` (set to the cwd by the provider, and ``PWD`` beats the
  cwd, see ``_build_env``) plus its git root; the variant is ``--model …#variant``, which 2.2 validates:
  an unknown one (``#xhigh``) exits 1 "Variant unavailable" before any model
  call, so ``run`` repeats once without the variant. ``--standalone`` keeps the
  work in a private server that a process kill reaches (the shared background
  service survives it). `#effort:` values are otherwise passed through raw.
* Instructions/config (measured 2026-10-28): 2.2 loads only ``AGENTS.md`` (also
  upwards beyond the git root under the home dir) and any project
  ``opencode.json``/``opencode.jsonc``/``.opencode/…``, which can lift the default-deny. ``_FORCED_ENV`` switches
  both off; the global sources no env switch reaches make ``run`` fail with
  ``external_instructions`` (``_external_instruction_sources``). XDG_CONFIG_HOME
  is stripped from the child env, not followed; a relative PROGRAMDATA is a find.
* opencode reports no token/cost numbers (measured: `cost=0` in 142/142 real
  log lines), so ``RunResult`` stays at the dataclass default of 0 for every
  token field — the orchestrator's char/duration estimate takes over exactly
  as it does for Codex and Vibe.
* A real ``-f`` + ``--agent extern-review`` run (cwd = repo) returns 0 with the
  agent reading the attachment through its own ``read`` tool — the delivery
  path is confirmed working end-to-end, not just plausible from the CLI help.
  Re-measured under 2.2 on 2026-10-28 through ``OpencodeProvider.run`` itself
  (``--standalone``, cwd + ``PWD`` from ``_build_env``, an inherited foreign
  ``PWD`` in the parent): the session ran in the repo, a read one level up was
  ``permission.rejected`` (external_directory), and ``#xhigh`` hit "Variant
  unavailable" and was repeated once without the variant, successfully.

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
outside the radius (see above) cannot be reread by either agent once attached, because
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
from collections.abc import Callable
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
# Picker ladder (S7). Exit codes of oc_pick_model.py that matter here; the
# pseudo codes stand for "no exit code" (timeout/OSError) and "exit 0, unusable stdout".
_PICKER_STUFEN = ("1", "2", "ausweich")
_PICKER_CONFIG = 4
_PICKER_UNOCCUPIED = 5
_PICKER_STALE_PRICES = 12
_PICKER_FAILED = -1
_PICKER_UNUSABLE = -2
# Less remaining total time than this: no further rung (a rung needs minutes).
_MIN_STAGE_SEC = 60

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
#   same non-transient outcome via error_code_of() == "". For the orchestrator,
#   auth_error and a generic failure differ only by label.
#   Inside the picker ladder the class does decide the fate: hang, empty output,
#   an unclassified error and policy_block from opencode go on to the next rung;
#   rate_limit, auth_error, timeout, prompt_too_large and a missing CLI do not.
#   Cost: worst case ladder (3 rungs) x MAX_RETRIES_PER_PROVIDER (2) paid runs
#   per pass; a hang at the end of the ladder requeues the task up to
#   MAX_HANG_RETRIES (2) times and each pass runs the ladder again:
#   3 x 2 x (1 + 2) = 18 paid runs per task, capped by the daily budget and
#   OPENCODE_MIN_REMAINING_USD (0.25). On top: one short probe per picker
#   query (at most 3 per pass, so up to 18 probes per task; fractions of a
#   cent each).
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
_VARIANT_UNAVAILABLE = "variant unavailable"
# Inherited opencode config overrides could replace the agents' default-deny
# permissions (e.g. OPENCODE_PERMISSION={"*":"allow"}); same list as oc_run.py.
# OPENCODE_CONFIG_PATH is the orchestrator's own variable and stays.
# XDG_CONFIG_HOME is stripped too: otherwise opencode reads the global config
# (and the default-deny) from another folder than the one we check, and a
# relative value would resolve against the task repo in the child.
_STRIPPED_ENV = frozenset({"OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT",
                           "OPENCODE_CONFIG_DIR", "OPENCODE_PERMISSION",
                           "XDG_CONFIG_HOME"})

# Always forced in the child env (inherited values are overwritten): no project
# config (neither above nor inside the repo: those can lift the default-deny),
# no AGENTS.md walk upwards, no .claude skill scan. Pitfall: opencode 2.2 reads
# ``OPENCODE_CONFIG_PROJECT_DISABLE ?? OPENCODE_DISABLE_PROJECT_CONFIG``, so an
# inherited ``OPENCODE_CONFIG_PROJECT_DISABLE=0`` would mask the second name --
# both are set. Measured 2026-10-28, see module docstring.
_FORCED_ENV = {"OPENCODE_DISABLE_PROJECT_CONFIG": "1",
               "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
               "OPENCODE_DISABLE_CLAUDE_CODE": "1"}

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
        # opencode 2.x ships as @opencode/cli; the 1.x layout (opencode-ai) is the fallback.
        for pkg in ("@opencode/cli", "opencode-ai"):
            candidate = Path(shim).parent / "node_modules" / pkg / "bin" / "opencode.exe"
            if candidate.is_file():
                return str(candidate)
        return None
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


def _zdr_contract_violation(cfg: dict, full_id: str) -> str | None:
    """None when *full_id* resolves to an alias carrying the full ZDR contract.

    The restriction lives on the ALIAS, never on the model: under
    `provider.openrouter` there is no provider-wide `options` block, so a raw
    model id goes out with no data-protection option at all (A/B measured
    2026-08-21 — same model, two throwaway aliases, only the option differing:
    without it "pong" and exit 0, with it "No endpoints found matching your
    data policy" and exit 1). Existence alone is therefore not enough; both
    halves have to be there. Pure function of ``(cfg, full_id)``; an empty or
    broken ``cfg`` (see ``_load_opencode_config``) yields "fehlt" -- fail-closed.
    Shared by ``doctor.check_opencode_cli`` and ``OpencodeProvider.run``.
    """
    providers_cfg = cfg.get("provider")
    providers_cfg = providers_cfg if isinstance(providers_cfg, dict) else {}
    provider_name, sep, model_key = full_id.partition("/")
    provider_entry = providers_cfg.get(provider_name) if sep else None
    provider_entry = provider_entry if isinstance(provider_entry, dict) else {}
    models = provider_entry.get("models")
    entry = models.get(model_key) if isinstance(models, dict) else None
    if not isinstance(entry, dict):
        return "fehlt in opencode.json"
    options = entry.get("options")
    provider_opts = options.get("provider") if isinstance(options, dict) else None
    provider_opts = provider_opts if isinstance(provider_opts, dict) else {}
    if not (provider_opts.get("data_collection") == "deny" and provider_opts.get("zdr") is True):
        return "ohne data_collection:deny + zdr:true"
    return None


def _agents_configured(cfg: dict) -> bool:
    """True if BOTH agents this repo depends on exist in opencode.json's
    ``agent`` map. Only their presence is checked here -- their permission
    shape (read_only vs write-capable) is opencode.json's job, not ours to
    police at runtime."""
    agents = cfg.get("agent")
    if not isinstance(agents, dict):
        return False
    return all(name in agents for name in _REQUIRED_AGENTS)


def _external_instruction_sources() -> list[str]:
    """Global instruction sources opencode 2.2 loads that no env switch turns
    off: ``~/.config/opencode/AGENTS.md``, an ``instructions`` key in the global
    opencode.json, and the ``opencode`` folder under ``%ProgramData%``. None exists on this machine;
    tests monkeypatch this function. The global folder is always ``~/.config/opencode``
    (``XDG_CONFIG_HOME`` is stripped from the child env, never followed). Project
    config (``opencode.json``, ``opencode.jsonc``, ``.opencode/``) is switched off
    by ``_FORCED_ENV``. A non-empty relative ``PROGRAMDATA`` is reported as
    ``<value>#relative`` (fail closed). An ``opencode.jsonc`` is always a source, and an
    unreadable or non-object ``opencode.json`` is reported as ``#unreadable``."""
    found = []
    base = Path.home() / ".config" / "opencode"
    agents_md = base / "AGENTS.md"
    if agents_md.is_file():
        found.append(str(agents_md))
    jsonc = base / "opencode.jsonc"
    if jsonc.is_file():  # Python cannot parse JSONC: treat as a source
        found.append(str(jsonc))
    plain = base / "opencode.json"
    if plain.is_file():
        try:
            data = json.loads(plain.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception:  # fail closed: unreadable config may hold instructions
            found.append(f"{plain}#unreadable")
        else:
            if "instructions" in data:
                found.append(f"{plain}#instructions")
    program_data = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    if not Path(program_data).is_absolute():
        found.append(f"{program_data}#relative")
    system_dir = Path(program_data) / "opencode"
    if system_dir.is_dir():
        found.append(str(system_dir))
    return found


def _start_blocked() -> RunResult | None:
    """Failure to return instead of starting opencode, or None to start."""
    if _OPENCODE_EXE is None:
        return RunResult(success=False, error="opencode CLI not found")
    external = _external_instruction_sources()
    if external:
        logger.warning("[opencode] global instruction source(s) the env switches "
                       "cannot disable, not starting: %s", external)
        return RunResult(success=False, error=f"external_instructions: {external[0]}")
    return None


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
        prompt_path: str,
    ) -> list[str]:
        # opencode 2.2 has no --dir / --variant: the radius comes from the child's
        # cwd + PWD (see _build_env), the variant rides on the model id. Without
        # --standalone the work runs in a shared background service that a process
        # kill does not reach.
        target = f"{model}#{effort}" if effort else model
        # yargs array-flag trap: -f must be LAST, with nothing prompt-shaped
        # after it, or it swallows the next token as a second filename.
        return [exe, "run", _SHORT_MESSAGE, "--model", target, "--agent", agent,
                "--standalone", "-f", prompt_path]

    @staticmethod
    def _build_env(cwd: str | None) -> dict[str, str]:
        """Full merged copy of the child environment, with the radius pinned.

        opencode 2.2 resolves its working directory as ``process.env.PWD ??
        process.cwd()`` -- PWD beats the cwd Popen sets. A PWD inherited from the
        parent (Git Bash exports it) would silently move the permission radius to
        a foreign directory, so it is set to ``cwd`` or, without a cwd, removed.
        The ``_STRIPPED_ENV`` config/permission overrides are dropped too
        (case-insensitive), so they cannot override the agents' default-deny.
        ``_FORCED_ENV`` is set last, overwriting inherited values.
        Known limit: with ``cwd=None`` the radius is the orchestrator's own cwd
        plus its git root (same as 1.x without ``--dir``).

        A copy rather than ``env=None`` (inherit as-is): providers are shared
        singletons run from parallel threads, so mutating ``os.environ`` would
        leak one run's setting into another's.
        """
        env = {k: v for k, v in os.environ.items()
               if k.upper() not in _STRIPPED_ENV and k.upper() not in _FORCED_ENV}
        env.update(_FORCED_ENV)
        if cwd:
            env["PWD"] = cwd
        else:
            env.pop("PWD", None)
        return env

    @staticmethod
    def _ask_picker(
        aufgabe: str, stufe: str, cwd: str | None, timeout: int,
    ) -> tuple[int, str | None]:
        """Ask the external model picker for one rung of its ladder (at most
        ``timeout`` seconds).

        Returns ``(exit_code, model_id | None)``. The model id is set only for
        exit 0 with a clean single-token stdout. Pseudo codes for what has no exit
        code: ``_PICKER_FAILED`` (timeout/OSError), ``_PICKER_UNUSABLE`` (exit 0,
        stdout not a single token). The caller decides what each code means --
        this function never falls back to anything.

        ``config.OPENCODE_MODEL_PICKER`` defaults to "" -- the picker script
        lives in ``~/.claude/scripts`` and is simply absent on a fresh
        machine, which is the NORMAL case; ``run`` does not call this then.
        """
        directory = os.path.abspath(cwd) if cwd else os.getcwd()
        cmd = [
            sys.executable, "-X", "utf8", config.OPENCODE_MODEL_PICKER,
            "--profile", config.OPENCODE_PICKER_PROFILE,
            "--aufgabe", aufgabe, "--stufe", stufe, "--dir", directory,
        ]
        if aufgabe == "review":
            # Review passes write no ledger line, so a challenger probe would
            # never be measured -- the picker must not spend them on it.
            cmd.append("--ohne-erkundung")
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return _PICKER_FAILED, None
        if result.returncode != 0:
            return result.returncode, None
        token = (result.stdout or "").strip()
        return (0, token) if _PICKER_TOKEN_RE.match(token) else (_PICKER_UNUSABLE, None)

    def _next_pick(
        self, aufgabe: str, stufen: list[str], cwd: str | None, tried: set[str],
        rest: Callable[[], int | None] = lambda: None,
    ) -> tuple[str, str] | RunResult:
        """Next usable ``(stufe, model)`` from ``stufen`` (consumed in place), or
        the ``RunResult`` that ends the run without starting opencode.

        ``tried`` holds the models this run already handed out: the three rung
        calls are not atomic (a parallel pass can shift the picker's exploration
        beat in between), so a repeated model is skipped like exit 5, never
        started twice (ADR-007).

        Exit 5 (rung unoccupied) always means "next rung". Exit 12 (price data
        stale) aborts under every profile. Under ``zdr`` any other failure aborts
        fail-closed (4 -> ``policy_block``, else ``unreachable``): a customer-code
        pass never runs on a model nobody vetted. Under ``open`` the next rung
        is tried. There is never a fall back to ``OPENCODE_DEFAULT_MODEL`` here.

        ``rest`` gives the seconds left of the run deadline, ``None`` before the
        first opencode start (full picker timeout). After a start every query gets
        at most the rest; under ``_MIN_STAGE_SEC`` no further query is made.
        """
        profile = config.OPENCODE_PICKER_PROFILE
        notes: list[str] = []
        while stufen:
            left = rest()
            if left is not None and left < _MIN_STAGE_SEC:
                return RunResult(
                    success=False, error="unreachable",
                    output=f"Wähler {aufgabe}/{profile}: Restfrist {left} s, keine weitere Abfrage",
                )
            stufe = stufen.pop(0)
            timeout = config.OPENCODE_PICKER_TIMEOUT_SEC
            if left is not None:
                timeout = min(timeout, left)
            code, model = self._ask_picker(aufgabe, stufe, cwd, timeout)
            logger.info("[opencode] Wähler %s/%s Stufe %s → %s", aufgabe, profile, stufe, model or "-")
            if model and model in tried:
                notes.append(f"Stufe {stufe}: {model} doppelt")
                logger.info("[opencode] Wähler %s/%s Stufe %s: %s schon versucht, übersprungen", aufgabe, profile, stufe, model)
                continue
            if model:
                tried.add(model)
                return stufe, model
            if code != 0:
                logger.warning("[opencode] Wähler %s/%s Stufe %s: Exit %s", aufgabe, profile, stufe, code)
            note = f"Stufe {stufe}: Exit {code}"
            if code == _PICKER_UNOCCUPIED:
                notes.append(note + " (unbelegt)")
                continue
            if code == _PICKER_STALE_PRICES or profile == "zdr":
                return RunResult(
                    success=False,
                    error="policy_block" if code == _PICKER_CONFIG and profile == "zdr" else "unreachable",
                    output=f"Wähler {aufgabe}/{profile} {note}; kein opencode-Start",
                )
            notes.append(note)
        return RunResult(
            success=False, error="unreachable",
            output=f"Wähler {aufgabe}/{profile}: keine Stufe belegt ({'; '.join(notes)})",
        )

    def _uses_picker(self) -> bool:
        return bool(config.OPENCODE_MODEL_PICKER) and not self._forced_model

    def _zdr_block(self) -> RunResult | None:
        """A model the picker did not hand out (forced tag, or the default without
        a picker) was never vetted by it: under ``zdr`` its ZDR contract is checked
        right before the start, fail-closed (config drift, unreadable
        opencode.json). ``RunResult`` ``policy_block`` = do not start; ``None`` =
        fine, or profile ``open`` (no check)."""
        if self._uses_picker() or config.OPENCODE_PICKER_PROFILE != "zdr":
            return None
        model = self._forced_model or config.OPENCODE_DEFAULT_MODEL
        problem = _zdr_contract_violation(_load_opencode_config(), model)
        if problem is None:
            return None
        logger.warning("[opencode] %s %s unter zdr -- kein Start", model, problem)
        return RunResult(
            success=False, error="policy_block",
            output=f"ZDR-Vertrag verletzt: {model} {problem}; kein opencode-Start",
        )

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = config.TASK_TIMEOUT_SEC,
        read_only: bool = False,
        session_id: str | None = None,  # accepted but ignored (supports_sessions=False)
        resume: bool = False,            # accepted but ignored
    ) -> RunResult:
        if (blocked := _start_blocked() or self._zdr_block()) is not None:
            return blocked

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

        agent = "extern-review" if read_only else "extern-dev"
        aufgabe = "review" if read_only else "umsetzung"
        # PWD and Popen cwd must name the same absolute dir.
        cwd = os.path.abspath(cwd) if cwd else cwd
        # Model resolution: forced tag > picker ladder > static default. A forced
        # tag bypasses the picker; the default is only the answer WITHOUT a picker
        # (fresh machine) -- with one configured it is never a fallback.
        use_picker = self._uses_picker()
        stufen = list(_PICKER_STUFEN) if use_picker else []

        prompt_path = None
        try:
            prompt_path = _write_prompt_file(task)
            started: float | None = None  # first opencode start; the deadline runs from it
            last: RunResult | None = None
            tried: set[str] = set()  # models the picker already handed out in this run

            def _left() -> int:
                return timeout if started is None else int(timeout - (time.monotonic() - started))

            def _rest() -> int | None:  # None before the first start: full picker timeout
                return None if started is None else _left()

            while True:
                if last is not None and _left() < _MIN_STAGE_SEC:
                    return last
                if use_picker:
                    pick = self._next_pick(aufgabe, stufen, cwd, tried, rest=_rest)
                    if isinstance(pick, RunResult):
                        # Rungs used up/refused after a run that gave no result:
                        # that run's failure is the more useful answer.
                        return last or pick
                    model = pick[1]
                    if last is not None and _left() < _MIN_STAGE_SEC:
                        return last  # the picker itself ate the remaining time
                else:
                    model = self._forced_model or config.OPENCODE_DEFAULT_MODEL

                logger.info("[opencode → %s/%s] Führe Task aus...", agent, model)
                left = _left()  # first rung: the full timeout, before the clock starts
                if started is None:
                    started = time.monotonic()
                result, advance = self._execute(
                    model, agent, effort=self._forced_effort, cwd=cwd,
                    prompt_path=prompt_path, hard_timeout=left,
                )
                if result.success or not advance or not use_picker:
                    return result
                last = result
        finally:
            if prompt_path:
                _unlink_quiet(prompt_path)

    def _execute(
        self, model: str, agent: str, *, effort: str | None, cwd: str | None,
        prompt_path: str, hard_timeout: int,
    ) -> tuple[RunResult, bool]:
        """One opencode run. ``(result, advance)``: ``advance`` is True when a
        failure without result is worth a different model (hang, empty output,
        unclassified error, policy_block from opencode). Never for rate_limit,
        auth_error, timeout, a missing CLI or a start error -- those would hit
        the next model just the same or are spent on the deadline."""
        try:
            t0 = time.monotonic()

            def _attempt(eff: str | None, hard: int):
                cmd = self._build_command(
                    _OPENCODE_EXE, model, agent, effort=eff, prompt_path=prompt_path
                )
                return run_with_watchdog(
                    cmd,
                    # Prompt travels via the -f file written above, NOT stdin --
                    # see module docstring for why that removes the whole
                    # stdin_incomplete contract for this provider by construction.
                    input_text=None,
                    cwd=cwd,
                    idle_timeout=config.OPENCODE_IDLE_TIMEOUT_SEC,
                    hard_timeout=hard,
                    shell=False,
                    env=self._build_env(cwd),
                )

            result = _attempt(effort, hard_timeout)
            # 2.2 validates the variant before any model call (cost 0): an unknown
            # one exits 1 "Variant unavailable". Exactly one repeat without it, so
            # a planned run degrades to the model default instead of failing.
            # Phrase in stderr AND empty stdout: the check fails before any model
            # call, so stdout never holds agent text (which may quote the phrase).
            if (
                effort
                and result.returncode != 0
                and not _strip_ansi(result.stdout or "").strip()
                and _VARIANT_UNAVAILABLE in _strip_ansi(result.stderr or "").lower()
                # the repeat gets what is left of the deadline, not a fresh one
                and (rest := int(hard_timeout - (time.monotonic() - t0))) > 0
            ):
                logger.warning(
                    "[opencode] variant %r unavailable for %s -- repeating once without it",
                    effort, model,
                )
                result = _attempt(None, rest)

            # Stripped before classification too, not just before returning:
            # an escape sequence could in principle sit inside a matched
            # phrase, and RunResult.output must be ANSI-free either way.
            output = _strip_ansi(result.stdout or "").strip()
            stderr = _strip_ansi(result.stderr or "").strip()

            if result.returncode == 0 and output:
                return RunResult(success=True, output=output), False

            # exit 1 is ambiguous by itself (see module docstring) -- classify
            # from the actual content instead of trusting the return code.
            combined = f"{output} {stderr}".lower()

            if "data policy" in combined or "zero data retention" in combined:
                return RunResult(success=False, error="policy_block", output=output or stderr), True
            if _looks_rate_limited(combined):
                return RunResult(success=False, error="rate_limit", output=output or stderr), False
            # stderr only, deliberately narrower than the two checks above --
            # the reasoning is in the comment block over _AUTH_TEXT: this code
            # buys a label (inside the ladder a generic failure
            # advances to the next rung, auth_error does not), so it must not be
            # earned by words the agent itself wrote.
            if _looks_auth_failed(stderr.lower()):
                return RunResult(success=False, error="auth_error", output=output or stderr), False

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
            ), True

        except subprocess.TimeoutExpired as exc:
            kind = getattr(exc, "timeout_kind", "hard")
            # No sweeper built. State: the tree kill ends the --standalone session
            # (measured 2026-10-28 via oc_run.py, same `taskkill /F /T`); not
            # measured separately through this provider path.
            logger.warning(
                "[opencode] %s-Kill -- an opencode.exe orphan may remain holding "
                "the SQLite session DB (tree kill ends a --standalone session, "
                "measured 2026-10-28 via oc_run.py; not measured via this "
                "provider path -- no sweeper built).",
                "idle" if kind == "idle" else "hard",
            )
            idle = kind == "idle"
            return RunResult(success=False, error="hang" if idle else "timeout"), idle
        except FileNotFoundError:
            return RunResult(success=False, error="opencode CLI not found"), False
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e)), False
