"""
AI Orchestrator — Web Dashboard

Lightweight HTTP server serving an analytics dashboard.
Uses only stdlib (http.server) + Chart.js via CDN.

Standalone usage:
    python dashboard.py              # open browser on port 8411
    python dashboard.py --port 9000  # custom port
    python dashboard.py --no-open    # don't auto-open browser

Programmatic usage:
    from dashboard import start_server
    start_server()                   # blocking
    start_server(background=True)    # returns immediately
"""

import argparse
import json
import logging
import socketserver
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from analytics import get_dashboard_data
from config import DASHBOARD_PORT

logger = logging.getLogger(__name__)

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
  @media (max-width: 700px) {
    .charts { grid-template-columns: 1fr; }
    .timeline-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<header>
  <h1>AI Orchestrator</h1>
  <span class="ts" id="gen-ts">—</span>
</header>

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
      <h3>5h + 24h (Claude 5h, Codex (1))</h3>
      <canvas id="limit-chart-short" height="140"></canvas>
      <button class="zoom-reset" id="lim-short-reset">Zoom zurücksetzen</button>
    </div>
    <div class="timeline-box">
      <h3>Gemini Modelle</h3>
      <canvas id="limit-chart-gemini" height="140"></canvas>
      <button class="zoom-reset" id="lim-gemini-reset">Zoom zurücksetzen</button>
    </div>
    <div class="timeline-box">
      <h3>7d (Claude 7d, Codex (2))</h3>
      <canvas id="limit-chart-long" height="140"></canvas>
      <button class="zoom-reset" id="lim-long-reset">Zoom zurücksetzen</button>
    </div>
  </div>
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
function providerColor(name) {
  if (name === 'claude_seven_day') return '#b0a8ff';
  if (name.startsWith('claude')) return '#6c63ff';
  if (name.includes('secondary')) return '#81c784';
  if (name.startsWith('codex')) return '#4caf50';
  if (name.startsWith('gemini_gemini_1')) return '#ffda6a';
  if (name.startsWith('gemini')) return '#ffc107';
  return '#888';
}
function providerLabel(name) {
  const known = {
    claude: 'Claude', claude_five_hour: 'Claude 5h', claude_seven_day: 'Claude 7d',
    gemini: 'Gemini', codex: 'Codex',
    codex_primary_window: 'Codex (1)', codex_secondary_window: 'Codex (2)',
  };
  if (known[name]) return known[name];
  // gemini model windows: gemini_gemini_2_5_flash_ → "Gemini 2.5 Flash"
  if (name.startsWith('gemini_gemini_')) {
    const parts = name.replace(/^gemini_gemini_/, '').replace(/_+$/, '').split('_');
    const ver = [], words = [];
    for (const p of parts) (/^\d+$/.test(p) ? ver : words).push(p);
    return 'Gemini ' + ver.join('.') + (words.length ? ' ' + words.map(w => w[0].toUpperCase() + w.slice(1)).join(' ') : '');
  }
  return name.replace(/_/g, ' ');
}
// kept for doughnut chart colours
const COLORS = {
  claude: '#6c63ff', gemini: '#ffc107', codex: '#4caf50',
};
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

let tpdChart, pdChart, limitShortChart, limitGeminiChart, limitLongChart;
let _allTpd = { labels: [], values: [] };
let _activeRange = 30;
let _allLimits = {};
let _activeLimitRange = 168;

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
  if (group === 'short') return provider === 'claude_five_hour' || provider === 'codex_primary_window';
  if (group === 'gemini') return provider === 'gemini' || provider.startsWith('gemini_');
  if (group === 'long') return provider === 'claude_seven_day' || provider === 'codex_secondary_window';
  return false;
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
      label: providerLabel(prov),
      data: pts.map(p => ({ x: tsKey(p.ts), y: p.pct })),
      borderColor: providerColor(prov),
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
  updateLimitChart(limitGeminiChart, 'gemini');
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
    data: { labels: [], datasets: [{ data: [], backgroundColor: ['#6c63ff', '#ffc107', '#4caf50', '#ef5350', '#29b6f6'] }] },
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
  limitGeminiChart = createLimitChart('limit-chart-gemini');
  limitLongChart = createLimitChart('limit-chart-long');

  document.getElementById('tpd-reset').onclick = () => safeResetZoom(tpdChart);
  document.getElementById('lim-short-reset').onclick = () => safeResetZoom(limitShortChart);
  document.getElementById('lim-gemini-reset').onclick = () => safeResetZoom(limitGeminiChart);
  document.getElementById('lim-long-reset').onclick = () => safeResetZoom(limitLongChart);
}

function update(d) {
  document.getElementById('gen-ts').textContent = 'Stand: ' + d.generated_at;
  document.getElementById('total-tasks').textContent = d.total_tasks;
  document.getElementById('success-rate').textContent = d.success_rate + '%';
  document.getElementById('avg-dur').textContent = d.avg_duration_sec;
  document.getElementById('providers').textContent = (d.active_providers || []).length;
  document.getElementById('suggest-today').textContent = d.usage_suggest_today ?? '—';

  // Tasks per day — store full 90-day data, then apply active range
  _allTpd = d.tasks_per_day || { labels: [], values: [] };
  applyRange();

  // Provider dist
  pdChart.data.labels = d.provider_distribution.labels || [];
  pdChart.data.datasets[0].data = d.provider_distribution.values || [];
  pdChart.update();

  // Limits timeline — store full history and apply active range to all three charts
  _allLimits = d.limits_timeline || {};
  applyLimitRange();

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
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    """Handles GET / (HTML) and GET /api/data (JSON)."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/data":
            self._json_response(parsed.query)
        elif parsed.path in ("/", "/index.html"):
            self._html_response()
        else:
            self.send_error(404)

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
            days = max(1, min(int(params.get("days", ["7"])[0]), 365))
            data = get_dashboard_data(days=days)
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        except Exception as e:
            logger.exception("dashboard data error")
            body = json.dumps({"error": str(e)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default stderr logging; use logger instead."""
        logger.debug("dashboard: %s", format % args)


class _ReuseServer(socketserver.TCPServer):
    allow_reuse_address = True


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
    server = _ReuseServer(("127.0.0.1", port), _Handler)
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
