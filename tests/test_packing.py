"""Tests for nr2grafana.packing.

No real cluster is required: topology / nodepool / nodeclaim data is either
injected directly or produced by stubbing ``kubectl`` (subprocess.run). The
tests assert the bin-pack constraints, the right-sizing keeps-performance
math, the durability findings, and that the proposed Karpenter config keeps
availability (on-demand ingesters, disruption budgets, no customer values).
"""

import json
import unittest
from unittest import mock

from nr2grafana import packing
from nr2grafana.packing import (
    SCHEMA, GIB, HOURS_MO, PRICE_HR, analyze, bin_pack, classify,
    collect_topology, durability_audit, karpenter_analyze, kubectl_available,
    packing_sim, qty, rightsizing,
)


def _pod(name, stack, comp, cpu, mem_gib, lim_gib=None, zone_group=None,
         anti=None, anti_hard=False, node="n1", ns=None, owner="StatefulSet",
         dnd=False, priority="obs-critical", cpu_lim=0.0, restarts=0,
         last_term=None):
    """A topology pod record (as collect_topology would produce)."""
    return {
        "name": name, "ns": ns or stack, "node": node, "stack": stack,
        "component": comp, "zone_group": zone_group, "anti": anti,
        "anti_hard": anti_hard, "owner": owner, "cpu_req": cpu,
        "mem_req": mem_gib * GIB, "cpu_lim": cpu_lim,
        "mem_lim": (lim_gib or mem_gib) * GIB, "dnd": dnd,
        "priority_class": priority, "node_selector": None,
        "restarts": restarts, "last_term": last_term or [],
    }


def _node(name="n1", itype="m6a.2xlarge", zone="us-east-1a", in_pool=True,
          nodepool="obs", ctype="on-demand"):
    return {
        "in_pool": in_pool, "instance_type": itype, "zone": zone,
        "nodepool": nodepool, "capacity_type": ctype, "arch": "amd64",
        "cpu_alloc": 7.9, "mem_alloc": 30 * GIB, "cpu_cap": 8.0,
        "mem_cap": 32 * GIB, "cpu_req": 0.0, "mem_req": 0.0, "cpu_lim": 0.0,
        "mem_lim": 0.0, "pods": [], "dnd_pods": [], "daemon_cpu_req": 0.4,
        "daemon_mem_req": 1 * GIB, "price_hr": PRICE_HR.get(itype),
        "cost_mo": (PRICE_HR.get(itype) or 0) * HOURS_MO,
        "util_cpu_req": 0.3, "util_mem_req": 0.4,
    }


def _mimir_ingesters(mem_gib=14, lim_gib=16, cpu=1.0):
    """Six Mimir ingesters, two per zone group across three groups."""
    pods = []
    for i, zg in enumerate(("a", "a", "b", "b", "c", "c")):
        pods.append(_pod("mimir-ingester-zone-%s-%d" % (zg, i), "mimir",
                         "ingester", cpu, mem_gib, lim_gib,
                         zone_group="mimir:zone-%s" % zg, node="n1"))
    return pods


def _make_topo(pods=None, nodes=None, pvcs=None, karpenter=None,
               zones=None, mimir_zone_to_az=None):
    nodes = nodes or {"n1": _node()}
    pool_cost = sum(n["cost_mo"] for n in nodes.values() if n["in_pool"])
    return {
        "available": True, "pool_selector": "node-type=observability",
        "namespaces": ["mimir"], "nodes": nodes, "pods": pods or [],
        "pvcs": pvcs or [], "pool_cost_mo": pool_cost,
        "pool_node_count": sum(1 for n in nodes.values() if n["in_pool"]),
        "mimir_zone_to_az": mimir_zone_to_az or {},
        "zones": zones or ["us-east-1a"],
        "karpenter": karpenter or {"nodepools": {}, "nodeclaims": []},
    }


class QtyTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(qty("2"), 2.0)
        self.assertEqual(qty("500m"), 0.5)
        self.assertEqual(qty("2Gi"), 2 * GIB)
        self.assertEqual(qty("256Mi"), 256 * 2 ** 20)
        self.assertEqual(qty(None), 0.0)
        self.assertEqual(qty("garbage"), 0.0)


class ClassifyTest(unittest.TestCase):
    def _pod(self, ns, comp, name):
        return {"metadata": {"namespace": ns, "labels": {
            "app.kubernetes.io/name": name,
            "app.kubernetes.io/component": comp}},
            "spec": {"containers": []}}

    def test_mimir_ingester_zone_group(self):
        p = self._pod("mimir", "ingester", "mimir")
        p["metadata"]["labels"]["rollout-group"] = "zone-a"
        stack, comp, zg, anti, hard = classify(p)
        self.assertEqual(stack, "mimir")
        self.assertEqual(zg, "mimir:zone-a")
        self.assertIsNone(anti)          # same-zone may co-locate
        self.assertFalse(hard)

    def test_loki_ingester_hard_anti(self):
        _, _, zg, _, hard = classify(self._pod("loki", "ingester", "loki"))
        self.assertIsNone(zg)
        self.assertTrue(hard)            # RF=3 one-per-node


class BinPackTest(unittest.TestCase):
    def test_mimir_zone_groups_never_mix(self):
        pods = [
            _pod("a1", "mimir", "ingester", 0.5, 2, zone_group="mimir:a"),
            _pod("a2", "mimir", "ingester", 0.5, 2, zone_group="mimir:a"),
            _pod("b1", "mimir", "ingester", 0.5, 2, zone_group="mimir:b"),
        ]
        bins = bin_pack(pods, "r6a.2xlarge", 0.4, GIB)
        # a1+a2 share a node; b1 cannot join it -> 2 bins.
        self.assertEqual(len(bins), 2)
        for b in bins:
            groups = {g for g in [b["zone_group"]] if g}
            self.assertLessEqual(len(groups), 1)

    def test_loki_ingesters_one_per_node(self):
        pods = [_pod("l%d" % i, "loki", "ingester", 0.2, 1,
                     anti="loki-ingester", anti_hard=True) for i in range(3)]
        bins = bin_pack(pods, "r6a.2xlarge", 0.4, GIB)
        self.assertEqual(len(bins), 3)   # never share

    def test_mem_limit_over_alloc(self):
        # Two 16Gi-limit ingesters on a 32Gi node -> ratio > 1.0 (OOM risk).
        pods = _mimir_ingesters(mem_gib=14, lim_gib=16)[:2]
        bins = bin_pack(pods, "m6a.2xlarge", 0.4, GIB)
        self.assertEqual(len(bins), 1)
        self.assertGreater(bins[0]["mem_limit_over_alloc"], 1.0)
        # Same pods on a 64Gi r-class node are safe.
        rbins = bin_pack(pods, "r6a.2xlarge", 0.4, GIB)
        self.assertLessEqual(rbins[0]["mem_limit_over_alloc"], 1.0)

    def test_pod_too_big_returns_none(self):
        pods = [_pod("huge", "mimir", "ingester", 0.5, 200)]
        self.assertIsNone(bin_pack(pods, "m6a.2xlarge", 0.4, GIB))


class PackingSimTest(unittest.TestCase):
    def test_floor_and_memory_safe_warning(self):
        topo = _make_topo(pods=_mimir_ingesters())
        sim = packing_sim(topo)
        self.assertIsNotNone(sim["floor"])
        # Cheapest raw floor is the m-class (cheaper $/hr); recommended floor
        # is the memory-safe r-class, and a warning explains the swap.
        self.assertTrue(sim["floor"]["shape"].startswith("m"))
        self.assertEqual(sim["recommended_floor"]["instance_category"], "r")
        titles = " ".join(f["title"] for f in sim["findings"])
        self.assertIn("burst-OOM", titles)
        self.assertIn("Bin-pack floor", titles)

    def test_candidates_sorted_by_cost(self):
        sim = packing_sim(_make_topo(pods=_mimir_ingesters()))
        costs = [c["cost_mo"] for c in sim["candidates"]]
        self.assertEqual(costs, sorted(costs))

    def test_overrides_reduce_cpu_do_not_mutate_topo(self):
        topo = _make_topo(pods=_mimir_ingesters(cpu=2.0))
        before = topo["pods"][0]["cpu_req"]
        packing_sim(topo, overrides={"mimir:ingester": {"cpu_req": 1.0}})
        self.assertEqual(topo["pods"][0]["cpu_req"], before)


class RightsizingTest(unittest.TestCase):
    def test_peak_headroom_and_savings(self):
        usage = {"mimir/distributor": {"cpu_peak": 1.0, "mem_peak": 2 * GIB,
                                       "pods": 2}}
        reqs = {"mimir/distributor": {"cpu_req": 6.0, "mem_req": 8 * GIB,
                                      "pods": 2}}
        out = rightsizing(usage, reqs)
        c = out["components"]["mimir/distributor"]
        # peak per pod = 0.5 cores -> suggest 0.5*1.5 = 0.75, never below peak
        self.assertAlmostEqual(c["suggested_cpu_req_per_pod"], 0.75, places=2)
        # mem peak per pod = 1Gi -> suggest 1.3Gi
        self.assertAlmostEqual(c["suggested_mem_req_per_pod_gib"], 1.3,
                               places=2)
        self.assertTrue(c["keeps_performance"])
        self.assertGreater(c["cpu_saved_cores"], 0)
        self.assertGreater(out["totals"]["est_usd_saved_mo"], 0)
        self.assertTrue(out["totals"]["keeps_performance"])

    def test_never_below_peak(self):
        # A workload already near its request should not be cut below peak.
        usage = {"loki/ingester": {"cpu_peak": 2.0, "mem_peak": 4 * GIB,
                                   "pods": 1}}
        reqs = {"loki/ingester": {"cpu_req": 2.5, "mem_req": 4 * GIB,
                                  "pods": 1}}
        out = rightsizing(usage, reqs)
        c = out["components"]["loki/ingester"]
        self.assertGreaterEqual(c["suggested_cpu_req_per_pod"], 2.0)
        self.assertGreaterEqual(c["suggested_mem_req_per_pod_gib"], 4.0)
        self.assertTrue(c["keeps_performance"])

    def test_ingester_cpu_advice_warns_no_cpu_limit(self):
        usage = {"mimir/ingester": {"cpu_peak": 0.4, "mem_peak": 8 * GIB,
                                    "pods": 2}}
        reqs = {"mimir/ingester": {"cpu_req": 4.0, "mem_req": 24 * GIB,
                                   "pods": 2}}
        out = rightsizing(usage, reqs)
        note = " ".join(f["rationale"] for f in out["findings"])
        self.assertIn("NEVER add a CPU limit", note)


class DurabilityTest(unittest.TestCase):
    def test_cpu_limited_mimir_ingester_fails(self):
        pods = [_pod("mi", "mimir", "ingester", 1.0, 8, cpu_lim=2.0,
                     zone_group="mimir:a")]
        finds = durability_audit(_make_topo(pods=pods))
        f = [x for x in finds if "CPU limit" in x["title"]]
        self.assertTrue(f)
        self.assertEqual(f[0]["severity"], "FAIL")
        self.assertFalse(f[0]["keeps_durability"])

    def test_missing_priority_class(self):
        pods = [_pod("li", "loki", "ingester", 1.0, 2, priority=None,
                     anti_hard=True)]
        finds = durability_audit(_make_topo(pods=pods))
        self.assertTrue(any("no priorityClassName" in x["title"]
                            for x in finds))

    def test_az_hosts_two_zones(self):
        topo = _make_topo(
            mimir_zone_to_az={"mimir:a": ["us-east-1a"],
                              "mimir:c": ["us-east-1a"]})
        finds = durability_audit(topo)
        f = [x for x in finds if "hosts 2 Mimir logical zones" in x["title"]]
        self.assertTrue(f)
        self.assertFalse(f[0]["keeps_availability"])

    def test_oom_and_zero_request(self):
        pods = [
            _pod("oomer", "mimir", "querier", 1.0, 4,
                 last_term=["OOMKilled"]),
            _pod("noreq", "loki", "gateway", 0.0, 0),
        ]
        finds = durability_audit(_make_topo(pods=pods))
        self.assertTrue(any("OOMKilled" in x["title"] for x in finds))
        zr = [x for x in finds if "NO resource requests" in x["title"]]
        self.assertTrue(zr)
        self.assertEqual(zr[0]["severity"], "FAIL")

    def test_pvc_zone_span_and_pdb_and_ring(self):
        pvcs = [
            {"ns": "mimir", "name": "data-mimir-ingester-zone-c-0",
             "zone": "us-east-1a", "size": 0, "storageclass": "gp3"},
            {"ns": "mimir", "name": "data-mimir-ingester-zone-c-1",
             "zone": "us-east-1b", "size": 0, "storageclass": "gp3"},
        ]
        topo = _make_topo(pvcs=pvcs)
        topo["pdbs"] = {"mimir/mimir-ingester": {
            "allowed": 0, "current": 2, "desired": 2}}
        finds = durability_audit(topo, ring_health={"ingester": "JOINING"})
        self.assertTrue(any("PVCs span AZs" in x["title"] for x in finds))
        self.assertTrue(any("allows 0 disruptions" in x["title"]
                            for x in finds))
        self.assertTrue(any(x["area"] == "durability"
                            and "not ACTIVE" in x["title"] for x in finds))


class KarpenterTest(unittest.TestCase):
    def _kdata(self, **over):
        req = over.pop("requirements", [
            {"key": "karpenter.k8s.aws/instance-category", "operator": "In",
             "values": ["r"]},
            {"key": "karpenter.sh/capacity-type", "operator": "In",
             "values": ["on-demand"]}])
        spec = {"requirements": req, "expireAfter": "Never",
                "disruption": {"consolidationPolicy": "WhenEmpty"},
                "limits": None, "resources_in_use": None}
        spec.update(over)
        return {"nodepools": {"obs": spec},
                "nodeclaims": [{"name": "nc1", "nodepool": "obs",
                                "type": "r6a.2xlarge", "zone": "us-east-1a",
                                "capacity_type": "on-demand"}]}

    def test_proposed_yaml_keeps_availability(self):
        topo = _make_topo(pods=_mimir_ingesters(),
                          karpenter=self._kdata(),
                          zones=["us-east-1a", "us-east-1b"])
        sim = packing_sim(topo)
        kar = karpenter_analyze(topo, sim)
        y = kar["proposed_nodepool_yaml"]
        # Ingester pool is on-demand only; stateless pool gets spot fallback.
        self.assertIn("on-demand", y)
        self.assertIn('values: ["on-demand"]', y)
        self.assertIn('values: ["spot", "on-demand"]', y)
        # Disruption budgets present and underutilized consolidation.
        self.assertIn("budgets:", y)
        self.assertIn('nodes: "1"', y)
        self.assertIn("WhenEmptyOrUnderutilized", y)
        # Finite expireAfter, not Never; limits present.
        self.assertIn("expireAfter: 720h", y)
        self.assertNotIn("expireAfter: Never", y)
        self.assertIn("limits:", y)
        # r-class recommendation for the memory-bound pool.
        self.assertIn('values: ["r"]', y)
        # Live zones flow into the AZ requirement.
        self.assertIn("us-east-1a", y)
        self.assertIn("us-east-1b", y)
        self.assertTrue(kar["est_savings"]["keeps_availability"])

    def test_no_customer_values_leak(self):
        topo = _make_topo(pods=_mimir_ingesters(), karpenter=self._kdata())
        kar = karpenter_analyze(topo, packing_sim(topo))
        blob = (kar["proposed_nodepool_yaml"]
                + kar["proposed_ec2nodeclass_note"])
        for secret in ("348342704569", "prod-shared-use1", "prod-shared-use1"
                       "-mimir", "prod-shared-use1-loki"):
            self.assertNotIn(secret, blob)
        # Uses placeholders for cluster-specific names.
        self.assertIn("${OBS_EC2NODECLASS}", kar["proposed_nodepool_yaml"])

    def test_spot_ingester_pool_fails(self):
        pods = _mimir_ingesters()
        for p in pods:
            p["node"] = "n1"
        nodes = {"n1": _node(nodepool="obs")}
        kdata = self._kdata(requirements=[
            {"key": "karpenter.sh/capacity-type", "operator": "In",
             "values": ["spot"]}])
        topo = _make_topo(pods=pods, nodes=nodes, karpenter=kdata)
        kar = karpenter_analyze(topo, packing_sim(topo))
        f = [x for x in kar["findings"] if "allows spot" in x["title"]]
        self.assertTrue(f)
        self.assertEqual(f[0]["severity"], "FAIL")
        self.assertFalse(f[0]["keeps_availability"])

    def test_expire_never_and_whenempty_and_no_budgets(self):
        topo = _make_topo(pods=_mimir_ingesters(), karpenter=self._kdata())
        kar = karpenter_analyze(topo, packing_sim(topo))
        titles = " ".join(x["title"] for x in kar["findings"])
        self.assertIn("expireAfter=Never", titles)
        self.assertIn("consolidates WhenEmpty only", titles)
        self.assertIn("no disruption budgets", titles)
        self.assertIn("no cpu/memory limits", titles)

    def test_mclass_on_memory_bound_pool(self):
        kdata = self._kdata(requirements=[
            {"key": "karpenter.k8s.aws/instance-category", "operator": "In",
             "values": ["m"]}])
        topo = _make_topo(pods=_mimir_ingesters(), karpenter=kdata)
        kar = karpenter_analyze(topo, packing_sim(topo))
        self.assertTrue(any("m-class" in x["title"] for x in kar["findings"]))

    def test_empty_zones_use_placeholders(self):
        topo = _make_topo(pods=_mimir_ingesters(), karpenter=self._kdata())
        topo["zones"] = []
        kar = karpenter_analyze(topo, packing_sim(topo))
        self.assertIn("${AZ_A}", kar["proposed_nodepool_yaml"])


class AnalyzeTest(unittest.TestCase):
    def test_injected_topology_full_schema(self):
        pods = _mimir_ingesters()
        pods.append(_pod("mi-cpu", "mimir", "ingester", 1.0, 8, cpu_lim=2.0,
                         zone_group="mimir:a"))
        topo = _make_topo(pods=pods, karpenter={
            "nodepools": {"obs": {"expireAfter": "Never", "disruption": {},
                                  "requirements": []}},
            "nodeclaims": []})
        cfg = {
            "topology": topo,
            "usage": {"mimir/distributor": {"cpu_peak": 1.0,
                                            "mem_peak": 2 * GIB, "pods": 2}},
            "requests": {"mimir/distributor": {"cpu_req": 6.0,
                                               "mem_req": 8 * GIB, "pods": 2}},
        }
        out = analyze(cfg)
        self.assertEqual(out["schema"], SCHEMA)
        self.assertTrue(out["available"])
        for key in ("topology", "packing_sim", "rightsizing", "durability",
                    "karpenter", "findings", "est_savings"):
            self.assertIn(key, out)
        self.assertTrue(out["est_savings"]["keeps_availability"])
        # findings are ranked FAIL -> WARN -> INFO
        sev = [f["severity"] for f in out["findings"]]
        order = {"FAIL": 0, "WARN": 1, "INFO": 2}
        self.assertEqual(sev, sorted(sev, key=lambda s: order[s]))
        # JSON-serializable (artifact-ready).
        json.dumps(out)

    def test_no_cluster_degrades_cleanly(self):
        with mock.patch.object(packing, "kubectl_available",
                               return_value=False):
            out = analyze({})
        self.assertEqual(out["schema"], SCHEMA)
        self.assertFalse(out["available"])
        self.assertIn("note", out)
        self.assertEqual(out["findings"], [])

    def test_disabled_flag_skips_kubectl(self):
        out = analyze({"enabled": False})
        self.assertFalse(out["available"])


# ---------------------------------------------------------------------------
# collect_topology via a stubbed kubectl (no real cluster)
# ---------------------------------------------------------------------------
class _Completed:
    def __init__(self, stdout, rc=0, stderr=""):
        self.stdout = stdout
        self.returncode = rc
        self.stderr = stderr


def _kube_fixture(args):
    """Return canned kubectl JSON keyed off the sub-command."""
    joined = " ".join(args)
    if args[:2] == ["get", "nodes"]:
        return {"items": [{
            "metadata": {"name": "ip-1", "labels": {
                "node-type": "observability",
                "node.kubernetes.io/instance-type": "m6a.2xlarge",
                "topology.kubernetes.io/zone": "us-east-1a",
                "karpenter.sh/nodepool": "obs",
                "karpenter.sh/capacity-type": "on-demand",
                "kubernetes.io/arch": "amd64"}},
            "status": {"capacity": {"cpu": "8", "memory": "32Gi"},
                       "allocatable": {"cpu": "7910m",
                                       "memory": "30Gi"}}}]}
    if args[:2] == ["get", "pods"]:
        return {"items": [
            {"metadata": {"name": "mimir-ingester-zone-a-0", "namespace":
                          "mimir", "labels": {
                              "app.kubernetes.io/name": "mimir",
                              "app.kubernetes.io/component": "ingester",
                              "rollout-group": "zone-a"},
                          "ownerReferences": [{"kind": "StatefulSet"}],
                          "annotations": {
                              "karpenter.sh/do-not-disrupt": "true"}},
             "spec": {"nodeName": "ip-1",
                      "priorityClassName": "obs-critical",
                      "containers": [{"resources": {
                          "requests": {"cpu": "1", "memory": "10Gi"},
                          "limits": {"memory": "12Gi"}}}]},
             "status": {"phase": "Running", "containerStatuses": [
                 {"restartCount": 0}]}},
            {"metadata": {"name": "node-exporter-x", "namespace":
                          "monitoring", "labels": {},
                          "ownerReferences": [{"kind": "DaemonSet"}]},
             "spec": {"nodeName": "ip-1", "containers": [{"resources": {
                 "requests": {"cpu": "100m", "memory": "128Mi"}}}]},
             "status": {"phase": "Running"}}]}
    if args[:2] == ["get", "pv"]:
        return {"items": [{"metadata": {"name": "pv-1"}, "spec": {
            "nodeAffinity": {"required": {"nodeSelectorTerms": [{
                "matchExpressions": [{
                    "key": "topology.kubernetes.io/zone",
                    "values": ["us-east-1a"]}]}]}}}}]}
    if args[:2] == ["get", "pvc"]:
        if args[3] == "mimir":
            return {"items": [{"metadata": {
                "name": "data-mimir-ingester-zone-a-0"},
                "spec": {"volumeName": "pv-1", "storageClassName": "gp3"},
                "status": {"capacity": {"storage": "50Gi"}}}]}
        return {"items": []}
    if "nodepools.karpenter.sh" in joined:
        return {"items": [{"metadata": {"name": "obs"}, "spec": {
            "limits": {"cpu": "48"}, "disruption": {
                "consolidationPolicy": "WhenEmptyOrUnderutilized",
                "budgets": [{"nodes": "1"}]},
            "template": {"spec": {
                "expireAfter": "720h",
                "requirements": [{
                    "key": "karpenter.sh/capacity-type",
                    "operator": "In", "values": ["on-demand"]}]}}},
            "status": {"resources": {"cpu": "24"}}}]}
    if "nodeclaims.karpenter.sh" in joined:
        return {"items": [{"metadata": {"name": "nc1", "labels": {
            "karpenter.sh/nodepool": "obs",
            "node.kubernetes.io/instance-type": "m6a.2xlarge",
            "topology.kubernetes.io/zone": "us-east-1a"}},
            "status": {"conditions": []}}]}
    return {"items": []}


class CollectTopologyTest(unittest.TestCase):
    def test_kubectl_unavailable(self):
        with mock.patch.object(packing, "kubectl_available",
                               return_value=False):
            topo = collect_topology()
        self.assertFalse(topo["available"])
        self.assertIn("kubectl not available", topo["note"])

    def test_stubbed_kubectl_parses_topology(self):
        def fake_run(cmd, **kw):
            # cmd == ["kubectl", *args, "-o", "json"]
            args = cmd[1:-2]
            return _Completed(json.dumps(_kube_fixture(args)))

        with mock.patch.object(packing, "kubectl_available",
                               return_value=True), \
                mock.patch.object(packing.subprocess, "run", fake_run):
            topo = collect_topology(namespaces=["mimir", "monitoring"])

        self.assertTrue(topo["available"])
        self.assertIn("ip-1", topo["nodes"])
        nd = topo["nodes"]["ip-1"]
        # ingester requests counted; daemonset kept separate.
        self.assertAlmostEqual(nd["daemon_cpu_req"], 0.1, places=3)
        self.assertGreater(nd["cpu_req"], 1.0)
        self.assertEqual(len(topo["pods"]), 2)
        self.assertEqual(len(topo["pvcs"]), 1)
        self.assertGreater(topo["pool_cost_mo"], 0)
        self.assertIn("obs", topo["karpenter"]["nodepools"])
        # analyze runs end to end on the collected topology.
        out = analyze({"topology": topo})
        self.assertTrue(out["available"])
        json.dumps(out)

    def test_cluster_unreachable_degrades(self):
        def fail_run(cmd, **kw):
            return _Completed("", rc=1, stderr="no current context")

        with mock.patch.object(packing, "kubectl_available",
                               return_value=True), \
                mock.patch.object(packing.subprocess, "run", fail_run):
            topo = collect_topology()
        self.assertFalse(topo["available"])
        self.assertIn("could not be read", topo["note"])


if __name__ == "__main__":
    unittest.main()
