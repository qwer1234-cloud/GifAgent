"""Regression: defensive identifier quoting in audit_data (Mimosa triage hardening).

Table/column identifiers are read back from the audited snapshot's own
sqlite_master / PRAGMA; the DB file is CLI-supplied, so identifiers must be
quoted defensively before interpolation into SQL.
"""

from __future__ import annotations

import sqlite3


def test_quote_ident_escapes_embedded_quotes():
    from scripts.token_sprint.audit_data import _quote_ident

    assert _quote_ident('plain') == '"plain"'
    assert _quote_ident('evil"table') == '"evil""table"'


def test_scan_snapshot_survives_hostile_identifiers(tmp_path):
    from scripts.token_sprint.audit_data import scan_snapshot

    db_path = tmp_path / "hostile.db"
    conn = sqlite3.connect(db_path)
    conn.execute('CREATE TABLE "evil""table" ("weird""col" TEXT, file_path TEXT)')
    conn.execute(
        'INSERT INTO "evil""table" VALUES (?, ?)',
        ('x', str(tmp_path / "missing.gif")),
    )
    conn.commit()
    conn.close()

    observations = []
    scan_report = scan_snapshot(db_path, tmp_path, observations)
    broken = [f for f in scan_report if f.get("rule") == "broken_artifact"]
    assert any("missing.gif" in str(f.get("detail")) for f in broken)
