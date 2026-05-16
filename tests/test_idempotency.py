"""Tests for idempotency.py — duplicate-trigger deduplication via JSONL store."""

from datetime import datetime, timedelta
from unittest.mock import patch

import json
import pytest

with patch("config._load_dotenv"):
    import idempotency


@pytest.fixture(autouse=True)
def isolated_store(tmp_path):
    """Each test gets its own JSONL store."""
    store = tmp_path / "idempotency.jsonl"
    idempotency.set_store_path(store)
    idempotency.reset_for_tests()
    yield store
    idempotency.reset_for_tests()


# ---------------------------------------------------------------------------
# compute_key
# ---------------------------------------------------------------------------

def test_compute_key_is_deterministic():
    a = idempotency.compute_key("src", "payload", "bucket")
    b = idempotency.compute_key("src", "payload", "bucket")
    assert a == b


def test_compute_key_differs_for_different_sources():
    a = idempotency.compute_key("telegram", "x", "1")
    b = idempotency.compute_key("webhook", "x", "1")
    assert a != b


def test_compute_key_differs_for_different_payloads():
    a = idempotency.compute_key("src", "payload-a", "1")
    b = idempotency.compute_key("src", "payload-b", "1")
    assert a != b


def test_compute_key_differs_for_different_buckets():
    a = idempotency.compute_key("src", "x", "1")
    b = idempotency.compute_key("src", "x", "2")
    assert a != b


def test_compute_key_handles_dict_payload_stably():
    a = idempotency.compute_key("src", {"b": 1, "a": 2}, "1")
    b = idempotency.compute_key("src", {"a": 2, "b": 1}, "1")
    assert a == b  # dict key order must not matter


def test_compute_key_int_and_str_bucket_equivalent():
    a = idempotency.compute_key("src", "p", 42)
    b = idempotency.compute_key("src", "p", "42")
    assert a == b


# ---------------------------------------------------------------------------
# check_and_record — new vs duplicate
# ---------------------------------------------------------------------------

def test_first_check_returns_true():
    assert idempotency.check_and_record("telegram", "task A", 1) is True


def test_second_check_with_same_args_returns_false():
    assert idempotency.check_and_record("telegram", "task A", 1) is True
    assert idempotency.check_and_record("telegram", "task A", 1) is False


def test_different_bucket_treated_as_new():
    assert idempotency.check_and_record("telegram", "task A", 1) is True
    assert idempotency.check_and_record("telegram", "task A", 2) is True


def test_different_payload_treated_as_new():
    assert idempotency.check_and_record("telegram", "task A", 1) is True
    assert idempotency.check_and_record("telegram", "task B", 1) is True


def test_different_source_treated_as_new():
    assert idempotency.check_and_record("telegram", "x", 1) is True
    assert idempotency.check_and_record("webhook", "x", 1) is True


# ---------------------------------------------------------------------------
# is_recorded does not mutate
# ---------------------------------------------------------------------------

def test_is_recorded_returns_false_before_record():
    assert idempotency.is_recorded("src", "p", 1) is False


def test_is_recorded_returns_true_after_record():
    idempotency.check_and_record("src", "p", 1)
    assert idempotency.is_recorded("src", "p", 1) is True


def test_is_recorded_does_not_create_entry(isolated_store):
    idempotency.is_recorded("src", "p", 1)
    assert not isolated_store.exists() or isolated_store.read_text() == ""


# ---------------------------------------------------------------------------
# Storage format
# ---------------------------------------------------------------------------

def test_store_is_valid_jsonl(isolated_store):
    idempotency.check_and_record("src", "p", 1)
    lines = isolated_store.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["source"] == "src"
    assert obj["bucket_ts"] == "1"
    assert "key" in obj and len(obj["key"]) == 64
    assert "recorded_at" in obj
    assert "payload_hash" in obj


def test_corrupt_lines_are_skipped(isolated_store):
    """A garbled JSONL line must not crash the scanner."""
    isolated_store.write_text("not-valid-json\n", encoding="utf-8")
    # Adding a fresh entry should work; the corrupt line is ignored.
    assert idempotency.check_and_record("src", "p", 1) is True


# ---------------------------------------------------------------------------
# Retention / pruning
# ---------------------------------------------------------------------------

def test_prune_old_removes_expired_entries(isolated_store):
    idempotency.check_and_record("src", "p", 1)

    # Rewrite the entry with an old timestamp
    data = json.loads(isolated_store.read_text(encoding="utf-8").strip())
    data["recorded_at"] = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
    isolated_store.write_text(json.dumps(data) + "\n", encoding="utf-8")

    removed = idempotency.prune_old(max_age_days=30)
    assert removed == 1
    assert isolated_store.read_text(encoding="utf-8") == ""


def test_prune_old_keeps_fresh_entries(isolated_store):
    idempotency.check_and_record("src", "p", 1)
    removed = idempotency.prune_old(max_age_days=30)
    assert removed == 0
    assert isolated_store.exists()


def test_prune_handles_missing_file(isolated_store):
    if isolated_store.exists():
        isolated_store.unlink()
    assert idempotency.prune_old(max_age_days=30) == 0


def test_prune_keeps_malformed_lines(isolated_store):
    """Corrupt lines are kept (we don't know if they're old or recent)."""
    isolated_store.parent.mkdir(parents=True, exist_ok=True)
    isolated_store.write_text("junk-line\n", encoding="utf-8")
    removed = idempotency.prune_old(max_age_days=30)
    assert removed == 0
    assert "junk-line" in isolated_store.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Reset helper
# ---------------------------------------------------------------------------

def test_reset_for_tests_clears_store(isolated_store):
    idempotency.check_and_record("src", "p", 1)
    assert isolated_store.exists()
    idempotency.reset_for_tests()
    assert not isolated_store.exists()


# ---------------------------------------------------------------------------
# Concurrent inserts (basic sanity — full race testing is OS-dependent)
# ---------------------------------------------------------------------------

def test_repeated_inserts_same_key_only_record_once(isolated_store):
    for _ in range(5):
        idempotency.check_and_record("src", "p", 1)
    lines = isolated_store.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
