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


class PolicyUnreadableError(ValueError):
    """policy.yaml exists but its last load failed (`PolicyEngine._load_error`).

    A ValueError, so the callers that already caught get_tool_phase()'s ValueError
    keep working. Raised by check_task() since 2026-10-08: run_once() holds every
    task on it instead of classifying with no (or stale) rules as AUTO.
    """


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
        return self.search(text) is not None

    def search(self, text: str) -> re.Match | None:
        """The match itself — the approval message quotes the text that triggered it."""
        if self._compiled is None:
            try:
                object.__setattr__(self, "_compiled", re.compile(self.pattern, re.IGNORECASE))
            except re.error:
                object.__setattr__(self, "_compiled", re.compile(re.escape(self.pattern), re.IGNORECASE))
        return self._compiled.search(text)


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


_EXCERPT_CONTEXT_CHARS = 30
_EXCERPT_MAX_CHARS = 120


def _excerpt(text: str, m: re.Match) -> str:
    """The match plus a little context, whitespace collapsed, capped in length."""
    start = max(0, m.start() - _EXCERPT_CONTEXT_CHARS)
    end = min(len(text), m.end() + _EXCERPT_CONTEXT_CHARS)
    snippet = " ".join(text[start:end].split())
    clipped_tail = end < len(text)
    if len(snippet) > _EXCERPT_MAX_CHARS:
        snippet = snippet[:_EXCERPT_MAX_CHARS - 1].rstrip()
        clipped_tail = True
    return ("…" if start > 0 else "") + snippet + ("…" if clipped_tail else "")


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


def policy_file_path(vault_path: "Path | None" = None) -> Path:
    """Absolute path of policy.yaml under *vault_path* (config.POLICY_FILE by default).

    The layout itself lives in ``config.POLICY_FILE_RELATIVE`` and is NOT
    restated here — ``config.POLICY_FILE`` already spelled it out for
    ``doctor.py``, so a second literal would be the very duplication this
    function exists to remove. This adds only the parametrised case: an engine
    built against an explicit vault (every test does that).

    Why the parametrised case is needed at all: queue_linter has to stat and
    parse the file ITSELF — on the provider side PolicyEngine reports a missing
    file, an unreadable one and a deliberately empty one all as
    ``get_allowed_providers() -> None`` = "no restriction configured", so the
    linter cannot tell corruption from a fresh install through it. (The rule side
    tells them apart since 2026-10-08 — check_task() raises on an unreadable file
    — but a missing and an empty one still look alike there.) Sharing the path is
    what keeps the two from checking different files.
    """
    if vault_path is None:
        from config import POLICY_FILE
        return Path(POLICY_FILE)
    from config import POLICY_FILE_RELATIVE
    return Path(vault_path) / POLICY_FILE_RELATIVE


class PolicyEngine:
    """Load policy.yaml, classify tasks, manage approval flow."""

    def __init__(self, vault_path: Path) -> None:
        self._vault_path = vault_path
        self._rules: list[PolicyRule] = []
        self._tool_providers: dict[str, list[str]] = {}
        self._tool_contracts: dict[str, ToolContract] = {}
        # `tool_phases:` — per-tool phase switches (dev-loop plan_approval, …).
        # Raw per-tool entries; the shape of one entry is judged on read, see
        # get_tool_phase().
        self._tool_phases: dict[str, object] = {}
        # Why the last attempt to parse an EXISTING policy.yaml failed, or None.
        # A file that does not parse at all (or whose top level is no mapping)
        # leaves rules and every section at their last good state. A section
        # parser that raises leaves the sections BEFORE it applied from the new
        # file and the rest at their last good state (see _apply_sections_locked).
        # check_task() and get_tool_phase() refuse to answer from that state while
        # the error stands (PolicyUnreadableError), because neither a rule set nor a
        # safety switch that cannot be read is "off". get_allowed_providers() and
        # get_tool_contract() still answer from it (the provider side fails open or
        # closed per provider in dispatcher._allows()).
        self._load_error: str | None = None
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

    @property
    def config_path(self) -> Path:
        """The policy.yaml THIS engine reads.

        Public because queue_linter has to inspect the same file the running
        engine loads. Asking config.VAULT_PATH instead would let the two check
        different files whenever an engine is built with an explicit vault (the
        test suite does exactly that), and a linter that reports on a file the
        runtime never reads is worse than no check at all.
        """
        return policy_file_path(self._vault_path)

    def _reload_if_changed(self) -> None:
        """Reload policy.yaml if the file has changed since last load."""
        path = self.config_path
        if not path.exists():
            # A missing file is "nothing configured" — also when the file that was
            # there before it did not load (2026-10-08). Without clearing the error
            # here, a broken policy.yaml that is then deleted kept get_tool_phase()
            # raising and, since check_task() raises on it too, held every task until
            # a restart. The rules and sections of the last good load stay as they
            # are (unchanged). mtime 0 makes a file created later load even if it
            # carries the mtime of the one that was deleted.
            with self._lock:
                if self._load_error is not None:
                    logger.info("policy: %s is gone — its load error no longer applies", path)
                self._load_error = None
                self._mtime = 0.0
            return

        try:
            mtime = path.stat().st_mtime
        except OSError:
            return

        with self._lock:
            # While the last load failed, the mtime shortcut does not apply: the mtime
            # was stored BEFORE parsing, so a file repaired with the SAME mtime
            # (OneDrive sets mtimes on sync; coarse timestamp resolution) would never
            # be read again, and check_task() would hold every task until a restart.
            # A file that is still broken costs one re-parse per call (a small yaml),
            # logs nothing new and does not re-raise (see _load_rules_locked).
            if mtime == self._mtime and self._load_error is None:
                return
            self._mtime = mtime
            self._load_rules_locked(path)

    def _load_rules_locked(self, path: Path) -> None:
        """Parse policy.yaml into PolicyRule list. Caller must hold self._lock.

        `_load_error` is cleared only once EVERY section has parsed (2026-10-06, K6).
        A section parser that raises — `stop_conditions: 1` makes
        `_parse_tool_contract` raise TypeError — records the error and re-raises, so
        the triggering call fails exactly as before. The caller has already stored the
        new mtime, so no later call re-parses; with the error cleared up front, every
        call after the first read the stale `_tool_phases` as "auto" and dev-loop
        executed without its plan approval. Sections parsed before the failing one are
        applied as before (unchanged on purpose: an all-or-nothing rewrite would drop
        rules the same edit added).

        A failure that repeats the stored `_load_error` word for word (2026-10-08) is
        the same broken file read again — _reload_if_changed retries on every call
        while the error stands. It is logged at DEBUG only and NOT re-raised: the call
        that first hit it already raised, every later one reads `_load_error`, exactly
        as before the retry existed.
        """
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            self._record_load_error_locked(path, f"{type(e).__name__}: {e}")
            return

        if data is None:
            # Empty file: deliberately nothing configured, not a read failure.
            self._load_error = None
            return
        if not isinstance(data, dict):
            self._record_load_error_locked(
                path, f"top level is {type(data).__name__}, not a mapping",
            )
            return
        try:
            self._apply_sections_locked(data)
        except Exception as e:
            if self._record_load_error_locked(path, f"{type(e).__name__}: {e}"):
                raise
            return
        self._load_error = None

        logger.debug(
            "policy: loaded %d rules, %d tool policies, %d tool contracts from %s",
            len(self._rules), len(self._tool_providers), len(self._tool_contracts), path,
        )

    def _record_load_error_locked(self, path: Path, error: str) -> bool:
        """Store *error* as `_load_error`; True when it differs from the stored one.

        Only a new error is logged as a WARNING. The same error again is the retry
        of an unchanged broken file (see _reload_if_changed) and would otherwise log
        one warning per policy lookup.
        """
        new = error != self._load_error
        self._load_error = error
        if new:
            logger.warning("policy: could not load %s: %s", path, error)
        else:
            logger.debug("policy: %s still unreadable: %s", path, error)
        return new

    def _apply_sections_locked(self, data: dict) -> None:
        """Parse and apply the sections of one policy.yaml mapping, in this order:
        rules, tool_providers, tool_contracts, tool_phases. Each is applied as soon as
        it has parsed. Caller holds the lock and owns `_load_error` (see
        _load_rules_locked)."""
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

        phases_raw = data.get("tool_phases") or {}
        if not isinstance(phases_raw, dict):
            logger.warning(
                "policy: tool_phases is not a mapping (got %s) — ignored",
                type(phases_raw).__name__,
            )
            phases_raw = {}
        self._tool_phases = {str(k): v for k, v in phases_raw.items()}

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

    def get_tool_phase(self, tool: str, key: str, default: str) -> str:
        """One switch from policy.yaml's ``tool_phases:`` section, as a string.

        Example yaml::

            tool_phases:
              dev-loop:
                plan_approval: approve

        Returns *default* when the file has no ``tool_phases:`` section, no
        entry for *tool*, or no *key* in it, and when it is missing and never
        loaded. A file that goes missing AFTER a load keeps answering from the
        phases of that last good load (2026-10-08: a missing file is "nothing new
        configured", the same rule rules, providers and contracts follow — so
        deleting the file lifts neither an ``approve`` nor a ``skip``; a restart
        or a file without the key does). A value that is there is
        returned as ``str(value)`` WITHOUT validation — the caller knows the
        allowed set (``yes`` parses to ``True`` and comes back as ``"True"``).

        Raises ValueError when the answer cannot be known: the file exists but
        could not be parsed into a mapping, or the entry for *tool* is not a
        mapping. Deliberately not *default*: for a safety switch, "unreadable"
        and "not configured" must stay distinguishable, and only the caller can
        decide which way to fail.

        Readers: dev-loop (``plan_approval``, since 2026-10-06) and review-loop
        (``verification``, ``drift_check_mode``, since 2026-10-08 — until then it
        imported a ``load_policy`` that never existed, and both keys were inert).
        """
        self._reload_if_changed()
        with self._lock:
            load_error = self._load_error
            entry = self._tool_phases.get(tool)
        if load_error is not None:
            raise PolicyUnreadableError(f"policy.yaml unreadable ({load_error})")
        if entry is None:
            return default
        if not isinstance(entry, dict):
            raise ValueError(
                f"tool_phases['{tool}'] is not a mapping (got {type(entry).__name__})"
            )
        if key not in entry:
            return default
        return str(entry[key])

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
        tier, hits = self._classify_matches(task_text, rules)
        return tier, [rule.message for rule, _ in hits], bool(hits)

    @staticmethod
    def _classify_matches(
        task_text: str, rules: list[PolicyRule],
    ) -> tuple[str, list[tuple[PolicyRule, re.Match]]]:
        """(winning tier, [(rule, match)] of that tier) — the hits that decide.

        The one place that says which rule matches count: ``_classify`` (and so
        ``check_task``) reports their messages, ``match_excerpts`` quotes their
        text. An empty list means nothing matched (tier AUTO).
        """
        hits_by_tier: dict[str, list[tuple[PolicyRule, re.Match]]] = {
            TIER_DENY: [], TIER_APPROVE: [], TIER_AUTO: [],
        }
        for rule in rules:
            m = rule.search(task_text)
            if m is not None:
                hits_by_tier[rule.tier].append((rule, m))
        for tier in _TIER_ORDER:
            if hits_by_tier[tier]:
                return tier, hits_by_tier[tier]
        return TIER_AUTO, []

    def _raise_if_unreadable(self) -> None:
        """Reload, then raise PolicyUnreadableError if the existing policy.yaml did not load.

        Checked on every call, not just on the one that reloads: the reload stores the
        new mtime before parsing, so a check tied to the reload would see the error
        once and then classify with the stale rules again (the K6 defect of
        get_tool_phase, 2026-10-06).

        The call that hits a failing section parser gets that parser's raw exception
        out of the reload; it is swallowed here once it has been recorded, so that
        call raises the same PolicyUnreadableError as every later one. run_once()
        throttles its Telegram alert per error text, and the raw TypeError followed by
        "policy.yaml unreadable (TypeError: …)" would be two alerts for one fault.

        A MISSING policy.yaml stays "nothing configured" (classified AUTO) on purpose:
        the queue (99_System/AI/agent-queue.md) lives in the same vault folder, so
        without that folder nothing runs anyway, and the missing file is already
        reported by queue_linter (`policy_missing`, WARN) and doctor
        (`check_policy_file`). Only a file that is there and cannot be read holds.
        """
        try:
            self._reload_if_changed()
        except Exception:
            with self._lock:
                recorded = self._load_error is not None
            if not recorded:
                raise
        with self._lock:
            load_error = self._load_error
        if load_error is not None:
            raise PolicyUnreadableError(
                f"policy.yaml unreadable ({load_error}) — Datei: {self.config_path}. "
                f"Bis sie wieder parst, bleibt jeder Task in der Queue."
            )

    def check_task(self, task_text: str, profile_rules: dict | None = None) -> tuple[str, list[str]]:
        """Scan task text for all rule patterns.

        Returns (highest_tier, [matching_messages]).
        Tier order: deny > approve > auto.

        If profile_rules is provided and matches the task, its verdict takes
        priority over global rules (layering: profile > global).

        Raises PolicyUnreadableError while policy.yaml exists but its last load
        failed (2026-10-08) — on EVERY call, not only on the one that reloads. Until
        then such a file classified every task with no or stale rules, i.e. as AUTO,
        `git push` included. run_once() holds the task on the exception.
        """
        self._raise_if_unreadable()

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

    def match_excerpts(
        self,
        texts: list[str],
        reasons: list[str],
        profile_rules: dict | None = None,
    ) -> dict[str, str]:
        """Map each reason to an excerpt of the text that triggered it.

        Quotes only the hits that decided the classification, per text and with the
        same layering as ``check_task``: profile rules first, and where they match a
        text, global rules are not even searched on it. Searching every rule on every
        text instead (the first version) had two faults (Codex review r1): a global
        rule sharing a message with a profile rule could quote a text the profile had
        classified AUTO, and a pathological global regex ran on texts classification
        never gave it — measured `(a+)+$` on "a"*24+"!": 1.6 s here, 0.000 s in
        check_task. The classification pass is repeated once, but no rule runs on a
        text that classification did not also run it on.

        Among the deciding hits, APPROVE outranks AUTO (``_TIER_ORDER``): the
        approval is about the hits that made it necessary, and a message shared by
        an AUTO and an APPROVE hit is quoted from the APPROVE one. Excerpt: first
        such match, ~30 characters of context on each side. Reasons no rule decided —
        e.g. the scientific-investigation bypass, which passes free-form reasons —
        are absent from the result.

        Never raises: it runs inside the approval path, where an exception holds the
        task in the queue instead of sending the request (fail-closed since
        2026-10-06; until then orchestrator.py ran the task UNAPPROVED).
        """
        try:
            wanted = {str(r) for r in reasons}
            p_rules = _parse_rules_from_dict(profile_rules) if profile_rules else []
            with self._lock:
                g_rules = list(self._rules)
            decisive: list[tuple[PolicyRule, str, re.Match]] = []
            for text in texts:
                hits = self._classify_matches(text, p_rules)[1] if p_rules else []
                if not hits:
                    hits = self._classify_matches(text, g_rules)[1]
                decisive.extend((rule, text, m) for rule, m in hits)
            decisive.sort(key=lambda hit: _TIER_ORDER.index(hit[0].tier))  # stable
            out: dict[str, str] = {}
            for rule, text, m in decisive:
                if rule.message in wanted and rule.message not in out and m.group(0).strip():
                    out[rule.message] = _excerpt(text, m)
            return out
        except Exception as exc:
            logger.debug("policy: match_excerpts failed: %s", exc)
            return {}

    def request_approval(
        self,
        task_text: str,
        reasons: list[str],
        timeout_sec: int = POLICY_APPROVAL_TIMEOUT_SEC,
        *,
        cwd: str | None = None,
        checked_texts: list[str] | None = None,
        profile_rules: dict | None = None,
    ) -> str:
        """Send Telegram approval request and block until responded.

        The keyword arguments only enrich the message (cwd, repo state, the text
        that triggered each reason). ``checked_texts`` are the texts ``check_task``
        classified — tags stripped, subtasks included — and default to the raw
        *task_text*. None of it can make the request fail.

        Returns: "approved" | "denied" | "skipped" | "timeout"
        """
        from notifier import notify_approval_required

        triggers = self.match_excerpts(
            checked_texts if checked_texts else [task_text], reasons, profile_rules,
        )

        event = threading.Event()
        with self._lock:
            self._approval_response = ""
            self._approval_event = event

        notify_approval_required(task_text, reasons, timeout_sec, cwd=cwd, triggers=triggers)
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
