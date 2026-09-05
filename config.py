import os
import re
import sys
from pathlib import Path


def _normalize_dotenv_value(value: str) -> str:
    """Strip surrounding quotes and trailing comments from .env values."""
    # 1. Handle quoted values (supports trailing comments)
    m = re.match(r'^(["\'])(.*)\1(?:\s*#.*)?$', value)
    if m:
        return m.group(2)
    # 2. Handle unquoted values: strip trailing inline comments.
    # Require whitespace before # to avoid truncating URLs/paths (e.g. https://x.com#anchor).
    return re.split(r'\s+#', value)[0].strip()


def _load_dotenv() -> None:
    """Load .env file from project root into os.environ (no external deps)."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = _normalize_dotenv_value(value.strip())
            # Only set if not already defined (real env vars take precedence)
            if key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# --- Paths (override via env vars or .env) ---
VAULT_PATH = Path(os.getenv("ORCH_VAULT_PATH", ""))
QUEUE_FILE_PATH = os.getenv("ORCH_QUEUE_FILE", "")

if VAULT_PATH == Path(""):
    # Fallback: try common location
    _default = Path.home() / "obsidian_vault"
    VAULT_PATH = _default

if QUEUE_FILE_PATH:
    QUEUE_FILE = Path(QUEUE_FILE_PATH)
else:
    QUEUE_FILE = VAULT_PATH / "99_System" / "AI" / "agent-queue.md"

# Where results are appended inside the queue file
RESULTS_SECTION = "## Ergebnisse"
LOG_SECTION = "## Log"

# Provider cooldown after unreachable error (seconds)
PROVIDER_COOLDOWN_SEC = 30 * 60  # 30 minutes

def _parse_int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key) or str(default))
    except ValueError:
        return default


def _parse_positive_float_env(key: str, default: float) -> float:
    """Parse a float env var, falling back on anything unusable.

    Non-positive values fall back too: they reach a CLI as a cost/limit argument,
    where "0" or "-1" is either rejected outright or silently means "no budget".
    """
    try:
        value = float(os.getenv(key) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _parse_bool_env(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Feature flag — opt-in for Claude --session-id/--resume across tool phases.
# Default OFF: tools fall back to today's stateless subprocess behaviour. Toggle
# in .env (CLAUDE_SESSION_ENABLED=true) to enable; toggle off for instant rollback.
CLAUDE_SESSION_ENABLED = _parse_bool_env("CLAUDE_SESSION_ENABLED", False)

# Retention window for orchestrator-created Claude session JSONL files in
# ~/.claude/projects/**. Heartbeat session-cleanup deletes only sessions that
# appear in our sidecar registry (logs/orchestrator-sessions.jsonl) AND are
# older than this — interactive Claude Code sessions stay untouched.
ORCH_SESSION_RETENTION_DAYS = _parse_int_env("ORCH_SESSION_RETENTION_DAYS", 14)


# Minimum remaining capacity to consider a provider usable (percent)
# Override via .env: MIN_CAPACITY_PERCENT=15
MIN_CAPACITY_PERCENT = _parse_int_env("MIN_CAPACITY_PERCENT", 10)

# Per-window thresholds for Claude (five_hour resets every 5h, seven_day every 7d)
# five_hour is consumed faster → higher default; seven_day can go lower.
# Override via .env: CLAUDE_FIVE_HOUR_MIN_CAPACITY_PCT=15, CLAUDE_SEVEN_DAY_MIN_CAPACITY_PCT=3
CLAUDE_FIVE_HOUR_MIN_CAPACITY_PCT = _parse_int_env("CLAUDE_FIVE_HOUR_MIN_CAPACITY_PCT", 10)
CLAUDE_SEVEN_DAY_MIN_CAPACITY_PCT = _parse_int_env("CLAUDE_SEVEN_DAY_MIN_CAPACITY_PCT", 3)

# Per-window thresholds for Codex (primary resets every 5h, secondary every 7d)
# Primary is consumed faster → keep the higher default; secondary can go lower.
# Override via .env: CODEX_PRIMARY_MIN_CAPACITY_PCT=15, CODEX_SECONDARY_MIN_CAPACITY_PCT=3
CODEX_PRIMARY_MIN_CAPACITY_PCT = _parse_int_env("CODEX_PRIMARY_MIN_CAPACITY_PCT", 10)
CODEX_SECONDARY_MIN_CAPACITY_PCT = _parse_int_env("CODEX_SECONDARY_MIN_CAPACITY_PCT", 3)

# Claude subscription plan — used by the local-file 429 fallback to calculate
# remaining capacity from ~/.claude/projects JSONL data when cclimits is rate-limited.
# Values: pro (19k tokens/5h), max5 (88k), max20 (220k), custom (44k).
# Leave empty to disable the local fallback (existing snapshot logic is used instead).
CLAUDE_PLAN = os.getenv("CLAUDE_PLAN", "")

# How long to wait between cclimits polls when sleeping (seconds)
SLEEP_POLL_INTERVAL = 5 * 60

# Hard backstop for a single CLI task call (seconds). With the liveness watchdog
# (providers/process_runner.py) this is NO LONGER an aggressive deadline — a run
# that keeps making progress (Claude: emits NDJSON events / has an active tool;
# Gemini/Codex: emits output) runs to completion. This is only the absolute upper
# bound for a progressing process that never finishes. #timeout: tag / profile
# timeout_minutes set THIS value (semantics changed in the watchdog refactor).
TASK_TIMEOUT_SEC = _parse_int_env("TASK_TIMEOUT_SEC", 5400)  # 90 min hard backstop

# Liveness/hang detector. Claude is TOOL-AWARE (a running tool_use pauses the
# idle timer, see process_runner._Liveness), so 300s comfortably covers the gaps
# between NDJSON events when no tool runs. Gemini/Codex have NO structured tool
# signal — for them stdout-silence is the only liveness proxy and a long single
# tool phase (pytest/build/install) looks idle; CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC
# is therefore conservative.
TASK_IDLE_TIMEOUT_SEC = _parse_int_env("TASK_IDLE_TIMEOUT_SEC", 300)  # 5 min, Claude (tool-aware)
CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC = _parse_int_env(
    "CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC", 1200)  # 20 min, Gemini/Codex (byte-only)

# Hang handling: an idle-kill yields error="hang" (vs "timeout" for the hard
# backstop). A repeatedly hanging task is requeued with a short backoff up to
# MAX_HANG_RETRIES, then BLOCKED (not quota-reset-retried forever).
MAX_HANG_RETRIES = _parse_int_env("MAX_HANG_RETRIES", 2)
HANG_RETRY_BACKOFF_SEC = _parse_int_env("HANG_RETRY_BACKOFF_SEC", 5 * 60)

# Timeout for interactive Telegram chat responses (seconds)
TELEGRAM_CHAT_TIMEOUT_SEC = 180  # 3 minutes

# Send "still thinking..." notification after this many seconds without response
TELEGRAM_CHAT_THINKING_SEC = 30

# Max retries per provider before falling back to next provider
MAX_RETRIES_PER_PROVIDER = 2

# Max file size for context injection (bytes)
MAX_CONTEXT_FILE_SIZE = 1_000_000  # 1 MB

# --- Safety Guardrails ---

# A path prefix that may sit in front of a binary name (`/usr/bin/git`,
# `./git`, `C:\tools\git`, `/bin/bash`). Must run right up to the binary
# name with no separator/quote/space in between, so it can't span past a
# real shell token boundary and swallow an unrelated preceding word.
_PATH_PREFIX = r"(?:[^\s;&|()\"'`]*[\\/])?"

# Shell interpreters whose "-c" / "-Command" argument is executed as a real
# shell command line — unlike e.g. `python -c "..."`, where the string is
# inert Python source that never runs as shell. bash/sh/zsh/dash accept
# exactly "-c"; PowerShell (pwsh, powershell, powershell.exe) accepts "-c"
# too, plus "-Command" or any of its unambiguous prefixes ("-Com".."-Command"
# — bare "-Co" is deliberately NOT accepted: on real PowerShell it is
# ambiguous with "-ConfigurationName", so it isn't a valid abbreviation there
# either). IGNORECASE (applied when these patterns are compiled) makes the
# flag matching case-insensitive.
_SHELL_INTERP = r"(?:bash|sh|zsh|dash|pwsh|powershell(?:\.exe)?)"
_SHELL_C_FLAG = r"-(?:c|com(?:m(?:a(?:n(?:d)?)?)?)?)\b"

# Shell builtins that take a COMMAND as their argument rather than data:
# `eval "git commit -m x"` runs the string as a command line, `exec git push`
# replaces the shell with that command. Same class as the `bash -c` hole —
# whatever follows them is a real invocation — but the form differs: they are
# builtins, so there is no interpreter path and no `-c` flag, just the word,
# whitespace, and (optionally) an opening quote. The mandatory `\s+` after the
# word is what keeps "evaluate", "execute" and "retrieval" out.
_SHELL_CMD_BUILTIN = r"(?:eval|exec)"

# Command-start boundary: the true start of the command string, right after
# a shell separator (;, &&, ||, |, newline), an opening subshell /
# command-substitution marker ("(" — covers both a bare subshell and the
# "(" in "$(...)" — or a backtick), optionally followed by a shell
# interpreter's -c/-Command argument (`bash -c "`, `pwsh -Command '`) and/or a
# command-taking builtin (`eval "`, `exec `) — because unlike `python -c "..."`,
# those arguments ARE executed as a real command line, so whatever starts them
# is a real invocation too. Both prefixes are optional and composable, so
# `bash -c "eval 'git commit'"` is covered by the same expression; the builtin
# repeats at most twice (`eval eval "..."`) rather than unboundedly, which keeps
# the worst case linear instead of inviting catastrophic backtracking.
# The interpreter/builtin itself must in turn sit at one of the earlier boundary
# forms (^ or a separator); a shell name buried inside an unrelated word or
# string does not open this boundary (the `\s+` required right after the name
# rules that out — "shellcheck -c" does not match "sh" here, because "sh" is not
# followed by whitespace, and `find . -exec` does not match either, because "-"
# is not a boundary character). Python's `re` only supports fixed-width
# lookbehind, so the boundary is matched as an ordinary prefix rather than a
# lookbehind. Every git pattern below is anchored to it, so `git commit`/
# `git push` text sitting inside a quoted string, a `-c` payload of ANOTHER
# (non-shell) program (`python -c "...git commit..."`), or a shell comment
# (`# ... git push ...`) no longer counts as a real invocation — only text
# that actually starts a command does.
# Known accepted residuals (documented, not fixed — see the caller's report
# for why): `\n`/`\r` are real command separators, so a heredoc body line
# that happens to start with "git commit" still matches; extra flags
# between the interpreter and -c (`bash --norc -c ...`) are not recognized;
# a command passed to a non-shell wrapper (`find . -exec git commit`,
# `docker exec c git commit`, `xargs git commit`) is not recognized either —
# every one of those needs real shell/argv parsing, deliberately not built
# here. Fail-safe means the ambiguous case stays blocked or, in the wrapper
# cases, simply isn't specifically covered (out of the required scope).
_CMD_START = (
    r"(?:^|[\r\n;&|]|\(|`)\s*"
    rf"(?:{_PATH_PREFIX}{_SHELL_INTERP}\s+{_SHELL_C_FLAG}\s*[\"']?\s*)?"
    rf"(?:{_SHELL_CMD_BUILTIN}\s+[\"']?\s*){{0,2}}"
)

# The git binary itself, optionally reached through a path. See _PATH_PREFIX.
# (A "repeated slash-terminated segment" variant was measured and rejected:
# it cost MORE on adversarial deeply-nested-path input because Python's `re`
# pays more per backtrack step for a repeated group than for a single
# character-class backtrack — see check_command's has_git prefilter for the
# optimization that actually matters, and the perf note in the caller's
# report for measured numbers.)
_GIT_BIN = rf"{_PATH_PREFIX}git"

# Git global options that may sit between `git` and its subcommand
# (`git -C <path> commit`, `git -c user.email=x commit`, `git --no-pager push`).
# Used to build subcommand patterns that match the real command forms without
# firing on read-only commands that merely contain the word (`git log
# --grep=commit`, `git show`, `git rev-parse`).
_GIT_GLOBAL_OPT = (
    r"(?:-[cC]\s+(?:\"[^\"]*\"|'[^']*'|\S+)"
    r"|--(?:git-dir|work-tree|namespace|exec-path)(?:=\S+|\s+\S+)"
    r"|--(?:no-pager|paginate|bare|literal-pathspecs|no-replace-objects|no-optional-locks))"
)


def _git_pattern(rest: str) -> str:
    """Regex matching `git [global-opts] <rest>` anchored to a real command start.

    `rest` covers the subcommand and anything that must follow it, e.g.
    `commit(?![\\w-])`, `push\\s+.*--force`, `reset\\s+--hard`. Anchoring
    through _CMD_START/_GIT_BIN (see their docstrings) is what keeps this
    out of quoted strings, another program's `-c` argument, and comments.
    """
    return rf"{_CMD_START}{_GIT_BIN}(?:\s+{_GIT_GLOBAL_OPT})*\s+{rest}"


# Hard-deny patterns — used by Claude Code PreToolUse hook (scripts/safety_hook.py)
# AND injected into prompts for all providers (Gemini, Codex have no hook system).
# Each entry: (regex_pattern, human-readable description)
SAFETY_DENY_PATTERNS: list[tuple[str, str]] = [
    # Destructive file operations
    (r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+", "rm -rf recursive forced delete"),
    (r"rm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+", "rm -fr recursive forced delete"),
    (r"rm\s+--force\s+-r\s+", "rm --force -r recursive forced delete"),
    (r"rm\s+-r\s+--force\s+", "rm -r --force recursive forced delete"),
    (r"rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(/|~|%|\\)", "rm -r on root/home paths"),
    (r"del\s+/[sfq]", "Windows del with /s /f /q flags"),
    (r"Remove-Item\s.*-Recurse.*-Force", "PowerShell recursive force delete"),
    (r"rd\s+/[sq]", "Windows rd /s /q recursive delete"),
    # Git destructive operations
    (_git_pattern(r"push\s+.*--force"), "git push --force"),
    (_git_pattern(r"push\s+.*-f\b"), "git push -f (force)"),
    (_git_pattern(r"reset\s+--hard"), "git reset --hard"),
    (_git_pattern(r"clean\s+-[a-zA-Z]*f"), "git clean -f (untracked file deletion)"),
    (_git_pattern(r"checkout\s+--\s+\."), "git checkout -- . (discard all changes)"),
    # Publishing stays with the user — committing no longer does (revised 2026-08-17).
    # The original rule (2026-08-15) blocked `git commit` too, reasoning that
    # unattended runs should leave their changes in the working tree. It was dropped
    # because nothing here actually tests for "unattended": the hook is wired to every
    # Bash call, so it blocked interactive sessions just as hard — including ones where
    # the user had explicitly asked for the commit, with no way to override it from
    # inside the session. The asymmetry that remains is deliberate: a commit is local
    # and revertible (`git reset`), a push leaves the machine and cannot be taken back.
    # Placed after the force-push patterns so those keep reporting the specific reason.
    (_git_pattern(r"push(?![\w-])"), "git push (unattended runs must not change remote state)"),
    # Database destruction
    (r"DROP\s+(TABLE|DATABASE|SCHEMA)", "DROP TABLE/DATABASE/SCHEMA"),
    (r"TRUNCATE\s+TABLE", "TRUNCATE TABLE"),
    (r"DELETE\s+FROM\s+\S+\s*;?\s*$", "DELETE FROM without WHERE clause"),
    # Disk/partition operations
    (r"format\s+[A-Za-z]:", "Windows format drive"),
    (r"mkfs\b", "Linux mkfs (format filesystem)"),
    (r"diskpart", "Windows diskpart"),
    # System-level danger
    (r":\(\)\s*\{.*:\|:.*\}", "Fork bomb"),
    (r">\s*/dev/sda", "Write to raw disk device"),
    (r"dd\s+.*of=/dev/", "dd to raw device"),
    # Credential / secret exfiltration
    (r"curl\s.*(-d|--data)\s.*(_TOKEN|_SECRET|_KEY|PASSWORD)", "Exfiltrating secrets via curl"),
    (r"wget\s.*(_TOKEN|_SECRET|_KEY|PASSWORD)", "Exfiltrating secrets via wget"),
]

# Prompt-injectable safety rules: compact 4-liner (full pattern list stays in
# SAFETY_DENY_PATTERNS for the Claude Code PreToolUse hook — no need to repeat
# every variant in the prompt).
SAFETY_RULES = (
    "Safety rules (MUST follow — violations will be blocked):\n"
    "- NEVER run: rm -rf, git push --force/-f, git reset --hard, "
    "git clean -f, DROP TABLE, format/mkfs/diskpart\n"
    "- NEVER push to remote unless the task explicitly says to\n"
    "- NEVER modify files outside the working directory unless explicitly asked\n"
    "- If unsure whether destructive: skip and report what you would have done"
)

# Safety: track file changes before/after tasks
TRACK_FILE_CHANGES = True
# Safety: auto-stash in git repos before task execution
GIT_AUTO_STASH = True

# Safety: snapshots live in their OWN ref namespace, never in refs/stash.
# refs/stash is the user's workspace -- writing there means a user `git stash pop`
# after a nightly run pops an orchestrator snapshot instead of their own work.
GIT_SNAPSHOT_REF_PREFIX = "refs/orchestrator-backup/"
# Bounded retries when two snapshots collide on the same second in one repo.
GIT_SNAPSHOT_REF_MAX_ATTEMPTS = 4

# --- Snapshot retention -----------------------------------------------------
# The binding constraint: night tasks deliberately do NOT commit, so this snapshot
# is the ONLY undo for the changes waiting in the working tree for the morning
# review. Deleting on task success would therefore destroy the one artefact the
# feature exists to provide. Retention must outlive at least one review cycle,
# which makes the policy a VETO structure rather than a plain LRU.
#
# Veto over BOTH caps below: nothing younger than this is ever pruned, no matter
# how many snapshots exist. 14 days covers a missed weekend plus a week away.
# Matches ORCH_SESSION_RETENTION_DAYS, the repo's other "user still needs it" window.
GIT_SNAPSHOT_PROTECT_DAYS = 14
# Age cap. 30 days is this repo's existing retention convention
# (MEMORY_DAILY_LOG_RETENTION_DAYS, QUEUE_EVENTS_LOG_RETENTION_DAYS, replay.py rotation).
GIT_SNAPSHOT_MAX_AGE_DAYS = 30
# Count cap, newest-first. Far above the measured rate (11 snapshots in ~6 months),
# so it only ever bites in a high-frequency repo -- it bounds growth, it is not the
# routine pruner. The protect window can starve it; see _prune_snapshot_refs.
GIT_SNAPSHOT_MAX_COUNT = 50

# System prompts per provider (prepended to each task)
_BASE_PROMPT = "Antworte auf Deutsch, praegnant und strukturiert."
SYSTEM_PROMPTS: dict[str, str] = {
    "claude": f"{_BASE_PROMPT}\n\n{SAFETY_RULES}",
    "gemini": f"{_BASE_PROMPT}\n\n{SAFETY_RULES}",
    "codex": f"{_BASE_PROMPT}\n\n{SAFETY_RULES}",
    # opencode has neither a PreToolUse hook (that only fires for Claude) nor a
    # sandbox (unlike Codex's --sandbox workspace-write) — the agent's own tool
    # permission gates (opencode.json) plus this prompt are the only guardrails.
    "opencode": f"{_BASE_PROMPT}\n\n{SAFETY_RULES}",
}

# --- OpenRouter (HTTP provider, pay-per-token) ---
# Activated only when OPENROUTER_API_KEY is set. Never enters the default
# fallback chain — requires an explicit #openrouter or #or_* tag.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_DEFAULT_MODEL = os.getenv(
    "OPENROUTER_DEFAULT_MODEL", "minimax/minimax-m2.5:free"
)

# --- Gemini HTTP API (preferred over the deprecated CLI) ---
# The consumer Gemini CLI (Code Assist for individuals / AI Pro / AI Ultra) was
# shut down 2026-06-18. When GEMINI_API_KEY is set, the Gemini provider calls the
# Google Gemini REST API directly via urllib (stdlib, like the OpenRouter provider).
# Without a key it falls back to the legacy `gemini` CLI for Standard/Enterprise
# users who retain CLI access. Either way Gemini stays in the default fallback
# chain; in HTTP mode there is no pollable subscription quota, so availability is
# cooldown-driven (HTTP 429 -> 30-min cooldown), exactly like OpenRouter.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
)
# Free-tier-safe GA default. Per-task override via #gemini_pro / #gemini_flash_lite.
GEMINI_DEFAULT_MODEL = os.getenv("GEMINI_DEFAULT_MODEL", "gemini-3.5-flash")
# Generous output cap: gemini-3.x are thinking models whose hidden reasoning is
# token-hungry (observed ~150 thinking tokens even for a one-word reply) and
# counts toward this cap — too small a value lets thinking consume everything and
# return empty text (finishReason=MAX_TOKENS). 16k leaves room for a full review
# plus reasoning. Free tier is uncharged, so headroom costs nothing.
GEMINI_MAX_OUTPUT_TOKENS = _parse_int_env("GEMINI_MAX_OUTPUT_TOKENS", 16384)

# --- Mistral Vibe CLI (opt-in second non-Claude voice) ---
# Registered only when the `vibe` binary is on PATH, and never part of the
# fallback chain — activation needs an explicit #vibe / #vibe_* tag or a
# #second_opinion:vibe value. Cost is pay-per-token on Mistral's API, so a run
# carries its own hard ceiling: vibe interrupts itself above --max-price.
VIBE_MAX_PRICE_USD = _parse_positive_float_env("VIBE_MAX_PRICE_USD", 0.50)
# Turn budget for tool-enabled runs (read_file/grep only — the provider never
# grants write tools). Enough for a reviewer to pull a handful of files.
VIBE_MAX_TURNS = _parse_int_env("VIBE_MAX_TURNS", 12)
# read_only runs disable every tool, so a single assistant turn is all that can
# happen — anything higher would just widen the blast radius of a hang.
VIBE_READONLY_MAX_TURNS = _parse_int_env("VIBE_READONLY_MAX_TURNS", 1)

# --- opencode CLI (Stufe 2: tag-activated third external voice) ---
# Registered only when opencode.exe resolves past the npm shim AND both
# required agents (extern-review/extern-dev) exist in opencode.json — see
# providers/opencode.py. Never part of the fallback chain (Stufe 3, not
# built): activation needs an explicit #opencode / #opencode_<alias> tag.
# Byte-only liveness like Vibe/Codex (no NDJSON tool-aware signal) — 300s
# instead of the CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC default of 1200s because
# measured normal runs finish in 8-21s with the longest observed silence at
# 46-58s, so 300s already sits at ~5x the worst observed gap.
OPENCODE_IDLE_TIMEOUT_SEC = _parse_int_env("OPENCODE_IDLE_TIMEOUT_SEC", 300)
# Prompt travels via a `-f` temp file, not stdin (see providers/opencode.py).
# A file outside --dir cannot be reread by either agent once attached
# (external_directory: deny is the LAST matching permission rule, measured
# 2026-09-04 — even the seemingly-allowed %TEMP%/opencode/* path fails the
# same way), so above this cap the run fails loudly with error=
# "prompt_too_large" instead of the agent silently judging a truncated
# fraction of the material. Measured largest real prompts: 16.8/29.3/32.0 KB —
# the cap does not fire in normal operation.
OPENCODE_MAX_PROMPT_BYTES = _parse_int_env("OPENCODE_MAX_PROMPT_BYTES", 50_000)
# Profile passed to the optional model picker (oc_pick_model.py --profile).
# "zdr" restricts picks to opencode.json aliases carrying
# data_collection:"deny" AND zdr:true — the only aliases safe for customer code.
OPENCODE_PICKER_PROFILE = os.getenv("OPENCODE_PICKER_PROFILE", "zdr")
# Absolute path to oc_pick_model.py. Empty by default and NOT a last resort:
# the picker lives in ~/.claude/scripts and is simply absent on a fresh
# machine, where OPENCODE_DEFAULT_MODEL below is the normal resolution path.
OPENCODE_MODEL_PICKER = os.getenv("OPENCODE_MODEL_PICKER", "")
OPENCODE_DEFAULT_MODEL = os.getenv("OPENCODE_DEFAULT_MODEL", "openrouter/zdr-review")
# opencode's own config file — read-only from here, never written to. Machine-
# local agent/model setup stays with the user; see doctor's WARN-only check.
OPENCODE_CONFIG_PATH = Path(
    os.getenv("OPENCODE_CONFIG_PATH", str(Path.home() / ".config" / "opencode" / "opencode.json"))
)
# Minimum OpenRouter $ remaining before AllLimits.opencode reports available=False
# (limits._opencode_budget_snapshot(), fail-closed — see its docstring). The
# threshold is sized off the most expensive curated review measured so far
# (~$0.195, ZDR auto-2/Kimi K3), so it leaves exactly one full run of headroom
# rather than an arbitrary buffer.
OPENCODE_MIN_REMAINING_USD = _parse_positive_float_env("OPENCODE_MIN_REMAINING_USD", 0.25)

# --- Telegram Notifications ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# When to notify (all default True)
NOTIFY_ON_TASK_STARTED = True
NOTIFY_ON_TASK_DONE = True
NOTIFY_ON_ERROR = True
NOTIFY_ON_QUEUE_COMPLETE = True
NOTIFY_ON_ALL_PROVIDERS_EXHAUSTED = True

# --- Security ---
# Allowed root directories for cwd: tags (empty list = allow all).
# When set, only tasks with cwd paths under these roots will be executed.
# Example: ALLOWED_CWD_ROOTS = [Path("D:/programmieren"), Path("C:/projects")]
_env_cwd_roots = os.getenv("ALLOWED_CWD_ROOTS", "")
ALLOWED_CWD_ROOTS: list[Path] = (
    [Path(p.strip()) for p in _env_cwd_roots.split(";") if p.strip()]
    if _env_cwd_roots
    else []  # empty = allow all paths; configure via ALLOWED_CWD_ROOTS in .env
)

# Max task length accepted via Telegram /task command (characters)
TELEGRAM_MAX_TASK_LENGTH = 500

# --- Tools ---
# Max iterations for review/fix loops
TOOL_MAX_ITERATIONS = 20

# Fallback total-runtime deadline for an iterative tool when its ToolContract
# omits max_runtime_sec. Caps the SUM of all phases/iterations (wall-clock),
# independent of the per-phase TOOL_*_TIMEOUT_SEC caps.
TOOL_DEFAULT_MAX_RUNTIME_SEC = _parse_int_env("TOOL_DEFAULT_MAX_RUNTIME_SEC", 3600)  # 60 min
TOOL_REVIEW_TIMEOUT_SEC = 1_200  # 20 min per review
TOOL_FIX_TIMEOUT_SEC = 2_400     # 40 min per fix
TOOL_INTER_STEP_SLEEP_SEC = 2    # pause between review/fix iterations

# Dev-Loop timeouts (Research → Plan → Execute → Dual-Review)
TOOL_DEV_RESEARCH_TIMEOUT_SEC          = 3_600  # 60 min: Research phase
TOOL_DEV_PLAN_TIMEOUT_SEC              = 1_800  # 30 min: Plan phase
TOOL_DEV_EXEC_TIMEOUT_SEC              = 7_200  #  2h:   Execution phase (TDD loops)
TOOL_DEV_QUALITY_REVIEW_TIMEOUT_SEC    = 3_600  # 60 min: Code Quality Review
TOOL_DEV_RESOLUTION_REVIEW_TIMEOUT_SEC = 1_800  # 30 min: Issue Resolution Review

# Review-Loop verification phase timeout
TOOL_VERIFICATION_TIMEOUT_SEC          =   600  # 10 min: Final verification after no findings

# Review-Loop second-opinion (opt-in via #second_opinion:<alias> tag)
TOOL_RL_SECOND_OPINION_TIMEOUT_SEC     =   600  # 10 min: single non-agentic LLM call
TOOL_RL_SECOND_OPINION_MAX_DIFF_CHARS  = 30_000  # cap on git-diff chars injected (≈7-8k tokens)

# Review-Loop drift check (mode configured via policy.yaml tool_phases.review-loop.drift_check_mode)
TOOL_RL_DRIFT_CHECK_TIMEOUT_SEC        =   120  # 2 min: mini-prompt without codebase exploration

# Research-QA timeouts (Discovery → Analysis → Questions)
TOOL_RQA_DISCOVERY_TIMEOUT_SEC = 1_200  # 20 min: Codebase exploration
TOOL_RQA_ANALYSIS_TIMEOUT_SEC  = 1_200  # 20 min: Deep analysis
TOOL_RQA_QUESTIONS_TIMEOUT_SEC =   600  # 10 min: Question generation

# Knowledge-Transfer timeouts (Know-How → Applications → Synthesis)
TOOL_KT_VAULT_SCAN_MAX_CHARS      = 80_000  # chars of vault content fed to LLM
TOOL_KT_KNOWHOW_TIMEOUT_SEC       =    600  # 10 min: know-how extraction
TOOL_KT_APPLICATIONS_TIMEOUT_SEC  =    900  # 15 min: cross-domain with WebSearch
TOOL_KT_SYNTHESIS_TIMEOUT_SEC     =    600  # 10 min: solution synthesis

# Critical-Review timeouts (3-pass adversarial)
TOOL_CR_PASS1_TIMEOUT_SEC = 2_400  # 40 min: Pass 1 — analysis (codebase exploration)
TOOL_CR_PASS2_TIMEOUT_SEC = 1_800  # 30 min: Pass 2 — adversarial challenge (reviews Pass 1)
TOOL_CR_PASS3_TIMEOUT_SEC = 1_800  # 30 min: Pass 3 — synthesis (improved plan output)
TOOL_CR_PASS1_MAX_INJECT_CHARS = 30_000  # max Pass 1 output injected into Pass 2 prompt
TOOL_CR_MAX_PLAN_CHARS = 50_000          # max plan file content injected into prompts

# Security-Audit timeouts
TOOL_SA_AUDIT_TIMEOUT_SEC  = 2_400  # 40 min: read-only vulnerability scan (Phase 1)

# Deep-Security-Audit timeouts (multi-agent)
TOOL_DSA_AGENT_TIMEOUT_SEC        = 1_800  # 30 min per expert agent (6 agents)
TOOL_DSA_SYNTHESIS_TIMEOUT_SEC    = 2_400  # 40 min: CISO synthesis of all findings
TOOL_DSA_FIX_TIMEOUT_SEC          = 3_600  # 60 min: fix implementation
TOOL_DSA_MAX_AGENT_OUTPUT_CHARS   = 15_000  # max per-agent output injected into synthesis
TOOL_DSA_MAX_TOTAL_INJECT_CHARS   = 80_000  # max combined output for synthesis prompt

# Brainstorm tool (multi-persona round-table, domain-aware)
TOOL_BS_PHASE0_TIMEOUT_SEC                  =    600  # 10 min: topic analysis + persona generation
TOOL_BS_PERSONA_TIMEOUT_SEC                 =    900  # 15 min: per-persona generation / cross-pollination call
TOOL_BS_SYNTHESIS_TIMEOUT_SEC               =  1_800  # 30 min: final clustering + ranking
TOOL_BS_MAX_ITERATIONS                      =      5  # hard cap on convergence loop
TOOL_BS_CONVERGENCE_THRESHOLD               =   0.20  # <20% new clusters vs. previous round → converged
TOOL_BS_CLUSTER_SIMILARITY_THRESHOLD        =   0.40  # cosine ≥ X = same cluster (Jaccard-cosine on short texts)
TOOL_BS_MIN_PERSONAS                        =      4  # min personas the topic-analysis must return
TOOL_BS_MAX_PERSONAS                        =      6  # max personas the topic-analysis must return
TOOL_BS_DEFAULT_TOP_N                       =      5  # synthesis: top-N ranked ideas in report
TOOL_BS_MAX_IDEAS_PER_PERSONA_PER_ROUND     =     10  # cap on ideas any one persona may produce per call
TOOL_BS_MAX_PEER_CHARS_PER_PERSONA          =  8_000  # truncate one peer's output to this for cross-pollination injection
TOOL_BS_MAX_TOTAL_INJECT_CHARS              = 50_000  # global cap on peer-context injected into one cross-pollination prompt

# --- Scientific-Investigation tool (#tool:scientific-investigation) ---
# Cross-provider DA bypass (#cross-provider:none) — rate-limited, then PolicyEngine.
TOOL_SI_BYPASS_LIMIT_PER_30_DAYS = 3
# Adversarial-citation-search diversity check (Levenshtein min between queries).
TOOL_SI_ADVERSARIAL_LEVENSHTEIN_MIN = 8
# Cross-investigation cherry-picking detector (Cosine similarity).
TOOL_SI_CHERRYPICKING_SIMILARITY_THRESHOLD = 0.7
# Default not enforcing external norms — LOW-cap fallback when missing.
TOOL_SI_DISCIPLINE_NORMS_REQUIRED = False
# Phase 0 framing + Phase 0.5 pre-registration timeouts.
TOOL_SI_PHASE0_TIMEOUT_SEC = 600
TOOL_SI_PHASE0_5_TIMEOUT_SEC = 900
# Phase 2 multi-persona review (per LLM call).
TOOL_SI_PHASE2_AUTHOR_TIMEOUT_SEC = 1_800   # 30 min — initial + rework plan write
TOOL_SI_PHASE2_REVIEW_TIMEOUT_SEC = 1_200   # 20 min — per-persona review
TOOL_SI_PHASE2_MAX_ITERATIONS = 3           # max rework loops before forced exit
# Phase 4 synthesis + Phase 5b heuristic review.
TOOL_SI_PHASE4_TIMEOUT_SEC = 2_400          # 40 min — proof.md draft writing
TOOL_SI_PHASE5B_TIMEOUT_SEC = 1_200         # 20 min — cross-provider heuristic review
# Per-threshold Telegram approval wait. Single threshold blocks for at most this long.
TOOL_SI_TELEGRAM_APPROVAL_TIMEOUT_SEC = 1_800  # 30 min
# Phase 7 engineering-reviewer (cross-provider).
TOOL_SI_PHASE7_TIMEOUT_SEC = 1_800
TOOL_SI_PHASE7_MAX_TOKENS = 30_000
TOOL_SI_PHASE7_MAX_REWORK_ITERATIONS = 3
# Phase 8 final user-approval gate (Telegram).
TOOL_SI_APPROVAL_TIMEOUT_HOURS = 24
# Sub-task dev-loop timeout (Phase 3 execution loop, per sub-task).
TOOL_SI_SUBTASK_TIMEOUT_SEC = 7_200
# Pflicht-Prosa-Limitations validation (Phase 4 synthesis).
TOOL_SI_LIMITATIONS_BLACKLIST = [
    "nicht relevant", "nicht anwendbar", "vernachlässigbar", "minimal",
    "nicht zutreffend", "kein einfluss", "ignorierbar", "trivial",
    "n/a", "entfällt", "nicht der fall",
]
TOOL_SI_LIMITATIONS_REQUIRED_CATEGORIES = [
    "Multi-LLM-Korpus", "Self-Reporting", "Disziplin-Restriktion",
    "Cross-Investigation-Cherry-Picking", "LLM-Drift",
]
TOOL_SI_LIMITATIONS_MIN_SENTENCES_PER_CATEGORY = 2
# Embedding model used for cherry-picking similarity index (documented in manifest).
TOOL_SI_EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# --- Logging ---
LOG_FILE = Path(__file__).parent / "logs" / "orchestrator.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 3

# --- Memory System ---
MEMORY_HALF_LIFE_DAYS              = 30
MEMORY_MAX_AGE_DAYS                = 30   # archive task_results after 30 days
MEMORY_ARCHIVE_DELETE_DAYS         = 90   # delete from archive/ after 90 days
MEMORY_DAILY_LOG_RETENTION_DAYS    = 30   # delete daily/*.md after 30 days
MEMORY_LESSONS_RETENTION_DAYS      = 180  # prune lessons.md entries after 180 days
MEMORY_TOP_K             = 5
MEMORY_SUMMARY_MAX_CHARS = 700   # first 500 + "...\n" + last 200
MEMORY_MIN_SCORE         = 0.10  # discard matches below this threshold (avoids noise injection)
MEMORY_TASK_LABEL_CHARS  = 60    # how much of a past task is quoted in the injected history

# A "no-op" run: the provider answered that it found no task to do. Such an answer carries
# zero information, and re-injecting it teaches the next run the same answer — the memory
# block becomes a few-shot prompt for doing nothing (measured 2026-08-19: 3 of 5 injected
# examples were exactly that). Both thresholds must be met at once, see
# memory._is_noninformative — a long, substantive report that happens to use the phrase
# stays in.
# Measured 2026-08-19 over 206 stored results carrying a token count (task_results/ AND
# archive/ — note that only the 69 in task_results/ are ever injected; the threshold is
# calibrated on the wider set). The no-ops the filter recognises run 71–772 tokens; the
# smallest genuine run is 329 (daily-activity-synthese, archive/2026-05-29). The two
# classes overlap, so this gate never separates them cleanly — below it the addressee
# anchor decides alone. Set to 900 rather than 1500 for exactly that reason: it keeps every
# recognised no-op (128 tokens of headroom above the largest) while shrinking the
# population where the regex is the only judge from 39 of 197 genuine runs to 29.
# Raising it on the assumption of a wide gap would be wrong — the gap is not there.
#
# `codex` and `vibe` report no token counts at all, so for those providers this gate is
# never satisfied and the layer-3 filter stays inactive by design (fail-open).
MEMORY_NOOP_MAX_OUTPUT_TOKENS = 900

# Heading over the injected TF-IDF block. Lives here because two prompt builders emit it —
# orchestrator._build_prompt and tools.base_tool._build_system_prompt — and a heading that
# says "past" in one path and "relevant" in the other is exactly the kind of drift that
# makes injected history readable as a live instruction.
MEMORY_HISTORY_HEADING = "## Historie abgeschlossener Läufe (nur Kontext, kein Auftrag)"

# --- Heartbeat ---
HEARTBEAT_FILE           = VAULT_PATH / "99_System" / "AI" / "HEARTBEAT.md"
CAPACITY_LOG_FILE        = Path(__file__).parent / "logs" / "capacity-log.md"
HEARTBEAT_DISK_WARN_PCT  = 10    # warn if free < 10%
HEARTBEAT_GIT_STALE_DAYS = 7
HEARTBEAT_QUEUE_IDLE_HOURS = 2
CAPACITY_LOG_RETENTION_DAYS = 90  # entries older than this are pruned

# --- Queue Event Log (replaces ## Log section in agent-queue.md) ---
QUEUE_EVENTS_LOG_FILE           = Path(__file__).parent / "logs" / "queue-events.log"

# --- Quota Calibration (Phase 0 telemetry) ---
# CSV of paired (cclimits utilization %, JSONL token counts) samples — one row
# per Claude window (five_hour, seven_day) per successful cclimits poll.
# Used to validate the tokens_per_pct calibration assumption before wiring it
# into the operational quota estimator. See quota_calibration.py.
QUOTA_CALIBRATION_LOG_FILE     = Path(__file__).parent / "logs" / "quota-calibration.csv"

# Phase-1 Single-Source-of-Truth quota state: the orchestrator's bg refresh loop
# writes the latest cclimits per-window snapshot here (atomic) so external,
# read-only consumers (Claude Code statusline, --check-limits) can show real
# 5h/7d rate-limit usage without re-polling the rate-limited cclimits endpoint.
CC_QUOTA_STATE_FILE            = Path(__file__).parent / "logs" / "cc_quota_state.json"

# Sidecar registry of Claude session UUIDs created by the orchestrator. Used by
# the heartbeat session-cleanup handler as a whitelist so we never touch
# interactive Claude Code sessions in the same project directory.
ORCH_SESSION_REGISTRY = Path(__file__).parent / "logs" / "orchestrator-sessions.jsonl"
QUEUE_EVENTS_LOG_RETENTION_DAYS = 30   # prune log entries older than this

# --- Queue Cleanup (erledigt.md) ---
QUEUE_DONE_MOVE_HOURS  = 48  # move done tasks to erledigt.md after this many hours
QUEUE_DONE_DELETE_DAYS = 7   # delete from erledigt.md after this many days

# --- Prompt Budget (token ≈ word heuristic) ---
# Per-component caps only; there is deliberately no aggregate ceiling. Two unused
# constants (PROMPT_BUDGET_TOKENS, PROMPT_CORE_TOKENS) were removed on 2026-08-28 --
# neither had a single caller, while README claimed the first was an enforced limit.
# Wiring one up now would let a new ceiling truncate prompts that pass today.
PROMPT_CURATED_MEMORY_TOKENS  = 500    # Layer 1: curated MEMORY.md (always loaded)
PROMPT_DAILY_LOG_TOKENS       = 500    # Layer 2: today + yesterday daily log (80-char entries)
PROMPT_MEMORY_TOKENS          = 2_000  # Layer 3: TF-IDF deep search
PROMPT_WIKILINK_TOKENS        = 1_500
PROMPT_SKILL_TOKENS           = 2_000

# --- Profiles ---
PROFILES_DIR = VAULT_PATH / "99_System" / "AI" / "profiles"

# --- Policy ---
POLICY_FILE = VAULT_PATH / "99_System" / "AI" / "policy.yaml"
POLICY_APPROVAL_TIMEOUT_SEC = 600  # 10 minutes

# --- Usage Suggester ---
USAGE_SUGGEST_MIN_REMAINING_PCT   = 30
USAGE_SUGGEST_RESET_WINDOW_SEC    = 15 * 60
USAGE_SUGGEST_TIMEOUT_SEC         = 5 * 60
USAGE_SUGGEST_SKILL_COOLDOWN_DAYS = 7
USAGE_SUGGEST_RETRY_WINDOW_DAYS   = 3
USAGE_SUGGEST_TASK_COOLDOWN_DAYS  = 3  # don't re-suggest same vault task within N days
USAGE_SUGGEST_LLM_TIMEOUT_SEC    = 3 * 60
USAGE_SUGGEST_MAX_PACE_FACTOR     = 2.5  # Suppress suggestions if daily usage > 2.5× target
USAGE_SUGGEST_VAULT_TASK_DIRS     = [
    "01_Tasks/01_Tasks_Lake.md",
    "01_Tasks/02_recTasks.md",
    "01_Tasks/01_Projekte",
]

# --- Model Selection ---
# Maps task tag aliases to full CLI model IDs, per provider.
# Tags are provider-bound: #claude_opus only applies to claude, not to gemini on fallback.
CLAUDE_MODEL_ALIASES: dict[str, str] = {
    "claude_haiku": "claude-haiku-4-5-20251001",
    "claude_sonnet": "claude-sonnet-5",    # Sonnet 5 = current Sonnet tier (2026-07)
    "claude_opus":   "claude-opus-5",      # Opus 5 = current Opus tier (2026-07); 4.8 is
                                           # previous-gen. Drop-in: same price as 4.8
                                           # ($5/$25 per Mtok), same CLI surface.
}
# Claude drift-check 2026-07-30 against the canonical model table (claude-api skill):
# `claude-opus-5` supersedes `claude-opus-4-8`; `claude-fable-5` deliberately NOT adopted
# ($10/$50 per Mtok is above Opus tier — no fallback executor is worth that). Note the
# heartbeat's `_probe_model` only detects *dead* IDs, and 4.8 is still served — superseded
# IDs surface only via `_llm_check_for_newer_models()`, so this pair can drift silently
# while every check stays green. Re-verify on the quarterly Claude Code sweep.

# --- Reasoning Effort (Claude only) ---
# Valid values for `claude --effort <level>`, verified against `claude --help` on
# 2026-07-30. That verification covers the CLI values only.
#
# HEURISTIC (not measured in this repo): try lowering the effort level before dropping a
# model tier. The claim behind it — that `low`/`medium` on a current-generation model can
# match an older generation's `xhigh` — comes from the Claude Code workflow notes, not
# from any benchmark here. Treat it as a default worth trying, not as an invariant, and
# re-check it when model generations change (quarterly sweep).
#
# Selected per task via the `#effort:<level>` queue tag. Codex, Gemini, Mistral and
# OpenRouter have no equivalent flag and ignore the tag *by construction*, because
# they never read `_forced_effort` — there is deliberately no `supports_effort`
# capability check. NOT Claude-exclusive since 2026-09-04: providers/opencode.py
# reads the same property and passes the level straight through to opencode's
# `--variant` flag, raw and unmapped (opencode tolerates unknown values; `--variant
# xhigh` was measured at exit 0). Corrected 2026-09-05 — this comment still claimed
# exclusivity months after opencode joined.
#
# Ordered tuple, not a set: this is an ordinal scale, and lint/help messages must show
# it in ascending order. A sorted frozenset would print "high, low, max, medium, xhigh"
# and mislead anyone picking a level. Membership over 5 items is cheap either way.
CLAUDE_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Gemini model IDs (drift-checked 2026-07-23 against Google's deprecations page and
# the live `/v1beta/models` listing for this API key): pro stays on the 3.1 preview
# (still no GA pro model of any generation). `gemini-3.1-flash-lite` is deprecated
# (shutdown 2027-05-07) with `gemini-3.5-flash-lite` (GA 2026-07-21) as Google's named
# successor. `gemini-3.5-flash` stays — not deprecated; the newer `gemini-3.6-flash`
# (GA 2026-07-21) is a watch item, not yet adopted for a fallback executor.
GEMINI_MODEL_ALIASES: dict[str, str] = {
    "gemini_pro":        "gemini-3.1-pro-preview",
    "gemini_flash":      "gemini-3.5-flash",
    "gemini_flash_lite": "gemini-3.5-flash-lite",
}
# Codex: the GPT-5.6 family (2026-07-09) replaced gpt-5.5/gpt-5.4 — Codex CLI 0.145.0
# migrated its own bundled selections to Terra/Luna. Tier mapping: sol = flagship,
# terra = balanced default, luna = cheap variant for subagents/lighter tasks.
# All three verified live via `codex exec -m <id>` on 2026-07-23.
# NOTE: the version-shaped keys `codex_5`/`codex_5_4` now point at 5.6 IDs — kept
# stable so existing queue lines keep working; rename is a separate cleanup.
CODEX_MODEL_ALIASES: dict[str, str] = {
    "codex_5":    "gpt-5.6-sol",
    "codex_5_4":  "gpt-5.6-terra",
    "codex_mini": "gpt-5.6-luna",
}
# Mistral Vibe aliases. The VALUES are vibe's own config aliases (`active_model`
# in ~/.vibe/config.toml), not raw model names — the provider passes them through
# `VIBE_ACTIVE_MODEL`, and vibe resolves alias → model itself. An unknown value
# falls back to vibe's configured default silently (no error), so these strings
# are the single source of truth. `local` (llamacpp) is deliberately absent:
# no local inference in this workflow.
VIBE_MODEL_ALIASES: dict[str, str] = {
    "vibe_medium": "mistral-medium-3.5",   # $1.5/$7.5 per Mtok, thinking=high
    "vibe_small":  "devstral-small",       # $0.1/$0.3 per Mtok, cheap pass
}
# OpenRouter aliases: prefix `or_*` so they cannot collide with native CLI tags.
# Free models for trivial single-call tasks (heartbeat, summaries). Paid flagships
# from Chinese open-source families for higher-quality non-agentic calls — all at
# a fraction of Anthropic/Codex pricing. IDs verified against OpenRouter /models
# API on 2026-05-15.
OPENROUTER_MODEL_ALIASES: dict[str, str] = {
    # Free — $0 always (subject to OpenRouter's daily request limits)
    "or_minimax_free":  "minimax/minimax-m2.5:free",
    "or_deepseek_free": "deepseek/deepseek-v4-flash:free",
    "or_qwen_free":     "qwen/qwen3-coder:free",
    "or_nemotron_free": "nvidia/nemotron-3-super-120b-a12b:free",
    # Paid flagships — pricing per MTok (prompt / completion)
    "or_glm":      "z-ai/glm-5",                   # $0.60 / $1.92, 202k ctx
    "or_kimi":     "moonshotai/kimi-k2.6",         # $0.73 / $3.49, 262k ctx
    "or_qwen":     "qwen/qwen3-max",               # $0.78 / $3.90, 262k ctx
    "or_deepseek": "deepseek/deepseek-v4-pro",     # $0.44 / $0.87, 1M ctx
    "or_minimax":  "minimax/minimax-m2.7",         # $0.28 / $1.20, 196k ctx
}
# opencode aliases: ONLY handpicked entries. The zdr-auto-* aliases in
# opencode.json are managed by oc_sync_zdr_aliases.py and their ORDER changes
# (it re-ranks by benchmark score) — a tag pointing at one of those would
# silently pick a different model over time, so they are not valid tag targets.
# All three verified 2026-09-04 against opencode.json: present, and each
# carries data_collection:"deny" + zdr:true.
OPENCODE_MODEL_ALIASES: dict[str, str] = {
    "opencode_deepseek":      "openrouter/zdr-review",       # deepseek-v4-pro
    "opencode_deepseek_long": "openrouter/zdr-review-long",  # deepseek-v4-flash, 1M ctx
    "opencode_glm":           "openrouter/zdr-review-alt",   # glm-5.2
}
_MODEL_ALIASES_BY_PROVIDER: dict[str, dict[str, str]] = {
    "claude":     CLAUDE_MODEL_ALIASES,
    "gemini":     GEMINI_MODEL_ALIASES,
    "codex":      CODEX_MODEL_ALIASES,
    "openrouter": OPENROUTER_MODEL_ALIASES,
    "vibe":       VIBE_MODEL_ALIASES,
    "opencode":   OPENCODE_MODEL_ALIASES,
}


def model_id_for_provider(model_tag: str | None, provider_name: str) -> str | None:
    """Resolve a model alias tag to a full CLI model ID, scoped to its owning provider.

    Returns None if model_tag is falsy or does not belong to provider_name.
    Example: model_id_for_provider("claude_opus", "gemini") -> None (prevents
    accidentally forcing a Claude model ID on Gemini during provider fallback).
    """
    if not model_tag:
        return None
    return _MODEL_ALIASES_BY_PROVIDER.get(provider_name, {}).get(model_tag)


def is_known_model_tag(model_tag: str | None) -> bool:
    """Return True if model_tag matches any provider's alias table."""
    if not model_tag:
        return False
    return any(model_tag in aliases for aliases in _MODEL_ALIASES_BY_PROVIDER.values())


# Model used by the usage suggester for LLM autonomy assessment (cheap + fast)
USAGE_SUGGEST_CLAUDE_MODEL = CLAUDE_MODEL_ALIASES["claude_haiku"]

# --- Startup ---
STARTUP_DELAY_SEC = 5 * 60  # 5 minutes: wait for tokens to renew

# --- 429 Token Estimation ---
# Chars-per-token ratio for text-based estimation (fallback when no real token counts)
ESTIMATE_CHARS_PER_TOKEN = int(os.getenv("ORCH_CHARS_PER_TOKEN", "4"))
# Output tokens are weighted heavier for rate-limit capacity (Anthropic weights ~5:1)
ESTIMATE_OUTPUT_TOKEN_WEIGHT = float(os.getenv("ORCH_OUTPUT_TOKEN_WEIGHT", "5"))
# Effective tokens (input + output*weight) that equal 1% of primary window capacity.
# Tune per subscription plan. These defaults assume Claude Max / Gemini free / Codex Plus.
ESTIMATE_TOKENS_PER_PCT: dict[str, int] = {
    "claude": int(os.getenv("ORCH_TOKENS_PER_PCT_CLAUDE", "15000")),
    "gemini": int(os.getenv("ORCH_TOKENS_PER_PCT_GEMINI", "100000")),
    "codex": int(os.getenv("ORCH_TOKENS_PER_PCT_CODEX", "30000")),
}

# Calibrated per-window tokens-per-pct for Claude (Phase-0 calibration 2026-05-27,
# io_only model = input + output*weight; cache tokens excluded as they barely
# count against the rate limit). 5h and 7d differ ~14x, so a single scalar cannot
# serve both windows. The scalar ESTIMATE_TOKENS_PER_PCT["claude"] above is only a
# headline reference for estimate_task_usage_pct(); its value CANCELS OUT in the
# per-window 429-fallback split (limits._estimate_window_usage_calibrated), which
# reconstructs true per-window usage via these ratios. Values are conservative
# (low percentile → consumption overestimated → never under-report near the limit):
# 5h P25 ~5400 (CV ~18% in the >=60% danger band), 7d ~75000 (tight, CV <1% there).
ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS: dict[str, int] = {
    "five_hour": int(os.getenv("ORCH_TOKENS_PER_PCT_CLAUDE_5H", "5400")),
    "seven_day": int(os.getenv("ORCH_TOKENS_PER_PCT_CLAUDE_7D", "75000")),
}
QUOTA_CALIBRATION_MODEL = "io_only"  # informational; emitted in cc_quota_state.json

# --- Phase-2: live between-poll quota estimation (Closed-Loop-Rebalancing) ---
# (a) When enabled, get_limits() decrements the cached cclimits snapshot by the
# calibrated per-task usage estimate as tasks run; the bg refresh loop re-anchors
# (resets the estimate) on every successful poll. Default OFF — it changes
# capacity gating in normal operation (conservative factors lean toward
# over-gating, the safe side). Toggle in .env; off = instant rollback.
QUOTA_LIVE_ESTIMATE_ENABLED = _parse_bool_env("ORCH_QUOTA_LIVE_ESTIMATE", False)

# (b) Auto-recalibrate the per-window tokens_per_pct factors from the running
# calibration CSV (drift correction) instead of the frozen defaults above. Only
# effective when QUOTA_LIVE_ESTIMATE_ENABLED. Guarded by a minimum sample count
# and clamped to a sane band [default/clamp, default*clamp] around the defaults;
# falls back to the defaults on thin/insane data.
QUOTA_AUTO_RECALIBRATE_ENABLED = _parse_bool_env("ORCH_QUOTA_AUTO_RECALIBRATE", False)
QUOTA_RECALIBRATE_MIN_SAMPLES = int(os.getenv("ORCH_QUOTA_RECAL_MIN_SAMPLES", "60"))
QUOTA_RECALIBRATE_CLAMP = float(os.getenv("ORCH_QUOTA_RECAL_CLAMP", "3.0"))
QUOTA_RECALIBRATE_PERCENTILE = float(os.getenv("ORCH_QUOTA_RECAL_PERCENTILE", "25"))

# --- Parallel Worktrees (P1) ---
# Subdir name under the parent CWD where #worktree-tagged parallel subtasks
# get isolated git worktrees. Kept short to stay within Windows' 260-char path
# limit even when parent CWDs are already deep.
PARALLEL_WORKTREE_ROOT = ".worktrees"

# --- PR-Babysitter (P2) + CI-Watcher (P4) ---
# Repo whitelist (semicolon-separated "owner/name" entries in .env). Empty list
# means the tool must be invoked with an explicit #repos:<...> tag — never
# triggers on every repo the user has gh access to.
_env_pr_repos = os.getenv("PR_BABYSITTER_REPOS", "")
PR_BABYSITTER_REPOS: list[str] = [
    r.strip() for r in _env_pr_repos.split(";") if r.strip()
]
PR_BABYSITTER_QUEUE_COOLDOWN_HOURS = _parse_int_env("PR_BABYSITTER_QUEUE_COOLDOWN_HOURS", 1)
PR_BABYSITTER_GH_TIMEOUT_SEC       = _parse_int_env("PR_BABYSITTER_GH_TIMEOUT_SEC", 20)
PR_BABYSITTER_MAX_PRS_PER_REPO     = _parse_int_env("PR_BABYSITTER_MAX_PRS_PER_REPO", 20)

# CI-Watcher: whitelist for `gh run list --status=failure` polling. Same format
# as PR_BABYSITTER_REPOS. Empty list disables the heartbeat handler.
_env_ci_repos = os.getenv("CI_WATCHER_REPOS", "")
CI_WATCHER_REPOS: list[str] = [
    r.strip() for r in _env_ci_repos.split(";") if r.strip()
]
CI_WATCHER_MAX_RUNS_PER_REPO       = _parse_int_env("CI_WATCHER_MAX_RUNS_PER_REPO", 20)
CI_WATCHER_QUEUE_COOLDOWN_HOURS    = _parse_int_env("CI_WATCHER_QUEUE_COOLDOWN_HOURS", 2)
# Optional mapping `owner/repo=local/path[;owner/repo=local/path]` so the queue
# item can carry a usable `cwd:` tag straight to dev-loop. Unmapped repos
# get a queue item without cwd — the user has to add one before it runs.
_env_ci_paths = os.getenv("CI_WATCHER_REPO_PATHS", "")
CI_WATCHER_REPO_PATHS: dict[str, str] = {}
for _entry in _env_ci_paths.split(";"):
    _entry = _entry.strip()
    if "=" in _entry:
        _k, _v = _entry.split("=", 1)
        _k, _v = _k.strip(), _v.strip()
        if _k and _v:
            CI_WATCHER_REPO_PATHS[_k] = _v

# --- Shutdown ---
SHUTDOWN_DELAY_SEC = 60

# --- Dashboard ---
# Default 8211 (8411 fell inside a Windows dynamically-reserved port range
# 8386-8485 → WSAEACCES/WinError 10013 on bind). Overridable via .env; the
# dashboard also falls back to a free port at bind time if this one is taken.
DASHBOARD_PORT = _parse_int_env("DASHBOARD_PORT", 8211)
SHUTDOWN_COMMAND = (
    ["shutdown", "/s", "/t", "0", "/f"]
    if sys.platform == "win32"
    else ["sudo", "shutdown", "-h", "now"]
)

# --- SOUL.md (Personality-as-Config) ---
import threading as _threading
_soul_lock = _threading.Lock()
_soul_cache: dict[str, str] | None = None
_soul_mtime: float = 0.0


def _parse_soul_sections(content: str) -> dict[str, str]:
    """Parse SOUL.md into sections keyed by 'base' and provider names."""
    sections: dict[str, str] = {}

    # Split by ### <ProviderName> headings
    parts = re.split(r"^###\s+(\w+)\s*$", content, flags=re.MULTILINE)

    # parts[0] is everything before the first ### heading
    # Followed by alternating: heading, content, heading, content, ...
    base_text = parts[0]

    # Extract ## Base section from the preamble
    base_match = re.search(r"^##\s+Base\s*\n(.*?)(?=^##|\Z)", base_text, re.MULTILINE | re.DOTALL)
    if base_match:
        sections["base"] = base_match.group(1).strip()
    else:
        sections["base"] = base_text.strip()

    # Parse provider-specific sections
    # re.split with a capturing group produces [before, g1, c1, g2, c2, ...].
    # Step by 2 starting at index 1 to visit all (heading, content) pairs.
    for i in range(1, len(parts), 2):
        provider_name = parts[i].strip().lower()
        provider_content = parts[i + 1].strip()
        # Strip HTML comments
        provider_content = re.sub(r"<!--.*?-->", "", provider_content, flags=re.DOTALL).strip()
        if provider_content:
            sections[provider_name] = provider_content

    return sections


def load_soul() -> dict[str, str]:
    """Load SOUL.md from vault. Returns {'base': ..., 'claude': ..., ...}.
    Falls back to empty dict (use hardcoded SYSTEM_PROMPTS) if file missing."""
    global _soul_cache, _soul_mtime

    soul_file = VAULT_PATH / "99_System" / "AI" / "SOUL.md"
    if not soul_file.exists():
        return {}

    try:
        with _soul_lock:
            mtime = soul_file.stat().st_mtime
            if _soul_cache is not None and mtime == _soul_mtime:
                return _soul_cache

            content = soul_file.read_text(encoding="utf-8")
            sections = _parse_soul_sections(content)
            _soul_cache = sections
            _soul_mtime = mtime
            return sections
    except (OSError, ValueError, KeyError, re.error):
        return {}


def get_system_prompt(provider_name: str) -> str:
    """Get assembled system prompt for provider. Falls back to hardcoded SYSTEM_PROMPTS."""
    soul = load_soul()
    if not soul:
        return SYSTEM_PROMPTS.get(provider_name, "")
    base = soul.get("base", "")
    override = soul.get(provider_name.lower(), "")
    return f"{base}\n\n{override}".strip() if override else base
