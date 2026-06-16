"""SQLite connection, schema, and the shared write lock used by all writers."""
import os
import sqlite3
import sys
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS systems (
    system_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    last_fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS killmails (
    killmail_id INTEGER PRIMARY KEY,
    system_id INTEGER NOT NULL,
    killmail_time TEXT NOT NULL,
    victim_ship_type_id INTEGER,
    attacker_count INTEGER NOT NULL,
    has_capital_attacker INTEGER NOT NULL DEFAULT 0,
    attacker_character_ids TEXT NOT NULL,
    attacker_corporation_ids TEXT NOT NULL,
    attacker_alliance_ids TEXT NOT NULL,
    FOREIGN KEY (system_id) REFERENCES systems(system_id)
);

CREATE INDEX IF NOT EXISTS idx_killmails_system_time
    ON killmails (system_id, killmail_time);

CREATE TABLE IF NOT EXISTS killmail_attackers (
    killmail_id INTEGER NOT NULL,
    system_id INTEGER NOT NULL,
    character_id INTEGER,
    corporation_id INTEGER,
    alliance_id INTEGER,
    FOREIGN KEY (killmail_id) REFERENCES killmails(killmail_id) ON DELETE CASCADE,
    FOREIGN KEY (system_id) REFERENCES systems(system_id)
);

CREATE INDEX IF NOT EXISTS idx_attackers_system ON killmail_attackers (system_id);
CREATE INDEX IF NOT EXISTS idx_attackers_killmail ON killmail_attackers (killmail_id);
CREATE INDEX IF NOT EXISTS idx_attackers_corp ON killmail_attackers (system_id, corporation_id);

CREATE TABLE IF NOT EXISTS scores (
    system_id INTEGER NOT NULL,
    window TEXT NOT NULL CHECK (window IN ('all_time', '30_day')),
    activity_score REAL NOT NULL,
    camping_score REAL NOT NULL,
    gang_composition_score REAL NOT NULL,
    blop_susceptibility_score REAL NOT NULL,
    overall_risk_score REAL NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (system_id, window),
    FOREIGN KEY (system_id) REFERENCES systems(system_id)
);
"""

# Serializes all DB writes across threads. The fetcher, scoring writer, and any
# future writer must take this lock; readers don't need it under WAL.
write_lock = threading.Lock()


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _backfill_attackers(conn)
    conn.commit()


def _backfill_attackers(conn: sqlite3.Connection) -> None:
    """One-shot migration: populate killmail_attackers from legacy JSON columns."""
    import json

    needs_backfill = conn.execute(
        """SELECT k.killmail_id, k.system_id,
                  k.attacker_character_ids, k.attacker_corporation_ids, k.attacker_alliance_ids
           FROM killmails k
           LEFT JOIN killmail_attackers a ON a.killmail_id = k.killmail_id
           WHERE a.killmail_id IS NULL"""
    ).fetchall()
    if not needs_backfill:
        return
    rows = []
    for km in needs_backfill:
        chars = json.loads(km["attacker_character_ids"])
        corps = json.loads(km["attacker_corporation_ids"])
        alliances = json.loads(km["attacker_alliance_ids"])
        for i in range(max(len(chars), len(corps), len(alliances))):
            rows.append((
                km["killmail_id"],
                km["system_id"],
                chars[i] if i < len(chars) else None,
                corps[i] if i < len(corps) else None,
                alliances[i] if i < len(alliances) else None,
            ))
    if rows:
        conn.executemany(
            "INSERT INTO killmail_attackers "
            "(killmail_id, system_id, character_id, corporation_id, alliance_id) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def default_db_path() -> str:
    """Pick a writable DB path. When frozen, persist to per-user app-data; otherwise repo root."""
    override = os.environ.get("EVE_RISK_DB_PATH")
    if override:
        return override
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Application Support/eve-risk-assessor")
        elif sys.platform.startswith("win"):
            base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "eve-risk-assessor")
        else:
            base = os.path.join(
                os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
                "eve-risk-assessor",
            )
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "data.db")
    return os.path.join(os.path.dirname(__file__), "..", "data.db")
