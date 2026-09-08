"""Localhost web UI for nr2grafana.

Exports :func:`serve`, which starts a ThreadingHTTPServer bound to
localhost and (optionally) opens the browser on the single-page app.
"""

from __future__ import annotations

from .server import serve

__all__ = ["serve"]
