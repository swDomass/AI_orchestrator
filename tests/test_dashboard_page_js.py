"""The dashboard page's JavaScript, executed — not only syntax-checked.

The script lives in ``dashboard_ui/*.js`` (assembled by ``tests/_ui_scripts.py``
in index.html order), so nothing else would ever run it before a browser does.
These tests load it, run it in
``node`` against a minimal DOM/Chart.js stub and feed ``update()`` a payload
built by the real Python side (``provider_meta_map``, ``_limits_now``). Skipped
when ``node`` is not on the PATH (the GitHub ubuntu runners have it; a Windows
box may not) — then the page rendering stays "unverified", as before.
"""

import json
from datetime import datetime

import pytest
from _ui_scripts import NODE, NODE_STUB, boot_hidden_ids, index_buttons, page_script, run_node

import analytics
import dashboard

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

def _page_script() -> str:
    return page_script()


def _run(tmp_path, payload: dict, probe: str, pre: str = "") -> dict:
    return run_node(
        tmp_path, NODE_STUB + "\n" + pre + "\n" + _page_script()
        + "\nupdate(" + json.dumps(payload) + ");\n"
        + "console.log(JSON.stringify((function(){" + probe + "})()));\n")


def _payload() -> dict:
    now = datetime(2026, 10, 9, 8, 33, 18)
    lines = [
        analytics.LimitSnapshot(datetime(2026, 10, 9, 8, 26, 18), "claude_five_hour", 58.0, True),
        analytics.LimitSnapshot(datetime(2026, 10, 9, 8, 26, 18), "claude_seven_day", 39.0, True),
        analytics.LimitSnapshot(datetime(2026, 10, 9, 8, 26, 18), "codex_primary_window", 91.0, True),
        analytics.LimitSnapshot(datetime(2026, 10, 9, 7, 0, 0), "codex_secondary_window", 70.0, True),
        analytics.LimitSnapshot(datetime(2026, 10, 9, 8, 26, 18), "opencode", 100.0, True),
        analytics.LimitSnapshot(datetime(2026, 10, 9, 8, 26, 18), "gemini", -1.0, False),
    ]
    limits_now = analytics._limits_now(lines, now=now)
    recent = datetime.now().replace(microsecond=0).isoformat()
    timeline = {
        "claude_five_hour": [{"ts": recent, "pct": 58.0}],
        "codex_secondary_window": [{"ts": recent, "pct": 70.0}],
        "opencode": [{"ts": recent, "pct": 100.0}],
        "neuer_provider": [{"ts": recent, "pct": 50.0}],
    }
    data = {
        "generated_at": recent, "total_tasks": 3, "success_rate": 100.0,
        "avg_duration_sec": 1.0, "active_providers": ["claude"],
        "tasks_per_day": {"labels": [], "values": []},
        "provider_distribution": {"labels": ["claude", "codex", "opencode", "x6"], "values": [3, 2, 1, 1]},
        "limits_timeline": timeline, "limits_now": limits_now, "current_limits": {},
        "recent_events": [], "usage_suggest_today": 0, "session": {},
        "billing_recent": {}, "billing_total": {}, "cache_hit_rate_recent": None,
        "cache_hit_rate_total": None, "tool_trace_stats": {}, "failure_counts": {},
        "failure_timeline": {}, "active_runs": [],
    }
    data["provider_meta"] = dashboard.provider_meta_map(dashboard._provider_names_in(data))
    return data


def test_quota_tiles_render_all_five_voices_with_age(tmp_path):
    out = _run(tmp_path, _payload(), "return document.getElementById('quota-now').innerHTML;")
    for label in ("Claude 5 h", "Claude 7 Tage", "Codex 5 h", "Codex 7 Tage", "opencode (Tagesbudget)"):
        assert label in out, label
    assert "vor 7 min" in out
    assert "veraltet" in out           # codex secondary is 93 min old
    assert "nicht verfügbar" in out    # gemini -1.0/false
    # tile order follows the Python table
    assert out.index("Claude 5 h") < out.index("Codex 7 Tage") < out.index("opencode (Tagesbudget)")


def test_capacity_charts_place_opencode_and_unknown_with_colour(tmp_path):
    probe = """
      const by = {};
      for (const c of _charts) by[c.canvasId] = (c.data.datasets || []).map(d => [d.label, d.borderColor]);
      return by;
    """
    out = _run(tmp_path, _payload(), probe)
    assert out["limit-chart-short"] == [["Claude 5 h", "#6c63ff"]]
    assert out["limit-chart-long"] == [["Codex 7 Tage", "#81c784"]]
    other = dict(out["limit-chart-other"])
    assert other["opencode (Tagesbudget)"] == "#29b6f6"
    assert other["neuer provider"] in dashboard._REST_PALETTE


def test_provider_doughnut_gets_one_colour_per_segment(tmp_path):
    probe = """
      const pd = _charts.find(c => c.canvasId === 'pd-chart');
      return { labels: pd.data.labels, colours: pd.data.datasets[0].backgroundColor };
    """
    out = _run(tmp_path, _payload(), probe)
    assert out["labels"] == ["claude", "codex", "opencode", "x6"]
    assert len(out["colours"]) == 4
    assert len(set(out["colours"])) == 4
    assert out["colours"][2] == "#29b6f6"


def test_overview_range_buttons_ask_the_server_for_that_many_days(tmp_path):
    # /api/data returns only `days` days (default 7); without ?days= the 30/90 buttons
    # would have nothing more to show than the 7-day button.
    probe = """
      const urls = [];
      globalThis.fetch = (u) => { urls.push(u); return new Promise(() => {}); };
      setRange(90); setRange(7); setRange(30);
      return urls;
    """
    out = _run(tmp_path, _payload(), probe)
    assert out == ["/api/data?days=90", "/api/data?days=7", "/api/data?days=30"]


_ACTION_NAMES = {"show-tab", "set-range", "set-limit-range", "set-harness-days", "set-harness-window",
                 "set-harness-metric", "reset-zoom", "queue-reload-page", "queue-undo", "queue-new", "queue-raw",
                 "claude-billing-toggle"}
_RESET_CANVAS = {"tpd-reset": "tpd-chart", "lim-short-reset": "limit-chart-short",
                 "lim-other-reset": "limit-chart-other", "lim-long-reset": "limit-chart-long"}


def test_every_data_action_button_reaches_its_handler_through_one_listener(tmp_path):
    buttons = index_buttons()
    assert len(buttons) == 30
    assert {b["data-action"] for b in buttons} == _ACTION_NAMES
    probe = """
      const buttons = @@;
      const clicks = _docListeners.click || [];
      const urls = [], zoomed = [];
      globalThis.fetch = (u) => { urls.push(u); return new Promise(() => {}); };
      Chart.prototype.resetZoom = function () { zoomed.push(this.canvasId); };
      const out = { listeners: clicks.length, rows: [],
        actionsOk: buttons.every(b => typeof ACTIONS[b['data-action']] === 'function') };
      for (const b of buttons.slice().reverse()) {   // reversed: the last click differs from the default
        const ds = {};
        for (const [k, v] of Object.entries(b)) if (k.startsWith('data-')) ds[k.slice(5)] = v;
        const fake = { id: b.id || '', dataset: ds };
        urls.length = 0; zoomed.length = 0;
        clicks[0]({ target: { closest: (sel) => sel === '[data-action]' ? fake : null } });
        out.rows.push({ ds, urls: urls.slice(), zoomed: zoomed.slice(), id: fake.id,
          range: [_activeRange, typeof _activeRange], limit: [_activeLimitRange, typeof _activeLimitRange],
          hDays: [_hDays, typeof _hDays], hWindow: [_hWindow, typeof _hWindow], hMetric: _hMetric,
          harness: document.getElementById('tab-harness').style.display,
          main: document.getElementById('tab-main').style.display,
          queue: document.getElementById('tab-queue').style.display,
          form: document.getElementById('q-form').style.display,
          raw: document.getElementById('q-raw').style.display, reloaded: globalThis._reloaded || 0 });
      }
      return out;
    """.replace("@@", json.dumps(buttons))
    out = _run(tmp_path, _payload(), probe)
    assert out["listeners"] == 1 and out["actionsOk"] is True
    seen = []
    for row in out["rows"]:
        ds = row["ds"]
        seen.append(ds["action"])
        if ds["action"] == "show-tab":
            # (queue, harness, main) display per tab: only the chosen tab is visible
            want = {"main": ("none", "none", ""), "harness": ("none", "", "none"), "queue": ("", "none", "none")}[ds["tab"]]
            assert (row["queue"], row["harness"], row["main"]) == want
        elif ds["action"] == "set-range":
            n = int(ds["days"])
            assert row["range"] == [n, "number"] and row["urls"] == [f"/api/data?days={n}"]
        elif ds["action"] == "set-limit-range":
            assert row["limit"] == [int(ds["hours"]), "number"]
        elif ds["action"] == "set-harness-days":
            assert row["hDays"] == [int(ds["hdays"]), "number"]
        elif ds["action"] == "set-harness-window":
            assert row["hWindow"] == [int(ds["hwin"]), "number"]
        elif ds["action"] == "set-harness-metric":
            assert row["hMetric"] == ds["hmetric"]
        elif ds["action"] == "queue-reload-page":
            assert row["reloaded"] == 1
        elif ds["action"] == "queue-new":
            assert row["urls"] == ["/api/queue/meta"]       # the form first loads its choices
        elif ds["action"] == "queue-raw":
            assert row["urls"] == ["/api/queue?raw=1"]      # the raw view first loads the file text
        elif ds["action"] in ("queue-undo", "claude-billing-toggle"):
            # nothing loaded / no target -> no request; the lift button has a fixed target -> one POST
            assert row["urls"] == (["/api/claude-billing"] if row["id"] == "claude-billing-lift" else [])
            assert row["zoomed"] == []
        else:
            assert row["zoomed"] == [_RESET_CANVAS[row["id"]]]
    assert sorted(seen) == sorted(b["data-action"] for b in buttons)


def test_boot_hidden_elements_get_inline_display_none_and_lose_the_class(tmp_path):
    ids = boot_hidden_ids()
    assert sorted(ids) == ["claude-billing-lift", "failure-empty", "h-body", "q-form", "q-raw", "session-box", "tab-harness", "tab-queue"]
    pre = """
      const _bootEls = @@.map(id => {
        const e = _el(id);
        e.classList = { toggle() {}, add() {}, remove(c) { e._removed = c; e._displayAtRemove = e.style.display; } };
        return e;
      });
      document.querySelectorAll = (sel) => sel === '.boot-hidden' ? _bootEls : [];
    """.replace("@@", json.dumps(ids))
    probe = "return @@.map(id => [_el(id)._displayAtRemove, _el(id)._removed]);".replace("@@", json.dumps(ids))
    out = _run(tmp_path, _payload(), probe, pre=pre)
    assert out == [["none", "boot-hidden"]] * len(ids)


def test_quota_tile_colour_is_a_data_attribute_applied_through_the_cssom(tmp_path):
    payload = _payload()
    probe = """
      const box = document.getElementById('quota-now');
      const fakes = [];
      box.querySelectorAll = (sel) => {
        if (sel !== '[data-color]') return [];
        for (const m of box.innerHTML.matchAll(/data-color="([^"]*)"/g)) fakes.push({ dataset: { color: m[1] }, style: {} });
        return fakes;
      };
      renderQuotaNow(@@);
      return { html: box.innerHTML, colours: fakes.map(f => f.style.color) };
    """.replace("@@", json.dumps(payload["limits_now"]))
    out = _run(tmp_path, payload, probe)
    assert 'data-color="#6c63ff"' in out["html"]
    assert 'style="' not in out["html"]
    assert len(out["colours"]) == 6 and out["colours"][0] == "#6c63ff"  # five voices + gemini
    assert "" in out["colours"]   # stale / unavailable tiles carry no colour


# --- write barrier, frontend half: orchToken() / orchPost() / start ping ---------------------

_META_PRE = """
  globalThis._calls = [];
  globalThis._meta = { content: 'tok-1' };
  document.querySelector = (sel) => sel === 'meta[name="orch-token"]' ? globalThis._meta : null;
  globalThis.fetch = (u, o) => {
    globalThis._calls.push({ u, o });
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
  };
"""


def _run_async(tmp_path, body: str, pre: str = _META_PRE) -> dict:
    """Load the page, then await an async ``body`` (returns a JSON-able value)."""
    return run_node(
        tmp_path, NODE_STUB + "\n" + pre + "\n" + _page_script()
        + "\n(async () => {" + body + "})().then(r => console.log(JSON.stringify(r)));\n")


def test_orchpost_sends_json_post_with_token_header(tmp_path):
    body = """
      _calls.length = 0;
      const r = await orchPost('/api/x', {a: 1});
      return { r, call: _calls[0] };
    """
    out = _run_async(tmp_path, body)
    call = out["call"]
    assert call["u"] == "/api/x"
    assert call["o"]["method"] == "POST"
    assert call["o"]["headers"]["X-Orch-Token"] == "tok-1"
    assert call["o"]["headers"]["Content-Type"] == "application/json"
    assert json.loads(call["o"]["body"]) == {"a": 1}
    assert out["r"] == {"ok": True, "status": 200, "data": {"ok": True}}


def test_orchpost_does_not_throw_when_fetch_rejects(tmp_path):
    body = """
      globalThis.fetch = () => Promise.reject(new Error('net'));
      return await orchPost('/api/x', {});
    """
    assert _run_async(tmp_path, body) == {"ok": False, "status": 0, "data": None}


def test_orchpost_does_not_throw_on_broken_json(tmp_path):
    body = """
      globalThis.fetch = () => Promise.resolve({ ok: false, status: 500,
        json: () => Promise.reject(new SyntaxError('bad')) });
      return await orchPost('/api/x', {});
    """
    assert _run_async(tmp_path, body) == {"ok": False, "status": 500, "data": None}


def test_page_start_sends_exactly_one_ping_post(tmp_path):
    body = "for (let i = 0; i < 20; i++) await null; return { n: _calls.length, calls: _calls.map(c => [c.u, c.o && c.o.method]), ok: _orchWriteOk };"
    out = _run_async(tmp_path, body)
    posts = [c for c in out["calls"] if c[1] == "POST"]
    assert posts == [["/api/ping", "POST"]]
    assert out["ok"] is True


def test_orchtoken_is_read_at_call_time_not_at_load(tmp_path):
    body = """
      _meta.content = 'tok-2';
      _calls.length = 0;
      await orchPost('/api/x', {});
      _meta.content = 'tok-3';
      return { sent: _calls[0].o.headers['X-Orch-Token'], now: orchToken(),
               gone: (document.querySelector = () => null, orchToken()) };
    """
    out = _run_async(tmp_path, body)
    assert out == {"sent": "tok-2", "now": "tok-3", "gone": ""}


# --- Claude billing switch (Scheibe B) ---------------------------------------------------------


def test_render_claude_billing_sets_text_and_the_button_for_the_other_mode(tmp_path):
    probe = """
      const btn = document.getElementById('claude-billing-btn');
      const txt = document.getElementById('claude-billing-text');
      const out = [];
      for (const d of [{mode: 'api', text: 'X'}, {mode: 'abo', text: '<b>Y</b>'}]) {
        btn.disabled = true;
        renderClaudeBilling(d);
        out.push([txt.textContent, btn.textContent, btn.dataset.target, btn.disabled]);
      }
      return out;
    """
    out = _run(tmp_path, _payload(), probe)
    assert out == [["X", "Auf Abo umschalten", "abo", False],
                   ["<b>Y</b>", "Auf API-Guthaben umschalten", "api", False]]


def test_lift_button_is_visible_only_for_api_mode_with_a_block_and_hidden_on_failure(tmp_path):
    probe = """
      const btn = document.getElementById('claude-billing-btn');
      const lift = document.getElementById('claude-billing-lift');
      const out = [];
      for (const d of [{mode: 'api', text: 'X', blocked_until: 1.5e9}, {mode: 'api', text: 'X', blocked_until: null},
                       {mode: 'abo', text: 'X', blocked_until: 1.5e9}, {mode: 'api', text: 'X', blocked_until: 1.5e9}]) {
        lift.disabled = true;
        renderClaudeBilling(d);
        out.push([lift.style.display, lift.disabled, btn.textContent]);
      }
      claudeBillingFailed();
      out.push([lift.style.display]);
      return out;
    """
    out = _run(tmp_path, _payload(), probe)
    assert out == [["", False, "Auf Abo umschalten"], ["none", False, "Auf Abo umschalten"],
                   ["none", False, "Auf API-Guthaben umschalten"], ["", False, "Auf Abo umschalten"], ["none"]]


def test_lift_button_posts_api_and_both_buttons_are_locked_during_the_request(tmp_path):
    body = """
      const btn = document.getElementById('claude-billing-btn');
      const lift = document.getElementById('claude-billing-lift');
      lift.dataset.target = 'api';
      _calls.length = 0;
      let during = null, release;
      globalThis.fetch = (u, o) => { _calls.push({u, o}); during = [btn.disabled, lift.disabled];
        return new Promise(res => { release = () => res({ ok: true, status: 200,
          json: () => Promise.resolve({ok: true, mode: 'api', text: 'T', lifted_block: true, blocked_until: null}) }); }); };
      const p = ACTIONS['claude-billing-toggle'](lift);
      release();
      await p;
      return { body: JSON.parse(_calls[0].o.body), during, after: [btn.disabled, lift.disabled, lift.style.display] };
    """
    out = _run_async(tmp_path, body)
    assert out == {"body": {"mode": "api"}, "during": [True, True], "after": [False, False, "none"]}


def test_load_claude_billing_reads_the_endpoint_and_fails_into_a_disabled_button(tmp_path):
    body = """
      const btn = document.getElementById('claude-billing-btn');
      const txt = document.getElementById('claude-billing-text');
      _calls.length = 0;
      globalThis.fetch = (u, o) => { _calls.push({u, o}); return Promise.resolve({ ok: true, status: 200,
        json: () => Promise.resolve({ ok: true, mode: 'abo', text: 'Abo!' }) }); };
      await loadClaudeBilling();
      const good = [txt.textContent, btn.dataset.target, btn.disabled, _calls[0].u, _calls[0].o.cache];
      globalThis.fetch = () => Promise.reject(new Error('net'));
      await loadClaudeBilling();
      const down = [txt.textContent, btn.disabled];
      globalThis.fetch = () => Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({ok: false}) });
      btn.disabled = false;
      await loadClaudeBilling();
      return { good, down, http500: [txt.textContent, btn.disabled] };
    """
    out = _run_async(tmp_path, body)
    assert out["good"] == ["Abo!", "api", False, "/api/claude-billing", "no-store"]
    assert out["down"] == ["Abrechnung nicht lesbar", True]
    assert out["http500"] == ["Abrechnung nicht lesbar", True]


def test_claude_billing_toggle_posts_the_target_and_reports_lifted_block_or_errors(tmp_path):
    body = """
      const btn = document.getElementById('claude-billing-btn');
      const msg = document.getElementById('claude-billing-msg');
      const reply = (status, data) => { globalThis.fetch = (u, o) => { _calls.push({u, o});
        return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(data) }); }; };
      const rows = {};
      btn.dataset.target = 'api';
      _calls.length = 0;
      reply(200, {ok: true, mode: 'api', text: 'T', lifted_block: true});
      await ACTIONS['claude-billing-toggle'](btn);
      rows.ok = [JSON.parse(_calls[0].o.body), _calls[0].u, msg.textContent, btn.disabled, btn.dataset.target];
      reply(403, {ok: false, code: 'token', error: 'x'});
      btn.dataset.target = 'abo';
      await ACTIONS['claude-billing-toggle'](btn);
      rows.token = [msg.textContent, btn.disabled];
      reply(503, {ok: false, code: 'unavailable', error: 'Schalter nicht gespeichert.'});
      await ACTIONS['claude-billing-toggle'](btn);
      rows.err = [msg.textContent, btn.disabled];
      return rows;
    """
    out = _run_async(tmp_path, body)
    assert out["ok"] == [{"mode": "api"}, "/api/claude-billing", "Sperre aufgehoben.", False, "abo"]
    assert out["token"] == ["Seite neu laden (Dashboard neu gestartet).", False]
    assert out["err"] == ["Schalter nicht gespeichert.", False]


def test_page_start_loads_the_billing_state_once_from_its_own_endpoint(tmp_path):
    body = """
      for (let i = 0; i < 20; i++) await null;
      return _calls.map(c => c.u);
    """
    out = _run_async(tmp_path, body)
    assert out.count("/api/claude-billing") == 1
