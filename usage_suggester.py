"""
AI Orchestrator — Usage Suggester

When running in --watch mode the heartbeat calls this module every 5 minutes.
It checks whether Claude's usage window is about to reset with significant
capacity still unused (>30 % remaining, <15 min to reset) and the queue is
empty.  If so it gathers 2-3 task suggestions from four strategies:

  1. Vault skills that haven't run in the last 7 days  (score 1.0 – 3.0)
  2. Git repos with uncommitted changes                (score 0.8 – 1.3)
  3. Recently failed tasks from memory                 (score 0.6)
  4. Open vault tasks assessed for AI autonomy         (score 0.5 – 2.5)

The top 3 candidates are sent to Telegram.  The user responds with:

  /pick N    — pick suggestion N, adds it to the queue automatically
  /decline   — dismiss all suggestions (20 min cooldown before next)

If no response arrives within 5 minutes the suggestions expire silently.
"""

import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from config import (
    ALLOWED_CWD_ROOTS,
    USAGE_SUGGEST_CLAUDE_MODEL,
    USAGE_SUGGEST_LLM_TIMEOUT_SEC,
    USAGE_SUGGEST_MIN_REMAINING_PCT,
    USAGE_SUGGEST_RESET_WINDOW_SEC,
    USAGE_SUGGEST_TIMEOUT_SEC,
    USAGE_SUGGEST_SKILL_COOLDOWN_DAYS,
    USAGE_SUGGEST_RETRY_WINDOW_DAYS,
    USAGE_SUGGEST_VAULT_TASK_DIRS,
    VAULT_PATH,
)

logger = logging.getLogger(__name__)

_COOLDOWN_SEC = 20 * 60  # Don't re-fire within same window


@dataclass
class Suggestion:
    rank: int
    label: str
    task_text: str
    source: str       # "skill" | "git" | "retry" | "vault"
    score: float


class UsageSuggester:
    """Singleton that checks limits and proposes idle-time tasks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._suggestion_event: Optional[threading.Event] = None
        self._suggestion_response: str = ""
        self._last_triggered: Optional[datetime] = None
        self._pending_suggestions: list[Suggestion] = []

    # ------------------------------------------------------------------
    # Public: called from heartbeat handler
    # ------------------------------------------------------------------

    def check_and_suggest(self, queue_read_fn: Callable) -> Optional[str]:
        """Main entry point. Returns a status string or None."""

        # Guard: already waiting for a response / cooldown
        now = datetime.now()
        with self._lock:
            if self._suggestion_event is not None:
                return None
            if self._last_triggered and (now - self._last_triggered).total_seconds() < _COOLDOWN_SEC:
                return None

        # Guard: no pending policy approval
        try:
            from policy import get_engine
            if get_engine().has_pending_approval():
                return None
        except Exception:
            pass

        # Guard: queue must be empty
        try:
            tasks = queue_read_fn()
            if tasks:
                return None
        except Exception:
            return None

        # Check limits
        remaining_pct, resets_in_sec = self._get_claude_limits()
        if remaining_pct is None:
            return None

        if remaining_pct < USAGE_SUGGEST_MIN_REMAINING_PCT:
            return None
        if resets_in_sec > USAGE_SUGGEST_RESET_WINDOW_SEC:
            return None

        # Gather suggestions
        suggestions = self._gather_suggestions()
        if not suggestions:
            logger.debug("usage-suggest: no suggestions found")
            return None

        event = threading.Event()
        with self._lock:
            self._pending_suggestions = suggestions
            self._suggestion_event = event
            self._suggestion_response = ""
            # Mark triggered BEFORE notification to prevent rapid retry loops
            # if the notification keeps failing (e.g. network issues).
            self._last_triggered = now

        try:
            # Send via Telegram
            from notifier import notify_usage_suggestions
            notify_usage_suggestions(suggestions, remaining_pct, resets_in_sec)

            # Block waiting for response
            responded = event.wait(timeout=USAGE_SUGGEST_TIMEOUT_SEC)
        except Exception as e:
            logger.warning("usage-suggest: notification/wait failed: %s", e)
            return None
        finally:
            # Always clean up pending state so we never leave a dangling event
            with self._lock:
                response = self._suggestion_response
                pending = list(self._pending_suggestions)
                self._suggestion_event = None
                self._pending_suggestions = []

        if not responded and not response:
            logger.info("usage-suggest: timeout, no response")
            return "timeout"

        if response == "decline":
            logger.info("usage-suggest: user declined")
            return "declined"

        # Parse pick N
        try:
            pick_idx = int(response) - 1
            if 0 <= pick_idx < len(pending):
                chosen = pending[pick_idx]
                from queue_manager import append_task
                if append_task(chosen.task_text):
                    from notifier import send_message
                    safe = chosen.task_text[:100].replace("`", "'")
                    send_message(f"✅ Task zur Queue hinzugefügt:\n`{safe}`")
                    logger.info("usage-suggest: picked #%d: %s", pick_idx + 1, chosen.label)
                    return f"picked: {chosen.label}"
                else:
                    from notifier import send_message
                    send_message("❌ Task konnte nicht zur Queue hinzugefügt werden.")
                    return "error"
            else:
                from notifier import send_message
                # response is a simple digit string, but sanitize just in case
                safe_resp = str(response).replace("*", "").replace("_", "").replace("`", "")
                send_message(f"❌ Ungültige Auswahl: {safe_resp}")
                return "invalid"
        except ValueError:
            logger.warning("usage-suggest: unexpected response: %s", response)
            return "invalid"

    # ------------------------------------------------------------------
    # Telegram response (called by listener)
    # ------------------------------------------------------------------

    def respond(self, response: str) -> bool:
        """Called by TelegramListener for /pick N or /decline.

        Returns True if a pending suggestion was active and got signaled.
        """
        with self._lock:
            if self._suggestion_event is None:
                return False
            self._suggestion_response = response
            event = self._suggestion_event
        event.set()
        return True

    def has_pending_suggestion(self) -> bool:
        with self._lock:
            return self._suggestion_event is not None

    def pending_suggestion_count(self) -> int:
        with self._lock:
            if self._suggestion_event is None:
                return 0
            return len(self._pending_suggestions)

    # ------------------------------------------------------------------
    # Limits check
    # ------------------------------------------------------------------

    def _get_claude_limits(self) -> tuple[Optional[float], int]:
        """Query Claude limits via the public get_limits() API.

        Returns (remaining_pct, resets_in_sec) or (None, 0) on failure.
        """
        try:
            from limits import get_limits
            all_limits = get_limits()
            claude = all_limits.claude
            if claude.error or not claude.available:
                return None, 0
            return claude.remaining_pct, claude.resets_in_sec
        except Exception as e:
            logger.debug("usage-suggest: limits check failed: %s", e)
            return None, 0

    # ------------------------------------------------------------------
    # Suggestion gathering
    # ------------------------------------------------------------------

    def _gather_suggestions(self) -> list[Suggestion]:
        """Collect and rank up to 3 suggestions from multiple strategies."""
        candidates: list[Suggestion] = []

        candidates.extend(self._suggest_skills())
        candidates.extend(self._suggest_git_changes())
        candidates.extend(self._suggest_failed_retries())
        candidates.extend(self._suggest_vault_tasks())

        # Sort by score descending, take top 3
        candidates.sort(key=lambda s: s.score, reverse=True)
        top = candidates[:3]

        # Assign ranks
        for i, s in enumerate(top):
            s.rank = i + 1

        return top

    def _suggest_skills(self) -> list[Suggestion]:
        """Strategy A: Vault skills that haven't run recently."""
        suggestions: list[Suggestion] = []
        try:
            from skills.discovery import discover_skills
            skills = discover_skills(vault_path=VAULT_PATH)
        except Exception:
            return suggestions

        now = datetime.now()
        for name, skill in skills.items():
            # Check memory for last run
            last_run = self._skill_last_run(name)
            if last_run and (now - last_run).days < USAGE_SUGGEST_SKILL_COOLDOWN_DAYS:
                continue

            score = 1.0
            # Weekly skills bonus on Mondays
            if now.weekday() == 0:
                score = 3.0
            # Monthly skills bonus on days 1-3
            if now.day <= 3:
                score = max(score, 2.5)

            desc = skill.description or name
            suggestions.append(Suggestion(
                rank=0,
                label=f"Skill: {name}",
                task_text=f"Führe den Skill '{name}' aus: {desc}",
                source="skill",
                score=score,
            ))

        return suggestions

    def _suggest_git_changes(self) -> list[Suggestion]:
        """Strategy B: Git repos with uncommitted changes.

        ALLOWED_CWD_ROOTS are parent directories, not git repos themselves.
        We scan one level deep for directories containing a .git folder.
        """
        suggestions: list[Suggestion] = []
        roots = ALLOWED_CWD_ROOTS or []

        repo_dirs: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            # Check if root itself is a git repo
            if (root / ".git").exists():
                repo_dirs.append(root)
                continue
            # Scan one level deep for git repos
            try:
                for child in root.iterdir():
                    if child.is_dir() and (child / ".git").exists():
                        repo_dirs.append(child)
            except OSError:
                pass

        for repo in repo_dirs:
            try:
                r = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=str(repo),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                if r.returncode != 0 or not r.stdout.strip():
                    continue
                lines = r.stdout.strip().splitlines()
                n_changes = len(lines)
                score = 0.8 + 0.05 * min(n_changes, 10)  # cap at 1.3

                suggestions.append(Suggestion(
                    rank=0,
                    label=f"Git: {repo.name} ({n_changes} Änderungen)",
                    task_text=(
                        f"Prüfe die uncommitted Changes in {repo} und erstelle "
                        f"sinnvolle Commits. cwd:{repo}"
                    ),
                    source="git",
                    score=score,
                ))
            except (OSError, subprocess.TimeoutExpired):
                pass

        return suggestions

    def _suggest_failed_retries(self) -> list[Suggestion]:
        """Strategy C: Recently failed tasks from memory."""
        suggestions: list[Suggestion] = []
        try:
            from memory import _TASK_RESULTS_DIR, _parse_memory_file
            if not _TASK_RESULTS_DIR.exists():
                return suggestions

            cutoff = datetime.now() - timedelta(days=USAGE_SUGGEST_RETRY_WINDOW_DAYS)
            for path in _TASK_RESULTS_DIR.glob("*.md"):
                mem = _parse_memory_file(path)
                if not mem:
                    continue
                if mem.get("success", True):
                    continue
                if mem["timestamp"] < cutoff:
                    continue

                task_text = mem.get("task", "")
                if not task_text:
                    continue

                suggestions.append(Suggestion(
                    rank=0,
                    label=f"Retry: {task_text[:50]}",
                    task_text=task_text,
                    source="retry",
                    score=0.6,
                ))
        except Exception as e:
            logger.debug("usage-suggest: failed retry scan error: %s", e)

        return suggestions

    # ------------------------------------------------------------------
    # Strategy D: Vault tasks (with optional LLM autonomy assessment)
    # ------------------------------------------------------------------

    # Hard-filter: roles that require physical/manual action
    _EXCLUDED_ROLES = {"#Rolle/haus", "#Rolle/Fam", "#Rolle/literal:YourOrg", "#Rolle/unternehmungen"}
    _KEPT_ROLES = {"#Rolle/arbeit", "#Rolle/ich"}
    _TAG_CONTINUE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/")

    def _suggest_vault_tasks(self) -> list[Suggestion]:
        """Strategy D: Open vault tasks assessed for AI autonomy."""
        try:
            candidates = self._scan_vault_tasks()
            if not candidates:
                return []

            # Sort by heuristic pre-score, take top 10 for LLM
            candidates.sort(key=lambda c: c[1], reverse=True)
            top10 = candidates[:10]

            # LLM autonomy assessment
            llm_scores = self._assess_autonomy(top10)

            suggestions: list[Suggestion] = []
            for i, (text, pre_score) in enumerate(top10):
                llm_score = llm_scores.get(i)
                if llm_score is not None:
                    final = pre_score * 0.3 + llm_score * 0.7
                else:
                    final = pre_score * 0.4

                # Scale to 0.5–2.5 range (pre_score max ~5, llm max 10 → raw max ~8.5)
                scaled = 0.5 + (final / 8.5) * 2.0
                scaled = max(0.5, min(2.5, scaled))

                # Only suggest tasks with reasonable autonomy potential
                if llm_score is not None and llm_score < 3:
                    continue
                # Without LLM, require minimum heuristic score
                if llm_score is None and pre_score < 1.0:
                    continue

                label = text[:60].rstrip()
                if len(text) > 60:
                    label += "…"

                suggestions.append(Suggestion(
                    rank=0,
                    label=f"Vault: {label}",
                    task_text=text,
                    source="vault",
                    score=scaled,
                ))

            return suggestions[:3]
        except Exception as e:
            logger.debug("usage-suggest: vault task scan error: %s", e)
            return []

    def _scan_vault_tasks(self) -> list[tuple[str, float]]:
        """Scan vault files for open tasks with Tasks-plugin metadata.

        Returns list of (task_text, heuristic_pre_score) tuples.
        """
        results: list[tuple[str, float]] = []
        open_task_re = re.compile(r"^- \[ \] (.+)", re.MULTILINE)

        for rel_path in USAGE_SUGGEST_VAULT_TASK_DIRS:
            full = VAULT_PATH / rel_path
            if not full.exists():
                continue

            # Collect files: single .md or directory glob
            if full.is_file():
                files = [full]
            elif full.is_dir():
                files = list(full.rglob("*.md"))
            else:
                continue

            for fpath in files:
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for m in open_task_re.finditer(content):
                    task_line = m.group(1).strip()
                    filtered = self._filter_vault_task(task_line)
                    if filtered is None:
                        continue
                    score = self._heuristic_score(task_line)
                    results.append((task_line, score))

        return results

    @staticmethod
    def _has_tag(text: str, tag: str) -> bool:
        """Check if *tag* appears as a whole word (not as substring of a longer tag).

        Tag-continuation characters are alphanumeric, ``_``, ``-``, and ``/``.
        """
        idx = 0
        cont = UsageSuggester._TAG_CONTINUE_CHARS
        while True:
            idx = text.find(tag, idx)
            if idx == -1:
                return False
            end = idx + len(tag)
            if end >= len(text) or text[end] not in cont:
                return True
            idx = end

    def _filter_vault_task(self, text: str) -> Optional[str]:
        """Apply hard filters. Returns text if it passes, None if filtered out."""
        # Must have a #Rolle/ tag
        if "#Rolle/" not in text:
            return None
        # Must be a kept role
        has_kept = any(self._has_tag(text, role) for role in self._KEPT_ROLES)
        if not has_kept:
            return None
        # Excluded roles
        if any(self._has_tag(text, role) for role in self._EXCLUDED_ROLES):
            return None
        # Blocked / habit / recurrence / too long
        if self._has_tag(text, "#wait"):
            return None
        if "#habit/" in text:  # any habit subtype
            return None
        if "\U0001f501" in text:  # 🔁
            return None
        if self._has_tag(text, "#Dauer/proj") or self._has_tag(text, "#Dauer/d"):
            return None
        return text

    def _heuristic_score(self, text: str) -> float:
        """Compute heuristic pre-score (0–5 scale) for a vault task."""
        score = 0.0

        # Urgency
        if "#Urgent/1" in text:
            score += 2.0
        elif "#Urgent/2" in text:
            score += 1.5
        elif "#Urgent/3" in text:
            score += 1.0
        elif "#Urgent/4" in text:
            score += 0.3

        # Priority emojis
        if "\u2b06\ufe0f" in text or "\u2b06" in text:  # 🔼 (or variant)
            score += 0.5
        if "\u23eb" in text:  # ⏫
            score += 1.0
        if "\U0001f53d" in text:  # 🔽
            score -= 0.5

        # Overdue date (📅 YYYY-MM-DD)
        due_match = re.search(r"\U0001f4c5\s*(\d{4}-\d{2}-\d{2})", text)
        if due_match:
            try:
                due = datetime.strptime(due_match.group(1), "%Y-%m-%d")
                if due.date() < datetime.now().date():
                    score += 1.5
            except ValueError:
                pass

        # Duration
        if "#Dauer/15min" in text:
            score += 1.0
        elif "#Dauer/30min" in text:
            score += 0.8
        elif "#Dauer/1h" in text:
            score += 0.5
        elif "#Dauer/2h" in text:
            score += 0.3

        return max(score, 0.0)

    def _assess_autonomy(self, candidates: list[tuple[str, float]]) -> dict[int, float]:
        """Use LLM to assess how well each task can be done autonomously.

        Returns {index: score_0_to_10} dict. Empty dict on failure.
        """
        if not candidates:
            return {}

        # Build numbered task list
        task_lines = []
        for i, (text, _score) in enumerate(candidates):
            # Strip metadata tags for cleaner LLM input
            clean = re.sub(r"#\S+", "", text).strip()
            clean = re.sub(r"[\U0001f4c5\u23f3\U0001f6eb\u23eb\u2b06\ufe0f\U0001f53d\U0001f501]", "", clean).strip()
            clean = re.sub(r"\d{4}-\d{2}-\d{2}", "", clean).strip()
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                task_lines.append(f"{i + 1}: {clean}")

        if not task_lines:
            return {}

        prompt = (
            "Bewerte diese Tasks auf einer Skala von 0-10: Wie gut kann ein autonomer AI-Agent "
            "(der Code schreiben, Reviews machen, Recherche betreiben, Dokumentation erstellen, "
            "und Obsidian-Vault-Pflege ausführen kann) diesen Task OHNE menschliche Interaktion "
            "erledigen?\n\n"
            "Antworte NUR mit einer Zeile pro Task im Format: N: SCORE\n"
            "Wobei N die Task-Nummer und SCORE eine Zahl 0-10 ist.\n\n"
            "Tasks:\n" + "\n".join(task_lines)
        )

        try:
            from limits import get_limits
            from dispatcher import select_provider

            limits = get_limits()
            provider = select_provider("", limits)
            if provider is None:
                logger.debug("usage-suggest: no provider for autonomy assessment")
                return {}

            previous_forced_model = None
            if provider.name == "claude":
                previous_forced_model = provider._forced_model
                provider._forced_model = USAGE_SUGGEST_CLAUDE_MODEL
            try:
                result = provider.run(prompt, cwd=None, timeout=USAGE_SUGGEST_LLM_TIMEOUT_SEC)
            finally:
                if provider.name == "claude":
                    provider._forced_model = previous_forced_model
            if not result.success or not result.output:
                return {}

            # Parse "N: SCORE" lines
            scores: dict[int, float] = {}
            for line in result.output.splitlines():
                m = re.match(r"^\s*(\d+)\s*:\s*(\d+(?:\.\d+)?)\s*$", line)
                if m:
                    idx = int(m.group(1)) - 1  # 1-based → 0-based
                    val = float(m.group(2))
                    if 0 <= idx < len(candidates) and 0 <= val <= 10:
                        scores[idx] = val
            return scores

        except Exception as e:
            logger.debug("usage-suggest: LLM autonomy assessment failed: %s", e)
            return {}

    def _skill_last_run(self, skill_name: str) -> Optional[datetime]:
        """Check memory for the last time a skill was run."""
        try:
            from memory import _TASK_RESULTS_DIR, _parse_memory_file
            if not _TASK_RESULTS_DIR.exists():
                return None

            latest: Optional[datetime] = None
            skill_lower = skill_name.lower()
            for path in _TASK_RESULTS_DIR.glob("*.md"):
                mem = _parse_memory_file(path)
                if not mem:
                    continue
                task = mem.get("task", "").lower()
                if skill_lower in task:
                    ts = mem["timestamp"]
                    if latest is None or ts > latest:
                        latest = ts
            return latest
        except Exception:
            return None


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_suggester: Optional[UsageSuggester] = None
_suggester_init_lock = threading.Lock()


def get_suggester() -> UsageSuggester:
    """Return the module-level UsageSuggester singleton (lazy init, thread-safe)."""
    global _suggester
    if _suggester is None:
        with _suggester_init_lock:
            if _suggester is None:
                _suggester = UsageSuggester()
    return _suggester
