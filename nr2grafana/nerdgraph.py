"""NerdGraph client for bulk-exporting New Relic dashboards.

Stdlib-only (urllib). Auth: a New Relic USER API key (NRAK-...), passed via
--api-key or the NEW_RELIC_API_KEY environment variable.
"""

from __future__ import annotations

import json
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


class NerdGraphError(Exception):
    pass


class NerdGraphClient:
    def __init__(self, api_key: str, region: str = "US", timeout: int = 60,
                 insecure: bool = False):
        region = region.upper()
        if region not in ENDPOINTS:
            raise NerdGraphError("region must be US or EU, got %r" % region)
        self.endpoint = ENDPOINTS[region]
        self.api_key = api_key
        self.timeout = timeout
        self._ctx = ssl._create_unverified_context() if insecure else None

    def _post(self, query: str, variables: Optional[Dict[str, Any]] = None,
              retries: int = 3) -> Dict[str, Any]:
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
