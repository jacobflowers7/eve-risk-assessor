import sqlite3
from backend.db import get_connection, init_schema

def test_init_schema_creates_tables(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(str(db_path))
    init_schema(conn)

    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert {"killmails", "scores", "systems"}.issubset(tables)
    conn.close()

def test_init_schema_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(str(db_path))
    init_schema(conn)
    init_schema(conn)  # should not raise
    conn.close()
