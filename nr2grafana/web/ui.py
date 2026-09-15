"""Embedded single-page UI for the nr2grafana web app (1.2 redesign).

One module-level HTML string (``PAGE``) with inline CSS and JS -- no
external assets, fonts, or CDNs. Served by web/server.py at ``GET /``.

Layout of the embedded app (all vanilla JS):

* design system  -- CSS custom properties, dark default + light theme
  (``prefers-color-scheme`` plus a persistent toggle), 8px spacing
  grid, one badge/chip/button/input vocabulary reused everywhere.
* state object   -- ``App`` (session state, route, caches, job list).
* api() helper   -- fetch wrapper; every server string is escaped with
  ``esc()`` before being injected into HTML.
* hash router    -- ``#/overview``, ``#/connect``, ``#/convert``,
  ``#/datasources``, ``#/dash/<slug>[/tab]``, ``#/import``,
  ``#/changes``, ``#/ai`` (legacy 1.1 hashes are aliased).
* per-view render functions plus small component helpers: ``chip()``,
  ``ring()`` (SVG readiness ring), ``sparkline()`` (inline SVG
  polyline), ``statStrip()``, ``stepper()``, ``ico()`` (inline SVG
  icon set), ``errorCard()`` (the one reusable error component:
  what failed / exact detail / next action), ``consoleHtml()`` +
  ``consoleUpdate()`` (structured job-log console with level-tinted
  lines, timestamps, follow pin and copy), ``jsonDetails()``
  (collapsible pretty-printed JSON with copy) and ``truncHtml()``
  (long strings truncate with an expand toggle).
* AI backend picker in Connect -- "Anthropic API" or "Local console
  AI" (command template, POST /api/ai/test probe); the header AI
  pill reflects the active backend from /api/state.
* global job drawer -- long operations (fetch/convert/test/parity/
  diagnose/heal/import) poll ``/api/jobs/<id>`` and stream their logs
  into a drawer that survives navigation, plus toasts.
"""

from __future__ import annotations

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nr2grafana</title>
<style>
/* =================================================== design tokens */
:root {
  /* layered dark surfaces: page -> chrome -> card -> raised -> active */
  --bg: #0b0f16;
  --bg2: #121824;
  --bg3: #1a2231;
  --bg4: #222c3e;
  --border: #2a3547;
  --border-soft: #212a3a;
  --zebra: rgba(255,255,255,.022);
  --text: #e3e9f2;
  --muted: #939eb1;
  --faint: #5f6a7d;
  --accent: #549bff;
  --accent-dim: rgba(84,155,255,.16);
  --accent-soft: rgba(84,155,255,.09);
  --green: #3fc873;
  --green-bg: rgba(63,200,115,.13);
  --amber: #e8ab44;
  --amber-bg: rgba(232,171,68,.13);
  --red: #f0716f;
  --red-bg: rgba(240,113,111,.12);
  --blue: #63a9ec;
  --blue-bg: rgba(99,169,236,.13);
  --purple: #ab90fa;
  --purple-bg: rgba(171,144,250,.13);
  --console-bg: #0a0d13;
  --console-fg: #c9d2de;
  --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.28);
  --shadow-sm: 0 1px 2px rgba(0,0,0,.28);
  --shadow-card: 0 1px 0 rgba(255,255,255,.02) inset,
                 0 1px 3px rgba(0,0,0,.25);
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Helvetica, Arial, sans-serif;
  --r-sm: 6px; --r-md: 8px; --r-lg: 12px;
  --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 24px;
  --s6: 32px;
  --fs-xs: 11px; --fs-sm: 12px; --fs-md: 13px; --fs-lg: 14px;
  --fs-xl: 16px; --fs-h1: 20px;
  --t-fast: .13s ease;
}
[data-theme="light"] {
  --bg: #f4f6f9;
  --bg2: #ffffff;
  --bg3: #edf1f6;
  --bg4: #e2e8f0;
  --border: #d5dce6;
  --border-soft: #e4e9f0;
  --zebra: rgba(15,30,60,.024);
  --text: #1a2330;
  --muted: #5a6879;
  --faint: #97a3b4;
  --accent: #1665cf;
  --accent-dim: rgba(22,101,207,.14);
  --accent-soft: rgba(22,101,207,.07);
  --green: #148544;
  --green-bg: rgba(20,133,68,.11);
  --amber: #96620a;
  --amber-bg: rgba(150,98,10,.12);
  --red: #c03434;
  --red-bg: rgba(192,52,52,.09);
  --blue: #1f639f;
  --blue-bg: rgba(31,99,159,.10);
  --purple: #6947d0;
  --purple-bg: rgba(105,71,208,.10);
  --console-bg: #171c26;
  --console-fg: #cfd7e2;
  --shadow: 0 1px 2px rgba(25,35,55,.08), 0 8px 24px rgba(25,35,55,.09);
  --shadow-sm: 0 1px 2px rgba(25,35,55,.07);
  --shadow-card: 0 1px 3px rgba(25,35,55,.06);
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --bg: #f4f6f9;
    --bg2: #ffffff;
    --bg3: #edf1f6;
    --bg4: #e2e8f0;
    --border: #d5dce6;
    --border-soft: #e4e9f0;
    --zebra: rgba(15,30,60,.024);
    --text: #1a2330;
    --muted: #5a6879;
    --faint: #97a3b4;
    --accent: #1665cf;
    --accent-dim: rgba(22,101,207,.14);
    --accent-soft: rgba(22,101,207,.07);
    --green: #148544;
    --green-bg: rgba(20,133,68,.11);
    --amber: #96620a;
    --amber-bg: rgba(150,98,10,.12);
    --red: #c03434;
    --red-bg: rgba(192,52,52,.09);
    --blue: #1f639f;
    --blue-bg: rgba(31,99,159,.10);
    --purple: #6947d0;
    --purple-bg: rgba(105,71,208,.10);
    --console-bg: #171c26;
    --console-fg: #cfd7e2;
    --shadow: 0 1px 2px rgba(25,35,55,.08),
              0 8px 24px rgba(25,35,55,.09);
    --shadow-sm: 0 1px 2px rgba(25,35,55,.07);
    --shadow-card: 0 1px 3px rgba(25,35,55,.06);
  }
}
/* ======================================================== base */
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--bg); color: var(--text);
  font: var(--fs-lg)/1.55 var(--sans);
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
svg.i { width: 16px; height: 16px; flex: 0 0 auto;
  vertical-align: -3px; }
:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px;
  border-radius: 4px;
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .01ms !important;
  }
}
#app { display: flex; min-height: 100vh; }

/* ======================================================== sidebar */
#sidebar {
  width: 224px; flex: 0 0 224px; background: var(--bg2);
  border-right: 1px solid var(--border-soft);
  padding: var(--s4) var(--s3); position: sticky; top: 0;
  height: 100vh; display: flex; flex-direction: column;
}
.brand { display: flex; align-items: center; gap: 10px;
  padding: 2px var(--s2) var(--s4); }
.brand-mark {
  width: 30px; height: 30px; border-radius: 8px; flex: 0 0 30px;
  background: linear-gradient(135deg, #1ce783 0%, #f46800 100%);
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 800; font-size: 13px;
  box-shadow: var(--shadow-sm);
}
.brand-name { font-weight: 700; font-size: 15px;
  letter-spacing: .2px; }
.brand-sub { font-size: var(--fs-xs); color: var(--muted); }
.nav-sec { font-size: 10px; font-weight: 700; letter-spacing: .1em;
  text-transform: uppercase; color: var(--faint);
  padding: var(--s3) var(--s2) var(--s1); }
.nav a {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 10px; border-radius: var(--r-sm); color: var(--text);
  font-weight: 500; margin-bottom: 2px; font-size: var(--fs-md);
  transition: background var(--t-fast), color var(--t-fast);
}
.nav a:hover { background: var(--bg3); text-decoration: none; }
.nav a.active { background: var(--accent-dim); color: var(--accent);
  font-weight: 600; }
.nav .ico { width: 18px; display: inline-flex;
  align-items: center; justify-content: center; opacity: .75; }
.nav a.active .ico, .nav a:hover .ico { opacity: 1; }
.nav .cnt { margin-left: auto; font-size: var(--fs-xs);
  color: var(--muted); background: var(--bg3); border-radius: 999px;
  padding: 0 7px; font-variant-numeric: tabular-nums; }
.nav a.active .cnt { background: transparent; color: var(--accent); }
.sidebar-foot { margin-top: auto; padding: var(--s3) var(--s2) 0;
  font-size: var(--fs-xs); color: var(--faint); line-height: 1.5; }

/* ======================================================== topbar */
#mainwrap { flex: 1; min-width: 0; display: flex;
  flex-direction: column; }
#topbar {
  position: sticky; top: 0; z-index: 30;
  display: flex; align-items: center; gap: var(--s2);
  padding: var(--s2) var(--s5); background: var(--bg2);
  border-bottom: 1px solid var(--border-soft); min-height: 52px;
}
#crumb { font-weight: 600; font-size: var(--fs-lg); flex: 1;
  min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--border); border-radius: 999px;
  padding: 3px 10px; font-size: var(--fs-sm); color: var(--muted);
  background: var(--bg); white-space: nowrap; cursor: default;
  transition: border-color var(--t-fast), color var(--t-fast);
}
.pill .dot { width: 7px; height: 7px; border-radius: 50%;
  background: var(--faint); flex: 0 0 7px;
  transition: background var(--t-fast); }
.pill.ok { color: var(--green); border-color: var(--green);
  background: var(--green-bg); }
.pill.ok .dot { background: var(--green); }
.pill.err { color: var(--red); border-color: var(--red);
  background: var(--red-bg); }
.pill.err .dot { background: var(--red); }
button.pill { font: inherit; font-size: var(--fs-sm);
  cursor: pointer; }
button.pill:hover { border-color: var(--accent);
  color: var(--accent); }
#jobsbtn .spin { display: none; }
#jobsbtn.running .spin { display: inline-block; width: 10px;
  height: 10px; border: 2px solid var(--accent);
  border-top-color: transparent; border-radius: 50%;
  animation: spin .8s linear infinite; }

/* ======================================================== main */
main { padding: var(--s5); max-width: 1240px; width: 100%;
  margin: 0 auto; }
h1 { font-size: var(--fs-h1); margin: 0 0 var(--s1);
  letter-spacing: -.01em; }
h2 { font-size: var(--fs-lg); margin: 0 0 var(--s3);
  font-weight: 650; display: flex; align-items: center; gap: 8px; }
h2 svg.i { color: var(--muted); }
h3 { margin: 0 0 var(--s2); font-weight: 700; color: var(--muted);
  text-transform: uppercase; letter-spacing: .06em;
  font-size: var(--fs-xs); }
.lead { color: var(--muted); margin: 0 0 var(--s4);
  font-size: var(--fs-md); }
.card {
  background: var(--bg2); border: 1px solid var(--border-soft);
  border-radius: var(--r-lg); padding: var(--s4);
  box-shadow: var(--shadow-card); margin-bottom: var(--s4);
}
.grid2 { display: grid; grid-template-columns: 1fr 1fr;
  gap: var(--s4); align-items: start; }
@media (max-width: 960px) { .grid2 { grid-template-columns: 1fr; } }
.cards-row { display: flex; gap: var(--s3); flex-wrap: wrap;
  margin-bottom: var(--s4); }
.stat-card { background: var(--bg2);
  border: 1px solid var(--border-soft);
  border-radius: var(--r-lg); padding: var(--s3) var(--s4);
  min-width: 128px; box-shadow: var(--shadow-card); }
.stat-card .num { font-size: 22px; font-weight: 700;
  font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
.stat-card .lbl { font-size: var(--fs-sm); color: var(--muted); }

/* ======================================================== forms */
label { display: block; font-size: var(--fs-sm); font-weight: 600;
  color: var(--muted); margin: var(--s3) 0 var(--s1); }
label .req { color: var(--red); }
input, select, textarea {
  width: 100%; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: var(--r-sm);
  padding: 7px 10px; font: inherit; font-size: var(--fs-md);
  outline: none;
  transition: border-color var(--t-fast), box-shadow var(--t-fast);
}
textarea { font-family: var(--mono); font-size: 12.5px;
  min-height: 72px; resize: vertical; }
input:focus, select:focus, textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-dim); }
input::placeholder, textarea::placeholder { color: var(--faint); }
input[type="checkbox"] { width: auto; accent-color: var(--accent); }
.row { display: flex; gap: var(--s2); align-items: center;
  flex-wrap: wrap; }
.row > * { width: auto; }
.btnbar { margin-top: var(--s3); display: flex; gap: var(--s2);
  flex-wrap: wrap; align-items: center; }
.field-help { font-size: var(--fs-xs); color: var(--faint);
  margin-top: 3px; line-height: 1.4; }

/* segmented control (AI backend picker etc.) */
.seg { display: inline-flex; background: var(--bg);
  border: 1px solid var(--border); border-radius: var(--r-md);
  padding: 2px; gap: 2px; }
.seg button { border: 0; background: transparent;
  color: var(--muted); font: inherit; font-size: var(--fs-md);
  font-weight: 600; padding: 5px 12px; border-radius: var(--r-sm);
  cursor: pointer; display: inline-flex; align-items: center;
  gap: 6px;
  transition: background var(--t-fast), color var(--t-fast); }
.seg button:hover { color: var(--text); }
.seg button.on { background: var(--bg3); color: var(--text);
  box-shadow: var(--shadow-sm); }

/* ======================================================== buttons */
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--bg3); color: var(--text);
  border: 1px solid var(--border); border-radius: var(--r-sm);
  padding: 6px 14px; font: inherit; font-size: var(--fs-md);
  font-weight: 600; cursor: pointer; white-space: nowrap;
  transition: border-color var(--t-fast), background var(--t-fast),
              color var(--t-fast), box-shadow var(--t-fast);
}
.btn svg.i { width: 14px; height: 14px; }
.btn:hover { border-color: var(--accent); color: var(--accent);
  text-decoration: none; }
.btn:active { transform: translateY(.5px); }
.btn.primary { background: var(--accent); border-color: var(--accent);
  color: #fff; box-shadow: var(--shadow-sm); }
.btn.primary:hover { filter: brightness(1.12); color: #fff; }
.btn.danger { color: var(--red); border-color: var(--red);
  background: transparent; }
.btn.danger:hover { background: var(--red-bg); }
.btn.ghost { background: transparent; border-color: transparent;
  color: var(--muted); }
.btn.ghost:hover { color: var(--accent); background: var(--bg3); }
.btn.small { padding: 3px 10px; font-size: var(--fs-sm); }
.btn.iconbtn { padding: 3px 7px; }
.btn:disabled { opacity: .45; cursor: default;
  pointer-events: none; }
.btn.busy::after { content: ""; width: 11px; height: 11px;
  border: 2px solid currentColor; border-top-color: transparent;
  border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
a.btn.armed { background: var(--green); border-color: var(--green);
  color: #fff; }
a.btn.armed:hover { filter: brightness(1.1); color: #fff; }
.linklike { background: none; border: 0; padding: 0;
  color: var(--accent); font: inherit; font-size: inherit;
  cursor: pointer; }
.linklike:hover { text-decoration: underline; }

/* ======================================================== tables */
table { width: 100%; border-collapse: collapse;
  font-size: var(--fs-md); }
.tablewrap { overflow-x: auto; }
.tablewrap.tall { max-height: 440px; overflow-y: auto; }
th { text-align: left; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .07em; color: var(--muted); font-weight: 650;
  padding: var(--s2) 10px; border-bottom: 1px solid var(--border);
  position: sticky; top: 0; background: var(--bg2); z-index: 2; }
td { padding: 9px 10px; border-bottom: 1px solid var(--border-soft);
  vertical-align: top; }
td.num, th.num { text-align: right;
  font-variant-numeric: tabular-nums; }
table.zebra tbody tr:nth-child(even) td { background: var(--zebra); }
tbody tr:last-child td { border-bottom: 0; }
tr.click { cursor: pointer; }
tr.click:hover td, tr.click:focus-visible td {
  background: var(--bg3); }
tr.expand-row td { background: var(--bg);
  padding: var(--s4); border-bottom: 1px solid var(--border); }

/* ======================================================== chips */
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  border-radius: 5px; padding: 1px 8px;
  font-size: 11.5px; font-weight: 600; margin: 1px 3px 1px 0;
  border: 1px solid transparent; white-space: nowrap;
  vertical-align: middle;
}
.chip svg.i { width: 11px; height: 11px; }
.chip.ok    { color: var(--green);  background: var(--green-bg); }
.chip.warn  { color: var(--amber);  background: var(--amber-bg); }
.chip.err   { color: var(--red);    background: var(--red-bg); }
.chip.info  { color: var(--blue);   background: var(--blue-bg); }
.chip.purple{ color: var(--purple); background: var(--purple-bg); }
.chip.dim   { color: var(--muted);  background: var(--bg3); }
.chip.lg { font-size: var(--fs-sm); padding: 2px 10px; }

pre, code, .mono { font-family: var(--mono); font-size: 12.5px; }
pre {
  background: var(--bg); border: 1px solid var(--border-soft);
  border-radius: var(--r-sm); padding: 10px 12px; overflow-x: auto;
  margin: var(--s2) 0; white-space: pre-wrap; word-break: break-word;
}

/* =================================================== console panel */
.console {
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--console-bg); margin-top: var(--s3);
  display: none; overflow: hidden;
}
.console.show { display: block; }
.console-bar { display: flex; align-items: center; gap: var(--s2);
  padding: 4px 6px 4px 12px;
  border-bottom: 1px solid rgba(255,255,255,.07);
  background: rgba(255,255,255,.025); }
.console-bar .ct { font-size: 10.5px; font-weight: 700;
  letter-spacing: .08em; text-transform: uppercase;
  color: var(--faint); display: inline-flex; align-items: center;
  gap: 6px; }
.console-bar .cn { font-size: var(--fs-xs); color: var(--faint);
  font-variant-numeric: tabular-nums; margin-left: auto; }
.console-bar .btn { background: transparent; border-color:
  transparent; color: var(--faint); }
.console-bar .btn:hover { color: var(--accent); }
.console-bar .btn.on { color: var(--accent); }
.console-body { font-family: var(--mono); font-size: var(--fs-sm);
  color: var(--console-fg); max-height: 280px; overflow-y: auto;
  padding: 8px 12px; line-height: 1.65; }
.cline { white-space: pre-wrap; word-break: break-word; }
.cline .cts { color: var(--faint); margin-right: 10px;
  font-size: var(--fs-xs); user-select: none; }
.cline.err  { color: var(--red); }
.cline.warn { color: var(--amber); }
.cline.ok   { color: var(--green); }

/* ==================================================== error card */
.ecard { border: 1px solid var(--border);
  border-left: 3px solid var(--red); border-radius: var(--r-md);
  background: var(--bg2); padding: var(--s3) var(--s4);
  margin: var(--s2) 0; box-shadow: var(--shadow-sm); }
.ecard.warn { border-left-color: var(--amber); }
.ecard-head { display: flex; align-items: flex-start; gap: 8px;
  font-weight: 600; font-size: var(--fs-md); }
.ecard-head svg.i { color: var(--red); margin-top: 2px; }
.ecard.warn .ecard-head svg.i { color: var(--amber); }
.ecard-detail { margin-top: 6px; font-family: var(--mono);
  font-size: var(--fs-sm); color: var(--muted);
  white-space: pre-wrap; word-break: break-word; }
.ecard-detail details summary { cursor: pointer;
  color: var(--accent); font-family: var(--sans);
  font-size: var(--fs-sm); }
.ecard-act { margin-top: var(--s2); }

/* ================================================== json details */
details.jd { border: 1px solid var(--border-soft);
  border-radius: var(--r-md); background: var(--bg);
  margin: var(--s2) 0; }
details.jd > summary { cursor: pointer; list-style: none;
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; font-size: var(--fs-sm); font-weight: 600;
  color: var(--muted); user-select: none; }
details.jd > summary::-webkit-details-marker { display: none; }
details.jd > summary:hover { color: var(--text); }
details.jd > summary .chev { transition: transform var(--t-fast);
  display: inline-flex; }
details.jd[open] > summary .chev { transform: rotate(90deg); }
details.jd > summary .btn { margin-left: auto; }
details.jd pre { margin: 0; border: 0;
  border-top: 1px solid var(--border-soft); border-radius: 0;
  max-height: 340px; overflow: auto; }

.trunc-rest[hidden] { display: none; }

.empty {
  border: 1px dashed var(--border); border-radius: var(--r-lg);
  padding: var(--s6) var(--s4); text-align: center;
  color: var(--muted); font-size: var(--fs-md);
}
.empty b { color: var(--text); }
.empty .eico { display: flex; justify-content: center;
  margin-bottom: var(--s2); color: var(--faint); }
.empty .eico svg.i { width: 26px; height: 26px; }
.helper { font-size: var(--fs-sm); color: var(--muted);
  margin-top: 6px; }
.kv { font-size: var(--fs-sm); color: var(--muted); }
.kv b { color: var(--text); }
.err-text { color: var(--red); font-family: var(--mono);
  font-size: var(--fs-sm); white-space: pre-wrap;
  word-break: break-word; margin: var(--s1) 0; }
.sec-note { font-size: var(--fs-sm); color: var(--muted);
  border-left: 3px solid var(--accent); padding: 2px 0 2px 10px;
  margin: var(--s3) 0; background: var(--accent-soft);
  border-radius: 0 var(--r-sm) var(--r-sm) 0; }
.ai-box { border: 1px solid var(--border-soft);
  border-radius: var(--r-md);
  padding: var(--s3); background: var(--bg); margin-top: var(--s2); }
.ai-box .conf { float: right; }
.backlink { font-size: var(--fs-sm); display: inline-flex;
  align-items: center; gap: 4px; margin-bottom: var(--s2); }
.checkbox-row { display: flex; gap: var(--s2); align-items: center;
  padding: 6px var(--s1); border-bottom: 1px solid var(--border-soft);
}
.checkbox-row:hover { background: var(--zebra); }
.right { text-align: right; }

/* ======================================================== stepper */
.stepper { display: flex; align-items: flex-start; gap: 0;
  overflow-x: auto; padding: var(--s3) var(--s1);
  margin-bottom: var(--s4); }
.step { display: flex; align-items: center; flex: 1;
  min-width: 84px; }
.step a { display: flex; flex-direction: column; align-items: center;
  gap: 5px; color: var(--muted); font-size: var(--fs-xs);
  font-weight: 600; text-decoration: none; min-width: 68px; }
.step a:hover { color: var(--accent); text-decoration: none; }
.step .bubble { width: 26px; height: 26px; border-radius: 50%;
  border: 2px solid var(--border); background: var(--bg2);
  display: flex; align-items: center; justify-content: center;
  font-size: var(--fs-xs); font-weight: 700; color: var(--muted);
  transition: border-color var(--t-fast), box-shadow var(--t-fast); }
.step.done .bubble { border-color: var(--green);
  color: var(--green); background: var(--green-bg); }
.step.attn .bubble { border-color: var(--amber);
  color: var(--amber); background: var(--amber-bg); }
.step.blocked .bubble { border-color: var(--red);
  color: var(--red); background: var(--red-bg); }
.step.active a { color: var(--text); }
.step.active .bubble { border-color: var(--accent);
  color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.step .bar { flex: 1; height: 2px; background: var(--border);
  margin: 13px 4px 0; min-width: 10px; }
.step.done .bar { background: var(--green); }
.step:last-child .bar { display: none; }

/* ======================================================== rings */
.ring { position: relative; display: inline-flex;
  align-items: center; justify-content: center; flex: 0 0 auto; }
.ring svg { transform: rotate(-90deg); display: block; }
.ring .track { stroke: var(--bg4); fill: none; }
.ring .arc { fill: none; stroke-linecap: round;
  transition: stroke-dasharray .4s; }
.ring.ok .arc { stroke: var(--green); }
.ring.warn .arc { stroke: var(--amber); }
.ring.err .arc { stroke: var(--red); }
.ring.dim .arc { stroke: var(--faint); }
.ring .ring-num { position: absolute; font-weight: 700;
  font-variant-numeric: tabular-nums; }
.ring.ok .ring-num { color: var(--green); }
.ring.warn .ring-num { color: var(--amber); }
.ring.err .ring-num { color: var(--red); }
.ring.dim .ring-num { color: var(--muted); }

/* ======================================================== overview */
.ov-grid { display: grid;
  grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
  gap: var(--s4); }
.ov-card { background: var(--bg2);
  border: 1px solid var(--border-soft);
  border-radius: var(--r-lg); padding: var(--s4);
  box-shadow: var(--shadow-card); display: flex; gap: var(--s4);
  transition: border-color var(--t-fast), box-shadow var(--t-fast); }
.ov-card:hover { border-color: var(--accent);
  box-shadow: var(--shadow); }
.ov-main { flex: 1; min-width: 0; }
.ov-title { font-weight: 650; font-size: var(--fs-lg);
  margin-bottom: 2px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.ov-title a { color: var(--text); }
.ov-sub { font-size: var(--fs-xs); color: var(--faint);
  font-family: var(--mono); margin-bottom: var(--s2);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ov-chips { margin-bottom: var(--s3); min-height: 22px; }

/* ======================================================== spark */
.side-by-side { display: grid; grid-template-columns: 1fr 1fr;
  gap: var(--s3); margin: var(--s2) 0; }
@media (max-width: 860px) {
  .side-by-side { grid-template-columns: 1fr; } }
.side-card { border: 1px solid var(--border-soft);
  border-radius: var(--r-md); background: var(--bg);
  padding: var(--s2) var(--s3); min-height: 92px; }
.side-card .side-head { display: flex; align-items: center;
  gap: var(--s2); font-size: var(--fs-xs); font-weight: 700;
  letter-spacing: .06em; text-transform: uppercase;
  color: var(--muted); margin-bottom: var(--s1); }
.spark { display: flex; align-items: center; gap: var(--s2); }
.spark svg { display: block; flex: 1; min-width: 0; }
.spark .last { font-family: var(--mono); font-size: var(--fs-md);
  font-weight: 700; font-variant-numeric: tabular-nums;
  white-space: nowrap; }
.spark-empty { color: var(--faint); font-size: var(--fs-sm);
  padding: var(--s3) 0; text-align: center; }
.spark-err { color: var(--red); font-size: var(--fs-sm);
  font-family: var(--mono); word-break: break-word;
  max-height: 76px; overflow-y: auto; }
.sample-lines { font-family: var(--mono); font-size: var(--fs-sm);
  white-space: pre-wrap; word-break: break-word;
  max-height: 220px; overflow-y: auto; line-height: 1.6; }
.sample-lines .ts { color: var(--faint); margin-right: 8px; }
.sample-tbl { font-size: 12px; margin-top: 4px; }
.sample-tbl td { padding: 2px 8px 2px 0; border-bottom: 0; }
.sample-tbl th { position: static; background: transparent; }
.signoff { display: flex; gap: var(--s2); align-items: center;
  flex-wrap: wrap; margin-top: var(--s2);
  padding: var(--s2) var(--s3); border: 1px dashed var(--border);
  border-radius: var(--r-md); background: var(--bg); }
.signoff .q { font-weight: 600; font-size: var(--fs-md); }
.signoff input { flex: 1; min-width: 140px; width: auto; }
.statstrip { display: flex; gap: var(--s4); flex-wrap: wrap;
  padding: var(--s1) 0; }
.statstrip .st { text-align: left; }
.statstrip .st .v { font-family: var(--mono); font-weight: 700;
  font-size: var(--fs-md); font-variant-numeric: tabular-nums; }
.statstrip .st .k { font-size: 10px; color: var(--faint);
  text-transform: uppercase; letter-spacing: .06em; }

/* ======================================================== tabs */
.tabs { display: flex; gap: 2px; border-bottom: 1px solid
  var(--border); margin-bottom: var(--s4); }
.tabs a { padding: var(--s2) var(--s3); color: var(--muted);
  font-weight: 600; font-size: var(--fs-md);
  border-bottom: 2px solid transparent; margin-bottom: -1px;
  transition: color var(--t-fast), border-color var(--t-fast); }
.tabs a:hover { color: var(--text); text-decoration: none; }
.tabs a.active { color: var(--accent);
  border-bottom-color: var(--accent); }

/* ======================================================== findings */
.finding { border: 1px solid var(--border-soft);
  border-left-width: 3px; border-radius: var(--r-md);
  background: var(--bg2); padding: var(--s3) var(--s4);
  margin-bottom: var(--s3); box-shadow: var(--shadow-sm); }
.finding.blocker { border-left-color: var(--red); }
.finding.warn { border-left-color: var(--amber); }
.finding.info { border-left-color: var(--blue); }
.finding .f-head { display: flex; gap: var(--s2);
  align-items: center; flex-wrap: wrap; }
.finding .f-problem { font-weight: 600; flex: 1; min-width: 200px; }
.finding .f-evidence { margin-top: var(--s2); }
.fix-preview { border: 1px dashed var(--border);
  border-radius: var(--r-md); padding: var(--s3);
  margin-top: var(--s2); background: var(--bg); }

/* ======================================================== flyout */
#overlay { position: fixed; inset: 0; background: rgba(4,8,14,.55);
  z-index: 90; display: none; }
#overlay.show { display: block; }
.flyout { position: fixed; top: 0; right: 0; height: 100vh;
  width: min(460px, 94vw); background: var(--bg2);
  border-left: 1px solid var(--border); z-index: 95;
  box-shadow: var(--shadow); transform: translateX(102%);
  transition: transform .18s ease-out; display: flex;
  flex-direction: column; }
.flyout.show { transform: translateX(0); }
.flyout-head { display: flex; align-items: center; gap: var(--s2);
  padding: var(--s4); border-bottom: 1px solid var(--border); }
.flyout-head h2 { margin: 0; flex: 1; }
.flyout-body { flex: 1; overflow-y: auto; padding: var(--s4); }
.flyout-foot { padding: var(--s3) var(--s4);
  border-top: 1px solid var(--border); display: flex;
  gap: var(--s2); justify-content: flex-end; }

/* ======================================================== modal */
.modal-wrap { position: fixed; inset: 0; z-index: 96;
  display: flex; align-items: center; justify-content: center;
  background: rgba(4,8,14,.55); }
.modal { background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--r-lg); box-shadow: var(--shadow);
  padding: var(--s4); width: min(420px, 92vw); }
.modal h2 { margin-top: 0; }

/* ======================================================== drawer */
#drawer { position: fixed; top: 0; right: 0; height: 100vh;
  width: min(440px, 94vw); background: var(--bg2);
  border-left: 1px solid var(--border); z-index: 80;
  box-shadow: var(--shadow); transform: translateX(102%);
  transition: transform .18s ease-out; display: flex;
  flex-direction: column; }
#drawer.show { transform: translateX(0); }
.job-item { border: 1px solid var(--border-soft);
  border-radius: var(--r-md); margin-bottom: var(--s2);
  overflow: hidden; }
.job-head { display: flex; align-items: center; gap: var(--s2);
  padding: var(--s2) var(--s3); cursor: pointer;
  background: var(--bg3); font-size: var(--fs-md);
  font-weight: 600; transition: background var(--t-fast); }
.job-head:hover { background: var(--bg4); }
.job-log { font-family: var(--mono); font-size: var(--fs-xs);
  background: var(--console-bg); color: var(--console-fg);
  max-height: 220px; overflow-y: auto; padding: var(--s2) var(--s3);
  display: none; line-height: 1.6; }
.job-item.open .job-log { display: block; }

/* ======================================================== ds view */
.health-dot { display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 5px; background: var(--faint);
  vertical-align: baseline; }
.health-dot.ok { background: var(--green); }
.health-dot.err { background: var(--red); }
.health-dot.warn { background: var(--amber); }

/* ======================================================== editor */
.editor-wrap { position: relative; }
.ac { position: absolute; left: 0; right: 0; top: 100%;
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--r-sm); box-shadow: var(--shadow);
  z-index: 50; max-height: 200px; overflow-y: auto; display: none; }
.ac.show { display: block; }
.ac button { display: block; width: 100%; text-align: left;
  background: none; border: 0; color: var(--text);
  font-family: var(--mono); font-size: var(--fs-sm);
  padding: 5px 10px; cursor: pointer; }
.ac button:hover, .ac button.sel { background: var(--accent-dim);
  color: var(--accent); }

/* ======================================================== chat */
#chatlog { max-height: 52vh; overflow-y: auto; padding: var(--s1); }
.msg { max-width: 82%; margin: var(--s2) 0; padding: 9px 13px;
  border-radius: 10px; white-space: pre-wrap;
  word-break: break-word; font-size: var(--fs-md); }
.msg.user { background: var(--accent-dim); margin-left: auto; }
.msg.assistant { background: var(--bg3); }
.msg pre { margin: var(--s2) 0; }

/* ======================================================== toasts */
#toasts { position: fixed; right: var(--s4); bottom: var(--s4);
  z-index: 100; display: flex; flex-direction: column;
  gap: var(--s2); max-width: 420px; }
.toast { background: var(--bg2); border: 1px solid var(--border);
  border-left: 4px solid var(--accent); border-radius: var(--r-md);
  padding: 10px 14px; box-shadow: var(--shadow);
  font-size: var(--fs-md); word-break: break-word;
  display: flex; gap: 8px; align-items: flex-start;
  animation: toast-in .15s ease-out; }
.toast svg.i { margin-top: 2px; color: var(--accent); }
.toast.err { border-left-color: var(--red); }
.toast.err svg.i { color: var(--red); }
.toast.ok { border-left-color: var(--green); }
.toast.ok svg.i { color: var(--green); }
@keyframes toast-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}

/* =================================================== charts (3a) */
/* Categorical series palette (validated, colorblind-safe) --
   dark-first to match the app default; light overridden below. */
.chart {
  position: relative; width: 100%;
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
  --series-4: #c98500; --series-5: #d55181; --series-6: #008300;
  --series-7: #9085e9; --series-8: #e66767;
}
[data-theme="light"] .chart {
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
  --series-4: #eda100; --series-5: #e87ba4; --series-6: #008300;
  --series-7: #4a3aa7; --series-8: #e34948;
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) .chart {
    --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
    --series-4: #eda100; --series-5: #e87ba4; --series-6: #008300;
    --series-7: #4a3aa7; --series-8: #e34948;
  }
}
.chart svg { display: block; width: 100%; height: auto;
  overflow: visible; }
.chart .ch-grid { stroke: var(--border-soft); stroke-width: 1;
  shape-rendering: crispEdges; }
.chart .ch-axis { stroke: var(--border); stroke-width: 1;
  shape-rendering: crispEdges; }
.chart text.ch-tick { fill: var(--faint); font: 10px var(--sans);
  font-variant-numeric: tabular-nums; }
.chart .ch-line { fill: none; stroke-width: 2;
  stroke-linejoin: round; stroke-linecap: round; }
.chart .ch-guide { stroke: var(--muted); stroke-width: 1;
  stroke-dasharray: 3 3; opacity: 0; pointer-events: none; }
.chart .ch-guide.on { opacity: .85; }
.chart .ch-dot { stroke: var(--bg2); stroke-width: 1.5; }
.chart .ch-lastlbl { fill: var(--muted); font: 700 10px var(--sans);
  font-variant-numeric: tabular-nums; }
.chart .ch-bar { rx: 3; }
.chart .ch-barlbl { fill: var(--muted); font: 700 10px var(--sans);
  font-variant-numeric: tabular-nums; text-anchor: middle; }
.chart .ch-slice { stroke: var(--bg2); stroke-width: 2; }
.chart-legend { display: flex; flex-wrap: wrap; gap: 4px 12px;
  margin-top: 6px; font-size: var(--fs-xs); color: var(--muted); }
.chart-legend .lg { display: inline-flex; align-items: center;
  gap: 5px; max-width: 220px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.chart-legend .sw { width: 10px; height: 10px; border-radius: 2px;
  flex: 0 0 10px; }
.chart-stat { display: flex; flex-direction: column;
  justify-content: center; align-items: flex-start;
  min-height: 120px; padding: 4px 2px; }
.chart-stat .cs-num { font-size: 34px; font-weight: 750;
  letter-spacing: -.02em; line-height: 1.05;
  font-variant-numeric: tabular-nums; }
.chart-stat .cs-num.ok { color: var(--green); }
.chart-stat .cs-num.warn { color: var(--amber); }
.chart-stat .cs-num.err { color: var(--red); }
.chart-stat .cs-unit { font-size: var(--fs-md); color: var(--muted);
  font-weight: 600; margin-left: 4px; }
.chart-stat .cs-spark { width: 100%; margin-top: 8px; color:
  var(--accent); }
.chart-logs { font-family: var(--mono); font-size: var(--fs-sm);
  line-height: 1.6; max-height: 200px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word; }
.chart-logs .lt { color: var(--faint); margin-right: 8px;
  user-select: none; }
.chart-table { max-height: 220px; overflow: auto; }
.chart-table table { font-size: 12px; }
.chart-table td, .chart-table th { padding: 3px 8px; }
.chart-tip { position: absolute; z-index: 20; pointer-events: none;
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--r-sm); box-shadow: var(--shadow);
  padding: 6px 9px; font-size: var(--fs-sm); min-width: 90px;
  display: none; }
.chart-tip .ch-tip-t { font-weight: 700; margin-bottom: 3px;
  color: var(--text); }
.chart-tip .ch-tip-row { display: flex; align-items: center;
  gap: 6px; color: var(--muted); }
.chart-tip .ch-sw { width: 9px; height: 9px; border-radius: 2px;
  flex: 0 0 9px; }
.chart-tip .ch-tip-row b { color: var(--text);
  font-variant-numeric: tabular-nums; margin-left: auto;
  padding-left: 8px; }
.chart-empty { display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 8px;
  min-height: 120px; text-align: center; color: var(--muted);
  font-size: var(--fs-sm); border: 1px dashed var(--border-soft);
  border-radius: var(--r-md); padding: var(--s3); }
.chart-empty svg.i { color: var(--faint); }
.chart-empty.err { color: var(--red); border-color: var(--red-bg); }
.chart-empty.err svg.i { color: var(--red); }
.chart-empty.warn { color: var(--amber); }

/* ================================================ skeletons */
.skel { position: relative; overflow: hidden;
  background: var(--bg3); border-radius: var(--r-sm); }
.skel::after { content: ""; position: absolute; inset: 0;
  transform: translateX(-100%);
  background: linear-gradient(90deg, transparent,
    rgba(255,255,255,.06), transparent);
  animation: skel 1.2s infinite; }
[data-theme="light"] .skel::after {
  background: linear-gradient(90deg, transparent,
    rgba(15,30,60,.05), transparent); }
@keyframes skel { 100% { transform: translateX(100%); } }
.skel-chart { height: 150px; width: 100%; }

/* =============================================== compare (3b) */
.cmp-topbar { display: flex; align-items: center; gap: var(--s4);
  flex-wrap: wrap; }
.cmp-score { display: flex; align-items: center; gap: 10px; }
.cmp-score-lbl { font-size: var(--fs-xs); color: var(--muted);
  line-height: 1.25; }
.cmp-tally { display: flex; flex-wrap: wrap; gap: 3px;
  align-items: center; }
.cmp-controls { display: flex; align-items: center; gap: var(--s3);
  flex-wrap: wrap; margin-left: auto; }
.seg-range { display: inline-flex; gap: 4px; flex-wrap: wrap; }
.cmp-custom { display: flex; gap: 6px; align-items: center; }
.cmp-switch { display: inline-flex; align-items: center; gap: 6px;
  margin: 0; font-size: var(--fs-sm); color: var(--muted);
  font-weight: 600; cursor: pointer; }
.cmp-switch input { width: auto; margin: 0; }
.cmp-rowhead { font-size: var(--fs-xs); font-weight: 700;
  letter-spacing: .08em; text-transform: uppercase;
  color: var(--faint); margin: var(--s4) 0 var(--s2);
  padding-bottom: 4px; border-bottom: 1px solid var(--border-soft); }
.cmp-rowhead:first-child { margin-top: 0; }
.cmp-pair { border: 1px solid var(--border-soft);
  border-radius: var(--r-lg); background: var(--bg2);
  box-shadow: var(--shadow-card); padding: var(--s3) var(--s4);
  margin-bottom: var(--s3); }
.cmp-pair-head { display: flex; align-items: center; gap: var(--s2);
  flex-wrap: wrap; margin-bottom: var(--s2); }
.cmp-pair-title { font-weight: 650; font-size: var(--fs-lg); }
.cmp-agree { display: inline-flex; }
.cmp-why { color: var(--muted); font-size: var(--fs-sm);
  min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; flex: 1; }
.cmp-open { margin-left: auto; }
.cmp-cols { display: grid; grid-template-columns: 1fr 1fr;
  gap: var(--s3); align-items: stretch; }
@media (max-width: 720px) {
  .cmp-cols { grid-template-columns: 1fr; }
  .cmp-controls { margin-left: 0; }
}
.cmp-side { border: 1px solid var(--border-soft);
  border-radius: var(--r-md); background: var(--bg);
  padding: var(--s2) var(--s3); min-width: 0; }
.cmp-side-h { display: flex; align-items: center; gap: 6px;
  font-size: var(--fs-xs); font-weight: 700; letter-spacing: .05em;
  text-transform: uppercase; color: var(--muted);
  margin-bottom: var(--s2); }
.cmp-side-h .cmp-refresh { margin-left: auto; }
.cmp-side-h svg.i { color: var(--faint); }

/* =============================================== flow (3c) */
.flow-card { border: 1px solid var(--border-soft);
  border-radius: var(--r-md); background: var(--bg);
  padding: var(--s3); margin-top: var(--s2); }
.flow-head { display: flex; align-items: center; gap: var(--s2);
  flex-wrap: wrap; margin-bottom: var(--s2); }
.flow-ba { display: flex; align-items: center; gap: var(--s3);
  flex-wrap: wrap; }
.flow-num { text-align: center; }
.flow-num .flow-n { font-size: 28px; font-weight: 750;
  font-variant-numeric: tabular-nums; line-height: 1; }
.flow-num .flow-n.after { color: var(--green); }
.flow-num .flow-n.before { color: var(--muted); }
.flow-arrow { color: var(--green); display: flex;
  align-items: center; }
.flow-badges { display: flex; flex-wrap: wrap; gap: 3px;
  margin-left: auto; }
.flow-sample { margin-top: var(--s2); }

/* =============================================== welcome (3d) */
.welcome { border: 1px solid var(--accent);
  background: var(--accent-soft); }
.welcome-head { display: flex; align-items: center;
  justify-content: space-between; margin-bottom: var(--s2);
  font-size: var(--fs-lg); }
.wc-list { display: flex; flex-direction: column; gap: 4px; }
.wc-item { display: flex; align-items: center; gap: 10px;
  padding: 7px 10px; border-radius: var(--r-sm); color: var(--text);
  font-size: var(--fs-md); font-weight: 500;
  border: 1px solid transparent; }
.wc-item:hover { background: var(--bg3); text-decoration: none; }
.wc-item .wc-mark { width: 20px; height: 20px; border-radius: 50%;
  border: 2px solid var(--border); display: inline-flex;
  align-items: center; justify-content: center; flex: 0 0 20px;
  font-size: 12px; color: var(--muted); }
.wc-item.done .wc-mark { border-color: var(--green);
  color: var(--green); background: var(--green-bg); }
.wc-item.next { background: var(--bg2); border-color: var(--accent);
  font-weight: 650; }
.wc-item.next .wc-mark { border-color: var(--accent);
  color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.wc-item .wc-next { margin-left: auto; color: var(--accent);
  font-weight: 700; font-size: var(--fs-sm);
  display: inline-flex; align-items: center; gap: 4px; }

/* =============================================== help sheet */
.help-modal { width: min(520px, 94vw); }
.help-tbl { width: 100%; }
.help-tbl td { padding: 5px 8px; border-bottom: 1px solid
  var(--border-soft); }
.help-tbl td:first-child { width: 130px; }
.help-tbl kbd { font-family: var(--mono); font-size: 11px;
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: 4px; padding: 1px 6px; color: var(--text); }

/* =============================================== cost (1.5) */
.cost-actions { display: flex; align-items: flex-end; gap: var(--s3);
  flex-wrap: wrap; }
.cost-actions .fld { display: flex; flex-direction: column; gap: 0; }
.cost-actions .fld label { margin: 0 0 var(--s1); }
.cost-actions .fld input, .cost-actions .fld select { width: auto;
  min-width: 120px; }
.cost-actions .grow { flex: 1; }
.term-i { display: inline-flex; color: var(--faint); cursor: help;
  margin-left: 4px; vertical-align: middle; }
.term-i svg.i { width: 13px; height: 13px; }
.term-i:hover { color: var(--accent); }
.ds-tcard { border: 1px solid var(--border-soft);
  border-radius: var(--r-lg); background: var(--bg2);
  box-shadow: var(--shadow-card); padding: var(--s4);
  margin-bottom: var(--s4); }
.ds-tcard .tc-head { display: flex; align-items: center; gap: var(--s2);
  flex-wrap: wrap; margin-bottom: var(--s3); }
.ds-tcard .tc-head .tc-uid { font-family: var(--mono);
  font-size: var(--fs-xs); color: var(--faint); }
.tc-viz { display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--s4); align-items: start; margin-top: var(--s3); }
.tc-viz h3 { display: flex; align-items: center; }
.rank-tbl { max-height: 176px; overflow-y: auto; margin-top: 6px; }
.rank-tbl table { width: 100%; font-size: 12px; }
.rank-tbl td { padding: 3px 8px; border-bottom: 1px solid
  var(--border-soft); }
.rank-tbl td.v { text-align: right; font-variant-numeric: tabular-nums;
  font-family: var(--mono); white-space: nowrap; }
.rank-tbl td.n { font-family: var(--mono); word-break: break-all; }
.savings-hero { display: flex; align-items: center; gap: var(--s5);
  flex-wrap: wrap; padding: var(--s4);
  border: 1px solid var(--green); background: var(--green-bg);
  border-radius: var(--r-lg); }
.savings-hero .sh-flow { display: flex; align-items: center;
  gap: var(--s4); flex-wrap: wrap; }
.savings-hero .sh-big { margin-left: auto; text-align: right; }
.savings-hero .sh-pct { font-size: 40px; font-weight: 800;
  line-height: 1; color: var(--green); letter-spacing: -.02em;
  font-variant-numeric: tabular-nums; }
.savings-hero .sh-sub { font-size: var(--fs-md); color: var(--text);
  font-weight: 600; margin-top: 2px; }
.savings-hero .sh-note { flex-basis: 100%; font-size: var(--fs-xs);
  color: var(--muted); margin-top: var(--s1); }
.savings-hero.flat { border-color: var(--border); background: var(--bg); }
.savings-hero.flat .sh-pct { color: var(--muted); }
.cost-total { display: flex; align-items: baseline; gap: 8px;
  margin-bottom: var(--s3); }
.cost-total .ct-num { font-size: 30px; font-weight: 750;
  letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
.cost-total .ct-lbl { color: var(--muted); font-size: var(--fs-md); }
.pricing-grid { display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: var(--s2) var(--s4); }
.pricing-grid .pf label { margin-top: 0; }
.pricing-grid .pf .in-money { position: relative; }
.pricing-grid .pf .in-money::before { content: "$"; position: absolute;
  left: 9px; top: 50%; transform: translateY(-50%); color: var(--muted);
  font-size: var(--fs-md); pointer-events: none; }
.pricing-grid .pf .in-money input { padding-left: 20px; }
.recompute-note { font-size: var(--fs-sm); color: var(--muted);
  display: inline-flex; align-items: center; gap: 6px; }
.rec-card { border: 1px solid var(--border-soft);
  border-left-width: 3px; border-radius: var(--r-md);
  background: var(--bg2); padding: var(--s3) var(--s4);
  margin-bottom: var(--s3); box-shadow: var(--shadow-sm); }
.rec-card.sev-high { border-left-color: var(--red); }
.rec-card.sev-medium { border-left-color: var(--amber); }
.rec-card.sev-low { border-left-color: var(--blue); }
.rec-head { display: flex; gap: var(--s2); align-items: flex-start;
  flex-wrap: wrap; }
.rec-title { font-weight: 650; font-size: var(--fs-lg); flex: 1;
  min-width: 220px; }
.rec-rationale { color: var(--muted); font-size: var(--fs-md);
  margin: var(--s2) 0; }
.rec-ev { display: flex; flex-wrap: wrap; gap: 3px;
  margin: var(--s2) 0; }
.rec-save { display: flex; flex-wrap: wrap; gap: var(--s2);
  align-items: center; margin: var(--s2) 0; }
.rec-save .save-money { font-weight: 750; color: var(--green);
  font-size: var(--fs-lg); font-variant-numeric: tabular-nums; }
.rec-cfg { border: 1px dashed var(--border); border-radius: var(--r-md);
  background: var(--bg); padding: var(--s2) var(--s3);
  margin-top: var(--s2); }
.rec-cfg .cfg-bar { display: flex; align-items: center; gap: var(--s2);
  flex-wrap: wrap; }
.rec-cfg .cfg-bar label { margin: 0; }
.rec-cfg .cfg-bar select { width: auto; min-width: 150px; }
.rec-cfg pre { margin-top: var(--s2); }
.rec-cfg .cfg-note { font-size: var(--fs-xs); color: var(--muted);
  margin-top: 4px; font-style: italic; }
.recs-head { display: flex; align-items: center; gap: var(--s2);
  flex-wrap: wrap; margin-bottom: var(--s3); }
.recs-head .grow { flex: 1; }

/* =============================================== stack (1.6) */
.stack-fields { display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0 var(--s4); }
.stack-fields .fld label { margin-top: var(--s3); }
.stack-toggle { display: flex; align-items: center; gap: var(--s2);
  margin: var(--s3) 0 0; font-size: var(--fs-md);
  color: var(--text); font-weight: 600; }
.stack-toggle input { width: auto; margin: 0; }
.stack-toggle .field-help { font-weight: 400; margin: 0; }
.stack-group { font-size: var(--fs-xs); font-weight: 700;
  letter-spacing: .08em; text-transform: uppercase;
  color: var(--faint); margin: var(--s5) 0 var(--s2);
  padding-bottom: 4px; border-bottom: 1px solid var(--border-soft);
  display: flex; align-items: center; gap: 6px; }
.stack-group:first-child { margin-top: var(--s3); }
.stack-group svg.i { color: var(--muted); }
.stack-group .gc { margin-left: auto; font-weight: 600;
  letter-spacing: 0; text-transform: none; }
.risk-row { display: flex; flex-wrap: wrap; gap: 3px;
  margin: var(--s2) 0 0; }
.pack-floor td { background: var(--green-bg) !important; }
.pack-note { font-size: var(--fs-sm); color: var(--muted);
  margin: var(--s2) 0; }
.np-card { border: 1px solid var(--border-soft);
  border-radius: var(--r-md); background: var(--bg);
  padding: var(--s2) var(--s3); margin-bottom: var(--s2); }
.np-card .np-name { font-weight: 650; font-family: var(--mono);
  font-size: var(--fs-md); }
.np-card .np-kv { font-size: var(--fs-sm); color: var(--muted);
  margin-top: 2px; }
.np-card .np-kv b { color: var(--text); }
.ai-actions { display: flex; flex-wrap: wrap; gap: var(--s2);
  align-items: center; margin: var(--s2) 0; }
.ai-actions .grow { flex: 1; }
.mcp-tool { display: flex; gap: 8px; align-items: baseline;
  padding: 4px 0; border-bottom: 1px solid var(--border-soft);
  font-size: var(--fs-sm); }
.mcp-tool:last-child { border-bottom: 0; }
.mcp-tool .mono { color: var(--accent); flex: 0 0 auto; }
.mcp-tool .desc { color: var(--muted); }
.ctx-legend { font-size: var(--fs-sm); color: var(--muted);
  border-left: 3px solid var(--accent); padding: 2px 0 2px 10px;
  margin: var(--s3) 0; background: var(--accent-soft);
  border-radius: 0 var(--r-sm) var(--r-sm) 0; }
.ans-box { border: 1px solid var(--border-soft);
  border-radius: var(--r-md); background: var(--bg);
  padding: var(--s3); margin-top: var(--s3);
  white-space: pre-wrap; word-break: break-word;
  font-size: var(--fs-md); }
.stack-head { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 4px var(--s4); border: 1px solid var(--border-soft);
  border-left: 3px solid var(--green); border-radius: var(--r-md);
  background: var(--green-bg); padding: var(--s3) var(--s4);
  margin: var(--s3) 0; }
.stack-head .sh-money { font-size: 28px; font-weight: 750;
  color: var(--green); letter-spacing: -.02em;
  font-variant-numeric: tabular-nums; }
.stack-head .sh-and { color: var(--muted); font-size: var(--fs-lg); }
.stack-head .sh-cores { font-size: 22px; font-weight: 700;
  font-variant-numeric: tabular-nums; }
.stack-head .sh-cap { flex-basis: 100%; color: var(--muted);
  font-size: var(--fs-sm); margin-top: 2px; }
.pack-tbl { overflow-x: auto; margin: var(--s2) 0; }
.pack-tbl table { width: 100%; border-collapse: collapse;
  font-size: var(--fs-sm); }
.pack-tbl th, .pack-tbl td { padding: 6px 10px; text-align: left;
  border-bottom: 1px solid var(--border-soft); white-space: nowrap; }
.pack-tbl th { color: var(--muted); font-weight: 600;
  font-size: var(--fs-xs); text-transform: uppercase;
  letter-spacing: .05em; }
.pack-tbl td.num { text-align: right;
  font-variant-numeric: tabular-nums; }
.pack-tbl td.mono { font-family: var(--mono); }
.pack-tbl tr.pack-floor td { font-weight: 650; }
.finding-group { margin-top: var(--s3); }
.stack-yaml pre { max-height: 460px; overflow: auto; }
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
      <div class="nav-sec">Migrate</div>
      <a href="#/overview" data-r="overview">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><rect x="3" y="3" width="7" height="7"
          rx="1"></rect><rect x="14" y="3" width="7" height="7"
          rx="1"></rect><rect x="3" y="14" width="7" height="7"
          rx="1"></rect><rect x="14" y="14" width="7" height="7"
          rx="1"></rect></svg></span> Overview
        <span class="cnt" id="nav-cnt"></span></a>
      <a href="#/connect" data-r="connect">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><path d="M15 7h2a5 5 0 0 1 0 10h-2">
          </path><path d="M9 17H7A5 5 0 0 1 7 7h2"></path>
          <line x1="8" y1="12" x2="16" y2="12"></line></svg>
        </span> Connect</a>
      <a href="#/convert" data-r="convert">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><polyline points="23 4 23 10 17 10">
          </polyline><polyline points="1 20 1 14 7 14"></polyline>
          <path d="M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0
          0 0 20.5 15"></path></svg></span> Fetch &amp; Convert</a>
      <a href="#/datasources" data-r="datasources">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><ellipse cx="12" cy="5" rx="9" ry="3">
          </ellipse><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3">
          </path><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5">
          </path></svg></span> Datasources</a>
      <a href="#/import" data-r="import">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2
          2H5a2 2 0 0 1-2-2v-4"></path><polyline
          points="17 8 12 3 7 8"></polyline><line x1="12" y1="3"
          x2="12" y2="15"></line></svg></span> Import</a>
      <div class="nav-sec">Review</div>
      <a href="#/compare" data-r="compare">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><rect x="3" y="4" width="8" height="16"
          rx="1"></rect><rect x="13" y="4" width="8" height="16"
          rx="1"></rect></svg></span> Compare</a>
      <a href="#/changes" data-r="changes">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><circle cx="12" cy="12" r="9"></circle>
          <polyline points="12 7 12 12 15.5 14"></polyline></svg>
        </span> Changes</a>
      <a href="#/ai" data-r="ai">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><path d="M12 3l1.9 5.1L19 10l-5.1
          1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"></path><path d="M19
          15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9z">
          </path></svg></span> AI Assistant</a>
      <div class="nav-sec">Optimize</div>
      <a href="#/cost" data-r="cost">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><line x1="12" y1="1" x2="12" y2="23">
          </line><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1
          0 7H6"></path></svg></span> Cost &amp; efficiency</a>
      <a href="#/stack" data-r="stack">
        <span class="ico"><svg class="i" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"><polygon points="12 2 2 7 12 12 22 7
          12 2"></polygon><polyline points="2 17 12 22 22 17">
          </polyline><polyline points="2 12 12 17 22 12"></polyline>
          </svg></span> Stack deep-dive</a>
    </nav>
    <div class="sidebar-foot">
      Local only &mdash; API keys stay in server memory,
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
        <span id="pill-ai-lbl">AI</span></span>
      <button class="pill" id="jobsbtn" type="button"
        title="Background jobs" aria-label="Background jobs">
        <span class="spin"></span>Jobs
        <span id="jobscount"></span></button>
      <button class="pill" id="helpbtn" type="button"
        title="Keyboard shortcuts (press ?)"
        aria-label="Keyboard shortcuts">?</button>
      <button class="pill" id="themebtn" type="button" title="Theme">
        <span id="themelbl">Auto</span></button>
    </header>
    <main id="view"></main>
  </div>
</div>
<div id="drawer" role="dialog" aria-label="Background jobs">
  <div class="flyout-head"><h2>Background jobs</h2>
    <button class="btn small ghost" id="drawer-close"
      aria-label="Close">&#10005;</button></div>
  <div class="flyout-body" id="drawer-body"></div>
</div>
<div id="overlay"></div>
<div id="flyout-slot"></div>
<div id="modal-slot"></div>
<div id="toasts"></div>
<script>
'use strict';

/* ====================================================== state */
var App = {
  state: null,           /* /api/state payload */
  dashboards: [],        /* /api/dashboards rows */
  expanded: {},          /* slug:panel -> open */
  ai: [], aiBusy: false,
  timers: [],            /* view-local intervals (cleared on route) */
  ws: null,              /* current workspace {slug, detail, tab} */
  dsHealth: {},          /* ds uid -> {status,message} */
  dsFlow: {},            /* ds uid -> flow family (verify-flow) */
  dsFlowSlug: '',        /* dashboard the ds flow badges track */
  cmp: null,             /* compare view state {slug,from,to,...} */
  cmpIO: null,           /* IntersectionObserver for lazy panels */
  templates: null,       /* /api/grafana/ds-templates cache */
  cost: null             /* cost view state (traffic/cost/optimize) */
};

var Jobs = { items: [], open: false };

/* Live chart registry: id -> geometry+series, for hover/sync. Reset
   on every route change so it never leaks across views. */
var CHARTS = {};

/* ====================================================== utils */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function $(sel, el) { return (el || document).querySelector(sel); }
function $all(sel, el) {
  return Array.prototype.slice.call(
    (el || document).querySelectorAll(sel));
}

async function api(path, body, method) {
  var opt = { method: method || (body === undefined ? 'GET' : 'POST'),
              headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined && method !== 'DELETE') {
    opt.body = JSON.stringify(body);
  }
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
  el.setAttribute('role', 'status');
  el.innerHTML = ico(kind === 'err' ? 'xcircle' :
                     kind === 'ok' ? 'checkcircle' : 'info', 15);
  el.appendChild(document.createTextNode(msg));
  $('#toasts').appendChild(el);
  setTimeout(function () { el.remove(); },
             kind === 'err' ? 9000 : 5000);
}

function busy(btn, on) {
  if (!btn) return;
  btn.disabled = !!on;
  btn.classList.toggle('busy', !!on);
}

function fmtNum(v) {
  if (v == null || !isFinite(v)) return '&ndash;';
  var a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(1) + 'T';
  if (a >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (a >= 1e4) return (v / 1e3).toFixed(1) + 'k';
  if (a >= 100) return String(Math.round(v));
  if (a >= 1) return String(Math.round(v * 100) / 100);
  if (a === 0) return '0';
  return v.toPrecision(3);
}

function debounce(fn, ms) {
  var t = null;
  return function () {
    var args = arguments, self = this;
    clearTimeout(t);
    t = setTimeout(function () { fn.apply(self, args); }, ms);
  };
}

/* ====================================================== icons */
/* Tiny inline SVG icon set -- one stroke style everywhere. */
var ICONS = {
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 ' +
    '1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"></path>' +
    '<line x1="12" y1="9" x2="12" y2="13"></line>' +
    '<line x1="12" y1="17" x2="12.01" y2="17"></line>',
  xcircle: '<circle cx="12" cy="12" r="9"></circle>' +
    '<line x1="15" y1="9" x2="9" y2="15"></line>' +
    '<line x1="9" y1="9" x2="15" y2="15"></line>',
  check: '<polyline points="20 6 9 17 4 12"></polyline>',
  checkcircle: '<circle cx="12" cy="12" r="9"></circle>' +
    '<polyline points="8 12.5 11 15.5 16 9.5"></polyline>',
  info: '<circle cx="12" cy="12" r="9"></circle>' +
    '<line x1="12" y1="11" x2="12" y2="16"></line>' +
    '<line x1="12" y1="8" x2="12.01" y2="8"></line>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"></rect>' +
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 ' +
    '2 2v1"></path>',
  chevron: '<polyline points="9 6 15 12 9 18"></polyline>',
  pin: '<line x1="12" y1="17" x2="12" y2="22"></line>' +
    '<path d="M7 4h10l-1.5 7.5 2.5 3.5H6l2.5-3.5z"></path>',
  terminal: '<polyline points="4 17 10 11 4 5"></polyline>' +
    '<line x1="12" y1="19" x2="20" y2="19"></line>',
  play: '<polygon points="7 4 19 12 7 20 7 4"></polygon>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4">' +
    '</path><polyline points="7 10 12 15 17 10"></polyline>' +
    '<line x1="12" y1="15" x2="12" y2="3"></line>',
  zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2">' +
    '</polygon>',
  inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12">' +
    '</polyline><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 ' +
    '0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.1z">' +
    '</path>',
  search: '<circle cx="11" cy="11" r="7"></circle>' +
    '<line x1="21" y1="21" x2="16.2" y2="16.2"></line>',
  arrow: '<line x1="5" y1="12" x2="19" y2="12"></line>' +
    '<polyline points="12 5 19 12 12 19"></polyline>',
  sparkle: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1' +
    'L5 10l5.1-1.9z"></path><path d="M19 15l.9 2.1L22 18l-2.1.9' +
    'L19 21l-.9-2.1L16 18l2.1-.9z"></path>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>' +
    '<path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path>' +
    '<path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path>',
  wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 ' +
    '1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 ' +
    '1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"></path>',
  refresh: '<polyline points="23 4 23 10 17 10"></polyline>' +
    '<polyline points="1 20 1 14 7 14"></polyline>' +
    '<path d="M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 ' +
    '0 20.5 15"></path>'
};

function ico(name, size) {
  var body = ICONS[name];
  if (!body) return '';
  var s = size || 16;
  return '<svg class="i" style="width:' + s + 'px;height:' + s +
    'px" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' + body + '</svg>';
}

/* ====================================================== copy/trunc */
function copyText(text, btn) {
  var done = function () {
    toast('Copied to clipboard', 'ok');
    if (btn) {
      btn.classList.add('on');
      setTimeout(function () { btn.classList.remove('on'); }, 900);
    }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () {
      fallbackCopy(text); done(); });
  } else { fallbackCopy(text); done(); }
}

/* Copy button markup: copies textContent of #<targetId>. Wired once
   via the delegated click handler in boot. */
function copyBtn(targetId, label) {
  return '<button class="btn small ghost iconbtn" type="button" ' +
    'data-copy="' + esc(targetId) + '" title="Copy" ' +
    'aria-label="Copy ' + esc(label || 'text') + '">' +
    ico('copy', 13) + '</button>';
}

var _uidSeq = 0;
function uid() { _uidSeq += 1; return 'u' + _uidSeq; }

/* Long strings: show the head, expand the rest on demand. */
function truncHtml(s, n) {
  s = s == null ? '' : String(s);
  n = n || 280;
  if (s.length <= n) return esc(s);
  var more = 'show all (' + (s.length - n) + ' more chars)';
  return '<span>' + esc(s.slice(0, n)) +
    '<span class="trunc-rest" hidden>' + esc(s.slice(n)) +
    '</span> <button class="linklike" type="button" ' +
    'data-expand="1" data-more="' + esc(more) + '">&hellip; ' +
    esc(more) + '</button></span>';
}

/* Collapsible pretty-printed JSON block with a copy button. */
function jsonDetails(title, obj, open) {
  var txt = typeof obj === 'string' ? obj :
    JSON.stringify(obj, null, 2);
  var id = 'jd-' + uid();
  return '<details class="jd"' + (open ? ' open' : '') + '>' +
    '<summary><span class="chev">' + ico('chevron', 12) +
    '</span>' + esc(title) +
    ' <span class="chip dim">json</span>' + copyBtn(id, title) +
    '</summary><pre id="' + id + '">' + esc(txt) +
    '</pre></details>';
}

/* ================================================= error cards */
/* One error component everywhere: WHAT failed, the exact detail,
   and the NEXT ACTION (a real link when one can be derived). */
function errAction(msg) {
  var m = String(msg || '').toLowerCase();
  if (m.indexOf('grafana url') >= 0 || m.indexOf('token') >= 0 ||
      m.indexOf('unauthorized') >= 0 || m.indexOf('401') >= 0) {
    return { label: 'Open Connect', href: '#/connect' };
  }
  if (m.indexOf('new relic api key') >= 0 ||
      m.indexOf('nrak') >= 0 || m.indexOf('nerdgraph') >= 0) {
    return { label: 'Open Connect', href: '#/connect' };
  }
  if (m.indexOf('anthropic') >= 0 || m.indexOf('ai backend') >= 0 ||
      m.indexOf('ai command') >= 0) {
    return { label: 'Configure AI in Connect', href: '#/connect' };
  }
  if (m.indexOf('datasource') >= 0) {
    return { label: 'Open Datasources', href: '#/datasources' };
  }
  if (m.indexOf('run convert') >= 0 ||
      m.indexOf('convert first') >= 0) {
    return { label: 'Open Fetch & Convert', href: '#/convert' };
  }
  if (m.indexOf('diagnose') >= 0) return null;
  return null;
}

/* what: plain sentence (may contain safe HTML from our own code);
   detail: raw server/error text (escaped, collapsible when long);
   action: {label, href} | {label, id} (button wired by caller) |
   null (auto-derived from detail when possible). */
function errorCard(what, detail, action, severity) {
  var act = action === undefined ? errAction(detail) : action;
  var d = detail == null ? '' : String(detail);
  var detailHtml = '';
  if (d) {
    if (d.length > 220) {
      detailHtml = '<div class="ecard-detail"><details>' +
        '<summary>Show full error (' + d.length +
        ' chars)</summary><pre style="margin:6px 0 0">' + esc(d) +
        '</pre></details><span>' + esc(d.slice(0, 220)) +
        '&hellip;</span></div>';
    } else {
      detailHtml = '<div class="ecard-detail">' + esc(d) + '</div>';
    }
  }
  var actHtml = '';
  if (act && act.href) {
    actHtml = '<div class="ecard-act"><a class="btn small" href="' +
      esc(act.href) + '">' + esc(act.label) + ' ' +
      ico('arrow', 12) + '</a></div>';
  } else if (act && act.id) {
    actHtml = '<div class="ecard-act"><button class="btn small" ' +
      'type="button" id="' + esc(act.id) + '">' + esc(act.label) +
      '</button></div>';
  }
  return '<div class="ecard' +
    (severity === 'warn' ? ' warn' : '') + '" role="alert">' +
    '<div class="ecard-head">' +
    ico(severity === 'warn' ? 'alert' : 'xcircle', 15) +
    '<span>' + what + '</span></div>' + detailHtml + actHtml +
    '</div>';
}

/* ================================================ console panel */
/* Structured log console: mono, level-tinted lines, receive-time
   stamps, auto-scroll with a follow pin, copy button. */
function consoleHtml(id, title) {
  return '<div class="console" id="' + esc(id) + '">' +
    '<div class="console-bar"><span class="ct">' +
    ico('terminal', 12) + esc(title || 'Log') + '</span>' +
    '<span class="cn"></span>' +
    '<button class="btn small ghost iconbtn on" type="button" ' +
    'data-cpin="1" title="Follow output (auto-scroll)" ' +
    'aria-label="Toggle auto-scroll" aria-pressed="true">' +
    ico('pin', 13) + '</button>' +
    '<button class="btn small ghost iconbtn" type="button" ' +
    'data-ccopy="1" title="Copy log" aria-label="Copy log">' +
    ico('copy', 13) + '</button></div>' +
    '<div class="console-body" aria-live="polite"></div></div>';
}

function clineClass(line) {
  if (/^(FAIL|ERROR)\b/i.test(line) ||
      /\bFAILED\b/.test(line)) return ' err';
  if (/^(WARN|WARNING)\b/i.test(line) ||
      /\bcould not\b/i.test(line)) return ' warn';
  if (/^(ok\b|Done\b|done:)/i.test(line) ||
      /^Imported \d/.test(line)) return ' ok';
  return '';
}

/* Append only lines not yet rendered; stamp them with the time we
   received them. Auto-scrolls while the pin is on. */
function consoleUpdate(root, lines) {
  if (!root || !root.isConnected) return;
  root.classList.add('show');
  var body = $('.console-body', root);
  if (!body) return;
  var have = body.childElementCount;
  var now = new Date().toTimeString().slice(0, 8);
  var add = '';
  for (var i = have; i < lines.length; i++) {
    var ln = String(lines[i] == null ? '' : lines[i]);
    add += '<div class="cline' + clineClass(ln) + '">' +
      '<span class="cts">' + now + '</span>' + esc(ln) + '</div>';
  }
  if (have > lines.length) {  /* new job reusing the panel */
    body.innerHTML = '';
    add = lines.map(function (ln) {
      ln = String(ln == null ? '' : ln);
      return '<div class="cline' + clineClass(ln) + '">' +
        '<span class="cts">' + now + '</span>' + esc(ln) +
        '</div>';
    }).join('');
  }
  if (add) body.insertAdjacentHTML('beforeend', add);
  var cn = $('.cn', root);
  if (cn) {
    cn.textContent = lines.length ?
      lines.length + ' line' + (lines.length === 1 ? '' : 's') : '';
  }
  var pin = $('button[data-cpin]', root);
  if (!pin || pin.classList.contains('on')) {
    body.scrollTop = body.scrollHeight;
  }
}

/* ====================================================== jobs */
function renderJobsBtn() {
  var running = Jobs.items.filter(function (j) {
    return j.status === 'running'; }).length;
  var btn = $('#jobsbtn');
  btn.classList.toggle('running', running > 0);
  $('#jobscount').textContent = running ? '(' + running + ')' : '';
}

function renderDrawer() {
  var body = $('#drawer-body');
  if (!Jobs.items.length) {
    body.innerHTML = '<div class="empty"><span class="eico">' +
      ico('inbox', 26) + '</span>No background jobs yet.' +
      '<br>Long operations (fetch, convert, tests, parity, ' +
      'diagnose, heal) appear here with their live logs.</div>';
    return;
  }
  body.innerHTML = Jobs.items.map(function (j, i) {
    var st = j.status === 'running' ?
      '<span class="chip info">running</span>' :
      j.status === 'error' ?
      '<span class="chip err">' + ico('xcircle', 11) +
      'error</span>' :
      '<span class="chip ok">' + ico('check', 11) + 'done</span>';
    var lines = (j.log || []).map(function (ln) {
      ln = String(ln == null ? '' : ln);
      return '<div class="cline' + clineClass(ln) + '">' +
        esc(ln) + '</div>';
    }).join('') || '<div class="cline">(no output yet)</div>';
    if (j.error) {
      lines += '<div class="cline err">ERROR: ' + esc(j.error) +
        '</div>';
    }
    return '<div class="job-item' + (j.open ? ' open' : '') +
      '" data-ji="' + i + '">' +
      '<div class="job-head" role="button" tabindex="0" ' +
      'aria-expanded="' + (j.open ? 'true' : 'false') + '">' +
      '<span>' + esc(j.kind) + '</span>' + st +
      '<span class="kv" style="margin-left:auto">' +
      esc(j.started) + '</span></div>' +
      '<div class="job-log">' + lines + '</div></div>';
  }).join('');
  $all('.job-head', body).forEach(function (h) {
    var toggle = function () {
      var it = Jobs.items[+h.parentNode.getAttribute('data-ji')];
      if (it) { it.open = !it.open; renderDrawer(); }
    };
    h.onclick = toggle;
    h.onkeydown = function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault(); toggle();
      }
    };
  });
  $all('.job-item.open .job-log', body).forEach(function (el) {
    el.scrollTop = el.scrollHeight;
  });
}

function openDrawer(open) {
  Jobs.open = open === undefined ? !Jobs.open : !!open;
  $('#drawer').classList.toggle('show', Jobs.open);
  if (Jobs.open) renderDrawer();
}

/* Start a server job and poll it. onUpdate(job) per tick. The poll
   is intentionally NOT registered in App.timers: jobs keep running
   and logging into the drawer across navigation. */
async function startJob(kind, path, body, onUpdate) {
  var r = await api(path, body || {});
  var j = { id: r.job, kind: kind, status: 'running', log: [],
            result: null, error: '', open: Jobs.open,
            started: new Date().toTimeString().slice(0, 8) };
  Jobs.items.unshift(j);
  if (Jobs.items.length > 20) Jobs.items.length = 20;
  renderJobsBtn(); if (Jobs.open) renderDrawer();
  return new Promise(function (resolve, reject) {
    var t = setInterval(async function () {
      var job;
      try { job = await api('/api/jobs/' + j.id); }
      catch (e) {
        clearInterval(t);
        j.status = 'error'; j.error = e.message;
        renderJobsBtn(); if (Jobs.open) renderDrawer();
        reject(e); return;
      }
      j.log = job.log || []; j.status = job.status;
      j.result = job.result; j.error = job.error || '';
      if (onUpdate) { try { onUpdate(job); } catch (e) {} }
      renderJobsBtn(); if (Jobs.open) renderDrawer();
      if (job.status === 'done') { clearInterval(t); resolve(job); }
      else if (job.status === 'error') {
        clearInterval(t);
        reject(new Error(job.error || 'job failed'));
      }
    }, 700);
  });
}

function logInto(el) {
  return function (job) {
    consoleUpdate(el, job.log || []);
  };
}

/* ====================================================== chips */
var CONF_CLS = { exact: 'ok', approximate: 'info',
                 'needs-review': 'warn', untranslatable: 'err' };
var TEST_CLS = { data: 'ok', 'no-data': 'warn', error: 'err' };
var VERDICT_CLS = { match: 'ok', close: 'info',
                    'value-mismatch': 'warn',
                    'shape-mismatch': 'warn', 'nr-empty': 'dim',
                    'gf-empty': 'warn', 'both-empty': 'dim',
                    'nr-error': 'err', 'gf-error': 'err' };
/* Plain-language tooltip for every verdict -- used as the default
   badge title so hovering any verdict explains what it means. */
var VERDICT_HELP = {
  match: 'Both sides return the same values over this range.',
  close: 'Values track each other but differ by a small factor.',
  'value-mismatch': 'Both sides have data but the numbers differ.',
  'shape-mismatch': 'The curves have different shapes over time.',
  'nr-empty': 'New Relic returned no data for this range.',
  'gf-empty': 'Grafana returned no data -- likely a missing ' +
    'datasource or a query that needs a fix.',
  'both-empty': 'Neither side returned data for this range.',
  'nr-error': 'The New Relic query failed to run.',
  'gf-error': 'The Grafana query failed to run.' };
/* Short, friendly label for a compare agreement badge. */
var VERDICT_LBL = {
  match: 'match', close: 'close', 'value-mismatch': 'values differ',
  'shape-mismatch': 'shape differs', 'nr-empty': 'NR: no data',
  'gf-empty': 'Grafana: no data', 'both-empty': 'no data',
  'nr-error': 'NR error', 'gf-error': 'Grafana error' };
/* Verdicts that count as agreement (hidden by "only disagreements"). */
var AGREE_OK = { match: 1, close: 1 };
var SEV_CLS = { blocker: 'err', warn: 'warn', info: 'info' };
var SEV_ORDER = { blocker: 0, warn: 1, info: 2 };
var REVIEW_CLS = { confirmed: 'ok', rejected: 'err', unsure: 'dim' };
var REVIEW_LBL = { confirmed: 'confirmed', rejected: 'rejected',
                   unsure: 'not sure' };

function chip(text, cls, title) {
  return '<span class="chip ' + (cls || 'dim') + '"' +
    (title ? ' title="' + esc(title) + '"' : '') + '>' +
    esc(text) + '</span>';
}

function confChips(counts) {
  var order = ['exact', 'approximate', 'needs-review',
               'untranslatable'];
  var html = '';
  order.forEach(function (k) {
    if (counts && counts[k]) {
      html += chip(counts[k] + ' ' + k, CONF_CLS[k]);
    }
  });
  return html || chip('no panels', 'dim');
}

function confChip(c) { return chip(c || '?', CONF_CLS[c] || 'dim'); }

function testChip(s) {
  if (!s) return chip('not tested', 'dim');
  return chip(s, TEST_CLS[s] || 'dim');
}

function verdictChip(v, ratio, detail) {
  if (!v) return chip('no parity', 'dim');
  var label = v;
  if (ratio != null && isFinite(ratio) && v !== 'match') {
    label += ' (x' + fmtRatio(ratio) + ')';
  }
  return chip(label, VERDICT_CLS[v] || 'dim',
              detail || VERDICT_HELP[v] || '');
}

function fmtRatio(r) {
  if (r >= 100) return String(Math.round(r));
  if (r >= 10) return r.toFixed(1);
  return r.toFixed(2);
}

function sevChip(s) { return chip(s || 'info', SEV_CLS[s] || 'info'); }

function reviewChip(v, note) {
  if (!v) return '';
  return chip(REVIEW_LBL[v] || v, REVIEW_CLS[v] || 'dim', note || '');
}

/* Worst human verdict across a panel's targets (for the row badge):
   rejected > unsure > confirmed. */
function worstReview(rmap) {
  var worst = '';
  Object.keys(rmap || {}).forEach(function (k) {
    var v = (rmap[k] || {}).verdict;
    if (v === 'rejected') worst = 'rejected';
    else if (v === 'unsure' && worst !== 'rejected') worst = 'unsure';
    else if (v === 'confirmed' && !worst) worst = 'confirmed';
  });
  return worst;
}

function dsChips(list) {
  return (list || []).filter(Boolean).map(function (d) {
    return chip(d, 'info');
  }).join('') || chip('none', 'dim');
}

/* ====================================================== ring */
function ring(score, grade, size) {
  size = size || 64;
  var stroke = size >= 56 ? 5 : 4;
  var r = (size - stroke * 2) / 2;
  var c = 2 * Math.PI * r;
  var have = score != null && isFinite(score);
  var pct = have ? Math.max(0, Math.min(100, score)) / 100 : 0;
  var cls = !have ? 'dim' :
    grade === 'ready' ? 'ok' :
    grade === 'almost' ? 'warn' :
    grade === 'blocked' ? 'err' :
    score >= 90 ? 'ok' : score >= 60 ? 'warn' : 'err';
  var mid = size / 2;
  return '<div class="ring ' + cls + '" role="img" aria-label="' +
    'readiness ' + (have ? Math.round(score) : 'unknown') +
    '" style="width:' + size + 'px;height:' + size + 'px">' +
    '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' +
    size + ' ' + size + '">' +
    '<circle class="track" cx="' + mid + '" cy="' + mid + '" r="' +
    r + '" stroke-width="' + stroke + '"></circle>' +
    '<circle class="arc" cx="' + mid + '" cy="' + mid + '" r="' + r +
    '" stroke-width="' + stroke + '" stroke-dasharray="' +
    (c * pct).toFixed(1) + ' ' + c.toFixed(1) + '"></circle>' +
    '</svg><div class="ring-num" style="font-size:' +
    Math.max(11, Math.round(size / 4)) + 'px">' +
    (have ? Math.round(score) : '&ndash;') + '</div></div>';
}

/* ====================================================== sparkline */
function normPts(arr) {
  var out = [];
  (arr || []).forEach(function (p) {
    if (Array.isArray(p) && p.length >= 2) {
      var t = Number(p[0]), v = Number(p[1]);
      if (isFinite(t) && isFinite(v)) out.push([t, v]);
    } else if (typeof p === 'number' && isFinite(p)) {
      out.push([out.length, p]);
    }
  });
  return out;
}

/* Points arrays may live in several places depending on how the
   parity report was generated; try them all, defensively. */
function pickPoints(row, side) {
  if (!row) return null;
  var direct = row[side + '_points'];
  if (Array.isArray(direct)) {
    var d = normPts(direct);
    if (d.length) return d;
  }
  var s = row[side + '_summary'];
  if (s && typeof s === 'object') {
    if (Array.isArray(s.points)) {
      var p = normPts(s.points);
      if (p.length) return p;
    }
    if (Array.isArray(s.sample)) {
      var q = normPts(s.sample);
      if (q.length) return q;
    }
    if (Array.isArray(s.series) && s.series.length &&
        s.series[0] && Array.isArray(s.series[0].points)) {
      var r = normPts(s.series[0].points);
      if (r.length) return r;
    }
  }
  return null;
}

function sparkline(points, opts) {
  opts = opts || {};
  var w = opts.w || 170, h = opts.h || 40, pad = 3;
  if (!points || !points.length) {
    return '<div class="spark-empty">no data points</div>';
  }
  var xs = points.map(function (p) { return p[0]; });
  var ys = points.map(function (p) { return p[1]; });
  var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
  var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
  if (x1 === x0) { x1 = x0 + 1; }
  if (y1 === y0) { y0 -= 1; y1 += 1; }
  var pts = points.map(function (p) {
    var x = pad + (p[0] - x0) / (x1 - x0) * (w - pad * 2);
    var y = h - pad - (p[1] - y0) / (y1 - y0) * (h - pad * 2);
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  var last = points[points.length - 1][1];
  var single = points.length === 1;
  return '<div class="spark">' +
    '<svg viewBox="0 0 ' + w + ' ' + h + '" height="' + h +
    '" preserveAspectRatio="none" role="img" aria-label="' +
    points.length + ' points, last value ' + esc(fmtNum(last)) +
    '">' +
    (single ?
      '<circle cx="' + (w / 2) + '" cy="' + (h / 2) +
      '" r="3" fill="currentColor" opacity=".85"></circle>' :
      '<polyline fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linejoin="round" ' +
      'stroke-linecap="round" opacity=".85" points="' + pts +
      '"></polyline>') +
    '</svg><span class="last">' + fmtNum(last) + '</span></div>';
}

function statStrip(sum) {
  sum = sum || {};
  var cells = [
    ['series', sum.series], ['points', sum.points],
    ['min', sum.min], ['mean', sum.mean], ['max', sum.max],
    ['last', sum.last]];
  var html = cells.filter(function (c) {
    return c[1] != null;
  }).map(function (c) {
    return '<div class="st"><div class="v">' + fmtNum(c[1]) +
      '</div><div class="k">' + esc(c[0]) + '</div></div>';
  }).join('');
  return html ? '<div class="statstrip">' + html + '</div>' : '';
}

/* One side (NR or Grafana) of the side-by-side comparison. */
function sideCard(title, row, side) {
  var body;
  if (!row) {
    body = '<div class="spark-empty">run Parity to compare' +
      '</div>';
  } else {
    var verdict = row.verdict || '';
    var errHere = verdict === side + '-error';
    if (errHere) {
      body = '<div class="spark-err">' + esc(row.detail || 'error') +
        '</div>';
    } else {
      var pts = pickPoints(row, side);
      var sum = row[side + '_summary'] || {};
      if (pts && pts.length > 1) {
        body = sparkline(pts) + statStrip(sum);
      } else if (pts && pts.length === 1) {
        body = '<div class="statstrip"><div class="st">' +
          '<div class="v" style="font-size:18px">' +
          fmtNum(pts[0][1]) + '</div>' +
          '<div class="k">value</div></div></div>' + statStrip(sum);
      } else if (sum.points) {
        body = statStrip(sum);
      } else {
        body = '<div class="spark-empty">no data</div>';
      }
    }
  }
  return '<div class="side-card"><div class="side-head">' +
    esc(title) + '</div>' + body + '</div>';
}

/* ============================================== raw samples */
function fmtTs(t) {
  if (t == null || !isFinite(t)) return '';
  var d = new Date(t > 1e11 ? t : t * 1000);
  if (isNaN(d.getTime())) return '';
  return d.toISOString().replace('T', ' ').slice(5, 19);
}

function sampleLogsHtml(samples) {
  return '<div class="sample-lines">' +
    (samples || []).map(function (s) {
      return '<div>' + (s.ts ? '<span class="ts">' + esc(s.ts) +
        '</span>' : '') + esc(s.line || '') + '</div>';
    }).join('') + '</div>';
}

function sampleEventsHtml(samples) {
  return '<div class="sample-lines">' +
    (samples || []).map(function (r) {
      var ts = r.timestamp != null ? fmtTs(Number(r.timestamp)) : '';
      var line;
      if (r.message != null) {
        line = String(r.message);
      } else {
        line = Object.keys(r).filter(function (k) {
          return k !== 'timestamp';
        }).map(function (k) {
          return k + '=' + r[k];
        }).join(' ');
      }
      return '<div>' + (ts ? '<span class="ts">' + esc(ts) +
        '</span>' : '') + esc(line) + '</div>';
    }).join('') + '</div>';
}

function samplePointsHtml(seriesList) {
  return (seriesList || []).map(function (s) {
    var lbl = Object.keys(s.labels || {}).map(function (k) {
      return k + '=' + s.labels[k];
    }).join(', ');
    var rows = (s.points || []).map(function (p) {
      return '<tr><td class="kv">' + esc(fmtTs(p[0])) +
        '</td><td class="mono">' + fmtNum(p[1]) + '</td></tr>';
    }).join('');
    return (lbl ? '<div class="kv mono">' + esc(lbl) + '</div>' :
      '') + '<table class="sample-tbl"><tbody>' + rows +
      '</tbody></table>';
  }).join('');
}

function sampleRowsHtml(frames) {
  return (frames || []).map(function (f) {
    var head = (f.fields || []).map(function (n) {
      return '<th>' + esc(n) + '</th>'; }).join('');
    var rows = (f.rows || []).map(function (r) {
      return '<tr>' + r.map(function (v) {
        return '<td class="mono">' + esc(v == null ? '' : v) +
          '</td>';
      }).join('') + '</tr>';
    }).join('');
    return '<div class="tablewrap"><table class="sample-tbl">' +
      '<thead><tr>' + head + '</tr></thead><tbody>' + rows +
      '</tbody></table></div>';
  }).join('');
}

/* One side (NR or Grafana) of the raw-sample comparison. */
function sampleCard(title, side) {
  var body, kindChip = '';
  if (!side) {
    body = '<div class="spark-empty">no samples pulled yet</div>';
  } else {
    kindChip = ' ' + chip(side.kind || '?', 'dim');
    var samples = side.samples || [];
    if (side.kind === 'error') {
      body = '<div class="spark-err">' + esc(side.error || 'error') +
        '</div>';
    } else if (!samples.length) {
      body = '<div class="spark-empty">' +
        esc(side.error || 'no data in this range') + '</div>';
    } else if (side.kind === 'logs') {
      body = sampleLogsHtml(samples);
    } else if (side.kind === 'events') {
      body = sampleEventsHtml(samples);
    } else if (side.kind === 'points') {
      body = samplePointsHtml(samples);
    } else if (side.kind === 'rows' && samples[0] &&
               samples[0].fields) {
      body = sampleRowsHtml(samples);
    } else {
      body = sampleEventsHtml(samples);
    }
  }
  return '<div class="side-card"><div class="side-head">' +
    esc(title) + kindChip + '</div>' + body + '</div>';
}

/* "Is this what you expect?" confirm/reject bar under the samples. */
function signoffHtml(pid, ref, rv) {
  var current = rv ?
    '<span style="margin-left:auto">' +
    reviewChip(rv.verdict, rv.note) +
    '</span>' : '';
  return '<div class="signoff">' +
    '<span class="q">Is this what you expect?</span>' +
    btnA('rv-confirmed', pid, ref, 'Confirm',
         rv && rv.verdict === 'confirmed' ? 'primary' : '') +
    btnA('rv-rejected', pid, ref, 'Reject', 'danger') +
    btnA('rv-unsure', pid, ref, 'Not sure') +
    '<input id="rvnote-' + pid + '-' + ref + '" placeholder=' +
    '"optional note (what is wrong / what you checked)" ' +
    'autocomplete="off" value="' + esc((rv && rv.note) || '') +
    '">' + current + '</div>';
}

/* =================================================== charts (3a) */
/* A tiny inline-SVG charting layer -- no libraries, no CDN. Every
   chart is theme-aware (CSS vars for palette/axes), responsive
   (fixed viewBox + width:100%), formats numbers human-friendly and
   NEVER renders blank: empty/error states get a clean placeholder.
   chart(kind, data, opts) -> HTML string. Timeseries charts also
   register their geometry in CHARTS for hover tooltips + sync. */
var SERIES_COLORS = ['var(--series-1)', 'var(--series-2)',
  'var(--series-3)', 'var(--series-4)', 'var(--series-5)',
  'var(--series-6)', 'var(--series-7)', 'var(--series-8)'];
function seriesColor(i) {
  return SERIES_COLORS[i % SERIES_COLORS.length];
}

/* Human duration from milliseconds. */
function fmtDur(ms) {
  if (ms == null || !isFinite(ms)) return '–';
  var a = Math.abs(ms);
  if (a === 0) return '0';
  if (a < 1) return Math.round(ms * 1000) + 'µs';
  if (a < 1000) return (Math.round(ms * 10) / 10) + 'ms';
  var s = ms / 1000;
  if (a < 60000) return (Math.round(s * 100) / 100) + 's';
  var m = Math.floor(Math.abs(s) / 60), rem = Math.round(Math.abs(s) % 60);
  if (a < 3600000) return (s < 0 ? '-' : '') + m + 'm ' + rem + 's';
  var h = Math.floor(Math.abs(s) / 3600),
      mm = Math.floor((Math.abs(s) % 3600) / 60);
  return (s < 0 ? '-' : '') + h + 'h ' + mm + 'm';
}

/* Human bytes (binary). */
function fmtBytes(v) {
  if (v == null || !isFinite(v)) return '–';
  var u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'], i = 0;
  while (Math.abs(v) >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (Math.round(v * 100) / 100) + ' ' + u[i];
}

/* Format a value with unit awareness (%, ms/s durations, bytes, SI). */
function fmtUnit(v, unit) {
  if (v == null || !isFinite(v)) return '–';
  unit = String(unit || '').toLowerCase();
  if (unit === 'usd' || unit === '$' || unit === 'dollars') {
    return '$' + fmtNumP(v);
  }
  if (unit === 'percent' || unit === '%') {
    return (Math.round(v * 10) / 10) + '%';
  }
  if (unit === 'percentunit') return (Math.round(v * 1000) / 10) + '%';
  if (unit === 's' || unit === 'seconds') return fmtDur(v * 1000);
  if (unit === 'ms' || unit === 'milliseconds') return fmtDur(v);
  if (unit === 'ns' || unit === 'nanoseconds') return fmtDur(v / 1e6);
  if (unit.indexOf('byte') === 0 || unit === 'decbytes' ||
      unit === 'bytes/sec') {
    return fmtBytes(v) + (unit.indexOf('/sec') >= 0 ? '/s' : '');
  }
  var s = fmtNumP(v);
  return unit ? s + ' ' + unit : s;
}

/* Plain-text (no HTML entity) variant of fmtNum for SVG/axis text. */
function fmtNumP(v) {
  if (v == null || !isFinite(v)) return '–';
  var a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(1) + 'T';
  if (a >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (a >= 1e4) return (v / 1e3).toFixed(1) + 'k';
  if (a >= 100) return String(Math.round(v));
  if (a === 0) return '0';
  if (a >= 1) return String(Math.round(v * 100) / 100);
  return Number(v.toPrecision(3)).toString();
}

/* Clock label HH:MM from an epoch-seconds (or epoch-ms) timestamp. */
function fmtClock(t) {
  if (t == null || !isFinite(t)) return '';
  var d = new Date(t > 1e11 ? t : t * 1000);
  if (isNaN(d.getTime())) return '';
  return ('0' + d.getHours()).slice(-2) + ':' +
         ('0' + d.getMinutes()).slice(-2);
}

/* Defensive downsample: keep <= max points, always keep the last. */
function stridePts(pts, max) {
  pts = pts || [];
  if (pts.length <= max) return pts;
  var step = Math.ceil(pts.length / max), out = [];
  for (var i = 0; i < pts.length; i += step) out.push(pts[i]);
  if (out[out.length - 1] !== pts[pts.length - 1]) {
    out.push(pts[pts.length - 1]);
  }
  return out;
}

/* In-panel placeholder -- shown instead of a blank chart. */
function chartEmpty(msg, kind) {
  var cls = kind === 'err' ? ' err' : kind === 'warn' ? ' warn' : '';
  var icon = kind === 'err' ? 'alert' : 'inbox';
  return '<div class="chart-empty' + cls + '">' + ico(icon, 20) +
    '<span>' + esc(msg || 'No data') + '</span></div>';
}

/* Threshold color class (ok/warn/err) for a stat/gauge value.
   opts.thresholds = [{value, level}] ascending; highest match wins. */
function thresholdClass(v, thresholds) {
  if (v == null || !isFinite(v) || !thresholds ||
      !thresholds.length) return '';
  var cls = '';
  thresholds.forEach(function (t) {
    if (v >= t.value) cls = t.level || '';
  });
  return cls;
}

/* Public entry point. kind mirrors the Grafana panel viz so both
   sides of Compare draw the same chart. data is a "side" shape
   {kind, series, scalar, rows, lines, unit, error} or the loose
   {series}/{scalar}/... a caller passes directly. */
function chart(kind, data, opts) {
  opts = opts || {};
  data = data || {};
  if (data.kind === 'error' || data.error) {
    return chartEmpty(data.error || 'query error', 'err');
  }
  var unit = opts.unit != null ? opts.unit : (data.unit || '');
  kind = kind || 'timeseries';
  if (kind === 'text' || kind === 'unsupported' || kind === 'row') {
    return chartEmpty(opts.placeholder || 'Not a data panel');
  }
  if (kind === 'table') return chTable(data.rows, opts);
  if (kind === 'logs') return chLogs(data.lines, opts);
  if (kind === 'stat' || kind === 'billboard') {
    return chStat(data, unit, opts);
  }
  if (kind === 'gauge' || kind === 'bargauge') {
    return chGauge(data, unit, opts);
  }
  if (kind === 'bar' || kind === 'barchart') {
    return chBar(data.series, unit, opts);
  }
  if (kind === 'piechart' || kind === 'pie') {
    return chPie(data.series, unit, opts);
  }
  return chTimeseries(data.series, unit, opts);
}

/* nearest point (by time) in a [[t,v],...] array */
function nearestPt(pts, t) {
  if (!pts || !pts.length) return null;
  var best = pts[0], bd = Math.abs(pts[0][0] - t);
  for (var i = 1; i < pts.length; i++) {
    var d = Math.abs(pts[i][0] - t);
    if (d < bd) { bd = d; best = pts[i]; }
  }
  return best;
}

function chTimeseries(series, unit, opts) {
  var max = opts.maxPoints || 150;
  var norm = (series || []).filter(function (s) {
    return s && (s.points || []).length;
  }).slice(0, 8).map(function (s, i) {
    return { name: s.name || ('series ' + (i + 1)),
             color: seriesColor(i),
             pts: stridePts(normPts(s.points), max) };
  }).filter(function (s) { return s.pts.length; });
  if (!norm.length) return chartEmpty('No data in this range');
  var W = 480, H = opts.height || 150;
  var pl = 44, pr = 10, ptop = 8, pb = 20;
  var pw = W - pl - pr, ph = H - ptop - pb;
  var xs = [], ys = [];
  norm.forEach(function (s) {
    s.pts.forEach(function (p) { xs.push(p[0]); ys.push(p[1]); });
  });
  var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
  var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
  if (x1 === x0) x1 = x0 + 1;
  if (y1 === y0) { var d = Math.abs(y0) || 1; y0 -= d * 0.5;
                   y1 += d * 0.5; }
  var ypad = (y1 - y0) * 0.08; y0 -= ypad; y1 += ypad;
  if (y0 > 0 && y0 / (y1 - y0) < 0.35) y0 = 0;
  function X(t) { return pl + (t - x0) / (x1 - x0) * pw; }
  function Y(v) { return ptop + ph - (v - y0) / (y1 - y0) * ph; }
  var grid = '', i, gy, gv, gx;
  for (i = 0; i <= 4; i++) {
    gv = y0 + (i / 4) * (y1 - y0); gy = Y(gv);
    grid += '<line class="ch-grid" x1="' + pl + '" y1="' +
      gy.toFixed(1) + '" x2="' + (W - pr) + '" y2="' + gy.toFixed(1) +
      '"></line><text class="ch-tick" x="' + (pl - 5) + '" y="' +
      (gy + 3).toFixed(1) + '" text-anchor="end">' +
      esc(fmtUnit(gv, unit)) + '</text>';
  }
  var nt = Math.min(5, norm[0].pts.length);
  for (i = 0; i < nt; i++) {
    var tt = x0 + (i / (nt - 1 || 1)) * (x1 - x0); gx = X(tt);
    grid += '<text class="ch-tick" x="' + gx.toFixed(1) + '" y="' +
      (H - 6) + '" text-anchor="' +
      (i === 0 ? 'start' : i === nt - 1 ? 'end' : 'middle') + '">' +
      esc(fmtClock(tt)) + '</text>';
  }
  var lines = norm.map(function (s) {
    var pstr = s.pts.map(function (p) {
      return X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1);
    }).join(' ');
    if (s.pts.length === 1) {
      return '<circle cx="' + X(s.pts[0][0]).toFixed(1) + '" cy="' +
        Y(s.pts[0][1]).toFixed(1) + '" r="3" fill="' + s.color +
        '"></circle>';
    }
    return '<polyline class="ch-line" stroke="' + s.color +
      '" points="' + pstr + '"></polyline>';
  }).join('');
  var axis = '<line class="ch-axis" x1="' + pl + '" y1="' + ptop +
    '" x2="' + pl + '" y2="' + (ptop + ph) + '"></line>' +
    '<line class="ch-axis" x1="' + pl + '" y1="' + (ptop + ph) +
    '" x2="' + (W - pr) + '" y2="' + (ptop + ph) + '"></line>';
  var legend = norm.length > 1 ? '<div class="chart-legend">' +
    norm.map(function (s) {
      return '<span class="lg"><span class="sw" style="background:' +
        s.color + '"></span>' + esc(s.name) + '</span>';
    }).join('') + '</div>' : '';
  var id = 'ch-' + uid();
  CHARTS[id] = { kind: 'timeseries', W: W, pl: pl, pr: pr,
    x0: x0, x1: x1, X: X, Y: Y, series: norm, unit: unit,
    partner: '' };
  return '<div class="chart" id="' + id + '" data-tsid="' + id +
    '"><svg viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
    'aria-label="time series chart">' + grid + axis + lines +
    '<line class="ch-guide" x1="0" x2="0" y1="' + ptop + '" y2="' +
    (ptop + ph) + '"></line><g class="ch-dots"></g></svg>' +
    '<div class="chart-tip"></div>' + legend + '</div>';
}

function chBar(series, unit, opts) {
  var list = (series || []).filter(function (s) {
    return s && (s.points || []).length;
  });
  if (!list.length) return chartEmpty('No data');
  var cats;
  if (list.length === 1) {
    cats = stridePts(normPts(list[0].points), 24).map(function (p) {
      return { label: fmtClock(p[0]), value: p[1] };
    });
  } else {
    cats = list.slice(0, 12).map(function (s) {
      var p = normPts(s.points);
      return { label: s.name || '',
               value: p.length ? p[p.length - 1][1] : 0 };
    });
  }
  var W = 480, H = opts.height || 150;
  var pl = 44, pr = 10, ptop = 8, pb = 22;
  var pw = W - pl - pr, ph = H - ptop - pb;
  var vals = cats.map(function (c) { return c.value; });
  var y1 = Math.max.apply(null, vals);
  var y0 = Math.min.apply(null, vals.concat([0]));
  if (y1 === y0) y1 = y0 + 1;
  function Y(v) { return ptop + ph - (v - y0) / (y1 - y0) * ph; }
  var n = cats.length, gap = pw / n * 0.24, bw = pw / n - gap;
  var grid = '', i, gy, gv;
  for (i = 0; i <= 4; i++) {
    gv = y0 + (i / 4) * (y1 - y0); gy = Y(gv);
    grid += '<line class="ch-grid" x1="' + pl + '" y1="' +
      gy.toFixed(1) + '" x2="' + (W - pr) + '" y2="' + gy.toFixed(1) +
      '"></line><text class="ch-tick" x="' + (pl - 5) + '" y="' +
      (gy + 3).toFixed(1) + '" text-anchor="end">' +
      esc(fmtUnit(gv, unit)) + '</text>';
  }
  var bars = cats.map(function (c, k) {
    var x = pl + k * (pw / n) + gap / 2;
    var yv = Y(c.value), y00 = Y(Math.min(0, y1) > 0 ? y0 : 0);
    var top = Math.min(yv, Y(0)), hh = Math.abs(yv - Y(0));
    var lbl = n <= 12 ? '<text class="ch-tick" x="' +
      (x + bw / 2).toFixed(1) + '" y="' + (H - 7) +
      '" text-anchor="middle">' + esc(String(c.label).slice(0, 6)) +
      '</text>' : '';
    return '<rect class="ch-bar" x="' + x.toFixed(1) + '" y="' +
      top.toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' +
      Math.max(1, hh).toFixed(1) + '" fill="' + seriesColor(k) +
      '"><title>' + esc(c.label + ': ' + fmtUnit(c.value, unit)) +
      '</title></rect>' + lbl;
  }).join('');
  return '<div class="chart"><svg viewBox="0 0 ' + W + ' ' + H +
    '" role="img" aria-label="bar chart">' + grid + bars +
    '</svg></div>';
}

function chStat(data, unit, opts) {
  var v = data.scalar;
  if (v == null && data.series && data.series.length) {
    var p = normPts(data.series[0].points);
    if (p.length) v = p[p.length - 1][1];
  }
  if (v == null || !isFinite(v)) {
    return chartEmpty(opts.placeholder || 'No value');
  }
  var cls = thresholdClass(v, opts.thresholds);
  var spark = '';
  var s0 = (data.series || [])[0];
  if (s0 && (s0.points || []).length > 1) {
    var pts = stridePts(normPts(s0.points), 80);
    var W = 240, H = 34;
    var xs = pts.map(function (q) { return q[0]; });
    var ys = pts.map(function (q) { return q[1]; });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    if (x1 === x0) x1 = x0 + 1;
    if (y1 === y0) { y0 -= 1; y1 += 1; }
    var poly = pts.map(function (q) {
      return (2 + (q[0] - x0) / (x1 - x0) * (W - 4)).toFixed(1) + ',' +
        (H - 2 - (q[1] - y0) / (y1 - y0) * (H - 4)).toFixed(1);
    }).join(' ');
    spark = '<svg class="cs-spark" viewBox="0 0 ' + W + ' ' + H +
      '" preserveAspectRatio="none" height="' + H +
      '"><polyline fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linejoin="round" points="' + poly +
      '"></polyline></svg>';
  }
  var big = fmtUnit(v, unit);
  var num = big, un = '';
  var m = /^(-?[0-9.,]+)\s*(.*)$/.exec(big);
  if (m && m[2]) { num = m[1]; un = m[2]; }
  return '<div class="chart-stat"><div><span class="cs-num ' + cls +
    '">' + esc(num) + '</span>' +
    (un ? '<span class="cs-unit">' + esc(un) + '</span>' : '') +
    '</div>' + spark + '</div>';
}

function chGauge(data, unit, opts) {
  var v = data.scalar;
  if (v == null && data.series && data.series.length) {
    var p = normPts(data.series[0].points);
    if (p.length) v = p[p.length - 1][1];
  }
  if (v == null || !isFinite(v)) return chartEmpty('No value');
  var lo = opts.min != null ? opts.min : 0;
  var hi = opts.max != null ? opts.max :
    (v <= 1 ? 1 : Math.pow(10, Math.ceil(Math.log10(v * 1.1 || 1))));
  if (hi <= lo) hi = lo + 1;
  var frac = Math.max(0, Math.min(1, (v - lo) / (hi - lo)));
  var cls = thresholdClass(v, opts.thresholds);
  var col = cls === 'err' ? 'var(--red)' : cls === 'warn' ?
    'var(--amber)' : cls === 'ok' ? 'var(--green)' : 'var(--accent)';
  var W = 240, H = 132, cx = W / 2, cy = 116, r = 92;
  function pt(a) {
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  }
  var a0 = Math.PI, a1 = 2 * Math.PI;
  var av = a0 + frac * (a1 - a0);
  function arc(from, to, color, width) {
    var s = pt(from), e = pt(to);
    var large = (to - from) > Math.PI ? 1 : 0;
    return '<path d="M' + s[0].toFixed(1) + ' ' + s[1].toFixed(1) +
      ' A' + r + ' ' + r + ' 0 ' + large + ' 1 ' + e[0].toFixed(1) +
      ' ' + e[1].toFixed(1) + '" fill="none" stroke="' + color +
      '" stroke-width="' + width + '" stroke-linecap="round"></path>';
  }
  return '<div class="chart"><svg viewBox="0 0 ' + W + ' ' + H +
    '" role="img" aria-label="gauge">' +
    arc(a0, a1, 'var(--bg4)', 14) +
    (frac > 0 ? arc(a0, av, col, 14) : '') +
    '<text x="' + cx + '" y="' + (cy - 18) +
    '" text-anchor="middle" style="fill:var(--text);font:700 22px ' +
    'var(--sans)">' + esc(fmtUnit(v, unit)) + '</text>' +
    '<text class="ch-tick" x="' + (cx - r) + '" y="' + (cy + 16) +
    '" text-anchor="middle">' + esc(fmtNumP(lo)) + '</text>' +
    '<text class="ch-tick" x="' + (cx + r) + '" y="' + (cy + 16) +
    '" text-anchor="middle">' + esc(fmtNumP(hi)) + '</text>' +
    '</svg></div>';
}

function chTable(rows, opts) {
  rows = rows || [];
  if (!rows.length) return chartEmpty('No rows');
  var cols = [];
  rows.forEach(function (r) {
    Object.keys(r || {}).forEach(function (k) {
      if (cols.indexOf(k) < 0) cols.push(k);
    });
  });
  if (!cols.length) return chartEmpty('No rows');
  cols = cols.slice(0, 8);
  var head = cols.map(function (c) {
    return '<th>' + esc(c) + '</th>'; }).join('');
  var body = rows.slice(0, 60).map(function (r) {
    return '<tr>' + cols.map(function (c) {
      var val = r[c];
      if (typeof val === 'number') val = fmtNumP(val);
      return '<td class="mono">' + esc(val == null ? '' : val) +
        '</td>';
    }).join('') + '</tr>';
  }).join('');
  return '<div class="chart-table"><table class="zebra"><thead><tr>' +
    head + '</tr></thead><tbody>' + body + '</tbody></table></div>';
}

function chLogs(lines, opts) {
  lines = lines || [];
  if (!lines.length) return chartEmpty('No log lines');
  return '<div class="chart-logs">' + lines.slice(0, 200).map(
    function (l) {
      var ts = l.ts ? '<span class="lt">' +
        esc(String(l.ts).replace('T', ' ').slice(0, 19)) + '</span>'
        : '';
      return '<div>' + ts + esc(l.line == null ? '' : l.line) +
        '</div>';
    }).join('') + '</div>';
}

function chPie(series, unit, opts) {
  var slices = (series || []).map(function (s, i) {
    var p = normPts(s.points);
    return { name: s.name || ('slice ' + (i + 1)),
             value: p.length ? Math.abs(p[p.length - 1][1]) : 0,
             color: seriesColor(i) };
  }).filter(function (s) { return s.value > 0; });
  if (!slices.length) return chartEmpty('No data');
  var total = slices.reduce(function (a, s) { return a + s.value; }, 0);
  if (total <= 0) return chartEmpty('No data');
  var W = 300, H = 150, cx = 75, cy = 75, r = 66;
  var ang = -Math.PI / 2, paths = '';
  slices.forEach(function (s) {
    var frac = s.value / total, a2 = ang + frac * 2 * Math.PI;
    var large = frac > 0.5 ? 1 : 0;
    if (frac >= 0.999) {
      paths += '<circle cx="' + cx + '" cy="' + cy + '" r="' + r +
        '" class="ch-slice" fill="' + s.color + '"></circle>';
    } else {
      var x1 = cx + r * Math.cos(ang), y1 = cy + r * Math.sin(ang);
      var x2 = cx + r * Math.cos(a2), y2 = cy + r * Math.sin(a2);
      paths += '<path class="ch-slice" d="M' + cx + ' ' + cy + ' L' +
        x1.toFixed(1) + ' ' + y1.toFixed(1) + ' A' + r + ' ' + r +
        ' 0 ' + large + ' 1 ' + x2.toFixed(1) + ' ' + y2.toFixed(1) +
        ' Z" fill="' + s.color + '"><title>' +
        esc(s.name + ': ' + fmtUnit(s.value, unit)) +
        '</title></path>';
    }
    ang = a2;
  });
  var legend = '<div class="chart-legend" style="flex-direction:' +
    'column;gap:3px">' + slices.map(function (s) {
      return '<span class="lg"><span class="sw" style="background:' +
        s.color + '"></span>' + esc(s.name) + ' · ' +
        esc(fmtUnit(s.value, unit)) + '</span>';
    }).join('') + '</div>';
  return '<div class="chart" style="display:flex;align-items:center;' +
    'gap:8px"><svg viewBox="0 0 150 150" style="width:150px;flex:' +
    '0 0 150px" role="img" aria-label="pie chart">' + paths +
    '</svg>' + legend + '</div>';
}

/* Wire hover tooltips for every timeseries chart under root. Safe to
   call repeatedly -- already-bound charts are skipped. */
function mountCharts(root) {
  $all('.chart[data-tsid]', root || document).forEach(function (el) {
    var id = el.getAttribute('data-tsid');
    var rec = CHARTS[id];
    if (!rec || rec.bound) return;
    rec.bound = true;
    var svg = $('svg', el);
    if (!svg) return;
    svg.addEventListener('mousemove', function (ev) {
      chHover(id, ev.clientX);
    });
    svg.addEventListener('mouseleave', function () { chClear(id); });
  });
}

function chHover(id, clientX) {
  var rec = CHARTS[id];
  var el = document.getElementById(id);
  if (!rec || !el) return;
  var svg = $('svg', el);
  var rect = svg.getBoundingClientRect();
  if (!rect.width) return;
  var vbX = (clientX - rect.left) / rect.width * rec.W;
  var t = rec.x0 + (vbX - rec.pl) / (rec.W - rec.pl - rec.pr) *
    (rec.x1 - rec.x0);
  t = Math.max(rec.x0, Math.min(rec.x1, t));
  chShowAt(id, t, true);
  if (App.cmp && App.cmp.syncHover && rec.partner) {
    chShowAt(rec.partner, t, false);
  }
}

/* Draw the guide line + nearest-point dots (and the tooltip when
   withTip) at time t on chart id. */
function chShowAt(id, t, withTip) {
  var rec = CHARTS[id];
  var el = document.getElementById(id);
  if (!rec || !el) return;
  var gx = rec.X(t);
  var guide = $('.ch-guide', el);
  if (guide) {
    guide.setAttribute('x1', gx.toFixed(1));
    guide.setAttribute('x2', gx.toFixed(1));
    guide.classList.add('on');
  }
  var dots = '', rows = '', near = null;
  rec.series.forEach(function (s) {
    var p = nearestPt(s.pts, t);
    if (!p) return;
    dots += '<circle class="ch-dot" cx="' + rec.X(p[0]).toFixed(1) +
      '" cy="' + rec.Y(p[1]).toFixed(1) + '" r="3.2" fill="' +
      s.color + '"></circle>';
    rows += '<div class="ch-tip-row"><span class="ch-sw" style="' +
      'background:' + s.color + '"></span>' + esc(s.name) + '<b>' +
      esc(fmtUnit(p[1], rec.unit)) + '</b></div>';
    near = p;
  });
  var g = $('.ch-dots', el);
  if (g) g.innerHTML = dots;
  if (withTip && near) {
    var tip = $('.chart-tip', el);
    if (tip) {
      tip.innerHTML = '<div class="ch-tip-t">' +
        esc(fmtClock(near[0])) + '</div>' + rows;
      tip.style.display = 'block';
      var contW = el.clientWidth || rec.W;
      var px = gx / rec.W * contW, tw = tip.offsetWidth;
      var left = px + 12;
      if (left + tw > contW) left = px - tw - 12;
      if (left < 0) left = 0;
      tip.style.left = left.toFixed(0) + 'px';
      tip.style.top = '2px';
    }
  }
}

function chClear(id) {
  var rec = CHARTS[id];
  var el = document.getElementById(id);
  if (!el) return;
  var guide = $('.ch-guide', el);
  if (guide) guide.classList.remove('on');
  var g = $('.ch-dots', el);
  if (g) g.innerHTML = '';
  var tip = $('.chart-tip', el);
  if (tip) tip.style.display = 'none';
  if (rec && App.cmp && App.cmp.syncHover && rec.partner) {
    var pel = document.getElementById(rec.partner);
    if (pel) {
      var pg = $('.ch-guide', pel); if (pg) pg.classList.remove('on');
      var pd = $('.ch-dots', pel); if (pd) pd.innerHTML = '';
    }
  }
}

/* ====================================================== state/pills */
async function refreshState() {
  try {
    App.state = await api('/api/state');
    renderPills();
    var v = $('#verline');
    if (v && App.state.version) v.textContent = 'v' + App.state.version;
    var c = $('#nav-cnt');
    if (c) {
      var n = (App.state.db || {}).dashboards || 0;
      c.textContent = n ? String(n) : '';
    }
  } catch (e) { /* server briefly busy; keep old state */ }
}

function pillSet(id, status, title) {
  var el = $(id);
  el.classList.remove('ok', 'err');
  if (status === 'ok') el.classList.add('ok');
  else if (status === 'error') el.classList.add('err');
  el.title = title || '';
}

/* Active AI backend: "api" | "local" | "none". Falls back to the
   pre-1.2 key flag when the server does not send ai_backend yet. */
function aiBackend(ses) {
  ses = ses || {};
  if (ses.ai_backend) return ses.ai_backend;
  if (ses.ai_command_set) return 'local';
  return ses.anthropic_key_set ? 'api' : 'none';
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
  var backend = aiBackend(s.session);
  var lbl = $('#pill-ai-lbl');
  if (lbl) {
    lbl.textContent = backend === 'api' ? 'AI: API' :
      backend === 'local' ? 'AI: local' : 'AI';
  }
  pillSet('#pill-ai',
          backend === 'none' ? 'unset' : s.status.ai,
          d.ai || (backend === 'api' ? 'Anthropic API key set' :
                   backend === 'local' ?
                   'local console AI command configured' :
                   'no AI backend configured'));
}

/* ====================================================== router */
var VIEWS = { overview: vOverview, connect: vConnect,
              convert: vConvert, datasources: vDatasources,
              import: vImport, changes: vChanges, ai: vAI,
              cost: vCost, stack: vStack };
var ALIASES = { setup: 'connect', dashboards: 'overview',
                test: 'overview' };

function crumb(text) { $('#crumb').textContent = text; }

async function route() {
  App.timers.forEach(clearInterval); App.timers = [];
  if (App.cmpIO) { App.cmpIO.disconnect(); App.cmpIO = null; }
  CHARTS = {};
  closeFlyout(); closeModal();
  var h = location.hash.replace(/^#\/?/, '');
  if (!h) {
    h = (App.state && App.state.db &&
         App.state.db.dashboards > 0) ? 'overview' : 'connect';
  }
  var parts = h.split('/');
  var name = parts[0] || 'overview';
  var slug = '', tab = '';
  if (ALIASES[name]) {
    if (name === 'dashboards' && parts[1]) {
      name = 'dash'; slug = decodeURIComponent(parts[1]);
    } else { name = ALIASES[name]; }
  } else if (name === 'dash' && parts[1]) {
    slug = decodeURIComponent(parts[1]);
    tab = parts[2] || 'panels';
  } else if (name === 'compare' && parts[1]) {
    slug = decodeURIComponent(parts[1]);
  }
  var navKey = name === 'dash' ? 'overview' : name;
  $all('#nav a').forEach(function (a) {
    a.classList.toggle('active', a.getAttribute('data-r') === navKey);
  });
  var view = $('#view');
  try {
    if (name === 'dash' && slug) {
      await vWorkspace(view, slug, tab);
    } else if (name === 'compare') {
      await vCompare(view, slug);
    } else {
      await (VIEWS[name] || vOverview)(view);
    }
  } catch (e) {
    view.innerHTML = errorCard('This view could not load.',
                               e.message);
  }
}

/* ====================================================== flyout */
function openFlyout(title, bodyHtml, footHtml) {
  closeFlyout();
  var slot = $('#flyout-slot');
  slot.innerHTML = '<div class="flyout" role="dialog" aria-label="' +
    esc(title) + '"><div class="flyout-head"><h2>' + esc(title) +
    '</h2><button class="btn small ghost" id="fly-close" ' +
    'aria-label="Close">&#10005;</button></div>' +
    '<div class="flyout-body">' + bodyHtml + '</div>' +
    (footHtml ? '<div class="flyout-foot">' + footHtml + '</div>'
              : '') + '</div>';
  $('#overlay').classList.add('show');
  var fly = $('.flyout', slot);
  requestAnimationFrame(function () { fly.classList.add('show'); });
  $('#fly-close').onclick = closeFlyout;
  $('#overlay').onclick = closeFlyout;
  return fly;
}

function closeFlyout() {
  $('#flyout-slot').innerHTML = '';
  $('#overlay').classList.remove('show');
}

/* ====================================================== modal */
function closeModal() { $('#modal-slot').innerHTML = ''; }

/* Typed-confirm destructive action. resolve(true) only when the user
   typed `expect` exactly and confirmed. */
function typedConfirm(opts) {
  return new Promise(function (resolve) {
    var slot = $('#modal-slot');
    slot.innerHTML = '<div class="modal-wrap"><div class="modal">' +
      '<h2>' + esc(opts.title || 'Are you sure?') + '</h2>' +
      '<p class="kv">' + (opts.html || '') + '</p>' +
      '<label>Type <b class="mono">' + esc(opts.expect) +
      '</b> to confirm</label>' +
      '<input id="tc-input" autocomplete="off" spellcheck="false">' +
      '<div class="btnbar" style="justify-content:flex-end">' +
      '<button class="btn" id="tc-cancel">Cancel</button>' +
      '<button class="btn danger" id="tc-ok" disabled>' +
      esc(opts.action || 'Delete') + '</button></div></div></div>';
    var input = $('#tc-input'), ok = $('#tc-ok');
    function done(v) { closeModal(); resolve(v); }
    input.oninput = function () {
      ok.disabled = input.value !== opts.expect;
    };
    input.onkeydown = function (ev) {
      if (ev.key === 'Enter' && !ok.disabled) done(true);
      if (ev.key === 'Escape') done(false);
    };
    ok.onclick = function () { done(true); };
    $('#tc-cancel').onclick = function () { done(false); };
    $('.modal-wrap', slot).onclick = function (ev) {
      if (ev.target === this) done(false);
    };
    input.focus();
  });
}

/* ====================================================== stepper */
function stepStates(d) {
  var s = App.state || {}, st = (s.status || {});
  var out = {};
  out.connect = st.grafana === 'ok' ?
    (st.newrelic === 'error' ? 'attn' : 'done') :
    st.grafana === 'error' ? 'blocked' : 'attn';
  out.fetch = d ? 'done' : 'todo';
  out.convert = d ? 'done' : 'todo';
  var check = ((d || {}).check || {}).items || [];
  if (!check.length) { out.datasources = 'todo'; }
  else {
    var missing = check.some(function (i) {
      return i.status === 'missing'; });
    var warn = check.some(function (i) { return i.status !== 'ok'; });
    out.datasources = missing ? 'blocked' : warn ? 'attn' : 'done';
  }
  var ts = ((d || {}).datatest || {}).summary || {};
  if (!Object.keys(ts).length) { out.validate = 'todo'; }
  else if (ts.error) { out.validate = 'blocked'; }
  else if (ts['no-data']) { out.validate = 'attn'; }
  else { out.validate = 'done'; }
  var fs = ((d || {}).diagnosis || {}).summary || {};
  if (!(d || {}).diagnosis) { out.fix = 'todo'; }
  else if (fs.blocker) { out.fix = 'blocked'; }
  else if (fs.warn) { out.fix = 'attn'; }
  else { out.fix = 'done'; }
  var imported = ((d || {}).changes || []).some(function (c) {
    return c.action === 'import' || c.action === 'dashboard-updated';
  });
  out.import = imported ? 'done' : 'todo';
  var par = (d || {}).parity;
  out.verify = !par ? 'todo' :
    par.score >= 90 ? 'done' : par.score >= 60 ? 'attn' : 'blocked';
  out.download = (out.verify === 'done' ||
                  (out.verify === 'attn' && out.fix !== 'blocked')) ?
    'done' : 'todo';
  return out;
}

function stepper(slug, d, activeKey) {
  var base = '#/dash/' + encodeURIComponent(slug);
  var steps = [
    ['connect', 'Connect', '#/connect'],
    ['fetch', 'Fetch', '#/convert'],
    ['convert', 'Convert', '#/convert'],
    ['datasources', 'Datasources', '#/datasources'],
    ['validate', 'Validate', base],
    ['fix', 'Fix', base + '/diagnostics'],
    ['import', 'Import', base + '/verify'],
    ['verify', 'Verify', base + '/verify'],
    ['download', 'Download', base + '/verify']
  ];
  var states = stepStates(d);
  var titles = { done: 'done', attn: 'needs attention',
                 blocked: 'blocked', todo: 'not started' };
  return '<div class="stepper" role="navigation" ' +
    'aria-label="Migration steps">' +
    steps.map(function (sp, i) {
      var key = sp[0], state = states[key] || 'todo';
      var mark = state === 'done' ? '&#10003;' :
        state === 'blocked' ? '&#10007;' :
        state === 'attn' ? '!' : String(i + 1);
      return '<div class="step ' + state +
        (key === activeKey ? ' active' : '') + '">' +
        '<a href="' + sp[2] + '" title="' + esc(sp[1]) + ': ' +
        titles[state] + '"><span class="bubble">' + mark +
        '</span><span>' + esc(sp[1]) + '</span></a>' +
        '<span class="bar"></span></div>';
    }).join('') + '</div>';
}

/* ====================================================== OVERVIEW */
function readinessOf(d) {
  /* Approximate grade from the list payload (server-side readiness
     is fetched per-slug in the workspace). */
  var f = d.findings_summary || {};
  var score = d.parity_score;
  if (f.blocker) return { score: score, grade: 'blocked' };
  if (score == null) return { score: null, grade: '' };
  return { score: score,
           grade: score >= 90 ? 'ready' :
                  score >= 60 ? 'almost' : 'blocked' };
}

function primaryAction(d) {
  var base = '#/dash/' + encodeURIComponent(d.slug);
  var f = d.findings_summary || {};
  var t = d.datatest_summary || {};
  if (f.blocker) {
    return { label: f.blocker + ' blocker' +
             (f.blocker > 1 ? 's' : '') + ' &mdash; fix now',
             href: base + '/diagnostics', cls: 'danger' };
  }
  if (!Object.keys(t).length) {
    return { label: 'Run data tests', href: base, cls: 'primary' };
  }
  if (t.error) {
    return { label: t.error + ' failing panel' +
             (t.error > 1 ? 's' : '') + ' &mdash; open',
             href: base, cls: 'primary' };
  }
  if (d.parity_score == null) {
    return { label: 'Verify data parity', href: base + '/verify',
             cls: 'primary' };
  }
  return { label: 'Verify &amp; download', href: base + '/verify',
           cls: d.parity_score >= 60 ? 'armed' : '' };
}

async function vOverview(view) {
  crumb('Overview');
  view.innerHTML = welcomeHtml() +
    '<h1>Overview</h1><p class="lead">Every ' +
    'converted dashboard with its migration readiness.</p>' +
    '<div id="ov-area"><div class="empty">Loading&hellip;</div>' +
    '</div>';
  var data = await api('/api/dashboards');
  App.dashboards = data.dashboards || [];
  var area = $('#ov-area');
  if (!App.dashboards.length) {
    area.innerHTML = '<div class="empty"><span class="eico">' +
      ico('inbox', 26) + '</span><b>No dashboards yet.' +
      '</b><br>1. <a href="#/connect">Connect</a> New Relic and ' +
      'Grafana &middot; 2. <a href="#/convert">Fetch &amp; ' +
      'Convert</a> your dashboards.<br>They will appear here with ' +
      'readiness scores.</div>';
    return;
  }
  var totals = { panels: 0, review: 0, blockers: 0 };
  App.dashboards.forEach(function (d) {
    totals.panels += d.panels || 0;
    var c = d.confidence || {};
    totals.review += (c['needs-review'] || 0) +
      (c.untranslatable || 0);
    totals.blockers += (d.findings_summary || {}).blocker || 0;
  });
  var cards = App.dashboards.map(function (d) {
    var r = readinessOf(d);
    var act = primaryAction(d);
    var pchips = '';
    var ps = d.parity_summary || {};
    ['match', 'close', 'value-mismatch', 'gf-empty',
     'gf-error'].forEach(function (k) {
      if (ps[k]) pchips += chip(ps[k] + ' ' + k, VERDICT_CLS[k]);
    });
    var rs = d.review_summary || {};
    if (rs.confirmed) {
      pchips += chip(rs.confirmed + ' human-verified', 'ok',
                     'panels confirmed by raw-sample review');
    }
    if (rs.rejected) {
      pchips += chip(rs.rejected + ' rejected', 'err',
                     'panels rejected in raw-sample review');
    }
    var url = '#/dash/' + encodeURIComponent(d.slug);
    return '<div class="ov-card">' + ring(r.score, r.grade, 64) +
      '<div class="ov-main">' +
      '<div class="ov-title"><a href="' + url + '">' + esc(d.title) +
      '</a></div>' +
      '<div class="ov-sub">' + esc(d.slug) + ' &middot; ' +
      (d.panels || 0) + ' panels</div>' +
      '<div class="ov-chips">' + confChips(d.confidence) +
      (pchips ? '<br>' + pchips : '') + '</div>' +
      '<a class="btn small ' + act.cls + '" href="' + act.href +
      '">' + act.label + '</a></div></div>';
  }).join('');
  area.innerHTML =
    '<div class="cards-row">' +
    '<div class="stat-card"><div class="num">' +
    App.dashboards.length + '</div><div class="lbl">dashboards' +
    '</div></div>' +
    '<div class="stat-card"><div class="num">' + totals.panels +
    '</div><div class="lbl">panels</div></div>' +
    '<div class="stat-card"><div class="num">' + totals.review +
    '</div><div class="lbl">need review</div></div>' +
    '<div class="stat-card"><div class="num">' + totals.blockers +
    '</div><div class="lbl">open blockers</div></div></div>' +
    '<div class="ov-grid">' + cards + '</div>';
}

/* ====================================================== CONNECT */
async function vConnect(view) {
  crumb('Connect');
  var s = App.state || await api('/api/state');
  App.state = s;
  var ses = s.session;
  view.innerHTML =
  '<h1>Connect</h1>' +
  '<p class="lead">Connect New Relic (source), Grafana (target) ' +
  'and optionally an AI backend for automated help.</p>' +
  '<div class="sec-note">API keys are held in the server process ' +
  'memory only. They are never written to the database, to disk, ' +
  'or to logs, and are gone when the server stops.</div>' +
  '<div class="grid2">' +

  '<div class="card"><h2>New Relic</h2>' +
  '<label>User API key (NRAK-&hellip;)</label>' +
  '<input type="password" id="su-nrkey" autocomplete="off" ' +
  'placeholder="' + (ses.nr_key_set ? '**** key set' : 'NRAK-...') +
  '">' +
  '<label>Region</label>' +
  '<select id="su-region"><option' +
    (ses.nr_region === 'US' ? ' selected' : '') + '>US</option>' +
  '<option' + (ses.nr_region === 'EU' ? ' selected' : '') +
    '>EU</option></select>' +
  '<div class="btnbar">' +
  '<button class="btn primary" id="su-nr-save">Save</button>' +
  '<button class="btn" id="su-nr-test">Test key</button></div>' +
  '<div id="su-nr-out"></div></div>' +

  '<div class="card"><h2>Grafana</h2>' +
  '<label>URL</label>' +
  '<input id="su-gfurl" placeholder="http://localhost:3000" ' +
  'value="' + esc(ses.grafana_url) + '">' +
  '<label>Service account token</label>' +
  '<input type="password" id="su-gftoken" autocomplete="off" ' +
  'placeholder="' + (ses.grafana_token_set ? '**** token set' :
   'glsa_...') + '">' +
  '<div class="btnbar">' +
  '<button class="btn primary" id="su-gf-save">Save</button>' +
  '<button class="btn" id="su-gf-test">Test token</button></div>' +
  '<div id="su-gf-out"></div></div>' +

  '<div class="card"><h2>' + ico('sparkle', 14) +
  'AI assistance (optional)</h2>' +
  '<div class="kv" style="margin-bottom:4px">Powers &quot;Ask ' +
  'AI&quot; on failing panels and the assistant chat.</div>' +
  '<label id="su-ai-backend-lbl">Backend</label>' +
  '<div class="seg" role="group" ' +
  'aria-labelledby="su-ai-backend-lbl">' +
  '<button type="button" id="su-ai-mode-api">Anthropic API' +
  '</button>' +
  '<button type="button" id="su-ai-mode-local">Local console AI' +
  '</button></div>' +

  '<div id="su-ai-api">' +
  '<label>Anthropic API key</label>' +
  '<input type="password" id="su-aikey" autocomplete="off" ' +
  'placeholder="' + (ses.anthropic_key_set ? '**** key set' :
   'sk-ant-...') + '">' +
  '<label>Model</label>' +
  '<input id="su-aimodel" placeholder="claude-sonnet-5 (default)"' +
  ' value="' + esc(ses.ai_model) + '">' +
  '</div>' +

  '<div id="su-ai-local">' +
  '<label>Command template</label>' +
  '<input id="su-aicmd" autocomplete="off" spellcheck="false" ' +
  'class="mono" value="' + esc(ses.ai_command || '') +
  '" placeholder="claude -p {prompt}">' +
  '<div class="field-help">Examples: <span class="mono">claude ' +
  '-p {prompt}</span> &middot; <span class="mono">kiro-cli</span> ' +
  '&middot; plain commands read the prompt on stdin.</div>' +
  '<div class="field-help">Runs locally with your user ' +
  'permissions; panel/query/error text is sent to it.</div>' +
  '</div>' +

  '<div class="btnbar">' +
  '<button class="btn primary" id="su-ai-save">Save</button>' +
  '<button class="btn" id="su-ai-test">Test connection</button>' +
  '</div>' +
  '<div id="su-ai-out"></div></div>' +

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
    var out = $('#su-nr-out');
    try {
      var k = $('#su-nrkey').value.trim();
      var body = { nr_region: $('#su-region').value };
      if (k) body.nr_api_key = k;
      await api('/api/settings', body);
      var r = await api('/api/nr/test-key', {});
      var accts = (r.accounts || []).map(function (a) {
        return chip((a.name || '') + ' (' + a.id + ')', 'info');
      }).join('');
      out.innerHTML = '<div class="ai-box">' + chip('key OK', 'ok') +
        ' <span class="kv">' +
        esc((r.user || {}).email || (r.user || {}).name || '') +
        '</span><div style="margin-top:8px">' +
        (accts || chip('no accounts visible', 'warn')) +
        '</div></div>';
      toast('New Relic key OK (' + (r.accounts || []).length +
            ' account(s))', 'ok');
    } catch (e) {
      out.innerHTML = errorCard(
        'The New Relic key test failed. Check the key (NRAK-...) ' +
        'and region above, then test again.', e.message, null);
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
    var out = $('#su-gf-out');
    try {
      var body = { grafana_url: $('#su-gfurl').value.trim() };
      var t = $('#su-gftoken').value.trim();
      if (t) body.grafana_token = t;
      await api('/api/settings', body);
      var r = await api('/api/grafana/test-token', {});
      var p = r.permissions || {};
      var h = r.health || {};
      out.innerHTML = '<div class="ai-box">' +
        (r.ok ? chip('Grafana ' + (h.version || 'reachable'), 'ok')
              : chip('health: ' + (h.error || 'unknown'), 'err')) +
        ' ' + chip('role: ' + (p.role || '?'),
                   p.role ? 'info' : 'dim') +
        ' ' + chip('datasource admin', p.can_admin_datasources ?
                   'ok' : 'warn') +
        ' ' + chip('dashboard edit', p.can_edit_dashboards ?
                   'ok' : 'warn') +
        (p.detail ? '<div class="kv" style="margin-top:6px">' +
          esc(p.detail) + '</div>' : '') + '</div>';
      toast('Grafana token checked', 'ok');
    } catch (e) {
      out.innerHTML = errorCard(
        'The Grafana connection test failed. Check the URL and ' +
        'service-account token above, then test again.',
        e.message, null);
      toast('Grafana: ' + e.message, 'err');
    }
    busy(btn, false); refreshState();
  };
  /* AI backend picker: Anthropic API vs local console command. */
  var aiMode = aiBackend(ses) === 'local' ? 'local' : 'api';
  function renderAiMode() {
    $('#su-ai-mode-api').classList.toggle('on', aiMode === 'api');
    $('#su-ai-mode-local').classList.toggle('on',
                                            aiMode === 'local');
    $('#su-ai-api').style.display =
      aiMode === 'api' ? '' : 'none';
    $('#su-ai-local').style.display =
      aiMode === 'local' ? '' : 'none';
  }
  $('#su-ai-mode-api').onclick = function () {
    aiMode = 'api'; renderAiMode(); };
  $('#su-ai-mode-local').onclick = function () {
    aiMode = 'local'; renderAiMode(); };
  renderAiMode();

  function aiSettingsBody() {
    /* The picker is exclusive: saving one backend clears the
       other (the server prefers the API key when both are set). */
    if (aiMode === 'local') {
      var cmd = $('#su-aicmd').value.trim();
      var b = { ai_command: cmd };  /* empty clears */
      if (cmd) b.anthropic_api_key = '';
      return b;
    }
    var body = { ai_model: $('#su-aimodel').value.trim(),
                 ai_command: '' };
    var k = $('#su-aikey').value.trim();
    if (k) body.anthropic_api_key = k;
    return body;
  }
  $('#su-ai-save').onclick = function () {
    saveSettings(aiSettingsBody(), this);
  };
  $('#su-ai-test').onclick = async function () {
    var btn = this; busy(btn, true);
    var out = $('#su-ai-out');
    out.innerHTML = '';
    try {
      await api('/api/settings', aiSettingsBody());
      var r = await api('/api/ai/test', {});
      if (r.ok) {
        out.innerHTML = '<div class="ai-box">' +
          chip('AI reachable', 'ok') +
          (r.backend ? ' ' + chip(r.backend === 'local' ?
            'local console' : 'Anthropic API', 'info') : '') +
          (r.latency_ms != null ? ' <span class="kv mono">' +
            Math.round(r.latency_ms) + ' ms</span>' : '') +
          (r.reply_excerpt ? '<pre style="margin:8px 0 0">' +
            esc(r.reply_excerpt) + '</pre>' : '') + '</div>';
        toast('AI backend responded', 'ok');
      } else {
        out.innerHTML = errorCard('The AI backend test failed.',
          r.error || 'no reply from the AI backend', null);
        toast('AI test failed', 'err');
      }
    } catch (e) {
      out.innerHTML = errorCard('The AI backend test failed.',
                                e.message, null);
      toast('AI test failed: ' + e.message, 'err');
    }
    busy(btn, false); refreshState();
  };
  $('#su-ws-save').onclick = function () {
    saveSettings({ input_dir: $('#su-indir').value.trim(),
                   out_dir: $('#su-outdir').value.trim(),
                   config_path: $('#su-cfg').value.trim() }, this);
  };
}

/* ====================================================== CONVERT */
async function vConvert(view) {
  crumb('Fetch & Convert');
  var ses = (App.state || {}).session || {};
  view.innerHTML =
  '<h1>Fetch &amp; Convert</h1>' +
  '<p class="lead">Fetch dashboards from New Relic, then convert ' +
  'them into Grafana dashboards with requirements analysis and ' +
  'per-dashboard packages.</p>' +
  '<div class="grid2">' +
  '<div class="card"><h2>1 &middot; Fetch from New Relic</h2>' +
  '<label>Write NR JSON exports to</label>' +
  '<input id="cv-fetchdir" value="' + esc(ses.input_dir || '') +
  '">' +
  '<label>Dashboard GUIDs (optional, comma separated &mdash; ' +
  'empty = all)</label>' +
  '<input id="cv-guids" placeholder="all dashboards">' +
  '<div class="btnbar"><button class="btn primary" id="cv-fetch">' +
  ico('download', 14) + 'Fetch</button>' +
  '<button class="btn" id="cv-browse">Browse &amp; pick&hellip;' +
  '</button></div>' +
  '<div id="cv-pick"></div></div>' +
  '<div class="card"><h2>2 &middot; Convert &amp; package</h2>' +
  '<label>Input directory (NR JSON)</label>' +
  '<input id="cv-indir" value="' + esc(ses.input_dir || '') + '">' +
  '<label>Output directory</label>' +
  '<input id="cv-outdir" value="' + esc(ses.out_dir || '') + '">' +
  '<label>Mapping config (optional)</label>' +
  '<input id="cv-cfg" value="' + esc(ses.config_path || '') +
  '" placeholder="config/mappings.json">' +
  '<div class="row" style="margin-top:10px">' +
  '<input type="checkbox" id="cv-pkg" checked>' +
  '<span class="kv">Package (requirements.json, README, test.sh ' +
  'per dashboard)</span></div>' +
  '<div class="btnbar"><button class="btn primary" id="cv-run">' +
  ico('play', 14) + 'Run convert</button></div></div>' +
  '</div>' +
  consoleHtml('cv-log', 'Fetch / convert log') +
  '<div id="cv-result"></div>';

  $('#cv-browse').onclick = async function () {
    var btn = this; busy(btn, true);
    var box = $('#cv-pick');
    try {
      var job = await startJob('list New Relic dashboards',
        '/api/nr/list', {}, logInto($('#cv-log')));
      var list = (job.result || {}).dashboards || [];
      if (!list.length) {
        box.innerHTML = '<div class="kv" style="margin-top:8px">' +
          'No dashboards visible to this API key.</div>';
      } else {
        renderNrPicker(box, list);
      }
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false);
  };

  $('#cv-fetch').onclick = async function () {
    var btn = this; busy(btn, true);
    var guids = $('#cv-guids').value.split(',').map(function (s) {
      return s.trim(); }).filter(Boolean);
    try {
      var job = await startJob('fetch from New Relic',
        '/api/nr/fetch',
        { out: $('#cv-fetchdir').value.trim(), guids: guids },
        logInto($('#cv-log')));
      toast('Fetched ' + (job.result.written || []).length +
            ' dashboards', 'ok');
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false); refreshState();
  };

  $('#cv-run').onclick = async function () {
    var btn = this; busy(btn, true);
    $('#cv-result').innerHTML = '';
    try {
      var job = await startJob('convert & package', '/api/convert', {
        input_dir: $('#cv-indir').value.trim(),
        out_dir: $('#cv-outdir').value.trim(),
        config_path: $('#cv-cfg').value.trim(),
        package: $('#cv-pkg').checked
      }, logInto($('#cv-log')));
      renderConvertResult(job.result);
      toast('Converted ' + (job.result.dashboards || []).length +
            ' dashboard(s)', 'ok');
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false); refreshState();
  };
}

/* Checkbox picker over the NR dashboard list so a subset can be
   fetched without ever hunting GUIDs down in the New Relic UI. */
function renderNrPicker(box, list) {
  function rows(filter) {
    var needle = (filter || '').toLowerCase();
    var shown = 0;
    var html = list.map(function (e, i) {
      var name = e.name || e.guid || '?';
      if (needle && name.toLowerCase().indexOf(needle) < 0) {
        return '';
      }
      shown++;
      return '<div class="checkbox-row">' +
        '<input type="checkbox" class="nrp-cb" data-i="' + i + '">' +
        '<span>' + esc(name) + '</span>' +
        (e.accountId ? '<span class="kv" style="margin-left:auto">' +
          'account ' + esc(e.accountId) + '</span>' : '') + '</div>';
    }).join('');
    return html || '<div class="kv" style="padding:8px 4px">no ' +
      'dashboard names match ' + esc('"' + (filter || '') + '"') +
      '</div>';
  }
  box.innerHTML = '<div style="margin-top:10px">' +
    '<input id="nrp-filter" placeholder="filter by name..." ' +
    'autocomplete="off">' +
    '<div id="nrp-rows" style="max-height:260px;overflow-y:auto;' +
    'margin-top:6px">' + rows('') + '</div>' +
    '<div class="btnbar">' +
    '<button class="btn small" id="nrp-all">Select shown</button>' +
    '<button class="btn small" id="nrp-none">Clear</button>' +
    '<button class="btn small primary" id="nrp-use">Use selected' +
    '</button><span class="kv" id="nrp-count"></span></div></div>';
  function bindRows() {
    $all('.nrp-cb', box).forEach(function (cb) {
      cb.onchange = updateCount; });
    updateCount();
  }
  function updateCount() {
    var n = $all('.nrp-cb', box).filter(function (c) {
      return c.checked; }).length;
    $('#nrp-count').textContent = n ? n + ' selected' : '';
  }
  $('#nrp-filter').oninput = debounce(function () {
    $('#nrp-rows').innerHTML = rows($('#nrp-filter').value.trim());
    bindRows();
  }, 200);
  $('#nrp-all').onclick = function () {
    $all('.nrp-cb', box).forEach(function (c) {
      c.checked = true; });
    updateCount();
  };
  $('#nrp-none').onclick = function () {
    $all('.nrp-cb', box).forEach(function (c) {
      c.checked = false; });
    updateCount();
  };
  $('#nrp-use').onclick = function () {
    var guids = $all('.nrp-cb', box).filter(function (c) {
      return c.checked; }).map(function (c) {
      return list[+c.getAttribute('data-i')].guid; });
    $('#cv-guids').value = guids.join(',');
    toast(guids.length ?
          guids.length + ' dashboard(s) selected - hit Fetch' :
          'Selection cleared - Fetch now grabs everything', 'ok');
  };
  bindRows();
}

function renderConvertResult(res) {
  var list = (res.dashboards || []).map(function (d) {
    return '<tr class="click" tabindex="0" data-slug="' +
      esc(d.slug) + '">' +
      '<td><b>' + esc(d.title) + '</b></td><td class="num">' +
      d.panels +
      '</td><td>' + confChips(d.confidence) + '</td><td>' +
      dsChips(d.datasources) + '</td></tr>';
  }).join('');
  var failed = (res.failed || []).map(function (f) {
    return errorCard('Could not convert <span class="mono">' +
      esc(f.source) + '</span>.', f.error,
      { label: 'Check the export, then re-run convert',
        href: '#/convert' });
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
    (list ? '<div class="card"><div class="tablewrap">' +
    '<table class="zebra">' +
    '<thead><tr><th>Dashboard</th><th class="num">Panels</th>' +
    '<th>Confidence</th><th>Datasources</th></tr></thead><tbody>' +
    list + '</tbody></table></div></div>' : '') +
    (failed ? '<div class="card"><h2>Failed inputs</h2>' + failed +
    '</div>' : '');
  $all('#cv-result tr.click').forEach(function (tr) {
    var go = function () {
      location.hash = '#/dash/' +
        encodeURIComponent(tr.getAttribute('data-slug'));
    };
    tr.onclick = go;
    tr.onkeydown = function (ev) {
      if (ev.key === 'Enter') go(); };
  });
}

/* ====================================================== DATASOURCES */
async function loadTemplates() {
  if (!App.templates) {
    App.templates = await api('/api/grafana/ds-templates');
  }
  return App.templates;
}

function healthChip(uid) {
  var h = App.dsHealth[uid];
  if (!h) return '<span class="chip dim"><span class="health-dot">' +
    '</span>checking&hellip;</span>';
  var cls = h.status === 'ok' ? 'ok' :
    h.status === 'error' ? 'err' : 'warn';
  return '<span class="chip ' + cls + '" title="' +
    esc(h.message || '') + '"><span class="health-dot ' + cls +
    '"></span>' + esc(h.status || 'unknown') + '</span>';
}

async function vDatasources(view) {
  crumb('Datasources');
  try {
    if (!App.dashboards.length) {
      App.dashboards = (await api('/api/dashboards')).dashboards || [];
    }
  } catch (e) { /* datasources still work without the list */ }
  if (!App.dsFlowSlug) {
    App.dsFlowSlug = (App.ws && App.ws.slug) ||
      (App.cmp && App.cmp.slug) || '';
  }
  view.innerHTML = '<h1>Datasources</h1><p class="lead">The ' +
    'datasources on the connected Grafana instance. Create the ' +
    'ones your dashboards need, then watch real data start ' +
    'flowing through the panels that were empty.</p>' +
    '<div id="ds-area"><div class="empty">Loading&hellip;</div>' +
    '</div>';
  var area = $('#ds-area');
  var list;
  try {
    var r = await api('/api/grafana/datasources', {});
    list = r.datasources || [];
  } catch (e) {
    area.innerHTML = errorCard(
      'Could not list the datasources on this Grafana instance.',
      e.message,
      { label: 'Check the Grafana connection', href: '#/connect' });
    return;
  }
  renderDsTable(area, list);
  /* Live health badges: kick off checks for every ds in parallel. */
  list.forEach(function (ds) {
    var uid = ds.uid || '';
    if (!uid) return;
    api('/api/grafana/datasource/' + encodeURIComponent(uid) +
        '/health', {}).then(function (h) {
      App.dsHealth[uid] = h;
    }).catch(function (e) {
      App.dsHealth[uid] = { status: 'error', message: e.message };
    }).finally(function () {
      var cell = $('#ds-h-' + cssId(uid));
      if (cell) cell.innerHTML = healthChip(uid);
    });
  });
  if (App.dsFlowSlug) dsKickFlow(list);
}

/* Probe how many panels of a given dashboard now flow through each
   datasource -- the badge in the Flow column, re-checkable in place. */
function flowCellHtml(uid) {
  if (!App.dsFlowSlug) {
    return '<span class="kv">pick a dashboard above</span>';
  }
  var fam = App.dsFlow[uid];
  if (fam === undefined) {
    return '<span class="chip dim">checking&hellip;</span>';
  }
  if (fam === null) return '<span class="chip dim">not used</span>';
  return flowBadgeHtml(fam) +
    ' <button class="btn small ghost iconbtn" data-dsact="flow" ' +
    'data-uid="' + esc(uid) + '" title="Re-check flow" ' +
    'aria-label="Re-check flow">' + ico('refresh', 12) + '</button>';
}

async function dsCheckFlow(uid, toastIt, btn) {
  if (!App.dsFlowSlug || !uid) return;
  if (btn) busy(btn, true);
  try {
    var r = await api('/api/datasource/' + encodeURIComponent(uid) +
      '/verify-flow', { slug: App.dsFlowSlug });
    var fams = flowFamilies(r.flow);
    App.dsFlow[uid] = fams[0] || null;
    if (App.dsFlow[uid] && App.dsFlow[uid].health) {
      App.dsHealth[uid] = App.dsFlow[uid].health;
    }
    if (toastIt) {
      var f = App.dsFlow[uid] || {};
      toast((f.panels_with_data || 0) + '/' + (f.panels_total || 0) +
            ' panels flowing', 'ok');
    }
  } catch (e) {
    App.dsFlow[uid] = null;
    if (toastIt) toast(e.message, 'err');
  }
  var cell = $('#ds-flow-' + cssId(uid));
  if (cell) {
    cell.innerHTML = flowCellHtml(uid);
    var fb = $('button[data-dsact="flow"]', cell);
    if (fb) fb.onclick = function () { dsAction(fb, []); };
  }
  var hcell = $('#ds-h-' + cssId(uid));
  if (hcell) hcell.innerHTML = healthChip(uid);
  if (btn) busy(btn, false);
}

function dsKickFlow(list) {
  list.forEach(function (ds) {
    if (ds.uid) { App.dsFlow[ds.uid] = undefined;
                  dsCheckFlow(ds.uid, false, null); }
  });
}

function cssId(s) {
  return String(s).replace(/[^A-Za-z0-9_-]/g, '_');
}

function renderDsTable(area, list) {
  var rows = list.map(function (ds) {
    var uid = ds.uid || '';
    return '<tr><td><b>' + esc(ds.name) + '</b>' +
      (ds.isDefault ? ' ' + chip('default', 'purple') : '') +
      '</td>' +
      '<td class="mono">' + esc(ds.type || '') + '</td>' +
      '<td class="mono">' + esc(uid) + '</td>' +
      '<td class="mono kv">' + esc(ds.url || '') + '</td>' +
      '<td id="ds-h-' + cssId(uid) + '">' + healthChip(uid) +
      '</td>' +
      '<td id="ds-flow-' + cssId(uid) + '">' + flowCellHtml(uid) +
      '</td>' +
      '<td class="right" style="white-space:nowrap">' +
      '<button class="btn small" data-dsact="health" data-uid="' +
      esc(uid) + '">Re-check</button> ' +
      '<button class="btn small" data-dsact="edit" data-uid="' +
      esc(uid) + '">Edit</button> ' +
      '<button class="btn small danger" data-dsact="del" ' +
      'data-uid="' + esc(uid) + '" data-name="' + esc(ds.name) +
      '">Delete</button></td></tr>';
  }).join('');
  var flowOpts = '<option value="">&mdash; none &mdash;</option>' +
    (App.dashboards || []).map(function (d) {
      return '<option value="' + esc(d.slug) + '"' +
        (d.slug === App.dsFlowSlug ? ' selected' : '') + '>' +
        esc(d.title || d.slug) + '</option>';
    }).join('');
  area.innerHTML =
    '<div class="btnbar" style="margin:0 0 12px">' +
    '<button class="btn primary" id="ds-add">+ Add datasource' +
    '</button>' +
    '<button class="btn" id="ds-reload">Refresh</button>' +
    '<span class="row" style="gap:6px;margin-left:auto">' +
    '<label class="kv" style="margin:0" for="ds-flowdash">Watch ' +
    'data flow for</label>' +
    '<select id="ds-flowdash" style="max-width:220px">' + flowOpts +
    '</select></span></div>' +
    '<div class="card"><div class="tablewrap">' +
    '<table class="zebra">' +
    '<thead><tr><th>Name</th><th>Type</th><th>UID</th><th>URL</th>' +
    '<th>Health</th><th>Data flow</th><th class="right">Actions' +
    '</th></tr></thead>' +
    '<tbody>' + (rows ||
    '<tr><td colspan="7" class="kv">No datasources on this ' +
    'instance yet &mdash; add the first one.</td></tr>') +
    '</tbody></table></div></div>';
  $('#ds-add').onclick = function () { dsFlyout(null); };
  $('#ds-reload').onclick = function () { route(); };
  $('#ds-flowdash').onchange = function () {
    App.dsFlowSlug = this.value; App.dsFlow = {}; route();
  };
  $all('button[data-dsact]', area).forEach(function (btn) {
    btn.onclick = function () { dsAction(btn, list); };
  });
}

async function dsAction(btn, list) {
  var act = btn.getAttribute('data-dsact');
  var uid = btn.getAttribute('data-uid');
  var row = null;
  list.forEach(function (d) { if (d.uid === uid) row = d; });
  if (act === 'health') {
    busy(btn, true);
    try {
      var h = await api('/api/grafana/datasource/' +
                        encodeURIComponent(uid) + '/health', {});
      App.dsHealth[uid] = h;
      toast('Health: ' + (h.status || 'unknown') +
            (h.message ? ' - ' + h.message : ''),
            h.status === 'ok' ? 'ok' : 'err');
    } catch (e) {
      App.dsHealth[uid] = { status: 'error', message: e.message };
      toast(e.message, 'err');
    }
    var cell = $('#ds-h-' + cssId(uid));
    if (cell) cell.innerHTML = healthChip(uid);
    busy(btn, false);
  } else if (act === 'flow') {
    await dsCheckFlow(uid, true, btn);
  } else if (act === 'edit') {
    dsFlyout(row);
  } else if (act === 'del') {
    var name = btn.getAttribute('data-name') || uid;
    var yes = await typedConfirm({
      title: 'Delete datasource',
      html: 'This permanently deletes <b>' + esc(name) +
        '</b> <span class="mono">(' + esc(uid) + ')</span> from ' +
        'Grafana. Panels using it will stop working.',
      expect: name, action: 'Delete datasource' });
    if (!yes) return;
    try {
      await api('/api/grafana/datasource/' +
                encodeURIComponent(uid), undefined, 'DELETE');
      toast('Deleted ' + name, 'ok');
      route();
    } catch (e) { toast(e.message, 'err'); }
  }
}

function tplFieldHtml(f, val) {
  var id = 'dsf-' + esc(f.name);
  var input;
  var common = ' id="' + id + '" data-fname="' + esc(f.name) +
    '" placeholder="' + esc(f.placeholder || '') +
    '" autocomplete="off"';
  if (f.multiline) {
    input = '<textarea' + common + '>' + esc(val || '') +
      '</textarea>';
  } else if (f.secret) {
    input = '<input type="password"' + common + ' value="">';
  } else {
    input = '<input' + common + ' value="' + esc(val || '') + '">';
  }
  return '<label for="' + id + '">' + esc(f.label || f.name) +
    (f.required ? ' <span class="req">*</span>' : '') +
    (f.secret ? ' ' + chip('secret', 'purple') : '') + '</label>' +
    input +
    (f.help ? '<div class="field-help">' + esc(f.help) + '</div>'
            : '');
}

function tplValue(row, f) {
  /* Prefill an edit form from the existing ds row via the field's
     payload path. Secrets are never sent back by Grafana. */
  if (!row || f.secret) return '';
  var path = f.path || '';
  if (path === 'url') return row.url || '';
  if (path.indexOf('jsonData.') === 0) {
    return ((row.jsonData || {})[path.slice(9)]) || '';
  }
  return '';
}

/* Add (row=null) or edit (row=existing ds) flyout, driven entirely
   by /api/grafana/ds-templates. */
async function dsFlyout(row) {
  var templates;
  try { templates = await loadTemplates(); }
  catch (e) { toast('ds-templates: ' + e.message, 'err'); return; }
  var types = Object.keys(templates);
  var editing = !!row;
  var curType = editing ?
    (types.filter(function (t) {
      return templates[t].plugin_id === row.type ||
        t === row.type; })[0] || types[0]) : types[0];

  function bodyHtml(tkey) {
    var tpl = templates[tkey] || {};
    var opts = types.map(function (t) {
      return '<option value="' + esc(t) + '"' +
        (t === tkey ? ' selected' : '') + '>' +
        esc(templates[t].label || t) +
        (templates[t].core ? '' : ' (plugin)') + '</option>';
    }).join('');
    return '<label for="ds-type">Type</label>' +
      '<select id="ds-type"' + (editing ? ' disabled' : '') + '>' +
      opts + '</select>' +
      (tpl.core === false ? '<div class="field-help">' +
        chip('plugin required', 'warn') + ' install <span ' +
        'class="mono">' + esc(tpl.plugin_id || '') +
        '</span> on the Grafana server first.</div>' : '') +
      '<label for="ds-name">Name <span class="req">*</span>' +
      '</label>' +
      '<input id="ds-name" value="' +
      esc(editing ? row.name : '') + '" autocomplete="off">' +
      (tpl.fields || []).map(function (f) {
        return tplFieldHtml(f, tplValue(row, f));
      }).join('') +
      (editing ? '<div class="field-help">Secret fields are ' +
        'write-only: leave them empty to keep the current ' +
        'value.</div>' : '') +
      (tpl.notes ? '<div class="sec-note">' + esc(tpl.notes) +
        '</div>' : '') +
      '<div id="ds-fly-out"></div>';
  }

  var fly = openFlyout(
    editing ? 'Edit datasource' : 'Add datasource',
    bodyHtml(curType),
    '<button class="btn" id="ds-cancel">Cancel</button>' +
    '<button class="btn primary" id="ds-save">' +
    (editing ? 'Save changes' : 'Create &amp; health-check') +
    '</button>');

  function bind() {
    $('#ds-cancel', fly).onclick = closeFlyout;
    var sel = $('#ds-type', fly);
    if (sel && !editing) {
      sel.onchange = function () {
        curType = sel.value;
        $('.flyout-body', fly).innerHTML = bodyHtml(curType);
        bind();
      };
    }
    $('#ds-save', fly).onclick = save;
  }

  async function save() {
    var btn = $('#ds-save', fly);
    var tpl = templates[curType] || {};
    var name = ($('#ds-name', fly).value || '').trim();
    var out = $('#ds-fly-out', fly);
    if (!name) {
      out.innerHTML = '<div class="err-text">A name is required.' +
        '</div>';
      return;
    }
    var values = {};
    var missing = [];
    (tpl.fields || []).forEach(function (f) {
      var el = $('[data-fname="' + f.name + '"]', fly);
      var v = el ? el.value.trim() : '';
      if (v) values[f.name] = v;
      else if (f.required && !editing) missing.push(f.label || f.name);
    });
    if (missing.length) {
      out.innerHTML = '<div class="err-text">Required: ' +
        esc(missing.join(', ')) + '</div>';
      return;
    }
    busy(btn, true);
    try {
      if (editing) {
        var payload = { name: name, type: row.type,
                        access: row.access || 'proxy',
                        jsonData: JSON.parse(JSON.stringify(
                          row.jsonData || {})) };
        if (row.url) payload.url = row.url;
        var secure = {};
        (tpl.fields || []).forEach(function (f) {
          var v = values[f.name];
          if (v == null) return;
          var path = f.path || '';
          if (path === 'url') payload.url = v;
          else if (path.indexOf('jsonData.') === 0) {
            payload.jsonData[path.slice(9)] = v;
          } else if (path.indexOf('secureJsonData.') === 0) {
            secure[path.slice(15)] = v;
          }
        });
        if (Object.keys(secure).length) {
          payload.secureJsonData = secure;
        }
        await api('/api/grafana/datasource/' +
                  encodeURIComponent(row.uid), payload, 'PUT');
        var h2 = await api('/api/grafana/datasource/' +
                           encodeURIComponent(row.uid) + '/health',
                           {});
        App.dsHealth[row.uid] = h2;
        out.innerHTML = '<div class="ai-box">' +
          chip('saved', 'ok') + ' ' + healthChip(row.uid) +
          (h2.message ? '<div class="kv" style="margin-top:6px">' +
            esc(h2.message) + '</div>' : '') + '</div>';
        toast('Datasource updated', 'ok');
        setTimeout(function () { closeFlyout(); route(); }, 900);
      } else {
        var flowSlug = App.dsFlowSlug || (App.ws && App.ws.slug) ||
          (App.cmp && App.cmp.slug) || '';
        var reqBody = { type: curType, name: name, values: values };
        if (flowSlug) reqBody.slug = flowSlug;
        var res = await api('/api/grafana/datasource', reqBody);
        var h = res.health || {};
        if (res.uid) App.dsHealth[res.uid] = h;
        var cls = h.status === 'ok' ? 'ok' :
          h.status === 'error' ? 'err' : 'warn';
        var flowHtml = res.flow ?
          flowResultHtml(res.flow, res.uid, flowSlug) : '';
        if (res.uid && res.flow) {
          var fams = flowFamilies(res.flow);
          if (fams[0]) App.dsFlow[res.uid] = fams[0];
        }
        out.innerHTML = '<div class="ai-box">' +
          chip('created', 'ok') +
          (res.uid ? ' <span class="mono kv">' + esc(res.uid) +
            '</span>' : '') +
          '<div style="margin-top:6px">' +
          chip('health: ' + (h.status || 'unknown'), cls) +
          (h.message ? '<div class="kv" style="margin-top:4px">' +
            esc(h.message) + '</div>' : '') + '</div></div>' +
          (flowHtml ? '<h3 style="margin-top:12px">Watch it flow' +
            '</h3>' + flowHtml : (flowSlug ? '' :
            '<div class="kv" style="margin-top:8px">Tip: pick a ' +
            'dashboard under <b>Watch data flow for</b> on the ' +
            'Datasources page to watch panels light up the instant ' +
            'a datasource is added.</div>')) +
          '<div class="btnbar"><button class="btn primary" ' +
          'id="ds-flow-done">Done</button></div>';
        bindFlowRecheck(out); mountCharts(out);
        var done = $('#ds-flow-done', fly);
        if (done) done.onclick = function () {
          closeFlyout(); route();
        };
        toast('Datasource created' +
              (h.status === 'ok' ? ' and healthy' :
               ' - health: ' + (h.status || 'unknown')),
              h.status === 'ok' ? 'ok' : 'err');
      }
    } catch (e) {
      out.innerHTML = errorCard(
        'Grafana rejected this datasource change.', e.message,
        null);
    }
    busy(btn, false);
  }
  bind();
}

/* ====================================================== WORKSPACE */
async function loadDetail(slug) {
  var d = await api('/api/dashboards/' + encodeURIComponent(slug));
  /* index helpers */
  d._tests = {};
  ((d.datatest || {}).results || []).forEach(function (r) {
    (d._tests[r.panel_id] = d._tests[r.panel_id] || {})[
      r.refId || 'A'] = r;
  });
  d._parity = {};
  ((d.parity || {}).panels || []).forEach(function (p) {
    (d._parity[p.panel_id] = d._parity[p.panel_id] || {})[
      p.refId || 'A'] = p;
  });
  d._samples = {};
  ((d.samples || {}).panels || []).forEach(function (p) {
    (d._samples[p.panel_id] = d._samples[p.panel_id] || {})[
      p.refId || 'A'] = p;
  });
  d._reviews = {};
  var rvs = (d.review || {}).reviews || {};
  Object.keys(rvs).forEach(function (k) {
    var r = rvs[k];
    (d._reviews[r.panel_id] = d._reviews[r.panel_id] || {})[
      r.refId || 'A'] = r;
  });
  d._findings = {};
  ((d.diagnosis || {}).findings || []).forEach(function (f) {
    if (f.panel_id != null) {
      (d._findings[f.panel_id] = d._findings[f.panel_id] || [])
        .push(f);
    }
  });
  d._panelMap = {};
  (function walk(ps) {
    (ps || []).forEach(function (p) {
      d._panelMap[p.id] = p;
      if (p.type === 'row') walk(p.panels);
    });
  })((d.dashboard || {}).panels);
  return d;
}

function wsTabs(slug, tab) {
  var base = '#/dash/' + encodeURIComponent(slug);
  var tabs = [['panels', 'Panels', base],
              ['diagnostics', 'Diagnostics', base + '/diagnostics'],
              ['verify', 'Verify & Download', base + '/verify']];
  return '<div class="tabs" role="tablist">' +
    tabs.map(function (t) {
      return '<a href="' + t[2] + '" role="tab"' +
        (t[0] === tab ? ' class="active" aria-selected="true"' :
         ' aria-selected="false"') + '>' + esc(t[1]) + '</a>';
    }).join('') + '</div>';
}

var STEP_FOR_TAB = { panels: 'validate', diagnostics: 'fix',
                     verify: 'verify' };

async function vWorkspace(view, slug, tab) {
  crumb(slug);
  view.innerHTML = '<div class="empty">Loading ' + esc(slug) +
    '&hellip;</div>';
  var d;
  try { d = await loadDetail(slug); }
  catch (e) {
    view.innerHTML = errorCard('Cannot load the workspace for ' +
      '<span class="mono">' + esc(slug) + '</span>.', e.message,
      { label: 'Back to overview', href: '#/overview' });
    return;
  }
  App.ws = { slug: slug, detail: d, tab: tab };
  crumb(d.title || slug);
  var head =
    '<a class="backlink" href="#/overview">&larr; All dashboards' +
    '</a>' +
    '<h1>' + esc(d.title) + '</h1>' +
    '<p class="lead mono">' + esc(slug) +
    (d.package_dir ? ' &middot; ' + esc(d.package_dir) : '') +
    '</p>' +
    reviewProgressHtml(d) +
    stepper(slug, d, STEP_FOR_TAB[tab] || 'validate') +
    wsTabs(slug, tab);
  if (tab === 'diagnostics') {
    view.innerHTML = head + '<div id="ws-body"></div>';
    renderDiagnostics($('#ws-body'), slug, d);
  } else if (tab === 'verify') {
    view.innerHTML = head + '<div id="ws-body"></div>';
    renderVerify($('#ws-body'), slug, d);
  } else {
    view.innerHTML = head + '<div id="ws-body"></div>';
    renderPanels($('#ws-body'), slug, d);
  }
}

function rerenderWs() {
  if (App.ws) {
    vWorkspace($('#view'), App.ws.slug, App.ws.tab);
  }
}

/* Human-review progress for a loaded dashboard detail. */
function reviewCounts(d) {
  var c = { confirmed: 0, rejected: 0, unsure: 0, total: 0 };
  var rvs = (d.review || {}).reviews || {};
  Object.keys(rvs).forEach(function (k) {
    var v = (rvs[k] || {}).verdict;
    if (c[v] != null) c[v]++;
  });
  Object.keys(d._panelMap || {}).forEach(function (id) {
    var p = d._panelMap[id];
    if (p.type === 'row' || p.type === 'text') return;
    c.total += (p.targets || []).length;
  });
  return c;
}

function reviewProgressHtml(d) {
  var c = reviewCounts(d);
  if (!c.total) return '';
  var html = chip(c.confirmed + '/' + c.total + ' confirmed',
                  c.total && c.confirmed === c.total ? 'ok' : 'dim',
                  'human sample review progress');
  if (c.rejected) html += chip(c.rejected + ' rejected', 'err');
  if (c.unsure) html += chip(c.unsure + ' not sure', 'warn');
  return '<div style="margin:-6px 0 10px">' +
    '<span class="kv" style="margin-right:6px">Human review:' +
    '</span>' + html + '</div>';
}

/* ------------------------------------------------ panels tab */
function reqStatusChip(items, ds) {
  if (!items || !items.length) return chip('not checked', 'dim');
  var m = null;
  items.forEach(function (it) {
    var name = String(it.item || '').toLowerCase();
    if (name.indexOf(String(ds.family || '').toLowerCase()) >= 0 ||
        (ds.plugin_id &&
         name.indexOf(String(ds.plugin_id).toLowerCase()) >= 0)) {
      m = it;
    }
  });
  if (!m) return chip('not checked', 'dim');
  var cls = m.status === 'ok' ? 'ok' :
            (m.status === 'missing' ? 'err' : 'warn');
  var fix = m.status !== 'ok' ?
    '<div class="kv">' + (m.fix ? esc(m.fix) + ' ' : '') +
    '<a href="#/datasources">Open Datasources &rarr;</a></div>' : '';
  return chip(m.status, cls) + fix;
}

function worstStatus(tests) {
  var vals = Object.keys(tests).map(function (k) {
    return tests[k].status; });
  if (vals.indexOf('error') >= 0) return 'error';
  if (vals.indexOf('no-data') >= 0) return 'no-data';
  if (vals.indexOf('data') >= 0) return 'data';
  return '';
}

function worstVerdict(prow) {
  var order = ['gf-error', 'nr-error', 'shape-mismatch',
               'value-mismatch', 'gf-empty', 'both-empty',
               'nr-empty', 'close', 'match'];
  var best = '';
  var rows = Object.keys(prow || {}).map(function (k) {
    return prow[k]; });
  order.forEach(function (v) {
    if (!best && rows.some(function (r) {
      return r.verdict === v; })) best = v;
  });
  return best ? rows.filter(function (r) {
    return r.verdict === best; })[0] : null;
}

function requirementsCard(d) {
  var reqs = d.requirements || {};
  var checkItems = (d.check || {}).items || [];
  var dsRows = (reqs.datasources || []).map(function (ds) {
    return '<tr><td><b>' + esc(ds.family) + '</b>' +
      (ds.required === false ? ' ' + chip('optional', 'dim') : '') +
      '</td>' +
      '<td class="mono">' + esc(ds.plugin_id || '') + '</td>' +
      '<td>' + esc(ds.purpose || '') + '</td>' +
      '<td class="mono">' + esc(ds.uid_ref || '') + '</td>' +
      '<td>' + reqStatusChip(checkItems, ds) + '</td></tr>';
  }).join('');
  var pluginRows = (reqs.plugins || []).map(function (p) {
    return '<div class="kv" style="margin:4px 0"><b class="mono">' +
      esc(p.id) + '</b> &mdash; ' + esc(p.reason || '') +
      (p.grafana_cli ? '<pre>' + esc(p.grafana_cli) + '</pre>' : '') +
      '</div>';
  }).join('');
  var domainRows = (reqs.domains || []).map(function (dm) {
    var opts = (dm.options || []).map(function (o) {
      return '<li>' + (o.plugin_id ? '<b class="mono">' +
        esc(o.plugin_id) + '</b>: ' : '') + esc(o.note || '') +
        '</li>';
    }).join('');
    return '<div style="margin:6px 0">' + chip(dm.domain, 'info') +
      ' <span class="kv">panels ' +
      esc((dm.panel_ids || []).join(', ')) + '</span>' +
      (opts ? '<ul class="kv" style="margin:4px 0 0">' + opts +
       '</ul>' : '') + '</div>';
  }).join('');
  var nrNative = (reqs.nr_native || []).map(function (n) {
    return '<div class="kv" style="margin:4px 0">panel ' +
      esc(n.panel_id) + ' <b>' + esc(n.widget || '') +
      '</b> &mdash; ' + esc(n.why || '') +
      (n.equivalent ? ' <i>Equivalent: ' + esc(n.equivalent) +
       '</i>' : '') + '</div>';
  }).join('');
  return '<div class="card"><h2>' + ico('database', 14) +
    'Requirements &mdash; install these first</h2>' +
    (dsRows ?
      '<div class="tablewrap"><table class="zebra"><thead><tr>' +
      '<th>Datasource' +
      '</th><th>Plugin</th><th>Purpose</th><th>Referenced as</th>' +
      '<th>Live status</th></tr></thead><tbody>' + dsRows +
      '</tbody></table></div>' :
      '<div class="kv">No datasource requirements recorded. ' +
      'Re-run Convert &amp; Package to generate them.</div>') +
    (pluginRows ? '<h3 style="margin-top:14px">Plugins</h3>' +
      pluginRows : '') +
    (domainRows ? '<h3 style="margin-top:14px">Detected data ' +
      'domains</h3>' + domainRows : '') +
    (nrNative ? '<h3 style="margin-top:14px">New Relic-native ' +
      'widgets</h3>' + nrNative : '') +
    (Object.keys(reqs).length ?
      jsonDetails('requirements.json', reqs) : '') +
    '</div>';
}

function renderPanels(el, slug, d) {
  var panelRows = (d.widget_report || []).map(function (w) {
    var pt = d._tests[w.panel_id] || {};
    var pv = d._parity[w.panel_id] || {};
    var wv = worstVerdict(pv);
    var wr = worstReview(d._reviews[w.panel_id] || {});
    var open = App.expanded[slug + ':' + w.panel_id];
    var row = '<tr class="click" tabindex="0" data-exp="' +
      esc(w.panel_id) + '" aria-expanded="' +
      (open ? 'true' : 'false') + '">' +
      '<td>' + esc(w.panel_id) + '</td>' +
      '<td><b>' + esc(w.widget || w.widget_title || '(untitled)') +
      '</b><div class="kv">' + esc(w.page || '') + '</div></td>' +
      '<td class="mono">' + esc(w.panel_type || '') + '</td>' +
      '<td>' + confChip(w.confidence) + '</td>' +
      '<td>' + testChip(worstStatus(pt)) + '</td>' +
      '<td>' + (wv ? verdictChip(wv.verdict, wv.ratio, wv.detail) :
                chip('no parity', 'dim')) + '</td>' +
      '<td>' + (wr ? reviewChip(wr) : chip('unreviewed', 'dim')) +
      '</td>' +
      '<td>' + (open ? '&#9662;' : '&#9656;') + '</td></tr>';
    if (open) {
      row += '<tr class="expand-row"><td colspan="8">' +
        panelDetailHtml(slug, d, w) + '</td></tr>';
    }
    return row;
  }).join('');

  el.innerHTML =
    '<div class="btnbar" style="margin:0 0 12px">' +
    '<button class="btn" id="ws-check">Check requirements' +
    '</button>' +
    '<button class="btn primary" id="ws-test">' +
    ico('play', 14) + 'Run data tests' +
    '</button>' +
    '<a class="btn" href="#/dash/' + encodeURIComponent(slug) +
    '/diagnostics">Diagnose &rarr;</a></div>' +
    consoleHtml('ws-log', 'Data test log') +
    requirementsCard(d) +
    '<div class="card"><h2>Panels</h2><div class="tablewrap">' +
    '<table><thead><tr><th>Id</th><th>Panel</th><th>Type</th>' +
    '<th>Confidence</th><th>Test</th><th>Parity</th>' +
    '<th>Review</th><th></th>' +
    '</tr></thead><tbody>' + (panelRows ||
    '<tr><td colspan="8" class="kv">no widget report</td></tr>') +
    '</tbody></table></div></div>';

  $('#ws-check').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      await api('/api/grafana/check', { slug: slug });
      toast('Requirement check complete', 'ok');
      rerenderWs();
    } catch (e) { toast(e.message, 'err'); busy(btn, false); }
  };
  $('#ws-test').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      var job = await startJob('data tests: ' + slug,
        '/api/grafana/test', { slug: slug }, logInto($('#ws-log')));
      var s = job.result.summary || {};
      toast('Tested: ' + Object.keys(s).map(function (k) {
        return s[k] + ' ' + k; }).join(', '), 'ok');
      rerenderWs();
    } catch (e) { toast(e.message, 'err'); busy(btn, false); }
    refreshState();
  };

  $all('tr[data-exp]', el).forEach(function (tr) {
    var toggle = function (ev) {
      if (ev && ev.target &&
          ev.target.closest('button, textarea, input, select, a, ' +
                            '.ac')) return;
      var key = slug + ':' + tr.getAttribute('data-exp');
      App.expanded[key] = !App.expanded[key];
      renderPanels(el, slug, d);
    };
    tr.onclick = toggle;
    tr.onkeydown = function (ev) {
      if (ev.key === 'Enter' && ev.target === tr) {
        ev.preventDefault(); toggle();
      }
    };
  });
  bindEditors(el, slug, d);
  bindPanelFixes(el, slug, d);
}

function panelDetailHtml(slug, d, w) {
  var html = '';
  var panel = d._panelMap[w.panel_id];
  var pt = d._tests[w.panel_id] || {};
  var pv = d._parity[w.panel_id] || {};
  (w.nrql || []).forEach(function (q) {
    html += '<div class="kv"><b>Original NRQL</b></div><pre>' +
      esc(q.query || q) + '</pre>';
  });
  (w.notes || []).forEach(function (n) {
    html += '<div class="kv">note: ' + esc(n) + '</div>';
  });
  var targets = (panel && panel.targets) || [];
  if (!targets.length) {
    html += '<div class="kv" style="margin-top:8px">This panel ' +
      'has no query targets (text/placeholder panel)';
    if (w.fallback) html += ' &mdash; fallback: ' + esc(w.fallback);
    html += '.</div>';
  }
  targets.forEach(function (t) {
    var ref = t.refId || 'A';
    var tr = pt[ref];
    var srow = (d._samples[w.panel_id] || {})[ref];
    var rv = (d._reviews[w.panel_id] || {})[ref];
    var prow = pv[ref] ||
      (Object.keys(pv).length === 1 ? pv[Object.keys(pv)[0]] : null);
    var dsType = (t.datasource && t.datasource.type) || '';
    var dsUid = (t.datasource && t.datasource.uid) || '';
    var eid = 'ed-' + w.panel_id + '-' + ref;
    html += '<div style="margin-top:16px">' +
      '<div class="row">' + chip(ref, 'dim') +
      chip(dsType || 'unknown ds', 'info') +
      testChip(tr && tr.status) +
      (prow ? verdictChip(prow.verdict, prow.ratio, prow.detail)
            : '') +
      (tr && tr.frames != null ?
        '<span class="kv">' + tr.frames + ' frames / ' +
        (tr.points || 0) + ' points</span>' : '') +
      '</div>' +
      (tr && tr.error ? errorCard(
        'The last data test failed for target ' + esc(ref) +
        '. Edit the query below, then Test again — or run ' +
        'Diagnose for a root cause.', tr.error,
        { label: 'Run Diagnose', href: '#/dash/' +
          encodeURIComponent(slug) + '/diagnostics' }) : '') +
      (prow && prow.detail && prow.verdict !== 'match' ?
        '<div class="kv" style="margin:4px 0">parity: ' +
        esc(prow.detail) + '</div>' : '') +
      '<div class="side-by-side">' +
      sideCard('New Relic', prow, 'nr') +
      sideCard('Grafana', prow, 'gf') +
      '</div>' +
      '<h3 style="margin-top:14px">Samples &mdash; actual raw ' +
      'data, side by side</h3>' +
      '<div class="side-by-side">' +
      sampleCard('New Relic', srow ? srow.nr : null) +
      sampleCard('Grafana', srow ? srow.grafana : null) +
      '</div>' +
      '<div class="btnbar" style="margin-top:4px">' +
      btnA('samples', w.panel_id, ref,
           srow ? 'Refresh samples' : 'Pull samples') +
      (srow ?
        '<span class="kv">pulled ' +
        esc((d.samples || {}).generated_at || '') + '</span>' :
        '<span class="kv">pulls a handful of raw rows / log ' +
        'lines from each source so you can verify this is the ' +
        'data you expect</span>') +
      '</div>' +
      signoffHtml(w.panel_id, ref, rv) +
      '<label for="' + eid + '">Translated query</label>' +
      '<div class="editor-wrap">' +
      '<textarea id="' + eid + '" data-uid="' + esc(dsUid) +
      '" spellcheck="false">' +
      esc(t.expr || t.query || t.queryText || '') + '</textarea>' +
      '<div class="ac" id="ac-' + w.panel_id + '-' + ref +
      '"></div></div>' +
      '<label for="why-' + w.panel_id + '-' + ref + '">Change ' +
      'note (recorded in the change log)</label>' +
      '<input id="why-' + w.panel_id + '-' + ref +
      '" placeholder="why this edit?" autocomplete="off">' +
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
  /* diagnosis findings for this panel */
  var flist = d._findings[w.panel_id] || [];
  if (flist.length) {
    html += '<h3 style="margin-top:16px">Findings for this panel' +
      '</h3>' + flist.map(function (f) {
        return findingHtml(f, true);
      }).join('');
  }
  return html;
}

function btnA(act, pid, ref, label, extra) {
  return '<button class="btn small ' + (extra || '') +
    '" data-act="' + act + '" data-pid="' + esc(pid) +
    '" data-ref="' + esc(ref) + '">' + label + '</button>';
}

function bindEditors(el, slug, d) {
  $all('button[data-act]', el).forEach(function (btn) {
    btn.onclick = function (ev) {
      ev.stopPropagation();
      editorAction(el, slug, d, btn);
    };
  });
  /* metric-name autocomplete on every editor */
  $all('textarea[id^="ed-"]', el).forEach(function (ta) {
    attachAutocomplete(ta, el);
  });
}

function attachAutocomplete(ta, root) {
  var uid = ta.getAttribute('data-uid') || '';
  if (!uid || uid.indexOf('$') === 0) return;
  var box = document.getElementById('ac-' + ta.id.slice(3));
  if (!box) return;
  var items = [], sel = -1;

  function hide() { box.classList.remove('show');
    box.innerHTML = ''; items = []; sel = -1; }

  function token() {
    var pos = ta.selectionStart || 0;
    var head = ta.value.slice(0, pos);
    var m = head.match(/[A-Za-z_:][A-Za-z0-9_:]*$/);
    return m ? { word: m[0], start: pos - m[0].length, end: pos }
             : null;
  }

  var lookup = debounce(async function () {
    var tk = token();
    if (!tk || tk.word.length < 2) { hide(); return; }
    var r;
    try {
      r = await api('/api/metrics?uid=' + encodeURIComponent(uid) +
                    '&q=' + encodeURIComponent(tk.word));
    } catch (e) { hide(); return; }
    items = (r.metrics || []).slice(0, 12);
    if (!items.length) { hide(); return; }
    sel = -1;
    box.innerHTML = items.map(function (m, i) {
      return '<button type="button" data-i="' + i + '">' + esc(m) +
        '</button>';
    }).join('');
    box.classList.add('show');
    $all('button', box).forEach(function (b) {
      b.onmousedown = function (ev) {
        ev.preventDefault();
        apply(items[+b.getAttribute('data-i')]);
      };
    });
  }, 250);

  function apply(name) {
    var tk = token();
    if (!tk) { hide(); return; }
    ta.value = ta.value.slice(0, tk.start) + name +
      ta.value.slice(tk.end);
    var p = tk.start + name.length;
    ta.setSelectionRange(p, p);
    ta.focus();
    hide();
  }

  ta.addEventListener('input', lookup);
  ta.addEventListener('blur', function () {
    setTimeout(hide, 150); });
  ta.addEventListener('keydown', function (ev) {
    if (!box.classList.contains('show')) return;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      sel = ev.key === 'ArrowDown' ?
        Math.min(sel + 1, items.length - 1) : Math.max(sel - 1, 0);
      $all('button', box).forEach(function (b, i) {
        b.classList.toggle('sel', i === sel);
      });
    } else if (ev.key === 'Enter' && sel >= 0) {
      ev.preventDefault(); apply(items[sel]);
    } else if (ev.key === 'Escape') { hide(); }
  });
}

async function editorAction(el, slug, d, btn) {
  var act = btn.getAttribute('data-act');
  var pidRaw = btn.getAttribute('data-pid');
  var pid = /^\d+$/.test(pidRaw) ? parseInt(pidRaw, 10) : pidRaw;
  var ref = btn.getAttribute('data-ref');
  var ta = document.getElementById('ed-' + pidRaw + '-' + ref);
  var expr = ta ? ta.value : '';
  var tres = document.getElementById('tres-' + pidRaw + '-' + ref);
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
        (res.error ? errorCard(
          'Grafana could not run this query.', res.error,
          { label: 'Check the datasource',
            href: '#/datasources' }) : '') + '</div>';
    } else if (act === 'samples') {
      await startJob('samples: ' + slug + ' panel ' + pidRaw,
        '/api/samples', { slug: slug, panel_id: pid, limit: 5 },
        null);
      toast('Samples pulled - compare the two sides, then ' +
            'Confirm or Reject', 'ok');
      rerenderWs();
    } else if (act.indexOf('rv-') === 0) {
      var noteEl = document.getElementById(
        'rvnote-' + pidRaw + '-' + ref);
      var verdict = act.slice(3);
      var rr = await api('/api/review',
        { slug: slug, panel_id: pid, refId: ref, verdict: verdict,
          note: noteEl ? noteEl.value.trim() : '' });
      var sm = rr.summary || {};
      toast('Recorded: ' + verdict + ' (' + (sm.confirmed || 0) +
            ' confirmed, ' + (sm.rejected || 0) + ' rejected)',
            verdict === 'rejected' ? 'err' : 'ok');
      rerenderWs();
    } else if (act === 'ai') {
      var box = document.getElementById('ai-' + pidRaw + '-' + ref);
      box.innerHTML = '<div class="ai-box">' + ico('sparkle', 13) +
        ' Asking the AI backend&hellip;</div>';
      var aiBody = { slug: slug, panel_id: pid, refId: ref,
                     expr: expr };
      var rvRow = ((d._reviews || {})[pid] || {})[ref];
      if (rvRow && rvRow.verdict === 'rejected') {
        aiBody.context = { human_review:
          'A human reviewer REJECTED this panel\'s data samples' +
          (rvRow.note ? ': ' + rvRow.note : '') +
          '. Take that verdict into account.' };
      }
      var a = await api('/api/ai/suggest', aiBody);
      var fixed = a.fixed_expr || '';
      box.innerHTML = '<div class="ai-box">' +
        '<span class="chip ' + (a.confidence === 'high' ? 'ok' :
          a.confidence === 'low' ? 'warn' : 'info') +
        ' conf">' + esc(a.confidence || 'suggestion') + '</span>' +
        '<div>' + esc(a.explanation || '') + '</div>' +
        (fixed ? '<label>Suggested query</label><pre>' + esc(fixed) +
          '</pre><button class="btn small primary" id="apply-' +
          pidRaw + '-' + ref + '">Apply to editor</button>' : '') +
        ((a.actions || []).length ? '<ul class="kv">' +
          a.actions.map(function (x) {
            return '<li>' + esc(x) + '</li>'; }).join('') +
          '</ul>' : '') +
        '</div>';
      if (fixed) {
        document.getElementById('apply-' + pidRaw + '-' + ref)
          .onclick = function (ev) {
            ev.stopPropagation();
            ta.value = fixed;
            toast('Suggestion applied to the editor - Test then ' +
                  'Save', 'ok');
          };
      }
    } else if (act === 'save' || act === 'push') {
      var whyEl = document.getElementById(
        'why-' + pidRaw + '-' + ref);
      var body = { slug: slug, panel_id: pid, refId: ref,
                   expr: expr, why: whyEl ? whyEl.value.trim() : '',
                   retest: false, push: act === 'push' };
      await api('/api/panel/update', body);
      toast(act === 'push' ?
            'Saved and pushed to Grafana' : 'Saved', 'ok');
    }
  } catch (e) {
    var ACT_LBL = { test: 'Test', samples: 'Pull samples',
                    ai: 'Ask AI', save: 'Save',
                    push: 'Save & Push' };
    var lbl = ACT_LBL[act] ||
      (act.indexOf('rv-') === 0 ? 'Record review' : act);
    if (tres && tres.isConnected) {
      tres.innerHTML = errorCard('&quot;' + esc(lbl) +
        '&quot; failed for this panel.', e.message);
    }
    toast(e.message, 'err');
  }
  busy(btn, false);
}

/* ------------------------------------------------ diagnostics tab */
function findingHtml(f, compact) {
  var fix = f.fix || {};
  var hasApply = fix.kind && fix.kind !== 'none';
  return '<div class="finding ' + esc(f.severity || 'info') +
    '" data-fid="' + esc(f.id) + '">' +
    '<div class="f-head">' + sevChip(f.severity) +
    chip(f.area || '', 'dim') +
    (f.panel_id != null && !compact ?
      chip('panel ' + f.panel_id, 'purple') : '') +
    '<span class="f-problem">' + esc(f.problem || '') + '</span>' +
    (hasApply ?
      '<button class="btn small" data-fixbtn="' + esc(f.id) +
      '">Fix&hellip;</button>' : '') +
    '</div>' +
    (f.evidence ? '<div class="f-evidence"><pre style="margin:0">' +
      esc(f.evidence) + '</pre></div>' : '') +
    (fix.description ? '<div class="kv" style="margin-top:6px">' +
      '<b>Remediation:</b> ' + esc(fix.description) + '</div>' :
      '') +
    '<div class="fix-slot" id="fixslot-' + cssId(String(f.id)) +
    '"></div></div>';
}

/* Inline inputs for an add-datasource fix that still needs values
   (e.g. the datasource URL). Field labels/placeholders come from the
   ds-templates cache when it is loaded; names otherwise. */
function needsInputHtml(fix) {
  var action = fix.action || {};
  var needs = (fix.kind === 'add-datasource' &&
               action.needs_input) || [];
  if (!needs.length) return '';
  var tpl = (App.templates || {})[action.type] || {};
  var fieldOf = {};
  (tpl.fields || []).forEach(function (fd) {
    fieldOf[fd.name] = fd; });
  return '<div class="kv" style="margin-top:6px"><b>Fill in the ' +
    'missing value(s) to create it right here:</b></div>' +
    needs.map(function (n) {
      var fd = fieldOf[n] || {};
      var secret = fd.secret ||
        /key|secret|password|token/i.test(n);
      return '<label>' + esc(fd.label || n) +
        (secret ? ' ' + chip('secret', 'purple') : '') +
        '</label><input ' + (secret ? 'type="password" ' : '') +
        'data-dsfx="' + esc(n) + '" autocomplete="off" ' +
        'placeholder="' + esc(fd.placeholder || '') + '">' +
        (fd.help ? '<div class="field-help">' + esc(fd.help) +
          '</div>' : '');
    }).join('') +
    '<div class="kv" style="margin-top:4px">or use the full form: ' +
    '<a href="#/datasources">open Datasources &rarr;</a></div>';
}

/* Preview-before-apply: expands the exact change, then Apply. */
function bindFixButtons(root, slug, findings, onDone) {
  loadTemplates().catch(function () {});  /* warm the ds-templates
    cache so needs-input fields get proper labels */
  $all('button[data-fixbtn]', root).forEach(function (btn) {
    btn.onclick = function (ev) {
      ev.stopPropagation();
      var fid = btn.getAttribute('data-fixbtn');
      var f = null;
      findings.forEach(function (x) {
        if (String(x.id) === String(fid)) f = x; });
      if (!f) return;
      var slot = document.getElementById('fixslot-' +
        cssId(String(fid)));
      if (!slot) return;
      if (slot.innerHTML) { slot.innerHTML = ''; return; }
      var fix = f.fix || {};
      slot.innerHTML = '<div class="fix-preview">' +
        '<div class="kv"><b>This fix will:</b> ' +
        esc(fix.description || fix.kind || '') + ' ' +
        chip(fix.kind || '', 'info') + '</div>' +
        (fix.action ?
          jsonDetails('Exact change payload', fix.action, true) :
          '<div class="kv">No machine action payload &mdash; ' +
          'follow the remediation text manually.</div>') +
        needsInputHtml(fix) +
        (fix.action ?
          '<div class="btnbar">' +
          '<button class="btn small primary" data-applyfix="' +
          esc(fid) + '">Apply</button>' +
          '<button class="btn small" data-applyfix="' + esc(fid) +
          '" data-push="1">Apply &amp; Push live</button>' +
          '<button class="btn small ghost" data-cancelfix="1">' +
          'Cancel</button></div>' : '') +
        '<div class="fix-result"></div></div>';
      $all('button[data-applyfix]', slot).forEach(function (ab) {
        ab.onclick = async function () {
          busy(ab, true);
          var out = $('.fix-result', slot);
          try {
            var body = { slug: slug, finding_id: f.id,
                         push: !!ab.getAttribute('data-push') };
            var vals = {};
            $all('input[data-dsfx]', slot).forEach(function (inp) {
              var v = inp.value.trim();
              if (v) vals[inp.getAttribute('data-dsfx')] = v;
            });
            if (Object.keys(vals).length) body.values = vals;
            var res = await api('/api/fix', body);
            out.innerHTML = '<div style="margin-top:8px">' +
              (res.applied ? chip('applied', 'ok') :
               chip('not applied', 'warn')) +
              ' <span class="kv">' + esc(res.detail || '') +
              '</span></div>';
            toast(res.applied ? 'Fix applied' :
                  'Fix not applied: ' + (res.detail || ''),
                  res.applied ? 'ok' : 'err');
            if (res.applied && onDone) {
              setTimeout(onDone, 900);
            }
          } catch (e) {
            out.innerHTML = errorCard(
              'The fix could not be applied.', e.message);
          }
          busy(ab, false);
        };
      });
      var cancel = $('button[data-cancelfix]', slot);
      if (cancel) {
        cancel.onclick = function () { slot.innerHTML = ''; };
      }
    };
  });
}

function bindPanelFixes(el, slug, d) {
  var all = ((d.diagnosis || {}).findings) || [];
  bindFixButtons(el, slug, all, rerenderWs);
}

function renderDiagnostics(el, slug, d) {
  var diag = d.diagnosis;
  var findings = ((diag || {}).findings || []).slice();
  findings.sort(function (a, b) {
    return (SEV_ORDER[a.severity] != null ?
            SEV_ORDER[a.severity] : 3) -
           (SEV_ORDER[b.severity] != null ?
            SEV_ORDER[b.severity] : 3);
  });
  var sum = (diag || {}).summary || {};
  var sumChips = ['blocker', 'warn', 'info'].map(function (k) {
    return sum[k] ? chip(sum[k] + ' ' + k, SEV_CLS[k]) : '';
  }).join('');
  el.innerHTML =
    '<div class="btnbar" style="margin:0 0 12px">' +
    '<button class="btn primary" id="dg-run">' +
    ico('search', 14) + 'Run diagnose' +
    '</button>' +
    '<span class="row" style="gap:6px">' +
    '<input type="checkbox" id="dg-push">' +
    '<span class="kv">push safe fixes live</span></span>' +
    '<button class="btn" id="dg-heal">' + ico('zap', 14) +
    'Auto-heal</button>' +
    '<span class="kv" style="margin-left:auto">' + sumChips +
    (diag && diag.generated_at ? ' <span class="kv">generated ' +
      esc(diag.generated_at) + '</span>' : '') + '</span></div>' +
    consoleHtml('dg-log', 'Diagnose / heal log') +
    '<div id="dg-heal-out"></div>' +
    '<div id="dg-list">' +
    (findings.length ? findings.map(function (f) {
      return findingHtml(f, false); }).join('') :
     diag ? '<div class="empty"><span class="eico">' +
       ico('checkcircle', 26) + '</span><b>No findings.</b>' +
       '<br>The last ' +
       'diagnosis came back clean. Re-run after changes to ' +
       'confirm.</div>' :
     '<div class="empty"><span class="eico">' + ico('search', 26) +
     '</span><b>Not diagnosed yet.</b><br>Run ' +
     'diagnose to get root causes and one-click fixes for every ' +
     'failing panel.</div>') +
    '</div>';

  $('#dg-run').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      var job = await startJob('diagnose: ' + slug, '/api/diagnose',
        { slug: slug }, logInto($('#dg-log')));
      var n = ((job.result || {}).findings || []).length;
      toast('Diagnosis complete: ' + n + ' finding(s)', 'ok');
      rerenderWs();
    } catch (e) { toast(e.message, 'err'); busy(btn, false); }
  };

  $('#dg-heal').onclick = async function () {
    var btn = this; busy(btn, true);
    var out = $('#dg-heal-out');
    out.innerHTML = '';
    try {
      var job = await startJob('auto-heal: ' + slug, '/api/heal',
        { slug: slug, push: $('#dg-push').checked },
        logInto($('#dg-log')));
      var res = job.result || {};
      var rounds = (res.rounds || []).map(function (r, i) {
        var applied = (r.applied || []).map(function (a) {
          return '<div class="kv">' +
            (a.applied ? chip('fixed', 'ok') :
             chip('skipped', 'dim')) + ' ' +
            esc(a.detail || a.kind || '') + '</div>';
        }).join('');
        return '<div style="margin:8px 0"><b class="kv">Round ' +
          (r.round || i + 1) + '</b> &mdash; ' +
          chip((r.fixed || 0) + ' fixed', r.fixed ? 'ok' : 'dim') +
          (applied || '<div class="kv">no safe fixes found</div>') +
          '</div>';
      }).join('');
      out.innerHTML = '<div class="card"><h2>Auto-heal result' +
        '</h2>' + chip(res.fixed + ' total fix(es)',
                       res.fixed ? 'ok' : 'dim') + ' ' +
        chip((res.remaining_findings || []).length + ' remaining',
             (res.remaining_findings || []).length ? 'warn' : 'ok') +
        rounds +
        '<div class="btnbar"><button class="btn small" ' +
        'id="dg-rediag">Re-run diagnose</button></div></div>';
      $('#dg-rediag').onclick = function () {
        $('#dg-run').click(); };
      toast('Auto-heal: ' + (res.fixed || 0) + ' fix(es) applied',
            'ok');
      refreshState();
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false);
  };

  bindFixButtons(el, slug, findings, rerenderWs);
}

/* ------------------------------------------------ verify tab */
function signoffCardHtml(slug, d) {
  var c = reviewCounts(d);
  var rvs = (d.review || {}).reviews || {};
  var rejected = [];
  Object.keys(rvs).forEach(function (k) {
    if ((rvs[k] || {}).verdict === 'rejected') rejected.push(rvs[k]);
  });
  var chips = '';
  if (c.confirmed || c.rejected || c.unsure) {
    chips = chip(c.confirmed + ' confirmed', 'ok') +
      chip(c.rejected + ' rejected', c.rejected ? 'err' : 'dim') +
      chip(c.unsure + ' not sure', c.unsure ? 'warn' : 'dim') +
      (c.total ? chip(Math.max(0, c.total - c.confirmed -
        c.rejected - c.unsure) + ' unreviewed', 'dim') : '');
  }
  var rejList = rejected.map(function (r) {
    var p = (d._panelMap || {})[r.panel_id] || {};
    return '<div class="err-text">' + chip('blocker', 'err') +
      ' panel ' + esc(r.panel_id) + ' [' + esc(r.refId || 'A') +
      '] ' + esc(p.title || '') +
      (r.note ? ' &mdash; ' + esc(r.note) : '') + '</div>';
  }).join('');
  var body;
  if (!c.confirmed && !c.rejected && !c.unsure) {
    body = '<div class="kv">No panels reviewed yet. On the ' +
      '<a href="#/dash/' + encodeURIComponent(slug) +
      '">Panels tab</a>, expand a panel, hit <b>Pull samples</b> ' +
      'to see actual New Relic rows next to the same data in ' +
      'Grafana (e.g. CloudWatch logs via NR vs Loki), then ' +
      'Confirm or Reject. A fully confirmed dashboard is graded ' +
      'human-verified; rejected panels block readiness.</div>';
  } else {
    body = '<div style="margin-bottom:6px">' + chips + '</div>' +
      (rejList ||
       (c.total && c.confirmed === c.total ?
        '<div class="kv">' + chip('human-verified', 'ok') +
        ' every panel\'s live samples were confirmed.</div>' :
        '<div class="kv">Keep going: unreviewed panels are ' +
        'listed on the Panels tab.</div>'));
  }
  return '<div class="card"><h2>Human sign-off</h2>' + body +
    '</div>';
}

function renderVerify(el, slug, d) {
  var par = d.parity;
  var parityRows = ((par || {}).panels || []).map(function (p) {
    return '<tr><td class="num">' + esc(p.panel_id) + '</td>' +
      '<td><b>' + esc(p.panel_title || '') + '</b> ' +
      chip(p.refId || '', 'dim') + '</td>' +
      '<td>' + verdictChip(p.verdict, p.ratio, '') + '</td>' +
      '<td class="num mono">' + fmtNum((p.nr_summary || {}).last) +
      ' vs ' + fmtNum((p.gf_summary || {}).last) + '</td>' +
      '<td class="kv">' + truncHtml(p.detail || '', 160) +
      '</td></tr>';
  }).join('');
  var sum = (par || {}).summary || {};
  var sumChips = Object.keys(sum).sort().map(function (k) {
    return chip(sum[k] + ' ' + k, VERDICT_CLS[k] || 'dim');
  }).join('');

  el.innerHTML =
    '<div class="card"><h2>Verify: New Relic vs Grafana</h2>' +
    '<p class="kv">Runs every panel query on BOTH sides over the ' +
    'same time range and compares the numbers.</p>' +
    '<div class="row">' +
    '<label style="margin:0">From</label>' +
    '<input id="vf-from" value="now-1h" style="max-width:110px">' +
    '<label style="margin:0">To</label>' +
    '<input id="vf-to" value="now" style="max-width:110px">' +
    '<button class="btn primary" id="vf-run">' +
    ico('play', 14) + 'Run parity</button>' +
    '<span style="margin-left:auto">' + sumChips + '</span>' +
    '</div>' +
    consoleHtml('vf-log', 'Parity / import log') +
    (parityRows ?
      '<div class="tablewrap tall" style="margin-top:12px">' +
      '<table class="zebra">' +
      '<thead><tr><th class="num">Id</th><th>Panel</th>' +
      '<th>Verdict</th><th class="num">Last NR vs GF</th>' +
      '<th>Detail</th></tr></thead><tbody>' +
      parityRows + '</tbody></table></div>' :
      '<div class="empty" style="margin-top:12px">' +
      '<span class="eico">' + ico('search', 26) + '</span>' +
      'Not compared yet. Run parity to prove the migrated ' +
      'panels show the same data.</div>') +
    '</div>' +
    signoffCardHtml(slug, d) +
    '<div class="grid2">' +
    '<div class="card"><h2>Readiness</h2><div id="vf-ready">' +
    '<div class="kv">Loading readiness&hellip;</div></div></div>' +
    '<div class="card"><h2>Import to Grafana</h2>' +
    '<label>Folder (empty = General)</label>' +
    '<input id="vf-folder" placeholder="General">' +
    '<div class="row" style="margin-top:8px">' +
    '<input type="checkbox" id="vf-ow" checked>' +
    '<span class="kv">overwrite existing</span></div>' +
    '<div class="btnbar"><button class="btn primary" id="vf-imp">' +
    ico('play', 14) + 'Import dashboard</button></div>' +
    '<div id="vf-imp-out"></div></div>' +
    '</div>' +
    '<div class="card"><h2>' + ico('download', 14) +
    'Download</h2>' +
    '<p class="kv">Grab the current (post-fix) dashboard JSON or ' +
    'the full package.</p>' +
    '<div class="btnbar" id="vf-dl">' +
    '<a class="btn" id="dl-json" href="/download/dashboard/' +
    encodeURIComponent(slug) + '.json" download>' +
    ico('download', 13) + 'Dashboard JSON' +
    '</a>' +
    '<a class="btn" id="dl-pkg" href="/download/package/' +
    encodeURIComponent(slug) + '.zip" download>' +
    ico('download', 13) + 'Package .zip</a>' +
    '<a class="btn" id="dl-all" href="/download/all.zip" download>' +
    ico('download', 13) + 'Everything .zip</a></div></div>';

  /* readiness ring + armed download buttons */
  api('/api/readiness?slug=' + encodeURIComponent(slug))
    .then(function (r) {
      var box = $('#vf-ready');
      if (!box) return;
      box.innerHTML = '<div class="row" style="gap:16px">' +
        ring(r.score, r.grade, 84) +
        '<div><div style="font-weight:700;font-size:16px">' +
        chip(r.grade || 'unknown',
             r.grade === 'ready' ? 'ok' :
             r.grade === 'almost' ? 'warn' :
             r.grade === 'blocked' ? 'err' : 'dim') + '</div>' +
        ((r.reasons || []).length ? '<ul class="kv" ' +
          'style="margin:8px 0 0;padding-left:18px">' +
          r.reasons.map(function (x) {
            return '<li>' + esc(x) + '</li>'; }).join('') +
          '</ul>' : '<div class="kv" style="margin-top:6px">No ' +
          'outstanding issues.</div>') + '</div></div>';
      var armed = r.grade === 'ready' || r.grade === 'almost';
      ['#dl-json', '#dl-pkg', '#dl-all'].forEach(function (id) {
        var a = $(id);
        if (!a) return;
        if (armed) { a.classList.add('armed'); a.title = ''; }
        else {
          a.title = 'Readiness is ' + (r.grade || 'unknown') +
            ' - the download still works, but imported panels ' +
            'may show no data. Fix blockers first.';
        }
      });
    }).catch(function (e) {
      var box = $('#vf-ready');
      if (box) {
        box.innerHTML = '<div class="kv">Readiness unavailable: ' +
          esc(e.message) + '</div>';
      }
      ['#dl-json', '#dl-pkg', '#dl-all'].forEach(function (id) {
        var a = $(id);
        if (a) a.title = 'Readiness could not be computed yet - ' +
          'run data tests and parity first.';
      });
    });

  $('#vf-run').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      var job = await startJob('parity: ' + slug, '/api/parity',
        { slug: slug, from: $('#vf-from').value.trim() || 'now-1h',
          to: $('#vf-to').value.trim() || 'now' },
        logInto($('#vf-log')));
      toast('Parity score: ' + (job.result || {}).score, 'ok');
      rerenderWs();
    } catch (e) { toast(e.message, 'err'); busy(btn, false); }
    refreshState();
  };

  $('#vf-imp').onclick = async function () {
    var btn = this; busy(btn, true);
    var out = $('#vf-imp-out');
    try {
      var job = await startJob('import: ' + slug,
        '/api/grafana/import',
        { slugs: [slug], folder: $('#vf-folder').value.trim(),
          overwrite: $('#vf-ow').checked }, logInto($('#vf-log')));
      var r = (job.result.results || [])[0] || {};
      if (r.status === 'ok') {
        out.innerHTML = '<div style="margin-top:8px">' +
          chip('imported', 'ok') +
          (r.url ? ' <a href="' + esc(r.url) +
           '" target="_blank" rel="noopener">open in Grafana ' +
           '&rarr;</a>' : '') + '</div>';
        toast('Imported' + (r.url ? ': ' + r.url : ''), 'ok');
      } else {
        out.innerHTML = errorCard(
          'The dashboard could not be imported into Grafana.',
          r.error || 'import failed',
          { label: 'Check the Grafana connection',
            href: '#/connect' });
        toast(r.error || 'import failed', 'err');
      }
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
    view.innerHTML = '<h1>Import</h1><div class="empty">' +
      '<span class="eico">' + ico('inbox', 26) + '</span><b>No ' +
      'dashboards to import.</b><br>Run <a href="#/convert">' +
      'Fetch &amp; Convert</a> first.</div>';
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
    '<span class="row" style="gap:6px">' +
    '<input type="checkbox" id="imp-ow" checked>' +
    '<span class="kv">overwrite</span></span>' +
    '<button class="btn primary" id="imp-run">' +
    ico('play', 14) + 'Import selected' +
    '</button></div>' +
    consoleHtml('imp-log', 'Import log') + '</div>' +
    '<div class="card"><h2>' + ico('download', 14) +
    'Bulk download</h2>' +
    '<div class="btnbar">' +
    '<a class="btn" href="/download/all.zip" download>' +
    ico('download', 13) + 'Everything .zip</a></div></div>';

  $('#imp-run').onclick = async function () {
    var btn = this;
    var slugs = $all('.imp-cb').filter(function (c) {
      return c.checked; }).map(function (c) { return c.value; });
    if (!slugs.length) { toast('Nothing selected', 'err'); return; }
    busy(btn, true);
    try {
      var job = await startJob('bulk import',
        '/api/grafana/import', {
          slugs: slugs, folder: $('#imp-folder').value.trim(),
          overwrite: $('#imp-ow').checked
        }, logInto($('#imp-log')));
      (job.result.results || []).forEach(function (r) {
        var el = document.getElementById('imp-res-' + r.slug);
        if (!el) return;
        el.innerHTML = r.status === 'ok' ?
          chip('imported', 'ok') +
          (r.url ? ' <a href="' + esc(r.url) +
           '" target="_blank" rel="noopener">open</a>' : '') :
          chip('failed', 'err', r.error);
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
      $('#ch-table').innerHTML = '<div class="empty">' +
        '<span class="eico">' + ico('inbox', 26) + '</span><b>No ' +
        'changes recorded yet.</b><br>Edits made in the panel ' +
        'editor (Save / Save &amp; Push) and applied fixes land ' +
        'here.</div>';
      return;
    }
    var rows = list.map(function (c) {
      return '<tr><td class="kv mono">' +
        esc(String(c.ts || '').replace('T', ' ').slice(0, 19)) +
        '</td><td class="mono">' + esc(c.slug || '') + '</td>' +
        '<td>' + chip(c.action, 'dim') +
        '<div class="kv">' + esc(c.target || '') + '</div></td>' +
        '<td>' +
        (asText(c.before) ? '<div class="kv">before</div>' +
          '<pre style="margin:0 0 4px">' +
          truncHtml(asText(c.before), 300) + '</pre>' : '') +
        '<div class="kv">after</div><pre style="margin:0">' +
        truncHtml(asText(c.after), 300) +
        '</pre></td><td>' + esc(c.why || '') +
        '<div class="kv">' + esc(c.source || '') + '</div></td>' +
        '</tr>';
    }).join('');
    $('#ch-table').innerHTML = '<div class="card"><div class=' +
      '"tablewrap tall"><table class="zebra"><thead><tr>' +
      '<th>When</th><th>Dashboard' +
      '</th><th>Action</th><th>Before &rarr; After</th><th>Why' +
      '</th></tr></thead><tbody>' + rows +
      '</tbody></table></div></div>';
  }
  function asText(v) {
    if (v == null || v === '') return '';
    return typeof v === 'string' ? v : JSON.stringify(v);
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
                          (s ? '?slug=' + encodeURIComponent(s)
                             : ''));
      var txt = JSON.stringify(cfg, null, 2);
      $('#ch-cfg').innerHTML = '<div class="card"><h2>' +
        ico('wrench', 14) + 'Suggested config overlay</h2>' +
        '<div class="kv">Merge this into your ' +
        'mapping config (convert -c) to make these fixes ' +
        'permanent.</div><pre id="ch-cfg-pre"></pre>' +
        '<button class="btn small" id="ch-copy">' +
        ico('copy', 13) + 'Copy JSON</button>' +
        '</div>';
      $('#ch-cfg-pre').textContent = txt;
      $('#ch-copy').onclick = function () { copyText(txt, this); };
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

/* The AI view is AI-first: it packages every artifact into a
   context bundle an AI can consume, lets you troubleshoot the
   migration + LGTM stack with that bundle, wires a Grafana MCP
   server into your local agent, and keeps the free-form chat. */
function ensureAic() {
  if (!App.aic) {
    App.aic = { slug: '', context: null, ctxErr: null,
      ctxLoading: false, answer: null, ansErr: null,
      ansBusy: false, question: '', mcpKind: 'claude',
      mcpConfig: null, mcpErr: null, mcpTarget: '',
      probe: null, probeErr: null };
  }
  return App.aic;
}

async function vAI(view) {
  crumb('AI Assistant');
  var s = App.state || await api('/api/state');
  App.state = s;
  ensureAic();
  if (!App.dashboards.length) {
    try {
      App.dashboards =
        (await api('/api/dashboards')).dashboards || [];
    } catch (e) { /* scope selector just offers whole-instance */ }
  }
  view.innerHTML =
    '<h1>AI Assistant</h1>' +
    '<p class="lead">Everything nr2grafana knows &mdash; your ' +
    'dashboards, diagnoses, parity, cost and the ' +
    stackTermRaw('The LGTM stack: Loki (logs), Grafana, Tempo ' +
      '(traces) and Mimir (long-term Prometheus metrics).') +
    ' LGTM deep-dive &mdash; packaged for an AI to troubleshoot ' +
    'and optimize the whole stack without risking it.</p>' +
    '<section id="ai-context" class="card"></section>' +
    '<section id="ai-troubleshoot" class="card"></section>' +
    '<section id="ai-mcp" class="card"></section>' +
    '<section id="ai-chat"></section>';
  renderAiContext();
  renderAiTroubleshoot();
  renderAiMcp();
  renderAiChat(view);
}

/* ---- AI context bundle ---- */
function aiScopeSelect(id, slug) {
  var opts = '<option value="">Whole workspace</option>' +
    (App.dashboards || []).map(function (d) {
      var sg = d.slug || d.name || '';
      return '<option value="' + esc(sg) + '"' +
        (sg === slug ? ' selected' : '') + '>' + esc(sg) +
        '</option>';
    }).join('');
  return '<select id="' + id + '">' + opts + '</select>';
}

function renderAiContext() {
  var el = $('#ai-context'); if (!el) return;
  var a = App.aic;
  var q = a.slug ? '?slug=' + encodeURIComponent(a.slug) : '';
  el.innerHTML =
    '<h2>' + ico('sparkle', 15) + ' AI context bundle</h2>' +
    '<p class="kv">A compact, structured snapshot of every ' +
    'artifact &mdash; summaries and top-N, not raw dumps &mdash; ' +
    'with a legend and a task preamble, ready to paste into any ' +
    'AI. Secret-looking values are redacted.</p>' +
    '<div class="ai-actions">' +
    '<label style="margin:0">Scope</label>' +
    aiScopeSelect('aic-scope', a.slug) +
    '<span class="grow"></span>' +
    '<button class="btn" id="aic-refresh" type="button">' +
    ico('refresh', 13) + ' Build bundle</button>' +
    '<button class="btn" id="aic-copy" type="button">' +
    ico('copy', 13) + ' Copy AI context</button>' +
    '<a class="btn" href="/download/ai-context.md' + q + '">' +
    ico('download', 13) + ' .md</a>' +
    '<a class="btn" href="/download/ai-context.json' + q + '">' +
    ico('download', 13) + ' .json</a></div>' +
    '<div id="aic-body"></div>';
  $('#aic-scope').onchange = function () {
    a.slug = this.value; a.context = null; renderAiContext();
    loadAiContext();
  };
  $('#aic-refresh').onclick = function () {
    a.context = null; loadAiContext();
  };
  $('#aic-copy').onclick = async function () {
    var btn = this; busy(btn, true);
    try {
      var md = await apiText('/api/ai/context?format=markdown' +
        (a.slug ? '&slug=' + encodeURIComponent(a.slug) : ''));
      copyText(md, btn);
    } catch (e) { toast(e.message, 'err'); }
    busy(btn, false);
  };
  renderAicBody();
  if (!a.context && !a.ctxLoading && !a.ctxErr) loadAiContext();
}

async function loadAiContext() {
  var a = App.aic;
  a.ctxLoading = true; a.ctxErr = null; renderAicBody();
  try {
    var q = a.slug ? '?slug=' + encodeURIComponent(a.slug) : '';
    var r = await api('/api/ai/context' + q);
    a.context = (r && r.context) || r || null;
  } catch (e) { a.ctxErr = e.message; }
  a.ctxLoading = false; renderAicBody();
}

/* Keys rendered as prose, not JSON, when present. */
var CTX_META = { schema: 1, generated_at: 1, redacted: 1,
  version: 1 };
function renderAicBody() {
  var el = $('#aic-body'); if (!el) return;
  var a = App.aic;
  if (a.ctxLoading) {
    el.innerHTML = '<div class="skel" style="height:70px"></div>';
    return;
  }
  if (a.ctxErr) {
    el.innerHTML = errorCard('Could not build the AI context ' +
      'bundle.', a.ctxErr); return;
  }
  var ctx = a.context;
  if (!ctx) { el.innerHTML = ''; return; }
  var preamble = ctx.task || ctx.preamble || ctx.task_preamble ||
    '';
  var legend = ctx.legend;
  var html = '';
  if (preamble) {
    html += '<div class="ctx-legend"><b>Task preamble</b><br>' +
      esc(String(preamble)) + '</div>';
  }
  if (legend && typeof legend === 'object') {
    html += jsonDetails('Legend — what each field means', legend);
  } else if (typeof legend === 'string' && legend) {
    html += '<div class="ctx-legend">' + esc(legend) + '</div>';
  }
  var keys = Object.keys(ctx).filter(function (k) {
    return !CTX_META[k] && k !== 'task' && k !== 'preamble' &&
      k !== 'task_preamble' && k !== 'legend';
  });
  var avail = ctx.available_artifacts || [];
  var hasDash = ctx.dashboard &&
    Object.keys(ctx.dashboard).length > 0;
  /* Nothing to summarize yet: don't dump the raw "missing artifacts"
     list on a first-run user -- show a clear next action instead. */
  if (!hasDash && !avail.length) {
    html += '<div class="empty"><span class="eico">' +
      ico('sparkle', 26) + '</span><b>Nothing to bundle yet.</b><br>' +
      'Convert a New Relic dashboard or run a Stack deep-dive, and ' +
      'this bundle fills with the diagnosis, parity, cost and stack ' +
      'findings an AI needs to troubleshoot everything at once.' +
      '<div class="btnbar" style="justify-content:center;' +
      'margin-top:var(--s3)">' +
      '<a class="btn primary" href="#/convert">' + ico('download', 13) +
      ' Fetch &amp; convert a dashboard</a>' +
      '<a class="btn" href="#/stack">' + ico('zap', 13) +
      ' Run a Stack deep-dive</a></div></div>';
    el.innerHTML = html;
    return;
  }
  if (!keys.length) {
    html += '<div class="empty">The bundle is empty &mdash; ' +
      'convert a dashboard or run a deep-dive first.</div>';
  }
  keys.forEach(function (k) {
    var v = ctx[k];
    if (v == null) return;
    if (typeof v === 'object' && !Object.keys(v).length) return;
    html += jsonDetails(humanize(k), v);
  });
  el.innerHTML = html;
}

/* ---- troubleshoot with AI ---- */
function renderAiTroubleshoot() {
  var el = $('#ai-troubleshoot'); if (!el) return;
  var a = App.aic;
  var enabled = aiBackend((App.state || {}).session) !== 'none';
  var head = '<h2>' + ico('zap', 15) +
    ' Troubleshoot with AI</h2>';
  if (!enabled) {
    el.innerHTML = head + '<div class="empty"><span class="eico">' +
      ico('sparkle', 26) + '</span><b>AI is not configured.</b>' +
      '<br>Add an Anthropic API key or a local console AI command ' +
      'in <a href="#/connect">Connect</a> to troubleshoot with ' +
      'the full context bundle.</div>';
    return;
  }
  el.innerHTML = head +
    '<p class="kv">Ask a question and the full context bundle ' +
    'above is sent with it &mdash; the AI can reason over your ' +
    'dashboards and the whole LGTM stack at once.</p>' +
    '<div class="row" style="align-items:stretch">' +
    '<textarea id="ts-q" style="flex:1;min-height:60px" ' +
    'placeholder="e.g. Why do half my migrated panels show ' +
    'no data, and where can I safely cut LGTM cost?">' +
    esc(a.question || '') + '</textarea></div>' +
    '<div class="btnbar"><button class="btn primary" id="ts-go" ' +
    'type="button">' + ico('play', 13) + ' Ask</button>' +
    '<span class="kv">scope: ' +
    esc(a.slug || 'whole workspace') + '</span></div>' +
    '<div id="ts-answer"></div>';
  var go = $('#ts-go');
  go.onclick = function () { onTroubleshoot(go); };
  renderTsAnswer();
}

function renderTsAnswer() {
  var el = $('#ts-answer'); if (!el) return;
  var a = App.aic;
  if (a.ansBusy) {
    el.innerHTML = '<div class="ans-box"><span class="skel" ' +
      'style="width:14px;height:14px;border-radius:50%;' +
      'display:inline-block"></span> thinking&hellip;</div>';
    return;
  }
  if (a.ansErr) {
    el.innerHTML = errorCard('The AI could not answer.', a.ansErr);
    return;
  }
  if (!a.answer) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="ans-box">' +
    (a.answer.backend ? '<span class="chip info" ' +
      'style="float:right">' + esc(a.answer.backend) +
      '</span>' : '') +
    mdLite(a.answer.answer || a.answer.reply || '') + '</div>';
}

async function onTroubleshoot(btn) {
  var a = App.aic;
  var ta = $('#ts-q');
  a.question = ta ? ta.value.trim() : '';
  if (!a.question) { toast('Type a question first', 'err'); return; }
  a.ansBusy = true; a.ansErr = null; a.answer = null;
  renderTsAnswer(); busy(btn, true);
  try {
    var body = { question: a.question };
    if (a.slug) body.slug = a.slug;
    var r = await api('/api/ai/troubleshoot', body);
    if (r && r.job) r = await pollJob(r.job);
    a.answer = r || {};
  } catch (e) { a.ansErr = e.message; toast(e.message, 'err'); }
  a.ansBusy = false; busy(btn, false); renderTsAnswer();
  refreshState();
}

/* ---- Grafana MCP panel ---- */
function renderAiMcp() {
  var el = $('#ai-mcp'); if (!el) return;
  var a = App.aic;
  el.innerHTML =
    '<h2>' + ico('database', 15) + ' Grafana ' +
    stackTermRaw('MCP (Model Context Protocol): a standard way to ' +
      'give an AI agent live tools. The Grafana MCP server lets ' +
      'your agent query Grafana, datasources and dashboards ' +
      'directly.') + ' MCP</h2>' +
    '<p class="kv">Generate a ready MCP config that wires the ' +
    'Grafana MCP server into your local AI (Claude, Kiro). The ' +
    'service-account token is referenced from an environment ' +
    'variable &mdash; never written into the file.</p>' +
    '<div class="ai-actions">' +
    '<label style="margin:0">Target agent</label>' +
    '<div class="seg" role="group">' +
    ['claude', 'kiro', 'generic'].map(function (k) {
      return '<button type="button" data-mcpkind="' + k + '"' +
        (a.mcpKind === k ? ' class="on"' : '') + '>' +
        esc(k === 'claude' ? 'Claude' : k === 'kiro' ? 'Kiro' :
          'Generic') + '</button>';
    }).join('') + '</div>' +
    '<span class="grow"></span>' +
    '<button class="btn primary" id="mcp-gen" type="button">' +
    ico('wrench', 13) + ' Generate config</button></div>' +
    '<div id="mcp-config"></div>' +
    '<div class="ai-actions" style="margin-top:var(--s3)">' +
    '<label style="margin:0">Probe an MCP server</label>' +
    '<input id="mcp-target" placeholder="http://localhost:8000/sse ' +
    'or a stdio command" value="' + esc(a.mcpTarget || '') +
    '" style="flex:1;min-width:200px" spellcheck="false" ' +
    'autocomplete="off">' +
    '<button class="btn" id="mcp-probe" type="button">' +
    ico('play', 13) + ' Probe MCP server</button></div>' +
    '<div id="mcp-probe-out"></div>';
  $all('[data-mcpkind]', el).forEach(function (b) {
    b.onclick = function () {
      a.mcpKind = b.getAttribute('data-mcpkind');
      a.mcpConfig = null; renderAiMcp();
    };
  });
  $('#mcp-gen').onclick = function () { loadMcpConfig(this); };
  $('#mcp-probe').onclick = function () { onMcpProbe(this); };
  renderMcpConfig();
  renderMcpProbe();
}

async function loadMcpConfig(btn) {
  var a = App.aic;
  busy(btn, true);
  try {
    var r = await api('/api/mcp/config?kind=' +
      encodeURIComponent(a.mcpKind));
    a.mcpConfig = (r && r.config) || r || null;
    a.mcpErr = null;
  } catch (e) { a.mcpErr = e.message; toast(e.message, 'err'); }
  busy(btn, false); renderMcpConfig();
}

function renderMcpConfig() {
  var el = $('#mcp-config'); if (!el) return;
  var a = App.aic;
  if (a.mcpErr) {
    el.innerHTML = errorCard('Could not generate the MCP config.',
      a.mcpErr); return;
  }
  if (!a.mcpConfig) { el.innerHTML = ''; return; }
  var txt = typeof a.mcpConfig === 'string' ? a.mcpConfig :
    JSON.stringify(a.mcpConfig, null, 2);
  var id = 'mcpcfg-' + uid();
  el.innerHTML = '<div class="rec-cfg"><div class="cfg-bar">' +
    '<label style="margin:0">MCP config (' + esc(a.mcpKind) +
    ')</label><span style="margin-left:auto"></span>' +
    copyBtn(id, 'MCP config') + '</div>' +
    '<pre id="' + id + '">' + esc(txt) + '</pre>' +
    '<div class="cfg-note">Set ' +
    '<span class="mono">GRAFANA_SERVICE_ACCOUNT_TOKEN</span> in ' +
    'your environment; the token is never written into this ' +
    'file.</div></div>';
}

async function onMcpProbe(btn) {
  var a = App.aic;
  var inp = $('#mcp-target');
  a.mcpTarget = inp ? inp.value.trim() : '';
  a.probe = null; a.probeErr = null;
  busy(btn, true); renderMcpProbe();
  try {
    var body = {};
    var tgt = a.mcpTarget;
    if (tgt) {
      if (/^https?:\/\//i.test(tgt)) body.url = tgt;
      else body.command = tgt;
    }
    a.probe = await api('/api/mcp/probe', body);
  } catch (e) { a.probeErr = e.message; toast(e.message, 'err'); }
  busy(btn, false); renderMcpProbe();
}

function renderMcpProbe() {
  var el = $('#mcp-probe-out'); if (!el) return;
  var a = App.aic;
  if (a.probeErr) {
    el.innerHTML = errorCard('MCP probe failed.', a.probeErr);
    return;
  }
  var p = a.probe;
  if (!p) { el.innerHTML = ''; return; }
  if (p.ok === false || p.error) {
    el.innerHTML = errorCard('The MCP server did not respond.',
      p.error || 'no tools returned'); return;
  }
  var tools = p.tools || [];
  el.innerHTML = '<div class="ai-box">' +
    chip('connected', 'ok') + ' ' +
    chip(tools.length + ' tool' + (tools.length === 1 ? '' : 's'),
      'info') +
    (tools.length ? '<div style="margin-top:8px">' +
      tools.map(function (t) {
        var nm = typeof t === 'string' ? t : (t.name || '');
        var desc = (t && t.description) || '';
        return '<div class="mcp-tool"><span class="mono">' +
          esc(nm) + '</span><span class="desc">' +
          esc(truncStr(desc, 120)) + '</span></div>';
      }).join('') + '</div>' : '') + '</div>';
}

/* ---- assistant chat (unchanged behaviour) ---- */
function renderAiChat(view) {
  var el = $('#ai-chat'); if (!el) return;
  var enabled = aiBackend((App.state || {}).session) !== 'none';
  if (!enabled) {
    el.innerHTML = '<h2>' + ico('sparkle', 15) +
      ' Assistant chat</h2><div class="empty">' +
      'Configure an AI backend in <a href="#/connect">Connect' +
      '</a> to chat about individual queries.</div>';
    return;
  }
  var msgs = App.ai.map(function (m) {
    return '<div class="msg ' + m.role + '">' +
      mdLite(m.content) + '</div>';
  }).join('');
  el.innerHTML = '<h2>' + ico('sparkle', 15) +
    ' Assistant chat</h2>' +
    '<div class="card"><div id="chatlog">' + (msgs ||
      '<div class="kv">Try: &quot;Why would ' +
      'http_server_request_duration_seconds_bucket return no ' +
      'data?&quot;</div>') + '</div>' +
    '<div class="row" style="margin-top:10px">' +
    '<textarea id="ai-input" style="flex:1;min-height:44px" ' +
    'placeholder="Ask the assistant..."></textarea>' +
    '<button class="btn primary" id="ai-send">Send</button>' +
    '</div></div>';
  var logEl = $('#chatlog');
  logEl.scrollTop = logEl.scrollHeight;
  async function send() {
    var input = $('#ai-input');
    var text = input.value.trim();
    if (!text || App.aiBusy) return;
    App.ai.push({ role: 'user', content: text });
    input.value = '';
    App.aiBusy = true;
    renderAiChat(view);
    try {
      var r = await api('/api/ai/chat', { messages: App.ai });
      App.ai.push({ role: 'assistant', content: r.reply || '' });
    } catch (e) {
      App.ai.push({ role: 'assistant',
                    content: 'Error: ' + e.message });
      toast(e.message, 'err');
    }
    App.aiBusy = false;
    renderAiChat(view);
    refreshState();
  }
  $('#ai-send').onclick = send;
  $('#ai-input').onkeydown = function (ev) {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault(); send();
    }
  };
}

/* ============================================ datasource flow (3c) */
/* Normalize a flow block to a list of family entries. The real
   compare.datasource_flow returns {families:[...]}; the create/verify
   paths may hand back a single family dict -- handle both so the UI
   never breaks on either shape. */
function flowFamilies(flow) {
  if (!flow) return [];
  if (Array.isArray(flow.families)) return flow.families;
  if (flow.family != null || flow.panels_total != null) return [flow];
  return [];
}

/* Compact health + "N/total flowing" badges for one ds family. */
function flowBadgeHtml(fam) {
  if (!fam) return '';
  var h = fam.health || {};
  var hcls = h.status === 'ok' ? 'ok' :
    h.status === 'error' ? 'err' : 'warn';
  var wd = fam.panels_with_data || 0, tot = fam.panels_total || 0;
  var fcls = tot === 0 ? 'dim' : wd >= tot ? 'ok' :
    wd > 0 ? 'info' : 'warn';
  return '<span class="chip ' + hcls + '" title="' +
    esc(h.message || ('datasource health: ' + (h.status ||
      'unknown'))) + '"><span class="health-dot ' + hcls +
    '"></span>' + esc(h.status || 'unknown') + '</span>' +
    '<span class="chip ' + fcls + '" title="panels receiving real ' +
    'data through this datasource">' + wd + '/' + tot +
    ' flowing</span>' +
    (fam.panels_error ? chip(fam.panels_error + ' error', 'err') : '');
}

/* The full before/after "watch it flow" card for one ds family:
   0 had data -> N now flowing, a real sample chart proving data, the
   health status and a Re-check button. Still-no-data -> error card. */
function flowFamilyHtml(fam, uid, slug) {
  if (!fam) return '';
  var after = fam.panels_with_data || 0;
  var tot = fam.panels_total || 0;
  var nf = (fam.newly_flowing || []).length;
  var before = Math.max(0, after - nf);
  var health = fam.health || {};
  var head = '<div class="flow-head"><b>' +
    esc(fam.family || 'datasource') + '</b>' +
    '<div class="flow-badges">' + flowBadgeHtml(fam) + '</div></div>';
  var ba = '<div class="flow-ba">' +
    '<div class="flow-num"><div class="flow-n before">' + before +
    '</div><div class="kv">had data</div></div>' +
    '<div class="flow-arrow">' + ico('arrow', 20) + '</div>' +
    '<div class="flow-num"><div class="flow-n after">' + after +
    '</div><div class="kv">now flowing</div></div>' +
    '<div class="kv" style="margin-left:8px">of ' + tot +
    ' panels' + (nf ? ' &middot; ' + nf + ' newly lit up' : '') +
    '</div></div>';
  var sample = (fam.sample_series && fam.sample_series.length) ?
    '<div class="flow-sample"><div class="kv" ' +
    'style="margin-bottom:4px">Live sample proving data flows:' +
    '</div>' + chart('timeseries', { series: fam.sample_series },
                     { height: 120 }) + '</div>' : '';
  var noData = after === 0 ?
    errorCard('No panels are receiving data through ' +
      esc(fam.family || 'this datasource') + ' yet.',
      health.message || 'The datasource was created but no panel ' +
      'query returned data over this range. Check the URL / auth, ' +
      'then re-check flow.',
      { label: 'Open Datasources', href: '#/datasources' },
      'warn') : '';
  var recheck = (uid && slug) ? '<div class="btnbar">' +
    '<button class="btn small" data-flowrecheck="' + esc(uid) +
    '" data-slug="' + esc(slug) + '">' + ico('refresh', 12) +
    'Re-check flow</button></div>' : '';
  return '<div class="flow-card">' + head + ba + sample + noData +
    recheck + '</div>';
}

function flowResultHtml(flow, uid, slug) {
  return flowFamilies(flow).map(function (fam) {
    return flowFamilyHtml(fam, uid, slug);
  }).join('');
}

/* Wire every Re-check-flow button under root (re-binds itself after a
   card replaces its own node). */
function bindFlowRecheck(root) {
  $all('button[data-flowrecheck]', root).forEach(function (b) {
    b.onclick = async function () {
      var uid = b.getAttribute('data-flowrecheck');
      var slug = b.getAttribute('data-slug');
      var card = b.closest('.flow-card');
      busy(b, true);
      try {
        var r = await api('/api/datasource/' +
          encodeURIComponent(uid) + '/verify-flow', { slug: slug });
        var fams = flowFamilies(r.flow);
        if (fams[0]) App.dsFlow[uid] = fams[0];
        if (card && fams[0]) {
          card.outerHTML = flowFamilyHtml(fams[0], uid, slug);
          bindFlowRecheck(root); mountCharts(root);
        }
        toast('Flow re-checked', 'ok');
      } catch (e) { toast(e.message, 'err'); busy(b, false); }
    };
  });
}

/* =============================================== compare view (3b) */
var CMP_RANGES = [['now-15m', '15m'], ['now-1h', '1h'],
                  ['now-6h', '6h'], ['now-24h', '24h']];
var VERDICT_SORT = ['gf-error', 'nr-error', 'shape-mismatch',
  'value-mismatch', 'gf-empty', 'nr-empty', 'both-empty',
  'close', 'match'];

/* Friendly agreement badge for a compare panel pair. */
function cmpAgreeBadge(p) {
  var v = p.verdict || '';
  var lbl = VERDICT_LBL[v] || v || 'unknown';
  if ((v === 'close' || v === 'value-mismatch' ||
       v === 'shape-mismatch') && p.ratio != null &&
      isFinite(p.ratio)) {
    lbl += ' x' + fmtRatio(p.ratio);
  }
  return chip(lbl, VERDICT_CLS[v] || 'dim',
              p.detail || VERDICT_HELP[v] || '');
}

async function vCompare(view, slug) {
  crumb('Compare');
  try {
    if (!App.dashboards.length) {
      App.dashboards = (await api('/api/dashboards')).dashboards || [];
    }
  } catch (e) { /* keep going with whatever we have */ }
  if (!App.dashboards.length) {
    view.innerHTML = '<h1>Compare</h1><div class="empty">' +
      '<span class="eico">' + ico('inbox', 26) + '</span><b>' +
      'Nothing to compare yet.</b><br>Convert a dashboard first, ' +
      'then come back to see New Relic and Grafana side by side.' +
      '<div style="margin-top:12px"><a class="btn primary" ' +
      'href="#/convert">Fetch &amp; Convert ' + ico('arrow', 13) +
      '</a></div></div>';
    return;
  }
  if (!slug) slug = (App.cmp && App.cmp.slug) ||
    App.dashboards[0].slug;
  if (!App.dashboards.some(function (d) { return d.slug === slug; })) {
    slug = App.dashboards[0].slug;
  }
  if (!App.cmp || App.cmp.slug !== slug) {
    App.cmp = { slug: slug, from: 'now-1h', to: 'now', report: null,
                syncHover: true, onlyDisagree: false, custom: false,
                _panels: {} };
  }
  var opts = App.dashboards.map(function (d) {
    return '<option value="' + esc(d.slug) + '"' +
      (d.slug === slug ? ' selected' : '') + '>' +
      esc(d.title || d.slug) + '</option>';
  }).join('');
  view.innerHTML =
    '<h1>Compare &mdash; New Relic vs Grafana</h1>' +
    '<p class="lead">The whole dashboard rendered on both sides ' +
    'with real data, panel by panel, so you can see at a glance ' +
    'where the migration matches and where it needs a fix.</p>' +
    '<div class="card" style="padding:12px 16px">' +
    '<div class="row"><label style="margin:0">Dashboard</label>' +
    '<select id="cmp-dash" style="min-width:220px">' + opts +
    '</select>' +
    '<button class="btn primary" id="cmp-run">' + ico('play', 14) +
    'Run comparison</button>' +
    '<span id="cmp-status" class="kv" aria-live="polite"></span>' +
    '</div></div>' +
    '<div id="cmp-top"></div><div id="cmp-hint"></div>' +
    '<div id="cmp-pairs"></div>';
  $('#cmp-dash').onchange = function () {
    location.hash = '#/compare/' + encodeURIComponent(this.value);
  };
  $('#cmp-run').onclick = function () { cmpRun(); };
  if (App.cmp.report) cmpRenderReport();
  else cmpRun();
}

async function cmpRun() {
  if (!App.cmp) return;
  var slug = App.cmp.slug;
  var status = $('#cmp-status');
  cmpRenderTop(App.cmp.report);
  var pairs = $('#cmp-pairs');
  if (pairs) pairs.innerHTML = cmpSkeletons(6);
  if (status) status.textContent = 'Comparing over ' + App.cmp.from +
    ' …';
  try {
    var job = await startJob('compare: ' + slug, '/api/compare',
      { slug: slug, from: App.cmp.from, to: App.cmp.to }, null);
    if (!App.cmp || App.cmp.slug !== slug) return;  /* navigated away */
    App.cmp.report = job.result || {};
    var st = $('#cmp-status'); if (st) st.textContent = '';
    if ($('#cmp-pairs')) cmpRenderReport();
  } catch (e) {
    var p2 = $('#cmp-pairs');
    if (p2) {
      p2.innerHTML = errorCard('The comparison could not be built.',
        e.message, { label: 'Check the connections',
                     href: '#/connect' });
    }
    var st2 = $('#cmp-status'); if (st2) st2.textContent = '';
  }
}

function cmpRenderReport() {
  cmpRenderTop(App.cmp.report);
  cmpRenderHint(App.cmp.report || {});
  cmpRenderPairs(App.cmp.report || {});
}

/* When Grafana panels fail because no datasource resolves, the fix is
   not per-panel -- it is "add the datasource". Surface one actionable
   banner above the pairs so a first-run user is not left staring at a
   wall of red errors with no next step. */
function cmpRenderHint(rep) {
  var host = $('#cmp-hint'); if (!host) return;
  var panels = rep.panels || [];
  var dsErrs = panels.filter(function (p) {
    if (p.verdict !== 'gf-error') return false;
    var d = String(p.detail || '').toLowerCase();
    return d.indexOf('datasource') !== -1 || d.indexOf('no matching') !== -1;
  });
  if (!dsErrs.length) { host.innerHTML = ''; return; }
  var n = dsErrs.length;
  host.innerHTML = errorCard(
    n + (n === 1 ? ' panel' : ' panels') + " can't render on the " +
    'Grafana side because no matching datasource is configured yet.',
    'Add the datasource these panels need, then re-run the ' +
    'comparison to watch them light up next to New Relic.',
    { label: 'Add a datasource', href: '#/datasources' },
    'warn');
}

function cmpRenderTop(rep) {
  var top = $('#cmp-top'); if (!top) return;
  var score = rep ? rep.score : null;
  var grade = score == null ? '' : score >= 90 ? 'ready' :
    score >= 60 ? 'almost' : 'blocked';
  var sum = (rep && rep.summary) || {};
  var tally = VERDICT_SORT.filter(function (k) {
    return sum[k];
  }).map(function (k) {
    return chip(sum[k] + ' ' + (VERDICT_LBL[k] || k),
                VERDICT_CLS[k] || 'dim', VERDICT_HELP[k] || '');
  }).join('');
  var ranges = CMP_RANGES.map(function (r) {
    var on = !App.cmp.custom && App.cmp.from === r[0];
    return '<button class="btn small' + (on ? ' primary' : '') +
      '" data-cmprange="' + r[0] + '">' + r[1] + '</button>';
  }).join('') + '<button class="btn small' +
    (App.cmp.custom ? ' primary' : '') +
    '" data-cmprange="custom">custom</button>';
  var custom = App.cmp.custom ? '<span class="cmp-custom">' +
    '<input id="cmp-from" value="' + esc(App.cmp.from) +
    '" style="max-width:96px" aria-label="from">' +
    '<span class="kv">to</span><input id="cmp-to" value="' +
    esc(App.cmp.to) + '" style="max-width:80px" aria-label="to">' +
    '<button class="btn small primary" id="cmp-apply">Apply' +
    '</button></span>' : '';
  top.innerHTML = '<div class="card"><div class="cmp-topbar">' +
    '<div class="cmp-score">' + ring(score, grade, 56) +
    '<div class="cmp-score-lbl">overall<br>agreement</div></div>' +
    '<div class="cmp-tally">' + (tally ||
      '<span class="kv">run the comparison to see results</span>') +
    '</div><div class="cmp-controls">' +
    '<div class="seg-range" role="group" aria-label="Time range">' +
    ranges + '</div>' + custom +
    '<label class="cmp-switch" title="Hovering a point on one side ' +
    'marks the same moment on the other"><input type="checkbox" ' +
    'id="cmp-sync"' + (App.cmp.syncHover ? ' checked' : '') +
    '>Sync hover</label>' +
    '<label class="cmp-switch" title="Hide panels that already ' +
    'match"><input type="checkbox" id="cmp-only"' +
    (App.cmp.onlyDisagree ? ' checked' : '') +
    '>Only disagreements</label></div></div></div>';
  $all('[data-cmprange]', top).forEach(function (b) {
    b.onclick = function () {
      var v = b.getAttribute('data-cmprange');
      if (v === 'custom') {
        App.cmp.custom = true; cmpRenderTop(App.cmp.report);
      } else {
        App.cmp.custom = false; App.cmp.from = v; App.cmp.to = 'now';
        cmpRun();
      }
    };
  });
  var ap = $('#cmp-apply', top);
  if (ap) {
    ap.onclick = function () {
      App.cmp.from = ($('#cmp-from').value || 'now-1h').trim();
      App.cmp.to = ($('#cmp-to').value || 'now').trim();
      cmpRun();
    };
  }
  var sy = $('#cmp-sync', top);
  if (sy) sy.onchange = function () { App.cmp.syncHover = sy.checked; };
  var on = $('#cmp-only', top);
  if (on) {
    on.onchange = function () {
      App.cmp.onlyDisagree = on.checked;
      cmpRenderPairs(App.cmp.report || {});
    };
  }
}

function cmpSideSkeleton(lbl) {
  return '<div class="cmp-side"><div class="cmp-side-h">' +
    esc(lbl) + '</div><div class="skel skel-chart"></div></div>';
}

function cmpSkeletons(n) {
  var one = '<div class="cmp-pair"><div class="cmp-pair-head">' +
    '<span class="skel" style="height:15px;width:190px;' +
    'display:inline-block"></span></div><div class="cmp-cols">' +
    cmpSideSkeleton('New Relic') + cmpSideSkeleton('Grafana') +
    '</div></div>';
  var out = '';
  for (var i = 0; i < n; i++) out += one;
  return out;
}

function cmpSide(p, side, label) {
  var data = p[side] || {};
  return '<div class="cmp-side"><div class="cmp-side-h">' +
    ico(side === 'grafana' ? 'database' : 'zap', 12) + esc(label) +
    '<button class="btn small ghost iconbtn cmp-refresh" ' +
    'data-cmprefresh="' + esc(p.panel_id) + '" data-side="' + side +
    '" title="Refresh this side" aria-label="Refresh ' + esc(label) +
    '">' + ico('refresh', 12) + '</button></div>' +
    chart(p.viz || 'timeseries', data, { height: 130 }) + '</div>';
}

function cmpPairHtml(p) {
  var pid = p.panel_id;
  var why = p.detail || VERDICT_HELP[p.verdict] || '';
  return '<div class="cmp-pair" data-cmppid="' + esc(pid) + '">' +
    '<div class="cmp-pair-head">' +
    '<span class="cmp-pair-title">' +
    esc(p.title || ('panel ' + pid)) + '</span>' +
    '<span class="cmp-agree">' + cmpAgreeBadge(p) + '</span>' +
    '<span class="cmp-why" title="' + esc(why) + '">' + esc(why) +
    '</span>' +
    '<button class="btn small ghost cmp-open" data-cmpopen="' +
    esc(pid) + '">Open panel ' + ico('arrow', 12) + '</button>' +
    '</div><div class="cmp-cols" data-cmpbody="' + esc(pid) + '">' +
    cmpSideSkeleton('New Relic') + cmpSideSkeleton('Grafana') +
    '</div></div>';
}

/* Fill one pair's charts (lazy, on scroll) and link the two
   timeseries as sync-hover partners. */
function cmpFillPair(pairEl) {
  if (!pairEl || pairEl.getAttribute('data-filled')) return;
  var pid = pairEl.getAttribute('data-cmppid');
  var p = (App.cmp._panels || {})[pid];
  if (!p) return;
  var body = $('[data-cmpbody]', pairEl);
  if (body) {
    body.innerHTML = cmpSide(p, 'nr', 'New Relic') +
      cmpSide(p, 'grafana', 'Grafana');
    var charts = $all('.chart[data-tsid]', body);
    if (charts.length === 2) {
      var a = charts[0].getAttribute('data-tsid');
      var b = charts[1].getAttribute('data-tsid');
      if (CHARTS[a]) CHARTS[a].partner = b;
      if (CHARTS[b]) CHARTS[b].partner = a;
    }
    mountCharts(body);
  }
  pairEl.setAttribute('data-filled', '1');
}

function cmpMountLazy() {
  if (App.cmpIO) { App.cmpIO.disconnect(); App.cmpIO = null; }
  var host = $('#cmp-pairs'); if (!host) return;
  var targets = $all('.cmp-pair', host);
  if (!('IntersectionObserver' in window)) {
    targets.forEach(cmpFillPair); return;
  }
  App.cmpIO = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (en.isIntersecting) {
        cmpFillPair(en.target);
        App.cmpIO.unobserve(en.target);
      }
    });
  }, { root: null, rootMargin: '250px 0px' });
  targets.forEach(function (t) { App.cmpIO.observe(t); });
}

function cmpPairByPid(pid) {
  var host = $('#cmp-pairs'), found = null;
  if (!host) return null;
  $all('.cmp-pair', host).forEach(function (el) {
    if (el.getAttribute('data-cmppid') === String(pid)) found = el;
  });
  return found;
}

function cmpOpenPanel(pid) {
  var slug = App.cmp.slug;
  App.expanded[slug + ':' + pid] = true;
  location.hash = '#/dash/' + encodeURIComponent(slug);
}

async function cmpRefreshSide(btn) {
  var pid = btn.getAttribute('data-cmprefresh');
  var side = btn.getAttribute('data-side');
  busy(btn, true);
  try {
    var r = await api('/api/panel-data?slug=' +
      encodeURIComponent(App.cmp.slug) + '&panel_id=' +
      encodeURIComponent(pid) + '&side=' + encodeURIComponent(side) +
      '&from=' + encodeURIComponent(App.cmp.from) + '&to=' +
      encodeURIComponent(App.cmp.to));
    var p = (App.cmp._panels || {})[pid];
    if (p) p[side] = r.data;
    var pairEl = cmpPairByPid(pid);
    if (pairEl) { pairEl.removeAttribute('data-filled');
                  cmpFillPair(pairEl); }
    toast('Refreshed the ' + (side === 'nr' ? 'New Relic' :
          'Grafana') + ' side', 'ok');
  } catch (e) { toast(e.message, 'err'); }
  busy(btn, false);
}

function cmpRenderPairs(rep) {
  var host = $('#cmp-pairs'); if (!host) return;
  var panels = (rep.panels || []).slice();
  App.cmp._panels = {};
  panels.forEach(function (p) { App.cmp._panels[p.panel_id] = p; });
  panels.sort(function (a, b) {
    var ga = a.grid || {}, gb = b.grid || {};
    return (ga.y || 0) - (gb.y || 0) || (ga.x || 0) - (gb.x || 0);
  });
  if (!panels.length) {
    host.innerHTML = '<div class="empty"><span class="eico">' +
      ico('inbox', 26) + '</span>No comparable panels in this ' +
      'dashboard.</div>';
    return;
  }
  var shown = App.cmp.onlyDisagree ? panels.filter(function (p) {
    return !AGREE_OK[p.verdict];
  }) : panels;
  if (!shown.length) {
    host.innerHTML = '<div class="empty"><span class="eico">' +
      ico('checkcircle', 26) + '</span><b>Every panel agrees.</b>' +
      '<br>Nothing to review — turn off &ldquo;only ' +
      'disagreements&rdquo; to see them all.</div>';
    return;
  }
  var groups = [], seen = {};
  shown.forEach(function (p) {
    var r = p.row || '';
    if (!seen[r]) { seen[r] = { row: r, items: [] };
                    groups.push(seen[r]); }
    seen[r].items.push(p);
  });
  host.innerHTML = groups.map(function (g) {
    return (g.row ? '<div class="cmp-rowhead">' + esc(g.row) +
      '</div>' : '') + g.items.map(cmpPairHtml).join('');
  }).join('');
  $all('[data-cmpopen]', host).forEach(function (b) {
    b.onclick = function () {
      cmpOpenPanel(b.getAttribute('data-cmpopen'));
    };
  });
  $all('[data-cmprefresh]', host).forEach(function (b) {
    b.onclick = function () { cmpRefreshSide(b); };
  });
  cmpMountLazy();
}

/* ============================================= welcome path (3d) */
var WELCOME_STEPS = [
  ['connect', 'Connect New Relic & Grafana', '#/connect'],
  ['convert', 'Fetch & convert your dashboards', '#/convert'],
  ['datasources', 'Add the datasources they need', '#/datasources'],
  ['compare', 'Compare New Relic vs Grafana', '#/compare'],
  ['fix', 'Fix any panels with no data', '#/overview'],
  ['download', 'Import & download', '#/import']];

function welcomeState() {
  var s = App.state || {}, st = s.status || {}, db = s.db || {};
  return { connect: st.grafana === 'ok',
           convert: (db.dashboards || 0) > 0 };
}

function welcomeHtml() {
  if (localStorage.getItem('nr2g-welcome') === 'dismissed') return '';
  var done = welcomeState();
  var firstNext = '';
  WELCOME_STEPS.forEach(function (s) {
    if (!firstNext && !done[s[0]]) firstNext = s[0];
  });
  var items = WELCOME_STEPS.map(function (s, i) {
    var isDone = !!done[s[0]];
    var isNext = s[0] === firstNext;
    return '<a class="wc-item' + (isDone ? ' done' : '') +
      (isNext ? ' next' : '') + '" href="' + s[2] + '">' +
      '<span class="wc-mark">' + (isDone ? '&#10003;' :
        String(i + 1)) + '</span>' + esc(s[1]) +
      (isNext ? '<span class="wc-next">start here ' +
        ico('arrow', 12) + '</span>' : '') + '</a>';
  }).join('');
  return '<div class="card welcome"><div class="welcome-head">' +
    '<b>' + ico('sparkle', 15) + ' Welcome — migrate a ' +
    'dashboard in a few guided steps</b>' +
    '<button class="btn small ghost" data-wcdismiss="1">Dismiss' +
    '</button></div><div class="wc-list">' + items + '</div></div>';
}

/* ============================================== help / shortcuts */
var GKEYS = { o: '#/overview', c: '#/connect', f: '#/convert',
  d: '#/datasources', m: '#/compare', i: '#/import',
  h: '#/changes', a: '#/ai', e: '#/cost', s: '#/stack' };
var HELP_KEYS = [
  ['g then o', 'Overview'], ['g then c', 'Connect'],
  ['g then f', 'Fetch & Convert'], ['g then d', 'Datasources'],
  ['g then m', 'Compare'], ['g then i', 'Import'],
  ['g then h', 'Changes (history)'], ['g then a', 'AI Assistant'],
  ['g then e', 'Cost & efficiency'],
  ['g then s', 'Stack deep-dive'],
  ['?', 'Show this help'], ['j', 'Toggle background jobs'],
  ['t', 'Cycle theme'], ['Esc', 'Close dialogs / drawers']];

function openHelp() {
  var rows = HELP_KEYS.map(function (k) {
    return '<tr><td><kbd>' + esc(k[0]) + '</kbd></td><td>' +
      esc(k[1]) + '</td></tr>';
  }).join('');
  $('#modal-slot').innerHTML = '<div class="modal-wrap">' +
    '<div class="modal help-modal"><h2>Keyboard shortcuts</h2>' +
    '<table class="help-tbl"><tbody>' + rows + '</tbody></table>' +
    '<div class="btnbar" style="justify-content:flex-end">' +
    '<button class="btn primary" id="help-close">Got it</button>' +
    '</div></div></div>';
  $('#help-close').onclick = closeModal;
  $('.modal-wrap', $('#modal-slot')).onclick = function (ev) {
    if (ev.target === this) closeModal();
  };
}

/* ================================================ cost (1.5) */
/* The Cost & efficiency view. Samples real datasource traffic (a
   job), cross-references it against what the migrated dashboards
   need, and shows an estimated monthly-cost breakdown plus ranked,
   paste-ready recommendations. Nothing a dashboard uses is ever
   proposed for removal; used-but-costly dimensions are "review". */

/* Plain-language definitions surfaced as hover tooltips. */
var COST_TERMS = {
  cardinality: 'the number of distinct values a label has. High ' +
    'cardinality (many unique values, like ids or pod names) is the ' +
    '#1 driver of Loki and Mimir cost.',
  streams: 'a stream is one unique combination of Loki label ' +
    'values. More streams means a bigger index and higher cost.',
  'active-series': 'one active series is a unique metric + ' +
    'label-value combination stored by Mimir/Prometheus. Cost ' +
    'scales with the number of active series.',
  'bytes-per-day': 'estimated log volume ingested per day, ' +
    'projected from the sampled window.',
  histogram: 'histogram metrics (_bucket) multiply series by every ' +
    '`le` bucket, so they can dominate active-series cost.'
};
var TARGET_LBL = { promtail: 'Promtail', alloy: 'Grafana Alloy',
  'otel-collector': 'OTel Collector', otel: 'OTel Collector',
  'loki-limits': 'Loki limits', loki: 'Loki',
  'prometheus-relabel': 'Prometheus relabel',
  prometheus: 'Prometheus', 'mimir-limits': 'Mimir limits',
  mimir: 'Mimir' };
var FAM_LBL = { loki: 'Loki', prometheus: 'Mimir', mimir: 'Mimir',
  tempo: 'Tempo' };
var COST_SEV = { high: 'err', medium: 'warn', low: 'info' };
var PRICING_META = {
  loki_ingest_per_gb: { label: 'Loki ingest ($/GB)',
    help: 'Cost per GB of logs ingested by Loki.', money: true },
  loki_store_per_gb_month: { label: 'Loki storage ($/GB-mo)',
    help: 'Cost per GB of log storage per month.', money: true },
  loki_retention_days: { label: 'Loki retention (days)',
    help: 'How long logs are kept; drives storage cost.',
    money: false },
  mimir_per_1k_series_month: {
    label: 'Mimir ($/1k active series-mo)',
    help: 'Cost per 1,000 active series per month.', money: true },
  mimir_store_per_gb_month: { label: 'Mimir storage ($/GB-mo)',
    help: 'Cost per GB of metric storage per month.', money: true },
  tempo_per_gb: { label: 'Tempo ($/GB)',
    help: 'Cost per GB of trace data.', money: true } };

function num(v) { v = Number(v); return isFinite(v) ? v : 0; }
function fmtMoney(v) {
  if (v == null || !isFinite(v)) return '$0';
  if (Math.abs(v) >= 100000) return '$' + fmtNumP(v);
  var r = Math.round(v * 100) / 100;
  return '$' + r.toLocaleString(undefined,
    { maximumFractionDigits: 2 });
}
function fmtPerDay(v) { return fmtBytes(num(v)) + '/day'; }
function humanize(k) {
  return String(k).replace(/_/g, ' ')
    .replace(/\bgb\b/gi, 'GB').replace(/\bram\b/gi, 'RAM')
    .replace(/\busd\b/gi, 'USD');
}
function costTermRaw(text) {
  return '<span class="term-i" title="' + esc(text) +
    '" tabindex="0" role="img" aria-label="' + esc(text) + '">' +
    ico('info', 13) + '</span>';
}
function costTerm(term) {
  var t = COST_TERMS[term];
  if (!t) return '';
  return costTermRaw(term.replace(/-/g, ' ') + ': ' + t);
}
function labelStr(obj) {
  obj = obj || {};
  return Object.keys(obj).map(function (k) {
    return k + '=' + obj[k];
  }).join(', ');
}

function ensureCost() {
  if (!App.cost) {
    App.cost = { range: { from: 'now-24h', to: 'now' }, slug: '',
      traffic: null, cost: null, optimize: null, savings: null,
      pricing: null, pricingDefaults: null,
      loadingTraffic: false, loadingCost: false,
      trafficErr: null, costErr: null };
  }
  return App.cost;
}

/* Wait on an already-started job id (used if /api/cost is served as
   a job rather than synchronously). Resolves with job.result. */
function pollJob(jid) {
  return new Promise(function (resolve, reject) {
    var t = setInterval(function () {
      api('/api/jobs/' + jid).then(function (job) {
        if (job.status === 'done') {
          clearInterval(t); resolve(job.result);
        } else if (job.status === 'error') {
          clearInterval(t);
          reject(new Error(job.error || 'job failed'));
        }
      }, function (e) { clearInterval(t); reject(e); });
    }, 700);
  });
}

/* ============================================ stack deep-dive (1.6) */
/* Plain-text fetch, mirroring api() but returning the raw body --
   used to Copy the AI-context markdown and pull config text. */
async function apiText(path) {
  var res;
  try { res = await fetch(path); }
  catch (e) {
    throw new Error('Cannot reach the nr2grafana server (' +
                    e.message + '). Is it still running?');
  }
  if (!res.ok) {
    var msg = 'HTTP ' + res.status;
    try { var j = await res.json(); if (j && j.error) msg = j.error; }
    catch (e2) { /* non-JSON error body */ }
    throw new Error(msg);
  }
  return await res.text();
}

/* One-line truncation for compact rows (MCP tool descriptions). */
function truncStr(s, n) {
  s = s == null ? '' : String(s);
  n = n || 120;
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

/* Plain-language glossary for every LGTM / Kubernetes term the Stack
   and AI views surface. Hovering the (i) explains it in one line. */
var STACK_TERMS = {
  'active series': 'one active series is a unique metric + label-' +
    'value combination held in an ingester\'s memory; ingester RAM ' +
    'and Mimir cost both scale with it.',
  'bytes per series': 'measured ingester memory (RSS) divided by ' +
    'active series; multiply by your series ceiling for the real RAM ' +
    'capacity -- the configured max_global_series limit is a guard, ' +
    'not a capacity number.',
  'bin-pack': 'fitting the same set of pods onto the fewest nodes ' +
    'that still respect every safety rule (zone spread, one ingester ' +
    'per node) -- fewer, better-used nodes at the same durability.',
  'zone-aware': 'zone-aware replication places each of the 3 copies ' +
    'of a series in a different failure zone, so losing one node (or ' +
    'zone) drops only one copy and quorum survives. It is what makes ' +
    'packing two ingesters per node safe -- never disable it to save ' +
    'nodes.',
  'churn': 'how fast series are created and retired; high churn (low ' +
    'samples per series) bloats the head block and TSDB index without ' +
    'adding useful data -- usually an unbounded label like a pod id.',
  'right-size': 'set a pod\'s CPU/memory request to its observed peak ' +
    'plus headroom (CPU peak x1.5, memory peak x1.3), never below the ' +
    'peak -- frees requested capacity for packing without starving ' +
    'the workload.',
  'spot': 'spare EC2 capacity at a steep discount that AWS can ' +
    'reclaim with ~2 minutes notice; safe for stateless components, ' +
    'never for stateful ingesters (a reclaim risks ring churn / WAL ' +
    'loss).',
  'on-demand': 'standard, non-reclaimable EC2 capacity; the safe ' +
    'choice for stateful Mimir/Loki/Tempo ingesters.',
  'consolidation': 'Karpenter reclaiming underused or empty nodes and ' +
    're-packing their pods onto fewer nodes -- the main cost lever, ' +
    'gated by disruption budgets so it never evicts more than one ' +
    'ingester at a time.',
  'PDB': 'a PodDisruptionBudget caps how many pods of a group may be ' +
    'down at once; maxUnavailable must be >= 1 on ingesters (0 blocks ' +
    'both node drains and consolidation).',
  'NodePool': 'the Karpenter object that decides what nodes to launch ' +
    '(instance families, capacity type, limits, disruption rules) for ' +
    'a set of pods.',
  'Sigma-limits': 'the sum of every pod\'s memory *limit* on a node ' +
    'divided by the node\'s capacity; above 1.0 a simultaneous burst ' +
    'can OOM the node even though requests fit.',
  'remote_write': 'the Prometheus/agent path that ships samples to ' +
    'Mimir; dropping unused metrics here (write_relabel) cuts ingest ' +
    'cost while keeping full-fidelity data locally for debugging.'
};
function stackTermRaw(text) {
  return '<span class="term-i" title="' + esc(text) +
    '" tabindex="0" role="img" aria-label="' + esc(text) + '">' +
    ico('info', 13) + '</span>';
}
function stackTerm(term) {
  var t = STACK_TERMS[term];
  if (!t) return '';
  return stackTermRaw(term + ': ' + t);
}

/* deepdive/packing area -> one of the 7 contract display groups. */
var AREA_TO_GROUP = {
  capacity: 'capacity',
  cardinality: 'cardinality', churn: 'cardinality',
  efficiency: 'efficiency', loki: 'efficiency', tempo: 'efficiency',
  rightsizing: 'efficiency', 'right-sizing': 'efficiency',
  packing: 'efficiency',
  durability: 'durability',
  cost: 'cost',
  network: 'network',
  karpenter: 'karpenter' };
var GROUP_ORDER = ['capacity', 'cardinality', 'efficiency',
  'durability', 'cost', 'network', 'karpenter', 'other'];
var GROUP_LBL = { capacity: 'Capacity', cardinality: 'Cardinality',
  efficiency: 'Efficiency', durability: 'Durability', cost: 'Cost',
  network: 'Network', karpenter: 'Karpenter', other: 'Other' };
var GROUP_ICO = { capacity: 'database', cardinality: 'search',
  efficiency: 'zap', durability: 'checkcircle', cost: 'wrench',
  network: 'arrow', karpenter: 'database', other: 'info' };
/* deepdive severities are FAIL/WARN/INFO; map to chip + card classes. */
var STACK_SEV_CLS = { FAIL: 'err', WARN: 'warn', INFO: 'info',
  fail: 'err', warn: 'warn', info: 'info', blocker: 'err',
  high: 'err', medium: 'warn', low: 'info' };
var STACK_SEV_CARD = { FAIL: 'high', WARN: 'medium', INFO: 'low',
  fail: 'high', warn: 'medium', info: 'low', blocker: 'high',
  high: 'high', medium: 'medium', low: 'low' };
var STACK_SEV_ORDER = { FAIL: 0, fail: 0, blocker: 0, high: 0,
  WARN: 1, warn: 1, medium: 1, INFO: 2, info: 2, low: 2 };

function ensureStack() {
  if (!App.stack) {
    App.stack = { prom: '', mimir: '', loki: '', kube: false,
      slug: '', deepdive: null, packing: null, loading: false,
      err: null, ran: false };
  }
  return App.stack;
}

async function vStack(view) {
  crumb('Stack deep-dive');
  var s = App.state || await api('/api/state');
  App.state = s;
  var st = ensureStack();
  if (!st.deepdive && !st.ran && !st.loading) {
    try {
      var prev = await api('/api/deepdive' +
        (st.slug ? '?slug=' + encodeURIComponent(st.slug) : ''));
      if (prev && prev.deepdive) {
        st.deepdive = prev.deepdive;
        st.packing = prev.packing || null;
        st.ran = true;
      }
    } catch (e) { /* no prior run: the form is shown */ }
  }
  view.innerHTML =
    '<h1>Stack deep-dive</h1>' +
    '<p class="lead">Read the LGTM stack\'s own metrics ' +
    '(' + stackTermRaw('cortex_*, loki_*, tempo_* and container ' +
      'memory -- the components report their own health.') +
    ' self-metrics) and, optionally, your Kubernetes topology, then ' +
    'get safe ways to reclaim ' + stackTerm('active series') +
    ' capacity, cores and dollars &mdash; every recommendation ' +
    'states its risk and <b>defaults to the option that keeps ' +
    'durability, availability and performance</b>.</p>' +
    '<div class="card">' + stackFormHtml() + '</div>' +
    consoleHtml('stack-console', 'Deep-dive log') +
    '<section id="stack-headline"></section>' +
    '<section id="stack-findings"></section>' +
    '<section id="stack-packing"></section>' +
    '<section id="stack-karpenter"></section>';
  wireStackForm();
  renderStackResults();
}

function stackFieldHtml(id, label, value, placeholder) {
  return '<div class="fld"><label>' + esc(label) + '</label>' +
    '<input id="' + id + '" value="' + esc(value || '') +
    '" placeholder="' + esc(placeholder) + '" autocomplete="off" ' +
    'spellcheck="false"></div>';
}

function stackFormHtml() {
  var st = App.stack;
  return '<div class="stack-fields">' +
    stackFieldHtml('sd-prom', 'Prometheus URL', st.prom,
      'http://localhost:9090') +
    stackFieldHtml('sd-mimir', 'Mimir URL', st.mimir,
      'http://localhost:8080/prometheus') +
    stackFieldHtml('sd-loki', 'Loki URL', st.loki,
      'http://localhost:3100') +
    '</div>' +
    '<label class="stack-toggle"><input type="checkbox" id="sd-kube"' +
    (st.kube ? ' checked' : '') + '> Include Kubernetes ' +
    stackTerm('bin-pack') +
    '<span class="field-help">Needs <span class="mono">kubectl</span>' +
    ' on PATH; adds node topology, ' + stackTerm('right-size') +
    ' right-sizing, packing and Karpenter. The metric deep-dive runs ' +
    'with or without a cluster.</span></label>' +
    '<div class="btnbar" style="margin-top:var(--s3)">' +
    '<button class="btn primary" id="sd-run" type="button">' +
    ico('zap', 14) + ' Run deep-dive</button>' +
    '<span class="kv">Leave the URLs blank to read the metrics ' +
    'through your configured Grafana datasources.</span></div>';
}

function wireStackForm() {
  var st = App.stack;
  var p = $('#sd-prom'), m = $('#sd-mimir'), l = $('#sd-loki');
  if (p) p.onchange = function () { st.prom = p.value.trim(); };
  if (m) m.onchange = function () { st.mimir = m.value.trim(); };
  if (l) l.onchange = function () { st.loki = l.value.trim(); };
  var k = $('#sd-kube');
  if (k) k.onchange = function () { st.kube = k.checked; };
  var run = $('#sd-run');
  if (run) run.onclick = function () { onRunDeepdive(run); };
}

async function onRunDeepdive(btn) {
  var st = App.stack;
  var p = $('#sd-prom'), m = $('#sd-mimir'), l = $('#sd-loki'),
      k = $('#sd-kube');
  if (p) st.prom = p.value.trim();
  if (m) st.mimir = m.value.trim();
  if (l) st.loki = l.value.trim();
  if (k) st.kube = k.checked;
  st.loading = true; st.err = null;
  busy(btn, true); renderStackResults();
  var body = { kube: !!st.kube };
  if (st.prom) body.prom = st.prom;
  if (st.mimir) body.mimir = st.mimir;
  if (st.loki) body.loki = st.loki;
  if (st.slug) body.slug = st.slug;
  try {
    var job = await startJob('deepdive', '/api/deepdive', body,
      logInto($('#stack-console')));
    var r = (job && job.result) || {};
    st.deepdive = r.deepdive || null;
    st.packing = r.packing || null;
    st.ran = true;
    toast('Deep-dive complete', 'ok');
  } catch (e) { st.err = e.message; toast(e.message, 'err'); }
  st.loading = false; busy(btn, false);
  renderStackResults();
  refreshState();
}

function renderStackResults() {
  renderStackHeadline();
  renderStackFindings();
  renderStackPacking();
  renderStackKarpenter();
}

/* ---- risk / evidence / savings chips (shared by findings) ---- */
function riskChips(f) {
  f = f || {};
  var dims = [['keeps_performance', 'performance'],
              ['keeps_durability', 'durability'],
              ['keeps_availability', 'availability']];
  var out = '';
  dims.forEach(function (d) {
    var v = f[d[0]];
    if (v === true) {
      out += chip('keeps ' + d[1], 'ok',
        'Safe: this recommendation does not reduce ' + d[1] + '.');
    } else if (v === false) {
      out += chip('may reduce ' + d[1] + ' — review', 'warn',
        'CAUTION: this could reduce ' + d[1] + '. Read the rationale ' +
        'and prefer the safe option; nothing is applied ' +
        'automatically.');
    }
  });
  if (f.keeps_intact === true && !out) {
    out += chip('safe', 'ok', 'Keeps durability, availability and ' +
      'performance intact.');
  }
  var caveat = f.caveat || f.risk_note || f.warning;
  if (caveat) out += chip('caveat', 'warn', String(caveat));
  return out;
}

function evidenceChips(ev) {
  ev = ev || {};
  return Object.keys(ev).map(function (k) {
    var v = ev[k];
    if (v == null || typeof v === 'object') return '';
    var disp = typeof v === 'number' ? fmtNumP(v) :
      (v === true ? 'yes' : v === false ? 'no' : String(v));
    return chip(humanize(k) + ': ' + disp, 'dim');
  }).join('');
}

function stackSaveHtml(es) {
  es = es || {};
  var bits = '';
  if (es.monthly_usd != null) {
    bits += '<span class="save-money">~' +
      esc(fmtMoney(num(es.monthly_usd))) + ' / mo</span>';
  }
  if (es.series != null) {
    bits += chip('-' + fmtNumP(num(es.series)) + ' series', 'info');
  }
  if (es.streams != null) {
    bits += chip('-' + fmtNumP(num(es.streams)) + ' streams', 'info');
  }
  if (es.bytes_per_day != null) {
    bits += chip('-' + fmtBytes(num(es.bytes_per_day)) + '/day',
      'info');
  }
  var comp = es.compute;
  if (comp && typeof comp === 'object') {
    if (comp.cores != null) {
      bits += chip('-' + fmtNumP(num(comp.cores)) + ' cores', 'info');
    }
    if (comp.gib != null || comp.mem_gib != null) {
      bits += chip('-' + fmtNumP(num(comp.gib != null ? comp.gib :
        comp.mem_gib)) + ' GiB', 'info');
    }
    if (comp.nodes != null) {
      bits += chip('-' + fmtNumP(num(comp.nodes)) + ' nodes', 'info');
    }
  } else if (comp != null) {
    bits += chip('-' + fmtNumP(num(comp)) + ' cores', 'info');
  }
  if (es.cores != null) {
    bits += chip('-' + fmtNumP(num(es.cores)) + ' cores', 'info');
  }
  if (!bits) return '';
  return '<span class="kv">estimated savings</span>' + bits;
}

function sevLabel(sev) {
  var s = String(sev || 'INFO');
  return s.length <= 4 ? s.toUpperCase() : s;
}

function stackFindingCard(f) {
  f = f || {};
  var sev = f.severity || 'INFO';
  var cardSev = STACK_SEV_CARD[sev] || 'low';
  var evHtml = evidenceChips(f.evidence);
  var save = stackSaveHtml(f.est_savings);
  var risk = riskChips(f);
  return '<div class="rec-card sev-' + cardSev + '">' +
    '<div class="rec-head"><span class="rec-title">' +
    esc(f.title || f.finding || 'Finding') + '</span>' +
    chip(sevLabel(sev), STACK_SEV_CLS[sev] || 'dim') +
    (f.area ? chip(f.area, 'purple') : '') + '</div>' +
    (f.rationale || f.detail ?
      '<div class="rec-rationale">' + esc(f.rationale || f.detail) +
      '</div>' : '') +
    (evHtml ? '<div class="rec-ev">' + evHtml + '</div>' : '') +
    (save ? '<div class="rec-save">' + save + '</div>' : '') +
    (risk ? '<div class="risk-row">' + risk + '</div>' : '') +
    cfgHtml(f.config || []) + '</div>';
}

/* Every finding from the deep-dive AND the (optional) packing run,
   excluding karpenter-area findings which live in the Karpenter card. */
function allStackFindings(includeKarpenter) {
  var st = App.stack;
  var out = [];
  if (st.deepdive && st.deepdive.findings) {
    out = out.concat(st.deepdive.findings);
  }
  if (st.packing && st.packing.findings) {
    out = out.concat(st.packing.findings);
  }
  if (includeKarpenter) return out;
  return out.filter(function (f) {
    return (f.area || '').toLowerCase() !== 'karpenter';
  });
}

/* ---- headline: $ and cores saveable, safely ---- */
function stackTotals() {
  var st = App.stack;
  var dd = st.deepdive || {}, pk = st.packing || {};
  var usd = 0, cores = 0, nodes = 0;
  allStackFindings(true).forEach(function (f) {
    if (f.keeps_intact === false) return;  /* headline = safe only */
    var es = f.est_savings || {};
    usd += num(es.monthly_usd);
    var c = es.compute;
    if (c && typeof c === 'object') cores += num(c.cores);
    else if (c != null) cores += num(c);
    cores += num(es.cores);
  });
  var pes = pk.est_savings ||
    (pk.packing_sim && pk.packing_sim.est_savings) || {};
  usd += num(pes.monthly_usd);
  cores += num(pes.cores);
  nodes += num(pes.nodes);
  var kes = (pk.karpenter && pk.karpenter.est_savings) || {};
  if (!num(pes.monthly_usd)) usd += num(kes.monthly_usd);
  if (!num(pes.nodes)) nodes += num(kes.nodes);
  var ov = dd.saveable || dd.summary || {};
  if (ov.monthly_usd_saveable != null) usd = num(ov.monthly_usd_saveable);
  if (ov.cores_saveable != null) cores = num(ov.cores_saveable);
  return { usd: usd, cores: cores, nodes: nodes };
}

function renderStackHeadline() {
  var el = $('#stack-headline'); if (!el) return;
  var st = App.stack;
  if (!st.deepdive && !st.packing) {
    if (st.err) { el.innerHTML = errorCard('Deep-dive failed.',
      st.err); return; }
    el.innerHTML = ''; return;
  }
  var t = stackTotals();
  if (t.usd <= 0 && t.cores <= 0 && t.nodes <= 0) {
    el.innerHTML = ''; return;
  }
  var parts = [];
  if (t.usd > 0) {
    parts.push('<span class="sh-money">~' +
      esc(fmtMoney(t.usd)) + ' / mo</span>');
  }
  if (t.cores > 0) {
    if (parts.length) parts.push('<span class="sh-and">and</span>');
    parts.push('<span class="sh-cores">~' + esc(fmtNumP(t.cores)) +
      ' cores</span>');
  }
  if (t.nodes > 0) {
    if (parts.length) parts.push('<span class="sh-and">and</span>');
    parts.push('<span class="sh-cores">~' + esc(fmtNumP(t.nodes)) +
      ' nodes</span>');
  }
  el.innerHTML = '<div class="stack-head">' +
    ico('checkcircle', 22) + '<span class="sh-and">estimated</span>' +
    parts.join(' ') +
    '<span class="sh-and">saveable</span>' +
    '<span class="sh-cap">Without reducing durability, ' +
    'availability or performance. Every figure is an estimate from ' +
    'your metrics and pricing assumptions, not an exact bill; ' +
    'anything that would trade away safety is excluded here and ' +
    'flagged with an amber caveat below.</span></div>';
}

/* ---- findings grouped by area ---- */
function renderStackFindings() {
  var el = $('#stack-findings'); if (!el) return;
  var st = App.stack;
  if (st.loading && !st.deepdive) {
    el.innerHTML = '<h2>' + ico('search', 15) + ' Findings</h2>' +
      '<div class="card"><div class="skel" ' +
      'style="height:80px"></div></div>';
    return;
  }
  if (!st.deepdive && !st.packing) { el.innerHTML = ''; return; }
  var findings = allStackFindings(false);
  var head = '<h2>' + ico('search', 15) + ' Findings</h2>';
  if (!findings.length) {
    el.innerHTML = head + '<div class="empty"><span class="eico">' +
      ico('check', 26) + '</span><b>No issues found.</b><br>' +
      'The metrics the deep-dive read look healthy, or there is not ' +
      'enough signal to recommend a change. Nice and lean.</div>';
    return;
  }
  var buckets = {};
  findings.forEach(function (f) {
    var g = AREA_TO_GROUP[(f.area || '').toLowerCase()] || 'other';
    (buckets[g] = buckets[g] || []).push(f);
  });
  var html = head;
  GROUP_ORDER.forEach(function (g) {
    var list = buckets[g];
    if (!list || !list.length) return;
    list.sort(function (a, b) {
      var d = (STACK_SEV_ORDER[a.severity] == null ? 3 :
                STACK_SEV_ORDER[a.severity]) -
              (STACK_SEV_ORDER[b.severity] == null ? 3 :
                STACK_SEV_ORDER[b.severity]);
      if (d) return d;
      return num((b.est_savings || {}).monthly_usd) -
             num((a.est_savings || {}).monthly_usd);
    });
    html += '<div class="finding-group"><div class="stack-group">' +
      ico(GROUP_ICO[g] || 'zap', 13) + ' ' + esc(GROUP_LBL[g]) +
      '<span class="gc">' + list.length + '</span></div>' +
      list.map(stackFindingCard).join('') + '</div>';
  });
  el.innerHTML = html;
}

/* ---- packing: candidate-shapes table ---- */
function pctStr(v) {
  if (v == null || !isFinite(Number(v))) return '–';
  var n = Number(v);
  if (n <= 1.5 && n >= -1.5) n = n * 100;  /* fraction -> percent */
  return Math.round(n) + '%';
}

function packTableHtml(pk) {
  var sim = pk.packing_sim || pk.packing || pk || {};
  var cands = sim.candidates || sim.shapes || [];
  if (!cands.length) return '';
  var rows = cands.map(function (c, i) {
    var floor = c.floor === true ||
      (i === 0 && c.floor !== false);
    var shape = c.shape || c.instance_type || c.name || '?';
    var nodes = c.nodes != null ? c.nodes : c.node_count;
    var cost = c.cost_mo != null ? c.cost_mo :
      (c.monthly_usd != null ? c.monthly_usd : c['$/mo']);
    var memu = c.avg_mem_util != null ? c.avg_mem_util :
      (c.mem_util != null ? c.mem_util : c.memory_util);
    var lim = c.max_mem_limit_over_alloc != null ?
      c.max_mem_limit_over_alloc :
      (c.sigma_limits_over_capacity != null ?
        c.sigma_limits_over_capacity :
        c.limits_over_capacity);
    var limCell = lim == null ? '&ndash;' :
      esc((Math.round(num(lim) * 100) / 100).toFixed(2)) +
      (num(lim) > 1 ? ' <span title="a simultaneous burst can OOM ' +
        'this node" style="color:var(--amber)">⚠</span>' : '');
    return '<tr' + (floor ? ' class="pack-floor"' : '') + '>' +
      '<td class="mono">' + esc(String(shape)) + '</td>' +
      '<td class="num">' + esc(fmtNumP(num(nodes))) + '</td>' +
      '<td class="num">' + esc(fmtMoney(num(cost))) + '</td>' +
      '<td class="num">' + esc(pctStr(memu)) + '</td>' +
      '<td class="num">' + limCell + '</td></tr>';
  }).join('');
  return '<div class="pack-tbl"><table><thead><tr>' +
    '<th>Candidate shape</th><th>Nodes</th><th>$/mo</th>' +
    '<th>Mem util</th><th>' + stackTermRaw(STACK_TERMS['Sigma-limits']) +
    ' &Sigma;limits/capacity</th></tr></thead><tbody>' + rows +
    '</tbody></table></div>';
}

function renderStackPacking() {
  var el = $('#stack-packing'); if (!el) return;
  var st = App.stack;
  var pk = st.packing;
  if (!pk) { el.innerHTML = ''; return; }
  var head = '<h2>' + ico('database', 15) +
    ' Node topology &amp; ' + stackTerm('bin-pack') +
    ' bin-pack</h2>';
  if (pk.available === false) {
    el.innerHTML = head + '<div class="empty"><span class="eico">' +
      ico('database', 26) + '</span><b>Kubernetes analysis skipped.' +
      '</b><br>' + esc(pk.note || 'kubectl is not available on ' +
      'PATH.') + '<br>The metric-driven deep-dive above is ' +
      'unaffected.</div>';
    return;
  }
  var sim = pk.packing_sim || pk.packing || pk || {};
  var tbl = packTableHtml(pk);
  var curNote = '';
  var cur = sim.current_cost != null ? sim.current_cost :
    sim.current_monthly_usd;
  var curNodes = sim.current_nodes;
  if (cur != null || curNodes != null) {
    var b = [];
    if (curNodes != null) b.push(esc(fmtNumP(num(curNodes))) +
      ' node(s)');
    if (cur != null) b.push(esc(fmtMoney(num(cur))) + ' / mo');
    curNote = '<p class="pack-note">Current pool: ' +
      b.join(' &middot; ') + '. The highlighted row is the ' +
      'bin-pack <b>floor</b> &mdash; the cheapest shape that still ' +
      'holds every pod under the zone and one-ingester-per-node ' +
      'rules.</p>';
  }
  var intro = '<p class="kv">The same pods re-packed onto each ' +
    'candidate instance shape, with daemon overhead subtracted and ' +
    'the ' + stackTerm('zone-aware') + ' zone / anti-affinity rules ' +
    'honored. A memory-optimized (r-class) shape is usually the ' +
    'honest floor for ingesters; a &Sigma;limits/capacity above 1.0 ' +
    'means a burst could OOM the node, so it is not safe even if it ' +
    'is cheaper.</p>';
  if (!tbl) {
    el.innerHTML = head + curNote +
      '<div class="empty"><span class="eico">' + ico('database', 26) +
      '</span><b>No packing simulation returned.</b><br>' +
      'The cluster topology did not yield candidate shapes to ' +
      'compare.</div>';
    return;
  }
  el.innerHTML = head + '<div class="card">' + intro + curNote +
    tbl + '</div>';
}

/* ---- Karpenter card ---- */
function npSummaryHtml(np) {
  np = np || {};
  var name = np.name || np.metadata_name ||
    (np.metadata && np.metadata.name) || 'nodepool';
  var bits = [];
  var fams = np.instance_families || np.families ||
    np.instance_family;
  if (fams) {
    bits.push('<b>families</b> ' +
      esc(Array.isArray(fams) ? fams.join(', ') : String(fams)));
  }
  if (np.capacity_type || np.capacity_types) {
    var ct = np.capacity_type || np.capacity_types;
    bits.push('<b>capacity</b> ' +
      esc(Array.isArray(ct) ? ct.join(', ') : String(ct)));
  }
  if (np.consolidation_policy || np.consolidationPolicy) {
    bits.push('<b>consolidation</b> ' +
      esc(String(np.consolidation_policy || np.consolidationPolicy)));
  }
  if (np.consolidate_after || np.consolidateAfter) {
    bits.push('<b>consolidateAfter</b> ' +
      esc(String(np.consolidate_after || np.consolidateAfter)));
  }
  if (np.expire_after || np.expireAfter) {
    bits.push('<b>expireAfter</b> ' +
      esc(String(np.expire_after || np.expireAfter)));
  }
  if (np.limits) {
    var lim = np.limits;
    bits.push('<b>limits</b> ' + esc(typeof lim === 'object' ?
      Object.keys(lim).map(function (k) {
        return k + '=' + lim[k]; }).join(' ') : String(lim)));
  }
  if (np.nodes != null || np.node_count != null) {
    bits.push('<b>nodes</b> ' +
      esc(fmtNumP(num(np.nodes != null ? np.nodes :
        np.node_count))));
  }
  var kvHtml = bits.length ?
    '<div class="np-kv">' + bits.join(' &middot; ') + '</div>' :
    '<div class="np-kv">no summary fields reported</div>';
  return '<div class="np-card"><div class="np-name">' + esc(name) +
    '</div>' + kvHtml +
    (np.raw || np.spec ? jsonDetails('nodepool spec',
      np.raw || np.spec) : '') + '</div>';
}

function renderStackKarpenter() {
  var el = $('#stack-karpenter'); if (!el) return;
  var st = App.stack;
  var kp = st.packing && st.packing.karpenter;
  if (!kp) { el.innerHTML = ''; return; }
  var head = '<h2>' + ico('database', 15) + ' ' +
    stackTermRaw(STACK_TERMS['NodePool']) + ' Karpenter</h2>';
  var nps = kp.nodepools || [];
  var npHtml = nps.length ? nps.map(npSummaryHtml).join('') :
    '<p class="pack-note">No current observability NodePool ' +
    'detected.</p>';
  var kfindings = (kp.findings || []).slice().sort(function (a, b) {
    return (STACK_SEV_ORDER[a.severity] == null ? 3 :
             STACK_SEV_ORDER[a.severity]) -
           (STACK_SEV_ORDER[b.severity] == null ? 3 :
             STACK_SEV_ORDER[b.severity]);
  }).map(stackFindingCard).join('');
  var yamlHtml = '';
  var yaml = kp.proposed_nodepool_yaml || kp.proposed_yaml || '';
  if (yaml) {
    var yid = 'npyaml-' + uid();
    yamlHtml = '<div class="rec-cfg stack-yaml"><div class="cfg-bar">' +
      '<label style="margin:0">Proposed optimized NodePool</label>' +
      chip('YAML', 'dim') + '<span style="margin-left:auto"></span>' +
      copyBtn(yid, 'NodePool YAML') + '</div>' +
      '<pre id="' + yid + '">' + esc(yaml) + '</pre>' +
      (kp.proposed_ec2nodeclass_note ? '<div class="cfg-note">' +
        esc(kp.proposed_ec2nodeclass_note) + '</div>' : '') +
      '</div>';
  }
  var kes = kp.est_savings || {};
  var savBits = '';
  if (kes.monthly_usd != null) {
    savBits += '<span class="save-money">~' +
      esc(fmtMoney(num(kes.monthly_usd))) + ' / mo</span>';
  }
  if (kes.nodes != null) {
    savBits += chip('-' + fmtNumP(num(kes.nodes)) + ' nodes', 'info');
  }
  if (kes.keeps_availability === true) {
    savBits += chip('keeps availability', 'ok',
      'On-demand ingesters and disruption budgets that respect PDBs ' +
      '-- consolidation never evicts more than one ingester at once.');
  } else if (kes.keeps_availability === false) {
    savBits += chip('review availability — caution', 'warn',
      'This proposal could affect availability; review before ' +
      'applying.');
  }
  var savLine = savBits ?
    '<div class="rec-save"><span class="kv">estimated savings</span>' +
    savBits + '</div>' : '';
  var intro = '<p class="kv">The observability workloads run on ' +
    'their own Karpenter NodePool, where cost and availability meet. ' +
    'The proposed NodePool keeps stateful ingesters on ' +
    stackTerm('on-demand') + ' on-demand, adds ' +
    stackTerm('consolidation') + ' consolidation with a budget that ' +
    'respects ' + stackTerm('PDB') + ' PDBs, and is generic &mdash; ' +
    'replace the placeholder cluster / AMI / role / subnet values ' +
    'before applying.</p>';
  el.innerHTML = '<section class="card">' + head + intro + npHtml +
    savLine + kfindings + yamlHtml + '</section>';
}

async function vCost(view) {
  crumb('Cost & efficiency');
  ensureCost();
  if (!App.dashboards.length) {
    try {
      App.dashboards = (await api('/api/dashboards')).dashboards || [];
    } catch (e) { /* scope selector just offers whole-instance */ }
  }
  if (!App.cost.pricing) {
    try {
      var pr = await api('/api/pricing');
      App.cost.pricing = (pr && pr.pricing) || pr || null;
      if (App.cost.pricing && !App.cost.pricingDefaults) {
        App.cost.pricingDefaults =
          JSON.parse(JSON.stringify(App.cost.pricing));
      }
    } catch (e) { /* defaults appear once an analysis runs */ }
  }
  view.innerHTML =
    '<h1>Cost &amp; efficiency</h1>' +
    '<p class="lead">Sample what your LGTM datasources actually ' +
    'ingest, compare it against what your migrated dashboards ' +
    'need, and get safe, paste-ready ways to cut spend. Every ' +
    'figure is an <b>estimate based on your pricing inputs</b> ' +
    '&mdash; never an exact bill.</p>' +
    '<div class="card">' + costActionsHtml() + '</div>' +
    consoleHtml('cost-console', 'Sampling log') +
    '<section id="cost-traffic"></section>' +
    '<section id="cost-breakdown"></section>' +
    '<section id="cost-recs"></section>';
  wireCostActions();
  renderTrafficSection();
  renderBreakdownSection();
  renderRecsSection();
}

function costActionsHtml() {
  var c = App.cost;
  var scopeOpts = '<option value="">Whole instance</option>' +
    (App.dashboards || []).map(function (d) {
      var s = d.slug || d.name || '';
      return '<option value="' + esc(s) + '"' +
        (s === c.slug ? ' selected' : '') + '>' + esc(s) +
        '</option>';
    }).join('');
  return '<div class="cost-actions">' +
    '<div class="fld"><label>Sample window from</label>' +
    '<input id="ct-from" value="' + esc(c.range.from) +
    '" autocomplete="off" spellcheck="false"></div>' +
    '<div class="fld"><label>to</label>' +
    '<input id="ct-to" value="' + esc(c.range.to) +
    '" autocomplete="off" spellcheck="false"></div>' +
    '<div class="fld"><label>Scope</label>' +
    '<select id="ct-scope">' + scopeOpts + '</select></div>' +
    '<div class="grow"></div>' +
    '<button class="btn" id="ct-sample" type="button">' +
    ico('database', 14) + ' Sample traffic</button>' +
    '<button class="btn primary" id="ct-analyze" type="button">' +
    ico('zap', 14) + ' Analyze cost &amp; efficiency</button>' +
    '</div>';
}

function wireCostActions() {
  var c = App.cost;
  var f = $('#ct-from'), t = $('#ct-to'), sc = $('#ct-scope');
  if (f) f.onchange = function () { c.range.from = f.value; };
  if (t) t.onchange = function () { c.range.to = t.value; };
  if (sc) sc.onchange = function () { c.slug = sc.value; };
  var sb = $('#ct-sample');
  if (sb) sb.onclick = function () { onSampleTraffic(sb); };
  var ab = $('#ct-analyze');
  if (ab) ab.onclick = function () { onAnalyze(ab); };
}

async function onSampleTraffic(btn) {
  var c = App.cost;
  c.loadingTraffic = true; c.trafficErr = null;
  renderTrafficSection();
  busy(btn, true);
  try {
    var job = await startJob('sample-traffic', '/api/traffic',
      { from: c.range.from, to: c.range.to }, logInto($('#cost-console')));
    c.traffic = (job && job.result) || null;
    toast('Traffic sampled', 'ok');
  } catch (e) {
    c.trafficErr = e.message; toast(e.message, 'err');
  }
  c.loadingTraffic = false; busy(btn, false);
  renderTrafficSection();
}

function applyCostResult(res) {
  res = res || {};
  var c = App.cost;
  c.costErr = null;
  c.cost = res.cost ||
    (res.schema === 'nr2grafana/cost/v1' ? res : c.cost);
  c.optimize = res.optimize ||
    (res.schema === 'nr2grafana/optimize/v1' ? res : c.optimize);
  if (res.traffic) c.traffic = res.traffic;
  if (res.pricing) c.pricing = res.pricing;
  else if (c.cost && c.cost.pricing) c.pricing = c.cost.pricing;
  if (!c.pricingDefaults && c.pricing) {
    c.pricingDefaults = JSON.parse(JSON.stringify(c.pricing));
  }
  c.savings = normalizeSavings(res);
}

async function onAnalyze(btn) {
  var c = App.cost;
  c.loadingCost = true; c.costErr = null;
  renderBreakdownSection(); renderRecsSection();
  busy(btn, true);
  var pricing = gatherPricing() || c.pricing || null;
  var body = {};
  if (c.slug) body.slug = c.slug;
  if (pricing) body.pricing = pricing;
  try {
    var res = await api('/api/cost', body);
    if (res && res.job) res = await pollJob(res.job);
    applyCostResult(res);
    toast('Cost analysis ready', 'ok');
  } catch (e) {
    c.costErr = e.message; toast(e.message, 'err');
  }
  c.loadingCost = false; busy(btn, false);
  renderTrafficSection();
  renderBreakdownSection();
  renderRecsSection();
}

/* Prefer an explicit savings/projection object from the server; fall
   back to the optimize summary, then to summing the recommendations.
   Everything is an estimate derived from the pricing inputs. */
function normalizeSavings(res) {
  var c = App.cost;
  var cost = (res && res.cost) || c.cost || {};
  var current = num(cost.monthly_total);
  var sv = (res && (res.savings || res.projection)) ||
    (cost && cost.savings) || null;
  var saved, projected, pct;
  if (sv && sv.saved_total != null) {
    saved = num(sv.saved_total);
    projected = sv.projected_total != null ?
      num(sv.projected_total) : Math.max(0, current - saved);
    pct = sv.saved_pct != null ? num(sv.saved_pct) :
      (current > 0 ? saved / current * 100 : 0);
  } else {
    var opt = (res && res.optimize) || c.optimize || {};
    var sum = opt.summary || {};
    if (sum.total_est_monthly_usd != null) {
      saved = num(sum.total_est_monthly_usd);
    } else {
      saved = (opt.recommendations || []).reduce(function (a, r) {
        return a + num((r.est_savings || {}).monthly_usd);
      }, 0);
    }
    projected = Math.max(0, current - saved);
    pct = current > 0 ? saved / current * 100 : 0;
  }
  return { current: current, saved: saved, projected: projected,
           pct: pct };
}

/* ---- traffic section ---- */
function renderTrafficSection() {
  var el = $('#cost-traffic'); if (!el) return;
  var c = App.cost;
  var head = '<h2>' + ico('database', 15) + ' Sampled traffic</h2>';
  if (c.loadingTraffic) {
    el.innerHTML = head +
      '<div class="card"><div class="skel skel-chart"></div></div>';
    return;
  }
  var t = c.traffic;
  var dss = (t && t.datasources) || [];
  if (!dss.length) {
    var msg = c.trafficErr ?
      errorCard('Traffic sampling failed.', c.trafficErr) : '';
    el.innerHTML = head + msg +
      '<div class="empty"><span class="eico">' +
      ico('database', 26) + '</span><b>No traffic sampled yet.</b>' +
      '<br>Sample your Loki, Mimir and Tempo datasources to see ' +
      'what they actually ingest &mdash; active series, streams and ' +
      'bytes per day.<div class="btnbar" ' +
      'style="justify-content:center">' +
      '<button class="btn primary" id="ct-empty-sample" ' +
      'type="button">' + ico('database', 14) +
      ' Sample traffic</button></div></div>';
    var b = $('#ct-empty-sample');
    if (b) b.onclick = function () { onSampleTraffic(b); };
    return;
  }
  var rangeNote = t.range ? '<p class="helper">Window: ' +
    esc(t.range.from || '') + ' .. ' + esc(t.range.to || '') +
    (t.generated_at ? ' &middot; sampled ' +
      esc(String(t.generated_at).replace('T', ' ').slice(0, 19)) :
      '') + '</p>' : '';
  el.innerHTML = head + rangeNote + dss.map(dsTcardHtml).join('');
  mountCharts(el);
}

function statCardHtml(v, lbl, term) {
  return '<div class="stat-card"><div class="num">' + v +
    '</div><div class="lbl">' + esc(lbl) +
    (term ? costTerm(term) : '') + '</div></div>';
}

function vizBlock(title, chartHtml, tableHtml, term) {
  return '<div><h3>' + esc(title) + (term ? costTerm(term) : '') +
    '</h3>' + chartHtml + (tableHtml || '') + '</div>';
}

function barChartFromPairs(pairs, nameKey, valKey, unit) {
  pairs = pairs || [];
  if (!pairs.length) return chartEmpty('No data sampled');
  var series = pairs.slice(0, 12).map(function (p) {
    var nm = nameKey === 'name' ? p.name : p[nameKey];
    return { name: String(nm == null ? '' : nm),
             points: [[0, num(p[valKey])]] };
  });
  return chart('bar', { series: series },
    { unit: unit, height: 150 });
}

function rankTableHtml(pairs, nameKey, valKey, unit) {
  pairs = pairs || [];
  if (!pairs.length) return '';
  var rows = pairs.slice(0, 12).map(function (p) {
    var nm = nameKey === 'name' ? p.name : p[nameKey];
    return '<tr><td class="n">' + esc(String(nm == null ? '' : nm)) +
      '</td><td class="v">' + esc(fmtUnit(num(p[valKey]), unit)) +
      '</td></tr>';
  }).join('');
  return '<div class="rank-tbl"><table><tbody>' + rows +
    '</tbody></table></div>';
}

function streamPairs(list) {
  return (list || []).map(function (s) {
    return { name: labelStr(s.labels) || '(stream)',
             bytes: num(s.bytes) };
  });
}

function dsTcardHtml(ds) {
  ds = ds || {};
  var fam = (ds.family || '').toLowerCase();
  var stats = '', viz = '';
  if (fam === 'prometheus' || fam === 'mimir' || ds.prometheus) {
    var p = ds.prometheus || {};
    stats = statCardHtml(fmtNumP(num(p.active_series)),
        'active series', 'active-series') +
      (p.histogram_series != null ? statCardHtml(
        fmtNumP(num(p.histogram_series)), 'histogram series',
        'histogram') : '');
    viz = vizBlock('Top metrics by series',
        barChartFromPairs(p.top_metrics, 'metric', 'series', ''),
        rankTableHtml(p.top_metrics, 'metric', 'series', '')) +
      vizBlock('Label cardinality',
        barChartFromPairs(p.label_cardinality, 'label', 'values', ''),
        rankTableHtml(p.label_cardinality, 'label', 'values', ''),
        'cardinality');
  } else if (fam === 'loki' || ds.loki) {
    var l = ds.loki || {};
    var sp = streamPairs(l.top_streams);
    stats = statCardHtml(fmtNumP(num(l.streams)), 'streams',
        'streams') +
      statCardHtml(fmtPerDay(num(l.bytes_per_day)), 'bytes / day',
        'bytes-per-day');
    viz = vizBlock('Top streams by volume',
        barChartFromPairs(sp, 'name', 'bytes', 'bytes'),
        rankTableHtml(sp, 'name', 'bytes', 'bytes')) +
      vizBlock('Label cardinality',
        barChartFromPairs(l.label_cardinality, 'label', 'values', ''),
        rankTableHtml(l.label_cardinality, 'label', 'values', ''),
        'cardinality');
  } else if (fam === 'tempo' || ds.tempo) {
    var te = ds.tempo || {};
    viz = '<p class="helper">' + esc(te.note ||
      'Tempo sampling is best-effort in this release.') + '</p>';
  }
  var errs = (ds.errors || []).length ?
    '<div class="helper" style="color:var(--amber)">' +
    (ds.errors || []).map(function (e) {
      return esc(String(e)); }).join('<br>') + '</div>' : '';
  return '<div class="ds-tcard"><div class="tc-head">' +
    chip(FAM_LBL[fam] || ds.family || 'datasource', 'info') +
    '<span class="tc-uid">' + esc(ds.uid || '') + '</span></div>' +
    (stats ? '<div class="cards-row">' + stats + '</div>' : '') +
    (viz ? '<div class="tc-viz">' + viz + '</div>' : '') + errs +
    '</div>';
}

/* ---- breakdown section ---- */
function renderBreakdownSection() {
  var el = $('#cost-breakdown'); if (!el) return;
  var c = App.cost;
  var head = '<h2>' + ico('zap', 15) + ' Cost breakdown</h2>';
  if (c.loadingCost && !c.cost) {
    el.innerHTML = head +
      '<div class="card"><div class="skel skel-chart"></div></div>';
    return;
  }
  if (!c.cost) {
    var msg = c.costErr ?
      errorCard('Cost analysis failed.', c.costErr) : '';
    el.innerHTML = head + msg +
      (c.pricing ? '<div class="card" id="cost-pricing">' +
        pricingPanelHtml(c.pricing) + '</div>' : '') +
      '<div class="empty"><span class="eico">' + ico('zap', 26) +
      '</span><b>No cost estimate yet.</b><br>Run an analysis to ' +
      'see your estimated monthly spend per component and where you ' +
      'can safely cut it.<div class="btnbar" ' +
      'style="justify-content:center">' +
      '<button class="btn primary" id="cost-empty-analyze" ' +
      'type="button">' + ico('zap', 14) +
      ' Analyze cost &amp; efficiency</button></div></div>';
    if (c.pricing) wirePricing();
    var b = $('#cost-empty-analyze');
    if (b) b.onclick = function () { onAnalyze(b); };
    return;
  }
  el.innerHTML = head + '<div class="grid2">' +
    '<div class="card"><div id="cost-viz"></div></div>' +
    '<div class="card" id="cost-pricing">' +
    pricingPanelHtml(c.pricing) + '</div></div>';
  wirePricing();
  renderCostViz();
}

function renderCostViz() {
  var el = $('#cost-viz'); if (!el) return;
  var c = App.cost, cost = c.cost || {};
  var comps = cost.components || [];
  var famCount = {};
  comps.forEach(function (m) {
    var f = (m.family || '').toLowerCase();
    famCount[f] = (famCount[f] || 0) + 1;
  });
  var series = comps.map(function (m) {
    var f = (m.family || '').toLowerCase();
    var nm = FAM_LBL[f] || m.family || 'component';
    if (famCount[f] > 1 && m.uid) {
      nm += ' (' + String(m.uid).slice(0, 6) + ')';
    }
    return { name: nm, points: [[0, num(m.monthly_cost)]] };
  });
  var donut = series.length ?
    chart('pie', { series: series }, { unit: 'usd' }) :
    chartEmpty('No component costs');
  var total = num(cost.monthly_total);
  var savings = c.savings || normalizeSavings({});
  var r = cost.resources || {};
  var resHtml = '';
  if (r.mimir_ram_gb_est != null || r.storage_gb_month_est != null) {
    var bits = [];
    if (r.mimir_ram_gb_est != null) {
      bits.push('~' + fmtNumP(num(r.mimir_ram_gb_est)) + ' GB RAM');
    }
    if (r.storage_gb_month_est != null) {
      bits.push('~' + fmtNumP(num(r.storage_gb_month_est)) +
        ' GB stored/mo');
    }
    resHtml = '<p class="helper">Rough resource estimate: ' +
      esc(bits.join(' · ')) + '</p>';
  }
  el.innerHTML =
    '<div class="cost-total"><span class="ct-num">' +
    esc(fmtMoney(total)) + '</span><span class="ct-lbl">estimated ' +
    'current spend / month</span></div>' + donut + resHtml +
    savingsHeroHtml(savings) +
    (c.loadingCost ? '<p class="recompute-note">' +
      '<span class="skel" style="width:14px;height:14px;' +
      'border-radius:50%;display:inline-block"></span> ' +
      'recomputing&hellip;</p>' : '');
  mountCharts(el);
}

function savingsHeroHtml(s) {
  s = s || {};
  var has = num(s.saved) > 0;
  return '<div class="savings-hero' + (has ? '' : ' flat') + '">' +
    '<div class="sh-flow"><div class="flow-num">' +
    '<div class="flow-n before">' + esc(fmtMoney(num(s.current))) +
    '</div><div class="k">current / mo</div></div>' +
    '<div class="flow-arrow">' + ico('arrow', 20) + '</div>' +
    '<div class="flow-num"><div class="flow-n after">' +
    esc(fmtMoney(num(s.projected))) +
    '</div><div class="k">projected / mo</div></div></div>' +
    '<div class="sh-big"><div class="sh-pct">~' +
    esc(String(Math.round(num(s.pct)))) + '%</div>' +
    '<div class="sh-sub">estimated ~' + esc(fmtMoney(num(s.saved))) +
    ' / mo saved</div></div>' +
    '<div class="sh-note">Estimated only, based on your pricing ' +
    'assumptions &mdash; never an exact bill. Nothing your ' +
    'dashboards use is ever proposed for removal.</div></div>';
}

function pricingMeta(k) {
  if (PRICING_META[k]) return PRICING_META[k];
  var lk = k.toLowerCase();
  var money = lk.indexOf('day') < 0 && (lk.indexOf('per') >= 0 ||
    lk.indexOf('usd') >= 0 || lk.indexOf('cost') >= 0 ||
    lk.indexOf('price') >= 0 || lk.indexOf('gb') >= 0 ||
    lk.indexOf('series') >= 0);
  return { label: humanize(k), help: '', money: money };
}

function pricingPanelHtml(pricing) {
  pricing = pricing || {};
  var fields = Object.keys(pricing).filter(function (k) {
    return typeof pricing[k] === 'number';
  }).map(function (k) {
    var meta = pricingMeta(k);
    return '<div class="pf"><label>' + esc(meta.label) +
      (meta.help ? costTermRaw(meta.help) : '') + '</label>' +
      (meta.money ? '<div class="in-money">' : '') +
      '<input type="number" step="any" min="0" data-pk="' + esc(k) +
      '" value="' + esc(String(pricing[k])) + '">' +
      (meta.money ? '</div>' : '') + '</div>';
  }).join('');
  if (!fields) {
    fields = '<p class="helper">No editable pricing inputs were ' +
      'returned by the server.</p>';
  }
  return '<h2>' + ico('wrench', 15) + ' Pricing assumptions ' +
    chip('assumptions', 'warn') + '</h2>' +
    '<p class="lead">Editable estimates &mdash; not real invoices. ' +
    'Set these to match your contract; the breakdown and savings ' +
    'recompute automatically.</p><div class="pricing-grid">' +
    fields + '</div><div class="btnbar">' +
    '<button class="btn small" id="pricing-reset" type="button">' +
    'Reset to defaults</button>' +
    '<span class="recompute-note" id="pricing-status"></span></div>';
}

function wirePricing() {
  var reset = $('#pricing-reset');
  if (reset) reset.onclick = function () {
    var c = App.cost;
    if (!c.pricingDefaults) return;
    c.pricing = JSON.parse(JSON.stringify(c.pricingDefaults));
    renderBreakdownSection();
    onPricingChange();
  };
  $all('#cost-pricing input[data-pk]').forEach(function (inp) {
    inp.oninput = onPricingChangeDebounced;
  });
}

function gatherPricing() {
  var inps = $all('#cost-pricing input[data-pk]');
  if (!inps.length) return null;
  var out = {}, base = App.cost.pricing || {};
  Object.keys(base).forEach(function (k) { out[k] = base[k]; });
  inps.forEach(function (inp) {
    var v = parseFloat(inp.value);
    out[inp.getAttribute('data-pk')] = isFinite(v) ? v : 0;
  });
  return out;
}

async function onPricingChange() {
  var c = App.cost;
  var pricing = gatherPricing();
  if (!pricing) return;
  c.pricing = pricing;
  var status = $('#pricing-status');
  if (status) status.textContent = 'saving & recomputing…';
  try { await api('/api/pricing', { pricing: pricing }); }
  catch (e) { /* persistence is best-effort */ }
  try {
    var body = { pricing: pricing };
    if (c.slug) body.slug = c.slug;
    var res = await api('/api/cost', body);
    if (res && res.job) res = await pollJob(res.job);
    applyCostResult(res);
    if (status) status.textContent = '';
    renderCostViz();
    renderRecsSection();
  } catch (e) {
    if (status) status.textContent = '';
    toast(e.message, 'err');
  }
}
var onPricingChangeDebounced = debounce(onPricingChange, 650);

/* ---- recommendations section ---- */
function renderRecsSection() {
  var el = $('#cost-recs'); if (!el) return;
  var c = App.cost, opt = c.optimize;
  var recs = (opt && opt.recommendations) || [];
  if (c.loadingCost && !opt) {
    el.innerHTML = '<h2>' + ico('wrench', 15) +
      ' Recommendations</h2><div class="card">' +
      '<div class="skel" style="height:80px"></div></div>';
    return;
  }
  if (!opt) { el.innerHTML = ''; return; }
  var sum = opt.summary || {};
  var safeN = sum.safe_count != null ? sum.safe_count :
    recs.filter(function (r) { return r.keeps_intact; }).length;
  var revN = sum.needs_review_count != null ? sum.needs_review_count :
    recs.filter(function (r) { return !r.keeps_intact; }).length;
  var dl = '/download/cost-config.zip' +
    (c.slug ? '?slug=' + encodeURIComponent(c.slug) : '');
  var head = '<div class="recs-head"><h2 style="margin:0">' +
    ico('wrench', 15) + ' Recommendations</h2>' +
    (recs.length ? chip(safeN + ' safe', 'ok') +
      (revN ? chip(revN + ' needs review', 'warn') : '') : '') +
    '<span class="grow"></span>' +
    (recs.length ? '<a class="btn primary" href="' + esc(dl) +
      '">' + ico('download', 14) + ' Download all config</a>' : '') +
    '</div>';
  if (!recs.length) {
    el.innerHTML = head + '<div class="empty"><span class="eico">' +
      ico('check', 26) + '</span><b>No savings found.</b><br>' +
      'Everything your datasources ingest is used by your ' +
      'dashboards, or traffic is too low to matter. Nice and lean.' +
      '</div>';
    return;
  }
  var so = { high: 0, medium: 1, low: 2 };
  var ordered = recs.slice().sort(function (a, b) {
    var d = (so[a.severity] == null ? 3 : so[a.severity]) -
            (so[b.severity] == null ? 3 : so[b.severity]);
    if (d) return d;
    return num((b.est_savings || {}).monthly_usd) -
           num((a.est_savings || {}).monthly_usd);
  });
  el.innerHTML = head + ordered.map(recCardHtml).join('');
}

function cfgHtml(config) {
  config = config || [];
  if (!config.length) return '';
  var grp = 'rg' + uid();
  var opts = config.map(function (cf, i) {
    return '<option value="' + i + '">' +
      esc(TARGET_LBL[cf.target] || cf.target ||
          ('option ' + (i + 1))) + '</option>';
  }).join('');
  var pres = config.map(function (cf, i) {
    return '<pre id="' + grp + '-' + i + '" data-recgrp="' + grp +
      '" data-recidx="' + i + '" data-note="' + esc(cf.note || '') +
      '"' + (i === 0 ? '' : ' hidden') + '>' + esc(cf.snippet || '') +
      '</pre>';
  }).join('');
  var lang = config[0].language ?
    ' ' + chip(config[0].language, 'dim') : '';
  return '<div class="rec-cfg"><div class="cfg-bar">' +
    '<label>Apply at</label><select data-rectarget="' + grp + '">' +
    opts + '</select>' + lang +
    '<span style="margin-left:auto"></span>' +
    '<button class="btn small ghost iconbtn" type="button" ' +
    'data-copy="' + grp + '-0" data-recgrp-btn="' + grp +
    '" title="Copy config" aria-label="Copy config">' +
    ico('copy', 13) + '</button></div>' + pres +
    '<div class="cfg-note" id="' + grp + '-note">' +
    esc(config[0].note || '') + '</div></div>';
}

function recCardHtml(rec) {
  rec = rec || {};
  var sev = rec.severity || 'low';
  var safe = rec.keeps_intact === true;
  var safeChip = safe ?
    chip('safe — nothing your dashboards use', 'ok',
      'Proven safe: this dimension is not referenced by any of ' +
      'your migrated dashboards, so removing it changes nothing ' +
      'you see.') :
    chip('review — touches used data', 'warn',
      'This touches a dimension your dashboards use. Review before ' +
      'applying; it is never dropped automatically.');
  var ev = rec.evidence || {};
  var evHtml = Object.keys(ev).map(function (k) {
    var v = ev[k];
    if (v == null) return '';
    var disp = typeof v === 'number' ? fmtNumP(v) : String(v);
    return chip(humanize(k) + ': ' + disp, 'dim');
  }).join('');
  var es = rec.est_savings || {};
  var saveBits = '';
  if (es.series != null) {
    saveBits += chip('-' + fmtNumP(num(es.series)) + ' series',
      'info');
  }
  if (es.streams != null) {
    saveBits += chip('-' + fmtNumP(num(es.streams)) + ' streams',
      'info');
  }
  if (es.gb_per_day != null) {
    saveBits += chip('-' + fmtNumP(num(es.gb_per_day)) + ' GB/day',
      'info');
  }
  var confCls = { high: 'ok', med: 'info', medium: 'info',
    low: 'warn' }[es.confidence] || 'dim';
  var confChipHtml = es.confidence ?
    chip(es.confidence + ' confidence', confCls) : '';
  var moneyHtml = es.monthly_usd != null ?
    '<span class="save-money">~' + esc(fmtMoney(num(es.monthly_usd))) +
    ' / mo</span>' : '';
  return '<div class="rec-card sev-' + esc(sev) + '">' +
    '<div class="rec-head"><span class="rec-title">' +
    esc(rec.title || 'Recommendation') + '</span>' +
    chip(sev, COST_SEV[sev] || 'dim') +
    (rec.kind ? chip(rec.kind, 'purple') : '') + safeChip + '</div>' +
    (rec.rationale ? '<div class="rec-rationale">' +
      esc(rec.rationale) + '</div>' : '') +
    (evHtml ? '<div class="rec-ev">' + evHtml + '</div>' : '') +
    '<div class="rec-save">' + moneyHtml +
    (moneyHtml && (saveBits || confChipHtml) ?
      '<span class="kv">estimated savings</span>' : '') +
    saveBits + confChipHtml + '</div>' + cfgHtml(rec.config || []) +
    '</div>';
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
/* Delegated handlers for the reusable components (copy buttons,
   expandable truncations, console pin/copy) -- bound once so any
   re-rendered HTML keeps working. */
document.addEventListener('click', function (ev) {
  var t = ev.target && ev.target.closest ?
    ev.target.closest('button') : null;
  if (!t) return;
  if (t.hasAttribute('data-copy')) {
    ev.preventDefault(); ev.stopPropagation();
    var src = document.getElementById(t.getAttribute('data-copy'));
    if (src) copyText(src.textContent, t);
  } else if (t.hasAttribute('data-expand')) {
    ev.preventDefault(); ev.stopPropagation();
    var rest = t.parentNode.querySelector('.trunc-rest');
    if (!rest) return;
    var show = rest.hasAttribute('hidden');
    if (show) rest.removeAttribute('hidden');
    else rest.setAttribute('hidden', '');
    t.textContent = show ? 'show less' :
      '… ' + (t.getAttribute('data-more') || 'show all');
  } else if (t.hasAttribute('data-cpin')) {
    ev.preventDefault(); ev.stopPropagation();
    t.classList.toggle('on');
    var on = t.classList.contains('on');
    t.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (on) {
      var body = $('.console-body', t.closest('.console'));
      if (body) body.scrollTop = body.scrollHeight;
    }
  } else if (t.hasAttribute('data-ccopy')) {
    ev.preventDefault(); ev.stopPropagation();
    var cb = $('.console-body', t.closest('.console'));
    if (cb) {
      copyText($all('.cline', cb).map(function (l) {
        var c = l.cloneNode(true);
        var ts = c.querySelector('.cts');
        if (ts) ts.remove();
        return c.textContent;
      }).join('\n'), t);
    }
  } else if (t.hasAttribute('data-wcdismiss')) {
    ev.preventDefault(); ev.stopPropagation();
    localStorage.setItem('nr2g-welcome', 'dismissed');
    var wc = t.closest('.welcome');
    if (wc) wc.remove();
  }
});

/* Recommendation config target picker: swap the visible snippet and
   re-point the copy button + note without a full re-render. Delegated
   so it survives every cost re-render. */
document.addEventListener('change', function (ev) {
  var sel = ev.target;
  if (!sel || sel.tagName !== 'SELECT' ||
      !sel.hasAttribute('data-rectarget')) return;
  var grp = sel.getAttribute('data-rectarget'), idx = sel.value;
  $all('pre[data-recgrp="' + grp + '"]').forEach(function (pre) {
    var on = pre.getAttribute('data-recidx') === idx;
    if (on) pre.removeAttribute('hidden');
    else pre.setAttribute('hidden', '');
    if (on) {
      var cb = $('[data-recgrp-btn="' + grp + '"]');
      if (cb) cb.setAttribute('data-copy', pre.id);
      var note = document.getElementById(grp + '-note');
      if (note) note.textContent = pre.getAttribute('data-note') || '';
    }
  });
});

$('#jobsbtn').onclick = function () { openDrawer(); };
$('#drawer-close').onclick = function () { openDrawer(false); };
$('#helpbtn').onclick = function () { openHelp(); };
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape') {
    if ($('#modal-slot').innerHTML) closeModal();
    else if ($('#flyout-slot').innerHTML) closeFlyout();
    else if (Jobs.open) openDrawer(false);
  }
});

/* Global shortcuts: "?" help, "g" then a letter to jump views, "j"
   jobs, "t" theme. Never fires while typing in a field. */
var _gPending = false, _gTimer = null;
document.addEventListener('keydown', function (ev) {
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  var tag = (ev.target && ev.target.tagName) || '';
  var typing = tag === 'INPUT' || tag === 'TEXTAREA' ||
    tag === 'SELECT' || (ev.target && ev.target.isContentEditable);
  if (_gPending) {
    _gPending = false; clearTimeout(_gTimer);
    var dest = GKEYS[(ev.key || '').toLowerCase()];
    if (dest && !typing) { ev.preventDefault(); location.hash = dest; }
    return;
  }
  if (typing) return;
  if (ev.key === '?') { ev.preventDefault(); openHelp(); }
  else if (ev.key === 'g') {
    _gPending = true;
    _gTimer = setTimeout(function () { _gPending = false; }, 1200);
  } else if (ev.key === 'j') { ev.preventDefault(); openDrawer(); }
  else if (ev.key === 't') {
    ev.preventDefault(); $('#themebtn').click();
  }
});

applyTheme(localStorage.getItem('nr2g-theme') || 'auto');
window.addEventListener('hashchange', route);
(async function boot() {
  await refreshState();
  setInterval(refreshState, 8000);
  renderJobsBtn();
  route();
})();
</script>
</body>
</html>
"""
