// Click delegation: index.html carries data-action="<name>" (no inline handlers,
// CSP forbids them); every tab file registers its actions, app.js installs the
// single click listener that looks them up.
const ACTIONS = {};
function registerActions(map) {
  Object.assign(ACTIONS, map);
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

function _billingCell(label, value, sub) {
  const cell = document.createElement('div');
  cell.className = 'billing-cell';
  cell.innerHTML = '<div class="label">' + escapeHtml(label) + '</div>'
    + '<div class="val">' + escapeHtml(value) + '</div>'
    + (sub ? '<div class="sub">' + escapeHtml(sub) + '</div>' : '');
  return cell;
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

function fmtAge(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return 'gerade eben';
  if (sec < 3600) return 'vor ' + Math.floor(sec / 60) + ' min';
  if (sec < 86400) return 'vor ' + Math.floor(sec / 3600) + ' h';
  return 'vor ' + Math.floor(sec / 86400) + ' Tagen';
}

// Write barrier, frontend half: the server puts its token in <meta name="orch-token">.
// Read at call time (not at load), so a re-rendered tag is picked up.
function orchToken() {
  try {
    const m = document.querySelector('meta[name="orch-token"]');
    return (m && typeof m.content === 'string') ? m.content : '';
  } catch (e) { return ''; }
}

// POST JSON with the token header. Always resolves to {ok, status, data}; never throws.
async function orchPost(path, body) {
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Orch-Token': orchToken()},
      body: JSON.stringify(body ?? {}), cache: 'no-store', redirect: 'error',
    });
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    return {ok: !!r.ok, status: r.status, data};
  } catch (e) {
    return {ok: false, status: 0, data: null};
  }
}

// True when only a page reload can help: server unreachable / restarted (status 0) or a
// token mismatch (403 code 'token'). Any other 403 (forbidden origin etc.) is not this case.
function orchNeedsReload(r) {
  return !!(r && (r.status === 0 || (r.status === 403 && r.data && r.data.code === 'token')));
}
