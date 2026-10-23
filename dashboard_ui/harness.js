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

let _hLoading = false, _hAgain = false;
let _hTimeoutMs = 15000;  // a request that never answers must not lock the tab for good
function harnessShown() {
  if (document.hidden) return false;
  return document.getElementById('tab-harness').style.display !== 'none';
}
async function loadHarness(fromTimer) {
  if (_hLoading) {  // a slow answer must not be overtaken by a second request
    if (!fromTimer) _hAgain = true;  // a click on another range is asked once more afterwards
    return;
  }
  _hLoading = true;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), _hTimeoutMs);
  try {
    const r = await fetch('/api/harness?days=' + _hDays + '&window=' + _hWindow, { signal: ctl.signal });
    if (r.ok) renderHarness((await r.json()).harness);
  } catch (e) {
    console.warn('harness fetch failed', e);
    if (ctl.signal.aborted) renderHarness({ available: false, reason: 'keine Antwort innerhalb von ' + Math.round(_hTimeoutMs / 1000) + ' s' });
  } finally { clearTimeout(timer); _hLoading = false; }
  if (_hAgain) { _hAgain = false; if (harnessShown()) loadHarness(); }
}
// Every 5 min, only while the tab is shown and the window is not hidden: no request otherwise.
function harnessTick() {
  if (!harnessShown()) return;
  loadHarness(true);
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

registerActions({
  'set-harness-days': el => setHarnessDays(Number(el.dataset.hdays)),
  'set-harness-window': el => setHarnessWindow(Number(el.dataset.hwin)),
  'set-harness-metric': el => setHarnessMetric(el.dataset.hmetric),
});
