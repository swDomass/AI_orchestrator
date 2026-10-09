"""P3 — harness_index.py: incremental SQLite index over local transcripts and logs.

Real JSONL files, real SQLite databases (an opencode.db with the real schema, the
index itself) and real ``python -m harness_index`` child processes, all under
``tmp_path`` (``tests/conftest.py::_isolate_harness_sources`` points every
``HARNESS_*`` path there). Fixture lines follow the anonymised formats of the
task description; ``CANARY_*`` strings stand in for prompt/answer/title/path text
and must never reach the index file.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import config
import dashboard
import harness_index as hi

REPO = Path(__file__).resolve().parent.parent

S_CLI = "27d83e65-0000-4000-8000-000000000001"
S_SDK = "809e62a4-0000-4000-8000-000000000002"
S_BRIDGE = "5b0c3a11-0000-4000-8000-000000000003"
S_NOENTRY = "6c1d4b22-0000-4000-8000-000000000004"
AGENT_A = "a34730aa-0000-4000-8000-00000000000a"
AGENT_B = "b45841bb-0000-4000-8000-00000000000b"
PROJECT_DIR = "C--proj-beispiel"
CWD = "C:\\CANARY_DRIVE_PATH\\CANARY_FOLDER_NAME"  # neither part may reach the index (K9)


def _j(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def write_jsonl(path: Path, objs, *, newline_at_end: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(o if isinstance(o, str) else _j(o) for o in objs)
    if newline_at_end and objs:
        text += "\n"
    # newline="": byte-exact \n on every platform — the offsets below are counted in \n bytes
    path.write_text(text, encoding="utf-8", newline="")
    return path


def append_jsonl(path: Path, objs) -> None:
    with path.open("a", encoding="utf-8", newline="") as f:
        for o in objs:
            f.write((o if isinstance(o, str) else _j(o)) + "\n")


# ── Claude line builders ────────────────────────────────────────────────────


def c_header(session, entrypoint="cli", ts="2026-10-09T06:04:24.838Z", sidechain=False):
    line = {
        "parentUuid": None, "isSidechain": sidechain, "promptId": "c72f9845-0000",
        "type": "user", "message": {"role": "user", "content": "CANARY_PROMPT_TEXT"},
        "uuid": f"u-{session[:8]}", "timestamp": ts, "permissionMode": "bypassPermissions",
        "promptSource": "typed" if entrypoint == "cli" else "sdk", "userType": "external",
        "cwd": CWD, "sessionId": session, "version": "2.1.295", "gitBranch": "CANARY_BRANCH",
    }
    if entrypoint is not None:
        line["entrypoint"] = entrypoint
    return line


def c_assistant(session, msg_id, *, model="claude-opus-5-5", usage=None, ts="2026-10-09T06:04:34.484Z",
                entrypoint="cli", block=0, content=None, sidechain=False, agent_id=None, uuid=None):
    usage = usage if usage is not None else {
        "input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
        "output_tokens": 50, "output_tokens_details": {"thinking_tokens": 9},
        "service_tier": "standard",
    }
    line = {
        "parentUuid": "dddbe42f-0000", "isSidechain": sidechain,
        "message": {
            "model": model, "id": msg_id, "type": "message", "role": "assistant",
            "content": content if content is not None else [
                {"type": "thinking", "thinking": "CANARY_THINKING_TEXT", "signature": "CANARY_SIG"},
            ],
            "stop_reason": "tool_use", "usage": usage,
        },
        "apiBlockIndex": block, "requestId": "req_011C", "type": "assistant",
        "uuid": uuid or f"{msg_id}-{block}", "timestamp": ts, "effort": "CANARY_EFFORT",
        "userType": "external", "entrypoint": entrypoint, "cwd": CWD, "sessionId": session,
        "version": "2.1.295", "gitBranch": "CANARY_BRANCH",
    }
    if agent_id:
        line["agentId"] = agent_id
    if entrypoint is None:
        del line["entrypoint"]
    return line


def c_agent_call(session, msg_id, tool_id, *, name="Agent", subagent_type="Explore", model="sonnet",
                 entrypoint="cli", ts="2026-10-09T06:04:42.535Z"):
    inp = {"description": "CANARY_AGENT_DESCRIPTION", "subagent_type": subagent_type,
           "prompt": "CANARY_AGENT_PROMPT", "effort": "CANARY_EFFORT"}
    if model is not None:
        inp["model"] = model
    return c_assistant(
        session, msg_id, entrypoint=entrypoint, ts=ts,
        content=[{"type": "tool_use", "id": tool_id, "name": name, "input": inp,
                  "caller": {"type": "direct"}}],
    )


def c_cost(session, cost, start_ms=1791452753910):
    return {"type": "cost-state", "sessionId": session, "totalCostUSD": cost,
            "totalAPIDuration": 1, "startTime": start_ms,
            "modelUsage": {"claude-opus-5-5": {"inputTokens": 1, "costUSD": cost}},
            "hasUnknownModelCost": False}


def claude_root() -> Path:
    return Path(config.HARNESS_CLAUDE_PROJECTS_DIR)


def main_path(session: str) -> Path:
    return claude_root() / PROJECT_DIR / f"{session}.jsonl"


def sub_path(session: str, agent: str) -> Path:
    return claude_root() / PROJECT_DIR / session / "subagents" / f"agent-{agent}.jsonl"


def meta_path(session: str, agent: str) -> Path:
    return claude_root() / PROJECT_DIR / session / "subagents" / f"agent-{agent}.meta.json"


def write_meta(session, agent, **kw):
    data = {"agentType": "Explore", "description": "CANARY_META_DESCRIPTION", "toolUseId": "toolu_x",
            "spawnDepth": 1, "requestShape": "background", "requestNonInteractive": True,
            "model": "sonnet", "effort": "CANARY_EFFORT"}
    data.update(kw)
    p = meta_path(session, agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_j(data), encoding="utf-8")


# ── running the index ───────────────────────────────────────────────────────


def run() -> dict:
    lines: list[str] = []
    summary = hi.run_update(hi.Sources.from_config(), out=lines.append)
    summary["_out"] = lines
    return summary


def db() -> sqlite3.Connection:
    return sqlite3.connect(str(config.HARNESS_DB_FILE))


def q(sql, *args):
    with db() as conn:
        return conn.execute(sql, args).fetchall()


def dump_tables() -> dict:
    tables = ["claude_msg", "claude_session", "claude_cost", "claude_agent_call", "claude_agent_meta",
              "codex_rollout", "oc_session", "oc_message", "ledger", "marker"]
    with db() as conn:
        return {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr) for t in tables}


def claude_tokens(where="1=1"):
    return q(f"SELECT coalesce(sum(input),0), coalesce(sum(output),0), coalesce(sum(cache_read),0), "
             f"coalesce(sum(cache_write),0), count(*) FROM claude_msg WHERE {where}")[0]


# ── Claude: counting ────────────────────────────────────────────────────────


@pytest.mark.parametrize("repeats", [2, 4])
def test_duplicate_message_id_is_counted_once(repeats):
    write_jsonl(main_path(S_CLI), [
        c_header(S_CLI),
        *[c_assistant(S_CLI, "msg_dup", block=i) for i in range(repeats)],
        c_assistant(S_CLI, "msg_other"),
    ])
    run()
    inp, out, cread, cwrite, rows = claude_tokens()
    assert rows == 2
    assert (inp, out, cread, cwrite) == (4, 100, 2000, 200)


@pytest.mark.parametrize(
    ("entrypoint", "origin"),
    [("cli", "interaktiv"), ("sdk-cli", "orchestrator"), (None, "unbekannt"), ("claude-vscode", "unbekannt")],
)
def test_entrypoint_maps_to_origin(entrypoint, origin):
    session = S_CLI
    write_jsonl(main_path(session), [
        {"type": "queue-operation", "operation": "enqueue", "content": "CANARY_QUEUE_TEXT"},
        {"type": "mode", "mode": "normal"},
        c_header(session, entrypoint=entrypoint),
        c_assistant(session, "msg_1", entrypoint=entrypoint),
    ])
    run()
    assert q("SELECT origin FROM claude_msg") == [(origin,)]
    assert q("SELECT origin FROM claude_session WHERE session_id=?", session) == [(origin,)]


def test_bridge_session_only_file_is_unknown_and_has_no_usage():
    write_jsonl(main_path(S_BRIDGE), [{
        "type": "bridge-session", "sessionId": S_BRIDGE, "bridgeSessionId": "CANARY_BRIDGE",
        "lastSequenceNum": 0, "ownerAccountUuid": "CANARY_OWNER", "ownerOrganizationUuid": "CANARY_ORG",
    }])
    run()
    assert q("SELECT entrypoint, origin FROM claude_session") == [(None, "unbekannt")]
    assert claude_tokens()[4] == 0


def test_subagent_is_its_own_kind_and_inherits_the_parent_entrypoint():
    write_jsonl(main_path(S_SDK), [c_header(S_SDK, "sdk-cli"), c_assistant(S_SDK, "msg_p", entrypoint="sdk-cli")])
    # The subagent's own lines say "cli" — the PARENT (sdk-cli) must win.
    write_jsonl(sub_path(S_SDK, AGENT_A), [
        c_assistant(S_SDK, "msg_s1", model="claude-sonnet-5-5", entrypoint="cli", sidechain=True, agent_id=AGENT_A),
    ])
    run()
    rows = q("SELECT msg_id, kind, origin, agent_id, session_id FROM claude_msg ORDER BY msg_id")
    assert rows == [
        ("msg_p", "main", "orchestrator", None, S_SDK),
        ("msg_s1", "subagent", "orchestrator", AGENT_A, S_SDK),
    ]


def test_subagent_without_known_parent_falls_back_to_its_own_entrypoint():
    write_jsonl(sub_path(S_CLI, AGENT_A), [
        c_assistant(S_CLI, "msg_s1", entrypoint="cli", sidechain=True, agent_id=AGENT_A),
    ])
    run()
    assert q("SELECT kind, origin FROM claude_msg") == [("subagent", "interaktiv")]


def test_split_by_origin_and_kind_adds_up_to_the_total():
    write_jsonl(main_path(S_CLI), [c_header(S_CLI, "cli"), c_assistant(S_CLI, "m1"), c_assistant(S_CLI, "m2")])
    write_jsonl(main_path(S_SDK), [c_header(S_SDK, "sdk-cli"), c_assistant(S_SDK, "m3", entrypoint="sdk-cli")])
    write_jsonl(sub_path(S_CLI, AGENT_A), [c_assistant(S_CLI, "m4", sidechain=True, agent_id=AGENT_A)])
    write_jsonl(sub_path(S_SDK, AGENT_B), [c_assistant(S_SDK, "m5", sidechain=True, agent_id=AGENT_B,
                                                       entrypoint="sdk-cli")])
    run()
    total = claude_tokens()
    inter = claude_tokens("kind='main' AND origin='interaktiv'")
    orch = claude_tokens("kind='main' AND origin='orchestrator'")
    sub = claude_tokens("kind='subagent'")
    unknown = claude_tokens("kind='main' AND origin='unbekannt'")
    assert [a + b + c + d for a, b, c, d in zip(inter, orch, sub, unknown, strict=True)] == list(total)
    assert (inter[4], orch[4], sub[4]) == (2, 1, 2)
    assert q("SELECT origin, count(*) FROM claude_msg WHERE kind='subagent' GROUP BY origin ORDER BY origin") == [
        ("interaktiv", 1), ("orchestrator", 1)]


def test_synthetic_model_is_not_counted_and_families_are_open():
    write_jsonl(main_path(S_CLI), [
        c_header(S_CLI),
        c_assistant(S_CLI, "m_syn", model="<synthetic>"),
        c_assistant(S_CLI, "m_op", model="claude-opus-5"),
        c_assistant(S_CLI, "m_so", model="claude-sonnet-5-5"),
        c_assistant(S_CLI, "m_ha", model="claude-haiku-4-5-20251001"),
        c_assistant(S_CLI, "m_fa", model="claude-fable-5-1"),
        c_assistant(S_CLI, "m_xx", model="gpt-irgendwas"),
    ])
    run()
    assert sorted(q("SELECT msg_id, family FROM claude_msg")) == [
        ("m_fa", "fable"), ("m_ha", "haiku"), ("m_op", "opus"), ("m_so", "sonnet"), ("m_xx", "andere")]


def test_agent_calls_are_counted_from_tool_use_and_meta_files_separately():
    write_jsonl(main_path(S_CLI), [
        c_header(S_CLI),
        c_agent_call(S_CLI, "m1", "toolu_1", subagent_type="Explore", model="sonnet"),
        c_agent_call(S_CLI, "m2", "toolu_2", name="Task", subagent_type="Plan", model=None),
        c_agent_call(S_CLI, "m3", "toolu_3", subagent_type="author", model="opus"),  # refused: no meta file
        c_agent_call(S_CLI, "m1", "toolu_1", subagent_type="Explore", model="sonnet"),  # split line, same id
    ])
    write_meta(S_CLI, AGENT_A, toolUseId="toolu_1", agentType="Explore", model="sonnet")
    write_meta(S_CLI, AGENT_B, toolUseId="toolu_2", agentType="Plan", parentAgentId=AGENT_A)
    data = json.loads(meta_path(S_CLI, AGENT_B).read_text(encoding="utf-8"))
    del data["model"]  # model missing → the default
    meta_path(S_CLI, AGENT_B).write_text(_j(data), encoding="utf-8")
    run()
    assert sorted(q("SELECT tool_use_id, tool_name, subagent_type, model_req, origin FROM claude_agent_call")) == [
        ("toolu_1", "Agent", "Explore", "sonnet", "interaktiv"),
        ("toolu_2", "Task", "Plan", "Standard", "interaktiv"),
        ("toolu_3", "Agent", "author", "opus", "interaktiv"),
    ]
    assert sorted(q("SELECT agent_id, tool_use_id, agent_type, model, nested FROM claude_agent_meta")) == [
        (AGENT_A, "toolu_1", "Explore", "sonnet", 0),
        (AGENT_B, "toolu_2", "Plan", "Standard", 1),
    ]
    # Kontrollzahl: 3 calls from tool_use against 2 meta files — one call never ran.
    assert q("SELECT count(*) FROM claude_agent_call")[0][0] - q("SELECT count(*) FROM claude_agent_meta")[0][0] == 1


def test_cost_state_last_line_per_session_counts():
    start_ms = int(datetime(2026, 10, 8, 12, 0).timestamp() * 1000)
    write_jsonl(main_path(S_CLI), [
        c_header(S_CLI), c_cost(S_CLI, 10.0, start_ms), c_assistant(S_CLI, "m1"),
        c_cost(S_CLI, 97.76, start_ms),
    ])
    run()
    assert q("SELECT session_id, cost_usd, day, origin FROM claude_cost") == [
        (S_CLI, 97.76, "2026-10-08", "interaktiv")]


# ── Claude: robustness of the line reader ───────────────────────────────────


def test_broken_line_is_skipped_counted_and_the_reader_goes_on():
    write_jsonl(main_path(S_CLI), [
        c_header(S_CLI), '{"type":"assistant","message":{"id":"broken"', c_assistant(S_CLI, "m_after"),
    ])
    summary = run()
    assert q("SELECT msg_id FROM claude_msg") == [("m_after",)]
    assert summary["per_source"]["claude"]["lines_skipped"] == 1
    assert summary["lines_skipped"] == 1


def test_unknown_fields_are_ignored_and_missing_fields_are_null():
    line = c_assistant(S_CLI, "m1", usage={"input_tokens": 7, "brand_new_counter": 3})
    line["someFutureField"] = {"nested": [1, 2]}
    line["message"]["anotherNewField"] = "x"
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), line])
    summary = run()
    assert q("SELECT input, output, cache_read, cache_write FROM claude_msg") == [(7, None, None, None)]
    assert summary["lines_skipped"] == 0


def test_half_last_line_waits_and_then_counts_exactly_once():
    path = main_path(S_CLI)
    full = _j(c_assistant(S_CLI, "m_tail"))
    write_jsonl(path, [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(full[: len(full) // 2])  # the writer is mid-line
    first = run()
    assert q("SELECT msg_id FROM claude_msg") == [("m1",)]
    assert first["lines_skipped"] == 0
    state_offset = q("SELECT offset FROM file_state WHERE source='claude'")[0][0]
    assert state_offset == len((_j(c_header(S_CLI)) + "\n" + _j(c_assistant(S_CLI, "m1")) + "\n").encode())
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(full[len(full) // 2:] + "\n")
    second = run()
    assert sorted(q("SELECT msg_id FROM claude_msg")) == [("m1",), ("m_tail",)]
    assert second["lines_read"] == 1
    third = run()
    assert third["lines_read"] == 0
    assert claude_tokens()[4] == 2


def test_quiet_complete_tail_without_newline_is_evaluated_once_and_the_offset_moves_to_the_end():
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m_tail")], newline_at_end=False)
    old = time.time() - 3600
    os.utime(path, (old, old))
    run()
    assert q("SELECT msg_id FROM claude_msg") == [("m_tail",)]
    assert q("SELECT offset, size FROM file_state WHERE source='claude'") == [(path.stat().st_size,) * 2]
    append_jsonl(path, ["", _j(c_assistant(S_CLI, "m_next"))])  # writer adds the newline later
    run()
    assert sorted(q("SELECT msg_id FROM claude_msg")) == [("m_next",), ("m_tail",)]
    assert claude_tokens()[4] == 2


def test_a_fresh_last_line_without_newline_is_counted_once_after_the_file_got_quiet(monkeypatch):
    """K19: size and mtime of run 1 are what run 2 sees — only the clock moved on (the quiet
    time is shortened between the runs; touching the file would hide the bug)."""
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m_tail")], newline_at_end=False)
    stat_1 = path.stat()
    first = run()  # the file was written just now: the writer may still be mid-line
    assert q("SELECT msg_id FROM claude_msg") == []
    assert first["per_source"]["claude"]["lines_skipped"] == 0
    monkeypatch.setattr(hi, "TAIL_QUIET_SEC", 0)
    second = run()
    assert q("SELECT msg_id FROM claude_msg") == [("m_tail",)]
    assert second["per_source"]["claude"]["lines_read"] == 1
    third = run()
    assert third["per_source"]["claude"]["lines_read"] == 0
    assert third["per_source"]["claude"]["files_read"] == 0
    assert claude_tokens()[4] == 1
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (stat_1.st_size, stat_1.st_mtime_ns)


def test_a_broken_rest_of_a_quiet_file_is_skipped_once_and_the_file_is_then_left_alone(monkeypatch):
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write('{"type":"assistant","message":{"id":"m_half')  # never completed
    run()  # young: the rest may still grow
    monkeypatch.setattr(hi, "TAIL_QUIET_SEC", 0)
    second = run()
    assert second["per_source"]["claude"]["lines_skipped"] == 1
    assert q("SELECT offset, size FROM file_state WHERE source='claude'") == [(path.stat().st_size,) * 2]
    opened = []
    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (opened.append(self), real_open(self, *a, **k))[1])
    third = run()
    assert third["per_source"]["claude"]["lines_skipped"] == 0
    assert path not in opened
    assert [r[0] for r in q("SELECT msg_id FROM claude_msg")] == ["m1"]


def test_a_quiet_rest_completed_later_is_skipped_in_both_halves_and_never_counted_twice(monkeypatch):
    """R4-04, known limit: the writer finishes the rest only after the quiet time."""
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write('{"type":"assistant","message":{"id":"m_half')
    monkeypatch.setattr(hi, "TAIL_QUIET_SEC", 0)
    first = run()
    assert first["per_source"]["claude"]["lines_skipped"] == 1  # first half
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write('_rest","usage":{}}}\n' + _j(c_assistant(S_CLI, "m_next")) + "\n")
    second = run()
    assert second["per_source"]["claude"]["lines_skipped"] == 1  # second half, now a complete line
    assert sorted(r[0] for r in q("SELECT msg_id FROM claude_msg")) == ["m1", "m_next"]
    assert claude_tokens()[4] == 2  # nothing twice
    assert run()["per_source"]["claude"]["lines_skipped"] == 0


def test_non_compact_json_is_still_parsed_despite_the_prefilter():
    spaced = json.dumps(c_assistant(S_CLI, "m_spaced"))  # default separators: ", " and ": "
    assert '"type": "assistant"' in spaced
    write_jsonl(main_path(S_CLI), [json.dumps(c_header(S_CLI)), spaced])
    run()
    assert q("SELECT msg_id, origin FROM claude_msg") == [("m_spaced", "interaktiv")]


def test_second_run_without_change_reads_zero_lines_and_changes_nothing():
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1"), c_cost(S_CLI, 1.5)])
    write_jsonl(sub_path(S_CLI, AGENT_A), [c_assistant(S_CLI, "m2", sidechain=True, agent_id=AGENT_A)])
    write_meta(S_CLI, AGENT_A)
    write_codex_rollout()
    write_ledger()
    write_markers()
    make_opencode_db()
    first = run()
    assert first["lines_read"] > 0
    before = dump_tables()
    second = run()
    assert second["lines_read"] == 0
    assert second["bytes_read"] == 0
    assert second["files_read"] == 0
    assert dump_tables() == before


def test_appended_lines_only_are_read_and_counted():
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1"), c_assistant(S_CLI, "m2")])
    run()
    append_jsonl(path, [c_assistant(S_CLI, "m3"), c_assistant(S_CLI, "m2", block=1)])
    second = run()
    assert second["lines_read"] == 2
    assert claude_tokens()[4] == 3


def test_file_replaced_by_a_smaller_one_is_reread_without_double_counting():
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), *[c_assistant(S_CLI, f"m{i}") for i in range(6)]])
    run()
    # rewritten: same head line, fewer lines → size < stored offset
    write_jsonl(path, [c_header(S_CLI), c_assistant(S_CLI, "m0"), c_assistant(S_CLI, "m_new")])
    second = run()
    assert second["lines_read"] == 3  # read from the start
    assert sorted(r[0] for r in q("SELECT msg_id FROM claude_msg")) == sorted(
        [f"m{i}" for i in range(6)] + ["m_new"])
    assert claude_tokens()[0] == 2 * 7


def test_file_replaced_by_a_larger_one_with_another_head_is_reread_without_double_counting():
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    run()
    other_head = c_header(S_CLI, ts="2026-10-09T09:00:00.000Z")
    write_jsonl(path, [other_head, c_assistant(S_CLI, "m1"), *[c_assistant(S_CLI, f"n{i}") for i in range(5)]])
    second = run()
    assert second["lines_read"] == 7
    assert claude_tokens()[4] == 6  # m1 once + n0..n4


def test_file_rewritten_in_place_with_the_same_head_is_reread_from_the_start():
    """Same first 256 bytes, larger, different body: the stored offset would land
    in the middle of a new line and everything in front of it would be missed."""
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1"), c_assistant(S_CLI, "m2")])
    run()
    head = _j(c_header(S_CLI))
    assert len(head.encode()) > hi.HEAD_BYTES  # the head fingerprint cannot see the change
    write_jsonl(path, [c_header(S_CLI), c_assistant(S_CLI, "x1", usage={"input_tokens": 1000}),
                       c_assistant(S_CLI, "x2"), c_assistant(S_CLI, "x3"), c_assistant(S_CLI, "m2")])
    second = run()
    assert second["lines_skipped"] == 0  # no half line was parsed
    assert sorted(r[0] for r in q("SELECT msg_id FROM claude_msg")) == ["m1", "m2", "x1", "x2", "x3"]
    assert q("SELECT input FROM claude_msg WHERE msg_id='x1'") == [(1000,)]


def test_deleted_file_keeps_its_rows():
    write_jsonl(main_path(S_SDK), [c_header(S_SDK, "sdk-cli"), c_assistant(S_SDK, "m1", entrypoint="sdk-cli")])
    run()
    main_path(S_SDK).unlink()  # heartbeat._check_session_cleanup after 14 days
    run()
    assert q("SELECT msg_id, origin FROM claude_msg") == [("m1", "orchestrator")]


def test_files_older_than_the_window_are_skipped(monkeypatch):
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m_old")])
    old = (datetime.now() - timedelta(days=91)).timestamp()
    os.utime(path, (old, old))
    summary = run()
    assert claude_tokens()[4] == 0
    assert summary["per_source"]["claude"]["files_skipped_window"] == 1


def test_days_are_local_calendar_days():
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only")
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Auckland"  # UTC+13 in October
    time.tzset()
    try:
        assert hi._local_day_from_iso("2026-10-08T15:00:00.000Z") == "2026-10-09"
        assert hi._local_day_from_iso("2026-10-08T19:25:16+02:00") == "2026-10-09"
        assert hi._local_day_from_iso("2026-10-08T10:00:00") == "2026-10-08"  # naive = local
        ms = int(datetime(2026, 10, 8, 23, 30).timestamp() * 1000)  # local wall clock
        assert hi._local_day_from_ms(ms) == "2026-10-08"
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


# ── Codex ───────────────────────────────────────────────────────────────────


def _rl(primary_used=7.0, secondary_used=12.0, primary_resets=1791490181, secondary_resets=1791962514):
    return {"limit_id": "codex", "limit_name": None,
            "primary": {"used_percent": primary_used, "window_minutes": 300, "resets_at": primary_resets},
            "secondary": {"used_percent": secondary_used, "window_minutes": 10080, "resets_at": secondary_resets},
            "credits": {"has_credits": False}, "plan_type": "plus"}


def _tc(ts, inp, out, *, info=True, rl=None):
    usage = {"input_tokens": inp, "cached_input_tokens": inp // 2, "cache_write_input_tokens": 0,
             "output_tokens": out, "reasoning_output_tokens": 1, "total_tokens": inp + out}
    return {"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"total_token_usage": usage, "last_token_usage": usage, "model_context_window": 258400}
        if info else None,
        "rate_limits": rl or _rl()}}


def codex_path(name: str = "rollout-2026-10-08T17-25-30-01a11c8c.jsonl") -> Path:
    return Path(config.HARNESS_CODEX_SESSIONS_DIR) / "2026" / "10" / "08" / name


def write_codex_rollout(path=None, *, thread_source="user", extra=()):
    path = path or codex_path()
    return write_jsonl(path, [
        {"timestamp": "2026-10-08T17:25:30.689Z", "type": "session_meta", "payload": {
            "id": "01a11c8c-0000-4000-8000-000000000001", "timestamp": "2026-10-08T17:25:30.400Z",
            "cwd": CWD, "originator": "codex_exec", "cli_version": "CANARY_CLI",
            "source": "exec", "thread_source": thread_source, "model_provider": "openai",
            "base_instructions": {"text": "CANARY_INSTRUCTIONS"},
            "git": {"commit_hash": "CANARY_HASH", "repository_url": "CANARY_REPO_URL"}}},
        {"timestamp": "2026-10-08T17:25:31.911Z", "type": "turn_context", "payload": {
            "turn_id": "x", "cwd": CWD, "approval_policy": "CANARY", "model": "gpt-6.1-sol",
            "summary": "CANARY_SUMMARY"}},
        {"timestamp": "2026-10-08T17:25:32.000Z", "type": "response_item",
         "payload": {"type": "message", "content": [{"type": "output_text", "text": "CANARY_ANSWER"}]}},
        _tc("2026-10-08T17:25:36.310Z", 100, 10),
        _tc("2026-10-08T17:25:40.000Z", 0, 0, info=False, rl=_rl(8.0, 13.0)),
        {"timestamp": "2026-10-08T17:25:41.000Z", "type": "token_usage_record",
         "payload": {"usage": {"input_tokens": 999999}}},
        _tc("2026-10-08T17:25:50.000Z", 300, 30),
        _tc("2026-10-08T17:26:00.000Z", 600, 60, rl=_rl(9.0, 14.0)),
        *extra,
    ])


def test_codex_takes_the_last_cumulative_token_count_not_a_sum():
    write_codex_rollout()
    run()
    row = q("SELECT input, cached_input, output, total, model, thread_source, originator, day "
            "FROM codex_rollout")
    assert row == [(600, 300, 60, 660, "gpt-6.1-sol", "user", "codex_exec", hi._local_day_from_iso(
        "2026-10-08T17:25:30.400Z"))]


def test_codex_info_null_does_not_disturb_and_rate_limits_are_the_newest():
    write_codex_rollout(extra=[_tc("2026-10-08T17:27:00.000Z", 0, 0, info=False, rl=_rl(11.0, 15.0))])
    run()
    assert q("SELECT input, primary_used, secondary_used, primary_window, secondary_window, "
             "secondary_resets, rl_ts FROM codex_rollout") == [
        (600, 11.0, 15.0, 300, 10080, 1791962514, "2026-10-08T17:27:00.000Z")]


def test_codex_append_broken_half_and_extra_fields():
    path = write_codex_rollout()
    run()
    future = _tc("2026-10-08T17:30:00.000Z", 900, 90)
    future["payload"]["info"]["brand_new"] = {"x": 1}
    append_jsonl(path, ['{"timestamp":"broken', future])
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(_j(_tc("2026-10-08T17:31:00.000Z", 1200, 120))[:40])  # half line
    second = run()
    assert second["per_source"]["codex"]["lines_skipped"] == 1
    assert q("SELECT input, output FROM codex_rollout") == [(900, 90)]


class _BeginHook:
    """A connection whose first BEGIN IMMEDIATE is preceded by ``before_begin()``."""

    def __init__(self, conn, before_begin):
        self._conn, self._before_begin, self.fired = conn, before_begin, False

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE" and not self.fired:
            self.fired = True
            self._before_begin()
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.mark.parametrize("window", ["before_state_read", "before_begin"])
def test_two_interleaved_codex_runs_end_on_the_newer_counter(monkeypatch, window):
    """K21: run B commits (counter 990) while run A is on its way into its transaction. A built
    on the counter it had read before (660) and wrote it back: 660 -> 990 -> 660. The two
    windows are the ones between the old reads: row before file state, and reads before BEGIN."""
    path = write_codex_rollout()
    run()
    assert q("SELECT total FROM codex_rollout") == [(660,)]
    append_jsonl(path, [_tc("2026-10-08T17:30:00.000Z", 900, 90)])
    root = Path(config.HARNESS_CODEX_SESSIONS_DIR)
    fired = []

    def second_run_in_between():
        fired.append(1)
        conn_b = sqlite3.connect(str(config.HARNESS_DB_FILE), timeout=30, isolation_level=None)
        try:
            hi._codex_file(conn_b, root, path, hi.SourceStats())
        finally:
            conn_b.close()
        # a later line that carries no counter (info: null) — all that is left for A to read
        append_jsonl(path, [_tc("2026-10-08T17:31:00.000Z", 0, 0, info=False, rl=_rl(11.0, 15.0))])

    conn_a = sqlite3.connect(str(config.HARNESS_DB_FILE), timeout=30, isolation_level=None)
    try:
        if window == "before_state_read":
            real_load = hi._load_state

            def load(conn, source, file_key):
                if source == "codex" and not fired:
                    second_run_in_between()
                return real_load(conn, source, file_key)

            monkeypatch.setattr(hi, "_load_state", load)
            hi._codex_file(conn_a, root, path, hi.SourceStats())
        else:
            hi._codex_file(_BeginHook(conn_a, second_run_in_between), root, path, hi.SourceStats())
    finally:
        conn_a.close()
    assert fired
    assert q("SELECT input, output, total, primary_used FROM codex_rollout") == [(900, 90, 990, 11.0)]


def test_codex_subagent_thread_source_is_kept():
    write_codex_rollout(codex_path("rollout-2026-10-08T18-00-00-02b22c9d.jsonl"), thread_source="subagent")
    run()
    assert q("SELECT thread_source FROM codex_rollout") == [("subagent",)]


# ── opencode.db ─────────────────────────────────────────────────────────────

_OC_SCHEMA = """
CREATE TABLE `project` (`id` text PRIMARY KEY);
CREATE TABLE `session` (
  `id` text PRIMARY KEY, `project_id` text NOT NULL, `parent_id` text, `slug` text NOT NULL,
  `directory` text NOT NULL, `title` text NOT NULL, `version` text NOT NULL, `share_url` text,
  `summary_additions` integer, `summary_deletions` integer, `summary_files` integer, `summary_diffs` text,
  `revert` text, `permission` text,
  `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `time_compacting` integer, `time_archived` integer,
  `workspace_id` text, `path` text, `agent` text, `model` text,
  `cost` real DEFAULT 0 NOT NULL, `tokens_input` integer DEFAULT 0 NOT NULL, `tokens_output` integer DEFAULT 0 NOT NULL,
  `tokens_reasoning` integer DEFAULT 0 NOT NULL, `tokens_cache_read` integer DEFAULT 0 NOT NULL,
  `tokens_cache_write` integer DEFAULT 0 NOT NULL, `metadata` text);
CREATE TABLE `message` (
  `id` text PRIMARY KEY, `session_id` text NOT NULL,
  `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL);
CREATE INDEX `message_session_time_created_id_idx` ON `message` (`session_id`,`time_created`,`id`);
CREATE TABLE `part` (`id` text PRIMARY KEY, `message_id` text, `data` text);
"""


def _now_ms(offset_sec=0):
    return int((time.time() + offset_sec) * 1000)


def oc_assistant(cost=0.00528193008, model="zdr-review", t=None, inp=643):
    t = t or _now_ms(-600)
    return {"parentID": "msg_p", "role": "assistant", "mode": "extern-review", "agent": "extern-review",
            "path": {"cwd": CWD, "root": CWD}, "cost": cost,
            "tokens": {"total": 117548, "input": inp, "output": 2813, "reasoning": 1196,
                       "cache": {"write": 0, "read": 112896}},
            "modelID": model, "providerID": "openrouter",
            "time": {"created": t, "completed": t + 50_000}, "finish": "tool-calls"}


def make_opencode_db(path=None, *, keep_open=False):
    path = Path(path or config.HARNESS_OPENCODE_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_OC_SCHEMA)
    t = _now_ms(-900)
    conn.execute(
        "INSERT INTO session(id, project_id, parent_id, slug, directory, title, version, summary_diffs, "
        "time_created, time_updated, agent, model, cost, tokens_input, tokens_output, tokens_reasoning, "
        "tokens_cache_read, tokens_cache_write) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ses_parent", "p1", None, "CANARY_SLUG", "C:\\CANARY_OC_DIR\\beispiel", "CANARY_OC_TITLE", "1.18.35",
         "CANARY_DIFFS", t, t, "extern-review",
         '{"id":"zdr-review","providerID":"openrouter","variant":"default"}', 0.063, 105164, 5681, 6589,
         1028608, 0))
    conn.execute(
        "INSERT INTO session(id, project_id, parent_id, slug, directory, title, version, time_created, "
        "time_updated, agent, model) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("ses_child", "p1", "ses_parent", "s", "C:\\CANARY_OC_DIR\\x", "CANARY_OC_TITLE2", "1.18.35", t, t,
         None, "not json"))
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                 ("msg_u", "ses_parent", t, t, _j({"role": "user", "time": {"created": t}, "agent": "extern-review",
                                                    "model": {"providerID": "openrouter", "modelID": "zdr-review"},
                                                    "summary": {"diffs": [], "title": "CANARY_USER_TITLE"}})))
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)", ("msg_a1", "ses_parent", t, t + 1, _j(oc_assistant())))
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                 ("msg_a2", "ses_child", t, t + 2, _j(oc_assistant(0.01, "z-ai/glm-5.3-flash"))))
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)", ("msg_bad", "ses_child", t, t + 3, "{broken"))
    conn.execute("INSERT INTO part VALUES (?,?,?)", ("prt_1", "msg_a1", '{"text":"CANARY_PART_TEXT"}'))
    conn.commit()
    if keep_open:
        return conn
    conn.close()
    return None


def test_opencode_sessions_and_messages_are_indexed():
    make_opencode_db()
    summary = run()
    assert sorted(q("SELECT id, parent_id, agent, model_id, provider_id, cost FROM oc_session")) == [
        ("ses_child", "ses_parent", None, None, None, 0.0),
        ("ses_parent", None, "extern-review", "zdr-review", "openrouter", 0.063),
    ]
    assert sorted(q("SELECT id, role, model_id, cost, t_input, t_cache_read, finish FROM oc_message "
                    "WHERE role IS NOT NULL")) == [
        ("msg_a1", "assistant", "zdr-review", 0.00528193008, 643, 112896, "tool-calls"),
        ("msg_a2", "assistant", "z-ai/glm-5.3-flash", 0.01, 643, 112896, "tool-calls"),
        ("msg_u", "user", None, None, None, None, None),
    ]
    assert summary["per_source"]["opencode"]["lines_skipped"] == 2  # broken message + non-JSON session model


def test_opencode_is_opened_read_only_and_never_touches_part(monkeypatch):
    path = Path(config.HARNESS_OPENCODE_DB)
    make_opencode_db()
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    tables_read: set[str] = set()
    write_attempt: list[str] = []
    real_open = hi._open_opencode_ro

    def spy_open(p):
        conn = real_open(p)

        def auth(action, arg1, arg2, _db, _trigger):
            if action == sqlite3.SQLITE_READ:
                tables_read.add(arg1)
            return sqlite3.SQLITE_OK

        try:
            conn.execute("INSERT INTO project(id) VALUES ('x')")
        except sqlite3.OperationalError as e:
            write_attempt.append(str(e))
        conn.set_authorizer(auth)
        return conn

    monkeypatch.setattr(hi, "_open_opencode_ro", spy_open)
    run()
    assert write_attempt and ("readonly" in write_attempt[0] or "query_only" in write_attempt[0])
    assert "part" not in tables_read
    assert {"session", "message"} <= tables_read
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before


def test_opencode_sees_rows_still_in_the_wal():
    """`immutable=1` would ignore the WAL and miss rows opencode has not checkpointed."""
    writer = make_opencode_db(keep_open=True)
    assert writer is not None
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        t = _now_ms(-60)
        writer.execute("INSERT INTO message VALUES (?,?,?,?,?)", ("msg_wal", "ses_parent", t, t, _j(oc_assistant())))
        writer.commit()
        assert Path(str(config.HARNESS_OPENCODE_DB) + "-wal").stat().st_size > 0
        run()
        assert q("SELECT count(*) FROM oc_message WHERE id='msg_wal'") == [(1,)]
    finally:
        writer.close()


def test_opencode_second_run_reads_nothing_and_an_update_is_read_once():
    make_opencode_db()
    run()
    assert run()["per_source"]["opencode"]["lines_read"] == 0
    conn = sqlite3.connect(str(config.HARNESS_OPENCODE_DB))
    t = _now_ms()
    conn.execute("UPDATE message SET time_updated=?, data=? WHERE id='msg_a1'", (t, _j(oc_assistant(0.5))))
    conn.commit()
    conn.close()
    third = run()
    assert third["per_source"]["opencode"]["lines_read"] == 1
    assert q("SELECT cost FROM oc_message WHERE id='msg_a1'") == [(0.5,)]
    assert run()["per_source"]["opencode"]["lines_read"] == 0


# ── ledger and markers ──────────────────────────────────────────────────────


def write_ledger(extra=()):
    path = Path(config.HARNESS_EXTERN_LEDGER)
    return write_jsonl(path, [
        {"ts_local": "2026-10-08T19:25:16+02:00", "voice": "codex", "status": "ok", "blocked_until": None,
         "repo": "CANARY_REPO_FOLDER", "tokens": 59012, "note": ""},
        {"ts_local": "2026-09-22T10:19:31+02:00", "voice": "opencode", "status": "stillstand",
         "blocked_until": None, "repo": "C:\\CANARY_LEDGER_PATH\\CANARY_LEDGER_FOLDER", "tokens": None,
         "note": "CANARY_LEDGER_NOTE"},
        {"ts_local": "2026-09-22T10:11:28+02:00", "voice": "codex", "status": "limit",
         "blocked_until": "2026-09-24T09:17:00+02:00", "repo": "CANARY_REPO_FOLDER", "tokens": None,
         "note": "Nutzungslimit erreicht (codex)", "neues_feld": 1},
        *extra,
    ])


def _ledger_line(ts="2026-10-08T19:25:16+02:00", voice="codex", status="ok", tokens=59012, repo="r"):
    return {"ts_local": ts, "voice": voice, "status": status, "blocked_until": None, "repo": repo,
            "tokens": tokens, "note": ""}


def test_ledger_rows_keep_no_text_and_count_bad_lines():
    write_ledger(extra=['{"ts_local":"broken', {"voice": "codex"}])
    summary = run()
    rows = q("SELECT line_no, ts_local, voice, status, tokens, day FROM ledger ORDER BY line_no")
    assert rows == [
        (1, "2026-10-08T19:25:16+02:00", "codex", "ok", 59012, hi._local_day_from_iso("2026-10-08T19:25:16+02:00")),
        (2, "2026-09-22T10:19:31+02:00", "opencode", "stillstand", None,
         hi._local_day_from_iso("2026-09-22T10:19:31+02:00")),
        (3, "2026-09-22T10:11:28+02:00", "codex", "limit", None, hi._local_day_from_iso("2026-09-22T10:11:28+02:00")),
    ]
    # K18: a stock of the file as it is now, not lines_skipped (summed over runs)
    assert (summary["per_source"]["ledger"]["broken_lines"], summary["per_source"]["ledger"]["lines_skipped"]) == (2, 0)


def test_ledger_two_calls_in_the_same_second_are_two_rows():
    """Measured on real data: 4 keys (second, voice, repo) carried two lines each,
    2 of them Codex calls with different tokens — both are real calls."""
    write_jsonl(Path(config.HARNESS_EXTERN_LEDGER), [_ledger_line(tokens=100), _ledger_line(tokens=36)])
    run()
    assert q("SELECT count(*), sum(tokens) FROM ledger") == [(2, 136)]


def test_ledger_three_identical_lines_are_three_calls():
    write_jsonl(Path(config.HARNESS_EXTERN_LEDGER), [_ledger_line()] * 3)
    run()
    assert q("SELECT count(*) FROM ledger") == [(3,)]
    append_jsonl(Path(config.HARNESS_EXTERN_LEDGER), [_ledger_line()])  # a fourth call
    run()
    assert q("SELECT count(*) FROM ledger") == [(4,)]


@pytest.mark.parametrize("same_length", [True, False], ids=["same_length", "other_length"])
def test_ledger_line_edited_in_the_middle_is_picked_up_without_a_ghost(same_length):
    path = write_jsonl(Path(config.HARNESS_EXTERN_LEDGER),
                       [_ledger_line(tokens=111), _ledger_line(status="ok", tokens=222), _ledger_line(tokens=333)])
    run()
    size, mtime = path.stat().st_size, path.stat().st_mtime_ns
    edited = _ledger_line(status="no", tokens=999) if same_length else _ledger_line(status="limit", tokens=None)
    write_jsonl(path, [_ledger_line(tokens=111), edited, _ledger_line(tokens=333)])
    assert (path.stat().st_size == size) is same_length
    if same_length:
        os.utime(path, ns=(mtime, mtime))  # same size AND same mtime: only the content differs
    run()
    rows = q("SELECT line_no, status, tokens FROM ledger ORDER BY line_no")
    assert rows == [(1, "ok", 111), (2, edited["status"], edited["tokens"]), (3, "ok", 333)]


def test_deleted_ledger_keeps_its_rows():
    path = write_jsonl(Path(config.HARNESS_EXTERN_LEDGER), [_ledger_line(), _ledger_line(voice="opencode")])
    run()
    path.unlink()
    run()
    assert q("SELECT count(*) FROM ledger") == [(2,)]


def write_markers(lines=None):
    path = Path(config.HARNESS_CHANGES_FILE)
    return write_jsonl(path, lines or [
        {"date": "2026-09-25", "id": "extern-diaet", "scope": "interaktiv",
         "change": "Externe Stimmen: Stempel-Gate", "expect": "externe Aufrufe je Loop sinken, Fehlversuche sinken",
         "source": "CANARY_MARKER_SOURCE"},
        {"date": "2026-10-08", "id": "oc-endpunktpreise-lesebeleg", "scope": "beide",
         "change": "opencode-Waehler rechnet mit Endpunktpreisen",
         "expect": "opencode-Kosten je Pass sinken, Sessions je Pass = 1", "source": "CANARY_MARKER_SOURCE"},
    ])


def test_markers_are_replaced_when_the_file_changes_and_kept_when_it_is_deleted():
    path = write_markers()
    run()
    assert sorted(q("SELECT date, id FROM marker")) == [
        ("2026-09-25", "extern-diaet"), ("2026-10-08", "oc-endpunktpreise-lesebeleg")]
    write_markers([{"date": "2026-09-26", "id": "extern-diaet", "scope": "interaktiv", "change": "x",
                    "expect": "externe Aufrufe sinken"}, "{broken"])
    summary = run()
    assert q("SELECT date, id FROM marker") == [("2026-09-26", "extern-diaet")]
    assert summary["per_source"]["markers"]["broken_lines"] == 1  # K18: stock, not lines_skipped
    path.unlink()
    run()
    assert q("SELECT date, id FROM marker") == [("2026-09-26", "extern-diaet")]


# ── privacy ─────────────────────────────────────────────────────────────────


def test_no_prompt_answer_title_or_path_text_reaches_the_index():
    write_jsonl(main_path(S_CLI), [
        {"type": "queue-operation", "content": "CANARY_QUEUE_TEXT"},
        {"type": "ai-title", "title": "CANARY_AI_TITLE"},
        {"type": "last-prompt", "lastPrompt": "CANARY_LAST_PROMPT"},
        c_header(S_CLI),
        c_assistant(S_CLI, "m1", content=[{"type": "text", "text": "CANARY_ANSWER_TEXT"}]),
        c_agent_call(S_CLI, "m2", "toolu_1"),
        {"type": "attachment", "content": "CANARY_ATTACHMENT", "entrypoint": "cli"},
        c_cost(S_CLI, 1.0),
    ])
    write_jsonl(sub_path(S_CLI, AGENT_A), [
        c_assistant(S_CLI, "m3", sidechain=True, agent_id=AGENT_A,
                    content=[{"type": "text", "text": "CANARY_SUBAGENT_ANSWER"}]),
    ])
    write_meta(S_CLI, AGENT_A)
    write_jsonl(main_path(S_BRIDGE), [{"type": "bridge-session", "sessionId": S_BRIDGE,
                                       "bridgeSessionId": "CANARY_BRIDGE"}])
    write_codex_rollout(extra=[{"timestamp": "2026-10-08T17:30:00Z", "type": "event_msg",
                                "payload": {"type": "user_message", "message": "CANARY_CODEX_PROMPT"}}])
    make_opencode_db()
    write_ledger()
    write_markers()
    summary = run()
    assert summary["status"] == "ok", summary
    db_path = Path(config.HARNESS_DB_FILE)
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            blob += p.read_bytes()
    assert blob, "index file missing"
    assert b"CANARY" not in blob  # covers the folder names too (K9: only the cwd hash is kept)
    assert PROJECT_DIR.encode() not in blob  # the encoded cwd directory name is a path too
    # the hash of the cwd is what stays
    assert hi._hash(CWD).encode() in blob


def test_index_run_is_logged_with_counts():
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1"), "{broken"])
    summary = run()
    rows = q("SELECT files_read, lines_read, lines_skipped, status, per_source FROM index_runs")
    assert len(rows) == 1
    files_read, lines_read, skipped, status, per_source = rows[0]
    assert (files_read, lines_read, skipped, status) == (1, 3, 1, "ok")
    assert json.loads(per_source)["claude"]["lines_skipped"] == 1
    assert any("1 übersprungen" in line for line in summary["_out"])


def test_suite_paths_never_point_at_real_sources(tmp_path):
    home = Path.home()
    real = [home / ".claude", home / ".codex", home / ".local" / "share" / "opencode",
            REPO / "logs"]
    for key in ("HARNESS_DB_FILE", "HARNESS_CLAUDE_PROJECTS_DIR", "HARNESS_OPENCODE_DB",
                "HARNESS_CODEX_SESSIONS_DIR", "HARNESS_EXTERN_LEDGER", "HARNESS_CHANGES_FILE"):
        value = Path(getattr(config, key))
        assert value.is_relative_to(tmp_path), (key, value)
        assert os.environ[key] == str(value)
        assert not any(value.is_relative_to(r) for r in real), (key, value)


def test_relative_harness_paths_are_taken_relative_to_the_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_DB_FILE", "logs/other.sqlite")
    assert config._harness_path("HARNESS_DB_FILE", tmp_path / "x") == REPO / "logs" / "other.sqlite"
    monkeypatch.setenv("HARNESS_DB_FILE", str(tmp_path / "abs.sqlite"))
    assert config._harness_path("HARNESS_DB_FILE", tmp_path / "x") == tmp_path / "abs.sqlite"
    monkeypatch.delenv("HARNESS_DB_FILE")
    assert config._harness_path("HARNESS_DB_FILE", tmp_path / "x") == tmp_path / "x"


# ── lock and child process ──────────────────────────────────────────────────


def _lock_path() -> Path:
    return hi.lock_path_for(Path(config.HARNESS_DB_FILE))


def _child_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def _run_cli() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "harness_index", "--update"],
        cwd=str(REPO), env=_child_env(), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120, check=False,
    )


def test_a_live_lock_held_by_another_process_ends_the_run_cleanly():
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    holder_code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(REPO)!r})
        from pathlib import Path
        import harness_index as hi
        lock = hi.IndexLock(Path({str(_lock_path())!r}), 7200)
        assert lock.acquire()
        print("HELD", flush=True)
        sys.stdin.readline()
        lock.release()
    """)
    holder = subprocess.Popen([sys.executable, "-c", holder_code], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, encoding="utf-8", env=_child_env())
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"
        busy = _run_cli()
        assert busy.returncode == 0, busy.stderr
        assert "läuft bereits" in busy.stdout
        assert not Path(config.HARNESS_DB_FILE).exists()  # did no work at all
        assert _lock_path().exists()  # and did not remove the foreign lock
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(30)
    assert not _lock_path().exists()
    done = _run_cli()
    assert done.returncode == 0, done.stderr
    assert "läuft bereits" not in done.stdout
    assert q("SELECT msg_id FROM claude_msg") == [("m1",)]


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(30)
    return proc.pid


def test_a_lock_of_a_dead_process_is_taken_over():
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    _lock_path().write_text(_j({"pid": _dead_pid(), "started": time.time(), "token": "old"}), encoding="utf-8")
    summary = run()
    assert summary["status"] == "ok"
    assert any("Veralteten Lock übernommen" in line for line in summary["_out"])
    assert not _lock_path().exists()
    assert claude_tokens()[4] == 1


def test_a_lock_older_than_the_stale_limit_is_taken_over_even_if_the_pid_lives(monkeypatch):
    monkeypatch.setattr(config, "HARNESS_LOCK_STALE_SEC", 100)
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    _lock_path().write_text(_j({"pid": os.getppid(), "started": time.time() - 101, "token": "old"}),
                            encoding="utf-8")
    assert run()["status"] == "ok"


def test_a_fresh_live_lock_is_respected_in_process(monkeypatch):
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    _lock_path().write_text(_j({"pid": os.getppid(), "started": time.time(), "token": "other"}), encoding="utf-8")
    summary = run()
    assert summary["status"] == "busy"
    assert _lock_path().exists()


@pytest.mark.parametrize(("age", "taken"), [(0, False), (3600, True)])
def test_a_half_written_lock_is_taken_over_only_once_it_is_old(age, taken):
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    _lock_path().write_text('{"pid": 12', encoding="utf-8")
    if age:
        old = time.time() - age
        os.utime(_lock_path(), (old, old))
    assert (run()["status"] == "ok") is taken


def test_run_update_subprocess_indexes_in_a_child_with_the_repo_as_cwd():
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    result = hi.run_update_subprocess(timeout=120)
    assert result["returncode"] == 0, result
    assert result.get("error") is None
    assert "Harness-Index ok" in result["stdout"]
    assert q("SELECT msg_id FROM claude_msg") == [("m1",)]


def test_run_update_subprocess_kills_its_own_child_on_timeout():
    result = hi.run_update_subprocess(timeout=0.001)
    assert result["timed_out"] is True
    assert result["returncode"] is not None  # reaped, not left running
    assert "Zeitgrenze" in result["error"]


def test_run_update_subprocess_reports_a_failed_start(monkeypatch):
    def no_start(*a, **kw):
        raise OSError("no interpreter")

    monkeypatch.setattr(hi.subprocess, "Popen", no_start)
    result = hi.run_update_subprocess(timeout=5)
    assert result["returncode"] is None
    assert "Start fehlgeschlagen" in result["error"]


# ── the scheduler thread created by the dashboard autostart ─────────────────


def test_scheduler_runs_the_index_and_warns_once_per_distinct_failure(monkeypatch):
    calls = []
    results = [{"error": "Exit 1: boom"}, {"error": "Exit 1: boom"}, {"returncode": 0, "stdout": "ok"},
               {"error": "Exit 1: other"}]

    def fake_run(*, timeout):
        calls.append(timeout)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(hi, "run_update_subprocess", fake_run)
    monkeypatch.setattr(dashboard, "HARNESS_FIRST_RUN_DELAY_SEC", 0.01)
    monkeypatch.setattr(config, "HARNESS_UPDATE_INTERVAL_SEC", 0.02)

    def no_port(_port):
        raise OSError("every candidate refused")

    monkeypatch.setattr(dashboard, "_bind_server", no_port)  # the index must not depend on the bind
    warnings, infos = [], []
    handle = dashboard.start_autostart(port=1, warn=warnings.append, info=infos.append)
    deadline = time.monotonic() + 10
    while len(calls) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    handle.shutdown()
    assert len(calls) >= 5
    assert calls[0] == float(config.HARNESS_LOCK_STALE_SEC)
    index_warnings = [w for w in warnings if w.startswith("Harness-Index")]
    assert index_warnings == ["Harness-Index: Exit 1: boom — Orchestrator läuft weiter",
                              "Harness-Index: Exit 1: other — Orchestrator läuft weiter"]
    assert "Harness-Index läuft wieder" in infos
    assert any("kein Port frei" in w for w in warnings)
    assert not handle.index_thread.is_alive()


def test_scheduler_survives_a_raising_index_and_is_off_at_interval_zero(monkeypatch):
    def boom(*, timeout):
        raise RuntimeError("cannot even start")

    monkeypatch.setattr(hi, "run_update_subprocess", boom)
    monkeypatch.setattr(dashboard, "HARNESS_FIRST_RUN_DELAY_SEC", 0.01)
    monkeypatch.setattr(config, "HARNESS_UPDATE_INTERVAL_SEC", 0.02)
    monkeypatch.setattr(dashboard, "_bind_server", lambda _p: (_ for _ in ()).throw(OSError("x")))
    warnings = []
    handle = dashboard.start_autostart(port=1, warn=warnings.append, info=lambda _m: None)
    time.sleep(0.2)
    assert handle.index_thread.is_alive()
    handle.shutdown()
    assert [w for w in warnings if w.startswith("Harness-Index")] == [
        "Harness-Index: RuntimeError: cannot even start — Orchestrator läuft weiter"]

    monkeypatch.setattr(config, "HARNESS_UPDATE_INTERVAL_SEC", 0)
    off = dashboard.start_autostart(port=1, warn=lambda _m: None, info=lambda _m: None)
    assert off.index_thread is None
    off.shutdown()


# ── Korrekturrunde 1: K3 robustness ─────────────────────────────────────────


def _proj_path(project: str, session: str) -> Path:
    return claude_root() / project / f"{session}.jsonl"


def test_an_out_of_range_number_skips_one_line_and_never_blocks_the_source():
    """Review probe: a 2**70 token count raised OverflowError out of the line
    handler and ended the whole Claude source — every later file (subagents and
    .meta.json included) was missing on every run."""
    sa, sb, sc = (f"{c}0000000-0000-4000-8000-00000000000{i}" for i, c in enumerate("abc", 1))
    write_jsonl(_proj_path("A", sa), [c_header(sa), c_assistant(sa, "a1")])
    write_jsonl(_proj_path("B", sb), [c_header(sb), c_assistant(sb, "b_big", usage={"input_tokens": 2**70}),
                                       c_assistant(sb, "b_ok")])
    write_jsonl(_proj_path("C", sc), [c_header(sc), c_assistant(sc, "c1")])
    write_jsonl(sub_path(sc, AGENT_A), [c_assistant(sc, "c_sub", sidechain=True, agent_id=AGENT_A)])
    write_meta(sc, AGENT_A)
    first = run()
    assert sorted(r[0] for r in q("SELECT msg_id FROM claude_msg")) == ["a1", "b_ok", "c1", "c_sub"]
    assert q("SELECT count(*) FROM claude_agent_meta") == [(1,)]
    claude = first["per_source"]["claude"]
    assert claude["lines_skipped"] == 1
    assert claude["skipped_by_type"] == {"_OutOfRangeError": 1}
    assert claude["errors"] == []
    second = run()
    assert second["lines_read"] == 0
    assert sorted(r[0] for r in q("SELECT msg_id FROM claude_msg")) == ["a1", "b_ok", "c1", "c_sub"]


def test_any_unexpected_exception_in_a_line_is_skipped_by_type(monkeypatch):
    real = hi.model_family

    def picky(model):
        if model == "claude-explodes":
            raise RuntimeError("unexpected")
        return real(model)

    monkeypatch.setattr(hi, "model_family", picky)
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1"),
                                   c_assistant(S_CLI, "m2", model="claude-explodes"), c_assistant(S_CLI, "m3")])
    summary = run()
    assert sorted(r[0] for r in q("SELECT msg_id FROM claude_msg")) == ["m1", "m3"]
    assert summary["per_source"]["claude"]["skipped_by_type"] == {"RuntimeError": 1}


def test_a_failing_file_is_recorded_and_the_next_file_still_runs(monkeypatch):
    sa, sb = "a0000000-0000-4000-8000-000000000001", "b0000000-0000-4000-8000-000000000002"
    write_jsonl(_proj_path("A", sa), [c_header(sa), c_assistant(sa, "a1")])
    write_jsonl(_proj_path("B", sb), [c_header(sb), c_assistant(sb, "b1")])
    real = hi._claude_main_file

    def fails_for_a(conn, root, path, stats):
        if path.parent.name == "A":
            raise LookupError(f"{path} CANARY_ERROR_PATH")  # not an OSError
        return real(conn, root, path, stats)

    monkeypatch.setattr(hi, "_claude_main_file", fails_for_a)
    summary = run()
    assert q("SELECT msg_id FROM claude_msg") == [("b1",)]
    assert summary["status"] == "partial"
    (err,) = summary["per_source"]["claude"]["errors"]
    assert err.endswith(": LookupError")  # K9: type only, no message, no path
    stored = q("SELECT errors, per_source FROM index_runs")[0]
    assert "CANARY" not in stored[0] and "CANARY" not in stored[1]


def test_a_timestamp_before_1970_skips_the_line_and_the_file_goes_on():
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "old", ts="1969-12-31T23:00:00Z"),
                                   c_assistant(S_CLI, "new")])
    summary = run()
    assert q("SELECT msg_id FROM claude_msg") == [("new",)]
    assert summary["per_source"]["claude"]["lines_skipped"] == 1
    assert hi._local_day_from_iso("1969-12-31T23:00:00Z") is None
    assert hi._local_day_from_iso("1969-12-31T23:00:00") is None


def test_int_bounds_follow_sqlite():
    assert hi._int(2**63 - 1) == 2**63 - 1
    assert hi._int(-(2**63)) == -(2**63)
    with pytest.raises(hi._OutOfRangeError):
        hi._int(2**63)
    with pytest.raises(hi._OutOfRangeError):
        hi._int(1e30)
    assert hi._int(float("nan")) is None
    assert hi._int(True) is None


def test_an_out_of_range_opencode_row_is_skipped_and_the_others_stay():
    make_opencode_db()
    conn = sqlite3.connect(str(config.HARNESS_OPENCODE_DB))
    t = _now_ms(-30)
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                 ("msg_huge", "ses_parent", t, t, _j(oc_assistant(inp=2**70))))
    conn.commit()
    conn.close()
    summary = run()
    ids = {r[0] for r in q("SELECT id FROM oc_message")}
    assert {"msg_a1", "msg_a2"} <= ids and "msg_huge" not in ids
    assert summary["per_source"]["opencode"]["skipped_by_type"].get("_OutOfRangeError") == 1


# ── K4: one row per message.id across files ────────────────────────────────


def test_a_message_id_counts_once_across_main_fork_and_subagent_files():
    fork = "f0000000-0000-4000-8000-00000000000f"
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1"), c_assistant(S_CLI, "m2")])
    write_jsonl(main_path(fork), [c_header(fork), c_assistant(fork, "m1"), c_assistant(fork, "m2"),
                                  c_assistant(fork, "m3")])
    write_jsonl(sub_path(S_CLI, AGENT_A), [c_assistant(S_CLI, "m1", sidechain=True, agent_id=AGENT_A),
                                           c_assistant(S_CLI, "m9", sidechain=True, agent_id=AGENT_A)])
    run()
    rows = dict(q("SELECT msg_id, kind FROM claude_msg"))
    assert rows == {"m1": "main", "m2": "main", "m3": "main", "m9": "subagent"}
    assert claude_tokens()[0] == 4 * 2  # input_tokens 2 per message, once each


def test_two_subagent_files_of_one_parent_with_the_same_id_count_once():
    write_jsonl(sub_path(S_SDK, AGENT_A), [c_assistant(S_SDK, "s1", sidechain=True, agent_id=AGENT_A)])
    write_jsonl(sub_path(S_SDK, AGENT_B), [c_assistant(S_SDK, "s1", sidechain=True, agent_id=AGENT_B,
                                                       usage={"input_tokens": 5, "output_tokens": 70})])
    run()
    assert q("SELECT msg_id, kind, input, output FROM claude_msg") == [("s1", "subagent", 5, 70)]


def test_a_main_file_read_later_takes_over_a_subagent_row():
    write_jsonl(sub_path(S_SDK, AGENT_A), [c_assistant(S_SDK, "x1", sidechain=True, agent_id=AGENT_A,
                                                       entrypoint="cli")])
    run()
    assert q("SELECT kind, origin, agent_id FROM claude_msg") == [("subagent", "interaktiv", AGENT_A)]
    write_jsonl(main_path(S_SDK), [c_header(S_SDK, "sdk-cli"), c_assistant(S_SDK, "x1", entrypoint="sdk-cli")])
    run()
    assert q("SELECT kind, origin, agent_id FROM claude_msg") == [("main", "orchestrator", None)]


def test_usage_lines_of_one_id_differ_and_the_maximum_per_field_counts():
    """Real data: 57 % of ids carry DIFFERENT usage across their lines (streaming)."""
    write_jsonl(main_path(S_CLI), [
        c_header(S_CLI),
        c_assistant(S_CLI, "m1", block=0, usage={"input_tokens": 2, "output_tokens": 1, "cache_read_input_tokens": 50}),
        c_assistant(S_CLI, "m1", block=1, usage={"input_tokens": 2, "output_tokens": 400}),
    ])
    run()
    assert q("SELECT input, output, cache_read FROM claude_msg") == [(2, 400, 50)]


# ── K11: index details ─────────────────────────────────────────────────────


def test_cost_state_keeps_the_largest_value_per_session():
    start_ms = int(datetime(2026, 10, 8, 12, 0).timestamp() * 1000)
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_cost(S_CLI, 97.76, start_ms), c_cost(S_CLI, 10.0, start_ms)])
    run()
    assert q("SELECT cost_usd FROM claude_cost") == [(97.76,)]


def test_unchanged_files_are_not_even_opened(monkeypatch):
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    run()
    real_open = Path.open

    def no_open(self, *a, **kw):
        if self.suffix == ".jsonl":
            raise AssertionError(f"opened {self.name}")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", no_open)
    summary = run()
    assert summary["status"] == "ok", summary
    assert summary["files_read"] == 0


def test_an_incomplete_opencode_message_is_reread_even_without_a_new_time_updated():
    make_opencode_db()
    conn = sqlite3.connect(str(config.HARNESS_OPENCODE_DB))
    t = _now_ms(-120)
    pending = oc_assistant(cost=0.0, inp=0)
    del pending["time"]["completed"]
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)", ("msg_pending", "ses_parent", t, t, _j(pending)))
    conn.commit()
    run()
    assert q("SELECT cost, completed FROM oc_message WHERE id='msg_pending'") == [(0.0, 0)]
    done = oc_assistant(cost=0.25, inp=900, t=pending["time"]["created"])
    conn.execute("UPDATE message SET data=? WHERE id='msg_pending'", (_j(done),))  # time_updated unchanged
    conn.commit()
    conn.close()
    second = run()
    assert q("SELECT cost, t_input, completed FROM oc_message WHERE id='msg_pending'") == [(0.25, 900, 1)]
    assert second["per_source"]["opencode"]["lines_read"] == 1
    assert run()["per_source"]["opencode"]["lines_read"] == 0


# ── K5: priority / memory calls ─────────────────────────────────────────────


def test_lower_priority_reports_success_in_a_child():
    """Run in a child: on Windows it puts the process into background mode."""
    proc = subprocess.run(
        [sys.executable, "-c", "import harness_index as hi; print(hi._lower_priority())"],
        cwd=str(REPO), env=_child_env(), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "True", proc.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_windows_peak_memory_and_pid_check_work():
    peak = hi._peak_private_mb_windows()
    assert peak is not None and peak > 0
    assert hi._pid_alive_windows(os.getpid()) is True
    assert hi._pid_alive_windows(_dead_pid()) is False


# ── Korrekturrunde 2: K13 memory peak and priority class ────────────────────


def test_peak_is_the_private_peak_not_the_capped_working_set():
    """Background mode caps the working set at ~32 MB; the private peak is what
    the run used (review probe on Windows: 32.0 against 318.3 MB)."""
    counters = hi._ProcessMemoryCounters()
    counters.PeakWorkingSetSize = 32 * 1_048_576
    counters.PeakPagefileUsage = int(318.3 * 1_048_576)
    assert hi._peak_private_mb(counters) == 318.3


def test_run_log_names_the_peak_by_what_it_measures(monkeypatch):
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    monkeypatch.setattr(hi, "_peak_memory_mb", lambda: ("peak_private_mb", 75.0))
    summary = run()
    assert q("SELECT peak_private_mb, peak_rss_mb FROM index_runs") == [(75.0, None)]
    assert (summary["peak_private_mb"], summary["peak_rss_mb"]) == (75.0, None)
    assert "Spitze privater Speicher 75.0 MB" in summary["_out"][-1]
    monkeypatch.setattr(hi, "_peak_memory_mb", lambda: ("peak_rss_mb", 26.7))
    append_jsonl(main_path(S_CLI), [c_assistant(S_CLI, "m2")])
    assert "Spitze RSS 26.7 MB" in run()["_out"][-1]
    assert q("SELECT peak_private_mb, peak_rss_mb FROM index_runs ORDER BY id") == [(75.0, None), (None, 26.7)]


@pytest.mark.skipif(os.name == "nt", reason="the POSIX branch of _peak_memory_mb")
def test_posix_peak_is_the_resident_set():
    column, mb = hi._peak_memory_mb()
    assert column == "peak_rss_mb" and mb is not None and mb > 0


class _FakeKernel32:
    """Records SetPriorityClass calls; GetPriorityClass answers ``reads``."""

    def __init__(self, reads: int, fail: tuple[int, ...] = ()):
        self.reads, self.fail = reads, fail
        self.calls: list[int] = []

    def GetCurrentProcess(self):  # noqa: N802 — the Windows API name
        return -1

    def SetPriorityClass(self, handle, value):  # noqa: N802
        self.calls.append(value)
        return 0 if value in self.fail else 1

    def GetPriorityClass(self, handle):  # noqa: N802
        return self.reads


BACKGROUND, BELOW_NORMAL, NORMAL, IDLE = 0x00100000, 0x4000, 0x20, 0x40


@pytest.mark.parametrize(
    ("reads", "fail", "expected"),
    [
        (NORMAL, (), ([BACKGROUND, BELOW_NORMAL], True)),     # measured: background mode read NORMAL
        (0, (), ([BACKGROUND, BELOW_NORMAL], True)),          # GetPriorityClass failed: set it anyway
        (BELOW_NORMAL, (), ([BACKGROUND], True)),
        (IDLE, (), ([BACKGROUND], True)),                     # lower than BELOW_NORMAL: kept
        (NORMAL, (BACKGROUND,), ([BACKGROUND], False)),
        (NORMAL, (BELOW_NORMAL,), ([BACKGROUND, BELOW_NORMAL], False)),
    ],
    ids=["normal", "unknown", "below_normal", "idle", "background_fails", "below_normal_fails"],
)
def test_windows_priority_class_is_never_left_above_below_normal(monkeypatch, capsys, reads, fail, expected):
    """The Windows branch with a recording kernel32 — runs on every platform."""
    calls, ok = expected
    fake = _FakeKernel32(reads, fail)
    monkeypatch.setattr(hi, "_kernel32", lambda: fake)
    assert hi._lower_priority_windows() is ok
    assert fake.calls == calls
    err = capsys.readouterr().err
    assert ("not set" in err) is (not ok)


_BACKGROUND_PROBE = textwrap.dedent("""
    import ctypes
    import harness_index as hi
    ok = hi._lower_priority()
    k = hi._kernel32()
    me = k.GetCurrentProcess()
    cls = k.GetPriorityClass(me)
    # documented probe: a second BEGIN fails with ERROR_PROCESS_MODE_ALREADY_BACKGROUND
    # (402) exactly while the process is still in background mode
    again = k.SetPriorityClass(me, 0x00100000)
    err = ctypes.get_last_error()
    block = bytearray(100 * 1_048_576)
    for i in range(0, len(block), 4096):
        block[i] = 1
    print(ok, cls, again, err, hi._peak_private_mb_windows())
""")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_windows_background_mode_keeps_below_normal_and_private_peak():
    """In a child (background mode would slow this test process down): the class
    reads at most BELOW_NORMAL, background mode is still on after the second
    SetPriorityClass, and 100 MB touched in background mode show as ≥ 90 MB."""
    proc = subprocess.run(
        [sys.executable, "-c", _BACKGROUND_PROBE], cwd=str(REPO), env=_child_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    ok, cls, again, err, peak = proc.stdout.split()
    assert ok == "True", proc.stderr
    assert int(cls) in (BELOW_NORMAL, IDLE), cls
    assert (again, err) == ("0", "402"), "background mode ended by SetPriorityClass(BELOW_NORMAL)"
    assert float(peak) >= 90, peak


# ── Korrekturrunde 2: K14 a line that raises while parsing ──────────────────

DEEP = "[" * 200_000  # json.loads raises RecursionError, not ValueError


def test_a_deeply_nested_line_is_skipped_and_never_blocks_its_file():
    """Review probe: the parse ran outside the line's try, so RecursionError
    rolled the whole file back — on every run."""
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1"), DEEP,
                                          c_assistant(S_CLI, "m2")])
    first = run()
    claude = first["per_source"]["claude"]
    assert sorted(r[0] for r in q("SELECT msg_id FROM claude_msg")) == ["m1", "m2"]
    assert (claude["lines_skipped"], claude["skipped_by_type"], claude["errors"]) == (1, {"RecursionError": 1}, [])
    assert q("SELECT offset FROM file_state WHERE source='claude'") == [(path.stat().st_size,)]
    second = run()
    assert (second["lines_read"], second["lines_skipped"], second["errors"]) == (0, 0, {})
    assert sorted(r[0] for r in q("SELECT msg_id FROM claude_msg")) == ["m1", "m2"]
    append_jsonl(path, [c_assistant(S_CLI, "m3")])
    third = run()
    assert (third["lines_read"], third["lines_skipped"]) == (1, 0)
    assert claude_tokens()[4] == 3


def test_a_deeply_nested_quiet_tail_does_not_block_the_file():
    path = write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1"), DEEP], newline_at_end=False)
    old = time.time() - 3600
    os.utime(path, (old, old))
    summary = run()
    assert summary["errors"] == {}
    assert q("SELECT msg_id FROM claude_msg") == [("m1",)]
    assert summary["per_source"]["claude"]["lines_skipped"] == 1
    assert q("SELECT offset, size FROM file_state WHERE source='claude'") == [(path.stat().st_size,) * 2]


def test_a_deeply_nested_meta_file_is_skipped_by_type_and_not_retried():
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    p = meta_path(S_CLI, AGENT_A)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DEEP, encoding="utf-8")
    first = run()
    assert first["errors"] == {}
    assert first["per_source"]["claude"]["skipped_by_type"] == {"RecursionError": 1}
    assert q("SELECT msg_id FROM claude_msg") == [("m1",)]  # the source went on after the meta file
    assert run()["lines_read"] == 0  # the meta file is not retried on every run


def test_a_deeply_nested_marker_line_is_counted_by_type_and_the_others_stay():
    write_markers([{"date": "2026-09-25", "id": "a", "expect": "x"}, DEEP, {"date": "2026-09-26", "id": "b"}])
    first = run()
    assert first["errors"] == {}
    assert first["per_source"]["markers"]["broken_by_type"] == {"RecursionError": 1}
    assert [r[0] for r in q("SELECT id FROM marker ORDER BY id")] == ["a", "b"]


def test_a_skipped_line_leaves_no_partial_rows():
    """The agent call of a line whose usage is out of range used to stay behind."""
    bad = c_agent_call(S_CLI, "m_bad", "toolu_bad")
    bad["message"]["usage"] = {"input_tokens": 2**70}
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), bad, c_agent_call(S_CLI, "m_ok", "toolu_ok")])
    summary = run()
    assert summary["per_source"]["claude"]["skipped_by_type"] == {"_OutOfRangeError": 1}
    assert q("SELECT tool_use_id FROM claude_agent_call") == [("toolu_ok",)]
    assert q("SELECT msg_id FROM claude_msg") == [("m_ok",)]


def test_a_skipped_line_leaves_no_file_info_behind():
    """A line's entrypoint is file information — from a skipped line, none is kept."""
    bad = c_assistant(S_SDK, "m_bad", entrypoint="sdk-cli", usage={"input_tokens": 2**70})
    write_jsonl(main_path(S_SDK), [bad, c_assistant(S_SDK, "m_ok", entrypoint=None)])
    run()
    assert q("SELECT origin FROM claude_msg") == [("unbekannt",)]
    assert q("SELECT entrypoint, cwd_hash FROM claude_session") == [(None, None)]


def test_a_skipped_codex_line_does_not_mix_into_the_rollout_row():
    """Review probe: input of the broken token_count line, output of the one before."""
    broken = _tc("2026-10-08T17:26:10.000Z", 900, 90)
    broken["payload"]["info"]["total_token_usage"]["output_tokens"] = 2**70
    write_codex_rollout(extra=[broken])
    summary = run()
    assert summary["per_source"]["codex"]["skipped_by_type"] == {"_OutOfRangeError": 1}
    assert q("SELECT input, output, total, tc_ts FROM codex_rollout") == [
        (600, 60, 660, "2026-10-08T17:26:00.000Z")]


# ── Korrekturrunde 2: K18 broken ledger/marker lines are a stock ────────────


def test_a_broken_ledger_line_is_a_stock_not_counted_on_every_rebuild():
    """Since K1 the ledger is rebuilt on every change — several times a day; its
    broken line used to be added to "übersprungen über alle Läufe" each time."""
    path = write_jsonl(Path(config.HARNESS_EXTERN_LEDGER), [_ledger_line(), "{broken"])
    run()
    for i in range(3):  # three rebuilds
        append_jsonl(path, [_ledger_line(tokens=i)])
        assert run()["per_source"]["ledger"]["broken_lines"] == 1
    assert q("SELECT count(*), sum(lines_skipped) FROM index_runs") == [(4, 0)]
    page = hi.dashboard_payload(config.HARNESS_DB_FILE)["harness"]["last_run"]
    assert page["broken_lines"] == {"ledger": 1, "markers": 0}
    assert page["skipped_total"] == 0
    write_jsonl(path, [_ledger_line()])  # line repaired: the stock follows the file
    run()
    assert hi.dashboard_payload(config.HARNESS_DB_FILE)["harness"]["last_run"]["broken_lines"]["ledger"] == 0


def test_an_unchanged_file_keeps_its_stock():
    write_markers([{"date": "2026-09-25", "id": "a"}, "{broken", {"id": "no-date"}])
    first = run()
    assert (first["per_source"]["markers"]["broken_lines"], first["per_source"]["markers"]["broken_by_type"]) == (
        2, {"JSONDecodeError": 1, "ValueError": 1})
    second = run()  # unchanged: not rebuilt, the stock stays where it is
    assert second["per_source"]["markers"]["broken_lines"] is None
    assert hi.dashboard_payload(config.HARNESS_DB_FILE)["harness"]["last_run"]["broken_lines"]["markers"] == 2


def test_main_lowers_priority_before_the_lock_is_taken(monkeypatch):
    order: list[str] = []

    class FakeLock:
        held_by = None
        taken_over = None

        def __init__(self, *_args):
            pass

        def acquire(self):
            order.append("lock")
            return False

    monkeypatch.setattr(hi, "_lower_priority", lambda: order.append("priority") or True)
    monkeypatch.setattr(hi, "IndexLock", FakeLock)
    assert hi.main(["--update"]) == 0
    assert order == ["priority", "lock"]


def test_main_lowers_priority_before_the_startup_warnings(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(hi, "_lower_priority", lambda: order.append("priority") or True)
    monkeypatch.setattr(hi, "_print_startup_warnings", lambda: order.append("warnings"))
    monkeypatch.setattr(hi, "run_update", lambda: {"status": "ok"})
    assert hi.main(["--update"]) == 0
    assert order == ["priority", "warnings"]


def test_startup_sec_is_stored_in_the_run_log_and_named_on_the_console(monkeypatch):
    write_jsonl(main_path(S_CLI), [c_header(S_CLI), c_assistant(S_CLI, "m1")])
    monkeypatch.setattr(hi, "_startup_sec", lambda now=None: 41.5)
    summary = run()
    assert q("SELECT startup_sec FROM index_runs") == [(41.5,)]
    assert summary["startup_sec"] == 41.5
    assert "Start davor 41.5 s" in summary["_out"][-1]


def test_startup_sec_is_a_real_non_negative_number():
    value = hi._startup_sec()
    assert value is not None and value >= 0


def test_a_meta_file_that_holds_null_is_counted_as_skipped_once():
    sess = "a0000000-0000-4000-8000-000000000001"
    write_jsonl(_proj_path("A", sess), [c_header(sess), c_assistant(sess, "a1")])
    path = meta_path(sess, AGENT_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("null", encoding="utf-8")
    claude = run()["per_source"]["claude"]
    assert q("SELECT count(*) FROM claude_agent_meta") == [(0,)]
    assert claude["lines_skipped"] == 1
    assert run()["per_source"]["claude"]["lines_skipped"] == 0  # file state saved: not counted again


def test_a_garbled_meta_file_is_still_counted_exactly_once():
    sess = "a0000000-0000-4000-8000-000000000001"
    write_jsonl(_proj_path("A", sess), [c_header(sess), c_assistant(sess, "a1")])
    path = meta_path(sess, AGENT_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{kaputt", encoding="utf-8")
    assert run()["per_source"]["claude"]["lines_skipped"] == 1


def test_the_console_names_broken_ledger_and_marker_lines_only_when_there_are_some():
    write_ledger()
    clean = "\n".join(run()["_out"])
    assert "kaputte Zeilen" not in clean
    write_ledger(extra=['{"ts_local":"broken'])
    out = run()["_out"]
    assert any("Ledger: 1 kaputte Zeilen" in line for line in out)


def test_main_prints_each_startup_warning_once_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(config, "STARTUP_WARNINGS", ["config: X=1 ungültig — Standardwert 2"])
    monkeypatch.setattr(hi, "_lower_priority", lambda: True)
    monkeypatch.setattr(hi, "run_update", lambda *a, **k: {"status": "ok"})
    assert hi.main(["--update"]) == 0
    assert hi.main(["--update"]) == 0
    assert capsys.readouterr().err.splitlines() == ["config: X=1 ungültig — Standardwert 2"]


@pytest.mark.parametrize("info", [
    pytest.param('{"broken": ' + str(2**70) + '}', id="out_of_range"),
    pytest.param("[" * 200_000, id="recursion_error"),  # not a ValueError
])
def test_an_unreadable_broken_stock_gives_0_and_never_raises_in_the_endpoint(info):
    write_ledger()
    run()
    with db() as conn:
        conn.execute("UPDATE file_state SET info = ? WHERE source = 'ledger'", (info,))
    page = hi.dashboard_payload(config.HARNESS_DB_FILE)["harness"]["last_run"]
    assert page["broken_lines"] == {"ledger": 0, "markers": 0}
