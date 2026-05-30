"""Tests for taxonomy.py — failure classifier."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import replay
import taxonomy
from taxonomy import (
    CAT_APPROVAL,
    CAT_CAPACITY,
    CAT_CWD,
    CAT_DEP,
    CAT_HANG,
    CAT_POLICY,
    CAT_PROFILE,
    CAT_QUEUE,
    CAT_RATE_LIMIT,
    CAT_REFUSAL,
    CAT_RUNTIME,
    CAT_TEST,
    CAT_TIMEOUT,
    CAT_TOOL_INTERNAL,
    CAT_UNKNOWN,
    CAT_UNREACHABLE,
    classify,
    counts_by_category,
    failures_by_day,
)


def _rec(*, exit_status="error", error_code=None, task_text="t", tool="",
         ts_start: str = "2026-05-18T10:00:00") -> dict:
    return {
        "exit_status": exit_status,
        "error_code": error_code,
        "task_text": task_text,
        "tool": tool,
        "ts_start": ts_start,
    }


def test_ok_status_returns_ok():
    assert classify(_rec(exit_status="ok")) == "ok"


def test_blocked_returns_dep_unsatisfied():
    assert classify(_rec(exit_status="blocked")) == CAT_DEP


def test_error_code_rate_limit():
    assert classify(_rec(error_code="rate_limit")) == CAT_RATE_LIMIT


def test_error_code_timeout():
    assert classify(_rec(error_code="timeout")) == CAT_TIMEOUT


def test_error_code_policy_denied():
    assert classify(_rec(error_code="policy_denied")) == CAT_POLICY


def test_error_code_profile_denied():
    assert classify(_rec(error_code="profile_denied")) == CAT_PROFILE


def test_error_code_cwd_invalid():
    assert classify(_rec(error_code="cwd_invalid")) == CAT_CWD


def test_error_code_approval_variants():
    assert classify(_rec(error_code="approval_denied")) == CAT_APPROVAL
    assert classify(_rec(error_code="approval_timeout")) == CAT_APPROVAL
    assert classify(_rec(error_code="approval_skipped")) == CAT_APPROVAL


def test_error_code_capacity_exhausted():
    assert classify(_rec(error_code="capacity_exhausted")) == CAT_CAPACITY


def test_error_code_queue_update_failed():
    assert classify(_rec(error_code="queue_update_failed")) == CAT_QUEUE


def test_error_code_unreachable():
    assert classify(_rec(error_code="unreachable")) == CAT_UNREACHABLE
    assert classify(_rec(error_code="provider_unreachable")) == CAT_UNREACHABLE


def test_devloop_no_code_implies_test_failure():
    rec = _rec(tool="dev-loop", exit_status="error", error_code=None)
    assert classify(rec) == CAT_TEST


def test_keyword_rate_limit():
    rec = _rec(error_code=None, task_text="Got 429 from server, please wait")
    assert classify(rec) == CAT_RATE_LIMIT


def test_keyword_timeout():
    rec = _rec(error_code=None, task_text="task timed out after 600s")
    assert classify(rec) == CAT_TIMEOUT


def test_keyword_traceback_is_tool_internal():
    rec = _rec(error_code=None, task_text="Traceback (most recent call last)")
    assert classify(rec) == CAT_TOOL_INTERNAL


def test_keyword_refusal():
    rec = _rec(error_code=None, task_text="I cannot help with that request")
    assert classify(rec) == CAT_REFUSAL


def test_keyword_auth_401():
    rec = _rec(error_code=None, task_text="server returned 401")
    assert classify(rec) == "auth_error"


def test_unknown_fallback():
    rec = _rec(error_code="weirdness", task_text="something obscure")
    assert classify(rec) == CAT_UNKNOWN


def test_counts_by_category_excludes_ok_by_default():
    records = [
        _rec(exit_status="ok"),
        _rec(error_code="rate_limit"),
        _rec(error_code="rate_limit"),
        _rec(error_code="timeout"),
    ]
    counts = counts_by_category(records)
    assert counts == {CAT_RATE_LIMIT: 2, CAT_TIMEOUT: 1}


def test_counts_by_category_include_ok():
    records = [
        _rec(exit_status="ok"),
        _rec(error_code="timeout"),
    ]
    counts = counts_by_category(records, include_ok=True)
    assert counts == {"ok": 1, CAT_TIMEOUT: 1}


def test_failures_by_day_buckets_correctly():
    now = datetime.now()
    today_ts = now.strftime("%Y-%m-%dT%H:%M:%S")
    yesterday_ts = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")

    records = [
        _rec(error_code="rate_limit", ts_start=today_ts),
        _rec(error_code="timeout", ts_start=today_ts),
        _rec(error_code="timeout", ts_start=yesterday_ts),
        _rec(exit_status="ok", ts_start=today_ts),  # ignored
    ]
    result = failures_by_day(records, days=3)

    today_key = now.date().isoformat()
    yesterday_key = (now - timedelta(days=1)).date().isoformat()
    assert result[today_key].get(CAT_RATE_LIMIT) == 1
    assert result[today_key].get(CAT_TIMEOUT) == 1
    assert result[yesterday_key].get(CAT_TIMEOUT) == 1


def test_backfill_log_adds_category_key(tmp_path):
    replay.set_store_path(tmp_path / "runs.jsonl")
    try:
        rec_dict = {"exit_status": "error", "error_code": "timeout",
                    "task_text": "x", "ts_start": "2026-05-18T10:00:00"}
        from datetime import datetime as _dt
        replay.append_run(replay.build_record(
            run_id="r1",
            ts_start=_dt(2026, 5, 18, 10, 0),
            task_text="x",
            exit_status="error",
            error_code="timeout",
        ))
        enriched = taxonomy.backfill_log()
        assert len(enriched) == 1
        assert enriched[0]["category"] == CAT_TIMEOUT
    finally:
        replay.reset_for_tests()


def test_error_code_case_insensitive():
    assert classify(_rec(error_code="RATE_LIMIT")) == CAT_RATE_LIMIT
    assert classify(_rec(error_code="TimeOut")) == CAT_TIMEOUT


def test_empty_error_code_and_no_keywords_returns_unknown():
    assert classify(_rec(error_code=None, task_text="")) == CAT_UNKNOWN


def test_error_code_hang_and_blocked():
    assert classify(_rec(error_code="hang")) == CAT_HANG
    assert classify(_rec(error_code="hang_blocked")) == CAT_HANG


def test_error_code_tool_runtime_exceeded():
    assert classify(_rec(error_code="tool_runtime_exceeded")) == CAT_RUNTIME


# ---------------------------------------------------------------------------
# Invariant: every error code literal emitted by the orchestrator / tools must
# be mapped in _ERROR_CODE_MAP. The module docstring promises this stays "in
# sync with the codes actually emitted" — this test enforces it for real
# instead of trusting a hand-maintained comment.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Codes that are intentionally free-form / routed through keyword scan rather
# than the direct map (provider error strings, dynamic messages, etc.).
_NON_MAP_CODES = {
    "",                       # empty / cleared
    "session_missing",        # provider-internal, never reaches replay error_code
    "pipeline_complete",      # success marker on a success=True result (not a failure)
}


def _emitted_error_codes() -> set[str]:
    """Scan orchestrator.py + tools for _span.error/_span.retry/error_code= literals."""
    sources = [_REPO_ROOT / "orchestrator.py"]
    sources.extend((_REPO_ROOT / "tools").glob("*.py"))
    patterns = [
        re.compile(r"_span\.(?:error|retry)\(\s*[\"']([a-z_]+)[\"']"),
        re.compile(r"error_code\s*=\s*[\"']([a-z_]+)[\"']"),
    ]
    found: set[str] = set()
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for pat in patterns:
            found.update(pat.findall(text))
    return found - _NON_MAP_CODES


def test_emitted_error_codes_are_all_mapped():
    emitted = _emitted_error_codes()
    # Codes routed via record.error_code that may legitimately be free-form
    # provider strings fall through to the keyword scan; only the structured
    # literals emitted in source must be in the map.
    unmapped = {
        code for code in emitted
        if code not in taxonomy._ERROR_CODE_MAP
    }
    assert not unmapped, (
        f"error codes emitted in source but missing from _ERROR_CODE_MAP: "
        f"{sorted(unmapped)}"
    )


def test_all_mapped_categories_are_in_all_categories():
    for category in taxonomy._ERROR_CODE_MAP.values():
        assert category in taxonomy.ALL_CATEGORIES
