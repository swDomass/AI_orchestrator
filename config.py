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
SAFETY_RULES = """\
Safety rules (MUST follow):
- NEVER run: rm -rf, git push --force, git reset --hard, DROP TABLE, format, mkfs
- NEVER delete more than 5 files in a single operation
- NEVER push to remote repositories unless the task explicitly says to
- NEVER modify files outside the working directory unless the task explicitly says to
- Prefer creating new files/branches over overwriting existing ones
- If unsure whether an action is destructive, skip it and report what you would have done"""

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
    else [
        Path("D:/programmieren"),
        Path(r"literal:/path/to/your/obsidian_vault"),
        Path(r"literal:D:\projects\work"),
        Path(r"literal:D:\OneDrive - YourOrg\YourOrg-Data\marketing\AI_Marketing"),
    ]
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

# Critical-Review timeouts
TOOL_CR_REVIEW_TIMEOUT_SEC = 2_400  # 40 min: full radical-honesty architectural review

# Security-Audit timeouts
TOOL_SA_AUDIT_TIMEOUT_SEC  = 2_400  # 40 min: read-only vulnerability scan (Phase 1)

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
QUEUE_EVENTS_LOG_RETENTION_DAYS = 30   # prune log entries older than this

# --- Queue Cleanup (erledigt.md) ---
QUEUE_DONE_MOVE_HOURS  = 48  # move done tasks to erledigt.md after this many hours
QUEUE_DONE_DELETE_DAYS = 7   # delete from erledigt.md after this many days

# --- Prompt Budget (token ≈ word heuristic) ---
PROMPT_BUDGET_TOKENS          = 10_000
PROMPT_CORE_TOKENS            = 200
PROMPT_CURATED_MEMORY_TOKENS  = 500    # Layer 1: curated MEMORY.md (always loaded)
PROMPT_DAILY_LOG_TOKENS       = 1_500  # Layer 2: today + yesterday daily log
PROMPT_MEMORY_TOKENS          = 2_000  # Layer 3: TF-IDF deep search
PROMPT_WIKILINK_TOKENS        = 3_000
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

# --- Claude Model Selection ---
# Maps task tag aliases to full Claude CLI model IDs.
CLAUDE_MODEL_ALIASES: dict[str, str] = {
    "claude_haiku": "claude-haiku-4-5-20251001",
    "claude_sonnet": "claude-sonnet-4-6",
    "claude_opus":   "claude-opus-4-6",
}
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
