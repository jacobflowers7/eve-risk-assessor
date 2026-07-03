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
    last_fetched_at TEXT,
    has_ice_belt INTEGER NOT NULL DEFAULT 0,
    gate_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS killmails (
    killmail_id INTEGER PRIMARY KEY,
    system_id INTEGER NOT NULL,
    killmail_time TEXT NOT NULL,
    victim_ship_type_id INTEGER,
    attacker_count INTEGER NOT NULL,
    player_attacker_count INTEGER,
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

CREATE TABLE IF NOT EXISTS type_names (
    type_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    group_id INTEGER
);

-- Cache of ESI /universe/names lookups for corporations/alliances/characters.
CREATE TABLE IF NOT EXISTS entity_names (
    entity_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    system_id INTEGER NOT NULL,
    window TEXT NOT NULL CHECK (window IN ('all_time', '30_day')),
    activity_score REAL NOT NULL,
    camping_score REAL NOT NULL,
    gang_composition_score REAL NOT NULL,
    blop_susceptibility_score REAL NOT NULL,
    hunter_score REAL NOT NULL DEFAULT 0,
    prey_score REAL NOT NULL DEFAULT 0,
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
    _migrate_add_columns(conn)
    _backfill_attackers(conn)
    _backfill_player_counts(conn)
    conn.commit()


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """ALTER TABLE for columns added after the initial schema. CREATE TABLE IF NOT EXISTS
    won't add new columns to existing tables, so we check each one explicitly."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(systems)").fetchall()}
    if "has_ice_belt" not in cols:
        conn.execute("ALTER TABLE systems ADD COLUMN has_ice_belt INTEGER NOT NULL DEFAULT 0")
    if "gate_count" not in cols:
        conn.execute("ALTER TABLE systems ADD COLUMN gate_count INTEGER NOT NULL DEFAULT 0")

    km_cols = {row["name"] for row in conn.execute("PRAGMA table_info(killmails)").fetchall()}
    if "player_attacker_count" not in km_cols:
        conn.execute("ALTER TABLE killmails ADD COLUMN player_attacker_count INTEGER")

    tn_cols = {row["name"] for row in conn.execute("PRAGMA table_info(type_names)").fetchall()}
    if "group_id" not in tn_cols:
        conn.execute("ALTER TABLE type_names ADD COLUMN group_id INTEGER")

    score_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scores)").fetchall()}
    if "hunter_score" not in score_cols:
        conn.execute("ALTER TABLE scores ADD COLUMN hunter_score REAL NOT NULL DEFAULT 0")
    if "prey_score" not in score_cols:
        conn.execute("ALTER TABLE scores ADD COLUMN prey_score REAL NOT NULL DEFAULT 0")


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


def _backfill_player_counts(conn: sqlite3.Connection) -> None:
    """One-shot migration: derive player_attacker_count (attackers with a character_id,
    i.e. real pilots rather than NPC rats) from the legacy JSON column."""
    import json

    rows = conn.execute(
        "SELECT killmail_id, attacker_character_ids FROM killmails "
        "WHERE player_attacker_count IS NULL"
    ).fetchall()
    if not rows:
        return
    updates = []
    for row in rows:
        try:
            char_ids = json.loads(row["attacker_character_ids"])
        except (TypeError, ValueError):
            char_ids = []
        updates.append((sum(1 for c in char_ids if c is not None), row["killmail_id"]))
    conn.executemany(
        "UPDATE killmails SET player_attacker_count = ? WHERE killmail_id = ?",
        updates,
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
