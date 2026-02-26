"""
Execution policy engine for the AI Orchestrator.

Three-tier classification:
  AUTO    — proceed silently (default)
  APPROVE — send Telegram approval request, block until responded
  DENY    — reject task immediately

Policy config: vault/99_System/AI/policy.yaml
"""

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from config import POLICY_APPROVAL_TIMEOUT_SEC

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

TIER_AUTO = "auto"
TIER_APPROVE = "approve"
TIER_DENY = "deny"

_TIER_ORDER = [TIER_DENY, TIER_APPROVE, TIER_AUTO]


@dataclass
class PolicyRule:
    pattern: str    # regex
    message: str
    tier: str       # "auto" | "approve" | "deny"
    _compiled: re.Pattern | None = field(default=None, repr=False, compare=False)

    def matches(self, text: str) -> bool:
        if self._compiled is None:
            try:
                object.__setattr__(self, "_compiled", re.compile(self.pattern, re.IGNORECASE))
            except re.error:
                object.__setattr__(self, "_compiled", re.compile(re.escape(self.pattern), re.IGNORECASE))
        return bool(self._compiled.search(text))


def _parse_rules_from_dict(data: dict) -> list[PolicyRule]:
    """Build PolicyRule list from a raw YAML dict (same schema as policy.yaml)."""
    rules: list[PolicyRule] = []
    for pattern in data.get("auto", []):
        if isinstance(pattern, str):
            rules.append(PolicyRule(pattern=pattern, message=pattern, tier=TIER_AUTO))
    for item in data.get("approve", []):
        if isinstance(item, str):
            rules.append(PolicyRule(pattern=item, message=item, tier=TIER_APPROVE))
        elif isinstance(item, dict):
            pat = str(item.get("pattern", ""))
            msg = str(item.get("message", pat))
            if pat:
                rules.append(PolicyRule(pattern=pat, message=msg, tier=TIER_APPROVE))
    for pattern in data.get("deny", []):
        if isinstance(pattern, str):
            rules.append(PolicyRule(pattern=pattern, message=pattern, tier=TIER_DENY))
    return rules


class PolicyEngine:
    """Load policy.yaml, classify tasks, manage approval flow."""

    def __init__(self, vault_path: Path) -> None:
        self._vault_path = vault_path
        self._rules: list[PolicyRule] = []
        self._mtime: float = 0.0
        self._lock = threading.Lock()

        # Session-wide preapprovals (category → approved for this process lifetime)
        self._preapprovals: set[str] = set()

        # One pending approval slot
        self._approval_event: threading.Event | None = None
        self._approval_response: str = ""  # "approved" | "denied" | "skipped"

        self._reload_if_changed()

    # ------------------------------------------------------------------
    # Rule loading
    # ------------------------------------------------------------------

    def _reload_if_changed(self) -> None:
        """Reload policy.yaml if the file has changed since last load."""
        path = self._vault_path / "99_System" / "AI" / "policy.yaml"
        if not path.exists():
            return

        try:
            mtime = path.stat().st_mtime
        except OSError:
            return

        with self._lock:
            if mtime == self._mtime:
                return
            self._mtime = mtime

        self._load_rules(path)

    def _load_rules(self, path: Path) -> None:
        """Parse policy.yaml into PolicyRule list."""
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            logger.warning("policy: could not load %s: %s", path, e)
            return

        if not isinstance(data, dict):
            return

        rules = _parse_rules_from_dict(data)

        with self._lock:
            self._rules = rules

        logger.debug("policy: loaded %d rules from %s", len(rules), path)

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(self, task_text: str, rules: list[PolicyRule]) -> tuple[str, list[str], bool]:
        """Returns (tier, messages, had_any_match)."""
        matches_by_tier: dict[str, list[str]] = {TIER_DENY: [], TIER_APPROVE: [], TIER_AUTO: []}
        for rule in rules:
            if rule.matches(task_text):
                matches_by_tier[rule.tier].append(rule.message)
        for tier in _TIER_ORDER:
            if matches_by_tier[tier]:
                return tier, matches_by_tier[tier], True
        return TIER_AUTO, [], False

    def check_task(self, task_text: str, profile_rules: dict | None = None) -> tuple[str, list[str]]:
        """Scan task text for all rule patterns.

        Returns (highest_tier, [matching_messages]).
        Tier order: deny > approve > auto.

        If profile_rules is provided and matches the task, its verdict takes
        priority over global rules (layering: profile > global).
        """
        self._reload_if_changed()

        if profile_rules:
            p_tier, p_msgs, p_matched = self._classify(task_text, _parse_rules_from_dict(profile_rules))
            if p_matched:
                return p_tier, p_msgs

        with self._lock:
            global_rules = list(self._rules)
        g_tier, g_msgs, _ = self._classify(task_text, global_rules)
        return g_tier, g_msgs

    # ------------------------------------------------------------------
    # Session preapprovals
    # ------------------------------------------------------------------

    def is_preapproved(self, category: str) -> bool:
        return category.lower() in self._preapprovals

    def add_preapproval(self, category: str) -> None:
        self._preapprovals.add(category.lower())
        logger.info("policy: session preapproval added: %s", category)

    # ------------------------------------------------------------------
    # Approval request (blocking)
    # ------------------------------------------------------------------

    def request_approval(
        self,
        task_text: str,
        reasons: list[str],
        timeout_sec: int = POLICY_APPROVAL_TIMEOUT_SEC,
    ) -> str:
        """Send Telegram approval request and block until responded.

        Returns: "approved" | "denied" | "skipped" | "timeout"
        """
        from notifier import notify_approval_required

        event = threading.Event()
        self._approval_response = ""
        self._approval_event = event

        notify_approval_required(task_text, reasons, timeout_sec)
        logger.info("policy: approval requested for: %s", task_text[:80])

        responded = event.wait(timeout=timeout_sec)

        self._approval_event = None

        if not responded:
            logger.info("policy: approval timed out")
            return "timeout"

        result = self._approval_response
        logger.info("policy: approval response: %s", result)
        return result

    def _respond(self, response: str) -> None:
        """Called by TelegramListener commands (/approve, /deny, /skip)."""
        self._approval_response = response
        if self._approval_event is not None:
            self._approval_event.set()

    def has_pending_approval(self) -> bool:
        return self._approval_event is not None


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_engine: PolicyEngine | None = None


def get_engine() -> PolicyEngine:
    """Return the module-level PolicyEngine singleton (lazy init)."""
    global _engine
    if _engine is None:
        from config import VAULT_PATH
        _engine = PolicyEngine(vault_path=VAULT_PATH)
    return _engine
