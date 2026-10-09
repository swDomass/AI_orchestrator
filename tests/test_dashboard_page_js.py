"""The dashboard page's JavaScript, executed — not only syntax-checked.

The script lives in a Python string (``dashboard._HTML_PAGE``), so nothing else
would ever run it before a browser does. These tests extract it, run it in
``node`` against a minimal DOM/Chart.js stub and feed ``update()`` a payload
built by the real Python side (``provider_meta_map``, ``_limits_now``). Skipped
when ``node`` is not on the PATH (the GitHub ubuntu runners have it; a Windows
box may not) — then the page rendering stays "unverified", as before.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime

import pytest

import analytics
import dashboard

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Minimal browser surface: enough for the page script to load and render.
_STUB = r"""
const _els = {};
function _el(id) {
  if (!_els[id]) {
    _els[id] = {
      id, innerHTML: '', textContent: '', style: {}, dataset: {}, onclick: null,
      classList: { toggle() {}, add() {}, remove() {} },
      getContext() { return { canvasId: id }; },
      appendChild(c) { (this.children = this.children || []).push(c); },
      addEventListener() {},
    };
  }
  return _els[id];
}
globalThis.document = {
  getElementById: _el,
  querySelectorAll() { return []; },
  createElement() { return _el('__new' + Object.keys(_els).length); },
};
globalThis.window = globalThis;
globalThis.location = { hash: '' };
const _charts = [];
globalThis.Chart = function (ctx, cfg) {
  this.canvasId = ctx.canvasId; this.data = cfg.data; this.options = cfg.options;
  _charts.push(this);
};
Chart.prototype.update = function () {};
Chart.prototype.resetZoom = function () {};
Chart.register = function () {};
globalThis.fetch = () => new Promise(() => {});  // never resolves; we call update() ourselves
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
"""


def _page_script() -> str:
    scripts = re.findall(r"<script>(.*?)</script>", dashboard._HTML_PAGE, re.S)
    assert len(scripts) == 1
    return str(scripts[0])


def _run(tmp_path, payload: dict, probe: str) -> dict:
    js = tmp_path / "page_test.js"
    js.write_text(
        _STUB + "\n" + _page_script()
        + "\nupdate(" + json.dumps(payload) + ");\n"
        + "console.log(JSON.stringify((function(){" + probe + "})()));\n",
        encoding="utf-8",
    )
    assert NODE is not None
    proc = subprocess.run([NODE, str(js)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    result: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


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
