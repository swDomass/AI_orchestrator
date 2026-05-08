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

# Timeout for a single CLI task call (seconds)
TASK_TIMEOUT_SEC = 900  # 15 minutes

# Timeout for interactive Telegram chat responses (seconds)
TELEGRAM_CHAT_TIMEOUT_SEC = 180  # 3 minutes

# Send "still thinking..." notification after this many seconds without response
TELEGRAM_CHAT_THINKING_SEC = 30

# Max retries per provider before falling back to next provider
MAX_RETRIES_PER_PROVIDER = 2

# Max file size for context injection (bytes)
MAX_CONTEXT_FILE_SIZE = 1_000_000  # 1 MB

# --- Safety Guardrails ---

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
    (r"git\s+push\s+.*--force", "git push --force"),
    (r"git\s+push\s+.*-f\b", "git push -f (force)"),
    (r"git\s+reset\s+--hard", "git reset --hard"),
    (r"git\s+clean\s+-[a-zA-Z]*f", "git clean -f (untracked file deletion)"),
    (r"git\s+checkout\s+--\s+\.", "git checkout -- . (discard all changes)"),
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

# System prompts per provider (prepended to each task)
_BASE_PROMPT = "Antworte auf Deutsch, praegnant und strukturiert."
SYSTEM_PROMPTS: dict[str, str] = {
    "claude": f"{_BASE_PROMPT}\n\n{SAFETY_RULES}",
    "gemini": f"{_BASE_PROMPT}\n\n{SAFETY_RULES}",
    "codex": f"{_BASE_PROMPT}\n\n{SAFETY_RULES}",
}

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

# --- Heartbeat ---
HEARTBEAT_FILE           = VAULT_PATH / "99_System" / "AI" / "HEARTBEAT.md"
CAPACITY_LOG_FILE        = Path(__file__).parent / "logs" / "capacity-log.md"
HEARTBEAT_DISK_WARN_PCT  = 10    # warn if free < 10%
HEARTBEAT_GIT_STALE_DAYS = 7
HEARTBEAT_QUEUE_IDLE_HOURS = 2
CAPACITY_LOG_RETENTION_DAYS = 90  # entries older than this are pruned

# --- Queue Event Log (replaces ## Log section in agent-queue.md) ---
QUEUE_EVENTS_LOG_FILE           = Path(__file__).parent / "logs" / "queue-events.log"

# Sidecar registry of Claude session UUIDs created by the orchestrator. Used by
# the heartbeat session-cleanup handler as a whitelist so we never touch
# interactive Claude Code sessions in the same project directory.
ORCH_SESSION_REGISTRY = Path(__file__).parent / "logs" / "orchestrator-sessions.jsonl"
QUEUE_EVENTS_LOG_RETENTION_DAYS = 30   # prune log entries older than this

# --- Queue Cleanup (erledigt.md) ---
QUEUE_DONE_MOVE_HOURS  = 48  # move done tasks to erledigt.md after this many hours
QUEUE_DONE_DELETE_DAYS = 7   # delete from erledigt.md after this many days

# --- Prompt Budget (token ≈ word heuristic) ---
PROMPT_BUDGET_TOKENS          = 10_000
PROMPT_CORE_TOKENS            = 200
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
    "claude_sonnet": "claude-sonnet-4-6",
    "claude_opus":   "claude-opus-4-7",
}
GEMINI_MODEL_ALIASES: dict[str, str] = {
    "gemini_pro":        "gemini-3.1-pro-preview",
    "gemini_flash":      "gemini-3-flash-preview",
    "gemini_flash_lite": "gemini-3.1-flash-lite-preview",
}
CODEX_MODEL_ALIASES: dict[str, str] = {
    "codex_mini": "gpt-5.4-mini",
}
_MODEL_ALIASES_BY_PROVIDER: dict[str, dict[str, str]] = {
    "claude": CLAUDE_MODEL_ALIASES,
    "gemini": GEMINI_MODEL_ALIASES,
    "codex":  CODEX_MODEL_ALIASES,
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

# --- Shutdown ---
SHUTDOWN_DELAY_SEC = 60

# --- Dashboard ---
DASHBOARD_PORT = 8411
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
