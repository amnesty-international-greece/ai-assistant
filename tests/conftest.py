"""Repo-wide pytest fixtures.

The single most important thing here is :func:`_isolate_db`: a defensive,
autouse safety net that guarantees **no test ever touches the live
``data/amnesty.db``**. That file is held open (WAL mode) by the running
uvicorn server and Discord bot during development; a test that writes to it
can block indefinitely on the SQLite writer lock - which is exactly how a
``pytest`` run ends up "hung" for 40 minutes instead of finishing in ~1.

Most DB-touching tests already patch ``audit._DB_PATH`` to their own
``tmp_path`` (see ``tests/core/test_audit.py`` etc.). This fixture covers the
gap: any test that *doesn't* set up its own DB still gets an isolated,
schema-initialised throwaway database instead of the live one. Tests that DO
patch the path keep working unchanged - their narrower ``patch(...)`` overrides
this default for the duration of the test and restores it on exit.

For speed we build the schema **once** per session into a template file and
copy it per test, rather than re-running the (~dozen CREATE TABLE) schema
script 680 times.
"""

from __future__ import annotations

import shutil

import pytest


@pytest.fixture(scope="session")
def _db_template(tmp_path_factory):
    """Build a schema-only SQLite file once; tests copy it instead of re-init."""
    import src.core.audit as audit_mod

    path = tmp_path_factory.mktemp("db_template") / "template.db"
    saved = (audit_mod._DB_PATH, audit_mod._CONNECTION)
    try:
        audit_mod._DB_PATH = path
        audit_mod._CONNECTION = None
        audit_mod.init_db()
        conn = audit_mod._get_connection()
        # Fold the WAL back into the main file so a plain copy is self-contained.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        conn.close()
    finally:
        audit_mod._DB_PATH, audit_mod._CONNECTION = saved
    return path


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch, _db_template):
    """Point the audit DB at a per-test temp copy of the schema; never the live DB."""
    import src.core.audit as audit_mod

    db = tmp_path / "test_default.db"
    shutil.copyfile(_db_template, db)

    monkeypatch.setattr(audit_mod, "_DB_PATH", db)
    monkeypatch.setattr(audit_mod, "_CONNECTION", None)

    yield

    # Close any connection opened during the test so Windows can remove the
    # temp dir (an open SQLite handle would otherwise lock the file). The
    # globals themselves are restored automatically by monkeypatch.
    conn = getattr(audit_mod, "_CONNECTION", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
