"""Persistent local storage (sqlite3).

Keeps converted dashboards, per-dashboard artifacts (requirements,
widget reports, data-test results, live-check results), run history, a
change log, and non-secret settings in a single sqlite database at
``~/.nr2grafana/nr2grafana.db``.

Design notes:

- WAL journal mode so the web server's request threads and the CLI can
  read while a write is in flight.
- Thread safety: one shared connection guarded by a per-instance lock
  (``check_same_thread=False``); every public method takes the lock.
- Schema versioned via ``PRAGMA user_version`` with an ordered migration
  list (`_MIGRATIONS`) so future versions can evolve the schema in
  place.
- JSON payloads are stored as TEXT columns; timestamps are ISO-8601 UTC
  strings (``2026-01-01T00:00:00Z``).
- Secrets (API keys, tokens, passwords) must NEVER be stored here.
  ``set_setting`` actively refuses secret-looking keys.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DEFAULT_DIR = os.path.expanduser("~/.nr2grafana")
DEFAULT_DB = os.path.join(DEFAULT_DIR, "nr2grafana.db")

# Documented artifact kinds. save_artifact accepts other kinds too (the
# schema does not care), but these get "has_*" flags in
# list_dashboards().
ARTIFACT_KINDS = ("requirements", "widget-report", "datatest", "check",
                  "parity", "diagnosis", "heal", "samples", "review")

CHANGE_SOURCES = ("user", "ai", "auto")

# Setting keys that look like credentials are refused: secrets live in
# process memory / environment only, never on disk.
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[-_]?key|apikey|credential)",
    re.IGNORECASE)


def _utcnow() -> str:
    """Current time as an ISO-8601 UTC string (second precision)."""
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads(text: Optional[str], default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _migration_v1(conn: sqlite3.Connection) -> None:
    """Initial schema."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'running',
            summary TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS dashboards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            nr_guid TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            kind TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE (slug, kind)
        );
        CREATE TABLE IF NOT EXISTS changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '',
            before TEXT,
            after TEXT,
            why TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'user',
            ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_changes_slug ON changes (slug);
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)


# Ordered (target_version, migration_fn). To evolve the schema, append
# (2, _migration_v2) etc.; migrations run once, in order, on open.
_MIGRATIONS = [
    (1, _migration_v1),
]

SCHEMA_VERSION = _MIGRATIONS[-1][0]


class StoreError(Exception):
    """Raised for invalid store usage (e.g. storing a secret)."""


class Store:
    """sqlite3-backed local store. Usable as a context manager::

        with Store() as store:
            store.set_setting("grafana_url", "http://localhost:3000")

    ``path`` defaults to ``~/.nr2grafana/nr2grafana.db`` (directory
    created with mode 0700). Pass an explicit path for tests.
    """

    def __init__(self, path: str = "") -> None:
        if not path:
            if not os.path.isdir(DEFAULT_DIR):
                os.makedirs(DEFAULT_DIR, mode=0o700, exist_ok=True)
            path = DEFAULT_DB
        else:
            parent = os.path.dirname(os.path.abspath(path))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, mode=0o700, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- lifecycle --------------------------------------------------

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.execute("PRAGMA user_version")
            version = cur.fetchone()[0]
            for target, fn in _MIGRATIONS:
                if version < target:
                    fn(self._conn)
                    self._conn.execute(
                        "PRAGMA user_version = %d" % target)
                    version = target
            self._conn.commit()

    # -- runs -------------------------------------------------------

    def record_run(self, kind: str, meta: Dict[str, Any]) -> int:
        """Start a run (fetch/convert/test/...); returns its id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (kind, meta, status, started_at)"
                " VALUES (?, ?, 'running', ?)",
                (kind, _dumps(meta or {}), _utcnow()))
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str,
                   summary: Dict[str, Any]) -> None:
        """Mark a run finished with a status ("ok"/"error"/...)."""
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, summary = ?,"
                " finished_at = ? WHERE id = ?",
                (status, _dumps(summary or {}), _utcnow(), run_id))
            self._conn.commit()

    def list_runs(self, kind: str = "", limit: int = 50) -> List[Dict]:
        """Most-recent-first run history, optionally filtered by kind."""
        sql = ("SELECT id, kind, meta, status, summary, started_at,"
               " finished_at FROM runs")
        args: List[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "kind": r["kind"],
                "meta": _loads(r["meta"], {}),
                "status": r["status"],
                "summary": _loads(r["summary"], {}),
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
            })
        return out

    # -- dashboards -------------------------------------------------

    def upsert_dashboard(self, slug: str, title: str, source: str,
                         nr_guid: str, data: Dict[str, Any]) -> int:
        """Insert or update a converted dashboard; returns its row id."""
        if not slug:
            raise StoreError("dashboard slug must not be empty")
        now = _utcnow()
        blob = _dumps(data or {})
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM dashboards WHERE slug = ?",
                (slug,)).fetchone()
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO dashboards (slug, title, source,"
                    " nr_guid, data, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (slug, title or "", source or "", nr_guid or "",
                     blob, now, now))
                dash_id = int(cur.lastrowid)
            else:
                dash_id = int(row["id"])
                self._conn.execute(
                    "UPDATE dashboards SET title = ?, source = ?,"
                    " nr_guid = ?, data = ?, updated_at = ?"
                    " WHERE id = ?",
                    (title or "", source or "", nr_guid or "", blob,
                     now, dash_id))
            self._conn.commit()
            return dash_id

    def get_dashboard(self, slug: str) -> Optional[Dict[str, Any]]:
        """Full dashboard row including the decoded json blob."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM dashboards WHERE slug = ?",
                (slug,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            "source": row["source"],
            "nr_guid": row["nr_guid"],
            "data": _loads(row["data"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_dashboards(self) -> List[Dict[str, Any]]:
        """Metadata rows (no json blob), plus has_* artifact flags."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT slug, title, source, nr_guid, created_at,"
                " updated_at FROM dashboards ORDER BY slug").fetchall()
            arts = self._conn.execute(
                "SELECT slug, kind FROM artifacts").fetchall()
        kinds_by_slug: Dict[str, set] = {}
        for a in arts:
            kinds_by_slug.setdefault(a["slug"], set()).add(a["kind"])
        out = []
        for r in rows:
            have = kinds_by_slug.get(r["slug"], set())
            entry = {
                "slug": r["slug"],
                "title": r["title"],
                "source": r["source"],
                "nr_guid": r["nr_guid"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for kind in ARTIFACT_KINDS:
                entry["has_" + kind.replace("-", "_")] = kind in have
            out.append(entry)
        return out

    # -- artifacts --------------------------------------------------

    def save_artifact(self, slug: str, kind: str,
                      data: Dict[str, Any]) -> None:
        """Save/replace an artifact (kind: "requirements" |
        "widget-report" | "datatest" | "check")."""
        if not slug or not kind:
            raise StoreError("artifact slug and kind must not be empty")
        now = _utcnow()
        blob = _dumps(data or {})
        with self._lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET data = ?, updated_at = ?"
                " WHERE slug = ? AND kind = ?",
                (blob, now, slug, kind))
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO artifacts (slug, kind, data,"
                    " updated_at) VALUES (?, ?, ?, ?)",
                    (slug, kind, blob, now))
            self._conn.commit()

    def get_artifact(self, slug: str,
                     kind: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM artifacts WHERE slug = ?"
                " AND kind = ?", (slug, kind)).fetchone()
        if row is None:
            return None
        return _loads(row["data"], {})

    # -- change log -------------------------------------------------

    def log_change(self, slug: str, change: Dict[str, Any]) -> int:
        """Append a change entry. ``change`` keys: action, target,
        before, after, why, source ("user"|"ai"|"auto"). The store
        adds "ts" (ISO-8601 UTC) and "id". Returns the change id."""
        change = dict(change or {})
        source = change.get("source") or "user"
        if source not in CHANGE_SOURCES:
            raise StoreError(
                "change source must be one of %s, got %r"
                % ("/".join(CHANGE_SOURCES), source))
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO changes (slug, action, target, before,"
                " after, why, source, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (slug,
                 change.get("action") or "",
                 change.get("target") or "",
                 _dumps(change.get("before")),
                 _dumps(change.get("after")),
                 change.get("why") or "",
                 source,
                 _utcnow()))
            self._conn.commit()
            return int(cur.lastrowid)

    def list_changes(self, slug: str = "") -> List[Dict[str, Any]]:
        """Changes in insertion order; all dashboards when slug is
        empty."""
        sql = ("SELECT id, slug, action, target, before, after, why,"
               " source, ts FROM changes")
        args: List[Any] = []
        if slug:
            sql += " WHERE slug = ?"
            args.append(slug)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "slug": r["slug"],
                "action": r["action"],
                "target": r["target"],
                "before": _loads(r["before"]),
                "after": _loads(r["after"]),
                "why": r["why"],
                "source": r["source"],
                "ts": r["ts"],
            })
        return out

    # -- settings ---------------------------------------------------

    def set_setting(self, key: str, value: Any) -> None:
        """Persist a non-secret setting (JSON-encoded). Keys that look
        like credentials (token/secret/password/api key/...) are
        refused: secrets never touch disk."""
        if not key:
            raise StoreError("setting key must not be empty")
        if _SECRET_KEY_RE.search(key):
            raise StoreError(
                "refusing to persist secret-looking setting %r:"
                " keys and tokens must stay in process memory or"
                " environment variables" % key)
        now = _utcnow()
        blob = _dumps(value)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE settings SET value = ?, updated_at = ?"
                " WHERE key = ?", (blob, now, key))
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO settings (key, value, updated_at)"
                    " VALUES (?, ?, ?)", (key, blob, now))
            self._conn.commit()

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,)).fetchone()
        if row is None:
            return default
        return _loads(row["value"], default)
