"""Minimal Grafana HTTP API client (stdlib only).

Used by the interactive wizard to import converted dashboards directly
into a Grafana instance.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class GrafanaError(Exception):
    pass


class GrafanaClient:
    def __init__(self, url: str, token: str = "",
                 basic: Optional[Tuple[str, str]] = None,
                 timeout: int = 30, insecure: bool = False):
        self.base = url.rstrip("/")
        self.timeout = timeout
        self._ctx = ssl._create_unverified_context() if insecure else None
        self.headers = {"Content-Type": "application/json",
                        "Accept": "application/json"}
        if token:
            self.headers["Authorization"] = "Bearer " + token
        elif basic:
            cred = base64.b64encode(
                ("%s:%s" % basic).encode()).decode()
            self.headers["Authorization"] = "Basic " + cred

    def _req(self, method: str, path: str,
             body: Optional[Dict[str, Any]] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._ctx) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:400]
            except Exception:
                pass
            raise GrafanaError("HTTP %d on %s %s: %s"
                               % (e.code, method, path, detail))
        except urllib.error.URLError as e:
            raise GrafanaError("cannot reach %s: %s" % (self.base, e))

    def health(self) -> Dict[str, Any]:
        return self._req("GET", "/api/health")

    def datasources(self) -> List[Dict[str, Any]]:
        return self._req("GET", "/api/datasources")

    def find_or_create_folder(self, title: str) -> str:
        """Returns the folder uid, creating the folder if needed."""
        if not title:
            return ""
        for f in self._req("GET", "/api/folders"):
            if f.get("title") == title:
                return f.get("uid", "")
        created = self._req("POST", "/api/folders", {"title": title})
        return created.get("uid", "")

    def import_dashboard(self, dashboard: Dict[str, Any],
                         folder_uid: str = "", overwrite: bool = False,
                         message: str = "Imported by nr2grafana") \
            -> Dict[str, Any]:
        dash = dict(dashboard)
        dash["id"] = None
        body = {"dashboard": dash, "overwrite": overwrite,
                "folderUid": folder_uid, "message": message}
        return self._req("POST", "/api/dashboards/db", body)
