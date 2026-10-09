"""NerdGraph client guarantees, most importantly: strictly read-only."""

import unittest
from unittest import mock

from nr2grafana.nerdgraph import NerdGraphClient, NerdGraphError

# Placeholder GUID (base64-like, no real account/entity ids).
GUID = "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnw5ODc2NTQzMjE"


class ReadOnlyGuardTests(unittest.TestCase):
    def setUp(self):
        self.client = NerdGraphClient("NRAK-TEST")

    def test_mutation_refused_before_any_network_io(self):
        # The guard must trip before urlopen: an invalid endpoint would
        # otherwise produce a network error instead of the guard message.
        with self.assertRaises(NerdGraphError) as ctx:
            self.client._post("mutation { agentApplicationDelete }")
        self.assertIn("read-only", str(ctx.exception))

    def test_mutation_keyword_anywhere_refused(self):
        with self.assertRaises(NerdGraphError) as ctx:
            self.client._post(
                "query { x }  mutation Evil { dashboardDelete }")
        self.assertIn("refusing", str(ctx.exception))

    def test_shipped_queries_contain_no_mutation(self):
        import inspect
        import nr2grafana.nerdgraph as ng
        src = inspect.getsource(ng)
        # The word may only appear in the guard/comment lines, never in a
        # GraphQL payload; assert none of the module's triple-quoted
        # GraphQL blocks contain it.
        for name in ("_LIST_QUERY", "_GET_QUERY", "_NRQL_QUERY",
                     "_ENTITY_QUERY"):
            block = getattr(ng, name)
            self.assertIsInstance(block, str)
            if isinstance(block, str):
                self.assertNotIn("mutation", block)
        self.assertIn("read-only", src)


class EntityLookupTests(unittest.TestCase):
    """get_entity(): read-only `actor { entity(guid) }` resolution used
    by the live translation hints (entity.guid = '...' -> service)."""

    def setUp(self):
        self.client = NerdGraphClient("NRAK-TEST")

    def test_get_entity_returns_name_and_type(self):
        sent = []

        def fake_post(query, variables=None, retries=3):
            sent.append((query, variables))
            return {"actor": {"entity": {
                "guid": variables["guid"], "name": "acme-backend (prod)",
                "type": "APPLICATION", "domain": "APM",
                "entityType": "APM_APPLICATION_ENTITY"}}}

        with mock.patch.object(self.client, "_post",
                               side_effect=fake_post):
            ent = self.client.get_entity(GUID)
        self.assertEqual(ent["name"], "acme-backend (prod)")
        self.assertEqual(ent["type"], "APPLICATION")
        self.assertEqual(len(sent), 1)
        query, variables = sent[0]
        self.assertEqual(variables, {"guid": GUID})
        self.assertIn("entity(guid: $guid)", query)
        self.assertNotIn("mutation", query)
        self.assertTrue(query.lstrip().startswith("query"))

    def test_get_entity_unknown_guid_is_actionable(self):
        with mock.patch.object(self.client, "_post",
                               return_value={"actor": {"entity": None}}):
            with self.assertRaises(NerdGraphError) as ctx:
                self.client.get_entity(GUID)
        self.assertIn("No entity found", str(ctx.exception))
        self.assertIn("lacks access", str(ctx.exception))

    def test_get_entity_rejects_malformed_guid_before_io(self):
        with mock.patch.object(self.client, "_post") as post:
            for bad in ("", "   ", "not a guid!", "a'b"):
                with self.assertRaises(NerdGraphError) as ctx:
                    self.client.get_entity(bad)
                self.assertIn("not a valid New Relic entity GUID",
                              str(ctx.exception))
            post.assert_not_called()

    def test_get_entity_goes_through_the_mutation_guard(self):
        # The guard lives in _post; get_entity must call it (not urlopen
        # directly), so a tampered query document would still be refused.
        import nr2grafana.nerdgraph as ng
        with mock.patch.object(ng, "_ENTITY_QUERY",
                               "mutation { entityDelete }"):
            with self.assertRaises(NerdGraphError) as ctx:
                self.client.get_entity(GUID)
        self.assertIn("read-only", str(ctx.exception))


class RunNrqlTests(unittest.TestCase):
    """run_nrql() shape the hints module relies on for uniques()."""

    def test_run_nrql_returns_results_and_metadata(self):
        client = NerdGraphClient("NRAK-TEST")
        sent = []

        def fake_post(query, variables=None, retries=3):
            sent.append((query, variables))
            return {"actor": {"account": {"nrql": {
                "results": [{"uniques.env": ["prod", "dev"]}],
                "metadata": {"facets": None}}}}}

        with mock.patch.object(client, "_post", side_effect=fake_post):
            got = client.run_nrql("123456",
                                  "SELECT uniques(env, 10) FROM Metric")
        self.assertEqual(got["results"][0]["uniques.env"], ["prod", "dev"])
        self.assertEqual(sent[0][1]["id"], 123456)
        self.assertNotIn("mutation", sent[0][0])


if __name__ == "__main__":
    unittest.main()
