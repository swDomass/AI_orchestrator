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

function setRange(days) {
  _activeRange = days;
  document.querySelectorAll('.time-btn[data-days]').forEach(b =>
    b.classList.toggle('active', +b.dataset.days === days));
  applyRange();
  load();  // /api/data only carries `days` days (default 7): ask for this range
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
      + '<div class="value" data-color="' + escapeHtml(q.available && !q.stale ? meta.color : '') + '">' + escapeHtml(value) + '</div>'
      + '<div class="label">' + escapeHtml(meta.label) + '</div>'
      + '<div class="age">' + escapeHtml(fmtAge(q.age_sec)) + '</div>'
      + (badges ? '<div>' + badges + '</div>' : '')
      + '</div>';
  }).join('');
  // data-color instead of an inline style (CSP style-src 'self'): set via CSSOM.
  box.querySelectorAll('[data-color]').forEach(el => { el.style.color = el.dataset.color; });
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
    const asked = _activeRange;
    const r = await fetch('/api/data?days=' + asked);
    if (r.ok) {
      const d = await r.json();
      if (asked !== _activeRange) return;  // another range was clicked meanwhile
      _lastData = d;
      update(_lastData);
      // The tab may have been drawn before the first /api/data (direct #harness):
      // its Claude/opencode tiles need limits_now and provider_meta from here.
      if (_harness && _harness.available) renderHarnessQuota(_harness);
    }
  } catch (e) { console.warn('fetch failed', e); }
}

// Chart variable is looked up when the button is clicked (initCharts assigns them later).
function resetZoomFor(buttonId) {
  const charts = {
    'tpd-reset': tpdChart,
    'lim-short-reset': limitShortChart,
    'lim-other-reset': limitOtherChart,
    'lim-long-reset': limitLongChart,
  };
  safeResetZoom(charts[buttonId]);
}

registerActions({
  'set-range': el => setRange(Number(el.dataset.days)),
  'set-limit-range': el => setLimitRange(Number(el.dataset.hours)),
  'reset-zoom': el => resetZoomFor(el.id),
});
