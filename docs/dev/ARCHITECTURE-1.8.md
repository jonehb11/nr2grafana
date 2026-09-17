# nr2grafana 1.8 — paste-to-convert + AI conversion copilot

Fills the highest-impact core-flow gaps from the audit. Theme: **converting
a New Relic dashboard to Grafana must be dead-easy in the UI, and AI must be
able to convert/fix the hard panels live.** Builds on 1.1–1.7.1. Version
1.8.0. Zero deps (Python 3.9+ stdlib). Secrets in memory. NR/AWS read-only.

Three cross-file seams are agreed up front so the server (S) and UI (U)
agents never edit each other's files:
- SEAM-1 (AI context): server `_enrich_ai_context` sends, and `ai.py`
  consumes, these extra keys: `mode` ("fix"|"convert"), `translation_notes`
  (list of the converter's per-widget notes), `original_nrql` (the widget's
  raw NRQL), `ds_family` ("prometheus"|"loki"|"tempo"), `confidence`
  (exact|approximate|needs-review|untranslatable).
- SEAM-2 (paste convert): `POST /api/convert` accepts an `nr_json` field
  (a NR dashboard object OR array/list of them); when present the server
  parses+builds+packages+persists directly (no filesystem). Response is the
  same job/result shape as a directory convert.
- SEAM-3 (AI apply): `POST /api/ai/suggest` already returns
  `{explanation, fixed_expr, confidence, actions}`. `POST /api/panel/update`
  already applies an expr to a (panel_id, refId). For target-less
  untranslatable panels the UI calls a new `POST /api/panel/convert`
  {slug, panel_id, expr, ds_family} that CREATES a real target+datasource
  on the panel (converting a text-placeholder into a live query panel),
  rewrites the stored + package dashboard.json + datatest.json, records a
  change, and returns the updated panel.

## A. `ai.py` + `aicontext.py` — conversion-mode AI (owner: ai agent)
- Add a CONVERSION system framing distinct from `_FIX_SYSTEM`
  (`_CONVERT_SYSTEM`): "A New Relic dashboard panel was auto-migrated to
  Grafana/LGTM. The converter marked it needs-review or untranslatable for
  the reasons in translation_notes. Produce a higher-fidelity (or
  from-scratch) <ds_family> query that reproduces the original NRQL's intent
  against an OTel-fed LGTM stack. Return strict JSON
  {explanation, fixed_expr, confidence, actions}." Pick the framing by
  `context["mode"]`.
- `_fix_prompt` (shared) must include, when present: original_nrql,
  translation_notes, confidence, ds_family, requirements/label hints,
  instance metric/label samples. Keep the strict-JSON parsing + fallbacks.
- `aicontext.build_context`: include per-panel `translation_notes` and
  `original_nrql` so the AI-context bundle also carries them.
- Owns: ai.py, aicontext.py + their tests. Publishes SEAM-1 keys.

## B. `web/server.py` — paste convert, panel convert, AI enrich (owner: server agent)
- SEAM-2: `/api/convert` `nr_json` branch — accept object or list; derive a
  slug from each dashboard name; `model.parse_nr_dashboard` ->
  `grafana.builder.build_dashboards` -> requirements/artifacts/package ->
  persist (dashboard, widget-report, requirements, nr-source). Reuse the
  existing convert job pipeline; validate the posted JSON (clear 400 on a
  non-dashboard). Never touch the filesystem for the paste path (package to
  the configured out_dir like normal, but input is in-memory).
- SEAM-3: `POST /api/panel/convert` {slug, panel_id, expr, ds_family} —
  find the panel (recurse rows), attach a real target with the family's
  datasource ref + expr (expr key per family: expr for prom/loki, query for
  tempo), set the panel datasource, flip a text-placeholder panel to the
  proper viz where known (else keep type but add the target), rewrite stored
  + package dashboard.json + datatest.json, ChangeLog record, return the
  panel. Guarded by the 1.7.1 security guard already.
- SEAM-1: `_enrich_ai_context` populates mode/translation_notes/
  original_nrql/ds_family/confidence from the widget-report + the request
  (`mode` defaults "fix"; the UI sends "convert" for needs-review/
  untranslatable panels). translation_notes come from the stored
  widget-report row for that panel.
- New: `POST /api/ai/convert-panels` {slug} (job) — iterate the dashboard's
  needs-review/untranslatable panels, call the conversion-mode suggest per
  panel, return proposed {panel_id, refId, original_nrql, notes, proposed
  expr, explanation, confidence} WITHOUT applying (the UI reviews then
  applies via panel/convert or panel/update). Persist nothing until applied.
- Owns: web/server.py + tests/test_web.py. Consumes SEAM-1 (from A).

## C. `web/ui.py` — paste UI, AI copilot, verdict labels, flow polish (owner: ui agent)
- **Paste-to-convert** (Convert view): a "Paste New Relic dashboard JSON"
  textarea + "Convert pasted JSON" button -> POST /api/convert {nr_json}.
  Accept one object or an array. Show the same job log + result cards.
- **AI copilot on the hard panels** (panel detail): render an "Ask AI to
  translate this" action for target-LESS untranslatable/needs-review panels
  (today Ask-AI only renders inside targets.forEach, so these dead-end). On
  success show the proposed expr + explanation with **Apply** (calls
  /api/panel/convert to create the target) and **Apply & Test**. For panels
  that DO have a target but are approximate/needs-review, add "Ask AI to
  improve" (mode=convert) alongside the existing fix flow.
- **Batch**: a "Convert flagged panels with AI" button on the dashboard/
  workspace -> POST /api/ai/convert-panels (job) -> a review list of
  proposed diffs with per-row Apply / Apply&Test / Skip.
- **Verdict labels**: add VERDICT_CLS/HELP/LBL/worstVerdict entries for the
  new "unverifiable" and "unverifiable-logs" verdicts from 1.7.1 (gray
  "needs manual check" chip with a plain-language tooltip).
- **Flow polish** (from audit B/C): a "Fetch & convert selected" button that
  chains fetch->convert on the picked GUIDs; collapse the desyncing
  fetch-dir/convert-dir into one shared working-directory field (mirror on
  change); render the journey stepper on the Convert view; CSRF token echo
  is NOT needed (1.7.1 guard is origin-based).
- esc() on every server string; keep test hooks/ids; single module string;
  no external assets; node --check clean; page < 560KB. Owns ui.py ONLY.
  Consumes SEAM-2/SEAM-3 route shapes.

## D. e2e (owner: server agent, in test_e2e_mock via seam)
Extend tests/test_e2e_mock.py: (1) paste-convert a fixture dashboard object
through /api/convert nr_json and assert it persists + packages; (2) drive
conversion-mode AI suggest for an untranslatable panel (fake local agent
returning a PromQL) and apply via /api/panel/convert, asserting the panel
gains a real target and datatest updates; (3) update the e2e verdict
allowlist/threshold for the 1.7.1 "unverifiable"/"unverifiable-logs"
verdicts (the audit noted test_e2e_mock hardcodes an allowlist that must
include them). (Server agent owns test_e2e_mock for this release.)

## Cross-cutting
1. stdlib only, py3.9, ASCII/LF/4-space/79-col.
2. Keep the full suite green. AI conversions are proposals the user applies;
   nothing auto-applies to a dashboard without an explicit apply call.
3. Coordinator bumps version to 1.8.0 and writes docs/README after build.
