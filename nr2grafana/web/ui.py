"""Embedded single-page UI for the nr2grafana web app.

One module-level HTML string (``PAGE``) with inline CSS and JS -- no
external assets, fonts, or CDNs. Served by web/server.py at ``GET /``.
"""

from __future__ import annotations

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nr2grafana</title>
<style>
:root {
  --bg: #0e1116;
  --bg2: #161b23;
  --bg3: #1e2530;
  --border: #2a3341;
  --text: #dde3ec;
  --muted: #8b96a7;
  --accent: #4c9aff;
  --accent-dim: #1f3a5f;
  --green: #3fb96a;
  --green-bg: #12301e;
  --amber: #e2a33c;
  --amber-bg: #33270e;
  --red: #e05c5c;
  --red-bg: #341518;
  --blue: #5aa2e8;
  --blue-bg: #14263a;
  --shadow: 0 1px 3px rgba(0,0,0,.4);
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
[data-theme="light"] {
  --bg: #f4f6f9;
  --bg2: #ffffff;
  --bg3: #eef1f5;
  --border: #d7dde6;
  --text: #1c2430;
  --muted: #5c6878;
  --accent: #1a6ed8;
  --accent-dim: #d8e7fa;
  --green: #1d8a48;
  --green-bg: #e2f4e9;
  --amber: #a86d0c;
  --amber-bg: #faf0d8;
  --red: #c03434;
  --red-bg: #fae3e3;
  --blue: #2470b8;
  --blue-bg: #e2eefa;
  --shadow: 0 1px 3px rgba(20,30,50,.12);
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --bg: #f4f6f9;
    --bg2: #ffffff;
    --bg3: #eef1f5;
    --border: #d7dde6;
    --text: #1c2430;
    --muted: #5c6878;
    --accent: #1a6ed8;
    --accent-dim: #d8e7fa;
    --green: #1d8a48;
    --green-bg: #e2f4e9;
    --amber: #a86d0c;
    --amber-bg: #faf0d8;
    --red: #c03434;
    --red-bg: #fae3e3;
    --blue: #2470b8;
    --blue-bg: #e2eefa;
    --shadow: 0 1px 3px rgba(20,30,50,.12);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--bg); color: var(--text);
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI",
        Roboto, Helvetica, Arial, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
#app { display: flex; min-height: 100vh; }

/* ---- sidebar ---- */
#sidebar {
  width: 218px; flex: 0 0 218px; background: var(--bg2);
  border-right: 1px solid var(--border);
  padding: 18px 12px; position: sticky; top: 0; height: 100vh;
  display: flex; flex-direction: column;
}
.brand { display: flex; align-items: center; gap: 9px;
  padding: 2px 8px 16px; }
.brand-mark {
  width: 28px; height: 28px; border-radius: 7px; flex: 0 0 28px;
  background: linear-gradient(135deg, #1ce783 0%, #f46800 100%);
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 800; font-size: 13px;
}
.brand-name { font-weight: 700; font-size: 15px; letter-spacing: .2px; }
.brand-sub { font-size: 11px; color: var(--muted); }
.nav a {
  display: flex; align-items: center; gap: 9px;
  padding: 8px 10px; border-radius: 7px; color: var(--text);
  font-weight: 500; margin-bottom: 2px;
}
.nav a:hover { background: var(--bg3); text-decoration: none; }
.nav a.active { background: var(--accent-dim); color: var(--accent); }
.nav .ico { width: 18px; text-align: center; opacity: .85; }
.sidebar-foot { margin-top: auto; padding: 10px 8px 0;
  font-size: 11px; color: var(--muted); }

/* ---- header ---- */
#mainwrap { flex: 1; min-width: 0; display: flex;
  flex-direction: column; }
#topbar {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; gap: 12px;
  padding: 10px 22px; background: var(--bg2);
  border-bottom: 1px solid var(--border);
}
#crumb { font-weight: 600; font-size: 14px; flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--border); border-radius: 999px;
  padding: 3px 10px; font-size: 12px; color: var(--muted);
  background: var(--bg); white-space: nowrap;
}
.pill .dot { width: 8px; height: 8px; border-radius: 50%;
  background: var(--muted); }
.pill.ok { color: var(--green); border-color: var(--green); }
.pill.ok .dot { background: var(--green); }
.pill.err { color: var(--red); border-color: var(--red); }
.pill.err .dot { background: var(--red); }
#themebtn { cursor: pointer; }

/* ---- main ---- */
main { padding: 22px; max-width: 1180px; width: 100%;
  margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 0 0 10px; }
.lead { color: var(--muted); margin: 0 0 18px; }
.card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px; box-shadow: var(--shadow);
  margin-bottom: 16px;
}
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }
.cards-row { display: flex; gap: 12px; flex-wrap: wrap;
  margin-bottom: 16px; }
.stat-card { background: var(--bg2); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 18px; min-width: 130px; }
.stat-card .num { font-size: 22px; font-weight: 700; }
.stat-card .lbl { font-size: 12px; color: var(--muted); }

label { display: block; font-size: 12px; font-weight: 600;
  color: var(--muted); margin: 10px 0 4px; }
input, select, textarea {
  width: 100%; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 7px;
  padding: 7px 10px; font: inherit; outline: none;
}
textarea { font-family: var(--mono); font-size: 12.5px;
  min-height: 74px; resize: vertical; }
input:focus, select:focus, textarea:focus {
  border-color: var(--accent); }
.row { display: flex; gap: 8px; align-items: center;
  flex-wrap: wrap; }
.row > * { width: auto; }
.btnbar { margin-top: 12px; display: flex; gap: 8px;
  flex-wrap: wrap; align-items: center; }

.btn {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--bg3); color: var(--text);
  border: 1px solid var(--border); border-radius: 7px;
  padding: 7px 14px; font: inherit; font-weight: 600;
  cursor: pointer; white-space: nowrap;
}
.btn:hover { border-color: var(--accent); color: var(--accent); }
.btn.primary { background: var(--accent); border-color: var(--accent);
  color: #fff; }
.btn.primary:hover { filter: brightness(1.1); color: #fff; }
.btn.small { padding: 4px 10px; font-size: 12px; }
.btn:disabled { opacity: .5; cursor: default; }
.btn.busy::after { content: ""; width: 11px; height: 11px;
  border: 2px solid currentColor; border-top-color: transparent;
  border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
.tablewrap { overflow-x: auto; }
th { text-align: left; font-size: 11px; text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted); font-weight: 600;
  padding: 8px 10px; border-bottom: 1px solid var(--border); }
td { padding: 9px 10px; border-bottom: 1px solid var(--border);
  vertical-align: top; }
tr.click { cursor: pointer; }
tr.click:hover td { background: var(--bg3); }
tr.expand-row td { background: var(--bg); padding: 14px 16px; }

.chip {
  display: inline-block; border-radius: 5px; padding: 1px 8px;
  font-size: 11.5px; font-weight: 600; margin: 1px 3px 1px 0;
  border: 1px solid transparent; white-space: nowrap;
}
.chip.ok    { color: var(--green); background: var(--green-bg); }
.chip.warn  { color: var(--amber); background: var(--amber-bg); }
.chip.err   { color: var(--red);   background: var(--red-bg); }
.chip.info  { color: var(--blue);  background: var(--blue-bg); }
.chip.dim   { color: var(--muted); background: var(--bg3); }

pre, code, .mono { font-family: var(--mono); font-size: 12.5px; }
pre {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 7px; padding: 10px 12px; overflow-x: auto;
  margin: 6px 0; white-space: pre-wrap; word-break: break-word;
}
.log {
  background: #0b0e13; color: #c7d0dc; border: 1px solid var(--border);
  border-radius: 7px; font-family: var(--mono); font-size: 12px;
  padding: 10px 12px; max-height: 300px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word; display: none;
}
[data-theme="light"] .log { background: #10141b; }
.log.show { display: block; }

.empty {
  border: 1px dashed var(--border); border-radius: 10px;
  padding: 34px 20px; text-align: center; color: var(--muted);
}
.empty b { color: var(--text); }
.helper { font-size: 12px; color: var(--muted); margin-top: 6px; }
.kv { font-size: 12px; color: var(--muted); }
.kv b { color: var(--text); }
.err-text { color: var(--red); font-family: var(--mono);
  font-size: 12px; white-space: pre-wrap; word-break: break-word; }
.sec-note { font-size: 12px; color: var(--muted);
  border-left: 3px solid var(--accent); padding-left: 10px;
  margin: 10px 0; }
.ai-box { border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px; background: var(--bg); margin-top: 10px; }
.ai-box .conf { float: right; }

/* chat */
#chatlog { max-height: 52vh; overflow-y: auto; padding: 4px; }
.msg { max-width: 82%; margin: 8px 0; padding: 9px 13px;
  border-radius: 10px; white-space: pre-wrap; word-break: break-word; }
.msg.user { background: var(--accent-dim); margin-left: auto; }
.msg.assistant { background: var(--bg3); }
.msg pre { margin: 8px 0; }

#toasts { position: fixed; right: 18px; bottom: 18px; z-index: 100;
  display: flex; flex-direction: column; gap: 8px; max-width: 420px; }
.toast { background: var(--bg2); border: 1px solid var(--border);
  border-left: 4px solid var(--accent); border-radius: 8px;
  padding: 10px 14px; box-shadow: var(--shadow); font-size: 13px;
  word-break: break-word; }
.toast.err { border-left-color: var(--red); }
.toast.ok { border-left-color: var(--green); }

.backlink { font-size: 12.5px; display: inline-block;
  margin-bottom: 8px; }
.checkbox-row { display: flex; gap: 8px; align-items: center;
  padding: 6px 4px; border-bottom: 1px solid var(--border); }
.checkbox-row input { width: auto; }
.right { text-align: right; }
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <div class="brand">
      <div class="brand-mark">nG</div>
      <div>
        <div class="brand-name">nr2grafana</div>
        <div class="brand-sub" id="verline">migration studio</div>
      </div>
    </div>
    <nav class="nav" id="nav">
      <a href="#/setup" data-r="setup"><span class="ico">&#9881;</span>
        Setup</a>
      <a href="#/dashboards" data-r="dashboards">
        <span class="ico">&#9638;</span> Dashboards</a>
      <a href="#/convert" data-r="convert"><span class="ico">&#8635;
        </span> Convert &amp; Package</a>
      <a href="#/test" data-r="test"><span class="ico">&#10003;</span>
        Validate &amp; Test</a>
      <a href="#/import" data-r="import"><span class="ico">&#8682;
        </span> Import</a>
      <a href="#/changes" data-r="changes"><span class="ico">&#916;
        </span> Changes</a>
      <a href="#/ai" data-r="ai"><span class="ico">&#10024;</span>
        AI Assistant</a>
    </nav>
    <div class="sidebar-foot">
      Local only &mdash; API keys stay in server memory,<br>
      never written to disk.
    </div>
  </aside>
  <div id="mainwrap">
    <header id="topbar">
      <div id="crumb">nr2grafana</div>
      <span class="pill" id="pill-nr"><span class="dot"></span>
        New Relic</span>
      <span class="pill" id="pill-gf"><span class="dot"></span>
        Grafana</span>
      <span class="pill" id="pill-ai"><span class="dot"></span>
        AI</span>
      <span class="pill" id="themebtn" title="Theme">
        <span id="themelbl">Auto</span></span>
    </header>
    <main id="view"></main>
  </div>
</div>
<div id="toasts"></div>
<script>
'use strict';

/* ------------------------------------------------------ utilities */
var App = { state: null, dashboards: [], expanded: {}, ai: [],
            aiBusy: false, timers: [] };

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function $(sel, el) { return (el || document).querySelector(sel); }
function $all(sel, el) {
  return Array.prototype.slice.call(
    (el || document).querySelectorAll(sel));
}

async function api(path, body, method) {
  var opt = { method: method || (body === undefined ? 'GET' : 'POST'),
              headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  var res;
  try { res = await fetch(path, opt); }
  catch (e) {
    throw new Error('Cannot reach the nr2grafana server (' +
                    e.message + '). Is it still running?');
  }
  var data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    throw new Error(data.error || ('HTTP ' + res.status));
  }
  return data;
}

function toast(msg, kind) {
  var el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(function () { el.remove(); },
             kind === 'err' ? 9000 : 5000);
}

function busy(btn, on) {
  if (!btn) return;
  btn.disabled = !!on;
  btn.classList.toggle('busy', !!on);
}

/* Poll a job id; onUpdate(job) each tick; resolves with the job when
   done, rejects on error status. */
function pollJob(id, onUpdate) {
  return new Promise(function (resolve, reject) {
    var t = setInterval(async function () {
      var job;
      try { job = await api('/api/jobs/' + id); }
      catch (e) { clearInterval(t); reject(e); return; }
      if (onUpdate) onUpdate(job);
      if (job.status === 'done') { clearInterval(t); resolve(job); }
      else if (job.status === 'error') {
        clearInterval(t);
        reject(new Error(job.error || 'job failed'));
      }
    }, 700);
    App.timers.push(t);
  });
}

function logInto(el) {
  return function (job) {
    if (!el) return;
    el.classList.add('show');
    el.textContent = (job.log || []).join('\n');
    el.scrollTop = el.scrollHeight;
  };
}

async function runJob(path, body, logEl) {
  var r = await api(path, body || {});
  return pollJob(r.job, logInto(logEl));
}

/* ------------------------------------------------------ chips */
var CONF_CLS = { exact: 'ok', approximate: 'info',
                 'needs-review': 'warn', untranslatable: 'err' };
var TEST_CLS = { data: 'ok', 'no-data': 'warn', error: 'err' };

function confChips(counts) {
  var order = ['exact', 'approximate', 'needs-review',
               'untranslatable'];
  var html = '';
  order.forEach(function (k) {
    if (counts && counts[k]) {
      html += '<span class="chip ' + CONF_CLS[k] + '">' + counts[k] +
              ' ' + esc(k) + '</span>';
    }
  });
  return html || '<span class="chip dim">no panels</span>';
}

function confChip(c) {
  return '<span class="chip ' + (CONF_CLS[c] || 'dim') + '">' +
         esc(c || '?') + '</span>';
}

function testChip(s) {
  if (!s) return '<span class="chip dim">not tested</span>';
  return '<span class="chip ' + (TEST_CLS[s] || 'dim') + '">' +
         esc(s) + '</span>';
}

function dsChips(list) {
  return (list || []).filter(Boolean).map(function (d) {
    return '<span class="chip info">' + esc(d) + '</span>';
  }).join('') || '<span class="chip dim">none</span>';
}

/* ------------------------------------------------------ state/pills */
async function refreshState() {
  try {
    App.state = await api('/api/state');
    renderPills();
    var v = $('#verline');
    if (v && App.state.version) v.textContent = 'v' + App.state.version;
  } catch (e) { /* server briefly busy; keep old state */ }
}

function pillSet(id, status, title) {
  var el = $(id);
  el.classList.remove('ok', 'err');
  if (status === 'ok') el.classList.add('ok');
  else if (status === 'error') el.classList.add('err');
  el.title = title || '';
}

function renderPills() {
  var s = App.state; if (!s) return;
  var d = s.status_detail || {};
  pillSet('#pill-nr', s.status.newrelic,
          d.newrelic || (s.session.nr_key_set ?
          'key set, not tested yet' : 'no API key configured'));
  pillSet('#pill-gf', s.status.grafana,
          d.grafana || (s.session.grafana_url ?
          s.session.grafana_url : 'no Grafana URL configured'));
  pillSet('#pill-ai', s.status.ai,
          d.ai || (s.session.anthropic_key_set ?
          'key set' : 'no Anthropic key configured'));
}

/* ------------------------------------------------------ router */
var VIEWS = { setup: vSetup, dashboards: vDashboards,
              convert: vConvert, test: vTest, import: vImport,
              changes: vChanges, ai: vAI };

function crumb(text) { $('#crumb').textContent = text; }

async function route() {
  App.timers.forEach(clearInterval); App.timers = [];
  var h = location.hash.replace(/^#\/?/, '');
  if (!h) {
    h = (App.state && App.state.db &&
         App.state.db.dashboards > 0) ? 'dashboards' : 'setup';
  }
  var parts = h.split('/');
  var name = parts[0] || 'setup';
  $all('#nav a').forEach(function (a) {
    a.classList.toggle('active', a.getAttribute('data-r') === name ||
      (name === 'dashboards' && parts[1] &&
       a.getAttribute('data-r') === 'dashboards'));
  });
  var view = $('#view');
  try {
    if (name === 'dashboards' && parts[1]) {
      await vDetail(view, decodeURIComponent(parts[1]));
    } else {
      await (VIEWS[name] || vSetup)(view);
    }
  } catch (e) {
    view.innerHTML = '<div class="empty"><b>Something went wrong'
      + '</b><br>' + esc(e.message) + '</div>';
  }
}

/* ====================================================== SETUP */
async function vSetup(view) {
  crumb('Setup');
  var s = App.state || await api('/api/state');
  App.state = s;
  var ses = s.session;
  view.innerHTML =
  '<h1>Setup</h1>' +
  '<p class="lead">Connect New Relic (source), Grafana (target) and ' +
  'optionally Claude for AI help.</p>' +
  '<div class="sec-note">API keys are held in the server process ' +
  'memory only. They are never written to the database, to disk, or ' +
  'to logs, and are gone when the server stops.</div>' +
  '<div class="grid2">' +

  '<div class="card"><h2>New Relic</h2>' +
  '<label>User API key (NRAK-...)</label>' +
  '<input type="password" id="su-nrkey" placeholder="' +
    (ses.nr_key_set ? '**** key set' :
     'NRAK-...') + '">' +
  '<label>Region</label>' +
  '<select id="su-region"><option' +
    (ses.nr_region === 'US' ? ' selected' : '') + '>US</option>' +
  '<option' + (ses.nr_region === 'EU' ? ' selected' : '') +
    '>EU</option></select>' +
  '<div class="btnbar">' +
  '<button class="btn primary" id="su-nr-save">Save</button>' +
  '<button class="btn" id="su-nr-test">Test connection</button>' +
  '<span class="kv" id="su-nr-status"></span></div>' +
  '<div class="log" id="su-nr-log"></div></div>' +

  '<div class="card"><h2>Grafana</h2>' +
  '<label>URL</label>' +
  '<input id="su-gfurl" placeholder="http://localhost:3000" value="' +
    esc(ses.grafana_url) + '">' +
  '<label>Service account token</label>' +
  '<input type="password" id="su-gftoken" placeholder="' +
    (ses.grafana_token_set ? '**** token set' :
     'glsa_...') + '">' +
  '<div class="btnbar">' +
  '<button class="btn primary" id="su-gf-save">Save</button>' +
  '<button class="btn" id="su-gf-test">Test connection</button>' +
  '<span class="kv" id="su-gf-status"></span></div></div>' +

  '<div class="card"><h2>AI assistance (optional)</h2>' +
  '<label>Anthropic API key</label>' +
  '<input type="password" id="su-aikey" placeholder="' +
    (ses.anthropic_key_set ? '**** key set' :
     'sk-ant-...') + '">' +
  '<label>Model</label>' +
  '<input id="su-aimodel" placeholder="claude-sonnet-5 (default)"' +
    ' value="' + esc(ses.ai_model) + '">' +
  '<div class="btnbar">' +
  '<button class="btn primary" id="su-ai-save">Save</button>' +
  '<span class="kv">Powers &quot;Ask AI&quot; on failing panels and ' +
  'the assistant chat.</span></div></div>' +

  '<div class="card"><h2>Workspace</h2>' +
  '<label>New Relic export directory (fetch writes here)</label>' +
  '<input id="su-indir" value="' + esc(ses.input_dir) + '">' +
  '<label>Output directory (packages are written here)</label>' +
  '<input id="su-outdir" value="' + esc(ses.out_dir) + '">' +
  '<label>Mapping config JSON (optional)</label>' +
  '<input id="su-cfg" value="' + esc(ses.config_path) +
    '" placeholder="config/mappings.json">' +
  '<div class="btnbar">' +
  '<button class="btn primary" id="su-ws-save">Save</button>' +
  '<span class="kv">Database: <span class="mono">' +
    esc((s.db && s.db.path) || '~/.nr2grafana') + '</span></span>' +
  '</div></div>' +

  '</div>';

  function saveSettings(body, btn, after) {
    busy(btn, true);
    api('/api/settings', body).then(function () {
      toast('Saved', 'ok');
      return refreshState();
    }).then(after || null).catch(function (e) {
      toast(e.message, 'err');
    }).finally(function () { busy(btn, false); });
  }

  $('#su-nr-save').onclick = function () {
    var body = { nr_region: $('#su-region').value };
    var k = $('#su-nrkey').value.trim();
    if (k) body.nr_api_key = k;
    saveSettings(body, this);
  };
  $('#su-nr-test').onclick = async function () {
    var btn = this; busy(btn, true);
    $('#su-nr-status').textContent = 'listing dashboards...';
    try {
      var k = $('#su-nrkey').value.trim();
      var body = { nr_region: $('#su-region').value };
      if (k) body.nr_api_key = k;
      await api('/api/settings', body);
      var job = await runJob('/api/nr/list', {}, $('#su-nr-log'));
      $('#su-nr-status').textContent = 'OK - ' +
        job.result.count + ' dashboards visible';
      toast('New Relic OK: ' + job.result.count + ' dashboards',
            'ok');
    } catch (e) {
      $('#su-nr-status').textContent = '';
      toast('New Relic: ' + e.message, 'err');
    }
    busy(btn, false); refreshState();
  };
  $('#su-gf-save').onclick = function () {
    var body = { grafana_url: $('#su-gfurl').value.trim() };
    var t = $('#su-gftoken').value.trim();
    if (t) body.grafana_token = t;
    saveSettings(body, this);
  };
  $('#su-gf-test').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      var body = { grafana_url: $('#su-gfurl').value.trim() };
      var t = $('#su-gftoken').value.trim();
      if (t) body.grafana_token = t;
      await api('/api/settings', body);
      var h = await api('/api/grafana/health', {});
      $('#su-gf-status').textContent = 'OK - Grafana ' +
        (h.version || '');
      toast('Grafana reachable' +
            (h.version ? ' (v' + h.version + ')' : ''), 'ok');
    } catch (e) {
      $('#su-gf-status').textContent = '';
      toast('Grafana: ' + e.message, 'err');
    }
    busy(btn, false); refreshState();
  };
  $('#su-ai-save').onclick = function () {
    var body = { ai_model: $('#su-aimodel').value.trim() };
    var k = $('#su-aikey').value.trim();
    if (k) body.anthropic_api_key = k;
    saveSettings(body, this);
  };
  $('#su-ws-save').onclick = function () {
    saveSettings({ input_dir: $('#su-indir').value.trim(),
                   out_dir: $('#su-outdir').value.trim(),
                   config_path: $('#su-cfg').value.trim() }, this);
  };
}

/* ====================================================== DASHBOARDS */
async function vDashboards(view) {
  crumb('Dashboards');
  view.innerHTML = '<h1>Dashboards</h1><p class="lead">Converted ' +
    'dashboards in the local workspace.</p><div id="dash-area">' +
    '<div class="empty">Loading...</div></div>';
  var data = await api('/api/dashboards');
  App.dashboards = data.dashboards || [];
  var area = $('#dash-area');
  if (!App.dashboards.length) {
    area.innerHTML = '<div class="empty"><b>No dashboards yet.</b>' +
      '<br>Fetch your New Relic dashboards and run ' +
      '<a href="#/convert">Convert &amp; Package</a> to get started.' +
      '</div>';
    return;
  }
  var rows = App.dashboards.map(function (d) {
    return '<tr class="click" data-slug="' + esc(d.slug) + '">' +
      '<td><b>' + esc(d.title) + '</b><div class="kv mono">' +
        esc(d.slug) + '</div></td>' +
      '<td>' + (d.panels || 0) + '</td>' +
      '<td>' + confChips(d.confidence) + '</td>' +
      '<td>' + dsChips(d.datasources) + '</td>' +
      '<td>' + dsChips(d.domains) + '</td>' +
      '<td>' + (d.datatest_summary &&
                Object.keys(d.datatest_summary).length ?
        Object.keys(d.datatest_summary).map(function (k) {
          return '<span class="chip ' + (TEST_CLS[k] || 'dim') +
                 '">' + d.datatest_summary[k] + ' ' + esc(k) +
                 '</span>';
        }).join('') : '<span class="chip dim">not tested</span>') +
      '</td></tr>';
  }).join('');
  area.innerHTML = '<div class="card"><div class="tablewrap">' +
    '<table><thead><tr><th>Dashboard</th><th>Panels</th>' +
    '<th>Confidence</th><th>Datasources</th><th>Domains</th>' +
    '<th>Data test</th></tr></thead><tbody>' + rows +
    '</tbody></table></div></div>';
  $all('tr.click', area).forEach(function (tr) {
    tr.onclick = function () {
      location.hash = '#/dashboards/' +
        encodeURIComponent(tr.getAttribute('data-slug'));
    };
  });
}

/* ====================================================== DETAIL */
function reqStatusChip(items, ds) {
  if (!items || !items.length)
    return '<span class="chip dim">not checked</span>';
  var m = null;
  items.forEach(function (it) {
    var name = String(it.item || '').toLowerCase();
    if (name.indexOf(String(ds.family || '').toLowerCase()) >= 0 ||
        (ds.plugin_id &&
         name.indexOf(String(ds.plugin_id).toLowerCase()) >= 0)) {
      m = it;
    }
  });
  if (!m) return '<span class="chip dim">not checked</span>';
  var cls = m.status === 'ok' ? 'ok' :
            (m.status === 'missing' ? 'err' : 'warn');
  var fix = m.status !== 'ok' && m.fix ?
    '<div class="kv">' + esc(m.fix) + '</div>' : '';
  return '<span class="chip ' + cls + '">' + esc(m.status) +
         '</span>' + fix;
}

function worstStatus(tests) {
  var vals = Object.keys(tests).map(function (k) {
    return tests[k].status;
  });
  if (vals.indexOf('error') >= 0) return 'error';
  if (vals.indexOf('no-data') >= 0) return 'no-data';
  if (vals.indexOf('data') >= 0) return 'data';
  return '';
}

async function vDetail(view, slug) {
  crumb('Dashboard / ' + slug);
  view.innerHTML = '<div class="empty">Loading ' + esc(slug) +
    '...</div>';
  var d = await api('/api/dashboards/' + encodeURIComponent(slug));
  App.detail = d;

  /* panel map from the (authoritative) dashboard json */
  var panelMap = {};
  (function walk(ps) {
    (ps || []).forEach(function (p) {
      panelMap[p.id] = p;
      if (p.type === 'row') walk(p.panels);
    });
  })(d.dashboard.panels);

  var tests = {};
  ((d.datatest || {}).results || []).forEach(function (r) {
    (tests[r.panel_id] = tests[r.panel_id] || {})[r.refId || 'A'] = r;
  });

  var reqs = d.requirements || {};
  var checkItems = (d.check || {}).items || [];

  var dsRows = (reqs.datasources || []).map(function (ds) {
    return '<tr><td><b>' + esc(ds.family) + '</b>' +
      (ds.required === false ?
        ' <span class="chip dim">optional</span>' : '') + '</td>' +
      '<td class="mono">' + esc(ds.plugin_id || '') + '</td>' +
      '<td>' + esc(ds.purpose || '') + '</td>' +
      '<td class="mono">' + esc(ds.uid_ref || '') + '</td>' +
      '<td>' + reqStatusChip(checkItems, ds) + '</td></tr>';
  }).join('');

  var pluginRows = (reqs.plugins || []).map(function (p) {
    return '<div class="kv" style="margin:4px 0"><b class="mono">' +
      esc(p.id) + '</b> - ' + esc(p.reason || '') +
      (p.grafana_cli ? '<pre>' + esc(p.grafana_cli) + '</pre>' : '') +
      '</div>';
  }).join('');

  var domainRows = (reqs.domains || []).map(function (dm) {
    var opts = (dm.options || []).map(function (o) {
      return '<li>' + (o.plugin_id ? '<b class="mono">' +
        esc(o.plugin_id) + '</b>: ' : '') + esc(o.note || '') +
        '</li>';
    }).join('');
    return '<div style="margin:6px 0"><span class="chip info">' +
      esc(dm.domain) + '</span> <span class="kv">panels ' +
      esc((dm.panel_ids || []).join(', ')) + '</span>' +
      (opts ? '<ul class="kv" style="margin:4px 0 0">' + opts +
       '</ul>' : '') + '</div>';
  }).join('');

  var nrNative = (reqs.nr_native || []).map(function (n) {
    return '<div class="kv" style="margin:4px 0">panel ' +
      esc(n.panel_id) + ' <b>' + esc(n.widget || '') + '</b> - ' +
      esc(n.why || '') + (n.equivalent ?
      ' <i>Equivalent: ' + esc(n.equivalent) + '</i>' : '') +
      '</div>';
  }).join('');

  var panelRows = (d.widget_report || []).map(function (w, i) {
    var pt = tests[w.panel_id] || {};
    var open = App.expanded[slug + ':' + w.panel_id];
    var row = '<tr class="click" data-exp="' + esc(w.panel_id) +
      '"><td>' + esc(w.panel_id) + '</td>' +
      '<td><b>' + esc(w.widget || w.widget_title || '(untitled)') +
      '</b>' +
      '<div class="kv">' + esc(w.page || '') + '</div></td>' +
      '<td class="mono">' + esc(w.panel_type || '') + '</td>' +
      '<td>' + confChip(w.confidence) + '</td>' +
      '<td>' + testChip(worstStatus(pt)) + '</td>' +
      '<td>' + (open ? '&#9662;' : '&#9656;') + '</td></tr>';
    if (open) {
      row += '<tr class="expand-row"><td colspan="6">' +
        panelDetailHtml(slug, w, panelMap[w.panel_id], pt) +
        '</td></tr>';
    }
    return row;
  }).join('');

  view.innerHTML =
    '<a class="backlink" href="#/dashboards">&larr; All dashboards' +
    '</a>' +
    '<h1>' + esc(d.title) + '</h1>' +
    '<p class="lead mono">' + esc(slug) +
    (d.package_dir ? ' &middot; package: ' + esc(d.package_dir) : '') +
    '</p>' +
    '<div class="btnbar" style="margin-bottom:16px">' +
    '<button class="btn" id="dt-check">Check requirements</button>' +
    '<button class="btn" id="dt-test">Run data tests</button>' +
    '<button class="btn primary" id="dt-import">Import to Grafana' +
    '</button></div>' +
    '<div class="log" id="dt-log"></div>' +

    '<div class="card"><h2>Install these first</h2>' +
    (dsRows ?
      '<div class="tablewrap"><table><thead><tr><th>Datasource</th>' +
      '<th>Plugin</th><th>Purpose</th><th>Referenced as</th>' +
      '<th>Live status</th></tr></thead><tbody>' + dsRows +
      '</tbody></table></div>' :
      '<div class="kv">No datasource requirements recorded. Re-run ' +
      'Convert &amp; Package to generate them.</div>') +
    (pluginRows ? '<h2 style="margin-top:14px">Plugins</h2>' +
      pluginRows : '') +
    (domainRows ? '<h2 style="margin-top:14px">Detected data ' +
      'domains</h2>' + domainRows : '') +
    (nrNative ? '<h2 style="margin-top:14px">New Relic-native ' +
      'widgets</h2>' + nrNative : '') +
    '</div>' +

    '<div class="card"><h2>Panels</h2><div class="tablewrap">' +
    '<table><thead><tr><th>Id</th><th>Panel</th><th>Type</th>' +
    '<th>Confidence</th><th>Test</th><th></th></tr></thead>' +
    '<tbody>' + (panelRows ||
      '<tr><td colspan="6" class="kv">no widget report</td></tr>') +
    '</tbody></table></div></div>';

  /* actions */
  $('#dt-check').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      await api('/api/grafana/check', { slug: slug });
      toast('Requirement check complete', 'ok');
      vDetail(view, slug);
    } catch (e) { toast(e.message, 'err'); busy(btn, false); }
  };
  $('#dt-test').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      var job = await runJob('/api/grafana/test', { slug: slug },
                             $('#dt-log'));
      var s = job.result.summary || {};
      toast('Tested: ' + Object.keys(s).map(function (k) {
        return s[k] + ' ' + k; }).join(', '), 'ok');
      vDetail(view, slug);
    } catch (e) { toast(e.message, 'err'); busy(btn, false); }
    refreshState();
  };
  $('#dt-import').onclick = async function () {
    var folder = prompt('Grafana folder (empty = General):', '');
    if (folder === null) return;
    var btn = this; busy(btn, true);
    try {
      var job = await runJob('/api/grafana/import',
        { slugs: [slug], folder: folder, overwrite: true },
        $('#dt-log'));
      var r = (job.result.results || [])[0] || {};
      if (r.status === 'ok') {
        toast('Imported' + (r.url ? ': ' + r.url : ''), 'ok');
      } else { toast(r.error || 'import failed', 'err'); }
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false); refreshState();
  };

  /* expand/collapse + editor actions (event delegation) */
  $all('tr[data-exp]', view).forEach(function (tr) {
    tr.onclick = function (ev) {
      if (ev.target.closest('button, textarea, input, select, a'))
        return;
      var key = slug + ':' + tr.getAttribute('data-exp');
      App.expanded[key] = !App.expanded[key];
      vDetail(view, slug);
    };
  });
  bindEditors(view, slug);
}

function panelDetailHtml(slug, w, panel, pt) {
  var html = '';
  (w.nrql || []).forEach(function (q) {
    html += '<div class="kv"><b>NRQL</b></div><pre>' +
      esc(q.query || q) + '</pre>';
  });
  (w.notes || []).forEach(function (n) {
    html += '<div class="kv">note: ' + esc(n) + '</div>';
  });
  var targets = (panel && panel.targets) || [];
  if (!targets.length) {
    html += '<div class="kv" style="margin-top:8px">This panel has ' +
      'no query targets (text/placeholder panel)';
    if (w.fallback) html += ' - fallback: ' + esc(w.fallback);
    html += '.</div>';
    return html;
  }
  targets.forEach(function (t) {
    var ref = t.refId || 'A';
    var tr = pt[ref];
    var dsType = (t.datasource && t.datasource.type) || '';
    var eid = 'ed-' + w.panel_id + '-' + ref;
    html += '<div style="margin-top:12px">' +
      '<div class="row"><span class="chip dim">' + esc(ref) +
      '</span><span class="chip info">' + esc(dsType || 'unknown ds') +
      '</span>' + testChip(tr && tr.status) + '</div>' +
      (tr && tr.error ? '<div class="err-text">' + esc(tr.error) +
        '</div>' : '') +
      '<label>Query</label>' +
      '<textarea id="' + eid + '" data-orig="1">' +
      esc(t.expr || '') + '</textarea>' +
      '<div class="btnbar">' +
      btnA('test', w.panel_id, ref, 'Test') +
      btnA('ai', w.panel_id, ref, 'Ask AI') +
      btnA('save', w.panel_id, ref, 'Save') +
      btnA('push', w.panel_id, ref, 'Save &amp; Push', 'primary') +
      '</div>' +
      '<div id="tres-' + w.panel_id + '-' + ref + '"></div>' +
      '<div id="ai-' + w.panel_id + '-' + ref + '"></div>' +
      '</div>';
  });
  return html;
}

function btnA(act, pid, ref, label, extra) {
  return '<button class="btn small ' + (extra || '') +
    '" data-act="' + act + '" data-pid="' + esc(pid) +
    '" data-ref="' + esc(ref) + '">' + label + '</button>';
}

function bindEditors(view, slug) {
  $all('button[data-act]', view).forEach(function (btn) {
    btn.onclick = function (ev) {
      ev.stopPropagation();
      editorAction(view, slug, btn);
    };
  });
}

async function editorAction(view, slug, btn) {
  var act = btn.getAttribute('data-act');
  var pidRaw = btn.getAttribute('data-pid');
  var pid = /^\d+$/.test(pidRaw) ? parseInt(pidRaw, 10) : pidRaw;
  var ref = btn.getAttribute('data-ref');
  var ta = $('#ed-' + pidRaw + '-' + ref, view);
  var expr = ta ? ta.value : '';
  var tres = $('#tres-' + pidRaw + '-' + ref, view);
  busy(btn, true);
  try {
    if (act === 'test') {
      var r = await api('/api/panel/test',
        { slug: slug, panel_id: pid, refId: ref, expr: expr });
      var res = (r.results || [])[0] || {};
      tres.innerHTML = '<div style="margin-top:6px">' +
        testChip(res.status) +
        (res.frames != null ? ' <span class="kv">' + res.frames +
          ' frames / ' + (res.points || 0) + ' points</span>' : '') +
        (res.error ? '<div class="err-text">' + esc(res.error) +
          '</div>' : '') + '</div>';
    } else if (act === 'ai') {
      var box = $('#ai-' + pidRaw + '-' + ref, view);
      box.innerHTML = '<div class="ai-box">Asking Claude...</div>';
      var a = await api('/api/ai/suggest',
        { slug: slug, panel_id: pid, refId: ref, expr: expr });
      var fixed = a.fixed_expr || '';
      box.innerHTML = '<div class="ai-box">' +
        '<span class="chip ' + (a.confidence === 'high' ? 'ok' :
          a.confidence === 'low' ? 'warn' : 'info') +
        ' conf">' + esc(a.confidence || 'suggestion') + '</span>' +
        '<div>' + esc(a.explanation || '') + '</div>' +
        (fixed ? '<label>Suggested query</label><pre>' + esc(fixed) +
          '</pre><button class="btn small primary" id="apply-' +
          pidRaw + '-' + ref + '">Apply suggestion</button>' : '') +
        ((a.actions || []).length ? '<ul class="kv">' +
          a.actions.map(function (x) {
            return '<li>' + esc(x) + '</li>'; }).join('') +
          '</ul>' : '') +
        '</div>';
      if (fixed) {
        $('#apply-' + pidRaw + '-' + ref, view).onclick =
          function (ev) {
            ev.stopPropagation();
            ta.value = fixed;
            toast('Suggestion applied to the editor - Test then ' +
                  'Save', 'ok');
          };
      }
    } else if (act === 'save' || act === 'push') {
      var why = prompt('Why this change? (recorded in the change ' +
                       'log)', '') || '';
      var body = { slug: slug, panel_id: pid, refId: ref,
                   expr: expr, why: why, retest: false,
                   push: act === 'push' };
      var out = await api('/api/panel/update', body);
      toast(act === 'push' ?
            'Saved and pushed to Grafana' : 'Saved', 'ok');
      if (out.test) { /* not requested, ignore */ }
    }
  } catch (e) {
    toast(e.message, 'err');
  }
  busy(btn, false);
}

/* ====================================================== CONVERT */
async function vConvert(view) {
  crumb('Convert & Package');
  var ses = (App.state || {}).session || {};
  view.innerHTML =
  '<h1>Convert &amp; Package</h1>' +
  '<p class="lead">Fetch dashboards from New Relic, then convert ' +
  'them into Grafana dashboards with requirements analysis and ' +
  'per-dashboard packages.</p>' +
  '<div class="grid2">' +
  '<div class="card"><h2>1. Fetch from New Relic</h2>' +
  '<label>Write NR JSON exports to</label>' +
  '<input id="cv-fetchdir" value="' + esc(ses.input_dir || '') +
  '">' +
  '<label>Dashboard GUIDs (optional, comma separated - empty = ' +
  'all)</label>' +
  '<input id="cv-guids" placeholder="all dashboards">' +
  '<div class="btnbar"><button class="btn" id="cv-fetch">Fetch' +
  '</button></div></div>' +
  '<div class="card"><h2>2. Convert &amp; package</h2>' +
  '<label>Input directory (NR JSON)</label>' +
  '<input id="cv-indir" value="' + esc(ses.input_dir || '') + '">' +
  '<label>Output directory</label>' +
  '<input id="cv-outdir" value="' + esc(ses.out_dir || '') + '">' +
  '<label>Mapping config (optional)</label>' +
  '<input id="cv-cfg" value="' + esc(ses.config_path || '') +
  '" placeholder="config/mappings.json">' +
  '<div class="row" style="margin-top:10px">' +
  '<input type="checkbox" id="cv-pkg" checked style="width:auto">' +
  '<span>Package (requirements.json, README, test.sh per ' +
  'dashboard)</span></div>' +
  '<div class="btnbar"><button class="btn primary" id="cv-run">' +
  'Run convert</button></div></div>' +
  '</div>' +
  '<div class="log" id="cv-log"></div>' +
  '<div id="cv-result"></div>';

  $('#cv-fetch').onclick = async function () {
    var btn = this; busy(btn, true);
    var guids = $('#cv-guids').value.split(',').map(function (s) {
      return s.trim(); }).filter(Boolean);
    try {
      var job = await runJob('/api/nr/fetch',
        { out: $('#cv-fetchdir').value.trim(), guids: guids },
        $('#cv-log'));
      toast('Fetched ' + (job.result.written || []).length +
            ' dashboards', 'ok');
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false); refreshState();
  };

  $('#cv-run').onclick = async function () {
    var btn = this; busy(btn, true);
    $('#cv-result').innerHTML = '';
    try {
      var job = await runJob('/api/convert', {
        input_dir: $('#cv-indir').value.trim(),
        out_dir: $('#cv-outdir').value.trim(),
        config_path: $('#cv-cfg').value.trim(),
        package: $('#cv-pkg').checked
      }, $('#cv-log'));
      renderConvertResult(job.result);
      toast('Converted ' + (job.result.dashboards || []).length +
            ' dashboard(s)', 'ok');
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false); refreshState();
  };
}

function renderConvertResult(res) {
  var list = (res.dashboards || []).map(function (d) {
    return '<tr class="click" data-slug="' + esc(d.slug) + '">' +
      '<td><b>' + esc(d.title) + '</b></td><td>' + d.panels +
      '</td><td>' + confChips(d.confidence) + '</td><td>' +
      dsChips(d.datasources) + '</td></tr>';
  }).join('');
  var failed = (res.failed || []).map(function (f) {
    return '<div class="err-text">' + esc(f.source) + ': ' +
      esc(f.error) + '</div>';
  }).join('');
  var totalPanels = (res.dashboards || []).reduce(function (a, d) {
    return a + (d.panels || 0); }, 0);
  var review = (res.dashboards || []).reduce(function (a, d) {
    var c = d.confidence || {};
    return a + (c['needs-review'] || 0) + (c.untranslatable || 0);
  }, 0);
  $('#cv-result').innerHTML =
    '<div class="cards-row">' +
    '<div class="stat-card"><div class="num">' +
      (res.dashboards || []).length +
      '</div><div class="lbl">dashboards</div></div>' +
    '<div class="stat-card"><div class="num">' + totalPanels +
      '</div><div class="lbl">panels</div></div>' +
    '<div class="stat-card"><div class="num">' + review +
      '</div><div class="lbl">need review</div></div>' +
    '<div class="stat-card"><div class="num">' +
      (res.failed || []).length +
      '</div><div class="lbl">failed inputs</div></div></div>' +
    (list ? '<div class="card"><div class="tablewrap"><table>' +
      '<thead><tr><th>Dashboard</th><th>Panels</th>' +
      '<th>Confidence</th><th>Datasources</th></tr></thead><tbody>' +
      list + '</tbody></table></div></div>' : '') +
    (failed ? '<div class="card"><h2>Failed inputs</h2>' + failed +
      '</div>' : '');
  $all('#cv-result tr.click').forEach(function (tr) {
    tr.onclick = function () {
      location.hash = '#/dashboards/' +
        encodeURIComponent(tr.getAttribute('data-slug'));
    };
  });
}

/* ====================================================== TEST */
async function vTest(view) {
  crumb('Validate & Test');
  var data = await api('/api/dashboards');
  App.dashboards = data.dashboards || [];
  var opts = App.dashboards.map(function (d) {
    return '<option value="' + esc(d.slug) + '">' + esc(d.title) +
      '</option>';
  }).join('');
  view.innerHTML =
    '<h1>Validate &amp; Test</h1>' +
    '<p class="lead">Check that the target Grafana has the ' +
    'required datasources, then run every panel query against ' +
    'live data.</p>' +
    (opts ? '<div class="card"><div class="row">' +
      '<select id="vt-slug" style="min-width:280px">' + opts +
      '</select>' +
      '<button class="btn" id="vt-check">Check requirements' +
      '</button>' +
      '<button class="btn primary" id="vt-test">Run data tests' +
      '</button></div></div>' +
      '<div class="log" id="vt-log"></div><div id="vt-out"></div>' :
      '<div class="empty"><b>Nothing to test yet.</b><br>Convert ' +
      'dashboards first on the <a href="#/convert">Convert</a> ' +
      'page.</div>');
  if (!opts) return;

  $('#vt-check').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      var r = await api('/api/grafana/check',
        { slug: $('#vt-slug').value });
      var rows = (r.items || []).map(function (it) {
        var cls = it.status === 'ok' ? 'ok' :
          (it.status === 'missing' ? 'err' : 'warn');
        return '<tr><td>' + esc(it.item) + '</td><td>' +
          '<span class="chip ' + cls + '">' + esc(it.status) +
          '</span></td><td>' + esc(it.detail || '') + '</td><td>' +
          esc(it.fix || '') + '</td></tr>';
      }).join('');
      $('#vt-out').innerHTML = '<div class="card">' +
        '<h2>Requirement check</h2><div class="tablewrap"><table>' +
        '<thead><tr><th>Item</th><th>Status</th><th>Detail</th>' +
        '<th>Fix</th></tr></thead><tbody>' +
        (rows || '<tr><td colspan="4" class="kv">nothing to check' +
         '</td></tr>') + '</tbody></table></div></div>';
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false); refreshState();
  };

  $('#vt-test').onclick = async function () {
    var btn = this; busy(btn, true);
    var slug = $('#vt-slug').value;
    try {
      var job = await runJob('/api/grafana/test', { slug: slug },
                             $('#vt-log'));
      var res = job.result;
      var rows = (res.results || []).map(function (r) {
        return '<tr><td>' + esc(r.panel_id) + '</td><td>' +
          esc(r.panel_title || '') + ' <span class="chip dim">' +
          esc(r.refId || '') + '</span></td><td class="mono">' +
          esc(r.datasource || '') + '</td><td>' +
          testChip(r.status) + '</td><td>' +
          (r.error ? '<span class="err-text">' + esc(r.error) +
            '</span>' : (r.frames != null ?
            r.frames + ' frames' : '')) + '</td></tr>';
      }).join('');
      $('#vt-out').innerHTML = '<div class="card"><h2>Data test: ' +
        esc(slug) + '</h2><div class="tablewrap"><table><thead>' +
        '<tr><th>Panel</th><th>Title</th><th>Datasource</th>' +
        '<th>Status</th><th>Detail</th></tr></thead><tbody>' + rows +
        '</tbody></table></div>' +
        '<div class="btnbar"><a class="btn small" ' +
        'href="#/dashboards/' + encodeURIComponent(slug) +
        '">Open dashboard to fix panels &rarr;</a></div></div>';
      var s = res.summary || {};
      toast('Tested: ' + Object.keys(s).map(function (k) {
        return s[k] + ' ' + k; }).join(', '), 'ok');
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false); refreshState();
  };
}

/* ====================================================== IMPORT */
async function vImport(view) {
  crumb('Import');
  var data = await api('/api/dashboards');
  App.dashboards = data.dashboards || [];
  if (!App.dashboards.length) {
    view.innerHTML = '<h1>Import</h1><div class="empty"><b>No ' +
      'dashboards to import.</b><br>Run <a href="#/convert">' +
      'Convert &amp; Package</a> first.</div>';
    return;
  }
  var rows = App.dashboards.map(function (d) {
    return '<div class="checkbox-row">' +
      '<input type="checkbox" class="imp-cb" value="' +
      esc(d.slug) + '" checked>' +
      '<b>' + esc(d.title) + '</b> <span class="kv">(' +
      (d.panels || 0) + ' panels)</span>' +
      '<span style="margin-left:auto" id="imp-res-' + esc(d.slug) +
      '"></span></div>';
  }).join('');
  view.innerHTML =
    '<h1>Import</h1><p class="lead">Bulk-import converted ' +
    'dashboards into the connected Grafana instance.</p>' +
    '<div class="card">' + rows +
    '<div class="btnbar" style="margin-top:14px">' +
    '<input id="imp-folder" placeholder="Folder (empty = General)"' +
    ' style="max-width:260px">' +
    '<span class="row"><input type="checkbox" id="imp-ow" checked ' +
    'style="width:auto"> overwrite</span>' +
    '<button class="btn primary" id="imp-run">Import selected' +
    '</button></div>' +
    '<div class="log" id="imp-log"></div></div>';

  $('#imp-run').onclick = async function () {
    var btn = this;
    var slugs = $all('.imp-cb').filter(function (c) {
      return c.checked; }).map(function (c) { return c.value; });
    if (!slugs.length) { toast('Nothing selected', 'err'); return; }
    busy(btn, true);
    try {
      var job = await runJob('/api/grafana/import', {
        slugs: slugs, folder: $('#imp-folder').value.trim(),
        overwrite: $('#imp-ow').checked
      }, $('#imp-log'));
      (job.result.results || []).forEach(function (r) {
        var el = $('#imp-res-' + CSS.escape(r.slug));
        if (!el) return;
        el.innerHTML = r.status === 'ok' ?
          '<span class="chip ok">imported</span>' +
          (r.url ? ' <a href="' + esc(r.url) +
           '" target="_blank" rel="noopener">open</a>' : '') :
          '<span class="chip err" title="' + esc(r.error) +
          '">failed</span>';
      });
      toast('Imported ' + job.result.ok + '/' + job.result.total,
            job.result.ok === job.result.total ? 'ok' : 'err');
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false); refreshState();
  };
}

/* ====================================================== CHANGES */
async function vChanges(view) {
  crumb('Changes');
  var data = await api('/api/changes');
  var changes = data.changes || [];
  var slugs = {};
  changes.forEach(function (c) { if (c.slug) slugs[c.slug] = 1; });
  var opts = '<option value="">all dashboards</option>' +
    Object.keys(slugs).sort().map(function (s) {
      return '<option>' + esc(s) + '</option>'; }).join('');
  view.innerHTML =
    '<h1>Changes</h1><p class="lead">Every edit made while ' +
    'troubleshooting, ready to be codified back into the mapping ' +
    'config so future conversions are right the first time.</p>' +
    '<div class="card"><div class="row">' +
    '<select id="ch-slug" style="min-width:240px">' + opts +
    '</select>' +
    '<button class="btn" id="ch-suggest">Suggest config</button>' +
    '</div></div>' +
    '<div id="ch-cfg"></div><div id="ch-table"></div>';

  function renderTable(list) {
    if (!list.length) {
      $('#ch-table').innerHTML = '<div class="empty"><b>No changes ' +
        'recorded yet.</b><br>Edits made in the dashboard detail ' +
        'view (Save / Save &amp; Push) land here.</div>';
      return;
    }
    var rows = list.map(function (c) {
      return '<tr><td class="kv">' +
        esc(String(c.ts || '').replace('T', ' ').slice(0, 19)) +
        '</td><td class="mono">' + esc(c.slug || '') + '</td>' +
        '<td><span class="chip dim">' + esc(c.action) + '</span>' +
        '<div class="kv">' + esc(c.target || '') + '</div></td>' +
        '<td><pre style="margin:0">' + esc(shorten(c.before)) +
        '</pre><pre style="margin:4px 0 0">' + esc(shorten(c.after)) +
        '</pre></td><td>' + esc(c.why || '') +
        '<div class="kv">' + esc(c.source || '') + '</div></td>' +
        '</tr>';
    }).join('');
    $('#ch-table').innerHTML = '<div class="card"><div class=' +
      '"tablewrap"><table><thead><tr><th>When</th><th>Dashboard' +
      '</th><th>Action</th><th>Before &rarr; After</th><th>Why' +
      '</th></tr></thead><tbody>' + rows +
      '</tbody></table></div></div>';
  }
  function shorten(v) {
    var s = typeof v === 'string' ? v : JSON.stringify(v);
    return s && s.length > 300 ? s.slice(0, 300) + '...' : (s || '');
  }
  renderTable(changes);

  $('#ch-slug').onchange = async function () {
    var s = this.value;
    var d = await api('/api/changes' +
                      (s ? '?slug=' + encodeURIComponent(s) : ''));
    renderTable(d.changes || []);
  };
  $('#ch-suggest').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      var s = $('#ch-slug').value;
      var cfg = await api('/api/changes/suggest-config' +
                          (s ? '?slug=' + encodeURIComponent(s) : ''));
      var txt = JSON.stringify(cfg, null, 2);
      $('#ch-cfg').innerHTML = '<div class="card"><h2>Suggested ' +
        'config overlay</h2><div class="kv">Merge this into your ' +
        'mapping config (convert -c) to make these fixes ' +
        'permanent.</div><pre id="ch-cfg-pre"></pre>' +
        '<button class="btn small" id="ch-copy">Copy JSON</button>' +
        '</div>';
      $('#ch-cfg-pre').textContent = txt;
      $('#ch-copy').onclick = function () {
        var ok = function () { toast('Copied', 'ok'); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(txt).then(ok, function () {
            fallbackCopy(txt); ok(); });
        } else { fallbackCopy(txt); ok(); }
      };
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false);
  };
}

function fallbackCopy(text) {
  var ta = document.createElement('textarea');
  ta.value = text; document.body.appendChild(ta);
  ta.select(); document.execCommand('copy'); ta.remove();
}

/* ====================================================== AI CHAT */
function mdLite(text) {
  var parts = String(text || '').split('```');
  var out = '';
  parts.forEach(function (p, i) {
    if (i % 2 === 1) {
      var body = p.replace(/^[a-z]*\n/, '');
      out += '<pre>' + esc(body) + '</pre>';
    } else {
      out += esc(p);
    }
  });
  return out;
}

async function vAI(view) {
  crumb('AI Assistant');
  var s = App.state || await api('/api/state');
  var enabled = s.session && s.session.anthropic_key_set;
  var msgs = App.ai.map(function (m) {
    return '<div class="msg ' + m.role + '">' +
      mdLite(m.content) + '</div>';
  }).join('');
  view.innerHTML =
    '<h1>AI Assistant</h1>' +
    '<p class="lead">Ask about failing queries, PromQL/LogQL ' +
    'translation, datasource setup, or anything about this ' +
    'migration.</p>' +
    (!enabled ?
      '<div class="empty"><b>AI is not configured.</b><br>Add an ' +
      'Anthropic API key in <a href="#/setup">Setup</a> to enable ' +
      'the assistant.</div>' :
      '<div class="card"><div id="chatlog">' + (msgs ||
        '<div class="kv">Try: &quot;Why would ' +
        'http_server_request_duration_seconds_bucket return no ' +
        'data?&quot;</div>') + '</div>' +
      '<div class="row" style="margin-top:10px">' +
      '<textarea id="ai-input" style="flex:1;min-height:44px" ' +
      'placeholder="Ask the assistant..."></textarea>' +
      '<button class="btn primary" id="ai-send">Send</button>' +
      '</div></div>');
  if (!enabled) return;
  var logEl = $('#chatlog');
  logEl.scrollTop = logEl.scrollHeight;
  async function send() {
    var input = $('#ai-input');
    var text = input.value.trim();
    if (!text || App.aiBusy) return;
    App.ai.push({ role: 'user', content: text });
    input.value = '';
    App.aiBusy = true;
    vAI(view);
    try {
      var r = await api('/api/ai/chat', { messages: App.ai });
      App.ai.push({ role: 'assistant', content: r.reply || '' });
    } catch (e) {
      App.ai.push({ role: 'assistant',
                    content: 'Error: ' + e.message });
      toast(e.message, 'err');
    }
    App.aiBusy = false;
    vAI(view);
    refreshState();
  }
  $('#ai-send').onclick = send;
  $('#ai-input').onkeydown = function (ev) {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault(); send();
    }
  };
}

/* ====================================================== theme */
function applyTheme(mode) {
  if (mode === 'dark' || mode === 'light') {
    document.documentElement.setAttribute('data-theme', mode);
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
  $('#themelbl').textContent =
    mode === 'dark' ? 'Dark' : mode === 'light' ? 'Light' : 'Auto';
}

$('#themebtn').onclick = function () {
  var cur = localStorage.getItem('nr2g-theme') || 'auto';
  var next = cur === 'auto' ? 'dark' :
             cur === 'dark' ? 'light' : 'auto';
  localStorage.setItem('nr2g-theme', next);
  applyTheme(next);
};

/* ====================================================== boot */
applyTheme(localStorage.getItem('nr2g-theme') || 'auto');
window.addEventListener('hashchange', route);
(async function boot() {
  await refreshState();
  setInterval(refreshState, 8000);
  route();
})();
</script>
</body>
</html>
"""
