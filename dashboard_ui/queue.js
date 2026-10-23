// ── Queue tab (B1) — view + row actions. The server decides everything: which buttons exist
// (entry.actions), how a line is built, what is allowed (409/423/428). This file only renders
// and sends. Every piece of text from the queue file goes through escapeHtml / textContent.
let queueState = null, queueJson = '', queueInstance = null;
let queueGen = 0, queueOwnSha = null, queueMarks = new Set();
let queueFileNote = false;
let queueFails = 0, queueSoftReload = false, queueWritten = null, queueLastR = null;
let queueLoading = false, queueAgain = false, queueAbort = null;
let queueTimeoutMs = 15000, queueSlowMs = 30000, queueBusy = false, queueLateFrom = -1;
const queueExpanded = new Set(), queueOpenGroups = new Set();
const QUEUE_ORDER = ['laeuft', 'offen', 'wartet', 'geplant', 'fehlgeschlagen', 'erledigt', 'verworfen', 'weitere'];
const QUEUE_LABEL = { laeuft: 'Läuft', offen: 'Offen', wartet: 'Wartet', geplant: 'Geplant',
                      fehlgeschlagen: 'Fehlgeschlagen', erledigt: 'Erledigt', verworfen: 'Verworfen',
                      weitere: 'Weitere Zeilen' };
const QUEUE_FOLDED = ['erledigt', 'verworfen', 'weitere'];
const QUEUE_BTN = { edit: [['queue-edit', 'Bearbeiten']], delete: [['queue-delete', 'Löschen']],
                    retry: [['queue-retry', 'Erneut']], duplicate: [['queue-duplicate', 'Duplizieren']],
                    move: [['queue-move-up', '↑'], ['queue-move-down', '↓']] };
const QUEUE_MOVE_TITLE = { 'queue-move-up': 'Nach oben', 'queue-move-down': 'Nach unten' };
const QUEUE_MOVE_WHY = ': der Orchestrator arbeitet fällige Tasks in Dateireihenfolge ab — ändert die Reihenfolge ab dem nächsten Durchlauf';
const QUEUE_CLAMP_LEN = 160;

function queueEl(id) { return document.getElementById(id); }
function queueShown() {
  if (document.hidden) return false;
  return queueEl('tab-queue').style.display !== 'none';
}
function queueCanWrite() { return orchToken() !== ''; }
function queueShow(id, on) { queueEl(id).hidden = !on; }
function queueNote(text) {
  queueEl('q-msg').textContent = text || '';
  queueShow('q-msg', !!text);
}
// soft: a transient GET failure; it hides again with the next good answer, a sticky one never does.
function queueNeedReload(msg, soft) {
  if (soft && !queueEl('q-reload').hidden) return;
  queueSoftReload = !!soft;
  queueEl('q-reload-msg').textContent = msg || 'Verbindung verloren oder Sitzung ungültig — Seite neu laden.';
  queueShow('q-reload', true);
}

// Pure: group entries by state (fixed order, empty groups left out), filter by text.
function queueGroupsOf(entries, search) {
  const q = String(search || '').trim().toLowerCase();
  const by = {};
  for (const e of entries || []) {
    if (q && String(e.text).toLowerCase().indexOf(q) < 0) continue;
    const st = QUEUE_ORDER.includes(e.state) ? e.state : 'weitere';
    (by[st] = by[st] || []).push(e);
  }
  return QUEUE_ORDER.filter(st => by[st]).map(st => ({
    state: st, label: QUEUE_LABEL[st], folded: QUEUE_FOLDED.includes(st), entries: by[st] }));
}

function queueKey(e) { return String(e.text) + '\u0001' + (e.children || []).join('\n'); }
// Blocks of `next` that `prev` did not have (multiset by text + child lines): positions.
function queueNewPositions(prev, next) {
  const left = new Map();
  for (const e of prev) left.set(queueKey(e), (left.get(queueKey(e)) || 0) + 1);
  const out = new Set();
  next.forEach((e, i) => {
    const k = queueKey(e), n = left.get(k) || 0;
    if (n > 0) left.set(k, n - 1); else out.add(i);
  });
  return out;
}

async function loadQueue(fromTimer) {
  if (queueLoading) {  // a slow answer must not be overtaken by a second request
    if (!fromTimer) queueAgain = true;
    return;
  }
  queueLoading = true;
  queueAbort = new AbortController();
  const timer = setTimeout(() => queueAbort.abort(), queueTimeoutMs);
  try {
    const resp = await fetch('/api/queue', { signal: queueAbort.signal, cache: 'no-store' });
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    queueFails = 0;
    if (resp.ok && data && data.entries) {
      if (queueSoftReload) { queueShow('q-reload', false); queueSoftReload = false; }
      queueApply(data, false);
    } else if (orchNeedsReload({ status: resp.status, data })) queueNeedReload();
    else queueNote((data && data.error) || ('Queue nicht lesbar (' + resp.status + ')'));
  } catch (e) {
    console.warn('queue fetch failed', e);
    queueFails += 1;
    if (queueFails >= 3) queueNeedReload(null, true);   // one or two misses are noise
  } finally { clearTimeout(timer); queueLoading = false; }
  if (queueAgain) { queueAgain = false; if (queueShown()) loadQueue(); }
}
// Every 5 s, only while the tab is shown and the window is not hidden.
function queueTick() {
  if (!queueShown()) return;
  loadQueue(true);
}

// own=true: payload came back from our own mutation (no "changed from outside" marks).
function queueApply(p, own) {
  const prev = queueState;
  if (prev && queueInstance && p.instance && p.instance !== queueInstance) {
    queueNeedReload('Dashboard neu gestartet — Seite neu laden.');
  }
  if (p.instance) queueInstance = p.instance;
  const sha = p.file && p.file.sha;
  if (own) {
    queueOwnSha = sha;
    queueMarks = new Set();
    queueWritten = null;
    queueShow('q-external', false);
    queueShow('q-overwritten', false);
  } else if (prev && sha !== prev.file.sha) {
    if (sha !== queueOwnSha) {
      queueMarks = queueNewPositions(prev.entries, p.entries);
      queueShow('q-external', true);
      if (queueWritten !== null && !p.entries.some(e => e.text === queueWritten)) {
        queueShow('q-overwritten', true);   // our last add/edit is gone: probably overwritten by Obsidian
        queueWritten = null;
      }
    } else queueMarks = new Set();
  }
  const json = JSON.stringify(p);
  const same = json === queueJson;
  queueJson = json;
  if (!prev || (prev.file && prev.file.sha !== sha)) queueGen += 1;
  queueState = p;
  // The file's own note (not UTF-8 ...) must not wipe a message of ours (409 etc.) on the next poll.
  if (p.note) { queueNote(p.note); queueFileNote = true; } else if (queueFileNote) { queueNote(''); queueFileNote = false; }
  if (!same) queueRenderStatus();
  if (!same || own) renderQueue();
}

function queueFmtTime(t) {
  const d = typeof t === 'number' ? new Date(t * 1000) : new Date(String(t));
  return isNaN(d.getTime()) ? String(t) : d.toLocaleString('de-AT');
}
function queueRenderStatus() {
  const p = queueState;
  if (!p) return;
  queueEl('q-path').textContent = p.file.path + (p.file.exists ? '' : ' (fehlt)');
  queueEl('q-mtime').textContent = p.file.mtime ? 'geändert ' + queueFmtTime(p.file.mtime) : '';
  const r = p.running || {};
  let run = 'Laufstatus unbekannt (eigenständiger Betrieb)';
  if (r.mode === 'inprocess') {
    run = r.task ? 'läuft: ' + String(r.task.text).slice(0, 80) : 'läuft gerade nichts';
  }
  queueEl('q-run').textContent = run;
  const can = queueCanWrite();
  queueEl('q-hint').textContent = can ? '' : 'Bearbeiten nur über 127.0.0.1:' + (location.port || '<port>');
  ['q-new-btn', 'q-raw-btn'].forEach(id => queueShow(id, can));
  const u = p.undo || {};
  const undo = queueEl('q-undo');
  undo.textContent = u.available ? 'Rückgängig: ' + u.action + (u.at ? ' (' + queueFmtTime(u.at) + ')' : '') : '';
  queueShow('q-undo', !!(can && u.available));
}

function queueBadge(cls, text) {
  return '<span class="q-badge ' + cls + '">' + escapeHtml(text) + '</span>';
}
function queueBadges(e) {
  const out = [queueBadge('q-s-' + e.state, QUEUE_LABEL[e.state] || e.state)];
  const s = e.schedule || {};
  if (s.every_sec) out.push(queueBadge('q-info', 'alle ' + fmtDuration(s.every_sec)));
  if (s.at) out.push(queueBadge('q-info', 'um ' + s.at));
  if (s.due_at) out.push(queueBadge('q-info', 'fällig ' + s.due_at));
  if (e.waiting_for && e.waiting_for.length) out.push(queueBadge('q-s-wartet', 'wartet auf ' + e.waiting_for.join(', ')));
  const m = e.markers || {};
  if (m.retry) out.push(queueBadge('q-s-fehlgeschlagen', 'Retry ' + m.retry));
  if (m.hang) out.push(queueBadge('q-s-fehlgeschlagen', 'Hänger ×' + m.hang));
  if (e.comment_in_body) out.push(queueBadge('q-info', 'HTML-Kommentar (nur Rohtext)'));
  if (e.stamp) out.push(queueBadge('q-info', e.stamp));
  if (queueLateFrom >= 0 && e.state === 'offen' && e.pos > queueLateFrom) {
    out.push(queueBadge('q-info', 'vermutlich in dieser Runde eingeplant — Bearbeiten wirkt evtl. erst nach diesem Lauf'));
  }
  return out.join(' ');
}
function queueButtons(e, pos) {
  if (!queueCanWrite()) return '';
  const html = [];
  for (const act of e.actions || []) {
    for (const [name, label] of QUEUE_BTN[act] || []) {
      const tip = QUEUE_MOVE_TITLE[name] ? ' title="' + escapeHtml(QUEUE_MOVE_TITLE[name] + QUEUE_MOVE_WHY) + '"' : '';
      html.push('<button class="time-btn" data-action="' + name + '" data-gen="' + queueGen
        + '" data-idx="' + pos + '"' + tip + (queueBusy ? ' disabled' : '') + '>' + escapeHtml(label) + '</button>');
    }
  }
  return html.join(' ');
}
function queueEntryHtml(e) {
  const pos = e.pos;
  const long = String(e.text).length > QUEUE_CLAMP_LEN;
  const open = queueExpanded.has(e.ref.start);
  let h = '<div class="q-entry q-' + escapeHtml(e.state) + (queueMarks.has(pos) ? ' q-changed' : '')
    + '"><div class="q-head">' + queueBadges(e) + ' <span class="q-ln">Z. ' + e.line + '</span></div>'
    + '<div class="q-line' + (long && !open ? ' clamped' : '') + '">' + escapeHtml(e.text) + '</div>';
  if (long) {
    h += '<button class="time-btn" data-action="queue-expand" data-gen="' + queueGen + '" data-idx="' + pos + '">'
      + (open ? 'weniger' : 'mehr') + '</button> ';
  }
  for (const c of e.children || []) h += '<div class="q-line q-child">' + escapeHtml(c) + '</div>';
  return h + '<div class="q-btns">' + queueButtons(e, pos) + '</div></div>';
}
function renderQueue() {
  const box = queueEl('q-groups');
  box.querySelectorAll('details[data-group]').forEach(d => {
    if (d.open) queueOpenGroups.add(d.dataset.group); else queueOpenGroups.delete(d.dataset.group);
  });
  if (!queueState) { box.innerHTML = ''; return; }
  const run = queueState.running || {};
  const cur = run.task ? queueState.entries.find(e => e.state === 'laeuft') : null;
  queueLateFrom = cur ? cur.pos : -1;   // due entries below the running one: probably in this round's plan
  const groups = queueGroupsOf(queueState.entries, queueEl('q-search').value);
  if (!groups.length) { box.innerHTML = '<div class="empty-hint">Keine Einträge.</div>'; return; }
  box.innerHTML = groups.map(g => {
    const body = g.entries.map(queueEntryHtml).join('');
    const head = escapeHtml(g.label) + ' (' + g.entries.length + ')';
    if (!g.folded) return '<section class="q-group"><h3>' + head + '</h3>' + body + '</section>';
    return '<details class="q-group" data-group="' + g.state + '"' + (queueOpenGroups.has(g.state) ? ' open' : '')
      + '><summary>' + head + '</summary>' + body + '</details>';
  }).join('');
}

// Entry behind a button; a stale generation reloads instead of acting on old positions.
function queueEntryOf(el) {
  if (!queueState || el.dataset.gen !== String(queueGen)) { loadQueue(); return null; }
  return queueState.entries[Number(el.dataset.idx)] || null;
}
const QUEUE_CONFIRM = {
  confirm_unknown_running: 'Laufstatus unbekannt: Der Orchestrator läuft eigenständig, ob diese Zeile gerade bearbeitet wird, ist nicht prüfbar. Trotzdem ändern?',
  confirm_dangerous: 'Die Zeile enthält gefährliche Tags. Trotzdem fortfahren?',
};
// One POST to /api/queue/<op>. 428 asks once per flag; the answer is the new payload.
async function queueRun(op, body) {
  if (queueBusy) return false;
  queueBusy = true;
  renderQueue();
  try { return await queueRunInner(op, body); } finally { queueBusy = false; renderQueue(); }
}
// After 30 s of one request (not of confirm() dialogs): say so and reload; the request is not aborted.
async function queuePostTimed(op, b) {
  const slow = setTimeout(() => { queueNote('Die Änderung dauert ungewöhnlich lange — Stand wird neu geladen'); loadQueue(); }, queueSlowMs);
  try { return await orchPost('/api/queue/' + op, b); } finally { clearTimeout(slow); }
}
async function queueRunInner(op, body) {
  let b = body;
  for (let i = 0; i < 3; i++) {
    const r = await queuePostTimed(op, b);
    queueLastR = r;
    const code = r.data && r.data.code;
    if (r.ok && r.data && r.data.queue) { queueApply(r.data.queue, true); queueNote(''); return true; }
    if (orchNeedsReload(r)) { queueNeedReload(); return false; }
    if (r.status === 428 && QUEUE_CONFIRM[code]) {
      if (!confirm(r.data.error || QUEUE_CONFIRM[code])) return false;
      b = Object.assign({}, b, { [code]: true });
      continue;
    }
    queueNote((r.data && r.data.error) || ('Fehler (' + r.status + ')'));
    if (r.status === 409) loadQueue();
    return false;
  }
  return false;
}
async function queueEntryAction(el, op, extra, ask) {
  const e = queueEntryOf(el);
  if (!e) return false;
  if (ask && !confirm(ask + '\n\n' + String(e.text).slice(0, 160))) return false;
  return queueRun(op, Object.assign({ ref: e.ref }, extra || {}));
}
// ── Panels in #q-form / #q-raw (new task, edit, raw text). Markup only from escapeHtml'd values.
let queueMeta = null, queueMetaInstance = null, queueMode = null, queueLint = null;
let queuePrevTimer = 0, queuePrevSeq = 0, queueEditRef = null, queueRawSha = null, queuePanelGen = 0;
// queuePanelGen rises on every open and close of a panel: a late save answer closes only the panel it was started in.
const QUEUE_FIELDS = ['text', 'tool', 'model', 'profile', 'effort', 'cwd', 'id', 'needs', 'at', 'every',
                      'grace', 'timeout', 'verify'];

function queueOpenPanel(which) {
  queuePanelGen += 1;
  const show = which === 'form' ? 'q-form' : 'q-raw', hide = which === 'form' ? 'q-raw' : 'q-form';
  queueEl(show).style.display = '';
  queueEl(hide).style.display = 'none';
}
function queueRevealPanel(which, fieldId) {   // after the markup exists: bring the panel into view, then focus its field
  const panel = queueEl(which === 'form' ? 'q-form' : 'q-raw'), field = queueEl(fieldId);
  if (panel && typeof panel.scrollIntoView === 'function') panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (field && typeof field.focus === 'function') field.focus({ preventScroll: true });
}
function queueClosePanels() {
  queueMode = null; queueLint = null; queuePrevSeq += 1; queuePanelGen += 1;
  clearTimeout(queuePrevTimer);
  ['q-form', 'q-raw'].forEach(id => { queueEl(id).innerHTML = ''; queueEl(id).style.display = 'none'; });
}
async function queueLoadMeta() {
  if (queueMeta && queueMetaInstance === queueInstance) return queueMeta;
  let data = null;
  try { const resp = await fetch('/api/queue/meta', { cache: 'no-store' }); data = resp.ok ? await resp.json() : null; } catch (e) { data = null; }
  if (!data || !Array.isArray(data.providers) || !Array.isArray(data.switches)) { queueNote('Formular-Daten nicht ladbar.'); return null; }
  queueMeta = data; queueMetaInstance = queueInstance;
  return data;
}

function queueOpts(list, first) {
  return '<option value="">' + escapeHtml(first) + '</option>'
    + list.map(v => '<option value="' + escapeHtml(v) + '">' + escapeHtml(v) + '</option>').join('');
}
function queueFld(name, label, ctl, hidden, note, cls) {
  return '<div class="q-fld" id="qf-w-' + name + '"' + (hidden ? ' hidden' : '') + '><label><span>' + escapeHtml(label)
    + '</span> ' + ctl + '</label>' + (note ? ' <span class="' + cls + '">' + escapeHtml(note) + '</span>' : '')
    + '<span class="q-err" id="qf-err-' + name + '"></span></div>';
}
function queueInp(name, extra) { return '<input type="text" id="qf-' + name + '" autocomplete="off"' + (extra || '') + '>'; }
function queueGroupedModels(m) {
  return '<option value="">(Standard)</option>' + m.providers.map(p => {
    const note = !p.registered ? ' — nicht registriert' : (!p.allowed ? ' — nicht erlaubt' : '');
    const off = p.registered && p.allowed ? '' : ' disabled';
    return '<optgroup label="' + escapeHtml(p.name + note) + '">' + p.tags.map(t =>
      '<option value="' + escapeHtml(t) + '"' + off + '>' + escapeHtml(t) + '</option>').join('') + '</optgroup>';
  }).join('');
}
function queuePreviewHtml() {
  return '<div class="q-prev"><pre class="q-line" id="qf-line"></pre><div id="qf-hint"></div>'
    + '<ul class="q-danger" id="qf-dangers"></ul><ul id="qf-errors"></ul><ul id="qf-findings"></ul>'
    + '<label id="qf-w-force" hidden><input type="checkbox" id="qf-force"> Trotzdem speichern (die Prüfung meldet Fehler)</label></div>'
    + '<div class="q-btns"><button class="time-btn" id="qf-save" data-action="queue-save" disabled>Speichern</button> '
    + '<button class="time-btn" data-action="queue-cancel">Abbrechen</button></div>';
}
function queueFormHtml(m) {
  const sw = m.switches.map(s => queueFld(s.field, s.tag,
    s.kind === 'flag' ? '<input type="checkbox" id="qf-sw-' + s.field + '">' : queueInp('sw-' + s.field),
    s.field === 'grace' || s.field === 'freshonly', s.hint, s.danger ? 'q-danger' : 'q-muted')).join('');
  const roots = (m.cwd && m.cwd.roots) || [], recent = ((m.cwd && m.cwd.recent) || []).map(c => c.path);
  return '<h3>Neuer Task</h3>'
    + queueFld('text', 'Text', '<textarea id="qf-text" rows="2"></textarea>')
    + queueFld('tool', 'Tool', '<select id="qf-tool">' + queueOpts(m.tools.map(t => t.name), '(kein Tool)') + '</select>')
    + '<div class="q-muted" id="qf-tool-hint"></div>'
    + queueFld('model', 'Provider / Modell', '<select id="qf-model">' + queueGroupedModels(m) + '</select>')
    + queueFld('profile', 'Profil', '<select id="qf-profile">' + queueOpts(m.profiles || [], '(keins)') + '</select>')
    + queueFld('effort', 'Effort', '<select id="qf-effort">' + queueOpts((m.effort && m.effort.levels) || [], '(Standard)') + '</select>',
               false, m.effort && m.effort.note, 'q-muted')
    + queueFld('cwd', 'Ordner (cwd)', queueInp('cwd', ' list="qf-cwd-list"') + '<datalist id="qf-cwd-list">'
      + recent.concat(roots).map(c => '<option value="' + escapeHtml(c) + '"></option>').join('') + '</datalist>')
    + queueFld('id', 'ID', queueInp('id')) + queueFld('needs', 'Wartet auf (IDs, Komma)', queueInp('needs'))
    + queueFld('sched', 'Zeitplan', '<select id="qf-sched"><option value="">sofort</option><option value="once">einmalig</option>'
      + '<option value="every">wiederkehrend</option></select>')
    + queueFld('date', 'Datum', '<input type="date" id="qf-at-date">', true)
    + queueFld('every', 'Alle (z. B. 24h)', queueInp('every'), true)
    + queueFld('at', 'Uhrzeit', '<input type="time" id="qf-at-time">', true)
    + sw
    + queueFld('pos', 'Position', '<select id="qf-pos"><option value="bottom">unten</option><option value="top">oben</option></select>')
    + queuePreviewHtml();
}
function queueEditHtml() {
  return '<h3>Task bearbeiten</h3>' + queueFld('text', 'Ganzer Task-Text mit allen Tags', '<textarea id="qf-edit-text" rows="4"></textarea>')
    + queuePreviewHtml();
}

function queueVal(id) { const el = queueEl(id); return el && el.value ? String(el.value).trim() : ''; }
function queueEffortOn() {
  const tag = queueVal('qf-model');
  const p = queueMeta && queueMeta.providers.find(x => x.tags.includes(tag));
  return !tag || !!(p && p.name === 'claude');
}
function queueSyncForm() {
  if (queueMode !== 'form' || !queueMeta) return;
  const sched = queueVal('qf-sched'), hide = (n, h) => { const w = queueEl('qf-w-' + n); if (w) w.hidden = h; };
  hide('date', sched !== 'once'); hide('every', sched !== 'every'); hide('at', !sched);
  hide('grace', sched !== 'every'); hide('freshonly', sched !== 'every');
  queueEl('qf-effort').disabled = !queueEffortOn();
  const t = queueMeta.tools.find(x => x.name === queueVal('qf-tool'));
  queueEl('qf-tool-hint').textContent = t ? (t.hint || '') : '';
}
// The fields object for /api/queue/lint and /add; empty inputs are left out, Python builds the line.
function queueFormFields() {
  const f = { text: queueVal('qf-text') }, put = (k, v) => { if (v) f[k] = v; };
  ['tool', 'model', 'profile', 'cwd', 'id', 'needs'].forEach(k => put(k, queueVal('qf-' + k)));
  if (queueEffortOn()) put('effort', queueVal('qf-effort'));
  const sched = queueVal('qf-sched'), date = queueVal('qf-at-date'), time = queueVal('qf-at-time');
  if (sched === 'once') put('at', date ? date + 'T' + (time || '00:00') : time);
  if (sched === 'every') { put('every', queueVal('qf-every')); put('at', time); }
  for (const s of queueMeta.switches) {
    if ((s.field === 'grace' || s.field === 'freshonly') && sched !== 'every') continue;
    const el = queueEl('qf-sw-' + s.field);
    if (s.kind === 'flag') { if (el && el.checked) f[s.field] = true; } else put(s.field, queueVal('qf-sw-' + s.field));
  }
  return f;
}

function queueLi(cls, text) { return '<li class="' + escapeHtml(cls) + '">' + escapeHtml(text) + '</li>'; }
function queueHasErr(r) { return (r.findings || []).some(f => f.level === 'error'); }
// field_errors always block; error findings only until "Trotzdem speichern"; warnings never.
function queueCanSave() {
  const r = queueLint;
  if (!r || !r.line || (r.field_errors || []).length) return false;
  return !queueHasErr(r) || !!queueEl('qf-force').checked;
}
function queueUpdateSave() { const b = queueEl('qf-save'); if (b) b.disabled = !queueCanSave(); }
function queueRenderLint(r, err) {
  QUEUE_FIELDS.forEach(n => { const s = queueEl('qf-err-' + n); if (s) s.textContent = ''; });
  const res = r || {}, gen = [];
  for (const fe of res.field_errors || []) {
    const s = QUEUE_FIELDS.includes(fe.field) ? queueEl('qf-err-' + fe.field) : null;
    if (s) s.textContent = fe.message; else gen.push(queueLi('q-f-error', fe.message));
  }
  queueEl('qf-line').textContent = res.line || '';
  queueEl('qf-hint').textContent = res.schedule_hint || err || '';
  queueEl('qf-dangers').innerHTML = (res.dangers || []).map(d => queueLi('q-danger', d.message)).join('');
  queueEl('qf-errors').innerHTML = gen.join('');
  const fl = (res.findings || []).map(f => queueLi('q-f-' + f.level, f.message));
  if (res.lint_available === false) fl.push(queueLi('q-f-info', 'Prüfung nicht verfügbar — nur die Feldprüfung lief.'));
  queueEl('qf-findings').innerHTML = fl.join('');
  queueEl('qf-w-force').hidden = !(r && queueHasErr(r));
  queueUpdateSave();
}
function queueSchedulePreview() {
  queueLint = null; queuePrevSeq += 1;           // an answer for the older input must not unlock saving
  queueUpdateSave();
  clearTimeout(queuePrevTimer);
  queuePrevTimer = setTimeout(queuePreviewNow, 300);
}
async function queuePreviewNow() {
  clearTimeout(queuePrevTimer);
  let body;
  if (queueMode === 'form') body = { fields: queueFormFields(), position: queueVal('qf-pos') || 'bottom' };
  else if (queueMode === 'edit') body = { text: queueVal('qf-edit-text'), ref: queueEditRef };
  else return;
  const seq = ++queuePrevSeq;
  const r = await orchPost('/api/queue/lint', body);
  if (seq !== queuePrevSeq) return;             // a newer keystroke or a closed panel overtook this answer
  if (orchNeedsReload(r)) { queueNeedReload(); return; }
  if (!r.ok || !r.data) { queueLint = null; queueRenderLint(null, (r.data && r.data.error) || 'Vorschau nicht verfügbar.'); return; }
  queueLint = r.data;
  queueRenderLint(r.data);
}
// One delegated listener for typing and changes (registered for input and change in app.js).
function queueOnInput(e) {
  const id = e.target && e.target.id ? String(e.target.id) : '';
  if (id === 'q-search') queueSearchInput();
  else if (id.indexOf('qf-') === 0 && (queueMode === 'form' || queueMode === 'edit')) {
    queueSyncForm();
    if (id === 'qf-force') queueUpdateSave(); else queueSchedulePreview();
  }
}

async function queueOpenForm(mode, entry) {
  const m = await queueLoadMeta();
  if (!m) return;
  queueClosePanels();
  queueMode = mode;
  queueEl('q-form').innerHTML = mode === 'edit' ? queueEditHtml() : queueFormHtml(m);
  if (mode === 'edit') {
    queueEditRef = JSON.parse(JSON.stringify(entry.ref));   // kept as opened; polling never replaces it
    queueEl('qf-edit-text').value = entry.text;
  }
  queueOpenPanel('form');
  queueRevealPanel('form', mode === 'edit' ? 'qf-edit-text' : 'qf-text');
  queueSyncForm();
  await queuePreviewNow();
}
function queueRemember(line) {
  const t = String(line || '').replace(/^\s*-\s*\[.\]\s*/, '');
  queueWritten = queueState && queueState.entries.some(e => e.text === t) ? t : null;
}
async function queueSave() {
  if (!queueCanSave()) return false;
  const d = queueLint.dangers || [], extra = d.length ? { confirm_dangerous: true } : {};
  if (d.length && !confirm(d.map(x => x.message).join('\n') + '\n\nTrotzdem speichern?')) return false;
  const text = queueMode === 'edit' ? queueVal('qf-edit-text') : queueLint.line, gen = queuePanelGen;
  const ok = queueMode === 'edit'
    ? await queueRun('edit', Object.assign({ ref: queueEditRef, text }, extra))
    : await queueRun('add', Object.assign({ fields: queueFormFields(), position: queueVal('qf-pos') || 'bottom' }, extra));
  if (ok) {
    if (gen === queuePanelGen) queueClosePanels();   // a panel opened meanwhile keeps its draft
    queueRemember(text);                              // belongs to the file (overwritten banner), not the panel
  }
  return ok;
}

async function queueOpenRaw() {
  let data = null;
  try { const resp = await fetch('/api/queue?raw=1', { cache: 'no-store' }); data = resp.ok ? await resp.json() : null; } catch (e) { data = null; }
  if (!data || typeof data.raw !== 'string') { queueNote('Rohtext nicht ladbar (Datei fehlt oder ist nicht UTF-8).'); return; }
  queueClosePanels();
  queueMode = 'raw'; queueRawSha = data.file.sha;
  queueEl('q-raw').innerHTML = '<h3>Rohtext der Queue-Datei</h3><textarea id="q-raw-text" rows="18" spellcheck="false"></textarea>'
    + '<div class="q-banner warn" id="qr-hint" hidden><span id="qr-hint-text"></span> '
    + '<button class="time-btn" data-action="queue-raw-reload">Neu laden</button></div>'
    + '<div class="q-btns"><button class="time-btn" data-action="queue-raw-check">Prüfen</button> '
    + '<button class="time-btn" data-action="queue-raw-save">Speichern</button> '
    + '<button class="time-btn" data-action="queue-cancel">Verwerfen</button></div><ul id="qr-result"></ul>';
  queueEl('q-raw-text').value = data.raw;
  queueOpenPanel('raw');
  queueRevealPanel('raw', 'q-raw-text');
}
async function queueRawCheck() {
  const r = await orchPost('/api/queue/lint', { content: queueEl('q-raw-text').value });
  if (orchNeedsReload(r)) { queueNeedReload(); return; }
  const d = r.data || {};
  const items = (d.file_findings || []).concat(d.findings || []).map(f =>
    queueLi('q-f-' + f.level, (f.line ? 'Z. ' + f.line + ': ' : '') + f.message));
  queueEl('qr-result').innerHTML = r.ok ? (items.join('') || queueLi('q-f-info', 'Keine Befunde.'))
                                        : queueLi('q-f-error', d.error || 'Prüfung fehlgeschlagen.');
}
async function queueRawSave() {
  const gen = queuePanelGen;
  const ok = await queueRun('raw', { sha: queueRawSha, content: queueEl('q-raw-text').value });
  if (ok) { if (gen === queuePanelGen) queueClosePanels(); return true; }
  if (gen !== queuePanelGen) return false;       // the panel was closed or replaced meanwhile: no hint into a foreign panel
  const r = queueLastR || {}, code = r.data && r.data.code;
  if (r.status === 409 && code === 'sha_mismatch') {     // the user's text stays; reloading is their choice
    queueEl('qr-hint-text').textContent = 'Die Datei wurde inzwischen geändert. Dein Text bleibt hier stehen; „Neu laden“ ersetzt ihn durch den aktuellen Stand.';
    queueEl('qr-hint').hidden = false;
  } else if (r.status === 422 && r.data) {
    queueEl('qr-hint-text').textContent = r.data.error || 'Nicht gespeichert.';
    queueEl('qr-hint').hidden = false;
  }
  return false;
}
function queueSearchInput() { renderQueue(); }

registerActions({
  'queue-delete': el => queueEntryAction(el, 'delete', null, 'Task wirklich löschen?'),
  'queue-retry': el => queueEntryAction(el, 'retry'),
  'queue-duplicate': el => queueEntryAction(el, 'duplicate'),
  'queue-move-up': el => queueEntryAction(el, 'move', { direction: 'up' }),
  'queue-move-down': el => queueEntryAction(el, 'move', { direction: 'down' }),
  'queue-edit': el => { const e = queueEntryOf(el); if (e) return queueOpenForm('edit', e); },
  'queue-new': () => queueOpenForm('form'),
  'queue-raw': () => queueOpenRaw(),
  'queue-save': () => queueSave(),
  'queue-cancel': () => queueClosePanels(),
  'queue-raw-check': () => queueRawCheck(),
  'queue-raw-save': () => queueRawSave(),
  'queue-raw-reload': () => { if (confirm('Dein Text im Rohtext-Feld wird durch den aktuellen Dateistand ersetzt. Fortfahren?')) return queueOpenRaw(); },
  'queue-expand': el => {
    const e = queueEntryOf(el);
    if (!e) return;
    if (queueExpanded.has(e.ref.start)) queueExpanded.delete(e.ref.start); else queueExpanded.add(e.ref.start);
    renderQueue();
  },
  'queue-undo': () => {
    const u = queueState && queueState.undo;
    if (u && u.available) return queueRun('undo', { backup_id: u.id });
  },
  'queue-reload-page': () => { location.reload(); },
});
