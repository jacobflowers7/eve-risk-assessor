"""SQLite connection and schema management."""
import sqlite3

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


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
