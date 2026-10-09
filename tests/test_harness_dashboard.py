"""P4 — the dashboard's "Harness" tab: GET /api/harness over the index SQLite.

The index is built with ``harness_index.open_index`` (the real schema) and filled
either with plain INSERTs (exact numbers) or by a real ``run_update`` over
fixture files. The endpoint is exercised through a real 127.0.0.1 server — the
single-threaded one the orchestrator runs — to pin the "< 1 s, never raise,
never wait" contract for missing, empty, locked, corrupt and other-schema files.
"""

import hashlib
import json
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

import config
import dashboard
import harness_index as hi

NOW = datetime(2026, 10, 9, 12, 0, 0)


def db_path() -> Path:
    return Path(config.HARNESS_DB_FILE)


def new_index() -> sqlite3.Connection:
    return hi.open_index(db_path())


def add_run(conn, *, finished="2026-10-09T11:30:00", skipped=3, status="ok"):
    conn.execute(
        "INSERT INTO index_runs(started, finished, duration_sec, files_read, bytes_read, lines_read, "
        "lines_skipped, errors, per_source, peak_rss_mb, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-09T11:29:00", finished, 60.0, 10, 1000, 500, skipped, "{}", "{}", 30.0, status))


def add_msg(conn, msg_id, *, day="2026-10-08", kind="main", origin="interaktiv", family="opus",
            inp=10, out=20, cr=300, cw=40):
    conn.execute(
        "INSERT INTO claude_msg(file_key, msg_id, session_id, kind, origin, model, family, ts, day, "
        "input, output, cache_read, cache_write) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("f", msg_id, "s", kind, origin, "claude-" + family, family, day + "T10:00:00Z", day, inp, out, cr, cw))


def add_marker(conn, date, mid, expect, change="Satz zur Änderung"):
    conn.execute("INSERT INTO marker(date, id, scope, change, expect) VALUES (?,?,?,?,?)",
                 (date, mid, "beide", change, expect))


def add_ledger(conn, day, n, *, voice="codex", status="ok"):
    for i in range(n):
        conn.execute("INSERT INTO ledger(ts_local, voice, repo_hash, repo_name, status, tokens, day) "
                     "VALUES (?,?,?,?,?,?,?)", (f"{day}T10:{i:02d}:00+02:00", voice, "h", "r", status, None, day))


def payload(**kw) -> dict:
    kw.setdefault("now", NOW)
    result: dict = hi.dashboard_payload(db_path(), **kw)["harness"]
    return result


# ── availability: missing / empty / locked / corrupt / other schema ────────


def _serve():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server, port = dashboard._bind_server(port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def _get(port, path, timeout=5) -> tuple[dict, float]:
    started = time.monotonic()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data, time.monotonic() - started


@pytest.fixture
def server_port():
    server, port = _serve()
    yield port
    server.shutdown()
    server.server_close()


def _make_missing():
    pass


def _make_empty():
    db_path().parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(db_path())).close()  # a file with no tables


def _make_corrupt():
    db_path().parent.mkdir(parents=True, exist_ok=True)
    db_path().write_bytes(b"this is not a database at all" * 100)


def _make_old_schema():
    conn = new_index()
    conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    conn.close()


def _make_never_run():
    new_index().close()


def _make_locked():
    """A database in rollback-journal mode with an exclusive lock held — the case
    WAL is there to avoid; the reader must give up after its short timeout."""
    db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path()), isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA locking_mode=EXCLUSIVE")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("BEGIN EXCLUSIVE")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    return conn


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (_make_missing, "noch nicht gelaufen"),
        (_make_empty, "noch nicht gelaufen"),
        (_make_never_run, "noch nicht gelaufen"),
        (_make_corrupt, "nicht lesbar"),
        (_make_old_schema, "Schema"),
        (_make_locked, "locked"),
    ],
    ids=["missing", "empty", "never_run", "corrupt", "old_schema", "locked"],
)
def test_endpoint_answers_unavailable_in_under_a_second(server_port, make, reason):
    holder = make()
    try:
        data, elapsed = _get(server_port, "/api/harness?days=30&window=7")
        assert elapsed < 1.0, elapsed
        assert data["harness"]["available"] is False
        assert reason in data["harness"]["reason"], data
        # the single-threaded server is free again at once
        started = time.monotonic()
        with urllib.request.urlopen(f"http://127.0.0.1:{server_port}/", timeout=5) as resp:
            assert resp.status == 200
        assert time.monotonic() - started < 1.0
    finally:
        if holder is not None:
            holder.close()


def test_a_long_write_transaction_of_the_index_does_not_block_the_reader(server_port):
    conn = new_index()  # WAL, like the real indexer
    add_run(conn)
    add_msg(conn, "m1")
    writer = sqlite3.connect(str(db_path()), isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO meta VALUES ('busy', 'x')")
    try:
        data, elapsed = _get(server_port, "/api/harness")
        assert elapsed < 1.0
        assert data["harness"]["available"] is True
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        conn.close()


def test_query_budget_turns_a_slow_read_into_unavailable(monkeypatch):
    conn = new_index()
    add_run(conn)
    for i in range(2000):
        add_msg(conn, f"m{i}")
    conn.close()
    monkeypatch.setattr(hi, "HARNESS_QUERY_BUDGET_SEC", -1.0)  # already over budget
    data = hi.dashboard_payload(db_path(), now=NOW)["harness"]
    assert data["available"] is False
    assert "interrupted" in data["reason"]


def test_endpoint_reads_only_bytes_and_mtime_stay(server_port):
    conn = new_index()
    add_run(conn)
    add_msg(conn, "m1")
    add_marker(conn, "2026-10-01", "x", "externe Aufrufe sinken")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = (hashlib.sha256(db_path().read_bytes()).hexdigest(), db_path().stat().st_mtime_ns)
    for _ in range(2):
        data, _ = _get(server_port, "/api/harness?days=90&window=14")
        assert data["harness"]["available"] is True
    assert (hashlib.sha256(db_path().read_bytes()).hexdigest(), db_path().stat().st_mtime_ns) == before


def test_read_connection_refuses_writes():
    new_index().close()
    conn = hi._connect_read_only(db_path())
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta VALUES ('x', 'y')")
    finally:
        conn.close()


# ── (a) usage, split and totals ─────────────────────────────────────────────


def test_split_by_source_adds_up_to_the_total_row():
    conn = new_index()
    add_run(conn)
    add_msg(conn, "i1", origin="interaktiv", family="opus")
    add_msg(conn, "i2", origin="interaktiv", family="fable", day="2026-10-07")
    add_msg(conn, "o1", origin="orchestrator", family="sonnet")
    add_msg(conn, "s1", kind="subagent", origin="orchestrator", family="haiku", inp=1, out=2, cr=3, cw=4)
    add_msg(conn, "s2", kind="subagent", origin="interaktiv", family="sonnet")
    add_msg(conn, "u1", origin="unbekannt", family="andere")
    add_msg(conn, "old", day="2026-01-01")  # outside the 30-day range
    conn.close()
    h = payload(days=30)
    t = h["usage"]["totals"]
    for key in ("input", "output", "cache_read", "cache_write", "total", "messages"):
        assert sum(t[c][key] for c in hi.CATEGORIES) == t["all"][key], key
    assert t["all"]["messages"] == 6
    assert t["subagent"]["messages"] == 2
    assert t["orchestrator"]["messages"] == 1
    assert h["usage"]["subagent_by_origin"] == {"interaktiv": 370, "orchestrator": 10, "unbekannt": 0}
    assert h["usage"]["subagent_share_pct"] == round(380 / (5 * 370 + 10) * 100, 1)
    fam_all = h["usage"]["families"]["all"]
    assert set(fam_all) == {"opus", "sonnet", "haiku", "fable", "andere"}
    # per day: the two days carry data, the rest is absent (the page shows 0)
    assert set(h["usage"]["by_day"]) == {"2026-10-07", "2026-10-08"}
    assert len(h["day_list"]) == 30 and h["day_list"][-1] == "2026-10-09"


def test_status_line_data_carries_age_and_skipped_lines():
    conn = new_index()
    add_run(conn, finished="2026-10-09T11:30:00", skipped=7)
    conn.close()
    run = payload()["last_run"]
    assert run["age_sec"] == 1800
    assert run["lines_skipped"] == 7


def test_agent_calls_and_control_count():
    conn = new_index()
    add_run(conn)
    for i, (t, m) in enumerate([("Explore", "sonnet"), ("Explore", "sonnet"), ("Plan", "Standard")]):
        conn.execute("INSERT INTO claude_agent_call(tool_use_id, day, subagent_type, model_req, origin) "
                     "VALUES (?,?,?,?,?)", (f"t{i}", "2026-10-08", t, m, "interaktiv"))
    conn.execute("INSERT INTO claude_agent_meta(session_id, agent_id, day) VALUES ('s','a','2026-10-08')")
    conn.close()
    ac = payload()["agent_calls"]
    assert ac["rows"] == [{"subagent_type": "Explore", "model": "sonnet", "calls": 2},
                          {"subagent_type": "Plan", "model": "Standard", "calls": 1}]
    assert (ac["tool_use_count"], ac["meta_count"], ac["difference"]) == (3, 1, 2)


# ── (b) Codex quota from the newest rollout ─────────────────────────────────


def _rollout(conn, key, rl_ts, *, used1, used2, resets1, resets2):
    conn.execute(
        "INSERT INTO codex_rollout(file_key, rl_ts, primary_used, primary_window, primary_resets, "
        "secondary_used, secondary_window, secondary_resets) VALUES (?,?,?,?,?,?,?,?)",
        (key, rl_ts, used1, 300, resets1, used2, 10080, resets2))


def test_codex_quota_uses_the_newest_token_count_and_marks_expired_windows():
    now_epoch = NOW.timestamp()
    conn = new_index()
    add_run(conn)
    # "zzz" sorts last by name but is older by timestamp: the timestamp decides
    _rollout(conn, "zzz", "2026-10-09T09:00:00.000Z", used1=50.0, used2=60.0,
             resets1=now_epoch + 999, resets2=now_epoch + 999)
    _rollout(conn, "aaa", "2026-10-09T10:00:00.000Z", used1=7.0, used2=12.0,
             resets1=now_epoch - 60, resets2=now_epoch + 3600)
    conn.close()
    codex = payload()["quota"]["codex"]
    assert codex["ts"] == "2026-10-09T10:00:00.000Z"
    assert codex["primary"]["expired"] is True       # reset already passed
    assert codex["secondary"]["expired"] is False
    assert codex["secondary"]["free_pct"] == 88.0
    assert codex["secondary"]["window_minutes"] == 10080


# ── (d) markers ─────────────────────────────────────────────────────────────


def test_marker_windows_are_equally_long_and_exclude_the_marker_day():
    series = {f"2026-09-{d:02d}": float(d) for d in range(1, 31)}
    w = hi.marker_window(series, "2026-09-15", 7, today="2026-10-09")
    assert len(w["before_days"]) == len(w["after_days"]) == 7
    assert "2026-09-15" not in w["before_days"] + w["after_days"]
    assert w["before_days"][0] == "2026-09-08" and w["before_days"][-1] == "2026-09-14"
    assert w["after_days"][0] == "2026-09-16" and w["after_days"][-1] == "2026-09-22"
    assert w["complete"] is True
    assert w["before_avg"] == 11.0 and w["after_avg"] == 19.0
    assert w["delta_pct"] == round((19 - 11) / 11 * 100, 1)


@pytest.mark.parametrize("n", [3, 7, 14])
def test_marker_after_window_still_running_is_marked_incomplete(n):
    w = hi.marker_window({}, "2026-10-08", n, today="2026-10-09")
    assert len(w["before_days"]) == n
    assert w["after_days"] == []  # 10-09 is today, not a completed day
    assert w["complete"] is False
    w2 = hi.marker_window({}, "2026-10-05", n, today="2026-10-09")
    assert len(w2["after_days"]) == min(n, 3)
    assert w2["complete"] is (n <= 3)


# The eight live markers' `expect` texts (task description, 2026-10-09).
_LIVE_EXPECT = {
    "extern-diaet": "externe Aufrufe je Loop sinken, Fehlversuche sinken",
    "oc-endpunktpreise-lesebeleg": "opencode-Kosten je Pass sinken, Sessions je Pass = 1",
    "pr4-mindestaufwand": "verify_failed wird erkannt, keine ok-Laeufe mit leerem Ergebnis",
    "zitate": "Anteil belegter Zitate steigt",
    "policy-1": "keine ungeprueften Laeufe bei Policy-Stoerung",
    "policy-2": "keine ungeprueften Laeufe bei Policy-Stoerung",
    "oc-bericht": "opencode-Berichtsquote steigt, Kosten je Pass etwa gleich",
    "deckel": "keine Ausnahmen vom 100-KB-Deckel",
}


def test_keyword_table_maps_the_live_markers_and_never_guesses():
    mapped = {mid: [k for k, _ in hi.marker_kpis(expect)] for mid, expect in _LIVE_EXPECT.items()}
    assert mapped == {
        "extern-diaet": ["ledger_calls", "ledger_failures"],
        "oc-endpunktpreise-lesebeleg": ["opencode_cost", "opencode_sessions"],
        "pr4-mindestaufwand": [],
        "zitate": [],
        "policy-1": [],
        "policy-2": [],
        "oc-bericht": ["opencode_cost"],
        "deckel": [],
    }


def test_marker_payload_tables_and_lines():
    conn = new_index()
    add_run(conn)
    add_marker(conn, "2026-09-25", "extern-diaet", _LIVE_EXPECT["extern-diaet"])
    add_marker(conn, "2026-10-06", "pr4-mindestaufwand", _LIVE_EXPECT["pr4-mindestaufwand"])
    for d in range(18, 25):
        add_ledger(conn, f"2026-09-{d:02d}", 4)
    add_ledger(conn, "2026-09-25", 50)  # the marker day itself must not count
    for d in range(26, 31):
        add_ledger(conn, f"2026-09-{d:02d}", 2, status="limit")
    for d in (1, 2):
        add_ledger(conn, f"2026-10-{d:02d}", 2)
    conn.close()
    markers = {m["id"]: m for m in payload(window=7)["markers"]}
    assert markers["pr4-mindestaufwand"]["kpis"] == []          # only the line
    calls, fails = markers["extern-diaet"]["kpis"]
    assert calls["kpi"] == "ledger_calls" and fails["kpi"] == "ledger_failures"
    assert calls["before_avg"] == 4.0 and calls["after_avg"] == 2.0
    assert calls["complete"] is True
    assert fails["before_avg"] == 0.0 and fails["after_avg"] == round(10 / 7, 6)
    assert payload(window=3)["markers"][0]["kpis"][0]["n"] == 3
    assert payload(window=5)["window"] == 7  # only 3/7/14


# ── end to end: fixture files → run_update → payload ──────────────────────


def test_end_to_end_from_source_files():
    projects = Path(config.HARNESS_CLAUDE_PROJECTS_DIR) / "C--proj-beispiel"
    projects.mkdir(parents=True)
    sid = "27d83e65-0000-4000-8000-000000000001"
    lines = [
        {"type": "user", "entrypoint": "sdk-cli", "cwd": "C:\\proj\\beispiel", "sessionId": sid,
         "timestamp": "2026-10-08T06:00:00Z", "message": {"role": "user", "content": "p"}},
        {"type": "assistant", "entrypoint": "sdk-cli", "sessionId": sid, "timestamp": "2026-10-08T06:01:00Z",
         "message": {"id": "msg_1", "model": "claude-opus-5-5", "content": [],
                     "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3,
                               "cache_creation_input_tokens": 4}}},
    ]
    (projects / f"{sid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    Path(config.HARNESS_CHANGES_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(config.HARNESS_CHANGES_FILE).write_text(json.dumps(
        {"date": "2026-10-08", "id": "oc-endpunktpreise-lesebeleg", "scope": "beide", "change": "c",
         "expect": _LIVE_EXPECT["oc-endpunktpreise-lesebeleg"], "source": "s"}) + "\n", encoding="utf-8")
    assert hi.run_update(out=lambda _m: None)["status"] == "ok"
    h = payload(now=datetime(2026, 10, 9, 12, 0))
    assert h["available"] is True
    assert h["usage"]["totals"]["orchestrator"]["total"] == 10
    assert h["usage"]["totals"]["all"]["total"] == 10
    (marker,) = h["markers"]
    assert [k["kpi"] for k in marker["kpis"]] == ["opencode_cost", "opencode_sessions"]
    assert all(k["complete"] is False for k in marker["kpis"])


# ── the tab's JavaScript, executed in node ──────────────────────────────────

NODE = shutil.which("node")

_STUB = r"""
const _els = {};
function _el(id) {
  if (!_els[id]) _els[id] = { id, innerHTML: '', textContent: '', className: '', style: {}, dataset: {},
    classList: { toggle() {}, add() {}, remove() {} }, getContext() { return { canvasId: id }; },
    appendChild(c) { (this.children = this.children || []).push(c); } };
  return _els[id];
}
globalThis.document = { getElementById: _el, querySelectorAll() { return []; },
                        createElement() { return _el('__new' + Object.keys(_els).length); } };
globalThis.location = { hash: '' };
const _charts = [];
globalThis.Chart = function (ctx, cfg) { this.canvasId = ctx.canvasId; this.data = cfg.data;
  this.plugins = cfg.plugins || []; _charts.push(this); };
Chart.prototype.update = function () {};
Chart.prototype.resetZoom = function () {};
globalThis.fetch = () => new Promise(() => {});
globalThis.setInterval = () => 0;
"""


def _run_tab(tmp_path, harness: dict, data: dict | None = None) -> dict:
    script = re.findall(r"<script>(.*?)</script>", dashboard._HTML_PAGE, re.S)[0]
    js = tmp_path / "tab.js"
    probe = """
      const g = id => document.getElementById(id);
      return { status: g('h-status').textContent, statusClass: g('h-status').className,
               body: g('h-body').style.display, usage: g('h-usage-table').innerHTML,
               markers: g('h-markers').innerHTML, quota: g('h-quota').innerHTML,
               nullLine: g('h-extern-null').textContent, control: g('h-agent-control').textContent,
               charts: _charts.filter(c => c.canvasId.startsWith('h-')).map(c => [c.canvasId,
                 c.plugins.map(p => p.id), c.data.labels.length]) };
    """
    js.write_text(
        _STUB + script
        + (f"\n_lastData = {json.dumps(data)}; update(_lastData);\n" if data else "\n")
        + f"\nrenderHarness({json.dumps(harness)});\n"
        + "console.log(JSON.stringify((function(){" + probe + "})()));\n",
        encoding="utf-8")
    assert NODE is not None
    proc = subprocess.run([NODE, str(js)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    result: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_tab_renders_split_markers_quota_and_escapes(tmp_path):
    conn = new_index()
    add_run(conn, skipped=5)
    add_msg(conn, "i1", origin="interaktiv")
    add_msg(conn, "o1", origin="orchestrator")
    add_msg(conn, "s1", kind="subagent", origin="orchestrator")
    add_marker(conn, "2026-10-08", "<img src=x onerror=alert(1)>", "externe Aufrufe sinken",
               change="<script>bad()</script>")
    add_marker(conn, "2026-10-01", "nur-linie", "Anteil belegter Zitate steigt")
    _rollout(conn, "r", "2026-10-09T10:00:00.000Z", used1=7.0, used2=12.0,
             resets1=NOW.timestamp() - 1, resets2=NOW.timestamp() + 99)
    add_ledger(conn, "2026-10-08", 2, voice="opencode")
    conn.close()
    h = payload()
    out = _run_tab(tmp_path, h)
    assert out["body"] == ""
    assert "5 übersprungen" in out["status"]
    for label in ("interaktiv", "Orchestrator / claude -p", "Subagent", "Gesamt"):
        assert label in out["usage"]
    assert "unbekannt" not in out["usage"]  # shown only when it carries data
    assert "nur Linie" in out["markers"]
    assert "unvollständig (0 von 7 Tagen)" in out["markers"]
    assert "<img" not in out["markers"] and "&lt;img" in out["markers"]
    assert "<script>" not in out["markers"]
    assert "Fenster abgelaufen" in out["quota"]
    assert "Codex 7 Tage" in out["quota"]
    assert "2 von 2" in out["nullLine"]
    charts = {c[0]: c for c in out["charts"]}
    assert set(charts) == {"h-usage-chart", "h-cost-chart", "h-extern-chart"}
    assert all(c[1] == ["harnessMarkerLines"] for c in charts.values())
    assert all(c[2] == 30 for c in charts.values())


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_tab_without_index_says_so_and_hides_the_body(tmp_path):
    out = _run_tab(tmp_path, hi.dashboard_payload(db_path())["harness"])
    assert out["body"] == "none"
    assert "noch nicht gelaufen" in out["status"]
    assert "python -m harness_index --update" in out["status"]
    assert "warn" in out["statusClass"]
