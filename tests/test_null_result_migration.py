"""Startup repair for legacy resolved merges whose result was SQL NULL.

An older build persisted a conflict-free merge whose final document was the
JSON root value null as ``status='resolved'`` with ``result IS NULL`` (it
used SQL NULL for both "no result yet" and the JSON value null). On startup
the service repairs those rows from the saved ``document`` so the patch and
evidence reads stop misreporting them as pending (409). Pending rows, whose
SQL NULL genuinely means "no result produced yet", are left untouched. The
repair is idempotent across restarts.
"""

import json as _json
import os
import sqlite3
import tempfile

os.environ.setdefault(
    "DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "test_null_result_migration.db")
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, store  # noqa: E402
from app.main import app  # noqa: E402


def _insert_legacy_row(conn, merge_id, status, base, document, conflicts, result):
    """Insert a row exactly as the old schema/build would have stored it."""
    conn.execute(
        """
        INSERT INTO merges (
            id, idempotency_key, status, revision,
            base_doc, left_doc, right_doc, document, conflicts,
            source_conflicts, initial_document, result, result_schema,
            created_at, updated_at
        ) VALUES (?, NULL, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 't', 't')
        """,
        (
            merge_id,
            status,
            _json.dumps(base),
            _json.dumps(base),
            _json.dumps(base),
            _json.dumps(document),
            _json.dumps(conflicts),
            _json.dumps(conflicts),
            _json.dumps(document),
            result,
        ),
    )


def _write_legacy_database(path):
    """Create a database file in the exact pre-repair state."""
    bare = sqlite3.connect(path)
    # Minimal table shape predating the repair (column presence is all the
    # startup ALTERs care about).
    bare.executescript(
        """
        CREATE TABLE merges (
            id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
            status TEXT NOT NULL, revision INTEGER NOT NULL,
            base_doc TEXT NOT NULL, left_doc TEXT NOT NULL, right_doc TEXT NOT NULL,
            document TEXT NOT NULL, conflicts TEXT NOT NULL,
            source_conflicts TEXT, initial_document TEXT,
            result TEXT, result_schema TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE merge_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merge_id TEXT NOT NULL, seq INTEGER NOT NULL, method TEXT NOT NULL,
            revision_before INTEGER NOT NULL, revision_after INTEGER NOT NULL,
            pointers TEXT NOT NULL, detail TEXT NOT NULL,
            prev_hash TEXT NOT NULL, hash TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE (merge_id, seq)
        );
        """
    )
    # 1) The buggy row: resolved root-null result left as SQL NULL.
    _insert_legacy_row(bare, "legacy-resolved-null", "resolved",
                       {"a": 1}, None, [], None)
    # 2) A normal resolved merge (object result) must be byte-for-byte kept.
    _insert_legacy_row(bare, "legacy-resolved-object", "resolved",
                       {"a": 1}, {"a": 2}, [], '{"a": 2}')
    # 3) A pending merge with a placeholder document must stay SQL NULL.
    _insert_legacy_row(
        bare, "legacy-pending", "pending", {"a": 1},
        {"a": {"__merge_conflict__": "/a"}},
        [{"pointer": "/a", "base": 1, "left": 2, "right": 3, "missing": []}],
        None,
    )
    bare.commit()
    bare.close()


@pytest.fixture
def legacy_database():
    """Point the service at a fresh legacy database for one test, then restore.

    ``db.DATABASE_PATH`` is captured at import time and other test modules
    share the module-global connection, so swap both the path and the cached
    connection and restore them afterwards.
    """
    path = os.path.join(tempfile.mkdtemp(), "legacy-null-result.db")
    _write_legacy_database(path)

    old_path = db.DATABASE_PATH
    old_conn = db._conn
    db._conn = None
    db.DATABASE_PATH = path
    try:
        yield path
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = old_conn
        db.DATABASE_PATH = old_path


def test_startup_repairs_resolved_null_and_keeps_pending(legacy_database):
    # The lifespan startup runs db.init_schema(), which performs the repair.
    with TestClient(app):
        rows = {
            r["id"]: r
            for r in db.query_all("SELECT id, status, result FROM merges")
        }
        # Repaired from the saved document: JSON text "null".
        assert rows["legacy-resolved-null"]["status"] == "resolved"
        assert rows["legacy-resolved-null"]["result"] == "null"
        # Untouched.
        assert rows["legacy-resolved-object"]["result"] == '{"a": 2}'
        # Pending stays SQL NULL: the repair must not invent a result.
        assert rows["legacy-pending"]["status"] == "pending"
        assert rows["legacy-pending"]["result"] is None

    # The repaired row now behaves as a normal resolved merge end to end.
    view = store.get_merge("legacy-resolved-null")
    assert view["status"] == "resolved"
    assert view["result"] is None
    patch = store.get_merge_patch("legacy-resolved-null")
    assert patch["patch"] == [{"op": "replace", "path": "", "value": None}]
    package = store.export_evidence("legacy-resolved-null")
    assert package["payload"]["result"] is None

    # The pending merge is still unavailable (409 path) after the repair.
    with pytest.raises(store.MergeNotResolved):
        store.get_merge_patch("legacy-pending")
    with pytest.raises(store.MergeNotResolved):
        store.export_evidence("legacy-pending")


def test_repair_is_idempotent_across_startups(legacy_database):
    # First startup repairs the row.
    with TestClient(app):
        first = db.query_one(
            "SELECT result FROM merges WHERE id = ?", ("legacy-resolved-null",)
        )["result"]
        assert first == "null"
    # A second startup repairs nothing new and must not error.
    with TestClient(app):
        second = db.query_one(
            "SELECT result FROM merges WHERE id = ?", ("legacy-resolved-null",)
        )["result"]
        pending = db.query_one(
            "SELECT result FROM merges WHERE id = ?", ("legacy-pending",)
        )["result"]
    assert second == "null"
    assert pending is None
