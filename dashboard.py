"""
AI Orchestrator — Web Dashboard

Lightweight HTTP server serving an analytics dashboard.
Uses only stdlib (http.server) + Chart.js via CDN.

Autostart (2026-10-09): ``python orchestrator.py --watch`` starts this server
itself via ``start_autostart()`` — a daemon thread on 127.0.0.1, no browser
(``config.DASHBOARD_OPEN_BROWSER``, default False), switched off with
``DASHBOARD_AUTOSTART=false``. It never raises into the orchestrator and never
makes ``run_watch`` wait: the bind happens inside the thread, and every failure
(no port, import error, ``serve_forever`` dying later) is one warning line.

Standalone usage (unchanged — these still open the browser unless --no-open):
    python dashboard.py              # open browser on port 8211 (or free fallback)
    python dashboard.py --port 9000  # custom port
    python dashboard.py --no-open    # don't auto-open browser

Programmatic usage:
    from dashboard import start_server
    start_server()                   # blocking
    start_server(background=True)    # returns immediately
    start_autostart()                # --watch path: never raises, never waits

The server is single-threaded (``socketserver.TCPServer``): one slow request
blocks the others, so every endpoint must answer quickly.
"""

import argparse
import contextlib
import hashlib
import json
import logging
import socketserver
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler

import config
from analytics import get_dashboard_data
from config import DASHBOARD_PORT

logger = logging.getLogger(__name__)

# ── Provider → group / colour / label: the ONE place (P2, 2026-10-09) ─────────
# The page's JavaScript only looks names up in `provider_meta` (shipped with
# /api/data), it does not decide anything itself — JS inside the HTML string is
# not testable, this table is. Capacity-chart groups: "short" = 5-hour windows,
# "long" = 7-day windows, "other" = daily budget & everything else (opencode,
# Gemini, unknown names). A name nobody listed here still gets a group, a stable
# colour from _REST_PALETTE and a readable label instead of vanishing.
LIMIT_GROUPS = ("short", "long", "other")

# base provider → (label, colour, colour of the long window)
_PROVIDER_BASES: dict[str, tuple[str, str, str]] = {
    "claude": ("Claude", "#6c63ff", "#b0a8ff"),
    "codex": ("Codex", "#4caf50", "#81c784"),
    "opencode": ("opencode", "#29b6f6", "#81d4fa"),
    "gemini": ("Gemini", "#ffc107", "#ffda6a"),
    "openrouter": ("OpenRouter", "#ec407a", "#f48fb1"),
    "vibe": ("Vibe", "#ff7043", "#ffab91"),
}
# Window suffix of the capacity-log key → (group, window label). Derived from
# the key's window pattern, not per full key. Codex primary/secondary = 5 h /
# 7 days: measured in a Codex rollout (2026-10-08, rate_limits.primary
# window_minutes 300, secondary 10080) — the key itself does not carry it.
_WINDOW_SUFFIXES: dict[str, tuple[str, str]] = {
    "five_hour": ("short", "5 h"),
    "primary_window": ("short", "5 h"),
    "seven_day": ("long", "7 Tage"),
    "secondary_window": ("long", "7 Tage"),
}
# A bare key without window (e.g. "opencode") sits in "other"; these get a label
# that says what the number means.
_BARE_LABELS: dict[str, str] = {
    "opencode": "opencode (Tagesbudget)",
}
# Tile order of "Quoten jetzt"; anything not listed follows alphabetically.
_TILE_ORDER = (
    "claude_five_hour", "claude_seven_day",
    "codex_primary_window", "codex_secondary_window",
    "opencode", "gemini",
)
# Rest colours for names not in _PROVIDER_BASES — more than there are providers,
# none equal to a base colour; on overflow they repeat (never an error).
_REST_PALETTE = (
    "#ef5350", "#ab47bc", "#26a69a", "#d4e157", "#8d6e63", "#5c6bc0",
    "#ffa726", "#26c6da", "#9ccc65", "#7e57c2", "#bdbdbd", "#ff8a65",
)
_FALLBACK_GROUP = "other"


def _stable_index(name: str, size: int) -> int:
    """Index into a palette that is the same for a name in every process
    (``hash()`` is salted per process, sha256 is not)."""
    return int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16) % size


def _gemini_model_label(name: str) -> str:
    # gemini_gemini_2_5_flash_ → "Gemini 2.5 Flash" (former providerLabel in JS)
    parts = [p for p in name.removeprefix("gemini_gemini_").split("_") if p]
    ver = [p for p in parts if p.isdigit()]
    words = [p[:1].upper() + p[1:] for p in parts if not p.isdigit()]
    return " ".join(["Gemini", ".".join(ver), *words]).replace("  ", " ").strip()


def provider_meta(name: str) -> dict:
    """Group, colour, label and tile order for one provider/window key.

    Never raises and never returns None: an unknown name lands in the "other"
    group with a stable rest colour.
    """
    name = str(name)
    base, _, suffix = name.partition("_")
    order = _TILE_ORDER.index(name) if name in _TILE_ORDER else len(_TILE_ORDER)
    known = _PROVIDER_BASES.get(base)
    if known is not None:
        label, colour, long_colour = known
        if not suffix:
            return {"group": _FALLBACK_GROUP, "color": colour,
                    "label": _BARE_LABELS.get(name, label), "order": order, "known": True}
        window = _WINDOW_SUFFIXES.get(suffix)
        if window is not None:
            group, window_label = window
            return {"group": group, "color": colour if group == "short" else long_colour,
                    "label": f"{label} {window_label}", "order": order, "known": True}
        if base == "gemini":
            return {"group": _FALLBACK_GROUP, "color": long_colour if name.startswith("gemini_gemini_1") else colour,
                    "label": _gemini_model_label(name) if name.startswith("gemini_gemini_") else name.replace("_", " "),
                    "order": order, "known": True}
    return {
        "group": _FALLBACK_GROUP,
        "color": _REST_PALETTE[_stable_index(name, len(_REST_PALETTE))],
        "label": name.replace("_", " "),
        "order": order,
        "known": False,
    }


def provider_meta_map(names) -> dict[str, dict]:
    """``provider_meta`` for every name, with distinct rest colours.

    Unknown names keep their stable palette slot unless another name in the same
    set already uses that colour; then the next free slot is taken (sorted
    order, so the result is deterministic). Only when the palette is used up do
    colours repeat — the doughnut never runs out of colours.
    """
    result: dict[str, dict] = {}
    used: set[str] = set()
    unknown: list[str] = []
    for name in sorted({str(n) for n in names if n is not None}):
        meta = provider_meta(name)
        result[name] = meta
        if meta["known"]:
            used.add(meta["color"])
        else:
            unknown.append(name)
    for name in unknown:
        start = _stable_index(name, len(_REST_PALETTE))
        for step in range(len(_REST_PALETTE)):
            candidate = _REST_PALETTE[(start + step) % len(_REST_PALETTE)]
            if candidate not in used:
                result[name]["color"] = candidate
                break
        used.add(result[name]["color"])
    return result


def _provider_names_in(data: dict) -> set[str]:
    """Every provider/window name the page will draw from a /api/data payload."""
    names: set[str] = set()
    names.update((data.get("limits_timeline") or {}).keys())
    names.update((data.get("limits_now") or {}).keys())
    names.update((data.get("provider_distribution") or {}).get("labels") or [])
    return names


_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Orchestrator Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2/dist/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@1/dist/chartjs-plugin-zoom.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e0e0e0; --muted: #888; --accent: #6c63ff;
    --green: #4caf50; --red: #ef5350; --yellow: #ffc107;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); padding: 1.5rem;
  }
  header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border);
  }
  header h1 { font-size: 1.4rem; font-weight: 600; }
  header .ts { color: var(--muted); font-size: 0.85rem; }
  .cards {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1rem; margin-bottom: 1.5rem;
  }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1.2rem; text-align: center;
  }
  .card .value { font-size: 2rem; font-weight: 700; color: var(--accent); }
  .card .label { font-size: 0.8rem; color: var(--muted); margin-top: 0.3rem; }
  .quota-now h3 { font-size: 0.9rem; margin-bottom: 0.8rem; color: var(--muted); }
  .card .age { font-size: 0.75rem; color: var(--muted); margin-top: 0.2rem; }
  .card .badge { display: inline-block; font-size: 0.7rem; border-radius: 4px; padding: 0.05rem 0.4rem; margin-top: 0.3rem; }
  .card.stale .value { color: var(--muted); }
  .card.stale .badge.stale { background: var(--yellow); color: #000; }
  .card.unavailable .value { color: var(--red); font-size: 1.1rem; }
  .card.unavailable .badge.unavail { background: var(--red); color: #fff; }
  .charts {
    display: grid; grid-template-columns: 2fr 1fr; gap: 1rem; margin-bottom: 1.5rem;
  }
  .chart-box {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem;
  }
  .chart-box h3 { font-size: 0.9rem; margin-bottom: 0.8rem; color: var(--muted); }
  .chart-box canvas { width: 100% !important; }
  .timeline-box {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem;
  }
  .timeline-section {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem; margin-bottom: 1.5rem;
  }
  .timeline-head {
    display: flex; justify-content: space-between; align-items: center;
    gap: 1rem; margin-bottom: 0.8rem; flex-wrap: wrap;
  }
  .timeline-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 1rem;
  }
  .timeline-box h3 { font-size: 0.9rem; margin-bottom: 0.8rem; color: var(--muted); }
  .events-box {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem; margin-bottom: 1.5rem;
  }
  .events-box h3 { font-size: 0.9rem; margin-bottom: 0.8rem; color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.5rem 0.8rem; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  .tag-error { color: var(--red); }
  .tag-queue { color: var(--accent); }
  .tag-suggest { color: var(--yellow); }
  .time-btns { display: flex; gap: 0.5rem; margin-bottom: 0.8rem; }
  .time-btn { background: var(--surface); border: 1px solid var(--border); color: var(--muted); border-radius: 6px; padding: 0.3rem 0.8rem; cursor: pointer; font-size: 0.8rem; }
  .time-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .zoom-reset { background: none; border: none; color: var(--muted); font-size: 0.75rem; cursor: pointer; padding: 0.2rem 0; text-decoration: underline; display: block; margin-top: 0.4rem; }
  .session-box {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem;
  }
  .session-box h3 { font-size: 0.9rem; margin-bottom: 0.8rem; color: var(--muted); }
  .session-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.8rem; }
  .session-grid .item { font-size: 0.85rem; }
  .session-grid .item span { color: var(--accent); font-weight: 600; }
  .billing-box, .tool-stats-box, .failure-box {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem; margin-bottom: 1.5rem;
  }
  .billing-box h3, .tool-stats-box h3, .failure-box h3 { font-size: 0.9rem; margin-bottom: 0.8rem; color: var(--muted); }
  .billing-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0.8rem;
  }
  .billing-cell {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 0.7rem; text-align: center;
  }
  .billing-cell .label { font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .billing-cell .val { font-size: 1.2rem; font-weight: 600; color: var(--accent); margin-top: 0.2rem; }
  .billing-cell .sub { font-size: 0.7rem; color: var(--muted); margin-top: 0.1rem; }
  .failure-grid {
    display: grid; grid-template-columns: 1fr 2fr; gap: 1rem;
  }
  .empty-hint { color: var(--muted); font-size: 0.85rem; font-style: italic; padding: 0.4rem 0; }
  .active-box {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem; margin-bottom: 1.5rem;
  }
  .active-box h3 { font-size: 0.9rem; margin-bottom: 0.8rem; color: var(--muted); }
  .active-box .live-dot {
    display: inline-block; width: 0.6em; height: 0.6em; border-radius: 50%;
    background: var(--green); margin-right: 0.4em; vertical-align: middle;
    animation: pulse 1.4s infinite ease-in-out;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  .active-box table { table-layout: auto; }
  .active-box td.task-cell {
    max-width: 28ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .active-box tr.stale td { color: var(--muted); font-style: italic; }
  .active-box tr.stale .live-dot { background: var(--yellow); animation: none; }
  @media (max-width: 700px) {
    .charts { grid-template-columns: 1fr; }
    .timeline-grid { grid-template-columns: 1fr; }
    .failure-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<header>
  <h1>AI Orchestrator</h1>
  <span class="ts" id="gen-ts">—</span>
</header>

<section class="quota-now">
  <h3>Quoten jetzt</h3>
  <div class="cards" id="quota-now">
    <div class="card"><div class="value">—</div><div class="label">Noch keine Kapazitätswerte</div></div>
  </div>
</section>

<section class="active-box">
  <h3><span class="live-dot"></span>Active Runs <span id="active-count">(0)</span></h3>
  <table>
    <thead><tr>
      <th>Tool</th><th>Task</th><th>Iter</th><th>Phase</th>
      <th>Provider</th><th>Tokens (in / out / cache rd)</th><th>Elapsed</th>
    </tr></thead>
    <tbody id="active-runs-body">
      <tr><td colspan="7" class="empty-hint">No active runs.</td></tr>
    </tbody>
  </table>
</section>

<div class="cards">
  <div class="card"><div class="value" id="total-tasks">—</div><div class="label">Tasks gesamt</div></div>
  <div class="card"><div class="value" id="success-rate">—</div><div class="label">Erfolgsrate</div></div>
  <div class="card"><div class="value" id="avg-dur">—</div><div class="label">Ø Dauer (s)</div></div>
  <div class="card"><div class="value" id="providers">—</div><div class="label">Aktive Provider</div></div>
  <div class="card"><div class="value" id="suggest-today">—</div><div class="label">Vorschläge heute</div></div>
</div>

<div class="charts">
  <div class="chart-box">
    <h3>Tasks / Tag</h3>
    <div class="time-btns">
      <button class="time-btn" data-days="7" onclick="setRange(7)">7 Tage</button>
      <button class="time-btn active" data-days="30" onclick="setRange(30)">30 Tage</button>
      <button class="time-btn" data-days="90" onclick="setRange(90)">90 Tage</button>
    </div>
    <canvas id="tpd-chart" height="180"></canvas>
    <button class="zoom-reset" id="tpd-reset">Zoom zurücksetzen</button>
  </div>
  <div class="chart-box">
    <h3>Provider-Verteilung</h3>
    <canvas id="pd-chart" height="180"></canvas>
  </div>
</div>

<div class="timeline-section">
  <div class="timeline-head">
    <h3>Provider-Kapazität</h3>
    <div class="time-btns">
      <button class="time-btn" data-hours="48" onclick="setLimitRange(48)">48 h</button>
      <button class="time-btn active" data-hours="168" onclick="setLimitRange(168)">7 Tage</button>
      <button class="time-btn" data-hours="720" onclick="setLimitRange(720)">30 Tage</button>
    </div>
  </div>
  <div class="timeline-grid">
    <div class="timeline-box">
      <h3>5-h-Fenster (Claude 5 h, Codex 5 h)</h3>
      <canvas id="limit-chart-short" height="140"></canvas>
      <button class="zoom-reset" id="lim-short-reset">Zoom zurücksetzen</button>
    </div>
    <div class="timeline-box">
      <h3>Tagesbudget &amp; Sonstige (opencode, Gemini, weitere)</h3>
      <canvas id="limit-chart-other" height="140"></canvas>
      <button class="zoom-reset" id="lim-other-reset">Zoom zurücksetzen</button>
    </div>
    <div class="timeline-box">
      <h3>7-Tage-Fenster (Claude 7 Tage, Codex 7 Tage)</h3>
      <canvas id="limit-chart-long" height="140"></canvas>
      <button class="zoom-reset" id="lim-long-reset">Zoom zurücksetzen</button>
    </div>
  </div>
</div>

<div class="billing-box">
  <h3>Token-Verbrauch &amp; Cache-Hit-Rate</h3>
  <div class="billing-grid" id="billing-recent"></div>
  <div style="margin-top:0.8rem"><div class="billing-grid" id="billing-total"></div></div>
</div>

<div class="tool-stats-box">
  <h3>Tool-Action-Stats</h3>
  <table>
    <thead><tr>
      <th>Tool</th><th>Runs</th><th>Completed</th><th>Erfolg</th><th>Ø Dauer (s)</th><th>Events</th>
    </tr></thead>
    <tbody id="tool-stats-body"></tbody>
  </table>
</div>

<div class="failure-box">
  <h3>Fehler-Kategorien (Taxonomy)</h3>
  <div class="failure-grid">
    <div>
      <canvas id="failure-doughnut" height="220"></canvas>
    </div>
    <div>
      <canvas id="failure-timeline" height="220"></canvas>
    </div>
  </div>
  <div id="failure-empty" class="empty-hint" style="display:none">Keine Fehler im gewählten Zeitraum.</div>
</div>

<div class="events-box">
  <h3>Letzte Events</h3>
  <table>
    <thead><tr><th>Zeit</th><th>Typ</th><th>Nachricht</th></tr></thead>
    <tbody id="events-body"></tbody>
  </table>
</div>

<div class="session-box" id="session-box" style="display:none">
  <h3>Session-Stats (aktiv)</h3>
  <div class="session-grid" id="session-grid"></div>
</div>

<script>
function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
// Provider group/colour/label come from Python (dashboard.provider_meta_map,
// shipped as `provider_meta` in /api/data) — no mapping logic lives here.
let _providerMeta = {};
function providerMeta(name) {
  return _providerMeta[name] || { group: 'other', color: '#888', label: String(name), order: 99 };
}
const chartOpts = {
  responsive: true,
  plugins: { legend: { labels: { color: '#888' } } },
  scales: {
    x: { ticks: { color: '#666' }, grid: { color: '#2a2d3a' } },
    y: { ticks: { color: '#666' }, grid: { color: '#2a2d3a' }, beginAtZero: true },
  },
};
const zoomPlugin = {
  zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' },
  pan:  { enabled: true, mode: 'x' },
};

let tpdChart, pdChart, limitShortChart, limitOtherChart, limitLongChart;
let failureDoughnut, failureTimeline;
let _allTpd = { labels: [], values: [] };
let _activeRange = 30;
let _allLimits = {};
let _activeLimitRange = 168;

const FAILURE_COLORS = [
  '#ef5350', '#ffa726', '#ffc107', '#66bb6a', '#26c6da',
  '#42a5f5', '#7e57c2', '#ec407a', '#8d6e63', '#bdbdbd',
  '#5c6bc0', '#26a69a', '#9ccc65', '#d4e157', '#ff7043', '#ab47bc',
];
function failureColor(i) { return FAILURE_COLORS[i % FAILURE_COLORS.length]; }

function formatNumber(n) {
  if (n == null || isNaN(n)) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'G';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

function safeResetZoom(chart) {
  if (chart && typeof chart.resetZoom === 'function') chart.resetZoom();
}

function setRange(days) {
  _activeRange = days;
  document.querySelectorAll('.time-btn[data-days]').forEach(b =>
    b.classList.toggle('active', +b.dataset.days === days));
  applyRange();
}

function setLimitRange(hours) {
  _activeLimitRange = hours;
  document.querySelectorAll('.time-btn[data-hours]').forEach(b =>
    b.classList.toggle('active', +b.dataset.hours === hours));
  applyLimitRange();
}

function tsKey(ts) {
  // "2026-03-03T14:00:38" → "2026-03-03T14:00" (sortable key)
  return ts.slice(0, 16);
}

function providerInLimitGroup(provider, group) {
  return providerMeta(provider).group === group;
}

function buildLimitChartData(group) {
  const cutoff = new Date(Date.now() - _activeLimitRange * 3600 * 1000);

  // 1. Collect all unique sorted labels across every provider
  const labelSet = new Set();
  const provFiltered = {};
  for (const [prov, pts] of Object.entries(_allLimits)) {
    if (!providerInLimitGroup(prov, group)) continue;
    const filtered = pts.filter(p => new Date(p.ts) >= cutoff);
    if (!filtered.length) continue;
    provFiltered[prov] = filtered;
    for (const p of filtered) labelSet.add(tsKey(p.ts));
  }
  const labels = Array.from(labelSet).sort();

  // 2. Each dataset uses {x, y} so Chart.js places points at their actual label
  const datasets = [];
  for (const [prov, pts] of Object.entries(provFiltered)) {
    datasets.push({
      label: providerMeta(prov).label,
      data: pts.map(p => ({ x: tsKey(p.ts), y: p.pct })),
      borderColor: providerMeta(prov).color,
      backgroundColor: 'transparent',
      tension: 0.3,
      pointRadius: 2,
    });
  }

  return { labels, datasets };
}

function updateLimitChart(chart, group) {
  if (!chart) return;
  const d = buildLimitChartData(group);
  chart.data.labels = d.labels;
  chart.data.datasets = d.datasets;
  safeResetZoom(chart);
  chart.update();
}

function applyLimitRange() {
  updateLimitChart(limitShortChart, 'short');
  updateLimitChart(limitOtherChart, 'other');
  updateLimitChart(limitLongChart, 'long');
}

function applyRange() {
  const n = _activeRange;
  const labels = _allTpd.labels.slice(-n).map(l => l.slice(5));
  const values = _allTpd.values.slice(-n);
  tpdChart.data.labels = labels;
  tpdChart.data.datasets[0].data = values;
  safeResetZoom(tpdChart);
  tpdChart.update();
}

function initCharts() {
  const tpdCtx = document.getElementById('tpd-chart').getContext('2d');
  tpdChart = new Chart(tpdCtx, {
    type: 'bar',
    data: { labels: [], datasets: [{ label: 'Tasks', data: [], backgroundColor: '#6c63ff88', borderColor: '#6c63ff', borderWidth: 1 }] },
    options: {
      ...chartOpts,
      plugins: { legend: { display: false }, zoom: zoomPlugin },
    },
  });

  const pdCtx = document.getElementById('pd-chart').getContext('2d');
  pdChart = new Chart(pdCtx, {
    type: 'doughnut',
    data: { labels: [], datasets: [{ data: [], backgroundColor: [] }] },
    options: { responsive: true, plugins: { legend: { labels: { color: '#888' }, position: 'bottom' } } },
  });

  function createLimitChart(canvasId) {
    const limCtx = document.getElementById(canvasId).getContext('2d');
    return new Chart(limCtx, {
      type: 'line',
      data: { datasets: [] },
      options: {
        ...chartOpts,
        plugins: { legend: { labels: { color: '#888' } }, zoom: zoomPlugin },
        scales: {
          ...chartOpts.scales,
          x: {
            ...chartOpts.scales.x,
            type: 'category',
            ticks: {
              ...chartOpts.scales.x.ticks,
              callback: function(value) {
                const raw = this.getLabelForValue(value);
                return raw && raw.length >= 16 ? (raw.slice(5, 10) + ' ' + raw.slice(11, 16)) : raw;
              },
            },
          },
          y: { ...chartOpts.scales.y, min: 0, max: 100, title: { display: true, text: '%', color: '#888' } },
        },
      },
    });
  }

  limitShortChart = createLimitChart('limit-chart-short');
  limitOtherChart = createLimitChart('limit-chart-other');
  limitLongChart = createLimitChart('limit-chart-long');

  document.getElementById('tpd-reset').onclick = () => safeResetZoom(tpdChart);
  document.getElementById('lim-short-reset').onclick = () => safeResetZoom(limitShortChart);
  document.getElementById('lim-other-reset').onclick = () => safeResetZoom(limitOtherChart);
  document.getElementById('lim-long-reset').onclick = () => safeResetZoom(limitLongChart);

  const fdCtx = document.getElementById('failure-doughnut').getContext('2d');
  failureDoughnut = new Chart(fdCtx, {
    type: 'doughnut',
    data: { labels: [], datasets: [{ data: [], backgroundColor: [] }] },
    options: { responsive: true, plugins: { legend: { labels: { color: '#888' }, position: 'right' } } },
  });

  const ftCtx = document.getElementById('failure-timeline').getContext('2d');
  failureTimeline = new Chart(ftCtx, {
    type: 'bar',
    data: { labels: [], datasets: [] },
    options: {
      ...chartOpts,
      plugins: { legend: { labels: { color: '#888' } } },
      scales: {
        ...chartOpts.scales,
        x: { ...chartOpts.scales.x, stacked: true },
        y: { ...chartOpts.scales.y, stacked: true },
      },
    },
  });
}

function _billingCell(label, value, sub) {
  const cell = document.createElement('div');
  cell.className = 'billing-cell';
  cell.innerHTML = '<div class="label">' + escapeHtml(label) + '</div>'
    + '<div class="val">' + escapeHtml(value) + '</div>'
    + (sub ? '<div class="sub">' + escapeHtml(sub) + '</div>' : '');
  return cell;
}

function renderBilling(containerId, billing, cacheHitRate, periodLabel) {
  const c = document.getElementById(containerId);
  c.innerHTML = '';
  const b = billing || {};
  c.appendChild(_billingCell('Input', formatNumber(b.input_tokens || 0), periodLabel));
  c.appendChild(_billingCell('Output', formatNumber(b.output_tokens || 0), periodLabel));
  c.appendChild(_billingCell('Cache erstellt', formatNumber(b.cache_creation_input_tokens || 0), periodLabel));
  c.appendChild(_billingCell('Cache gelesen', formatNumber(b.cache_read_input_tokens || 0), periodLabel));
  c.appendChild(_billingCell('Weighted Units', formatNumber(Math.round(b.weighted_units || 0)), periodLabel));
  const hit = cacheHitRate == null ? '—' : (cacheHitRate.toFixed(1) + '%');
  c.appendChild(_billingCell('Cache-Hit-Rate', hit, periodLabel));
}

function renderToolStats(stats) {
  const tbody = document.getElementById('tool-stats-body');
  tbody.innerHTML = '';
  const entries = Object.entries(stats || {});
  if (!entries.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="6" class="empty-hint">Noch keine Tool-Traces erfasst.</td>';
    tbody.appendChild(tr);
    return;
  }
  entries.sort((a, b) => (b[1].runs || 0) - (a[1].runs || 0));
  for (const [tool, s] of entries) {
    const successRate = s.completed_runs ? (100 * (s.success_runs || 0) / s.completed_runs) : 0;
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + escapeHtml(tool) + '</td>'
      + '<td>' + (s.runs || 0) + '</td>'
      + '<td>' + (s.completed_runs || 0) + '</td>'
      + '<td>' + successRate.toFixed(0) + '%</td>'
      + '<td>' + (s.avg_duration_sec ?? 0).toFixed(1) + '</td>'
      + '<td>' + (s.total_events || 0) + '</td>';
    tbody.appendChild(tr);
  }
}

function renderFailures(counts, timeline) {
  const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
  const empty = entries.length === 0;
  document.getElementById('failure-empty').style.display = empty ? '' : 'none';

  failureDoughnut.data.labels = entries.map(e => e[0]);
  failureDoughnut.data.datasets[0].data = entries.map(e => e[1]);
  failureDoughnut.data.datasets[0].backgroundColor = entries.map((_, i) => failureColor(i));
  failureDoughnut.update();

  // Stacked bar: x = days, datasets = categories
  const days = Object.keys(timeline || {}).sort();
  const categories = entries.map(e => e[0]);
  const datasets = categories.map((cat, i) => ({
    label: cat,
    data: days.map(d => (timeline[d] || {})[cat] || 0),
    backgroundColor: failureColor(i),
  }));
  failureTimeline.data.labels = days.map(d => d.slice(5));
  failureTimeline.data.datasets = datasets;
  failureTimeline.update();
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + 'm ' + s + 's';
  const h = Math.floor(m / 60), mm = m % 60;
  return h + 'h ' + mm + 'm';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function renderActiveRuns(runs) {
  runs = runs || [];
  const body = document.getElementById('active-runs-body');
  const count = document.getElementById('active-count');
  count.textContent = '(' + runs.length + ')';
  if (runs.length === 0) {
    body.innerHTML = '<tr><td colspan="7" class="empty-hint">No active runs.</td></tr>';
    return;
  }
  body.innerHTML = runs.map(r => {
    const t = r.tokens || {};
    const iter = (r.iteration_current || 0) + ' / ' + (r.iteration_max || '?');
    const stale = r.status === 'stale' ? ' stale' : '';
    return '<tr class="' + stale.trim() + '">'
      + '<td>' + escapeHtml(r.tool || '') + '</td>'
      + '<td class="task-cell" title="' + escapeHtml(r.task || '') + '">'
        + escapeHtml(r.task || '') + '</td>'
      + '<td>' + iter + '</td>'
      + '<td>' + escapeHtml(r.phase || '—') + '</td>'
      + '<td>' + escapeHtml(r.provider || '') + '</td>'
      + '<td>' + formatNumber(t.input || 0) + ' / '
        + formatNumber(t.output || 0) + ' / '
        + formatNumber(t.cache_read || 0) + '</td>'
      + '<td>' + fmtDuration(r.elapsed_sec) + '</td>'
      + '</tr>';
  }).join('');
}

async function refreshActiveRuns() {
  try {
    const r = await fetch('/api/data?only=active_runs');
    if (r.ok) {
      const data = await r.json();
      renderActiveRuns(data.active_runs || []);
    }
  } catch (e) { console.warn('active-runs fetch failed', e); }
}

function fmtAge(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return 'gerade eben';
  if (sec < 3600) return 'vor ' + Math.floor(sec / 60) + ' min';
  if (sec < 86400) return 'vor ' + Math.floor(sec / 3600) + ' h';
  return 'vor ' + Math.floor(sec / 86400) + ' Tagen';
}

function renderQuotaNow(now) {
  const box = document.getElementById('quota-now');
  const entries = Object.entries(now || {});
  if (!entries.length) {
    box.innerHTML = '<div class="card"><div class="value">—</div>'
      + '<div class="label">Noch keine Kapazitätswerte (logs/capacity-log.md)</div></div>';
    return;
  }
  entries.sort((a, b) => (providerMeta(a[0]).order - providerMeta(b[0]).order) || a[0].localeCompare(b[0]));
  box.innerHTML = entries.map(([name, q]) => {
    const meta = providerMeta(name);
    const cls = ['card'];
    if (q.stale) cls.push('stale');
    if (!q.available) cls.push('unavailable');
    let value;
    if (!q.available) {
      value = 'nicht verfügbar' + (q.remaining_pct != null ? ' (' + q.remaining_pct.toFixed(0) + ' %)' : '');
    } else {
      value = q.remaining_pct.toFixed(0) + ' %';
    }
    const badges = (q.stale ? '<span class="badge stale">veraltet</span> ' : '')
      + (!q.available ? '<span class="badge unavail">nicht verfügbar</span>' : '');
    return '<div class="' + cls.join(' ') + '" title="' + escapeHtml(name + ' · Stand ' + q.ts) + '">'
      + '<div class="value" style="color:' + escapeHtml(q.available && !q.stale ? meta.color : '') + '">' + escapeHtml(value) + '</div>'
      + '<div class="label">' + escapeHtml(meta.label) + '</div>'
      + '<div class="age">' + escapeHtml(fmtAge(q.age_sec)) + '</div>'
      + (badges ? '<div>' + badges + '</div>' : '')
      + '</div>';
  }).join('');
}

function update(d) {
  _providerMeta = d.provider_meta || {};
  document.getElementById('gen-ts').textContent = 'Stand: ' + d.generated_at;
  document.getElementById('total-tasks').textContent = d.total_tasks;
  document.getElementById('success-rate').textContent = d.success_rate + '%';
  document.getElementById('avg-dur').textContent = d.avg_duration_sec;
  document.getElementById('providers').textContent = (d.active_providers || []).length;
  document.getElementById('suggest-today').textContent = d.usage_suggest_today ?? '—';
  renderActiveRuns(d.active_runs);

  // Tasks per day — store full 90-day data, then apply active range
  _allTpd = d.tasks_per_day || { labels: [], values: [] };
  applyRange();

  // Quoten jetzt — newest value per exact key (analytics._limits_now)
  renderQuotaNow(d.limits_now);

  // Provider dist — one colour per label from provider_meta (distinct, never runs out)
  const pdLabels = d.provider_distribution.labels || [];
  pdChart.data.labels = pdLabels;
  pdChart.data.datasets[0].data = d.provider_distribution.values || [];
  pdChart.data.datasets[0].backgroundColor = pdLabels.map(l => providerMeta(l).color);
  pdChart.update();

  // Limits timeline — store full history and apply active range to all three charts
  _allLimits = d.limits_timeline || {};
  applyLimitRange();

  // Billing + cache-hit-rate
  renderBilling('billing-recent', d.billing_recent, d.cache_hit_rate_recent, 'letzte ' + _activeRange + ' Tage');
  renderBilling('billing-total', d.billing_total, d.cache_hit_rate_total, 'gesamt');

  // Tool action stats
  renderToolStats(d.tool_trace_stats);

  // Failure taxonomy
  renderFailures(d.failure_counts, d.failure_timeline);

  // Events
  const typeClass = { error: 'tag-error', queue: 'tag-queue', suggest: 'tag-suggest' };
  const tbody = document.getElementById('events-body');
  tbody.innerHTML = '';
  for (const ev of (d.recent_events || [])) {
    const tr = document.createElement('tr');
    const cls = typeClass[ev.type] || 'tag-queue';
    tr.innerHTML = '<td>' + escapeHtml(ev.ts.slice(0, 16).replace('T', ' ')) + '</td>'
      + '<td class="' + cls + '">' + escapeHtml(ev.type) + '</td>'
      + '<td>' + escapeHtml(ev.msg) + '</td>';
    tbody.appendChild(tr);
  }

  // Session
  const s = d.session || {};
  const box = document.getElementById('session-box');
  if (s.started_at) {
    box.style.display = '';
    document.getElementById('session-grid').innerHTML =
      '<div class="item">Erledigt: <span>' + (s.tasks_done || 0) + '</span></div>' +
      '<div class="item">Fehler: <span>' + (s.tasks_failed || 0) + '</span></div>' +
      '<div class="item">Gestartet: <span>' + s.started_at.slice(11, 16) + '</span></div>' +
      '<div class="item">Provider: <span>' + Object.keys(s.providers_used || {}).join(', ') + '</span></div>';
  } else {
    box.style.display = 'none';
  }
}

async function load() {
  try {
    const r = await fetch('/api/data');
    if (r.ok) update(await r.json());
  } catch (e) { console.warn('fetch failed', e); }
}

initCharts();
load();
setInterval(load, 60000);
setInterval(refreshActiveRuns, 30000);
</script>
</body>
</html>
"""


_CLIENT_DISCONNECT_ERRORS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class _Handler(BaseHTTPRequestHandler):
    """Handles GET / (HTML) and GET /api/data (JSON)."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/data":
                self._json_response(parsed.query)
            elif parsed.path in ("/", "/index.html"):
                self._html_response()
            else:
                self.send_error(404)
        except _CLIENT_DISCONNECT_ERRORS as e:
            logger.debug("dashboard: client disconnected during %s (%s)", parsed.path, e)

    def _html_response(self):
        body = _HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_response(self, query_string: str = ""):
        try:
            params = urllib.parse.parse_qs(query_string)
            only = params.get("only", [""])[0]
            if only == "active_runs":
                # Lightweight poll endpoint — bypass the full dashboard cache
                # and only read the live registry. Used by the 30s active-runs
                # refresh tick to avoid latency from billing/replay aggregation.
                from analytics import _load_active_runs
                data = {"active_runs": _load_active_runs()}
            else:
                days = max(1, min(int(params.get("days", ["7"])[0]), 365))
                # Copy: get_dashboard_data() returns its 30-s cache object.
                full: dict = dict(get_dashboard_data(days=days))
                full["provider_meta"] = provider_meta_map(_provider_names_in(full))
                data = full
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        except Exception as e:
            logger.exception("dashboard data error")
            body = json.dumps({"error": str(e)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except _CLIENT_DISCONNECT_ERRORS as e:
            logger.debug("dashboard: client disconnect (%s)", e)
            self.close_connection = True

    def log_message(self, format, *args):
        """Suppress default stderr logging; use logger instead."""
        logger.debug("dashboard: %s", format % args)


class _ReuseServer(socketserver.TCPServer):
    allow_reuse_address = True


def _bind_server(port: int) -> tuple["_ReuseServer", int]:
    """Bind the dashboard to 127.0.0.1, tolerating an unavailable port.

    Windows dynamically reserves port ranges for Hyper-V/WSL (bind then fails
    with WSAEACCES / WinError 10013), and the preferred port may simply be in
    use. Try the preferred port first, then a few spaced fallbacks, and as a
    last resort let the OS pick a free ephemeral port — so the dashboard always
    comes up. Returns the bound server and the actual port.
    """
    candidates = [port, port + 100, port + 211, port - 100, 0]
    last_err: OSError | None = None
    seen: set[int] = set()
    for candidate in candidates:
        if candidate < 0 or candidate in seen:
            continue
        seen.add(candidate)
        try:
            server = _ReuseServer(("127.0.0.1", candidate), _Handler)
        except OSError as e:
            last_err = e
            logger.warning("dashboard: port %d unavailable (%s)", candidate, e)
            continue
        actual = server.server_address[1]
        if actual != port:
            logger.warning(
                "dashboard: preferred port %d unavailable, bound to %d instead",
                port, actual,
            )
        return server, actual
    # port 0 above should always succeed; be explicit if it somehow didn't.
    raise last_err or OSError(f"dashboard: could not bind any port near {port}")


class AutostartHandle:
    """What ``start_autostart()`` set in motion.

    ``run_watch`` ignores it on purpose (it must neither wait for nor depend on
    the dashboard); tests and diagnostics use it. ``bound`` is set once the bind
    attempt inside the server thread has finished — successfully (``server``/
    ``url`` set) or not (``error`` set). ``stop`` ends the server and every
    scheduler thread hanging off this handle.
    """

    def __init__(self) -> None:
        self.bound = threading.Event()
        self.stop = threading.Event()
        self.server: _ReuseServer | None = None
        self.url: str | None = None
        self.error: str | None = None
        self.server_thread: threading.Thread | None = None

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the server thread (tests; the orchestrator lets the daemon die)."""
        self.stop.set()
        server = self.server
        if server is not None:
            with contextlib.suppress(Exception):  # best effort, never raises
                server.shutdown()
        thread = self.server_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)


def _safe_report(report, message: str) -> None:
    """Call a reporting callback; a throwing reporter must not take the caller down."""
    with contextlib.suppress(Exception):  # the reporter is the last line
        report(message)


def _autostart_serve(
    handle: AutostartHandle,
    port: int,
    open_browser: bool,
    warn,
    info,
) -> None:
    """Server-thread body: bind, announce, serve. Never raises.

    The bind happens HERE and not in ``start_autostart()``: the caller is
    ``run_watch``, which must not wait for a bind result. Every failure ends in
    exactly one ``warn`` line; a server that bound and then died is closed in
    ``finally`` so no half-open socket stays behind.
    """
    server: _ReuseServer | None = None
    try:
        try:
            server, actual = _bind_server(port)
        except Exception as e:  # OSError and anything else alike
            handle.error = f"{type(e).__name__}: {e}"
            _safe_report(
                warn,
                f"Dashboard-Autostart: kein Port frei ({handle.error}) — "
                "Orchestrator läuft ohne Dashboard weiter",
            )
            return
        url = f"http://127.0.0.1:{actual}"
        handle.server = server
        handle.url = url
        handle.bound.set()
        if actual != port:
            _safe_report(warn, f"Dashboard-Autostart: Port {port} belegt, Dashboard läuft unter {url}")
        else:
            _safe_report(info, f"Dashboard läuft unter {url}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception as e:  # a missing browser is not a dashboard failure
                logger.debug("dashboard autostart: webbrowser.open failed: %s", e)
        # Always reached after a successful bind: AutostartHandle.shutdown() relies
        # on serve_forever() running (socketserver.shutdown waits for it).
        server.serve_forever()
    except Exception as e:  # a dying server thread must log, not vanish
        handle.error = f"{type(e).__name__}: {e}"
        _safe_report(
            warn,
            f"Dashboard-Autostart: Server beendet ({handle.error}) — Orchestrator läuft weiter",
        )
    finally:
        if server is not None:
            with contextlib.suppress(Exception):
                server.server_close()
        handle.bound.set()


def start_autostart(
    *,
    open_browser: bool | None = None,
    port: int | None = None,
    warn=None,
    info=None,
) -> AutostartHandle:
    """Start the dashboard for ``--watch`` as a daemon thread. NEVER raises.

    Returns at once — no join, no waiting for the bind (that happens in the
    thread). Any failure (thread start, bind, ``serve_forever`` dying later) is
    reported through ``warn`` exactly once and recorded in ``handle.error``; the
    orchestrator keeps running either way. ``open_browser`` defaults to
    ``config.DASHBOARD_OPEN_BROWSER`` (False) — this is the unattended path; the
    manual paths (``python dashboard.py``, ``--dashboard``) keep their own default.
    Bound to 127.0.0.1 only (``_bind_server``).
    """
    handle = AutostartHandle()
    warn = warn or logger.warning
    info = info or logger.info
    try:
        if open_browser is None:
            open_browser = bool(config.DASHBOARD_OPEN_BROWSER)
        port = port or config.DASHBOARD_PORT
        thread = threading.Thread(
            target=_autostart_serve,
            args=(handle, port, open_browser, warn, info),
            name="dashboard-autostart",
            daemon=True,
        )
        handle.server_thread = thread
        thread.start()
    except Exception as e:  # never raises, by contract
        handle.error = f"{type(e).__name__}: {e}"
        handle.server_thread = None
        handle.bound.set()
        _safe_report(
            warn,
            f"Dashboard-Autostart fehlgeschlagen ({handle.error}) — Orchestrator läuft ohne Dashboard weiter",
        )
    return handle


def start_server(
    port: int | None = None,
    open_browser: bool = True,
    background: bool = False,
) -> None:
    """Start the dashboard HTTP server.

    Args:
        port: TCP port (default from config.DASHBOARD_PORT).
        open_browser: auto-open in default browser.
        background: if True, run in a daemon thread and return immediately.
    """
    port = port or DASHBOARD_PORT
    server, port = _bind_server(port)
    url = f"http://127.0.0.1:{port}"

    if background:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info("Dashboard running at %s (background)", url)
        if open_browser:
            webbrowser.open(url)
        return

    print(f"Dashboard: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard gestoppt.")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="AI Orchestrator Dashboard")
    parser.add_argument("--port", type=int, default=None,
                        help=f"HTTP port (default: {DASHBOARD_PORT})")
    parser.add_argument("--no-open", action="store_true",
                        help="Don't auto-open browser")
    args = parser.parse_args()
    start_server(port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
