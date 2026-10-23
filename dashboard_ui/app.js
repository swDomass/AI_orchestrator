// Elements that start hidden carry .boot-hidden (CSS, no inline style). showTab() and
// harnessShown() work with the inline display value, so hand over to it first.
document.querySelectorAll('.boot-hidden').forEach(el => {
  el.style.display = 'none';
  el.classList.remove('boot-hidden');
});

const TABS = ['main', 'harness', 'queue'];
function showTab(name) {
  TABS.forEach(t => { document.getElementById('tab-' + t).style.display = t === name ? '' : 'none'; });
  document.querySelectorAll('.tab-btn[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  try { location.hash = name === 'main' ? '' : '#' + name; } catch (e) { /* ignore */ }
  if (name === 'harness') loadHarness();
  if (name === 'queue') loadQueue();
}

registerActions({
  'show-tab': el => showTab(el.dataset.tab),
});
// One delegated listener for every [data-action] button (see ACTIONS in core.js).
document.addEventListener('click', e => {
  const el = e.target && e.target.closest ? e.target.closest('[data-action]') : null;
  if (!el) return;
  const name = el.dataset.action;
  if (Object.prototype.hasOwnProperty.call(ACTIONS, name)) ACTIONS[name](el);
});

initCharts();
load();
// One silent write-barrier handshake; the page looks the same either way.
let _orchWriteOk = false;
orchPost('/api/ping', {}).then(r => {
  _orchWriteOk = !!(r.ok && r.data && r.data.ok === true);
  if (!_orchWriteOk) console.warn('dashboard: write barrier ping failed');
});
setInterval(load, 60000);
setInterval(refreshActiveRuns, 30000);
setInterval(harnessTick, 300000);
setInterval(queueTick, 5000);
// One delegated handler for typing and changes (queue search + form); no inline or .oninput handlers.
document.addEventListener('input', queueOnInput);
document.addEventListener('change', queueOnInput);
if (location.hash === '#harness') showTab('harness');
if (location.hash === '#queue') showTab('queue');
