# Datasource management

Since 1.2 you never need to open the Grafana admin UI to get a
migration's datasources in place: list, health-check, create, edit and
delete them from nr2grafana — CLI, web UI, or JSON API. All of it goes
through your Grafana service-account token; nothing talks to
Mimir/Loki/Tempo directly except via Grafana's own proxy.

## What your token can do

Different steps need different Grafana roles:

| Operation | Minimum role |
|---|---|
| check / test / parity / diagnose | Viewer |
| import dashboards, apply query fixes, heal | Editor |
| create / edit / delete datasources | Admin |

`POST /api/grafana/test-token` (the web UI's "Test" button on the
Connect step) probes what the configured token can actually do — with
GETs only, nothing destructive — and returns
`{"ok", "health", "permissions"}`; the permissions report:

```json
{
  "user": "sa-nr2grafana",
  "role": "Editor",
  "can_admin_datasources": false,
  "can_edit_dashboards": true,
  "detail": "authenticated as 'sa-nr2grafana'; org 'Main Org.'; can list datasources; no datasources:create permission - Admin role needed to create datasources"
}
```

On Grafana versions without the access-control API the report says so
and degrades conservatively (datasource-admin rights unverified rather
than claimed). Bad tokens surface as `token rejected (401): check the
service-account token` — the same text the diagnostics engine turns
into an `auth` finding.

## Listing and health

```bash
python3 -m nr2grafana grafana datasources
```

```
HEALTH     NAME                     TYPE                         DEFAULT  UID
ok         Mimir                    prometheus                   yes      mimir
ok         Loki                     loki                                  loki
error      Tempo                    tempo                                 tempo
           connection refused
```

Health uses Grafana's `GET /api/datasources/uid/<uid>/health` where it
exists; datasource types that predate that endpoint get a cheap probe
query through `/api/ds/query` instead (`vector(1)` for Prometheus, a
`count_over_time` for Loki, an empty TraceQL search for Tempo). Types
with neither report `unknown` rather than a false alarm. A failing
health check in `grafana diagnose` additionally explains the likely
causes: the URL must be reachable **from the Grafana server** (not
your workstation), auth headers, TLS.

## Adding a datasource

Seven types ship as guided templates — the same specs drive the CLI,
the web UI's "Add datasource" flyout, and `GET
/api/grafana/ds-templates`:

| Type | Plugin | Fields |
|---|---|---|
| `prometheus` | core | `url` (required), `httpMethod`, `timeInterval` |
| `loki` | core | `url` (required), `maxLines` |
| `tempo` | core | `url` (required) |
| `cloudwatch` | core | `authType` (required), `defaultRegion` (required), `accessKey`*, `secretKey`*, `assumeRoleArn` |
| `stackdriver` (Google Cloud Monitoring) | core | `authenticationType` (required), `defaultProject`, `clientEmail`, `tokenUri`, `privateKey`* |
| `grafana-azure-monitor-datasource` | core | `cloudName`, `tenantId` (required), `clientId` (required), `clientSecret`* (required), `subscriptionId` |
| `nrgrafanaplugin-newrelic-datasource` | community — install first | `apiKey`* (required), `accountId` (required), `region` |

Fields marked * are secrets: masked in the web UI, prompted for via
`getpass` in the CLI when omitted, sent to Grafana as `secureJsonData`
(write-only there), and never logged or stored by nr2grafana.

```bash
python3 -m nr2grafana grafana add-datasource --type loki --name Loki \
    --set url=http://loki.monitoring.svc:3100
```

```
created datasource 'Loki' (type loki, uid adhkijp2bs5kwf)
health: ok -- Data source successfully connected.
note: For multi-tenant Loki the X-Scope-OrgID header must be added as a custom HTTP header in Grafana after creation; it cannot be set through this form.
```

The health check runs immediately after creation, so a wrong URL or
bad credentials fail right there (exit 1) instead of as an empty panel
later. Missing required fields print the full `--set` field list for
that type, and creation failures remind you that an Admin
service-account token is required.

### CloudWatch

Grafana queries AWS **from the Grafana server**, so that host needs
network access to AWS plus credentials: `authType=keys` with
`accessKey`/`secretKey`, or `authType=default` to use the instance
profile / environment credential chain on the Grafana server, or
`authType=credentials` for a shared credentials file there.
`assumeRoleArn` optionally switches roles for queries. The IAM
identity needs CloudWatch read permissions
(`cloudwatch:GetMetricData`, `ListMetrics`, and
`logs:StartQuery`/`GetQueryResults` for Logs Insights).

```bash
python3 -m nr2grafana grafana add-datasource --type cloudwatch \
    --name CloudWatch --set authType=keys --set defaultRegion=us-east-1
Access key ID (accessKey, blank to skip): ...
Secret access key (secretKey, blank to skip): ...
```

### Google Cloud Monitoring (`stackdriver`)

The Grafana UI's "upload service account key file" button does not
exist in the HTTP API, so paste the fields from the downloaded JSON
key instead, with `authenticationType=jwt`: `defaultProject` (the
key's `project_id`), `clientEmail`, `tokenUri`, and the full
`private_key` block (`-----BEGIN PRIVATE KEY-----` ... ) as
`privateKey`. On GCE you can use `authenticationType=gce` and skip the
rest. The service account needs the **Monitoring Viewer** role.

### Azure Monitor

Create an App Registration in Microsoft Entra ID, grant it the
**Monitoring Reader** role on the subscription, and supply `tenantId`,
`clientId` and a `clientSecret` from its "Certificates & secrets"
page. `cloudName` defaults to public Azure (`azuremonitor`);
`subscriptionId` sets a default subscription for queries.

### New Relic passthrough

For panels converted with `--passthrough` (no LGTM equivalent exists):
install the community plugin first — `grafana-cli plugins install
nrgrafanaplugin-newrelic-datasource`, then restart Grafana — and
supply a New Relic USER `apiKey` plus the numeric `accountId`. Only
query access is ever used; nr2grafana never mutates New Relic.

### Post-creation notes

Some settings cannot be expressed in these forms and are added in
Grafana after creation (the template's notes say so inline): basic
auth / client TLS certs on Prometheus, the `X-Scope-OrgID` header for
multi-tenant Loki, and Tempo's trace-to-logs/metrics links (which
reference the Loki/Prometheus datasource uids once those exist).

## Editing and deleting

The web UI's Datasources view supports edit and delete (delete asks
for typed confirmation). Over the API:

- `POST /api/grafana/datasource` `{"type", "name", "values"}` —
  create from a template + immediate health result
- `PUT /api/grafana/datasource/<uid>` — update
- `DELETE /api/grafana/datasource/<uid>` — remove
- `POST /api/grafana/datasource/<uid>/health` — re-check health
- `GET /api/grafana/ds-templates` — the template specs above

## How this ties into diagnostics

`grafana diagnose` reports each missing required datasource as a
finding whose fix carries a *prepared* `create_datasource` payload —
type, access mode and known `jsonData` prefilled, with the fields only
you can know (URLs, credentials) listed under `needs_input`. Applying
the fix is refused until those are filled in, and auto-heal never
creates datasources on its own: creating infrastructure with guessed
values is exactly the kind of fix that should require a human. See
[parity-and-diagnostics.md](parity-and-diagnostics.md).
