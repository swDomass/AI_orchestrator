"""Queue tab (queue.js), executed in node. Payloads come from the real queue_editor.queue_state()
against a tmp queue file (never the real queue). Skipped when node is not on the PATH."""

import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from _ui_scripts import NODE, NODE_STUB, page_script, run_node

with patch("config._load_dotenv"):
    import queue_editor
    import queue_manager

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

XSS = "<img src=x onerror=alert(1)> boese"
STAMP = "2026-09-01 10:00 (claude)"
FUTURE = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
ROWS = [
    "- [ ] Alpha offen #claude", "- [ ] Beta wartet #needs:fehlt", f"- [ ] Gamma geplant #at:{FUTURE}",
    f"- [ ] {XSS}", "  - <b>kind</b>", "- [ ] Delta " + "lang " * 60,
    f"- [x] Erledigt eins #id:e1 ✅ {STAMP}", f"- [x] Fehl #id:f1 ❌ {STAMP}", "- [-] Verworfen", "Freitext",
]
EXTRA = "- [ ] Neu von aussen"
ALPHA = "Alpha offen #claude"

PRE = r"""
globalThis.T_calls = []; globalThis.T_confirms = []; globalThis.T_answers = [];
globalThis._meta = {content: 'tok'};
document.querySelector = sel => sel === 'meta[name="orch-token"]' ? _meta : null;
globalThis.confirm = m => { T_confirms.push(m); return T_answers.length ? T_answers.shift() : true; };
globalThis.T_route = () => null;
globalThis.fetch = (u, o) => {
  T_calls.push({u, m: (o && o.method) || 'GET', b: o && o.body ? JSON.parse(o.body) : null});
  const r = T_route(u, o);
  if (r === 'throw') return Promise.reject(new Error('net'));
  if (!r && o && o.method === 'POST' && u.startsWith('/api/queue/')) {   // unexpected POST: harmless answer, counted in T_calls
    return Promise.resolve({ok: false, status: 599, json: () => Promise.resolve({ok: false, code: 'unexpected', error: 'unerwarteter POST'})});
  }
  if (!r) return new Promise(() => {});
  return Promise.resolve({ok: r.status >= 200 && r.status < 300, status: r.status, json: () => Promise.resolve(r.data)});
};
const T_settle = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); };
const T_posts = () => T_calls.filter(c => c.m === 'POST' && c.u.startsWith('/api/queue/'));
const T_gets = () => T_calls.filter(c => c.u === '/api/queue');
const T_btn = (idx, gen) => ({dataset: {gen: String(gen === undefined ? queueGen : gen), idx: String(idx)}});
const T_html = () => document.getElementById('q-groups').innerHTML;
const T_pos = text => P.a.entries.find(e => e.text.startsWith(text)).pos;
const T_set = (id, v) => { document.getElementById(id).value = v; };
const T_lint = () => T_calls.filter(c => c.m === 'POST' && c.u === '/api/queue/lint');
const T_form = (lint, mut) => { T_route = (u, o) => {          // routes for the form / edit flows
  if (u === '/api/queue/meta') return {status: 200, data: P.meta};
  if (u === '/api/queue/lint') return {status: 200, data: lint};
  if (u === '/api/queue') return {status: 200, data: P.a};
  if (u.startsWith('/api/queue/') && mut) return mut.status ? mut : {status: 200, data: mut};
  return null; }; };
const T_hidden = id => document.getElementById(id).hidden;
['q-reload', 'q-external', 'q-overwritten', 'q-msg', 'q-undo'].forEach(id => { document.getElementById(id).hidden = true; });
"""

LOAD = "T_route = u => u === '/api/queue' ? {status: 200, data: P.a} : null; await loadQueue(); await T_settle();"


@pytest.fixture
def qfile(tmp_path, monkeypatch):
    path = tmp_path / "agent-queue.md"
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", path)
    assert queue_manager.queue_file().is_relative_to(tmp_path)
    return path


def _state(qfile, rows, in_flight=None, mode="standalone", instance="i1"):
    qfile.write_bytes("\n".join(["# Queue", "", "## Queue", *rows, ""]).encode("utf-8"))
    p = queue_editor.queue_state(in_flight=in_flight, mode=mode)
    p.update(ok=True, instance=instance)
    return p


def _run(tmp_path, body, payloads):
    src = (NODE_STUB + PRE + "const P = " + json.dumps(payloads) + ";\n" + page_script()
           + "\n(async () => {" + body + "})().then(r => console.log(JSON.stringify(r)));\n")
    return run_node(tmp_path, src)


def _buttons(html, pos):
    pat = r'data-action="(queue-(?!expand)[a-z-]+)" data-gen="\d+" data-idx="' + str(pos) + '"'
    return sorted(re.findall(pat, html))


def test_groups_follow_the_fixed_order_and_search_is_case_insensitive(tmp_path, qfile):
    run = SimpleNamespace(task_text=ALPHA, subtasks=[])
    a = _state(qfile, ROWS, in_flight=run, mode="inprocess")
    body = """const g = s => queueGroupsOf(P.a.entries, s).map(x => [x.state, x.folded, x.entries.map(e => e.text)]);
      return {all: g(''), found: g('ALPHA'), none: g('gibtsnicht')};"""
    out = _run(tmp_path, body, {"a": a})
    states = [x[0] for x in out["all"]]
    assert states == ["laeuft", "offen", "wartet", "geplant", "fehlgeschlagen", "erledigt", "verworfen", "weitere"]
    by = {x[0]: x for x in out["all"]}
    assert [by[s][1] for s in states] == [False] * 5 + [True] * 3     # done/cancelled/other are folded
    assert "Beta wartet" in by["wartet"][2][0] and not any("Beta" in t for t in by["offen"][2])
    assert [x[0] for x in out["found"]] == ["laeuft"] and out["none"] == []


def test_buttons_come_only_from_the_servers_actions(tmp_path, qfile):
    a = _state(qfile, ROWS)
    body = LOAD + """
      const idx = T_pos('Alpha');
      const before = T_html(), actions = P.a.entries.map(e => [e.pos, e.actions.slice()]);
      queueState.entries[idx].actions = ['retry'];       // the server decides, not the script
      renderQueue();
      return {before, after: T_html(), idx, actions};"""
    out = _run(tmp_path, body, {"a": a})
    names = {"edit": ["queue-edit"], "delete": ["queue-delete"], "retry": ["queue-retry"],
             "duplicate": ["queue-duplicate"], "move": ["queue-move-up", "queue-move-down"]}
    for pos, actions in out["actions"]:
        assert _buttons(out["before"], pos) == sorted(n for act in actions for n in names[act]), pos
    assert sum(1 for _, acts in out["actions"] if acts == []) >= 1        # "weitere" has none
    assert _buttons(out["after"], out["idx"]) == ["queue-retry"]


def test_without_a_token_the_view_is_read_only_with_a_hint(tmp_path, qfile):
    body = "_meta.content = '';" + LOAD + """
      return {html: T_html(), hint: document.getElementById('q-hint').textContent, newBtn: T_hidden('q-new-btn')};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    assert 'data-action="queue-delete"' not in out["html"] and "Alpha offen" in out["html"]
    assert out["hint"] == "Bearbeiten nur über 127.0.0.1:8211" and out["newBtn"] is True


@pytest.mark.parametrize("route,shown", [
    ("{status: 403, data: {ok: false, code: 'token', error: 'x'}}", True),
    ("'throw'", True),
    ("{status: 403, data: {ok: false, code: 'forbidden', error: 'Verboten'}}", False),
])
def test_reload_hint_for_token_and_unreachable_but_not_for_forbidden(tmp_path, qfile, route, shown):
    body = LOAD + f"""
      T_route = (u, o) => (o && o.method === 'POST') ? {route} : {{status: 200, data: P.a}};
      await ACTIONS['queue-retry'](T_btn(T_pos('Fehl')));
      return {{reload: !T_hidden('q-reload'), msg: document.getElementById('q-msg').textContent,
              need: [orchNeedsReload({{status: 0}}), orchNeedsReload({{status: 403, data: {{code: 'token'}}}}),
                     orchNeedsReload({{status: 403, data: {{code: 'forbidden'}}}}), orchNeedsReload({{status: 409, data: null}})]}};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    assert out["reload"] is shown
    assert out["need"] == [True, True, False, False]
    if not shown:
        assert out["msg"] == "Verboten"


def test_409_shows_the_servers_message_and_reloads_the_queue(tmp_path, qfile):
    body = LOAD + """
      const n0 = T_gets().length;
      T_route = (u, o) => (o && o.method === 'POST')
        ? {status: 409, data: {ok: false, code: 'changed', error: 'Inzwischen geändert — neu laden.'}}
        : {status: 200, data: P.a};
      await ACTIONS['queue-duplicate'](T_btn(T_pos('Alpha')));
      await T_settle();
      return {msg: document.getElementById('q-msg').textContent, extraGets: T_gets().length - n0};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    assert out == {"msg": "Inzwischen geändert — neu laden.", "extraGets": 1}


def test_text_from_the_file_is_escaped_and_the_markup_has_no_style(tmp_path, qfile):
    out = _run(tmp_path, LOAD + "return {html: T_html()};", {"a": _state(qfile, ROWS)})
    html = out["html"]
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "<b>" not in html and "&lt;b&gt;kind" in html
    assert "style=" not in html.lower()


def test_blocks_changed_from_outside_are_marked_but_our_own_write_is_not(tmp_path, qfile):
    a = _state(qfile, ROWS)
    b = _state(qfile, [*ROWS, EXTRA])
    body = LOAD + """
      const n = h => (h.match(/q-changed/g) || []).length;
      T_route = u => u === '/api/queue' ? {status: 200, data: P.b} : null;
      await loadQueue(true);
      const external = {marked: n(T_html()), banner: !T_hidden('q-external'),
                        on: T_html().split('q-changed')[1].includes('Neu von aussen')};
      // fresh page state, then our own mutation returning the new payload
      queueState = null; queueJson = ''; queueMarks = new Set(); queueOwnSha = null;
      document.getElementById('q-external').hidden = true;
      T_route = u => u === '/api/queue' ? {status: 200, data: P.a} : null;
      await loadQueue(); await T_settle();
      T_route = (u, o) => o && o.method === 'POST' ? {status: 200, data: {ok: true, changed: true, queue: P.b}} : {status: 200, data: P.b};
      await ACTIONS['queue-duplicate'](T_btn(T_pos('Alpha')));
      await loadQueue(true);
      return {external, own: {marked: n(T_html()), banner: !T_hidden('q-external'), shown: T_html().includes('Neu von aussen')}};"""
    out = _run(tmp_path, body, {"a": a, "b": b})
    assert out["external"] == {"marked": 1, "banner": True, "on": True}
    assert out["own"] == {"marked": 0, "banner": False, "shown": True}


def test_dashboard_restart_asks_for_a_reload(tmp_path, qfile):
    a, b = _state(qfile, ROWS, instance="i1"), _state(qfile, ROWS, instance="i2")
    body = LOAD + """
      T_route = () => ({status: 200, data: P.b}); await loadQueue(true);
      return {hidden: T_hidden('q-reload'), msg: document.getElementById('q-reload-msg').textContent};"""
    out = _run(tmp_path, body, {"a": a, "b": b})
    assert out == {"hidden": False, "msg": "Dashboard neu gestartet — Seite neu laden."}


def test_polling_runs_only_while_the_tab_is_shown(tmp_path, qfile):
    body = """
      const n = () => T_gets().length;
      const el = document.getElementById('tab-queue');
      el.style.display = 'none'; queueTick(); const hiddenTab = n();
      el.style.display = ''; document.hidden = true; queueTick(); const hiddenWindow = n();
      document.hidden = false; queueTick(); const shown = n();
      return {hiddenTab, hiddenWindow, shown};"""
    assert _run(tmp_path, body, {"a": _state(qfile, ROWS)}) == {"hiddenTab": 0, "hiddenWindow": 0, "shown": 1}


def test_declined_confirm_sends_nothing(tmp_path, qfile):
    body = LOAD + """
      T_answers.push(false);
      await ACTIONS['queue-delete'](T_btn(T_pos('Alpha')));
      return {posts: T_posts().length, asked: T_confirms.length};"""
    assert _run(tmp_path, body, {"a": _state(qfile, ROWS)}) == {"posts": 0, "asked": 1}


@pytest.mark.parametrize("code", ["confirm_unknown_running", "confirm_dangerous"])
def test_428_asks_and_repeats_with_the_confirmation_flag(tmp_path, qfile, code):
    body = LOAD + f"""
      let n = 0;
      T_route = (u, o) => {{ if (!(o && o.method === 'POST')) return {{status: 200, data: P.a}};
        return n++ === 0 ? {{status: 428, data: {{ok: false, code: '{code}', error: 'Wirklich?'}}}}
                         : {{status: 200, data: {{ok: true, changed: true, queue: P.b}}}}; }};
      const r = await ACTIONS['queue-delete'](T_btn(T_pos('Alpha')));
      return {{ok: r, bodies: T_posts().map(c => c.b), confirms: T_confirms, gets: T_gets().length,
              html: T_html().includes('Neu von aussen')}};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS), "b": _state(qfile, [*ROWS, EXTRA])})
    first, second = out["bodies"]
    assert out["ok"] is True and set(first) == {"ref"}
    assert second == {**first, code: True} and out["confirms"][1] == "Wirklich?"
    assert out["gets"] == 1 and out["html"] is True      # the answer carries the payload: no second GET


def test_a_declined_428_confirmation_stops_after_one_post(tmp_path, qfile):
    body = LOAD + """
      T_route = (u, o) => o && o.method === 'POST' ? {status: 428, data: {ok: false, code: 'confirm_unknown_running', error: 'Sicher?'}} : null;
      T_answers.push(true, false);
      await ACTIONS['queue-delete'](T_btn(T_pos('Alpha')));
      return {posts: T_posts().length, asked: T_confirms.length};"""
    assert _run(tmp_path, body, {"a": _state(qfile, ROWS)}) == {"posts": 1, "asked": 2}


def test_move_posts_the_direction_and_a_stale_button_reloads_instead_of_acting(tmp_path, qfile):
    body = LOAD + """
      const pos = T_pos('Alpha');
      T_route = (u, o) => o && o.method === 'POST' ? {status: 200, data: {ok: true, changed: false, queue: P.a}} : {status: 200, data: P.a};
      await ACTIONS['queue-move-down'](T_btn(pos));
      const moved = T_posts().map(c => [c.u, c.b.direction, c.b.ref.start]);
      const gets = T_gets().length;
      await ACTIONS['queue-move-up'](T_btn(pos, queueGen + 5));
      await T_settle();
      return {moved, posts: T_posts().length, extraGets: T_gets().length - gets, start: P.a.entries[pos].ref.start};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    assert out["moved"] == [["/api/queue/move", "down", out["start"]]]
    assert out["posts"] == 1 and out["extraGets"] == 1


def test_undo_button_shows_the_payloads_undo_and_posts_its_backup_id(tmp_path, qfile):
    a = _state(qfile, ROWS)
    a["undo"] = {"available": True, "id": "bk-1", "action": "delete", "at": "2026-10-23T10:00:00", "summary": "x"}
    body = LOAD + """
      const ui = {hidden: T_hidden('q-undo'), text: document.getElementById('q-undo').textContent};
      T_route = (u, o) => o && o.method === 'POST' ? {status: 200, data: {ok: true, changed: true, queue: P.a}} : null;
      await ACTIONS['queue-undo']();
      return {ui, posts: T_posts().map(c => [c.u, c.b])};"""
    out = _run(tmp_path, body, {"a": a})
    assert out["ui"]["hidden"] is False and out["ui"]["text"].startswith("Rückgängig: delete")
    assert out["posts"] == [["/api/queue/undo", {"backup_id": "bk-1"}]]


def test_long_lines_are_clamped_with_a_more_button_and_status_says_unknown_running(tmp_path, qfile):
    body = LOAD + """
      const before = T_html();
      await ACTIONS['queue-expand'](T_btn(T_pos('Delta')));
      return {before, after: T_html(), run: document.getElementById('q-run').textContent};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    assert out["before"].count("q-line clamped") == 1 and "mehr" in out["before"]
    assert "q-line clamped" not in out["after"] and "weniger" in out["after"]
    assert "Laufstatus unbekannt" in out["run"]


def test_typing_in_the_search_box_filters_through_one_delegated_input_listener(tmp_path, qfile):
    body = LOAD + """
      const listeners = _docListeners.input || [];
      document.getElementById('q-search').value = 'BETA';
      listeners[0]({target: {id: 'q-search'}});
      const filtered = T_html();
      listeners[0]({target: {id: 'something-else'}});
      return {n: listeners.length, filtered, same: T_html() === filtered};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    assert out["n"] == 1 and out["same"] is True
    assert "Beta wartet" in out["filtered"] and "Alpha offen" not in out["filtered"]


# ── S5b: form, edit, raw text, reload hint, "probably overwritten" ──────────────────────────────

def _meta():
    m = queue_editor.queue_meta()
    m["ok"] = True
    return m


def _lint(qfile, fields=None, text=None, ref=None):
    """The real /api/queue/lint answer: fields -> build_task_line -> lint_draft, or a free text."""
    content = qfile.read_text(encoding="utf-8")
    if fields is None:
        return queue_editor.lint_draft(content, line=text, replace_ref=ref)
    line, errs, _dangers, _hint = queue_editor.build_task_line(fields, datetime.now())
    if line is None:
        res = queue_editor.lint_draft(content, line="- [ ] x")
        res.update(line=None, valid=False, field_errors=[e._asdict() for e in errs])
        return res
    return queue_editor.lint_draft(content, line=line)


def _ok(queue):
    return {"ok": True, "changed": True, "queue": queue}


def _form(qfile, rows, fields, extra_rows=()):
    """(payload before, lint answer, add answer) for a form whose saved line is built from ``fields``."""
    a = _state(qfile, rows)
    lint = _lint(qfile, fields)
    b = _state(qfile, [*rows, *extra_rows])
    b["undo"] = {"available": True, "id": "bk-2", "action": "add", "at": "2026-10-23T10:00:00", "summary": "x"}
    return a, lint, _ok(b)


def test_form_preview_serializes_the_fields_and_renders_the_answer_line_per_textcontent(tmp_path, qfile):
    a = _state(qfile, ROWS)
    lint = _lint(qfile, {"text": XSS, "tool": "review-loop"})
    assert lint["line"] and "<img" in lint["line"]
    lint["findings"] = [{"level": "warning", "code": "w", "message": "<b>Achtung</b>"}]
    body = LOAD + """
      T_form(P.lint);
      await ACTIONS['queue-new']();
      T_set('qf-text', 'Wochenreport'); T_set('qf-tool', 'review-loop'); T_set('qf-model', '#claude_opus');
      T_set('qf-effort', 'high'); T_set('qf-profile', 'work'); T_set('qf-cwd', 'D:\\\\x y'); T_set('qf-id', 'n1');
      T_set('qf-needs', 'a1'); T_set('qf-sched', 'every'); T_set('qf-every', '24h'); T_set('qf-at-time', '09:00');
      T_set('qf-sw-grace', '2h'); T_set('qf-sw-timeout', '30m'); T_set('qf-sw-verify', 'python -V');
      document.getElementById('qf-sw-no_commit').checked = true;
      await queuePreviewNow();
      const every = T_lint().pop().b.fields;
      T_set('qf-sched', 'once'); T_set('qf-at-date', '2026-12-01'); T_set('qf-model', '#codex');
      await queuePreviewNow();
      const once = T_lint().pop().b.fields;
      return {every, once, line: document.getElementById('qf-line').textContent,
              findings: document.getElementById('qf-findings').innerHTML};"""
    out = _run(tmp_path, body, {"a": a, "lint": lint, "meta": _meta()})
    assert out["every"] == {"text": "Wochenreport", "tool": "review-loop", "model": "#claude_opus", "effort": "high",
                            "profile": "work", "cwd": "D:\\x y", "id": "n1", "needs": "a1", "every": "24h", "at": "09:00",
                            "grace": "2h", "timeout": "30m", "verify": "python -V", "no_commit": True}
    assert out["once"] == {"text": "Wochenreport", "tool": "review-loop", "model": "#codex", "profile": "work",
                           "cwd": "D:\\x y", "id": "n1", "needs": "a1", "at": "2026-12-01T09:00", "timeout": "30m",
                           "verify": "python -V", "no_commit": True}      # codex: no effort; once: no every/grace
    assert out["line"] == lint["line"] and "<img" in out["line"]
    assert "<b>" not in out["findings"] and "&lt;b&gt;Achtung" in out["findings"]


def test_the_preview_waits_300_ms_and_typing_disables_saving_until_the_answer_arrives(tmp_path, qfile):
    a = _state(qfile, ROWS)
    body = LOAD + """
      T_form(P.lint);
      await ACTIONS['queue-new']();
      T_set('qf-text', 'x'); await queuePreviewNow();
      const saveBefore = document.getElementById('qf-save').disabled, n0 = T_lint().length;
      const timers = []; globalThis.setTimeout = (fn, ms) => { timers.push([fn, ms]); return 7; };
      const lis = [(_docListeners.input || []).length, (_docListeners.change || []).length];
      if (lis[0] !== 1 || lis[1] !== 1) return {lis, saveBefore};
      _docListeners.input[0]({target: {id: 'qf-text'}}); _docListeners.change[0]({target: {id: 'qf-tool'}});
      const during = {save: document.getElementById('qf-save').disabled, posts: T_lint().length - n0, ms: timers.map(t => t[1])};
      await timers[timers.length - 1][0]();
      return {saveBefore, lis, during, after: {posts: T_lint().length - n0, save: document.getElementById('qf-save').disabled}};"""
    out = _run(tmp_path, body, {"a": a, "lint": _lint(qfile, {"text": "x"}), "meta": _meta()})
    assert out["lis"] == [1, 1] and out["saveBefore"] is False
    assert out["during"] == {"save": True, "posts": 0, "ms": [300, 300]}
    assert out["after"] == {"posts": 1, "save": False}


def test_field_errors_sit_at_the_field_and_block_saving_even_with_the_override(tmp_path, qfile):
    a = _state(qfile, ROWS)
    lint = _lint(qfile, {"text": "x", "model": "#nope"})
    assert lint["field_errors"][0]["field"] == "model"
    body = LOAD + """
      T_form(P.lint);
      await ACTIONS['queue-new']();
      document.getElementById('qf-force').checked = true; _docListeners.input[0]({target: {id: 'qf-force'}});
      return {err: document.getElementById('qf-err-model').textContent, save: document.getElementById('qf-save').disabled,
              sent: await ACTIONS['queue-save'](), posts: T_posts().filter(c => c.u.endsWith('/add')).length};"""
    out = _run(tmp_path, body, {"a": a, "lint": lint, "meta": _meta()})
    assert out == {"err": lint["field_errors"][0]["message"], "save": True, "sent": False, "posts": 0}


def test_an_error_finding_blocks_saving_only_until_the_override_and_warnings_never(tmp_path, qfile):
    rows = [*ROWS, "- [ ] Idrow #id:a1"]
    a = _state(qfile, rows)
    err = _lint(qfile, {"text": "x", "id": "a1"})
    warn = _lint(qfile, {"text": "x"})
    assert err["findings"][0]["level"] == "error" and {f["level"] for f in warn["findings"]} == {"warning"}
    body = LOAD + """
      const st = () => ({save: document.getElementById('qf-save').disabled, force: document.getElementById('qf-w-force').hidden});
      T_form(P.err); await ACTIONS['queue-new']();
      const blocked = st();
      document.getElementById('qf-force').checked = true; _docListeners.input[0]({target: {id: 'qf-force'}});
      const forced = st();
      T_form(P.warn); await queuePreviewNow();
      return {blocked, forced, warn: st()};"""
    out = _run(tmp_path, body, {"a": a, "err": err, "warn": warn, "meta": _meta()})
    assert out["blocked"] == {"save": True, "force": False}
    assert out["forced"] == {"save": False, "force": False}
    assert out["warn"] == {"save": False, "force": True}       # the warning-only answer neither blocks nor asks


@pytest.mark.parametrize("accept", [False, True])
def test_a_danger_asks_first_and_adds_with_confirm_dangerous_only_when_accepted(tmp_path, qfile, accept):
    fields = {"text": "Aufraeumen", "shutdown": True}
    a, lint, added = _form(qfile, ROWS, fields, ["- [ ] Aufraeumen #shutdown"])
    assert lint["dangers"] and lint["dangers"][0]["tag"] == "#shutdown"
    body = LOAD + """
      T_form(P.lint, P.added); T_answers.push(@@);
      await ACTIONS['queue-new']();
      T_set('qf-text', 'Aufraeumen'); document.getElementById('qf-sw-shutdown').checked = true; T_set('qf-pos', 'top');
      await queuePreviewNow();
      const warn = document.getElementById('qf-dangers').innerHTML;
      const ok = await ACTIONS['queue-save']();
      return {ok, warn, asked: T_confirms, adds: T_posts().filter(c => c.u === '/api/queue/add').map(c => c.b),
              formOpen: document.getElementById('q-form').innerHTML !== '', undo: T_hidden('q-undo'), html: T_html()};""".replace("@@", str(accept).lower())
    out = _run(tmp_path, body, {"a": a, "lint": lint, "added": added, "meta": _meta()})
    assert "q-danger" in out["warn"] and "Faehrt den Rechner" in out["warn"] and "Faehrt den Rechner" in out["asked"][0]
    if not accept:
        assert out["adds"] == [] and out["ok"] is False and out["formOpen"] is True
    else:
        assert out["adds"] == [{"fields": {"text": "Aufraeumen", "shutdown": True}, "position": "top", "confirm_dangerous": True}]
        assert out["ok"] is True and out["formOpen"] is False and out["undo"] is False and "Aufraeumen #shutdown" in out["html"]


def test_unregistered_or_disallowed_models_are_disabled_effort_follows_the_provider_and_meta_is_cached(tmp_path, qfile):
    m = _meta()
    for p in m["providers"]:
        p["registered"], p["allowed"] = p["name"] != "gemini", p["name"] != "vibe"
    body = LOAD + """
      T_form({ok: true, line: null, field_errors: [], findings: [], dangers: []});
      await ACTIONS['queue-new'](); await ACTIONS['queue-new']();
      const metaGets = () => T_calls.filter(c => c.u === '/api/queue/meta').length, cached = metaGets();
      const effort = {};
      for (const tag of ['', '#claude_opus', '#codex']) { T_set('qf-model', tag); effort[tag] = queueEffortOn(); }
      queueInstance = 'neu'; await ACTIONS['queue-new']();
      return {html: document.getElementById('q-form').innerHTML, cached, again: metaGets(), effort};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS), "meta": m})
    h = out["html"]
    assert '<optgroup label="gemini — nicht registriert"><option value="#gemini" disabled>' in h
    assert '<optgroup label="vibe — nicht erlaubt"><option value="#vibe" disabled>' in h
    assert '<option value="#claude_opus">' in h and "style=" not in h
    assert out["effort"] == {"": True, "#claude_opus": True, "#codex": False}
    assert (out["cached"], out["again"]) == (1, 2)


def test_editing_keeps_the_opened_ref_through_polling_and_a_409_leaves_the_form_open(tmp_path, qfile):
    a = _state(qfile, ROWS)
    ref = a["entries"][[e["text"] for e in a["entries"]].index(ALPHA)]["ref"]
    lint = _lint(qfile, text="Alpha neu #claude", ref=ref)
    assert lint["line"] and not lint["field_errors"]
    b = _state(qfile, ["- [ ] Davor von aussen", *ROWS])
    body = LOAD + """
      T_form(P.lint);
      await ACTIONS['queue-edit'](T_btn(T_pos('Alpha')));
      const opened = {text: document.getElementById('qf-edit-text').value, lintBody: T_lint().pop().b};
      T_route = u => u === '/api/queue' ? {status: 200, data: P.b} : {status: 200, data: P.meta};
      await loadQueue(true);                                     // polling replaces the list underneath
      const same = JSON.stringify(queueEditRef) === JSON.stringify(P.ref);
      T_set('qf-edit-text', 'Alpha neu #claude');
      T_form(P.lint, {status: 409, data: {ok: false, code: 'changed', error: 'Inzwischen geändert — neu laden.'}});
      const gets = T_gets().length;
      await queuePreviewNow();
      const ok = await ACTIONS['queue-save'](); await T_settle();
      return {opened, same, ok, edits: T_posts().filter(c => c.u === '/api/queue/edit').map(c => c.b),
              msg: document.getElementById('q-msg').textContent, open: queueMode, extraGets: T_gets().length - gets};"""
    out = _run(tmp_path, body, {"a": a, "b": b, "lint": lint, "ref": ref, "meta": _meta()})
    assert out["opened"] == {"text": ALPHA, "lintBody": {"text": ALPHA, "ref": ref}} and out["same"] is True
    assert out["edits"] == [{"ref": ref, "text": "Alpha neu #claude"}] and out["ok"] is False
    assert out["msg"] == "Inzwischen geändert — neu laden." and out["open"] == "edit" and out["extraGets"] == 1


def test_a_free_text_edit_with_a_preapproval_tag_asks_and_sends_confirm_dangerous(tmp_path, qfile):
    a = _state(qfile, ROWS)
    ref = a["entries"][[e["text"] for e in a["entries"]].index(ALPHA)]["ref"]
    lint = _lint(qfile, text="Alpha #approve:ja", ref=ref)
    assert lint["dangers"]
    b = _state(qfile, ROWS)
    body = LOAD + """
      T_form(P.lint, {status: 200, data: {ok: true, changed: true, queue: P.b}});
      await ACTIONS['queue-edit'](T_btn(T_pos('Alpha')));
      T_set('qf-edit-text', 'Alpha #approve:ja'); await queuePreviewNow();
      const ok = await ACTIONS['queue-save']();
      return {ok, asked: T_confirms.length, edits: T_posts().filter(c => c.u === '/api/queue/edit').map(c => c.b), open: queueMode};"""
    out = _run(tmp_path, body, {"a": a, "b": b, "lint": lint, "meta": _meta()})
    assert out["ok"] is True and out["asked"] == 1 and out["open"] is None
    assert out["edits"] == [{"ref": ref, "text": "Alpha #approve:ja", "confirm_dangerous": True}]


def test_raw_view_loads_the_text_checks_it_and_keeps_the_users_text_on_sha_mismatch(tmp_path, qfile):
    raw = _state(qfile, ROWS)
    raw_full = queue_editor.queue_state(in_flight=None, mode="standalone", include_raw=True)
    raw_full.update(ok=True, instance="i1")
    lint = {"ok": True, "findings": [{"level": "warning", "code": "w1", "message": "<i>zeile</i>"}],
            "file_findings": [{"level": "error", "code": "e1", "message": "Datei kaputt"}]}
    body = LOAD + """
      T_route = (u, o) => u === '/api/queue?raw=1' ? {status: 200, data: P.full}
        : u === '/api/queue/lint' ? {status: 200, data: P.lint}
        : u === '/api/queue/raw' ? {status: 409, data: {ok: false, code: 'sha_mismatch', error: 'Datei inzwischen geändert.'}}
        : {status: 200, data: P.a};
      await ACTIONS['queue-raw']();
      document.getElementById('qr-hint').hidden = true;          // the stub does not read the hidden attribute
      const loaded = {text: document.getElementById('q-raw-text').value, sha: queueRawSha, shown: document.getElementById('q-raw').style.display};
      T_set('q-raw-text', document.getElementById('q-raw-text').value + '\\n- [ ] meine Zeile');
      await ACTIONS['queue-raw-check']();
      const checked = {body: T_posts().pop().b, html: document.getElementById('qr-result').innerHTML};
      const ok = await ACTIONS['queue-raw-save'](); await T_settle();
      const after = document.getElementById('q-raw-text').value;
      return {loaded, checked, ok, after, raws: T_posts().filter(c => c.u === '/api/queue/raw').map(c => c.b),
              hint: [document.getElementById('qr-hint').hidden, document.getElementById('qr-hint-text').textContent],
              open: queueMode, msg: document.getElementById('q-msg').textContent};"""
    out = _run(tmp_path, body, {"a": raw, "full": raw_full, "lint": lint})
    assert out["loaded"] == {"text": raw_full["raw"], "sha": raw_full["file"]["sha"], "shown": ""}
    mine = raw_full["raw"] + "\n- [ ] meine Zeile"
    assert out["checked"]["body"] == {"content": mine}
    assert "&lt;i&gt;zeile" in out["checked"]["html"] and "Datei kaputt" in out["checked"]["html"]
    assert out["ok"] is False and out["after"] == mine and out["open"] == "raw"      # the user's text stays
    assert out["raws"] == [{"sha": raw_full["file"]["sha"], "content": mine}]
    assert out["hint"][0] is False and "Dein Text bleibt" in out["hint"][1] and out["msg"] == "Datei inzwischen geändert."


def test_raw_save_shows_a_422_message_and_closes_on_success(tmp_path, qfile):
    a = _state(qfile, ROWS)
    full = queue_editor.queue_state(in_flight=None, mode="standalone", include_raw=True)
    full.update(ok=True, instance="i1")
    body = LOAD + """
      let n = 0;
      T_route = (u, o) => u === '/api/queue?raw=1' ? {status: 200, data: P.full}
        : u === '/api/queue/raw' ? (n++ === 0 ? {status: 422, data: {ok: false, code: 'stamp_in_raw', error: 'Stempel in der Datei.'}}
                                              : {status: 200, data: {ok: true, changed: true, queue: P.a}})
        : {status: 200, data: P.a};
      await ACTIONS['queue-raw']();
      const first = await ACTIONS['queue-raw-save']();
      const hint = document.getElementById('qr-hint-text').textContent, mid = queueMode;
      const second = await ACTIONS['queue-raw-save']();
      return {first, hint, mid, second, end: queueMode, raw: document.getElementById('q-raw').innerHTML};"""
    out = _run(tmp_path, body, {"a": a, "full": full})
    assert out == {"first": False, "hint": "Stempel in der Datei.", "mid": "raw", "second": True, "end": None, "raw": ""}


def test_the_reload_hint_for_get_failures_needs_three_misses_and_goes_away_with_a_good_answer(tmp_path, qfile):
    body = LOAD + """
      const shown = () => !T_hidden('q-reload');
      T_route = () => 'throw';
      const seen = [];
      for (let i = 0; i < 3; i++) { await loadQueue(true); seen.push(shown()); }
      T_route = () => ({status: 200, data: P.a});
      await loadQueue(true);
      const healed = shown();
      T_route = () => 'throw'; await loadQueue(true); await loadQueue(true);          // the counter started over
      const restarted = shown();
      T_route = () => ({status: 403, data: {ok: false, code: 'token', error: 'x'}}); await loadQueue(true);
      const token = shown();
      T_route = () => ({status: 200, data: P.a}); await loadQueue(true);
      return {seen, healed, restarted, token, sticky: shown()};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    assert out == {"seen": [False, False, True], "healed": False, "restarted": False, "token": True, "sticky": True}


@pytest.mark.parametrize("case,banner", [("gone", True), ("kept", False), ("own-between", False)])
def test_the_overwritten_banner_needs_our_line_missing_after_a_foreign_change(tmp_path, qfile, case, banner):
    mine = "Neu von aussen"
    a, lint, added = _form(qfile, ROWS, {"text": mine}, ["- [ ] " + mine])
    c = _state(qfile, [*ROWS, "- [ ] Anderes"])
    d = _state(qfile, [*ROWS, "- [ ] " + mine, "- [ ] Anderes"])
    e = _state(qfile, [*ROWS, "- [ ] " + mine, "- [ ] Alpha offen #claude"])
    body = LOAD + """
      T_form(P.lint, P.added);
      await ACTIONS['queue-new'](); T_set('qf-text', P.mine); await queuePreviewNow();
      await ACTIONS['queue-save']();
      const written = queueWritten;
      if (P.case === 'own-between') {
        T_route = (u, o) => o && o.method === 'POST' ? {status: 200, data: {ok: true, changed: true, queue: P.e}} : null;
        await ACTIONS['queue-duplicate'](T_btn(T_pos('Alpha')));
      }
      T_route = () => ({status: 200, data: P.case === 'kept' ? P.d : P.c});
      await loadQueue(true);
      return {written, banner: !T_hidden('q-overwritten'), external: !T_hidden('q-external')};"""
    out = _run(tmp_path, body, {"a": a, "lint": lint, "added": added, "c": c, "d": d, "e": e, "mine": mine, "case": case, "meta": _meta()})
    assert out["written"] == mine and out["banner"] is banner and out["external"] is True


def test_an_unexpected_queue_post_gets_a_harmless_answer_and_is_counted(tmp_path, qfile):
    body = LOAD + """
      const ok = await queueRun('delete', {ref: {}});
      return {ok, posts: T_posts().length, msg: document.getElementById('q-msg').textContent};"""
    assert _run(tmp_path, body, {"a": _state(qfile, ROWS)}) == {"ok": False, "posts": 1, "msg": "unerwarteter POST"}


def test_move_buttons_carry_a_title_that_explains_the_file_order(tmp_path, qfile):
    body = LOAD + "return {html: T_html(), pos: T_pos('Alpha')};"
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    for name, word in (("queue-move-up", "Nach oben"), ("queue-move-down", "Nach unten")):
        pat = 'data-action="' + name + '" data-gen="' + r"\d+" + '" data-idx="' + str(out["pos"]) + '" title="' + word
        assert re.search(pat + ": der Orchestrator arbeitet fällige Tasks in Dateireihenfolge ab", out["html"]), name


@pytest.mark.parametrize("mode,running,expected", [("inprocess", True, 2), ("inprocess", False, 0), ("standalone", False, 0)])
def test_due_entries_below_the_running_one_get_the_soft_round_hint(tmp_path, qfile, mode, running, expected):
    run = SimpleNamespace(task_text=ALPHA, subtasks=[]) if running else None
    a = _state(qfile, ROWS, in_flight=run, mode=mode)
    body = LOAD + """
      const h = T_html(), at = h.indexOf('vermutlich in dieser Runde eingeplant');
      const offen = P.a.entries.filter(e => e.state === 'offen').length;
      return {n: h.split('vermutlich in dieser Runde eingeplant').length - 1, first: at, offen,
              noBlock: h.indexOf('data-action="queue-delete"') > 0};"""
    out = _run(tmp_path, body, {"a": a})
    assert out["n"] == expected and out["noBlock"]       # a hint only; buttons stay


def test_a_slow_answer_after_30_s_shows_the_note_keeps_buttons_locked_and_leaves_no_timer(tmp_path, qfile):
    body = LOAD + """
      const timers = [];
      globalThis.setTimeout = (fn, ms) => { timers.push({fn, ms, live: true}); return timers.length; };
      globalThis.clearTimeout = id => { if (timers[id - 1]) timers[id - 1].live = false; };
      let release;
      const pending = new Promise(r => { release = r; });
      globalThis.fetch = (u, o) => {
        T_calls.push({u, m: (o && o.method) || 'GET', b: o && o.body ? JSON.parse(o.body) : null});
        if (o && o.method === 'POST') return pending;
        return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(P.a)});
      };
      const e = P.a.entries[T_pos('Alpha')];
      const run1 = queueRun('move', {ref: e.ref, direction: 'down'});
      await T_settle();
      const slow = timers.filter(t => t.ms === 30000);
      const locked = T_html().includes(' disabled>');
      slow[0].fn(); await T_settle();
      const note = document.getElementById('q-msg').textContent;
      const again = await queueRun('move', {ref: e.ref, direction: 'up'});
      const posts = T_posts().length;
      release({ok: true, status: 200, json: () => Promise.resolve({ok: true, queue: P.a})});
      const done = await run1;
      return {n: slow.length, locked, note, again, posts, done, live: timers.filter(t => t.live).length,
              after: T_html().includes(' disabled>'), msg: document.getElementById('q-msg').textContent};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    assert out["n"] == 1 and out["locked"] is True
    assert out["note"] == "Die Änderung dauert ungewöhnlich lange — Stand wird neu geladen"
    assert out["again"] is False and out["posts"] == 1 and out["done"] is True
    assert out["live"] == 0 and out["after"] is False and out["msg"] == ""


def test_the_30_s_note_is_armed_per_request_not_during_a_confirm_dialog(tmp_path, qfile):
    body = LOAD + """
      const timers = [];
      globalThis.setTimeout = (fn, ms) => { timers.push({fn, ms, live: true}); return timers.length; };
      globalThis.clearTimeout = id => { if (timers[id - 1]) timers[id - 1].live = false; };
      let n = 0;
      globalThis.fetch = (u, o) => {
        if (!(o && o.method === 'POST')) return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(P.a)});
        const first = n++ === 0;
        return Promise.resolve({ok: !first, status: first ? 428 : 200, json: () => Promise.resolve(
          first ? {code: 'confirm_dangerous', error: 'sicher?'} : {ok: true, queue: P.a})});
      };
      let liveInConfirm = -1;
      globalThis.confirm = () => { liveInConfirm = timers.filter(t => t.ms === 30000 && t.live).length; return true; };
      const e = P.a.entries[T_pos('Alpha')];
      const ok = await queueRun('move', {ref: e.ref, direction: 'down'});
      const slow = timers.filter(t => t.ms === 30000);
      return {ok, liveInConfirm, n: slow.length, live: slow.filter(t => t.live).length, posts: n};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS)})
    assert out == {"ok": True, "liveInConfirm": 0, "n": 2, "live": 0, "posts": 2}


def test_form_preview_sends_the_write_position_to_lint(tmp_path, qfile):
    body = LOAD + """
      T_form(P.lint);
      await ACTIONS['queue-new']();
      T_set('qf-text', 'x'); T_set('qf-pos', 'top'); await queuePreviewNow();
      const top = T_lint().pop().b.position;
      T_set('qf-pos', ''); await queuePreviewNow();
      return {top, dflt: T_lint().pop().b.position};"""
    out = _run(tmp_path, body, {"a": _state(qfile, ROWS), "lint": {"ok": True, "findings": []}, "meta": _meta()})
    assert out == {"top": "top", "dflt": "bottom"}


def test_a_lint_answer_for_older_input_never_unlocks_saving_during_the_debounce(tmp_path, qfile):
    a = _state(qfile, ROWS)
    lint_a, lint_b = _lint(qfile, {"text": "Eintrag A"}), _lint(qfile, {"text": "Eintrag B"})
    assert lint_a["line"] != lint_b["line"]
    body = LOAD + """
      T_form(P.lint_a);
      await ACTIONS['queue-new']();
      T_set('qf-text', 'Eintrag A');
      const base = globalThis.fetch, held = [], timers = [];
      globalThis.fetch = (u, o) => u === '/api/queue/lint'
        ? (T_calls.push({u, m: 'POST', b: JSON.parse(o.body)}), new Promise(r => held.push(r))) : base(u, o);
      globalThis.setTimeout = (fn, ms) => { timers.push([fn, ms]); return 7; };
      const ans = d => ({ok: true, status: 200, json: () => Promise.resolve(d)});
      const pa = queuePreviewNow(); await T_settle();                    // 1. lint request for text A is running
      T_set('qf-text', 'Eintrag B'); _docListeners.input[0]({target: {id: 'qf-text'}});   // 2. input changes, debounce runs
      held[0](ans(P.lint_a)); await pa; await T_settle();               // 3. answer A arrives
      const afterA = {lint: queueLint, save: document.getElementById('qf-save').disabled};
      const pb = timers.filter(t => t[1] === 300).pop()[0](); await T_settle();    // debounce fires: request for B
      held[1](ans(P.lint_b)); await pb; await T_settle();
      return {afterA, lint: queueLint && queueLint.line, save: document.getElementById('qf-save').disabled,
              shown: document.getElementById('qf-line').textContent, n: held.length};"""
    out = _run(tmp_path, body, {"a": a, "lint_a": lint_a, "lint_b": lint_b, "meta": _meta()})
    assert out["afterA"] == {"lint": None, "save": True}                 # 4. still locked
    assert out == {"afterA": out["afterA"], "lint": lint_b["line"], "save": False, "shown": lint_b["line"], "n": 2}   # 5. only B unlocks


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("interleave", [True, False])
def test_a_late_save_answer_closes_only_its_own_panel_and_never_a_newly_opened_one(tmp_path, qfile, raw, interleave):
    fields = {"text": "Aufraeumen"}
    a, lint, added = _form(qfile, ROWS, fields, ["- [ ] Aufraeumen"])
    full = queue_editor.queue_state(in_flight=None, mode="standalone", include_raw=True)
    full.update(ok=True, instance="i1")
    body = LOAD + """
      const RAW = @@;
      T_route = (u, o) => u === '/api/queue/meta' ? {status: 200, data: P.meta} : u === '/api/queue/lint' ? {status: 200, data: P.lint}
        : u === '/api/queue?raw=1' ? {status: 200, data: P.full} : u === '/api/queue' ? {status: 200, data: P.a} : null;
      const open = async () => { await ACTIONS[RAW ? 'queue-raw' : 'queue-new'](); };
      await open();
      if (!RAW) { T_set('qf-text', 'Aufraeumen'); await queuePreviewNow(); }
      const base = globalThis.fetch; let release;
      globalThis.fetch = (u, o) => (o && o.method === 'POST' && u === (RAW ? '/api/queue/raw' : '/api/queue/add'))
        ? (T_calls.push({u, m: 'POST', b: JSON.parse(o.body)}), new Promise(r => { release = r; })) : base(u, o);
      const saving = ACTIONS[RAW ? 'queue-raw-save' : 'queue-save'](); await T_settle();    // the user saves (slow)
      if (@@I) {
        ACTIONS['queue-cancel']();                                    // cancels, opens a new panel and types
        await open();
        T_set(RAW ? 'q-raw-text' : 'qf-text', 'neuer Entwurf');
      }
      release({ok: true, status: 200, json: () => Promise.resolve(RAW ? {ok: true, changed: true, queue: P.a} : P.added)});
      const ok = await saving; await T_settle();
      const f = document.getElementById(RAW ? 'q-raw-text' : 'qf-text');
      return {ok, mode: queueMode, draft: f ? f.value : null, html: document.getElementById(RAW ? 'q-raw' : 'q-form').innerHTML, written: queueWritten};""".replace("@@I", str(interleave).lower()).replace("@@", str(raw).lower())
    out = _run(tmp_path, body, {"a": a, "lint": lint, "added": added, "full": full, "meta": _meta()})
    assert out["ok"] is True
    if interleave:
        assert out["mode"] == ("raw" if raw else "form") and out["draft"] == "neuer Entwurf" and out["html"]   # the new panel and its text survive
    else:
        assert out["mode"] is None and out["html"] == ""                                            # normal case: the panel closes
    if not raw:
        assert out["written"] == "Aufraeumen"                       # queueRemember belongs to the file: it runs either way
