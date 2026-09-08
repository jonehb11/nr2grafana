# Web UI

A localhost single-page app over the whole migration — no frameworks,
no CDN, no extra dependencies; it's the same library code behind a
browser front-end.

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
  Setup page live in process memory only — never written to the local
  database, never echoed back by the API, gone when the server stops.
  They are pre-filled from `NEW_RELIC_API_KEY` / `GRAFANA_URL` /
  `GRAFANA_TOKEN` / `ANTHROPIC_API_KEY` env vars if set.
- Non-secret preferences (URLs, directories, region, AI model) persist
  in the local store (`~/.nr2grafana/`) and are restored on restart.

## Walkthrough

The left nav follows the migration order; the header shows live
NR / Grafana / AI connection status.

1. **Setup** — enter the New Relic API key + region, Grafana URL +
   service account token, and (optionally) an Anthropic API key.
2. **Dashboards** — everything fetched/converted so far, from the
   local store. Click through to a dashboard's detail view.
3. **Convert & Package** — pick input/output dirs and a config, run
   fetch/convert as background jobs with a live log (long operations
   return a job id; the UI polls until done).
4. **Validate & Test** — run the live requirement check and per-panel
   data tests against your Grafana. The dashboard detail view then
   shows, per panel: the confidence badge, test status
   (data / no-data / error with the actual error text), the original
   NRQL next to the translated query, and an inline editor with
   **Test** (try a candidate query without saving), **Ask AI**,
   **Save**, and **Save & Push** (updates the dashboard in Grafana).
   A requirements card up top lists what to install first.
5. **Import** — bulk import into a Grafana folder, with per-dashboard
   results.
6. **Changes** — every recorded edit, plus the suggested config
   overlay to copy into your mapping config
   (see [changes-and-codify.md](changes-and-codify.md)).
7. **AI Assistant** — free-form chat with the migration context
   (see [ai-assist.md](ai-assist.md)).

Dark theme by default, light theme honored via the OS preference or
the in-app toggle.

## API

Everything the UI does goes through a JSON API under `/api/` on the
same port, so it's scriptable too: `GET /api/state`,
`POST /api/settings`, `POST /api/nr/list|fetch`, `POST /api/convert`,
`GET /api/dashboards[/<slug>]`, `POST /api/grafana/health|datasources|
plugins|check|test|import`, `POST /api/panel/test`,
`POST /api/panel/update`, `GET /api/changes`,
`GET /api/changes/suggest-config`, `POST /api/ai/suggest|chat`.
Long operations return `{"job": id}`; poll `GET /api/jobs/<id>` for
`{"status", "log", "result"}`. Errors are always JSON
`{"error": "..."}` with a 4xx/5xx status.
