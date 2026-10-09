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
import importlib
import json
import logging
import socket
import socketserver
import sys
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
  .tabs { display: flex; gap: 0.5rem; margin-bottom: 1.2rem; }
  .tab-btn { background: var(--surface); border: 1px solid var(--border); color: var(--muted); border-radius: 6px; padding: 0.4rem 1rem; cursor: pointer; font-size: 0.9rem; }
  .tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .harness-status { color: var(--muted); font-size: 0.85rem; margin-bottom: 1rem; }
  .harness-status.warn { color: var(--yellow); }
  .harness-head { display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem; }
  .harness-head .lbl { color: var(--muted); font-size: 0.8rem; margin-right: 0.4rem; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.total td { font-weight: 600; border-top: 2px solid var(--border); }
  .kpi-incomplete { color: var(--yellow); font-size: 0.75rem; }
  .harness-box h4 { font-size: 0.85rem; color: var(--muted); margin: 1rem 0 0.5rem; }
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

<nav class="tabs">
  <button class="tab-btn active" data-tab="main" onclick="showTab('main')">Übersicht</button>
  <button class="tab-btn" data-tab="harness" onclick="showTab('harness')">Harness</button>
</nav>

<div id="tab-main">
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
</div>

<div id="tab-harness" style="display:none">
  <div class="harness-status" id="h-status">Index wird geladen …</div>
  <div class="harness-head">
    <div class="time-btns"><span class="lbl">Zeitraum</span>
      <button class="time-btn" data-hdays="7" onclick="setHarnessDays(7)">7 Tage</button>
      <button class="time-btn active" data-hdays="30" onclick="setHarnessDays(30)">30 Tage</button>
      <button class="time-btn" data-hdays="90" onclick="setHarnessDays(90)">90 Tage</button>
    </div>
    <div class="time-btns"><span class="lbl">Marker-Fenster</span>
      <button class="time-btn" data-hwin="3" onclick="setHarnessWindow(3)">3 Tage</button>
      <button class="time-btn active" data-hwin="7" onclick="setHarnessWindow(7)">7 Tage</button>
      <button class="time-btn" data-hwin="14" onclick="setHarnessWindow(14)">14 Tage</button>
    </div>
  </div>
  <div id="h-body" style="display:none">
    <section class="billing-box harness-box">
      <h3>(a) Verbrauch Claude je Tag — interaktiv / Orchestrator bzw. claude -p / Subagent</h3>
      <div class="time-btns">
        <button class="time-btn active" data-hmetric="total" onclick="setHarnessMetric('total')">alle Token</button>
        <button class="time-btn" data-hmetric="input" onclick="setHarnessMetric('input')">Input</button>
        <button class="time-btn" data-hmetric="output" onclick="setHarnessMetric('output')">Output</button>
        <button class="time-btn" data-hmetric="cache_read" onclick="setHarnessMetric('cache_read')">Cache lesen</button>
        <button class="time-btn" data-hmetric="cache_write" onclick="setHarnessMetric('cache_write')">Cache schreiben</button>
      </div>
      <canvas id="h-usage-chart" height="150"></canvas>
      <table id="h-usage-table"></table>
      <div class="billing-grid" id="h-sub-tiles" style="margin-top:0.8rem"></div>
      <h4>Modellmix (Anteil an allen Token · Anteil an Output)</h4>
      <table id="h-family-table"></table>
      <h4>Subagent-Aufrufe je Typ und Modell</h4>
      <div class="empty-hint" id="h-agent-control"></div>
      <table id="h-agent-table"></table>
      <h4>Kosten je Tag (USD)</h4>
      <canvas id="h-cost-chart" height="120"></canvas>
      <div class="empty-hint">Claude: höchster <code>cost-state</code>-Wert je Sitzung, dem Starttag der Sitzung zugeordnet (nicht tagesgenau); schließt Subagenten vermutlich ein (an echten Daten ein Indiz, kein Beweis). opencode: Katalogpreis, nicht die Rechnung. Codex: keine Kosten.</div>
    </section>
    <section class="billing-box harness-box">
      <h3>(b) Quoten-Spielraum (frei = 100 − verbraucht)</h3>
      <div class="cards" id="h-quota"></div>
    </section>
    <section class="billing-box harness-box">
      <h3>(c) Externe Stimmen</h3>
      <canvas id="h-extern-chart" height="120"></canvas>
      <div class="empty-hint" id="h-extern-null"></div>
      <table id="h-ledger-status"></table>
      <h4>opencode je Modell (Katalogpreis)</h4>
      <table id="h-oc-models"></table>
      <h4>Codex-Rollouts je Tag</h4>
      <table id="h-codex"></table>
    </section>
    <section class="billing-box harness-box">
      <h3>(d) Harness-Änderungen — vorher / nachher</h3>
      <table id="h-markers"></table>
    </section>
  </div>
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
      '<div class="item">Erledigt: <span>' + escapeHtml(s.tasks_done || 0) + '</span></div>' +
      '<div class="item">Fehler: <span>' + escapeHtml(s.tasks_failed || 0) + '</span></div>' +
      '<div class="item">Gestartet: <span>' + escapeHtml(String(s.started_at).slice(11, 16)) + '</span></div>' +
      '<div class="item">Provider: <span>' + escapeHtml(Object.keys(s.providers_used || {}).join(', ')) + '</span></div>';
  } else {
    box.style.display = 'none';
  }
}

async function load() {
  try {
    const r = await fetch('/api/data');
    if (r.ok) {
      _lastData = await r.json();
      update(_lastData);
      // The tab may have been drawn before the first /api/data (direct #harness):
      // its Claude/opencode tiles need limits_now and provider_meta from here.
      if (_harness && _harness.available) renderHarnessQuota(_harness);
    }
  } catch (e) { console.warn('fetch failed', e); }
}

// ── Harness tab (P4) — data only from GET /api/harness (the index SQLite) ──
let _lastData = null, _harness = null;
let _hDays = 30, _hWindow = 7, _hMetric = 'total';
let hUsageChart = null, hCostChart = null, hExternChart = null;
const H_CATS = ['interaktiv', 'orchestrator', 'subagent', 'unbekannt'];
const H_CAT_LABEL = { interaktiv: 'interaktiv', orchestrator: 'Orchestrator / claude -p',
                      subagent: 'Subagent', unbekannt: 'unbekannt' };
const H_CAT_COLOR = { interaktiv: '#6c63ff', orchestrator: '#4caf50', subagent: '#ffc107', unbekannt: '#888' };
const H_FAMILIES = ['opus', 'sonnet', 'haiku', 'fable', 'andere'];

// Inline Chart.js plugin: one dashed vertical line per harness-change marker,
// labelled with its id; hovering near it shows `change` as the canvas tooltip.
const markerLinesPlugin = {
  id: 'harnessMarkerLines',
  afterDraw(chart) {
    const markers = (_harness && _harness.markers) || [];
    const labels = chart.data.labels || [];
    const area = chart.chartArea, ctx = chart.ctx;
    if (!area || !ctx || !chart.scales || !chart.scales.x) return;
    chart.$markerX = [];
    for (const m of markers) {
      const idx = labels.indexOf(m.date);
      if (idx < 0) continue;
      const x = chart.scales.x.getPixelForValue(idx);
      chart.$markerX.push([x, m]);
      ctx.save();
      ctx.strokeStyle = '#ef5350'; ctx.setLineDash([4, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, area.top); ctx.lineTo(x, area.bottom); ctx.stroke();
      ctx.setLineDash([]); ctx.fillStyle = '#ef5350'; ctx.font = '10px sans-serif';
      ctx.fillText(String(m.id), x + 3, area.top + 10);
      ctx.restore();
    }
  },
  afterEvent(chart, args) {
    const e = args.event;
    if (!e || e.type !== 'mousemove' || !chart.canvas) return;
    const hit = (chart.$markerX || []).find(([x]) => Math.abs(x - e.x) <= 6);
    chart.canvas.title = hit ? (hit[1].id + ' (' + hit[1].date + '): ' + (hit[1].change || '')) : '';
  },
};

function showTab(name) {
  document.getElementById('tab-main').style.display = name === 'main' ? '' : 'none';
  document.getElementById('tab-harness').style.display = name === 'harness' ? '' : 'none';
  document.querySelectorAll('.tab-btn[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  try { location.hash = name === 'harness' ? '#harness' : ''; } catch (e) { /* ignore */ }
  if (name === 'harness') loadHarness();
}

function setHarnessDays(n) {
  _hDays = n;
  document.querySelectorAll('.time-btn[data-hdays]').forEach(b => b.classList.toggle('active', +b.dataset.hdays === n));
  loadHarness();
}
function setHarnessWindow(n) {
  _hWindow = n;
  document.querySelectorAll('.time-btn[data-hwin]').forEach(b => b.classList.toggle('active', +b.dataset.hwin === n));
  loadHarness();
}
function setHarnessMetric(m) {
  _hMetric = m;
  document.querySelectorAll('.time-btn[data-hmetric]').forEach(b => b.classList.toggle('active', b.dataset.hmetric === m));
  if (_harness && _harness.available) renderHarnessUsageChart(_harness);
}

async function loadHarness() {
  try {
    const r = await fetch('/api/harness?days=' + _hDays + '&window=' + _hWindow);
    if (r.ok) renderHarness((await r.json()).harness);
  } catch (e) { console.warn('harness fetch failed', e); }
}

function harnessChart(id, type, stacked) {
  const ctx = document.getElementById(id).getContext('2d');
  return new Chart(ctx, {
    type: type,
    data: { labels: [], datasets: [] },
    options: {
      ...chartOpts,
      plugins: { legend: { labels: { color: '#888' } } },
      scales: { x: { ...chartOpts.scales.x, stacked: stacked },
                y: { ...chartOpts.scales.y, stacked: stacked } },
    },
    plugins: [markerLinesPlugin],
  });
}

function fmtUsd(v) {
  if (v == null || isNaN(v)) return '—';
  return v >= 1 ? v.toFixed(2) : v.toFixed(4);
}
function pct(part, whole) { return whole ? (100 * part / whole).toFixed(0) + ' %' : '—'; }

function renderHarnessUsageChart(h) {
  if (!hUsageChart) hUsageChart = harnessChart('h-usage-chart', 'bar', true);
  const byDay = h.usage.by_day || {};
  hUsageChart.data.labels = h.day_list;
  hUsageChart.data.datasets = H_CATS.map(cat => ({
    label: H_CAT_LABEL[cat],
    data: h.day_list.map(d => (((byDay[d] || {})[cat] || {})[_hMetric]) || 0),
    backgroundColor: H_CAT_COLOR[cat],
  }));
  hUsageChart.update();
}

function renderHarness(h) {
  _harness = h || null;
  const status = document.getElementById('h-status');
  const body = document.getElementById('h-body');
  if (!h || !h.available) {
    status.className = 'harness-status warn';
    status.textContent = 'Harness-Index nicht verfügbar: ' + ((h && h.reason) || 'keine Antwort')
      + ' — der Index läuft mit --watch alle 30 min, von Hand: python -m harness_index --update';
    body.style.display = 'none';
    return;
  }
  body.style.display = '';
  const run = h.last_run || {};
  const errs = Object.keys(run.errors || {});
  status.className = 'harness-status' + ((run.status !== 'ok' || errs.length) ? ' warn' : '');
  status.textContent = 'Letzter Indexlauf ' + fmtAge(run.age_sec) + ' (' + run.finished + ', '
    + (run.duration_sec != null ? run.duration_sec.toFixed(1) : '?') + ' s, ' + formatNumber(run.lines_read)
    + ' Zeilen gelesen, ' + (run.lines_skipped || 0) + ' übersprungen, Status ' + run.status
    + (errs.length ? ', Fehler in: ' + errs.join(', ') : '') + ') — übersprungen über alle '
    + (run.runs_total || 0) + ' Läufe: ' + (run.skipped_total || 0);
  // Ledger and marker file: broken lines as they are NOW (a stock, not summed
  // over runs — both files are rebuilt whenever they change).
  const broken = run.broken_lines || {};
  if (broken.ledger || broken.markers) {
    status.textContent += '; Ledger/Marker: ' + (broken.ledger || 0) + '/' + (broken.markers || 0)
      + ' kaputte Zeilen';
  }

  // (a) usage
  renderHarnessUsageChart(h);
  const t = h.usage.totals || {};
  const cols = ['input', 'output', 'cache_read', 'cache_write', 'total', 'messages'];
  const head = '<thead><tr><th>Quelle</th><th class="num">Input</th><th class="num">Output</th>'
    + '<th class="num">Cache lesen</th><th class="num">Cache schreiben</th><th class="num">Summe</th>'
    + '<th class="num">Antworten</th></tr></thead>';
  const row = (label, v, cls) => '<tr' + (cls ? ' class="' + cls + '"' : '') + '><td>' + escapeHtml(label) + '</td>'
    + cols.map(c => '<td class="num">' + formatNumber((v || {})[c] || 0) + '</td>').join('') + '</tr>';
  const shown = H_CATS.filter(c => c !== 'unbekannt' || ((t.unbekannt || {}).messages || 0) > 0);
  document.getElementById('h-usage-table').innerHTML = head + '<tbody>'
    + shown.map(c => row(H_CAT_LABEL[c], t[c])).join('') + row('Gesamt', t.all, 'total') + '</tbody>';
  const subOrigin = h.usage.subagent_by_origin || {};
  const tiles = document.getElementById('h-sub-tiles');
  tiles.innerHTML = '';
  tiles.appendChild(_billingCell('Subagent-Anteil', h.usage.subagent_share_pct == null ? '—' : h.usage.subagent_share_pct + ' %', 'an allen Token'));
  tiles.appendChild(_billingCell('Subagent-Anteil (In+Out)', h.usage.subagent_share_io_pct == null ? '—' : h.usage.subagent_share_io_pct + ' %', 'ohne Cache'));
  tiles.appendChild(_billingCell('Subagent im Orchestrator', formatNumber(subOrigin.orchestrator || 0), 'Token'));
  tiles.appendChild(_billingCell('Subagent interaktiv', formatNumber(subOrigin.interaktiv || 0), 'Token'));

  const fam = h.usage.families || {};
  document.getElementById('h-family-table').innerHTML = '<thead><tr><th>Quelle</th>'
    + H_FAMILIES.map(f => '<th class="num">' + escapeHtml(f) + '</th>').join('') + '</tr></thead><tbody>'
    + [...shown, 'all'].map(cat => {
        const cells = fam[cat] || {};
        const tok = Object.values(cells).reduce((a, c) => a + (c.tokens || 0), 0);
        const out = Object.values(cells).reduce((a, c) => a + (c.output || 0), 0);
        return '<tr' + (cat === 'all' ? ' class="total"' : '') + '><td>' + escapeHtml(cat === 'all' ? 'Gesamt' : H_CAT_LABEL[cat]) + '</td>'
          + H_FAMILIES.map(f => '<td class="num">' + pct((cells[f] || {}).tokens || 0, tok) + ' · '
          + pct((cells[f] || {}).output || 0, out) + '</td>').join('') + '</tr>';
      }).join('') + '</tbody>';

  const ac = h.agent_calls || {};
  document.getElementById('h-agent-control').textContent = 'Kontrollzahl: ' + (ac.tool_use_count || 0)
    + ' Aufrufe aus tool_use (Agent/Task) gegen ' + (ac.meta_count || 0) + ' .meta.json-Dateien im selben Zeitraum'
    + ' — Abweichung ' + (ac.difference || 0) + ' (z. B. abgelehnte oder noch laufende Aufrufe).';
  document.getElementById('h-agent-table').innerHTML = '<thead><tr><th>subagent_type</th><th>Modell</th>'
    + '<th class="num">Aufrufe</th></tr></thead><tbody>'
    + ((ac.rows || []).length ? ac.rows.map(r => '<tr><td>' + escapeHtml(r.subagent_type) + '</td><td>'
      + escapeHtml(r.model) + '</td><td class="num">' + r.calls + '</td></tr>').join('')
      : '<tr><td colspan="3" class="empty-hint">Keine Subagent-Aufrufe im Zeitraum.</td></tr>') + '</tbody>';

  if (!hCostChart) hCostChart = harnessChart('h-cost-chart', 'bar', false);
  const cost = h.cost || {};
  hCostChart.data.labels = h.day_list;
  hCostChart.data.datasets = [
    { label: 'Claude (cost-state, Starttag, inkl. Subagenten vermutlich)', data: h.day_list.map(d => (cost.claude_by_day || {})[d] || 0), backgroundColor: '#6c63ff' },
    { label: 'opencode (Katalogpreis)', data: h.day_list.map(d => (cost.opencode_by_day || {})[d] || 0), backgroundColor: '#29b6f6' },
  ];
  hCostChart.update();

  renderHarnessQuota(h);
  renderHarnessExtern(h);
  renderHarnessMarkers(h);
}

function codexWindowLabel(w) {
  if (!w || !w.window_minutes) return '?';
  if (w.window_minutes === 300) return '5 h';
  if (w.window_minutes === 10080) return '7 Tage';
  return w.window_minutes + ' min';
}

function renderHarnessQuota(h) {
  const tiles = [];
  const codex = (h.quota || {}).codex;
  for (const key of ['primary', 'secondary']) {
    const w = codex && codex[key];
    if (!w) continue;
    const label = 'Codex ' + codexWindowLabel(w);
    if (w.expired) {
      tiles.push('<div class="card stale"><div class="value">—</div><div class="label">' + escapeHtml(label)
        + '</div><div class="age">Fenster abgelaufen, Wert vom ' + escapeHtml(codex.ts) + '</div></div>');
    } else {
      tiles.push('<div class="card"><div class="value">' + escapeHtml(w.free_pct == null ? '—' : w.free_pct.toFixed(0) + ' %')
        + '</div><div class="label">' + escapeHtml(label) + ' frei</div><div class="age">Stand ' + escapeHtml(codex.ts) + '</div></div>');
    }
  }
  const now = (_lastData && _lastData.limits_now) || {};
  for (const key of ['claude_five_hour', 'claude_seven_day', 'opencode']) {
    const q = now[key];
    if (!q) continue;
    const val = q.available && q.remaining_pct != null ? q.remaining_pct.toFixed(0) + ' %' : 'nicht verfügbar';
    tiles.push('<div class="card' + (q.stale ? ' stale' : '') + '"><div class="value">' + escapeHtml(val)
      + '</div><div class="label">' + escapeHtml(providerMeta(key).label) + '</div><div class="age">'
      + escapeHtml(fmtAge(q.age_sec) + (q.stale ? ' · veraltet' : '')) + '</div></div>');
  }
  document.getElementById('h-quota').innerHTML = tiles.length ? tiles.join('')
    : '<div class="card"><div class="value">—</div><div class="label">Noch keine Quotenwerte</div></div>';
}

function renderHarnessExtern(h) {
  const ex = h.extern || {};
  const voices = new Set();
  for (const v of Object.values(ex.ledger_by_day || {})) Object.keys(v).forEach(k => voices.add(k));
  if (!hExternChart) hExternChart = harnessChart('h-extern-chart', 'bar', true);
  hExternChart.data.labels = h.day_list;
  hExternChart.data.datasets = Array.from(voices).sort().map(v => ({
    label: v, data: h.day_list.map(d => ((ex.ledger_by_day || {})[d] || {})[v] || 0),
    backgroundColor: providerMeta(v).color,
  }));
  hExternChart.update();
  document.getElementById('h-extern-null').textContent = 'Ledger-Zeilen ohne Token (tokens: null): '
    + (ex.ledger_tokens_null || 0) + ' von ' + (ex.ledger_total || 0)
    + ' — opencode meldet keine Token ins Ledger, das ist kein Fehler.';
  const status = ex.ledger_status || {};
  const sts = new Set();
  Object.values(status).forEach(m => Object.keys(m).forEach(k => sts.add(k)));
  const stList = Array.from(sts).sort();
  document.getElementById('h-ledger-status').innerHTML = '<thead><tr><th>Stimme</th>'
    + stList.map(s => '<th class="num">' + escapeHtml(s) + '</th>').join('') + '</tr></thead><tbody>'
    + Object.keys(status).sort().map(v => '<tr><td>' + escapeHtml(v) + '</td>'
      + stList.map(s => '<td class="num">' + (status[v][s] || 0) + '</td>').join('') + '</tr>').join('') + '</tbody>';
  document.getElementById('h-oc-models').innerHTML = '<thead><tr><th>Modell</th><th class="num">Sitzungen</th>'
    + '<th class="num">Antworten</th><th class="num">Kosten (Katalogpreis)</th></tr></thead><tbody>'
    + (ex.opencode_by_model || []).map(r => '<tr><td>' + escapeHtml(r.model) + '</td><td class="num">' + r.sessions
      + '</td><td class="num">' + r.messages + '</td><td class="num">' + fmtUsd(r.cost) + '</td></tr>').join('') + '</tbody>';
  const cdx = ex.codex_by_day || {};
  const cdxDays = Object.keys(cdx).sort().reverse();
  document.getElementById('h-codex').innerHTML = '<thead><tr><th>Tag</th><th>thread_source</th>'
    + '<th class="num">Rollouts</th><th class="num">Token</th></tr></thead><tbody>'
    + cdxDays.flatMap(d => Object.entries(cdx[d]).map(([src, v]) => '<tr><td>' + escapeHtml(d) + '</td><td>'
      + escapeHtml(src) + '</td><td class="num">' + v.rollouts + '</td><td class="num">' + formatNumber(v.tokens)
      + '</td></tr>')).join('') + '</tbody>';
}

function renderHarnessMarkers(h) {
  const markers = h.markers || [];
  if (!markers.length) {
    document.getElementById('h-markers').innerHTML = '<tbody><tr><td class="empty-hint">Keine Marker '
      + '(~/.claude/harness-changes.jsonl).</td></tr></tbody>';
    return;
  }
  const fmt = (key, v) => v == null ? '—' : (key === 'opencode_cost' ? fmtUsd(v) : (+v).toFixed(1));
  let html = '<thead><tr><th>Datum</th><th>Marker</th><th>Kennzahl</th><th class="num">vorher Ø/Tag</th>'
    + '<th class="num">nachher Ø/Tag</th><th class="num">Δ</th></tr></thead><tbody>';
  // * = Mittel über weniger als N Tage (Fenster nicht voll abgedeckt)
  for (const m of markers) {
    const head = '<td>' + escapeHtml(m.date) + '</td><td title="' + escapeHtml(m.change || '') + '"><b>'
      + escapeHtml(m.id) + '</b> <span class="empty-hint">' + escapeHtml(m.scope || '') + '</span><br>'
      + '<span class="empty-hint">' + escapeHtml(m.expect || '') + '</span></td>';
    if (!m.kpis || !m.kpis.length) {
      html += '<tr>' + head + '<td colspan="4" class="empty-hint">nur Linie — kein Schlagwort in „expect“</td></tr>';
      continue;
    }
    m.kpis.forEach((k, i) => {
      // An incompletely covered window shows its note, never a "0" and never a
      // percentage (harness_index.marker_window: delta only when both are full).
      const notes = (k.notes || []).map(t => '<span class="kpi-incomplete">' + escapeHtml(t) + '</span>');
      const cell = (avg, complete) => avg == null ? '—' : fmt(k.kpi, avg) + (complete ? '' : '*');
      html += '<tr>' + (i === 0 ? head : '<td></td><td></td>') + '<td>' + escapeHtml(k.label)
        + (notes.length ? '<br>' + notes.join('<br>') : '') + '</td>'
        + '<td class="num">' + cell(k.before_avg, k.before_complete) + '</td>'
        + '<td class="num">' + cell(k.after_avg, k.after_complete) + '</td>'
        + '<td class="num">' + (k.delta_pct == null ? '—' : (k.delta_pct > 0 ? '+' : '') + k.delta_pct + ' %') + '</td></tr>';
    });
  }
  document.getElementById('h-markers').innerHTML = html + '</tbody>';
}

initCharts();
load();
setInterval(load, 60000);
setInterval(refreshActiveRuns, 30000);
if (location.hash === '#harness') showTab('harness');
</script>
</body>
</html>
"""


_CLIENT_DISCONNECT_ERRORS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _normalize_host(value: str) -> str:
    """``Host``-style value → bare lower-case name: port and IPv6 brackets off."""
    host = value.strip().lower()
    if host.startswith("["):          # [::1]:8211
        return host[1:host.find("]")] if "]" in host else host[1:]
    if host.count(":") == 1:          # name:port
        return host.rsplit(":", 1)[0]
    return host


def _allowed_hosts() -> frozenset[str]:
    extra = {_normalize_host(h) for h in getattr(config, "DASHBOARD_ALLOWED_HOSTS", ()) if str(h).strip()}
    return _LOOPBACK_HOSTS | extra


def _host_allowed(host_header: str | None) -> bool:
    """Anti DNS-rebinding: a browser always sends the name it resolved, so a page
    on evil.example that rebinds to 127.0.0.1 arrives with Host: evil.example.
    No Host header at all (a non-browser client) is let through. Configured extra
    names are normalized exactly like the header (port, brackets, case)."""
    if not host_header:
        return True
    return _normalize_host(host_header) in _allowed_hosts()


def _cross_site_refused(headers, path: str) -> bool:
    """Blind cross-site requests: any web page could fire ``no-cors`` fetches
    (with varying ``days``) and keep the log parser in the orchestrator process
    busy, without ever reading the answer. Refused: ``Sec-Fetch-Site:
    cross-site|same-site``, or an ``Origin`` that is not an allowed host. One
    exception, deliberately: a top-level NAVIGATION to the page itself (a link
    to the dashboard clicked on another site) only gets the static HTML. Without
    these headers (curl, old clients) nothing changes."""
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        mode = (headers.get("Sec-Fetch-Mode") or "").strip().lower()
        return not (mode == "navigate" and path in ("/", "/index.html"))
    origin = headers.get("Origin")
    if origin is None:
        return False
    hostname = urllib.parse.urlsplit(origin.strip()).hostname
    return hostname is None or hostname.lower() not in _allowed_hosts()


class _Handler(BaseHTTPRequestHandler):
    """Handles GET / (HTML), GET /api/data and GET /api/harness (JSON)."""

    # A silent open connection must not hold the single-threaded server forever.
    timeout = 10

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if not _host_allowed(self.headers.get("Host")):
                self.send_error(403, "Host not allowed")
                return
            if _cross_site_refused(self.headers, parsed.path):
                self.send_error(403, "Cross-site request refused")
                return
            if parsed.path == "/api/data":
                self._json_response(parsed.query)
            elif parsed.path == "/api/harness":
                self._harness_response(parsed.query)
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

    def _harness_response(self, query_string: str = "") -> None:
        """GET /api/harness?days=…&window=… — reads ONLY the harness index.

        Deliberately not part of get_dashboard_data(): its log parsers are slow
        and this server is single-threaded. harness_index.dashboard_payload()
        opens the SQLite read-only with a short busy timeout and a query budget,
        and answers {"harness": {"available": false, ...}} instead of raising.
        """
        try:
            params = urllib.parse.parse_qs(query_string)
            try:
                days = int(params.get("days", ["30"])[0])
            except ValueError:
                days = 30
            try:
                window = int(params.get("window", ["7"])[0])
            except ValueError:
                window = 7
            harness_index = importlib.import_module("harness_index")
            data = harness_index.dashboard_payload(config.HARNESS_DB_FILE, days=days, window=window)
        except Exception as e:
            logger.debug("harness payload failed", exc_info=True)
            data = {"harness": {"available": False, "reason": f"{type(e).__name__}: {e}"}}
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
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


# Windows: SO_EXCLUSIVEADDRUSE (winsock2.h, value ~SO_REUSEADDR == -5). Older
# Python builds do not export the name, hence the literal fallback.
_SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)


def _is_windows() -> bool:
    """Seam for the socket-option test: it runs the Windows branch on Linux too."""
    return sys.platform == "win32"


class _ReuseServer(socketserver.TCPServer):
    """TCP server that never shares its port with a second server.

    POSIX: SO_REUSEADDR only lets a restarted server rebind past TIME_WAIT; a
    port another socket is LISTENING on still fails, so the second dashboard
    falls back as documented. Windows: SO_REUSEADDR lets a second socket bind a
    port that is in use (measured twice by the Auftraggeber: two dashboards on
    8211, no fallback, no warning) — so there it is never set and
    SO_EXCLUSIVEADDRUSE is set instead. Both options are chosen in
    ``server_bind`` at bind time rather than in the class attribute, so a test
    can run the Windows branch on any platform.
    """

    allow_reuse_address = False  # TCPServer must not add SO_REUSEADDR itself

    def server_bind(self) -> None:
        if _is_windows():
            self.socket.setsockopt(socket.SOL_SOCKET, _SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


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
        self.index_thread: threading.Thread | None = None
        self.index_runs = 0

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the server thread (tests; the orchestrator lets the daemon die)."""
        self.stop.set()
        server = self.server
        if server is not None:
            with contextlib.suppress(Exception):  # best effort, never raises
                server.shutdown()
        for thread in (self.server_thread, self.index_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout)


def _safe_report(report, message: str) -> None:
    """Call a reporting callback; a throwing reporter must not take the caller down."""
    with contextlib.suppress(Exception):  # the reporter is the last line
        report(message)


def _report_startup_warnings(warn) -> None:
    """``config.STARTUP_WARNINGS`` (collected at import, before logging was set
    up), each exactly once per process: a reported entry leaves the list."""
    pending = config.STARTUP_WARNINGS
    while pending:
        _safe_report(warn, pending.pop(0))


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


# First harness-index run this long after the autostart (the orchestrator's own
# startup goes first); then every config.HARNESS_UPDATE_INTERVAL_SEC.
HARNESS_FIRST_RUN_DELAY_SEC = 60.0


def _harness_index_loop(handle: AutostartHandle, interval: float, warn, info) -> None:
    """Scheduler-thread body: launch the index as its OWN child process. Never raises.

    The child (``harness_index.run_update_subprocess``) runs at low priority
    and is killed by handle after ``HARNESS_LOCK_STALE_SEC``; this thread only
    waits for it. A failure is reported once per distinct message (no warning
    every 30 minutes for the same broken state), a recovery once.
    """
    delay = min(HARNESS_FIRST_RUN_DELAY_SEC, interval)
    last_error: str | None = None
    while not handle.stop.wait(delay):
        delay = interval
        error: str | None
        try:
            harness_index = importlib.import_module("harness_index")
            result = harness_index.run_update_subprocess(timeout=float(config.HARNESS_LOCK_STALE_SEC))
            error = result.get("error")
            handle.index_runs += 1
            if not error:
                lines = (result.get("stdout") or "").splitlines()
                logger.info("harness index: %s", lines[-1] if lines else "ok")
            if result.get("stderr"):
                # Full error texts of the child: the index stores type names only,
                # the details land here, in the local orchestrator log.
                logger.info("harness index stderr: %s", result["stderr"])
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        if error and error != last_error:
            _safe_report(warn, f"Harness-Index: {error} — Orchestrator läuft weiter")
        elif not error and last_error:
            _safe_report(info, "Harness-Index läuft wieder")
        last_error = error


def _start_harness_index_thread(handle: AutostartHandle, warn, info) -> str | None:
    """Start the index scheduler (independent of the dashboard's bind result).

    Returns the error text if the thread could not be started — the caller
    reports it, together with a server failure in ONE line."""
    interval = float(config.HARNESS_UPDATE_INTERVAL_SEC)
    if interval <= 0:
        return None
    try:
        thread = threading.Thread(
            target=_harness_index_loop, args=(handle, interval, warn, info),
            name="harness-index-scheduler", daemon=True,
        )
        handle.index_thread = thread
        thread.start()
    except Exception as e:  # never raises, by contract
        handle.index_thread = None
        return f"{type(e).__name__}: {e}"
    return None


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
    orchestrator keeps running either way. Also starts the harness-index
    scheduler thread (``HARNESS_UPDATE_INTERVAL_SEC``, 0 = off), whose work runs
    in a separate low-priority process. ``open_browser`` defaults to
    ``config.DASHBOARD_OPEN_BROWSER`` (False) — this is the unattended path; the
    manual paths (``python dashboard.py``, ``--dashboard``) keep their own default.
    Bound to 127.0.0.1 only (``_bind_server``).
    """
    handle = AutostartHandle()
    warn = warn or logger.warning
    info = info or logger.info
    with contextlib.suppress(Exception):  # never raises, by contract
        _report_startup_warnings(warn)
    # Local, not handle.error: the server thread sets handle.error itself on a
    # bind failure — concurrently — and reports that one on its own.
    start_error: str | None = None
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
        start_error = handle.error = f"{type(e).__name__}: {e}"
        handle.server_thread = None
        handle.bound.set()
    # The index is useful even when this dashboard did not come up (a manual
    # `python dashboard.py` reads the same SQLite), so it is tried regardless —
    # and a failure of both is ONE warning line, not two.
    index_error = _start_harness_index_thread(handle, warn, info)
    if start_error and index_error:
        _safe_report(warn, f"Dashboard-Autostart fehlgeschlagen ({start_error}); Harness-Index-Thread "
                           f"ebenfalls nicht gestartet ({index_error}) — Orchestrator läuft ohne beide weiter")
    elif start_error:
        _safe_report(warn, f"Dashboard-Autostart fehlgeschlagen ({start_error}) — "
                           "Orchestrator läuft ohne Dashboard weiter")
    elif index_error:
        _safe_report(warn, f"Harness-Index-Thread nicht gestartet ({index_error}) — Orchestrator läuft weiter")
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
    with contextlib.suppress(Exception):
        _report_startup_warnings(lambda message: print(message, file=sys.stderr))
    start_server(port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
