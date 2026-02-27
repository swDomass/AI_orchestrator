import os
import re
import sys
from pathlib import Path


def _normalize_dotenv_value(value: str) -> str:
    """Strip surrounding quotes from .env values (supports trailing comments)."""
    m = re.match(r'^(["\'])(.*)\1(?:\s+#.*)?$', value)
    if m:
        return m.group(2)
    return value


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

# Minimum remaining capacity to consider a provider usable (percent)
MIN_CAPACITY_PERCENT = 5

# How long to wait between cclimits polls when sleeping (seconds)
SLEEP_POLL_INTERVAL = 5 * 60

# Timeout for a single CLI task call (seconds)
TASK_TIMEOUT_SEC = 5 * 60  # 5 minutes

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
NOTIFY_ON_TASK_DONE = True
NOTIFY_ON_ERROR = True
NOTIFY_ON_QUEUE_COMPLETE = True
NOTIFY_ON_ALL_PROVIDERS_EXHAUSTED = True

# --- Security ---
# Allowed root directories for cwd: tags (empty list = allow all).
# When set, only tasks with cwd paths under these roots will be executed.
# Example: ALLOWED_CWD_ROOTS = [Path("D:/programmieren"), Path("C:/projects")]
ALLOWED_CWD_ROOTS: list[Path] = [
    Path("D:/programmieren"),
    Path(r"literal:/path/to/your/obsidian_vault"),
    Path(r"literal:D:\projects\work"),
    Path(r"literal:D:\OneDrive - YourOrg\YourOrg-Data\marketing\AI_Marketing")
]

# Max task length accepted via Telegram /task command (characters)
TELEGRAM_MAX_TASK_LENGTH = 500

# --- Tools ---
# Max iterations for review/fix loops
TOOL_MAX_ITERATIONS = 10
TOOL_REVIEW_TIMEOUT_SEC = 20 * 60  # 20 min per review
TOOL_FIX_TIMEOUT_SEC = 40 * 60     # 40 min per fix

# --- Logging ---
LOG_FILE = Path(__file__).parent / "logs" / "orchestrator.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 3

# --- Memory System ---
MEMORY_HALF_LIFE_DAYS    = 30
MEMORY_MAX_AGE_DAYS      = 180
MEMORY_TOP_K             = 5
MEMORY_SUMMARY_MAX_CHARS = 700   # first 500 + "...\n" + last 200

# --- Heartbeat ---
HEARTBEAT_FILE           = VAULT_PATH / "99_System" / "AI" / "HEARTBEAT.md"
HEARTBEAT_DISK_WARN_PCT  = 10    # warn if free < 10%
HEARTBEAT_GIT_STALE_DAYS = 7
HEARTBEAT_QUEUE_IDLE_HOURS = 2

# --- Prompt Budget (token ≈ word heuristic) ---
PROMPT_BUDGET_TOKENS     = 8_000
PROMPT_CORE_TOKENS       = 200
PROMPT_MEMORY_TOKENS     = 2_000
PROMPT_WIKILINK_TOKENS   = 3_000
PROMPT_SKILL_TOKENS      = 2_000

# --- Profiles ---
PROFILES_DIR = VAULT_PATH / "99_System" / "AI" / "profiles"

# --- Policy ---
POLICY_FILE = VAULT_PATH / "99_System" / "AI" / "policy.yaml"
POLICY_APPROVAL_TIMEOUT_SEC = 600  # 10 minutes

# --- Shutdown ---
SHUTDOWN_DELAY_SEC = 60
SHUTDOWN_COMMAND = (
    ["shutdown", "/s", "/t", "0"]
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
    except Exception:
        return {}


def get_system_prompt(provider_name: str) -> str:
    """Get assembled system prompt for provider. Falls back to hardcoded SYSTEM_PROMPTS."""
    soul = load_soul()
    if not soul:
        return SYSTEM_PROMPTS.get(provider_name, "")
    base = soul.get("base", "")
    override = soul.get(provider_name.lower(), "")
    return f"{base}\n\n{override}".strip() if override else base
