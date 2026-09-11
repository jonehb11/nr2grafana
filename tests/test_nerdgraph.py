"""NerdGraph client guarantees, most importantly: strictly read-only."""

import unittest

from nr2grafana.nerdgraph import NerdGraphClient, NerdGraphError


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
        for name in ("LIST_QUERY", "GET_QUERY", "NRQL_QUERY"):
            block = getattr(ng, name, "")
            if isinstance(block, str):
                self.assertNotIn("mutation", block)
        self.assertIn("read-only", src)


if __name__ == "__main__":
    unittest.main()
