"""FastAPI application exposing system list, lookup, and scoring endpoints."""
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.db import default_db_path, get_connection, init_schema
from backend.fetcher import (
    HEADERS,
    fetch_and_store_killmails,
    fetch_and_store_killmails_async,
)
from backend.scoring import recompute_and_store
from backend.systems_data import SYSTEMS

if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.join(os.path.dirname(__file__), "..")

DB_PATH = default_db_path()
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

# Skip re-fetching from zKillboard if the system's data was refreshed more recently than this.
FETCH_STALENESS_MINUTES = 10
DEFAULT_REFRESH_KILLMAILS = 10
REFRESH_ALL_CONCURRENCY = 8

app = FastAPI()
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def _init_database() -> sqlite3.Connection:
    conn = get_connection(DB_PATH)
    init_schema(conn)
    for system in SYSTEMS:
        conn.execute(
            "INSERT OR IGNORE INTO systems (system_id, name, region) VALUES (?, ?, ?)",
            (system["system_id"], system["name"], system["region"]),
        )
    conn.commit()
    return conn


# Single shared connection for the process lifetime -- safe for a single-user local app.
# row_factory is set once in get_connection(); endpoints must not mutate it.
_app_conn = _init_database()


def get_db_connection():
    yield _app_conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _confidence_label(kill_count: int, last_fetched_at: str | None) -> str:
    if last_fetched_at is None:
        return "unknown"
    if kill_count >= 20:
        return "high"
    if kill_count >= 5:
        return "medium"
    return "low"


def _row_to_system_summary(row: sqlite3.Row) -> dict:
    summary = dict(row)
    for key in ("kill_count_24h", "kill_count_7d", "kill_count_30d", "kill_count_all_time"):
        summary[key] = summary[key] or 0
    summary["overall_risk_score"] = summary["all_time_overall_risk_score"]
    summary["data_confidence"] = _confidence_label(
        summary["kill_count_all_time"], summary["last_fetched_at"]
    )
    return summary


def _should_fetch(last_fetched_at: str | None, force: bool = False) -> bool:
    if force or not last_fetched_at:
        return True
    try:
        last_fetched_dt = datetime.fromisoformat(last_fetched_at)
        if last_fetched_dt.tzinfo is None:
            last_fetched_dt = last_fetched_dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=FETCH_STALENESS_MINUTES)
        return last_fetched_dt < cutoff
    except ValueError:
        return True


def _get_system_row(conn: sqlite3.Connection, system_id: int) -> sqlite3.Row:
    system_row = conn.execute(
        "SELECT system_id, name, region, last_fetched_at FROM systems WHERE system_id = ?",
        (system_id,),
    ).fetchone()
    if system_row is None:
        raise HTTPException(status_code=404, detail="System not found")
    return system_row


def _refresh_system(
    conn: sqlite3.Connection,
    system_id: int,
    force: bool = False,
    max_details: int = DEFAULT_REFRESH_KILLMAILS,
) -> dict:
    system_row = _get_system_row(conn, system_id)
    fetched = False
    inserted = 0

    if _should_fetch(system_row["last_fetched_at"], force=force):
        inserted = fetch_and_store_killmails(conn, system_id, max_details=max_details)
        fetched = True

    if inserted > 0:
        recompute_and_store(conn, system_id)
    system_row = _get_system_row(conn, system_id)
    return {
        "system": _row_to_dict(system_row),
        "fetched": fetched,
        "inserted": inserted,
    }


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


_LIST_SYSTEMS_SQL = """
WITH counts_24h AS (
    SELECT system_id, COUNT(*) AS n FROM killmails
    WHERE killmail_time >= ? GROUP BY system_id
),
counts_7d AS (
    SELECT system_id, COUNT(*) AS n FROM killmails
    WHERE killmail_time >= ? GROUP BY system_id
),
counts_30d AS (
    SELECT system_id, COUNT(*) AS n FROM killmails
    WHERE killmail_time >= ? GROUP BY system_id
),
counts_all AS (
    SELECT system_id, COUNT(*) AS n, MAX(killmail_time) AS last_time
    FROM killmails GROUP BY system_id
)
SELECT s.system_id, s.name, s.region, s.last_fetched_at,
       all_sc.activity_score AS all_time_activity_score,
       all_sc.camping_score AS all_time_camping_score,
       all_sc.gang_composition_score AS all_time_gang_composition_score,
       all_sc.blop_susceptibility_score AS all_time_blop_susceptibility_score,
       all_sc.overall_risk_score AS all_time_overall_risk_score,
       day_sc.activity_score AS thirty_day_activity_score,
       day_sc.camping_score AS thirty_day_camping_score,
       day_sc.gang_composition_score AS thirty_day_gang_composition_score,
       day_sc.blop_susceptibility_score AS thirty_day_blop_susceptibility_score,
       day_sc.overall_risk_score AS thirty_day_overall_risk_score,
       COALESCE(c24.n, 0) AS kill_count_24h,
       COALESCE(c7.n, 0) AS kill_count_7d,
       COALESCE(c30.n, 0) AS kill_count_30d,
       COALESCE(call.n, 0) AS kill_count_all_time,
       call.last_time AS last_killmail_time
FROM systems s
LEFT JOIN scores all_sc ON all_sc.system_id = s.system_id AND all_sc.window = 'all_time'
LEFT JOIN scores day_sc ON day_sc.system_id = s.system_id AND day_sc.window = '30_day'
LEFT JOIN counts_24h c24 ON c24.system_id = s.system_id
LEFT JOIN counts_7d c7 ON c7.system_id = s.system_id
LEFT JOIN counts_30d c30 ON c30.system_id = s.system_id
LEFT JOIN counts_all call ON call.system_id = s.system_id
WHERE (? IS NULL OR s.region = ?)
ORDER BY s.region, s.name
"""


@app.get("/api/systems")
def list_systems(
    region: str | None = None,
    conn: sqlite3.Connection = Depends(get_db_connection),
):
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        _LIST_SYSTEMS_SQL,
        (
            (now - timedelta(days=1)).isoformat(),
            (now - timedelta(days=7)).isoformat(),
            (now - timedelta(days=30)).isoformat(),
            region, region,
        ),
    ).fetchall()
    return [_row_to_system_summary(r) for r in rows]


@app.get("/api/systems/{system_id}")
def get_system_detail(system_id: int, conn: sqlite3.Connection = Depends(get_db_connection)):
    refresh_result = _refresh_system(conn, system_id)
    system_row = refresh_result["system"]

    score_rows = conn.execute(
        "SELECT * FROM scores WHERE system_id = ?", (system_id,)
    ).fetchall()
    scores = {window: None for window in ("all_time", "30_day")}
    scores.update({row["window"]: _row_to_dict(row) for row in score_rows})

    return {"system": _row_to_dict(system_row), "scores": scores}


@app.post("/api/systems/{system_id}/refresh")
def refresh_system(
    system_id: int,
    force: bool = Query(default=False),
    max_details: int = Query(default=DEFAULT_REFRESH_KILLMAILS, ge=1, le=50),
    conn: sqlite3.Connection = Depends(get_db_connection),
):
    return _refresh_system(conn, system_id, force=force, max_details=max_details)


@app.post("/api/refresh-all")
async def refresh_all(
    region: str | None = None,
    force: bool = Query(default=False),
    max_details: int = Query(default=DEFAULT_REFRESH_KILLMAILS, ge=1, le=50),
    conn: sqlite3.Connection = Depends(get_db_connection),
):
    """Fan-out refresh across every system in the (optional) region, bounded by REFRESH_ALL_CONCURRENCY."""
    if region:
        rows = conn.execute(
            "SELECT system_id, last_fetched_at FROM systems WHERE region = ?",
            (region,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT system_id, last_fetched_at FROM systems").fetchall()

    targets = [
        row["system_id"]
        for row in rows
        if _should_fetch(row["last_fetched_at"], force=force)
    ]

    sem = asyncio.Semaphore(REFRESH_ALL_CONCURRENCY)
    succeeded = 0
    failed = 0
    total_inserted = 0

    async with httpx.AsyncClient(headers=HEADERS) as client:
        async def run_one(system_id: int) -> tuple[int, int]:
            async with sem:
                try:
                    inserted = await fetch_and_store_killmails_async(
                        client, conn, system_id, max_details=max_details
                    )
                    # recompute is sync + holds the write lock; safe to call from async ctx
                    if inserted > 0:
                        recompute_and_store(conn, system_id)
                    return (1, inserted)
                except Exception as exc:
                    print(f"refresh-all: system {system_id} failed: {exc}")
                    return (0, 0)

        for ok, inserted in await asyncio.gather(*(run_one(sid) for sid in targets)):
            succeeded += ok
            failed += 1 - ok
            total_inserted += inserted

    return {
        "attempted": len(targets),
        "succeeded": succeeded,
        "failed": failed,
        "inserted": total_inserted,
        "skipped": len(rows) - len(targets),
    }


@app.get("/api/systems/{system_id}/killmails")
def get_system_killmails(
    system_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    conn: sqlite3.Connection = Depends(get_db_connection),
):
    system_row = conn.execute(
        "SELECT system_id FROM systems WHERE system_id = ?",
        (system_id,),
    ).fetchone()
    if system_row is None:
        raise HTTPException(status_code=404, detail="System not found")

    rows = conn.execute(
        """SELECT killmail_id, killmail_time, victim_ship_type_id, attacker_count,
                  has_capital_attacker, attacker_character_ids,
                  attacker_corporation_ids, attacker_alliance_ids
           FROM killmails
           WHERE system_id = ?
           ORDER BY killmail_time DESC
           LIMIT ?""",
        (system_id, limit),
    ).fetchall()

    killmails = []
    for row in rows:
        killmail = _row_to_dict(row)
        killmail["has_capital_attacker"] = bool(killmail["has_capital_attacker"])
        killmail["zkillboard_url"] = f"https://zkillboard.com/kill/{killmail['killmail_id']}/"
        killmails.append(killmail)
    return killmails
