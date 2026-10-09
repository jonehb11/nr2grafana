"""Live translation hints (SEAM-HINTS): stubbed Grafana + NerdGraph
clients, no network, and proof that nothing mutating is ever sent."""

import json
import re
import unittest
from unittest import mock

from nr2grafana.model import parse_nr_dashboard
from nr2grafana.translate import hints
from nr2grafana.translate.hints import (
    collect_hints, prom_candidates, scan_dashboard, split_env_suffix,
    uniques_nrql)

# Placeholders only (see ARCHITECTURE-1.11 cross-cutting rules).
GUID_A = "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnw5ODc2NTQzMjE"
GUID_B = "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwxMTExMTExMTE"


def widget(nrql, account=123456, title="w"):
    return {"title": title, "visualization": {"id": "viz.line"},
            "layout": {"column": 1, "row": 1, "width": 4, "height": 3},
            "rawConfiguration": {
                "nrqlQueries": [{"accountId": account, "query": nrql}]}}


def dashboard(*nrqls, **kw):
    account = kw.get("account", 123456)
    return {"name": "Acme backend", "pages": [
        {"name": "Main", "widgets": [widget(q, account=account)
                                     for q in nrqls]}]}


NRQLS = [
    "SELECT sum(acme_backend.order.created) FROM Metric "
    "WHERE cluster = concat('p-', {{env}}) TIMESERIES",
    "SELECT average(acme_backend.request.duration) FROM Metric "
    "WHERE k8s.clusterName = 'acme-cluster-prod' FACET env",
    "SELECT average(acme_backend.latency.upper.percentiles) FROM Metric",
    "SELECT latest(acme_backend.queue.depth) FROM Metric "
    "WHERE metricName = 'acme_backend.other.gauge'",
    "SELECT count(*) FROM Transaction WHERE entity.guid = '%s' "
    "AND environment = {{env}}" % GUID_A,
    "SELECT count(*) FROM Transaction WHERE entity.guid IN ('%s', '%s')"
    % (GUID_A, GUID_B),
]


class FakeGrafana:
    """Looks like GrafanaLive from the outside: datasources(),
    prom_metric_names(), prom_label_values(), _proxy_get()."""

    def __init__(self, metadata=None, names=None, label_values=None,
                 datasources=None, metadata_error=None):
        self.metadata = metadata
        self.names = names or []
        self.label_values = label_values or {}
        self._datasources = datasources if datasources is not None else [
            {"uid": "mimir", "type": "prometheus", "isDefault": True},
            {"uid": "loki", "type": "loki"}]
        self.metadata_error = metadata_error
        self.requests = []

    def datasources(self):
        return self._datasources

    def _req(self, method, path, body=None):
        self.requests.append((method, path, body))
        if method != "GET":
            raise AssertionError("hints must only GET, got %s" % method)
        if path.endswith("/api/v1/metadata"):
            if self.metadata_error:
                raise self.metadata_error
            return {"status": "success", "data": self.metadata or {}}
        raise AssertionError("unexpected path %s" % path)

    def _proxy_get(self, uid, path, errors=None):
        try:
            return self._req("GET",
                             "/api/datasources/proxy/uid/%s%s" % (uid, path))
        except Exception as e:  # noqa: BLE001
            if errors is not None:
                errors.append(str(e))
            return None

    def prom_metric_names(self, uid, errors=None):
        self.requests.append(("GET", "names:" + uid, None))
        return list(self.names)

    def prom_label_values(self, uid, label, match="", errors=None):
        self.requests.append(("GET", "label:%s:%s" % (uid, label), None))
        return list(self.label_values.get(label, []))


class FakeNR:
    """NerdGraphClient stand-in recording every GraphQL/NRQL request."""

    def __init__(self, entities=None, uniques=None, fail_nrql=False,
                 account_ids=None):
        self.entities = entities or {}
        self.uniques = uniques or {}
        self.fail_nrql = fail_nrql
        self.account_ids = account_ids
        self.queries = []       # NRQL strings sent through run_nrql
        self.graphql = []       # GraphQL documents sent through _post

    def _post(self, query, variables=None, retries=3):
        self.graphql.append(query)
        if re.search(r"\bmutation\b", query):
            raise AssertionError("mutation sent")
        return {}

    def get_entity(self, guid):
        self.graphql.append("query { actor { entity(guid: $guid) } }")
        ent = self.entities.get(guid)
        if ent is None:
            raise RuntimeError("No entity found for guid %s" % guid)
        return dict(ent)

    def run_nrql(self, account_id, nrql):
        self.queries.append((account_id, nrql))
        if self.fail_nrql:
            raise RuntimeError("NRQL query failed for account %s"
                               % account_id)
        m = re.search(r"uniques\(`([^`]+)`", nrql)
        attr = m.group(1) if m else ""
        vals = self.uniques.get(attr)
        if vals is None:
            return {"results": [], "metadata": {}}
        return {"results": [{"uniques.%s" % attr: list(vals)}],
                "metadata": {}}

    def list_account_ids(self):
        self.graphql.append("{ actor { accounts { id } } }")
        return list(self.account_ids or [])


class ScanTests(unittest.TestCase):
    def test_scan_collects_metrics_guids_attrs_accounts(self):
        scan = scan_dashboard(dashboard(*NRQLS), {})
        self.assertEqual(scan["metrics"], [
            "acme_backend.order.created", "acme_backend.request.duration",
            "acme_backend.latency.upper.percentiles",
            "acme_backend.queue.depth", "acme_backend.other.gauge"])
        self.assertEqual(scan["guids"], [GUID_A, GUID_B])
        self.assertIn(("Metric", "cluster"), scan["attrs"])
        self.assertIn(("Metric", "k8s.clusterName"), scan["attrs"])
        self.assertIn(("Metric", "env"), scan["attrs"])
        self.assertIn(("Transaction", "environment"), scan["attrs"])
        self.assertEqual(scan["account_ids"], [123456])
        self.assertEqual(scan["parse_errors"], 0)

    def test_scan_accepts_model_and_raw_and_wrapped(self):
        raw = dashboard(NRQLS[0])
        model = parse_nr_dashboard(raw)
        for d in (raw, model, {"dashboard": raw}):
            scan = scan_dashboard(d, {})
            self.assertEqual(scan["metrics"],
                             ["acme_backend.order.created"])

    def test_scan_includes_variable_nrql(self):
        raw = dashboard()
        raw["variables"] = [{"name": "env", "type": "NRQL", "nrqlQuery": {
            "accountIds": [777], "query":
            "SELECT uniques(env) FROM Metric WHERE cluster = 'x'"}}]
        scan = scan_dashboard(raw, {})
        self.assertIn(("Metric", "cluster"), scan["attrs"])
        self.assertEqual(scan["account_ids"], [777])

    def test_scan_skips_unparseable_and_counts(self):
        scan = scan_dashboard(dashboard("SELECT FROM WHERE ((("), {})
        self.assertEqual(scan["metrics"], [])
        self.assertEqual(scan["parse_errors"], 1)

    def test_scan_ignores_var_placeholders_as_metric_names(self):
        scan = scan_dashboard(
            dashboard("SELECT average({{metric}}) FROM Metric"), {})
        self.assertEqual(scan["metrics"], [])

    def test_prom_candidates(self):
        cands = prom_candidates("acme_backend.order.created", {})
        self.assertEqual(cands[0], "acme_backend_order_created")
        self.assertIn("acme_backend_order_created_total", cands)
        self.assertIn("acme_backend_order_created_bucket", cands)
        cands = prom_candidates("x.y", {"metric_map": {"x.y": "x_mapped"}})
        self.assertEqual(cands[0], "x_mapped")
        cands = prom_candidates("x.y", {"metric_map": {
            "x.y": {"name": "x_m2", "type": "counter"}}})
        self.assertEqual(cands[0], "x_m2")

    def test_split_env_suffix(self):
        self.assertEqual(split_env_suffix("svc (prod)"), ("svc", "prod"))
        self.assertEqual(split_env_suffix("svc"), ("svc", None))
        self.assertEqual(split_env_suffix("  svc  "), ("svc", None))

    def test_uniques_nrql_is_a_bounded_select(self):
        nrql = uniques_nrql("Metric", "k8s.clusterName", 50, "1 day ago")
        self.assertEqual(
            nrql, "SELECT uniques(`k8s.clusterName`, 50) FROM Metric "
                  "SINCE 1 day ago")


class MimirHintTests(unittest.TestCase):
    def test_metadata_and_existence(self):
        g = FakeGrafana(
            metadata={
                "acme_backend_order_created_total": [{"type": "counter"}],
                "acme_backend_queue_depth": [{"type": "gauge"}],
                "acme_backend_request_duration": [{"type": "histogram"}],
                "mixed": [{"type": "counter"}, {"type": "gauge"}],
                "weird": [{"type": "unknown"}],
            },
            names=["acme_backend_order_created_total",
                   "acme_backend_queue_depth",
                   "acme_backend_request_duration_bucket",
                   "acme_backend_latency_upper_percentiles_sum",
                   "acme_backend_latency_upper_percentiles_count",
                   "up"],
            label_values={"cluster": ["acme-cluster-prod",
                                      "acme-cluster-dev"],
                          "deployment_environment": ["prod", "dev"]})
        h = collect_hints(grafana=g, dash=dashboard(*NRQLS), cfg={})
        mt = h["metric_types"]
        self.assertEqual(mt["acme_backend_order_created_total"], "counter")
        self.assertEqual(mt["acme_backend_queue_depth"], "gauge")
        self.assertEqual(mt["acme_backend_request_duration"], "histogram")
        # conflicting / unknown metadata is not guessed
        self.assertNotIn("mixed", mt)
        self.assertNotIn("weird", mt)
        # existence drives _total-vs-bare
        ex = h["metric_exists"]
        self.assertTrue(ex["acme_backend_order_created_total"])
        self.assertFalse(ex["acme_backend_order_created"])
        self.assertTrue(ex["acme_backend_queue_depth"])
        self.assertFalse(ex["acme_backend_queue_depth_total"])
        # summary family inferred from _sum/_count existence (no metadata)
        self.assertEqual(mt["acme_backend_latency_upper_percentiles"],
                         "summary")
        self.assertEqual(mt["acme_backend_latency_upper_percentiles_sum"],
                         "summary")
        # counter inferred for the bare name when only _total exists
        self.assertEqual(mt["acme_backend_order_created"], "counter")
        # histogram inferred from _bucket (metadata agrees)
        self.assertEqual(
            mt["acme_backend_request_duration_bucket"], "histogram")
        # label values for mapped env/cluster attrs
        self.assertEqual(h["label_values"]["cluster"],
                         ["acme-cluster-prod", "acme-cluster-dev"])
        self.assertEqual(h["label_values"]["deployment_environment"],
                         ["prod", "dev"])
        # the metadata GET went through the datasource proxy of the
        # default prometheus datasource
        paths = [p for (m, p, b) in g.requests]
        self.assertIn(
            "/api/datasources/proxy/uid/mimir/api/v1/metadata", paths)
        self.assertTrue(all(m == "GET" for (m, p, b) in g.requests))

    def test_metadata_unavailable_degrades_with_note(self):
        g = FakeGrafana(metadata_error=RuntimeError("HTTP 404 on GET"),
                        names=["acme_backend_order_created_total"])
        h = collect_hints(grafana=g, dash=dashboard(NRQLS[0]), cfg={})
        self.assertEqual(h["metric_types"],
                         {"acme_backend_order_created_total": "counter",
                          "acme_backend_order_created": "counter"})
        self.assertTrue(h["metric_exists"]
                        ["acme_backend_order_created_total"])
        self.assertTrue(any("metadata unavailable" in n
                            for n in h["notes"]), h["notes"])

    def test_no_prometheus_datasource_notes(self):
        g = FakeGrafana(datasources=[{"uid": "loki", "type": "loki"}])
        h = collect_hints(grafana=g, dash=dashboard(NRQLS[0]), cfg={})
        self.assertEqual(h["metric_types"], {})
        self.assertTrue(any("no prometheus-type datasource" in n
                            for n in h["notes"]), h["notes"])

    def test_pinned_uid_from_config(self):
        g = FakeGrafana(names=["x"])
        cfg = {"datasources": {"prometheus": {"type": "prometheus",
                                              "uid": "mimir-pinned"}}}
        collect_hints(grafana=g, dash=dashboard(NRQLS[0]), cfg=cfg)
        self.assertIn(("GET", "names:mimir-pinned", None), g.requests)
        g2 = FakeGrafana(names=["x"])
        collect_hints(grafana=g2, dash=dashboard(NRQLS[0]),
                      cfg={"hints_prometheus_uid": "explicit"})
        self.assertIn(("GET", "names:explicit", None), g2.requests)

    def test_grafana_failure_never_raises(self):
        class Broken:
            def datasources(self):
                raise RuntimeError("cannot reach grafana")
        h = collect_hints(grafana=Broken(), dash=dashboard(NRQLS[0]))
        self.assertEqual(h["metric_exists"], {})
        self.assertTrue(any("cannot list datasources" in n
                            for n in h["notes"]), h["notes"])

    def test_plain_client_without_helpers_uses_proxy_get(self):
        class Plain:
            def __init__(self):
                self.paths = []

            def datasources(self):
                return [{"uid": "m", "type": "prometheus"}]

            def _req(self, method, path, body=None):
                self.paths.append(path)
                if path.endswith("/api/v1/metadata"):
                    return {"data": {"a_total": [{"type": "counter"}]}}
                if path.endswith("/api/v1/label/__name__/values"):
                    return {"data": ["a_total"]}
                if "/api/v1/label/" in path:
                    return {"data": ["v1"]}
                raise AssertionError(path)
        p = Plain()
        h = collect_hints(grafana=p, dash=dashboard(
            "SELECT sum(a) FROM Metric WHERE env = {{env}}"))
        self.assertEqual(h["metric_types"]["a_total"], "counter")
        self.assertTrue(h["metric_exists"]["a_total"])
        self.assertFalse(h["metric_exists"]["a"])
        self.assertEqual(h["label_values"]["deployment_environment"],
                         ["v1"])
        self.assertIn("/api/datasources/proxy/uid/m/api/v1/metadata",
                      p.paths)

    def test_limits_are_honoured(self):
        g = FakeGrafana(names=["a", "b"], label_values={
            "cluster": [str(i) for i in range(500)]})
        h = collect_hints(grafana=g, dash=dashboard(
            "SELECT sum(a) FROM Metric WHERE cluster = 'c'",
            "SELECT sum(b) FROM Metric"),
            cfg={"hints_max_metrics": 1, "hints_max_values": 3})
        self.assertIn("a", h["metric_exists"])
        self.assertNotIn("b", h["metric_exists"])
        self.assertEqual(h["label_values"]["cluster"], ["0", "1", "2"])
        self.assertTrue(any("hints_max_metrics" in n for n in h["notes"]))


class NerdGraphHintTests(unittest.TestCase):
    def test_entities_resolved_and_env_suffix_split(self):
        nr = FakeNR(entities={
            GUID_A: {"guid": GUID_A, "name": "acme-backend (prod)",
                     "type": "APPLICATION", "domain": "APM"},
            GUID_B: {"guid": GUID_B, "name": "acme-worker",
                     "type": "APPLICATION", "domain": "APM"}})
        h = collect_hints(nr=nr, dash=dashboard(NRQLS[4], NRQLS[5]))
        self.assertEqual(h["entities"][GUID_A], {
            "name": "acme-backend (prod)", "type": "APPLICATION",
            "service_label": "acme-backend", "env": "prod",
            "domain": "APM"})
        self.assertEqual(h["entities"][GUID_B]["service_label"],
                         "acme-worker")
        self.assertNotIn("env", h["entities"][GUID_B])

    def test_unknown_entity_is_a_note_not_an_error(self):
        nr = FakeNR(entities={})
        h = collect_hints(nr=nr, dash=dashboard(NRQLS[4]))
        self.assertEqual(h["entities"], {})
        self.assertTrue(any("unresolved" in n for n in h["notes"]),
                        h["notes"])

    def test_attr_values_via_read_only_uniques(self):
        nr = FakeNR(uniques={"cluster": ["acme-cluster-prod",
                                         "acme-cluster-dev"],
                             "env": ["prod", "dev"],
                             "k8s.clusterName": ["acme-cluster-prod"],
                             "environment": ["prod"]})
        h = collect_hints(nr=nr, dash=dashboard(*NRQLS),
                          cfg={"hints_max_values": 50,
                               "hints_since": "2 hours ago"})
        self.assertEqual(h["attr_values"]["cluster"],
                         ["acme-cluster-prod", "acme-cluster-dev"])
        self.assertEqual(h["attr_values"]["env"], ["prod", "dev"])
        self.assertEqual(h["attr_values"]["environment"], ["prod"])
        # every NRQL sent is a bounded, time-boxed SELECT uniques()
        self.assertTrue(nr.queries)
        for aid, q in nr.queries:
            self.assertEqual(aid, 123456)
            self.assertTrue(q.startswith("SELECT uniques("), q)
            self.assertIn(", 50)", q)
            self.assertIn("SINCE 2 hours ago", q)
        self.assertIn((123456, "SELECT uniques(`k8s.clusterName`, 50) "
                               "FROM Metric SINCE 2 hours ago"),
                      nr.queries)

    def test_attr_values_each_attr_queried_once(self):
        nr = FakeNR(uniques={"env": ["prod"]})
        d = dashboard("SELECT count(*) FROM Transaction WHERE env = 'p'",
                      "SELECT count(*) FROM Transaction WHERE env = 'q'")
        collect_hints(nr=nr, dash=d)
        self.assertEqual(len(nr.queries), 1)

    def test_account_fallbacks(self):
        d = dashboard("SELECT count(*) FROM Transaction WHERE env = 'p'",
                      account=None)
        nr = FakeNR(uniques={"env": ["prod"]}, account_ids=[4242])
        h = collect_hints(nr=nr, dash=d)
        self.assertEqual(nr.queries[0][0], 4242)
        self.assertEqual(h["attr_values"]["env"], ["prod"])
        nr = FakeNR(uniques={"env": ["prod"]})
        h = collect_hints(nr=nr, dash=d, cfg={"account_id": "99"})
        self.assertEqual(nr.queries[0][0], 99)
        nr = FakeNR(uniques={"env": ["prod"]}, account_ids=[])
        h = collect_hints(nr=nr, dash=d)
        self.assertEqual(h["attr_values"], {})
        self.assertTrue(any("no account id known" in n
                            for n in h["notes"]), h["notes"])

    def test_nrql_failure_degrades(self):
        nr = FakeNR(fail_nrql=True)
        h = collect_hints(nr=nr, dash=dashboard(NRQLS[0]))
        self.assertEqual(h["attr_values"], {})
        self.assertTrue(any("no values for cluster" in n
                            for n in h["notes"]), h["notes"])

    def test_entity_limit(self):
        nr = FakeNR(entities={
            GUID_A: {"name": "a", "type": "APPLICATION"},
            GUID_B: {"name": "b", "type": "APPLICATION"}})
        h = collect_hints(nr=nr, dash=dashboard(NRQLS[5]),
                          cfg={"hints_max_entities": 1})
        self.assertEqual(list(h["entities"]), [GUID_A])
        self.assertTrue(any("hints_max_entities" in n for n in h["notes"]))

    def test_no_clients_gives_notes_only(self):
        h = collect_hints(dash=dashboard(*NRQLS))
        self.assertEqual(h["metric_types"], {})
        self.assertEqual(h["entities"], {})
        self.assertEqual(h["attr_values"], {})
        joined = " ".join(h["notes"])
        self.assertIn("no Grafana connection", joined)
        self.assertIn("entity guid(s) unresolved", joined)
        self.assertIn("attribute values not discovered", joined)

    def test_none_dashboard_is_fine(self):
        h = collect_hints(nr=FakeNR(), grafana=FakeGrafana(), dash=None)
        self.assertEqual(h["metric_exists"], {})
        self.assertEqual(h["notes"], [])

    def test_real_client_methods_exist_and_send_no_mutation(self):
        """Drive collect_hints through the real NerdGraphClient with
        _post stubbed, and assert every GraphQL document is a query."""
        from nr2grafana.nerdgraph import NerdGraphClient
        client = NerdGraphClient("NRAK-TEST")
        sent = []

        def fake_post(query, variables=None, retries=3):
            sent.append((query, variables))
            if "entity(guid" in query:
                return {"actor": {"entity": {
                    "guid": variables["guid"], "name": "acme-backend (prod)",
                    "type": "APPLICATION", "domain": "APM"}}}
            if "nrql(" in query:
                return {"actor": {"account": {"nrql": {
                    "results": [{"uniques.environment": ["prod"]}],
                    "metadata": {}}}}}
            return {}

        with mock.patch.object(client, "_post", side_effect=fake_post):
            h = collect_hints(nr=client, dash=dashboard(NRQLS[4]))
        self.assertEqual(h["entities"][GUID_A]["service_label"],
                         "acme-backend")
        self.assertEqual(h["attr_values"]["environment"], ["prod"])
        self.assertTrue(sent)
        for query, variables in sent:
            self.assertIsNone(re.search(r"\bmutation\b", query), query)
            self.assertNotIn("mutation", json.dumps(variables or {}))
            nrql = (variables or {}).get("q")
            if nrql:
                self.assertTrue(nrql.upper().startswith("SELECT "), nrql)

    def test_log_callback_receives_progress(self):
        lines = []
        collect_hints(nr=FakeNR(uniques={"cluster": ["c"]}),
                      grafana=FakeGrafana(names=["x"]),
                      dash=dashboard(NRQLS[0]), log=lines.append)
        self.assertTrue(any(line.startswith("hints:") for line in lines))


class ModuleHygieneTests(unittest.TestCase):
    def test_module_never_spells_a_mutation(self):
        import inspect
        src = inspect.getsource(hints)
        self.assertNotIn("mutation", src.replace("mutations outright", ""))
        self.assertNotIn("POST", src)


if __name__ == "__main__":
    unittest.main()
