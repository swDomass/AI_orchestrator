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


# ── Tool Contracts (P3) ──────────────────────────────────────────────────────

# Recognized reporting paths — used by Doctor schema validation. Unknown paths
# produce a warning, not a failure (so users can add custom ones).
_KNOWN_REPORTING_PATHS = frozenset({
    "telegram", "memory", "file",
    "telegram+memory", "telegram+file", "memory+file",
    "telegram+memory+file",
})


@dataclass(frozen=True)
class ToolContract:
    """Action budget + stop conditions + reporting destination for one tool.

    Loaded from policy.yaml's `tool_contracts:` section. Fields default to None
    so callers can fall back to existing config.py constants when a contract
    omits the field — supports the staged migration of tools off of constants.

    Example yaml:
        tool_contracts:
          review-loop:
            budget:
              max_iterations: 20
              max_runtime_sec: 3600
            stop_conditions: [all_findings_resolved, infinite_loop]
            reporting_path: telegram+file
    """
    tool_name: str
    max_iterations: int | None = None
    max_runtime_sec: int | None = None
    max_files_touched: int | None = None
    stop_conditions: tuple[str, ...] = ()
    reporting_path: str = "telegram+memory"


def _parse_tool_contract(name: str, raw: dict) -> ToolContract:
    """Build a ToolContract from one yaml entry. Unknown keys are ignored."""
    budget = raw.get("budget") or {}
    if not isinstance(budget, dict):
        budget = {}

    def _int_or_none(value) -> int | None:
        if value is None:
            return None
        try:
            n = int(value)
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    stop_raw = raw.get("stop_conditions") or ()
    if isinstance(stop_raw, str):
        stop_raw = (stop_raw,)
    stop_conditions = tuple(str(s) for s in stop_raw if s)

    reporting = raw.get("reporting_path") or "telegram+memory"

    return ToolContract(
        tool_name=name,
        max_iterations=_int_or_none(budget.get("max_iterations")),
        max_runtime_sec=_int_or_none(budget.get("max_runtime_sec")),
        max_files_touched=_int_or_none(budget.get("max_files_touched")),
        stop_conditions=stop_conditions,
        reporting_path=str(reporting),
    )

_TIER_ORDER = [TIER_DENY, TIER_APPROVE, TIER_AUTO]
_PREAPPROVAL_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*", re.IGNORECASE)
_PREAPPROVAL_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that",
    "und", "die", "der", "das", "mit", "von", "auf",
    "to", "into", "onto", "remote", "package", "command", "action",
}


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


def _coerce_provider_list(raw) -> list[str] | None:
    """Normalize one `tool_providers:` entry into a provider-name list, or None.

    Nothing validates that section's shape when policy.yaml loads, and the file is
    hand-edited, so the entry is whatever YAML produced. A plain ``list(raw)`` —
    what this used to be — turns the two most likely mistakes into silent nonsense:
    the scalar form ``dev-loop: claude`` becomes ``['c','l','a','u','d','e']``
    (matching no provider, so the tool is barred from everything), and a mapping
    becomes a list of its keys. Both then read downstream as a deliberate,
    perfectly well-formed allow-list.

    Returns None for anything unusable, which callers read as "no restriction
    configured" rather than "nothing allowed" — see dispatcher._allows() for the
    asymmetric fail-open/fail-closed split that applies from there.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        name = raw.strip()
        return [name] if name else None
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return None
    names = [p.strip() for p in raw if isinstance(p, str) and p.strip()]
    return names or None


def _freeze_policy_data(data):
    """Convert policy dict/list structures into a hashable, content-based cache key."""
    if isinstance(data, dict):
        return tuple(sorted((str(k), _freeze_policy_data(v)) for k, v in data.items()))
    if isinstance(data, (list, tuple)):
        return tuple(_freeze_policy_data(v) for v in data)
    if isinstance(data, (str, int, float, bool, type(None))):
        return data
    return repr(data)


def _preapproval_tokens(text: str) -> set[str]:
    """Extract normalized category tokens from a reason/message string."""
    tokens = {
        t.lower()
        for t in _PREAPPROVAL_TOKEN_RE.findall(str(text))
        if len(t) >= 3
    }
    return {t for t in tokens if t not in _PREAPPROVAL_STOPWORDS}


def reason_matches_preapproval(reason: str, category: str) -> bool:
    """Return True if a user preapproval category matches a policy reason string."""
    reason_norm = str(reason).strip().lower()
    cat_norm = str(category).strip().lower()
    if not reason_norm or not cat_norm:
        return False
    if reason_norm == cat_norm:
        return True
    return cat_norm in _preapproval_tokens(reason_norm)


class PolicyEngine:
    """Load policy.yaml, classify tasks, manage approval flow."""

    def __init__(self, vault_path: Path) -> None:
        self._vault_path = vault_path
        self._rules: list[PolicyRule] = []
        self._tool_providers: dict[str, list[str]] = {}
        self._tool_contracts: dict[str, ToolContract] = {}
        self._mtime: float = 0.0
        self._lock = threading.Lock()

        # Cache for parsed profile rules keyed by content (not object id).
        self._profile_cache: dict[tuple, list[PolicyRule]] = {}

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
            self._load_rules_locked(path)

    def _load_rules_locked(self, path: Path) -> None:
        """Parse policy.yaml into PolicyRule list. Caller must hold self._lock."""
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            logger.warning("policy: could not load %s: %s", path, e)
            return

        if not isinstance(data, dict):
            return

        self._rules = _parse_rules_from_dict(data)

        providers_raw = data.get("tool_providers") or {}
        if not isinstance(providers_raw, dict):
            logger.warning(
                "policy: tool_providers is not a mapping (got %s) — ignored",
                type(providers_raw).__name__,
            )
            providers_raw = {}
        self._tool_providers = providers_raw

        contracts_raw = data.get("tool_contracts") or {}
        contracts: dict[str, ToolContract] = {}
        if isinstance(contracts_raw, dict):
            for tool_name, entry in contracts_raw.items():
                if isinstance(entry, dict):
                    contracts[str(tool_name)] = _parse_tool_contract(str(tool_name), entry)
                else:
                    logger.warning(
                        "policy: tool_contracts['%s'] is not a mapping (got %s) — ignored",
                        tool_name, type(entry).__name__,
                    )
        self._tool_contracts = contracts

        logger.debug(
            "policy: loaded %d rules, %d tool policies, %d tool contracts from %s",
            len(self._rules), len(self._tool_providers), len(self._tool_contracts), path,
        )

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def get_tool_contract(self, tool_name: str) -> ToolContract:
        """Return the ToolContract for `tool_name`.

        Resolution order:
        1. Specific tool entry in `tool_contracts:` (if defined)
        2. `default` entry in `tool_contracts:` (if defined) — with tool_name rewritten
        3. Empty/default ToolContract (callers fall back to config.py constants)

        Never returns None — callers can rely on a contract object always being
        present and check individual fields (max_iterations is None, etc.) to
        decide whether to use the contract value or their config default.
        """
        self._reload_if_changed()
        with self._lock:
            contract = self._tool_contracts.get(tool_name)
            if contract is None:
                default = self._tool_contracts.get("default")
                if default is not None:
                    # Rewrite tool_name so callers see the actual name they asked for.
                    contract = ToolContract(
                        tool_name=tool_name,
                        max_iterations=default.max_iterations,
                        max_runtime_sec=default.max_runtime_sec,
                        max_files_touched=default.max_files_touched,
                        stop_conditions=default.stop_conditions,
                        reporting_path=default.reporting_path,
                    )
        if contract is None:
            return ToolContract(tool_name=tool_name)
        return contract

    def list_tool_contracts(self) -> dict[str, ToolContract]:
        """Snapshot of currently-loaded contracts (used by --doctor)."""
        self._reload_if_changed()
        with self._lock:
            return dict(self._tool_contracts)

    def get_allowed_providers(self, tool_name: str | None = None) -> list[str] | None:
        """Return the list of allowed providers for a tool, or None if no restriction.

        Resolution order:
        1. Specific tool entry in tool_providers (e.g. 'review-loop')
        2. 'default' entry in tool_providers
        3. None (all providers allowed)
        """
        self._reload_if_changed()
        with self._lock:
            if not self._tool_providers:
                return None

            if tool_name and tool_name in self._tool_providers:
                return _coerce_provider_list(self._tool_providers[tool_name])

            if "default" in self._tool_providers:
                return _coerce_provider_list(self._tool_providers["default"])

        return None

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
            cache_key = _freeze_policy_data(profile_rules)
            with self._lock:
                p_rules = self._profile_cache.get(cache_key)

            if p_rules is None:
                p_rules = _parse_rules_from_dict(profile_rules)
                with self._lock:
                    # Clear cache occasionally to prevent memory leak (crude)
                    if len(self._profile_cache) > 50:
                        self._profile_cache.clear()
                    self._profile_cache[cache_key] = p_rules

            p_tier, p_msgs, p_matched = self._classify(task_text, p_rules)
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
        category_norm = str(category).strip().lower()
        if not category_norm:
            return False
        with self._lock:
            preapprovals = set(self._preapprovals)
        if category_norm in preapprovals:
            return True
        return any(reason_matches_preapproval(category_norm, p) for p in preapprovals)

    def add_preapproval(self, category: str) -> None:
        category_norm = str(category).strip().lower()
        if not category_norm:
            return
        with self._lock:
            self._preapprovals.add(category_norm)
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
        with self._lock:
            self._approval_response = ""
            self._approval_event = event

        notify_approval_required(task_text, reasons, timeout_sec)
        logger.info("policy: approval requested for: %s", task_text[:80])

        responded = event.wait(timeout=timeout_sec)

        with self._lock:
            self._approval_event = None
            result = self._approval_response

        if not responded:
            logger.info("policy: approval timed out")
            return "timeout"

        logger.info("policy: approval response: %s", result)
        return result

    def _respond(self, response: str) -> None:
        """Called by TelegramListener commands (/approve, /deny, /skip)."""
        with self._lock:
            self._approval_response = response
            event = self._approval_event
        if event is not None:
            event.set()

    def has_pending_approval(self) -> bool:
        with self._lock:
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
