"""Adversarial-Citation-Search with diversity check (Plan §2.3, K6).

Goal: when a Sub-Task makes a literature claim, the tool also issues a set
of *adversarial* search queries — formulations that would surface
contradicting evidence rather than confirming ones. The diversity check
(Levenshtein ≥ ``TOOL_SI_ADVERSARIAL_LEVENSHTEIN_MIN`` between any two
queries) guards against the obvious failure mode where the LLM rewrites
the same query four times with cosmetic changes.

Tool-Call-Verification (K6 explicit): we compare the queries the LLM
*claims* to have issued against the trace events actually emitted by a
``ToolTracer``. Mismatches go into the audit trail as *hints*, not hard
blocks — Plan §2.3 deliberately frames this as visibility rather than
enforcement after the v4 review made the case that strict hard blocks
drive users into bypass behaviour.

The query generator + result evaluator are framework-agnostic — the
caller decides whether to actually perform WebSearch (real provider call)
or stub it for tests via the ``search_executor`` callable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from config import TOOL_SI_ADVERSARIAL_LEVENSHTEIN_MIN

logger = logging.getLogger(__name__)


@dataclass
class SearchQuery:
    text: str
    intent: str  # e.g. "refute_hypothesis", "find_alternative_cause"


@dataclass
class SearchResult:
    query: SearchQuery
    summary: str
    tool_used: str = "WebSearch"


@dataclass
class DiversityCheck:
    pass_: bool
    failures: list[str] = field(default_factory=list)

    def as_audit_dict(self) -> dict:
        return {"diversity_pass": self.pass_, "failures": list(self.failures)}


@dataclass
class ToolCallVerification:
    pass_: bool
    claimed_count: int
    actual_count: int
    note: str = ""

    def as_audit_dict(self) -> dict:
        return {
            "tool_call_pass": self.pass_,
            "claimed_count": self.claimed_count,
            "actual_count": self.actual_count,
            "note": self.note,
        }


@dataclass
class AdversarialSearchReport:
    claim_id: str
    queries: list[SearchQuery]
    results: list[SearchResult]
    diversity: DiversityCheck
    tool_calls: ToolCallVerification
    trace_path: Path | None = None

    def overall_pass(self) -> bool:
        return self.diversity.pass_ and self.tool_calls.pass_


# ── Levenshtein (stdlib, iterative) ───────────────────────────────────────


def levenshtein_distance(a: str, b: str) -> int:
    """Classic two-row DP. O(len(a)*len(b)) time, O(min) space."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cur[j] = min(
                cur[j - 1] + 1,
                prev[j] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            )
        prev = cur
    return prev[-1]


def check_query_diversity(
    queries: Iterable[SearchQuery],
    *,
    min_distance: int | None = None,
) -> DiversityCheck:
    """Return diversity check for a query set.

    Pairs with distance < min_distance are recorded as failures (note
    contains both query snippets). The check passes iff there are at least
    ``min_distance`` queries AND no pair below the threshold.
    """
    if min_distance is None:
        min_distance = TOOL_SI_ADVERSARIAL_LEVENSHTEIN_MIN
    q_list = list(queries)
    if len(q_list) < 4:
        return DiversityCheck(
            pass_=False,
            failures=[f"only {len(q_list)} queries (min 4 expected)"],
        )
    failures: list[str] = []
    for i, qa in enumerate(q_list):
        for qb in q_list[i + 1:]:
            if levenshtein_distance(qa.text, qb.text) < min_distance:
                failures.append(
                    f"queries too similar: '{qa.text[:40]}' vs '{qb.text[:40]}' "
                    f"(distance < {min_distance})"
                )
    return DiversityCheck(pass_=not failures, failures=failures)


def verify_tool_calls(
    claimed_queries: int,
    actual_calls: int,
    tool_name: str = "WebSearch",
) -> ToolCallVerification:
    """Compare claimed query count vs. trace-emitted tool-call count.

    Always passes when ``actual_calls >= claimed_queries``. Fails (audit
    hint, not block) when fewer real tool calls were emitted than the LLM
    claims — that's the signature of a hallucinated search.
    """
    if actual_calls >= claimed_queries:
        return ToolCallVerification(
            pass_=True,
            claimed_count=claimed_queries,
            actual_count=actual_calls,
        )
    return ToolCallVerification(
        pass_=False,
        claimed_count=claimed_queries,
        actual_count=actual_calls,
        note=(
            f"LLM claims {claimed_queries} {tool_name} queries but tracer "
            f"only logged {actual_calls}"
        ),
    )


# ── Trace file writer (JSONL inside run_dir/traces/) ──────────────────────


def write_adversarial_trace(
    run_dir: Path,
    *,
    claim_id: str,
    report: AdversarialSearchReport,
) -> Path:
    """Append one JSONL line per query+result to ``traces/adversarial_search.jsonl``.

    Used by Phase 6 (decision-log) and later replay-tests to verify what
    queries were actually issued.
    """
    traces_dir = run_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    trace_path = traces_dir / "adversarial_search.jsonl"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with trace_path.open("a", encoding="utf-8") as fh:
        for query, result in zip(report.queries, report.results):
            fh.write(json.dumps({
                "ts": ts,
                "claim_id": claim_id,
                "query": query.text,
                "intent": query.intent,
                "tool": result.tool_used,
                "summary": result.summary[:500],
            }, ensure_ascii=False) + "\n")
    return trace_path


# ── Runner ────────────────────────────────────────────────────────────────


SearchExecutor = Callable[[SearchQuery], SearchResult]


def run_adversarial_search(
    queries: list[SearchQuery],
    *,
    claim_id: str,
    run_dir: Path,
    search_executor: SearchExecutor,
    actual_tool_call_count: int | None = None,
) -> AdversarialSearchReport:
    """Execute the queries via the injected executor, write the trace,
    compute diversity + tool-call checks.

    ``actual_tool_call_count`` lets the caller pass the count of trace
    events emitted by the wrapper that owns the real WebSearch invocation
    (typically a ``ToolTracer``). When ``None``, we assume one tool call
    per executor invocation (the executor itself is the real tool call).
    """
    results = [search_executor(q) for q in queries]
    diversity = check_query_diversity(queries)
    tool_calls = verify_tool_calls(
        claimed_queries=len(queries),
        actual_calls=(
            actual_tool_call_count
            if actual_tool_call_count is not None
            else len(results)
        ),
    )
    report = AdversarialSearchReport(
        claim_id=claim_id,
        queries=queries,
        results=results,
        diversity=diversity,
        tool_calls=tool_calls,
    )
    report.trace_path = write_adversarial_trace(
        run_dir, claim_id=claim_id, report=report,
    )
    return report
