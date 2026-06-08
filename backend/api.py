"""FastAPI application exposing system list, lookup, and scoring endpoints."""
import os
import sqlite3
import sys

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.db import get_connection, init_schema
from backend.fetcher import fetch_and_store_killmails
from backend.scoring import recompute_and_store
from backend.systems_data import SYSTEMS

if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.join(os.path.dirname(__file__), "..")

DB_PATH = os.path.join(BASE_DIR, "data.db")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = FastAPI()
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def _init_database() -> None:
    conn = get_connection(DB_PATH)
    init_schema(conn)
    for system in SYSTEMS:
        conn.execute(
            "INSERT OR IGNORE INTO systems (system_id, name, region) VALUES (?, ?, ?)",
            (system["system_id"], system["name"], system["region"]),
        )
    conn.commit()
    conn.close()


_init_database()


def get_db_connection():
    conn = get_connection(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/api/systems")
def list_systems(conn: sqlite3.Connection = Depends(get_db_connection)):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT s.system_id, s.name, s.region, s.last_fetched_at,
                  sc.overall_risk_score AS overall_risk_score
           FROM systems s
           LEFT JOIN scores sc ON sc.system_id = s.system_id AND sc.window = 'all_time'
           ORDER BY s.region, s.name"""
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


@app.get("/api/systems/{system_id}")
def get_system_detail(system_id: int, conn: sqlite3.Connection = Depends(get_db_connection)):
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
