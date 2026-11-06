"""Folder exclusion for the local subscription estimate (``ORCH_QUOTA_EXCLUDE_DIRS``).

Sessions in excluded folders run under another account but write their transcripts into
the shared projects dir. ``claude_billing.load_subscription_entries`` must not see them;
without exclusion it is exactly the old call (default data path).
"""

import datetime as dt
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import claude_billing
import config
import limits
import quota_calibration

ROOT = Path(__file__).resolve().parent.parent
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _e(mid, rid, minute):
    return SimpleNamespace(message_id=mid, request_id=rid, timestamp=T0 + dt.timedelta(minutes=minute), tokens=1000)


@pytest.fixture
def fake_monitor(monkeypatch):
    """Importable claude_monitor stand-in; ``calls`` records every load_usage_entries call."""
    calls: list[dict] = []
    by_folder: dict[str, list] = {}
    default: list = []

    def load_usage_entries(**kw):
        calls.append(kw)
        if "data_path" in kw:
            return list(by_folder.get(Path(kw["data_path"]).name, [])), None
        return list(default), None

    models = ModuleType("claude_monitor.core.models")
    models.CostMode = SimpleNamespace(AUTO="auto")
    reader = ModuleType("claude_monitor.data.reader")
    reader.load_usage_entries = load_usage_entries
    analyzer = ModuleType("claude_monitor.data.analyzer")
    mods = {"claude_monitor.core.models": models, "claude_monitor.data.reader": reader,
            "claude_monitor.data.analyzer": analyzer}
    for name in ("claude_monitor", "claude_monitor.core", "claude_monitor.data"):
        pkg = ModuleType(name)
        pkg.__path__ = []
        mods[name] = pkg
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return SimpleNamespace(calls=calls, by_folder=by_folder, default=default, reader=reader, analyzer=analyzer)


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    for name in (
        "D--programmieren-LE-Python-Bremsen-Tool",  # sub-folder of an excluded path
        "D--programmieren-LE-Python",               # the excluded path itself
        "d--programmieren-le-python-klein",         # case-insensitive
        "D--programmieren-LE-Python-alt",           # documented limit: slug prefix collision
        "D--programmieren-LE-Python2",              # no "-" after the slug -> stays
        "D--programmieren-privat-python-x",
        "D--OneDrive---Linked-Engineering-GmbH-Kunde",
        "D--OneDrive---White-Lady-e-U",
    ):
        (root / name).mkdir(parents=True)
        (root / name / "s.jsonl").write_text("", encoding="utf-8")  # folders without a transcript are skipped
    (root / "stray-file.jsonl").write_text("", encoding="utf-8")  # files are skipped
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", root)
    return root


@pytest.fixture
def exclude(monkeypatch):
    monkeypatch.setattr(
        config, "QUOTA_EXCLUDE_DIRS",
        [r"D:\programmieren\LE_Python", r"D:\OneDrive - Linked Engineering GmbH"],
    )


def _loaded(fake_monitor):
    return sorted(Path(c["data_path"]).name for c in fake_monitor.calls)


def test_project_slug_matches_claude_code_folder_name():
    assert claude_billing._project_slug(r"D:\programmieren\LE_Python\Bremsen_Tool") == (
        "D--programmieren-LE-Python-Bremsen-Tool"
    )
    assert claude_billing._project_slug(r"D:\OneDrive - Linked Engineering GmbH") == (
        "D--OneDrive---Linked-Engineering-GmbH"
    )


def test_without_exclusion_old_call_exactly(fake_monitor, projects, monkeypatch):
    monkeypatch.setattr(config, "QUOTA_EXCLUDE_DIRS", [])
    fake_monitor.default.extend([_e("m1", "r1", 1)])

    result = claude_billing.load_subscription_entries(48)

    assert fake_monitor.calls == [{"hours_back": 48, "mode": "auto"}]  # no data_path
    assert [e.message_id for e in result] == ["m1"]


def test_without_exclusion_still_filters_credit_runs(fake_monitor, projects, monkeypatch):
    monkeypatch.setattr(config, "QUOTA_EXCLUDE_DIRS", [])
    monkeypatch.setattr(claude_billing, "api_message_ids", lambda: {"api-1"})
    fake_monitor.default.extend([_e("abo-1", "r1", 1), _e("api-1", "r2", 2)])

    assert [e.message_id for e in claude_billing.load_subscription_entries(48)] == ["abo-1"]


def test_exclusion_skips_excluded_folders_only(fake_monitor, projects, exclude):
    claude_billing.load_subscription_entries(24)

    assert _loaded(fake_monitor) == sorted([
        "D--OneDrive---White-Lady-e-U",
        "D--programmieren-LE-Python2",
        "D--programmieren-privat-python-x",
    ])
    assert all(c["hours_back"] == 24 and c["mode"] == "auto" for c in fake_monitor.calls)


def test_exclusion_merges_sorts_and_dedupes(fake_monitor, projects, exclude):
    fake_monitor.by_folder["D--programmieren-privat-python-x"] = [_e("m3", "r3", 30), _e("m1", "r1", 10)]
    fake_monitor.by_folder["D--programmieren-LE-Python2"] = [_e("m1", "r1", 10), _e("m2", "r2", 20)]
    fake_monitor.by_folder["D--programmieren-LE-Python"] = [_e("le", "le", 5)]  # excluded
    fake_monitor.by_folder["D--OneDrive---White-Lady-e-U"] = [_e(None, None, 40), _e(None, None, 41)]

    result = claude_billing.load_subscription_entries(24)

    assert [e.message_id for e in result] == ["m1", "m2", "m3", None, None]  # ids unique, id-less kept
    assert [e.timestamp for e in result] == sorted(e.timestamp for e in result)


def test_exclusion_keeps_all_entries_with_claude_monitor_default_ids(fake_monitor, projects, exclude):
    # claude_monitor fills missing ids with message_id="" / request_id="unknown"
    fake_monitor.by_folder["D--programmieren-privat-python-x"] = [_e("", "unknown", 10), _e("", "unknown", 11)]
    fake_monitor.by_folder["D--programmieren-LE-Python2"] = [_e("", "unknown", 12), _e("m9", "unknown", 13), _e("m9", "unknown", 14)]
    fake_monitor.by_folder["D--OneDrive---White-Lady-e-U"] = [_e("m1", "r1", 15), _e("m1", "r1", 16)]

    result = claude_billing.load_subscription_entries(24)

    assert [e.timestamp.minute for e in result] == [10, 11, 12, 13, 14, 15]  # only the real-id duplicate (16) dropped


def test_exclusion_still_filters_credit_runs(fake_monitor, projects, exclude, monkeypatch):
    monkeypatch.setattr(claude_billing, "api_message_ids", lambda: {"api-1"})
    fake_monitor.by_folder["D--programmieren-privat-python-x"] = [_e("abo-1", "r1", 1), _e("api-1", "r2", 2)]

    assert [e.message_id for e in claude_billing.load_subscription_entries(24)] == ["abo-1"]


def test_exclusion_with_missing_projects_dir_is_empty(fake_monitor, tmp_path, exclude, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", tmp_path / "nope")

    assert claude_billing.load_subscription_entries(24) == []
    assert fake_monitor.calls == []


def test_folder_without_jsonl_is_not_given_to_the_loader(fake_monitor, projects, exclude):
    (projects / "D--empty-folder").mkdir()
    (projects / "D--only-subdir" / "sub").mkdir(parents=True)
    (projects / "D--nested" / "sub").mkdir(parents=True)
    (projects / "D--nested" / "sub" / "deep.jsonl").write_text("", encoding="utf-8")

    claude_billing.load_subscription_entries(24)

    loaded = _loaded(fake_monitor)
    assert "D--empty-folder" not in loaded
    assert "D--only-subdir" not in loaded
    assert "D--nested" in loaded  # *.jsonl found recursively


def test_reader_logger_muted_during_loop_and_restored(fake_monitor, projects, exclude):
    reader_log = logging.getLogger("claude_monitor.data.reader")
    seen = []
    orig = fake_monitor.reader.load_usage_entries

    def spy(**kw):
        seen.append(reader_log.level)
        return orig(**kw)

    fake_monitor.reader.load_usage_entries = spy
    reader_log.setLevel(logging.DEBUG)
    try:
        claude_billing.load_subscription_entries(24)
        assert seen
        assert all(lvl == logging.WARNING for lvl in seen)  # INFO "Processed ..." muted in the loop
        assert reader_log.level == logging.DEBUG
    finally:
        reader_log.setLevel(logging.NOTSET)


def test_reader_logger_level_restored_when_loader_raises(fake_monitor, projects, exclude):
    reader_log = logging.getLogger("claude_monitor.data.reader")

    def boom(**_kw):
        raise RuntimeError("kaputt")

    fake_monitor.reader.load_usage_entries = boom
    reader_log.setLevel(logging.INFO)
    try:
        with pytest.raises(RuntimeError):
            claude_billing.load_subscription_entries(24)
        assert reader_log.level == logging.INFO
    finally:
        reader_log.setLevel(logging.NOTSET)


def _parse_exclude_env(raw):
    env = {**os.environ, "ORCH_QUOTA_EXCLUDE_DIRS": raw}
    out = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import config, json; print(json.dumps(config.QUOTA_EXCLUDE_DIRS))"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("suffix", ["\\", "/", "\\/"])
def test_trailing_separator_gives_same_exclusion_as_without(fake_monitor, projects, monkeypatch, suffix):
    plain = _parse_exclude_env(r"D:\programmieren\LE_Python;D:\OneDrive - Linked Engineering GmbH")
    with_sep = _parse_exclude_env(
        r"D:\programmieren\LE_Python" + suffix + r";D:\OneDrive - Linked Engineering GmbH" + suffix
    )
    assert with_sep == plain == [r"D:\programmieren\LE_Python", r"D:\OneDrive - Linked Engineering GmbH"]

    monkeypatch.setattr(config, "QUOTA_EXCLUDE_DIRS", with_sep)
    claude_billing.load_subscription_entries(24)
    assert _loaded(fake_monitor) == sorted([
        "D--OneDrive---White-Lady-e-U",
        "D--programmieren-LE-Python2",
        "D--programmieren-privat-python-x",
    ])


# ---------------------------------------------------------------- callers


def test_calibration_uses_exclusion_not_default_path(fake_monitor, projects, exclude):
    fake_monitor.by_folder["D--programmieren-privat-python-x"] = [_e("m1", "r1", 1)]

    result = quota_calibration._load_entries(24)

    assert [e.message_id for e in result] == ["m1"]
    assert fake_monitor.calls
    assert all("data_path" in c for c in fake_monitor.calls)


def test_calibration_loader_error_is_none(fake_monitor, projects, exclude):
    def boom(**_kw):
        raise RuntimeError("kaputt")

    fake_monitor.reader.load_usage_entries = boom

    assert quota_calibration._load_entries(24) is None


def test_local_fallback_uses_exclusion_not_default_path(fake_monitor, projects, exclude):
    seen = []

    class FakeAnalyzer:
        def __init__(self, session_duration_hours):
            pass

        def transform_to_blocks(self, entries):
            seen.extend(e.message_id for e in entries)
            return []

    fake_monitor.analyzer.SessionAnalyzer = FakeAnalyzer
    fake_monitor.by_folder["D--programmieren-privat-python-x"] = [_e("mine", "r1", 1)]
    fake_monitor.by_folder["D--programmieren-LE-Python-Bremsen-Tool"] = [_e("le", "r2", 2)]

    limits._get_claude_limits_from_local("pro")

    assert seen == ["mine"]
    assert fake_monitor.calls
    assert all("data_path" in c for c in fake_monitor.calls)


# ----------------------------------------------------------------- config


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        (" ; ;", []),
        (r"D:\a\b", [r"D:\a\b"]),
        (r" D:\a b ;;E:\c; ", [r"D:\a b", r"E:\c"]),
    ],
)
def test_config_parses_exclude_dirs(raw, expected):
    env = {**os.environ, "ORCH_QUOTA_EXCLUDE_DIRS": raw}
    out = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import config, json; print(json.dumps(config.QUOTA_EXCLUDE_DIRS))"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=True,
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == expected
