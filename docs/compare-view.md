# Side-by-side comparison & live datasource flow

Since 1.4 the web UI can show your **New Relic dashboard and the migrated
Grafana dashboard next to each other, both drawing real data**, and let you
**add a datasource and watch data start flowing** without leaving the app.

## Compare view

Open `n2g web`, pick a converted dashboard, and go to **Compare**. The app:

1. Runs every panel's original NRQL against New Relic (read-only) and the
   translated query against Grafana.
2. Draws both results with the same chart type, laid out in the Grafana
   grid order — New Relic on the left, Grafana on the right.
3. Puts an agreement badge on each pair:

   | Badge | Meaning |
   |---|---|
   | **match** | The two sides agree within tolerance. |
   | **close** | Consistent constant ratio — usually a unit difference (the badge shows the ratio, e.g. "~1000× — likely ms vs s"). |
   | **value-mismatch** | Both have data, but the numbers differ. |
   | **shape-mismatch** | Different series/facets. |
   | **no-data** | One side returned nothing (gray). |
   | **error** | A query failed (red) — the exact error is shown. |

Top bar: an overall **readiness/agreement score ring**, a verdict tally, a
**time-range picker** (15m / 1h / 6h / 24h / custom) that re-runs the
comparison, **sync-hover** (hovering a point marks the aligned time on the
other side), and a **"show only disagreements"** filter so you can jump
straight to what needs attention. Click any panel to open its detail —
the query editor, diagnosis, and one-click fixes.

Charts are drawn with a built-in inline-SVG renderer (no external
libraries): timeseries, stat, bar, gauge, table, logs, and pie.

CLI equivalent: `grafana parity` produces the same verdicts/score as JSON.

## Add a datasource and watch it flow

In **Datasources** (or from any diagnosis that says "add a datasource"),
the guided **Add datasource** form creates the datasource and then
immediately shows a **before/after flow card**: how many panels had data
before, how many are flowing now, a real sample chart pulled through the
new datasource to prove data is arriving, and its health status. If
nothing flows yet, you get the structured error card with the exact next
step instead of a dead end. Every datasource row carries a health + flow
badge with a **Re-check flow** button.

This closes the loop the product is built around: the app tells you a
datasource is missing, you add it right there, and you see — in real
time, with real data — whether it fixed the panels.
