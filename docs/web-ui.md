# Web UI

A localhost single-page app over the whole migration — no frameworks,
no CDN, no extra dependencies; it's the same library code behind a
browser front-end. In 1.2 it is the one-stop shop: after entering two
API keys you never need to open New Relic or Grafana directly.

```bash
python3 -m nr2grafana web            # binds 127.0.0.1:8765, opens browser
python3 -m nr2grafana web --port 9000 --no-browser
python3 -m nr2grafana web --host 0.0.0.0   # only if you know what you're doing
```

If the port is taken you get a one-line message (pick another with
`--port`), not a traceback.

## Security model

- Binds `127.0.0.1` only by default.
- Secrets (New Relic key, Grafana token, Anthropic key) entered on the
  Connect step live in process memory only — never written to the
  local database, never echoed back by the API, gone when the server
  stops. They are pre-filled from `NEW_RELIC_API_KEY` / `GRAFANA_URL`
  / `GRAFANA_TOKEN` / `ANTHROPIC_API_KEY` env vars if set.
- Non-secret preferences (URLs, directories, region, AI model) persist
  in the local store (`~/.nr2grafana/`) and are restored on restart.
- Datasource credentials you enter in the "Add datasource" form go
  straight to Grafana as `secureJsonData` (write-only there) and are
  never logged or stored locally.

## The workflow

Each dashboard workspace carries a stepper across the top — **Connect
→ Fetch → Convert → Datasources → Validate → Fix → Import → Verify →
Download** — where every step shows its state (done / needs attention
/ blocked) and clicking a step jumps to it.

1. **Connect** — enter the New Relic API key + region and the Grafana
   URL + service-account token (optionally an Anthropic key for AI
   assistance). Both get a live **Test** button: the NR key check
   lists your accounts; the Grafana check reports exactly what the
   token can do (role, dashboard-edit, datasource-admin) so you find
   out *now*, not at import time.
2. **Overview** — one card per dashboard with a readiness ring
   (0–100), verdict chips, and a single primary action ("2 blockers —
   fix now").
3. **Fetch / Convert** — background jobs with live logs. "Browse &
   pick…" lists every dashboard the NR key can see (name + account,
   filterable) so a subset can be fetched without ever hunting GUIDs
   down in the New Relic UI.
4. **Datasources** — the instance's datasources as a table (name,
   type, uid, default, live health badge with re-check), plus an "Add
   datasource" flyout generated entirely from the guided templates:
   labeled fields with inline help, secret masking, create →
   immediate health result inline. Edit and delete too (delete asks
   for typed confirmation). See
   [datasource-management.md](datasource-management.md).
5. **Validate / Fix** — the dashboard workspace shows one row per
   panel: confidence badge, data-test badge, and parity badge
   (match/close/mismatch with the ratio hint). Expanding a row shows
   the original NRQL, the translated query, and the New Relic vs
   Grafana results side by side (inline sparklines with last values;
   tables for scalars), any error text verbatim, and the diagnosis
   findings for that panel with one-click **Fix** buttons that show
   exactly what will change before applying. The query editor has
   metric-name autocomplete backed by the live instance, plus
   **Test** / **Ask AI** / **Save** / **Save & Push**.
6. **Diagnostics** — all findings ordered blocker → info, each with
   evidence and its exact remediation, plus **Auto-heal**, which runs
   the test→diagnose→fix loop and streams each round into the log
   drawer. What auto-heal will and won't touch:
   [parity-and-diagnostics.md](parity-and-diagnostics.md).
7. **Verify & Download** — run parity across all panels, see the
   readiness summary, and download the validated dashboard JSON, the
   package zip, or everything as one zip. The buttons arm green when
   readiness is ready/almost — downloading while blocked is still
   allowed, with a warning.

Global: a job log drawer, toasts, empty states that name the next
action, keyboard-focusable controls. Dark theme by default, light
honored via the OS preference or the in-app toggle.

## API

Everything the UI does goes through a JSON API under `/api/` on the
same port, so it's scriptable too. Errors are always JSON
`{"error": "..."}` with a 4xx/5xx status; long operations return
`{"job": id}` — poll `GET /api/jobs/<id>` for
`{"status", "log", "result"}`.

Session & sources:
`GET /api/state` (includes feature availability flags),
`POST /api/settings`, `POST /api/nr/test-key`, `POST /api/nr/list`,
`POST /api/nr/fetch`, `POST /api/convert`,
`GET /api/dashboards[/<slug>]`.

Grafana & datasources:
`POST /api/grafana/health|test-token|datasources|plugins|check|test|import`,
`GET /api/grafana/ds-templates`,
`POST /api/grafana/datasource` (create + immediate health),
`PUT|DELETE /api/grafana/datasource/<uid>`,
`POST /api/grafana/datasource/<uid>/health`.

Parity, diagnostics & fixes:
`POST /api/parity {slug, from?, to?}` (job),
`POST /api/diagnose {slug}` (job),
`POST /api/heal {slug, push?}` (job),
`POST /api/fix {slug, finding_id, push?, values?}` (`values` fills an
add-datasource fix's `needs_input` fields — e.g. the URL — so the
datasource is created without leaving Diagnostics),
`GET /api/readiness?slug=`,
`POST /api/panel/test`, `POST /api/panel/update`.

Editor autocomplete:
`GET /api/metrics?uid=&q=` (up to 200 matches, cached ~60 s per
datasource), `GET /api/labels?uid=&type=prometheus|loki[&label=]`.

Changes & AI:
`GET /api/changes?slug=`, `GET /api/changes/suggest-config`,
`POST /api/ai/suggest|chat`.

Downloads (Content-Disposition attachments; slugs are validated
against the store, so path traversal is impossible):
`GET /download/dashboard/<slug>.json` (current, post-fix JSON),
`GET /download/package/<slug>.zip`,
`GET /download/all.zip`.
