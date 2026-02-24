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

# Max retries per provider before falling back to next provider
MAX_RETRIES_PER_PROVIDER = 2

# Max file size for context injection (bytes)
MAX_CONTEXT_FILE_SIZE = 1_000_000  # 1 MB

# System prompts per provider (prepended to each task)
SYSTEM_PROMPTS: dict[str, str] = {
    "claude": "Antworte auf Deutsch, praegnant und strukturiert.",
    "gemini": "Antworte auf Deutsch, praegnant und strukturiert.",
    "codex": "Antworte auf Deutsch, praegnant und strukturiert.",
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

# --- Tools ---
# Max iterations for review/fix loops
TOOL_MAX_ITERATIONS = 10
TOOL_REVIEW_TIMEOUT_SEC = 20 * 60  # 20 min per review
TOOL_FIX_TIMEOUT_SEC = 40 * 60     # 40 min per fix
