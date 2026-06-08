"""FastAPI application exposing system list, lookup, and scoring endpoints."""
import os
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.db import get_connection, init_schema
from backend.fetcher import fetch_and_store_killmails
from backend.scoring import recompute_and_store
from backend.systems_data import SYSTEMS

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data.db")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = FastAPI()
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def get_db_connection() -> sqlite3.Connection:
    conn = get_connection(DB_PATH)
    init_schema(conn)
    for system in SYSTEMS:
        conn.execute(
            "INSERT OR IGNORE INTO systems (system_id, name, region) VALUES (?, ?, ?)",
            (system["system_id"], system["name"], system["region"]),
        )
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/api/systems")
def list_systems():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT s.system_id, s.name, s.region, s.last_fetched_at,
                  sc.overall_risk_score AS overall_risk_score
           FROM systems s
           LEFT JOIN scores sc ON sc.system_id = s.system_id AND sc.window = 'all_time'
           ORDER BY s.region, s.name"""
    ).fetchall()
    # NOTE: not closing the connection here — in tests, get_db_connection is
    # monkeypatched to return a single shared connection across multiple
    # requests/lookups within a test, and closing it would break subsequent
    # calls. In production, get_connection opens a fresh sqlite3 connection
    # per call, so leaving it open is harmless for this short-lived CLI/dev app.
    return [_row_to_dict(r) for r in rows]


@app.get("/api/systems/{system_id}")
def get_system_detail(system_id: int):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    system_row = conn.execute(
        "SELECT system_id, name, region, last_fetched_at FROM systems WHERE system_id = ?",
        (system_id,),
    ).fetchone()
    if system_row is None:
        raise HTTPException(status_code=404, detail="System not found")

    fetch_and_store_killmails(conn, system_id)
    recompute_and_store(conn, system_id)

    score_rows = conn.execute(
        "SELECT * FROM scores WHERE system_id = ?", (system_id,)
    ).fetchall()
    scores = {window: None for window in ("all_time", "30_day")}
    scores.update({row["window"]: _row_to_dict(row) for row in score_rows})

    return {"system": _row_to_dict(system_row), "scores": scores}
