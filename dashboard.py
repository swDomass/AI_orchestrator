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
    border-radius: 10px; padding: 1rem; margin-bottom: 1.5rem;
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
  .session-box {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem;
  }
  .session-box h3 { font-size: 0.9rem; margin-bottom: 0.8rem; color: var(--muted); }
  .session-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.8rem; }
  .session-grid .item { font-size: 0.85rem; }
  .session-grid .item span { color: var(--accent); font-weight: 600; }
  @media (max-width: 700px) { .charts { grid-template-columns: 1fr; } }
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
</div>

<div class="charts">
  <div class="chart-box">
    <h3>Tasks / Tag (30 Tage)</h3>
    <canvas id="tpd-chart" height="180"></canvas>
  </div>
  <div class="chart-box">
    <h3>Provider-Verteilung</h3>
    <canvas id="pd-chart" height="180"></canvas>
  </div>
</div>

<div class="timeline-box">
  <h3>Provider-Kapazität (48 h)</h3>
  <canvas id="limit-chart" height="140"></canvas>
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

let tpdChart, pdChart, limitChart;

function initCharts() {
  const tpdCtx = document.getElementById('tpd-chart').getContext('2d');
  tpdChart = new Chart(tpdCtx, {
    type: 'bar',
    data: { labels: [], datasets: [{ label: 'Tasks', data: [], backgroundColor: '#6c63ff88', borderColor: '#6c63ff', borderWidth: 1 }] },
    options: { ...chartOpts, plugins: { legend: { display: false } } },
  });

  const pdCtx = document.getElementById('pd-chart').getContext('2d');
  pdChart = new Chart(pdCtx, {
    type: 'doughnut',
    data: { labels: [], datasets: [{ data: [], backgroundColor: ['#6c63ff', '#ffc107', '#4caf50', '#ef5350', '#29b6f6'] }] },
    options: { responsive: true, plugins: { legend: { labels: { color: '#888' }, position: 'bottom' } } },
  });

  const limCtx = document.getElementById('limit-chart').getContext('2d');
  limitChart = new Chart(limCtx, {
    type: 'line',
    data: { datasets: [] },
    options: {
      ...chartOpts,
      scales: {
        ...chartOpts.scales,
        x: { ...chartOpts.scales.x, type: 'category' },
        y: { ...chartOpts.scales.y, min: 0, max: 100, title: { display: true, text: '%', color: '#888' } },
      },
    },
  });
}

function update(d) {
  document.getElementById('gen-ts').textContent = 'Stand: ' + d.generated_at;
  document.getElementById('total-tasks').textContent = d.total_tasks;
  document.getElementById('success-rate').textContent = d.success_rate + '%';
  document.getElementById('avg-dur').textContent = d.avg_duration_sec;
  document.getElementById('providers').textContent = (d.active_providers || []).length;

  // Tasks per day
  tpdChart.data.labels = (d.tasks_per_day.labels || []).map(l => l.slice(5));
  tpdChart.data.datasets[0].data = d.tasks_per_day.values || [];
  tpdChart.update();

  // Provider dist
  pdChart.data.labels = d.provider_distribution.labels || [];
  pdChart.data.datasets[0].data = d.provider_distribution.values || [];
  pdChart.update();

  // Limits timeline
  const lt = d.limits_timeline || {};
  const datasets = [];
  for (const [prov, pts] of Object.entries(lt)) {
    datasets.push({
      label: prov,
      data: pts.map(p => p.pct),
      borderColor: COLORS[prov] || '#888',
      backgroundColor: 'transparent',
      tension: 0.3,
      pointRadius: 2,
    });
  }
  // Use longest provider's timestamps as labels
  let maxLabels = [];
  for (const [, pts] of Object.entries(lt)) {
    if (pts.length > maxLabels.length) maxLabels = pts.map(p => p.ts.slice(5, 16));
  }
  limitChart.data.labels = maxLabels;
  limitChart.data.datasets = datasets;
  limitChart.update();

  // Events
  const tbody = document.getElementById('events-body');
  tbody.innerHTML = '';
  for (const ev of (d.recent_events || [])) {
    const tr = document.createElement('tr');
    const cls = ev.type === 'error' ? 'tag-error' : 'tag-queue';
    tr.innerHTML = '<td>' + ev.ts.slice(0, 16).replace('T', ' ') + '</td>'
      + '<td class="' + cls + '">' + ev.type + '</td>'
      + '<td>' + ev.msg.replace(/</g, '&lt;') + '</td>';
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
        if self.path == "/api/data":
            self._json_response()
        elif self.path == "/" or self.path == "/index.html":
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

    def _json_response(self):
        try:
            data = get_dashboard_data()
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        except Exception as e:
            logger.exception("dashboard data error")
            body = json.dumps({"error": str(e)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
