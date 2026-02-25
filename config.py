import os
from pathlib import Path


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
            value = value.strip()
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
SLEEP_POLL_INTERVAL = 60

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
