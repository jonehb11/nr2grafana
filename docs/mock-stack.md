# Mock stack: demo & e2e-test the whole product offline

`tools/mock_stack.py` runs a fake Grafana and a fake New Relic
NerdGraph endpoint on localhost — stdlib only, in-memory, no Docker,
no accounts, no network beyond the loopback. Every 1.2 feature that
talks to Grafana (check, test, parity, diagnose, fix/heal, datasource
management, import, downloads, the whole web UI) can be exercised
against it, which makes it the fastest way to see the product work
before pointing it at real infrastructure — and it is what the
end-to-end suite (`tests/test_e2e_mock.py`) runs against in CI.

```bash
python3 tools/mock_stack.py --port 3000 --nr-port 3001
```

```
mock Grafana:   http://127.0.0.1:3000   (token: mock-token)
mock NerdGraph: http://127.0.0.1:3001/graphql   (API key: NRAK-MOCK)
serving 11 fixture dashboard(s); Ctrl-C to stop
```

- **Fake Grafana** on `:3000` — bearer-token auth (the token it
  prints at startup; `--token` to change it; `/api/health` is public
  like the real one, everything else 401s without the token).
  In-memory datasource CRUD (`/api/datasources`, uid routes, health),
  folders, dashboard import/search, the Prometheus/Loki proxy
  introspection endpoints, and `POST /api/ds/query` returning
  deterministic dataframes for the metrics in its inventory — seeded
  with exactly what the bundled sample dashboards translate to.
  Unknown metrics return empty frames, so the no-data and diagnose
  paths are demoable too. It starts with **no datasources**, so the
  requirements-check and add-datasource steps have real work to do.
- **Fake NerdGraph** on `:3001` — API-key auth (`--nr-key`), the
  bundled NR dashboard fixtures as entities (`--fixtures DIR` to
  serve your own), and deterministic `nrql()` results that agree with
  the fake Grafana's numbers, so parity finds matches. The e2e tests
  point the NerdGraph client at it to cover fetch and the NR side of
  parity; the real CLI always talks to `api.newrelic.com` /
  `api.eu.newrelic.com`, so the walkthrough below converts the
  bundled fixture files instead of fetching, and skips the NR side
  of parity.

`--verbose` logs every request to stderr. Nothing is persisted;
restart the process to reset.

## Walkthrough

Point the normal commands at the mock Grafana:

```bash
export GRAFANA_URL=http://127.0.0.1:3000 GRAFANA_TOKEN=mock-token

# 1. convert the bundled sample NR dashboard into a package
python3 -m nr2grafana convert fixtures/newrelic/sample-service-dashboard.json \
    -o ./demo-out --package
```

```
sample-service-dashboard.json -> ./demo-out/checkout-service-overview/dashboard.json  (6 approximate, 4 exact, 7 needs-review, 1 untranslatable; 3 datasources (loki, prometheus, tempo); data domains: apm, browser-rum, infra-host, k8s, logs, traces; 1 NR-native panel needs manual attention)
Index: ./demo-out/INDEX.md

1 dashboards written, 18 widgets converted (8 need review). Report: ./demo-out/migration-report.json
```

The mock starts empty, so the requirements check reports what's
missing (and exits 1, as it would in CI):

```bash
python3 -m nr2grafana grafana check ./demo-out/*/
```

```
Checkout Service Overview  (./demo-out/checkout-service-overview/requirements.json)
  missing    datasource:loki  no datasource of type 'loki' on http://127.0.0.1:3000
             fix: Add a Loki datasource: Connections -> Data sources -> Add -> Loki
  missing    datasource:prometheus  no datasource of type 'prometheus' on http://127.0.0.1:3000
             fix: Add a Prometheus datasource: Connections -> Data sources -> Add -> Prometheus
  missing    datasource:tempo  no datasource of type 'tempo' on http://127.0.0.1:3000
             fix: Add a Tempo datasource: Connections -> Data sources -> Add -> Tempo

3 requirement(s) not met (see fixes above)
```

Create them against the mock's CRUD (health-checked immediately):

```bash
python3 -m nr2grafana grafana add-datasource --type prometheus \
    --name Mimir --set url=http://127.0.0.1:9009
python3 -m nr2grafana grafana add-datasource --type loki \
    --name Loki --set url=http://127.0.0.1:3100
python3 -m nr2grafana grafana add-datasource --type tempo \
    --name Tempo --set url=http://127.0.0.1:3200
python3 -m nr2grafana grafana datasources
```

```
HEALTH     NAME                     TYPE                         DEFAULT  UID
ok         Mimir                    prometheus                   yes      mock-ds-1
ok         Loki                     loki                                  mock-ds-2
ok         Tempo                    tempo                                 mock-ds-3
```

Now data tests pass, because the mock's metric inventory matches what
the sample dashboard translates to:

```bash
python3 -m nr2grafana grafana test ./demo-out/*/
```

```
Checkout Service Overview:
  data    Throughput [A] 1 frame(s), 31 point(s)
  data    p95 / p99 Latency [A] 1 frame(s), 31 point(s)
  ...
  no-data Slow error traces [A] 1 frame(s), 0 point(s)
  ...

18 target(s) returned data, 1 no-data (warning), 0 error(s)
```

To watch diagnose and heal actually work, break a metric name the way
a real pipeline mismatch would (this is exactly what the e2e test
does):

```bash
sed -i.bak 's/checkout_orders_completed/checkout_orders_complete/' \
    demo-out/checkout-service-overview/dashboard.json \
    demo-out/checkout-service-overview/datatest.json

python3 -m nr2grafana grafana diagnose ./demo-out/*/
```

```
Checkout Service Overview:
  diagnosis -> demo-out/checkout-service-overview/diagnosis.json
  warn     panel       panel 21: Panel 21: metric 'checkout_orders_complete' does not exist in the datasource - did you mean 'checkout_orders_completed'?
           fix (edit-query): Replace 'checkout_orders_complete' with 'checkout_orders_completed' in the query.
  info     config      Recurring rename patterns can be codified so the next conversion produces working queries directly.
           fix (config-overlay): Merge this overlay into your converter config (config-overlay.json) and re-run convert with it.
```

```bash
python3 -m nr2grafana grafana heal ./demo-out/*/
```

```
Checkout Service Overview:
auto-heal round 1: 2 finding(s), 2 safe fix(es)
auto-heal:   FIXED  panel 21 [A]: expr updated
auto-heal:   FIXED  merged {"metric_map": {"checkout_orders_complete": "checkout_orders_completed"}} into demo-out/config-overlay.json ...
auto-heal round 2: 0 finding(s), 0 safe fix(es)
auto-heal: converged (no new safe fixes)
round 1: 17 data, 2 no-data; 2 finding(s), 2 fixed
round 2: 18 data, 1 no-data; 0 finding(s), 0 fixed
2 fix(es) applied, 0 finding(s) remaining (converged)
```

Finally, import into the fake Grafana:

```bash
python3 -m nr2grafana grafana import ./demo-out --folder Demo
```

```
connected -- Grafana 11.0.0-mock

1 imported, 0 failed
  ok   Checkout Service Overview  /d/nr-checkout-service-overview/checkout-service-overview
```

### Parity against the mock

`grafana parity` requires an NR key flag-wise, and the real CLI's
NerdGraph client only talks to the real New Relic endpoints — so run
it with a dummy key and no account id: it prints `NR side will be
skipped`, never touches the network, and the Grafana side still runs.
Panels whose Grafana query returned data earn partial credit (the
0.6 "data without an NR comparison" weight):

```bash
NEW_RELIC_API_KEY=NRAK-DEMO python3 -m nr2grafana grafana parity ./demo-out/*/
```

```
note: no New Relic account id known -- pass --account-id or set NEW_RELIC_ACCOUNT_ID (NR side will be skipped)
Checkout Service Overview:
  results -> demo-out/checkout-service-overview/parity-results.json
  nr-error        [A] Throughput                       no New Relic account id available
  ...

score 54/100  (19 nr-error)
readiness: blocked (54/100)
  - 19 panel(s) hit errors during the parity run
```

Full two-sided parity (matches, unit-ratio hints, deliberate
mismatches) is exercised by the e2e suite, which points the NerdGraph
client at the mock's port.

## The zero-touch demo

The same thing without a terminal beyond starting two processes:

```bash
python3 tools/mock_stack.py --port 3000 --nr-port 3001 &
python3 -m nr2grafana web
```

In the browser: enter `http://127.0.0.1:3000` + `mock-token` on the
Connect step, then walk the stepper — point Convert at the bundled
`fixtures/newrelic` samples (skipping Fetch, which talks to the real
NR API), then Datasources → Validate → Fix → Import → Verify →
Download. Every button exercises the same API the CLI uses, ending
with validated dashboard JSON downloads.

## e2e tests

```bash
python3 -m unittest tests.test_e2e_mock -v
```

Boots both mock servers on ephemeral ports and asserts the whole loop
— fetch → convert --package → check → test → parity → diagnose → fix
→ import — succeeds against them, including the NR side (the tests
aim the NerdGraph client at the mock's port) and a deliberate 10x
backend disagreement that parity must flag.
