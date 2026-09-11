"""NerdGraph client for bulk-exporting New Relic dashboards.

Stdlib-only (urllib). Auth: a New Relic USER API key (NRAK-...), passed via
--api-key or the NEW_RELIC_API_KEY environment variable.
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

ENDPOINTS = {
    "US": "https://api.newrelic.com/graphql",
    "EU": "https://api.eu.newrelic.com/graphql",
}

_LIST_QUERY = """
query($cursor: String) {
  actor {
    entitySearch(query: "type = 'DASHBOARD'") {
      results(cursor: $cursor) {
        entities {
          guid
          name
          accountId
          ... on DashboardEntityOutline {
            dashboardParentGuid
          }
        }
        nextCursor
      }
    }
  }
}
"""

_GET_QUERY = """
query($guid: EntityGuid!) {
  actor {
    entity(guid: $guid) {
      ... on DashboardEntity {
        guid
        name
        description
        permissions
        pages {
          guid
          name
          description
          widgets {
            id
            title
            visualization { id }
            layout { column row width height }
            rawConfiguration
            linkedEntities { guid }
          }
        }
        variables {
          name
          title
          type
          defaultValues { value { string } }
          isMultiSelection
          replacementStrategy
          items { title value }
          nrqlQuery { accountIds query }
          options { excluded ignoreTimeRange showApplyAction hiddenOnVariablesBar }
        }
      }
    }
  }
}
"""


_NRQL_QUERY = """
query($id: Int!, $q: Nrql!) {
  actor {
    account(id: $id) {
      nrql(query: $q, timeout: 30) {
        results
        metadata {
          facets
          timeWindow { begin end }
        }
      }
    }
  }
}
"""


class NerdGraphError(Exception):
    pass


class NerdGraphClient:
    def __init__(self, api_key: str, region: str = "US", timeout: int = 60,
                 insecure: bool = False):
        region = region.upper()
        if region not in ENDPOINTS:
            raise NerdGraphError("region must be US or EU, got %r" % region)
        # N2G_NERDGRAPH_URL overrides the region endpoint -- lets the CLI,
        # wizard and web UI run against tools/mock_stack.py (offline demo)
        # or a corporate NerdGraph proxy without code changes.
        import os
        self.endpoint = (os.environ.get("N2G_NERDGRAPH_URL")
                         or ENDPOINTS[region])
        self.api_key = api_key
        self.timeout = timeout
        self._ctx = ssl._create_unverified_context() if insecure else None

    def _post(self, query: str, variables: Optional[Dict[str, Any]] = None,
              retries: int = 3) -> Dict[str, Any]:
        # Hard read-only guarantee: nr2grafana never modifies anything in
        # New Relic. Every request is a GraphQL query; a mutation reaching
        # this client is a bug, and we refuse to send it.
        if re.search(r"\bmutation\b", query):
            raise NerdGraphError(
                "refusing to send a GraphQL mutation: nr2grafana is "
                "strictly read-only against New Relic")
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            req = urllib.request.Request(
                self.endpoint, data=payload,
                headers={
                    "Content-Type": "application/json",
                    "API-Key": self.api_key,
                    "User-Agent": "nr2grafana/1.0",
                })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout,
                                            context=self._ctx) as resp:
                    body = json.loads(resp.read().decode())
                if body.get("errors"):
                    raise NerdGraphError(
                        "NerdGraph errors: %s" % json.dumps(body["errors"], indent=2))
                return body.get("data", {})
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode()[:500]
                except Exception:
                    pass
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    last_err = e
                    continue
                if e.code in (401, 403):
                    raise NerdGraphError(
                        "Authentication failed (HTTP %d). Check that your key is a "
                        "USER key (NRAK-...) with access to the account, and that "
                        "the --region (US/EU) matches. %s" % (e.code, detail))
                raise NerdGraphError("HTTP %d from NerdGraph: %s" % (e.code, detail))
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    last_err = e
                    continue
                raise NerdGraphError("Network error reaching %s: %s" % (self.endpoint, e))
        raise NerdGraphError("giving up after retries: %s" % last_err)

    def list_dashboards(self) -> List[Dict[str, Any]]:
        """List all dashboards visible to the key.

        Returns entity dicts with guid/name/accountId/dashboardParentGuid.
        Multi-page dashboards surface each page as its own DASHBOARD entity
        with dashboardParentGuid set; we keep only top-level dashboards
        (parent guid is None) since exporting the parent includes all pages.
        """
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            data = self._post(_LIST_QUERY, {"cursor": cursor})
            results = (((data.get("actor") or {}).get("entitySearch") or {})
                       .get("results") or {})
            for ent in results.get("entities") or []:
                if not ent.get("dashboardParentGuid"):
                    out.append(ent)
            cursor = results.get("nextCursor")
            if not cursor:
                break
        return out

    def get_dashboard(self, guid: str) -> Dict[str, Any]:
        data = self._post(_GET_QUERY, {"guid": guid})
        entity = (data.get("actor") or {}).get("entity")
        if not entity:
            raise NerdGraphError(
                "No dashboard found for guid %s (or key lacks access)" % guid)
        return entity

    def run_nrql(self, account_id: int, nrql: str) -> Dict[str, Any]:
        """Run a read-only NRQL query and return its raw results.

        Returns ``{"results": [...], "metadata": {...}}`` where metadata
        carries ``facets`` and ``timeWindow`` when NerdGraph provides
        them. Raises :class:`NerdGraphError` with actionable text on
        GraphQL errors (bad key, inaccessible account, NRQL syntax).
        """
        try:
            data = self._post(_NRQL_QUERY,
                              {"id": int(account_id), "q": nrql})
        except NerdGraphError as e:
            msg = str(e)
            low = msg.lower()
            hint = ""
            if "syntax" in low or ("nrql" in low and "error" in low):
                hint = " Check the NRQL syntax of: %s" % nrql[:200]
            elif ("not found" in low or "denied" in low
                    or "access" in low or "authoriz" in low):
                hint = (" Check that the API key can access account %s."
                        % account_id)
            raise NerdGraphError(
                "NRQL query failed for account %s: %s%s"
                % (account_id, msg, hint))
        account = ((data.get("actor") or {}).get("account")) or {}
        payload = account.get("nrql")
        if payload is None:
            raise NerdGraphError(
                "NerdGraph returned no NRQL result for account %s. Check "
                "that the account id is correct and the key (NRAK-...) "
                "has access to it." % account_id)
        return {"results": payload.get("results") or [],
                "metadata": payload.get("metadata") or {}}

    def list_account_ids(self) -> List[int]:
        """Account ids visible to this key (read-only actor query).

        Used as a last-resort fallback when neither the caller nor the
        widget report knows which account to run NRQL against.
        """
        data = self._post("{ actor { accounts { id } } }")
        out: List[int] = []
        for acct in ((data.get("actor") or {}).get("accounts")) or []:
            try:
                out.append(int(acct.get("id")))
            except (TypeError, ValueError):
                pass
        return sorted(set(out))

    def export_all(self, log=lambda msg: print(msg, file=sys.stderr)) \
            -> Iterable[Dict[str, Any]]:
        dashboards = self.list_dashboards()
        log("Found %d dashboards" % len(dashboards))
        for i, ent in enumerate(dashboards, 1):
            log("[%d/%d] exporting %s (%s)" % (i, len(dashboards),
                                               ent.get("name"), ent.get("guid")))
            try:
                yield self.get_dashboard(ent["guid"])
            except NerdGraphError as e:
                log("  ! failed: %s" % e)
