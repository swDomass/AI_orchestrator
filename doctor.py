"""
AI Orchestrator — Doctor / Onboarding Validator

Checks the full setup and reports pass/warn/fail status for each component.

Usage:
    python orchestrator.py --doctor
    python orchestrator.py --doctor --fix
    python orchestrator.py --doctor --fix --yes
"""

import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

from config import VAULT_PATH, QUEUE_FILE


# ── ANSI colours (stripped on non-TTY) ────────────────────────────────────────

def _c(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

GREEN  = lambda t: _c(t, "32")
RED    = lambda t: _c(t, "31")
YELLOW = lambda t: _c(t, "33")
BOLD   = lambda t: _c(t, "1")


# ── Check result ───────────────────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


class CheckResult:
    def __init__(self, status: str, label: str, message: str, fix_hint: str = "", fix_fn=None):
        self.status = status       # PASS | FAIL | WARN
        self.label = label         # Short label, e.g. "Claude CLI"
        self.message = message     # One-line detail
        self.fix_hint = fix_hint   # e.g. "npm install -g @anthropic/claude-code"
        self.fix_fn = fix_fn       # Optional callable that applies the fix

    def __repr__(self) -> str:
        return f"CheckResult({self.status}, {self.label!r}, {self.message!r})"


# ── Individual checks ──────────────────────────────────────────────────────────

def _check_cli(label: str, cmd: str, install_hint: str = "") -> CheckResult:
    """Check if a CLI binary is available in PATH."""
    found = shutil.which(cmd) or shutil.which(cmd + ".cmd") or shutil.which(cmd + ".exe")
    if found:
        # Try to get version
        try:
            r = subprocess.run(
                [cmd, "--version"],
                capture_output=True, text=True, timeout=5,
                shell=os.name == "nt",
            )
            version = (r.stdout or r.stderr).strip().splitlines()[0][:60]
        except Exception:
            version = found
        return CheckResult(PASS, label, version)
    return CheckResult(
        FAIL, label,
        f"not found in PATH",
        fix_hint=install_hint,
    )


def check_claude_cli() -> CheckResult:
    r = _check_cli("Claude CLI", "claude", "npm install -g @anthropic/claude-code")
    if r.status != PASS:
        return r
    # Doctor currently checks CLI presence/version only; it does not validate auth.
    return CheckResult(PASS, "Claude CLI", f"{r.message} (auth not verified)")


def check_gemini_cli() -> CheckResult:
    return _check_cli("Gemini CLI", "gemini", "npm install -g @google/gemini-cli")


def check_codex_cli() -> CheckResult:
    return _check_cli("Codex CLI", "codex", "npm install -g @openai/codex")


def check_node() -> CheckResult:
    return _check_cli("Node.js", "node", "https://nodejs.org")


def check_git() -> CheckResult:
    return _check_cli("Git", "git", "https://git-scm.com")


def check_cclimits() -> CheckResult:
    try:
        r = subprocess.run(
            ["npx", "cclimits", "--json"],
            capture_output=True, text=True, timeout=15,
            shell=os.name == "nt",
        )
        if r.returncode == 0:
            return CheckResult(PASS, "cclimits", "npx cclimits --json succeeded")
        return CheckResult(WARN, "cclimits", f"exited {r.returncode}: {(r.stderr or r.stdout).strip()[:80]}")
    except FileNotFoundError:
        return CheckResult(WARN, "cclimits", "npx not found — Node.js required")
    except subprocess.TimeoutExpired:
        return CheckResult(WARN, "cclimits", "timed out after 15s")
    except Exception as e:
        return CheckResult(WARN, "cclimits", str(e)[:80])


def check_vault_path() -> CheckResult:
    if not VAULT_PATH or str(VAULT_PATH) in ("", "."):
        return CheckResult(FAIL, "Vault path", "ORCH_VAULT_PATH not set",
                           fix_hint="Set ORCH_VAULT_PATH in .env")
    if VAULT_PATH.is_dir():
        return CheckResult(PASS, "Vault path", str(VAULT_PATH))
    return CheckResult(FAIL, "Vault path", f"directory not found: {VAULT_PATH}",
                       fix_hint="Set correct ORCH_VAULT_PATH in .env")


def check_queue_file() -> CheckResult:
    if QUEUE_FILE.is_file():
        return CheckResult(PASS, "Queue file", str(QUEUE_FILE))

    def _fix():
        from queue_manager import ensure_queue_file
        ensure_queue_file()

    return CheckResult(
        WARN, "Queue file",
        f"not found: {QUEUE_FILE}",
        fix_hint="Will be created automatically on first run",
        fix_fn=_fix,
    )


def check_telegram_bot() -> CheckResult:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token:
        return CheckResult(WARN, "Telegram bot", "TELEGRAM_BOT_TOKEN not set — notifications disabled")
    if not chat_id:
        return CheckResult(WARN, "Telegram bot", "TELEGRAM_CHAT_ID not set — notifications disabled")

    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status == 200:
                import json
                data = json.loads(resp.read())
                name = data.get("result", {}).get("username", "?")
                return CheckResult(PASS, "Telegram bot", f"@{name} authenticated")
            return CheckResult(WARN, "Telegram bot", f"getMe returned {resp.status}")
    except urllib.error.HTTPError as e:
        return CheckResult(WARN, "Telegram bot", f"getMe returned {e.code} (token may be invalid)")
    except Exception as e:
        return CheckResult(WARN, "Telegram bot", f"request failed: {e}")


def check_env_file() -> CheckResult:
    repo_root = Path(__file__).parent
    env_file = repo_root / ".env"

    def _fix():
        example = repo_root / ".env.example"
        if example.exists():
            import shutil as _shutil
            _shutil.copy(example, env_file)
            print(f"    Created .env from .env.example — edit it to set your credentials.")
        else:
            env_file.write_text(
                "TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nORCH_VAULT_PATH=\n",
                encoding="utf-8",
            )
            print(f"    Created minimal .env template at {env_file}")

    if not env_file.exists():
        return CheckResult(
            WARN, ".env file",
            "not found — defaults will be used",
            fix_hint="Create .env with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ORCH_VAULT_PATH",
            fix_fn=_fix,
        )

    # Check for required keys
    missing = []
    content = env_file.read_text(encoding="utf-8", errors="replace")
    for key in ("TELEGRAM_BOT_TOKEN", "ORCH_VAULT_PATH"):
        if not any(line.startswith(key + "=") for line in content.splitlines()):
            missing.append(key)

    if missing:
        return CheckResult(
            WARN, ".env file",
            f"found but missing keys: {', '.join(missing)}",
        )
    return CheckResult(PASS, ".env file", str(env_file))


def check_memory_dir() -> CheckResult:
    """Check that the memory directory is accessible and writable."""
    try:
        from config import VAULT_PATH
        memory_root = VAULT_PATH / "99_System" / "AI" / "memory"

        if not VAULT_PATH.is_dir():
            return CheckResult(WARN, "Memory dir", "vault not accessible — skipped")

        if not memory_root.exists():
            def _fix():
                (memory_root / "task_results").mkdir(parents=True, exist_ok=True)
                (memory_root / "archive").mkdir(parents=True, exist_ok=True)
                print(f"    Created memory directory at {memory_root}")

            return CheckResult(
                WARN, "Memory dir",
                f"not found: {memory_root}",
                fix_hint="Will be created automatically on first task completion",
                fix_fn=_fix,
            )

        task_results = memory_root / "task_results"
        count = len(list(task_results.glob("*.md"))) if task_results.exists() else 0
        return CheckResult(PASS, "Memory dir", f"{memory_root.name} ({count} stored results)")
    except Exception as e:
        return CheckResult(WARN, "Memory dir", f"check failed: {e}")


def check_heartbeat_file() -> CheckResult:
    """Check that HEARTBEAT.md exists and is parseable."""
    try:
        from config import HEARTBEAT_FILE
        if not HEARTBEAT_FILE.exists():
            def _fix():
                HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
                HEARTBEAT_FILE.write_text(
                    "# Heartbeat Checks\n\n"
                    "## Every 30 minutes\n"
                    "- [ ] Check if queue has been empty for >2 hours → notify via Telegram\n\n"
                    "## Every 2 hours\n"
                    "- [ ] Check disk space on project drives\n\n"
                    "## Daily (first run after 08:00)\n"
                    "- [ ] Summarize yesterday's completed tasks → post to Telegram\n",
                    encoding="utf-8",
                )
                print(f"    Created HEARTBEAT.md at {HEARTBEAT_FILE}")

            return CheckResult(
                WARN, "HEARTBEAT.md",
                f"not found: {HEARTBEAT_FILE}",
                fix_hint="Create HEARTBEAT.md in vault 99_System/AI/",
                fix_fn=_fix,
            )

        from heartbeat import _parse_heartbeat_md
        content = HEARTBEAT_FILE.read_text(encoding="utf-8")
        items = _parse_heartbeat_md(content)
        return CheckResult(PASS, "HEARTBEAT.md", f"{len(items)} check(s) configured")
    except Exception as e:
        return CheckResult(WARN, "HEARTBEAT.md", f"check failed: {e}")


def check_profiles() -> CheckResult:
    """Scan PROFILES_DIR, report count and any invalid YAML."""
    try:
        from config import PROFILES_DIR, VAULT_PATH
        if not VAULT_PATH.is_dir():
            return CheckResult(WARN, "Profiles dir", "vault not accessible — skipped")

        if not PROFILES_DIR.exists():
            def _fix():
                PROFILES_DIR.mkdir(parents=True, exist_ok=True)
                print(f"    Created profiles directory at {PROFILES_DIR}")

            return CheckResult(
                WARN, "Profiles dir",
                f"not found: {PROFILES_DIR}",
                fix_hint="Will be created automatically on first profile use",
                fix_fn=_fix,
            )

        yaml_files = list(PROFILES_DIR.glob("*.yaml"))
        if not yaml_files:
            return CheckResult(WARN, "Profiles dir", f"no profiles found in {PROFILES_DIR}")

        # Validate each YAML
        invalid = []
        for f in yaml_files:
            try:
                import yaml
                with open(f, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if not isinstance(data, dict):
                    invalid.append(f.name)
            except Exception:
                invalid.append(f.name)

        if invalid:
            return CheckResult(
                WARN, "Profiles dir",
                f"{len(yaml_files)} profiles, {len(invalid)} invalid: {', '.join(invalid)}"
            )

        return CheckResult(PASS, "Profiles dir", f"{len(yaml_files)} profile(s) found")
    except Exception as e:
        return CheckResult(WARN, "Profiles dir", f"check failed: {e}")


def check_policy_file() -> CheckResult:
    """Check that policy.yaml exists and is valid YAML."""
    try:
        from config import POLICY_FILE, VAULT_PATH
        if not VAULT_PATH.is_dir():
            return CheckResult(WARN, "Policy file", "vault not accessible — skipped")

        if not POLICY_FILE.exists():
            def _fix():
                POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
                POLICY_FILE.write_text(
                    "# AI Orchestrator — Policy\n\n"
                    "auto:\n"
                    "  - \"git add\"\n"
                    "  - \"git commit\"\n"
                    "  - \"pytest\"\n"
                    "  - \"npm install\"\n"
                    "  - \"pip install\"\n\n"
                    "approve:\n"
                    "  - pattern: \"git push\"\n"
                    "    message: \"git push to remote\"\n"
                    "  - pattern: \"npm publish\"\n"
                    "    message: \"npm publish package\"\n\n"
                    "deny:\n"
                    "  - \"git push --force.*(main|master)\"\n"
                    "  - \"rm -rf /\"\n"
                    "  - \"DROP (TABLE|DATABASE)\"\n"
                    "  - \"format [A-Z]:\"\n"
                    "  - \"mkfs\"\n",
                    encoding="utf-8",
                )
                print(f"    Created policy.yaml at {POLICY_FILE}")

            return CheckResult(
                WARN, "Policy file",
                f"not found: {POLICY_FILE}",
                fix_hint="Create policy.yaml in vault 99_System/AI/",
                fix_fn=_fix,
            )

        try:
            import yaml
            with open(POLICY_FILE, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return CheckResult(FAIL, "Policy file", "invalid YAML (not a mapping)")
            auto_count = len(data.get("auto", []))
            approve_count = len(data.get("approve", []))
            deny_count = len(data.get("deny", []))
            return CheckResult(
                PASS, "Policy file",
                f"{auto_count} auto, {approve_count} approve, {deny_count} deny rules"
            )
        except Exception as e:
            return CheckResult(FAIL, "Policy file", f"invalid YAML: {e}")
    except Exception as e:
        return CheckResult(WARN, "Policy file", f"check failed: {e}")


def check_skills() -> CheckResult:
    try:
        from skills.discovery import discover_skills
        from skills.gating import check_requirements
        skills = discover_skills(vault_path=VAULT_PATH)
        total = len(skills)
        gated = 0
        for skill in skills.values():
            ok, _ = check_requirements(skill)
            if not ok:
                gated += 1
        available = total - gated
        msg = f"{total} discovered, {available} available, {gated} gated"
        return CheckResult(PASS if total > 0 else WARN, "Skills", msg)
    except Exception as e:
        return CheckResult(WARN, "Skills", f"discovery failed: {e}")


# ── Formatting ─────────────────────────────────────────────────────────────────

def _format_result(r: CheckResult) -> str:
    if r.status == PASS:
        tag = GREEN(f"[{PASS}]")
    elif r.status == FAIL:
        tag = RED(f"[{FAIL}]")
    else:
        tag = YELLOW(f"[{WARN}]")

    label = r.label.ljust(18)
    line = f"{tag} {label} {r.message}"
    if r.fix_hint and r.status != PASS:
        line += f"\n  {YELLOW('Fix')}: {r.fix_hint}"
    return line


# ── Main doctor runner ─────────────────────────────────────────────────────────

def run_doctor(fix: bool = False, yes: bool = False) -> bool:
    """Run all checks. Returns True if all pass/warn (no fails)."""
    print(BOLD("\nAI Orchestrator — Doctor"))
    print("=" * 40)

    checks = [
        check_claude_cli(),
        check_gemini_cli(),
        check_codex_cli(),
        check_node(),
        check_git(),
        check_cclimits(),
        check_vault_path(),
        check_queue_file(),
        check_telegram_bot(),
        check_env_file(),
        check_skills(),
        check_memory_dir(),
        check_heartbeat_file(),
        check_profiles(),
        check_policy_file(),
    ]

    any_fail = False
    fixable = []

    for r in checks:
        print(_format_result(r))
        if r.status == FAIL:
            any_fail = True
        if r.status in (FAIL, WARN) and r.fix_fn:
            fixable.append(r)

    print()

    if fix and fixable:
        print(BOLD("Fixable issues:"))
        for r in fixable:
            print(f"  • {r.label}: {r.fix_hint or 'auto-fix available'}")
            if yes:
                apply = True
            else:
                answer = input(f"  Apply fix for '{r.label}'? [y/N] ").strip().lower()
                apply = answer == "y"
            if apply:
                try:
                    r.fix_fn()
                except Exception as e:
                    print(f"    Fix failed: {e}")
        print()

    if any_fail:
        print(RED("Result: FAIL — fix the issues above and re-run --doctor"))
    else:
        print(GREEN("Result: OK — all checks passed or warned only"))

    return not any_fail


# ── Startup checks (minimal subset for run_watch) ─────────────────────────────

def run_startup_checks() -> bool:
    """Run critical startup checks: vault, queue, ≥1 provider CLI.

    Returns True if all critical checks pass.
    Prints errors and returns False on any failure.
    """
    failed = False

    vault_check = check_vault_path()
    if vault_check.status == FAIL:
        print(f"CRITICAL: {vault_check.label}: {vault_check.message}")
        failed = True

    queue_check = check_queue_file()
    if queue_check.status == FAIL:
        print(f"CRITICAL: {queue_check.label}: {queue_check.message}")
        failed = True
    elif queue_check.status == WARN and queue_check.fix_fn:
        # Auto-create queue file on startup
        try:
            queue_check.fix_fn()
        except Exception:
            pass

    # At least one provider CLI must be available
    provider_checks = [
        ("claude", check_claude_cli()),
        ("gemini", check_gemini_cli()),
        ("codex", check_codex_cli()),
    ]
    available_providers = [name for name, r in provider_checks if r.status == PASS]
    if not available_providers:
        print("CRITICAL: No provider CLIs found (claude, gemini, codex). Install at least one.")
        for name, r in provider_checks:
            print(f"  {name}: {r.message}  {r.fix_hint}")
        failed = True
    else:
        print(f"[startup] Providers available: {', '.join(available_providers)}")

    if failed:
        # Try to send Telegram warning if possible
        try:
            from notifier import notify_error
            notify_error("startup", "system", "Startup checks failed — orchestrator not started")
        except Exception:
            pass

    return not failed
