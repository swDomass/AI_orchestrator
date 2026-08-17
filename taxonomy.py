"""
Failure taxonomy — stable categories for replay records with non-OK exit_status.

Sits on top of ``replay.py`` (#30). Consumes ``runs.jsonl`` records and returns
a category string so retries, analytics, and dashboards can reason about
*why* tasks fail.

Categories
----------

* ``rate_limit``           — provider quota hit
* ``timeout``              — task exceeded its time budget
* ``hang``                 — process idle-killed (no output, no running tool)
* ``tool_runtime_exceeded`` — tool total-runtime deadline reached
* ``auth_error``           — credentials missing / expired
* ``provider_unreachable`` — CLI not found, all providers exhausted, network
* ``model_refusal``        — provider returned a refusal/safety message
* ``tool_internal_error``  — exception inside a tool implementation
* ``cwd_invalid``          — path outside roots or non-existent
* ``policy_denied``        — PolicyEngine rejected the task
* ``profile_denied``       — Profile allowed/denied skill blocked execution
* ``approval_denied``      — Telegram approval rejected / timed-out / skipped
* ``capacity_exhausted``   — usage budget consumed mid-tool
* ``dep_unsatisfied``      — #needs: dependency never resolved
* ``test_failure``         — dev-loop terminal state with failing tests
* ``queue_update_failed``  — atomic queue mutation failed
* ``paused``               — task interrupted by /pause
* ``stdin_incomplete``     — prompt not fully delivered to the CLI over stdin
* ``unknown``              — fallback when no rule matches

Usage::

    from taxonomy import classify
    cat = classify(record)  # record is a dict from replay.read_runs()

CLI: ``python taxonomy.py`` prints failures-by-category for the last 30 days.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable

import replay

CAT_RATE_LIMIT = "rate_limit"
CAT_TIMEOUT = "timeout"
CAT_HANG = "hang"
CAT_RUNTIME = "tool_runtime_exceeded"
CAT_AUTH = "auth_error"
CAT_UNREACHABLE = "provider_unreachable"
CAT_REFUSAL = "model_refusal"
CAT_TOOL_INTERNAL = "tool_internal_error"
CAT_CWD = "cwd_invalid"
CAT_POLICY = "policy_denied"
CAT_PROFILE = "profile_denied"
CAT_APPROVAL = "approval_denied"
CAT_CAPACITY = "capacity_exhausted"
CAT_DEP = "dep_unsatisfied"
CAT_TEST = "test_failure"
CAT_QUEUE = "queue_update_failed"
CAT_PAUSED = "paused"
# Prompt did not fully reach the CLI over stdin → the run answered a truncated
# prompt. Own category because it is a LOCAL transport fault, not a provider
# health problem: it must never be lumped in with rate_limit/unreachable.
CAT_STDIN = "stdin_incomplete"
# The run itself was clean — exit 0, well-formed result event — but the task's
# `#verify:` outcome check says the promised artefact is not there. Own category
# because it is neither a provider fault nor a transport fault: the machinery worked
# and the WORK did not happen. Lumping it into tool_internal_error would hide exactly
# the class of silent failure the check exists to surface.
CAT_VERIFY = "verify_failed"
CAT_UNKNOWN = "unknown"

ALL_CATEGORIES: tuple[str, ...] = (
    CAT_RATE_LIMIT, CAT_TIMEOUT, CAT_HANG, CAT_RUNTIME, CAT_AUTH,
    CAT_UNREACHABLE, CAT_REFUSAL, CAT_TOOL_INTERNAL, CAT_CWD, CAT_POLICY,
    CAT_PROFILE, CAT_APPROVAL, CAT_CAPACITY, CAT_DEP, CAT_TEST, CAT_QUEUE,
    CAT_PAUSED, CAT_STDIN, CAT_VERIFY, CAT_UNKNOWN,
)

# error_code → category. The orchestrator emits these codes (see _RunSpan in
# orchestrator.py); the linter test for taxonomy verifies the mapping stays
# in sync with the codes actually emitted.
_ERROR_CODE_MAP: dict[str, str] = {
    "rate_limit":             CAT_RATE_LIMIT,
    "stdin_incomplete":       CAT_STDIN,
    "verify_failed":          CAT_VERIFY,
    "timeout":                CAT_TIMEOUT,
    # Idle-kill (process froze, no running tool) → its own category so the
    # hang vs. hard-timeout vs. runtime-deadline failure modes stay
    # differentiable in analytics/dashboards.
    "hang":                   CAT_HANG,
    "hang_blocked":           CAT_HANG,
    "tool_runtime_exceeded":  CAT_RUNTIME,
    "auth_error":             CAT_AUTH,
    "auth":                   CAT_AUTH,
    "unreachable":            CAT_UNREACHABLE,
    "provider_unreachable":   CAT_UNREACHABLE,
    "no_provider":            CAT_UNREACHABLE,
    "model_refusal":          CAT_REFUSAL,
    "refusal":                CAT_REFUSAL,
    "tool_internal_error":    CAT_TOOL_INTERNAL,
    "internal_error":         CAT_TOOL_INTERNAL,
    "parallel_subtask_failure": CAT_TOOL_INTERNAL,
    "executor_exception":     CAT_TOOL_INTERNAL,
    "read_only_violation":    CAT_TOOL_INTERNAL,
    # Tool precondition / input failures (empty topic, no ideas/repos, missing
    # cwd) — the tool itself could not proceed, not a provider/capacity issue.
    "empty_topic":            CAT_TOOL_INTERNAL,
    "no_ideas":               CAT_TOOL_INTERNAL,
    "no_repos":               CAT_TOOL_INTERNAL,
    "missing_cwd":            CAT_TOOL_INTERNAL,
    "gh_unavailable":         CAT_UNREACHABLE,
    "cwd_invalid":            CAT_CWD,
    "invalid_cwd":            CAT_CWD,
    "policy_denied":          CAT_POLICY,
    # A #provider tag the tool_providers policy bars — terminal, and deliberately
    # NOT rerouted to another provider.
    "provider_not_allowed":   CAT_POLICY,
    # Same policy layer, no tag involved: the allow-list and the routable chain
    # do not intersect, so nothing can run. Terminal too — unlike an exhausted
    # quota, waiting cannot clear it.
    "no_provider_allowed":    CAT_POLICY,
    "profile_denied":         CAT_PROFILE,
    "approval_denied":        CAT_APPROVAL,
    "approval_timeout":       CAT_APPROVAL,
    "approval_skipped":       CAT_APPROVAL,
    "capacity_exhausted":     CAT_CAPACITY,
    "dep_unsatisfied":        CAT_DEP,
    "test_failure":           CAT_TEST,
    "queue_update_failed":    CAT_QUEUE,
    "paused":                 CAT_PAUSED,
}

# stderr / output keyword heuristics (lowercase contains-match). Order matters:
# more specific patterns first. Used as fallback when error_code is missing.
_KEYWORD_RULES: list[tuple[str, str]] = [
    ("429",                       CAT_RATE_LIMIT),
    ("rate limit",                CAT_RATE_LIMIT),
    ("quota exceeded",            CAT_RATE_LIMIT),
    ("too many requests",         CAT_RATE_LIMIT),
    ("timed out",                 CAT_TIMEOUT),
    ("timeout",                   CAT_TIMEOUT),
    ("unauthorized",              CAT_AUTH),
    ("401",                       CAT_AUTH),
    ("403",                       CAT_AUTH),
    ("invalid api key",           CAT_AUTH),
    ("authentication failed",     CAT_AUTH),
    ("connection refused",        CAT_UNREACHABLE),
    ("connection reset",          CAT_UNREACHABLE),
    ("name or service not known", CAT_UNREACHABLE),
    ("network unreachable",       CAT_UNREACHABLE),
    ("command not found",         CAT_UNREACHABLE),
    ("i cannot",                  CAT_REFUSAL),
    ("i can't",                   CAT_REFUSAL),
    ("i'm not able to",           CAT_REFUSAL),
    ("traceback",                 CAT_TOOL_INTERNAL),
    ("exception",                 CAT_TOOL_INTERNAL),
    ("failed:",                   CAT_TOOL_INTERNAL),
    ("test failed",               CAT_TEST),
    ("tests failed",              CAT_TEST),
    ("pytest",                    CAT_TEST),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify(record: dict) -> str:
    """Return the failure category for a single replay record.

    Successful records (``exit_status == "ok"``) are returned as ``"ok"`` so
    callers can use this in mixed pipelines.

    Decision order:
      1. exit_status == "ok"           → "ok"
      2. exit_status == "blocked"      → dep_unsatisfied
      3. error_code in _ERROR_CODE_MAP → direct category
      4. tool-specific signals (test_failure detection for dev-loop)
      5. keyword scan on error_code + task_text
      6. fallback: unknown
    """
    exit_status = (record.get("exit_status") or "").lower()
    if exit_status == replay.EXIT_OK:
        return "ok"
    if exit_status == replay.EXIT_BLOCKED:
        return CAT_DEP

    error_code = (record.get("error_code") or "").strip()
    if error_code and error_code.lower() in _ERROR_CODE_MAP:
        return _ERROR_CODE_MAP[error_code.lower()]

    # Tool-specific signal: dev-loop finalized with no error_code → likely test failure
    tool = (record.get("tool") or "").lower()
    if tool == "dev-loop" and exit_status == replay.EXIT_ERROR and not error_code:
        return CAT_TEST

    # Keyword scan as last resort. Look at error_code (free-form) + task_text.
    haystack = " ".join([
        error_code.lower(),
        (record.get("task_text") or "").lower(),
    ])
    for keyword, category in _KEYWORD_RULES:
        if keyword in haystack:
            return category

    return CAT_UNKNOWN


def classify_many(records: Iterable[dict]) -> list[tuple[dict, str]]:
    """Apply ``classify`` to each record. Returns (record, category) pairs."""
    return [(r, classify(r)) for r in records]


def counts_by_category(
    records: Iterable[dict],
    *,
    include_ok: bool = False,
) -> dict[str, int]:
    """Aggregate category counts.

    Args:
        records: replay records (dicts).
        include_ok: when False (default), ``ok`` is excluded from the result.
    """
    counter: Counter[str] = Counter()
    for rec in records:
        cat = classify(rec)
        if cat == "ok" and not include_ok:
            continue
        counter[cat] += 1
    return dict(counter)


def failures_by_day(
    records: Iterable[dict],
    *,
    days: int = 30,
) -> dict[str, dict[str, int]]:
    """Per-day failure counts grouped by category. Useful for dashboard tiles.

    Returns ``{ "2026-05-18": {"rate_limit": 2, "timeout": 1}, ... }``.
    Zero-fills days with no failures.
    """
    today = datetime.now().date()
    output: dict[str, dict[str, int]] = {}
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        output[d.isoformat()] = {}

    for rec in records:
        try:
            ts = datetime.strptime(rec.get("ts_start", ""), "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            continue
        day_key = ts.date().isoformat()
        if day_key not in output:
            continue
        cat = classify(rec)
        if cat == "ok":
            continue
        output[day_key][cat] = output[day_key].get(cat, 0) + 1
    return output


def backfill_log(*, since: datetime | None = None) -> list[dict]:
    """Read replay records (incl. archive) and return them with a ``category`` key.

    Use for one-shot classification of historical runs. Does NOT mutate the
    JSONL — taxonomy is computed at read-time so re-runs benefit from updated
    rules immediately.
    """
    out: list[dict] = []
    for rec in replay.read_runs(since=since, include_archive=True):
        enriched = dict(rec)
        enriched["category"] = classify(rec)
        out.append(enriched)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    parser = argparse.ArgumentParser(description="Classify replay records by failure category.")
    parser.add_argument("--days", type=int, default=30,
                        help="Look back N days (default: 30).")
    parser.add_argument("--include-ok", action="store_true",
                        help="Include successful runs in the count.")
    parser.add_argument("--include-archive", action="store_true",
                        help="Include archived JSONL files.")
    args = parser.parse_args()

    since = datetime.now() - timedelta(days=args.days)
    records = replay.read_runs(since=since, include_archive=args.include_archive)

    if not records:
        print(f"No records in the last {args.days} day(s).")
        return 0

    counts = counts_by_category(records, include_ok=args.include_ok)

    print(f"Failure taxonomy over the last {args.days} day(s) ({len(records)} record(s)):\n")
    total = sum(counts.values())
    if total == 0:
        print("  No failures classified — all runs were successful.")
        return 0

    pad = max(len(c) for c in counts)
    for cat, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
        pct = round(n / total * 100, 1)
        print(f"  {cat:<{pad}}  {n:>5}   {pct:>5}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
