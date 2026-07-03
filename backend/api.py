"""FastAPI application exposing system list, lookup, and scoring endpoints."""
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.db import default_db_path, get_connection, init_schema
from backend.fetcher import (
    HEADERS,
    backfill_type_info,
    fetch_and_store_killmails,
    fetch_and_store_killmails_async,
    resolve_entity_names,
)
from backend.scoring import (
    CAPSULE_TYPE_IDS,
    PREY_GROUP_IDS,
    PREY_TYPE_IDS,
    compute_scores,
    recompute_and_store,
    recompute_overall_for_all,
    store_scores,
)
from backend.systems_data import GATE_COUNTS, ICE_BELT_SYSTEM_IDS, SYSTEMS

if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.join(os.path.dirname(__file__), "..")

DB_PATH = default_db_path()
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

# Skip re-fetching from zKillboard if the system's data was refreshed more recently than this.
FETCH_STALENESS_MINUTES = 10
DEFAULT_REFRESH_KILLMAILS = 100
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
    # Sync ice-belt flags from the source-of-truth set, so editing the set
    # propagates to existing rows on next launch (INSERT OR IGNORE doesn't update).
    conn.execute("UPDATE systems SET has_ice_belt = 0")
    if ICE_BELT_SYSTEM_IDS:
        placeholders = ",".join("?" for _ in ICE_BELT_SYSTEM_IDS)
        conn.execute(
            f"UPDATE systems SET has_ice_belt = 1 WHERE system_id IN ({placeholders})",
            tuple(ICE_BELT_SYSTEM_IDS),
        )
    # Sync stargate counts (static, baked in systems_data.py).
    if GATE_COUNTS:
        conn.executemany(
            "UPDATE systems SET gate_count = ? WHERE system_id = ?",
            [(count, sid) for sid, count in GATE_COUNTS.items()],
        )
    conn.commit()

    # One-shot: resolve ship names + groups for any killmails already in the DB so
    # the UI renders human-readable victim ships even before the next refresh.
    try:
        added = backfill_type_info(conn)
        if added:
            print(f"[startup] Backfilled {added} ship types")
    except Exception as exc:
        print(f"[startup] Type-info backfill skipped: {exc}")

    return conn


# Startup connection: runs schema init/migrations once, then stays open for the
# process lifetime so WAL files remain anchored.
_app_conn = _init_database()


def get_db_connection():
    """Yield a fresh connection per request. Endpoints run in parallel threadpool
    threads, and a single shared sqlite3 connection is not safe under concurrent
    cursor use (InterfaceError). WAL mode gives concurrent readers for free;
    writers all serialize on backend.db.write_lock."""
    conn = get_connection(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


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
    summary["has_ice_belt"] = bool(summary.get("has_ice_belt"))
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
        "SELECT system_id, name, region, last_fetched_at, has_ice_belt, gate_count "
        "FROM systems WHERE system_id = ?",
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

    # Recompute whenever we fetched, even with zero new kills: 30-day scores
    # decay as killmails age out of the window, so "no news" is still news.
    if fetched:
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
SELECT s.system_id, s.name, s.region, s.last_fetched_at, s.has_ice_belt, s.gate_count,
       all_sc.activity_score AS all_time_activity_score,
       all_sc.camping_score AS all_time_camping_score,
       all_sc.gang_composition_score AS all_time_gang_composition_score,
       all_sc.hunter_score AS all_time_hunter_score,
       all_sc.prey_score AS all_time_prey_score,
       all_sc.blop_susceptibility_score AS all_time_blop_susceptibility_score,
       all_sc.overall_risk_score AS all_time_overall_risk_score,
       day_sc.activity_score AS thirty_day_activity_score,
       day_sc.camping_score AS thirty_day_camping_score,
       day_sc.gang_composition_score AS thirty_day_gang_composition_score,
       day_sc.hunter_score AS thirty_day_hunter_score,
       day_sc.prey_score AS thirty_day_prey_score,
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
  AND (? = 0 OR s.has_ice_belt = 1)
ORDER BY s.region, s.name
"""


@app.get("/api/systems")
def list_systems(
    region: str | None = None,
    ice_only: bool = Query(default=False),
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
            1 if ice_only else 0,
        ),
    ).fetchall()
    return [_row_to_system_summary(r) for r in rows]


@app.get("/api/systems/{system_id}")
def get_system_detail(system_id: int, conn: sqlite3.Connection = Depends(get_db_connection)):
    """Cached detail only -- never hits the network, so row clicks render instantly.
    Use POST /api/systems/{id}/refresh to pull fresh killmails."""
    system_row = _get_system_row(conn, system_id)

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
    max_details: int = Query(default=DEFAULT_REFRESH_KILLMAILS, ge=1, le=200),
    conn: sqlite3.Connection = Depends(get_db_connection),
):
    return _refresh_system(conn, system_id, force=force, max_details=max_details)


@app.post("/api/refresh-all")
async def refresh_all(
    region: str | None = None,
    force: bool = Query(default=False),
    max_details: int = Query(default=DEFAULT_REFRESH_KILLMAILS, ge=1, le=200),
    conn: sqlite3.Connection = Depends(get_db_connection),
):
    """Stream NDJSON progress as systems are refreshed.

    Emits one JSON object per line:
      {"type": "start",    "total": N, "skipped": M}
      {"type": "progress", "system_id": ..., "ok": true|false, "inserted": ..., "completed": k}
      {"type": "complete", "succeeded": ..., "failed": ..., "inserted": ..., "skipped": ...}
    """
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
    skipped_count = len(rows) - len(targets)

    async def stream():
        succeeded = 0
        failed = 0
        total_inserted = 0
        refreshed_systems: list[int] = []

        yield json.dumps({"type": "start", "total": len(targets), "skipped": skipped_count}) + "\n"

        if not targets:
            yield json.dumps({
                "type": "complete",
                "succeeded": 0, "failed": 0, "inserted": 0, "skipped": skipped_count,
            }) + "\n"
            return

        # FastAPI tears down the request-scoped connection before a streaming
        # body runs, so this generator must own its own connection.
        stream_conn = get_connection(DB_PATH)
        try:
            sem = asyncio.Semaphore(REFRESH_ALL_CONCURRENCY)

            async with httpx.AsyncClient(headers=HEADERS) as client:
                async def run_one(sid: int) -> tuple[int, bool, int, str | None]:
                    async with sem:
                        try:
                            inserted = await fetch_and_store_killmails_async(
                                client, stream_conn, sid, max_details=max_details
                            )
                            # Recompute even when nothing inserted: kills aging out of
                            # the 30-day window change scores too.
                            for window in ("all_time", "30_day"):
                                scores = compute_scores(stream_conn, sid, window)
                                store_scores(stream_conn, sid, window, scores)
                            refreshed_systems.append(sid)
                            return (sid, True, inserted, None)
                        except Exception as exc:
                            return (sid, False, 0, f"{type(exc).__name__}: {exc}"[:200])

                tasks = [asyncio.create_task(run_one(sid)) for sid in targets]

                for fut in asyncio.as_completed(tasks):
                    sid, ok, inserted, err = await fut
                    if ok:
                        succeeded += 1
                        total_inserted += inserted
                    else:
                        failed += 1
                    event = {
                        "type": "progress",
                        "system_id": sid,
                        "ok": ok,
                        "inserted": inserted,
                        "completed": succeeded + failed,
                    }
                    if err:
                        event["error"] = err
                    yield json.dumps(event) + "\n"

            if refreshed_systems:
                recompute_overall_for_all(stream_conn)
        finally:
            stream_conn.close()

        yield json.dumps({
            "type": "complete",
            "succeeded": succeeded,
            "failed": failed,
            "inserted": total_inserted,
            "skipped": skipped_count,
        }) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


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
        """SELECT k.killmail_id, k.killmail_time, k.victim_ship_type_id,
                  tn.name AS victim_ship_name,
                  tn.group_id AS victim_group_id,
                  k.attacker_count,
                  COALESCE(k.player_attacker_count, k.attacker_count) AS player_attacker_count,
                  k.has_capital_attacker,
                  k.attacker_character_ids, k.attacker_corporation_ids,
                  k.attacker_alliance_ids
           FROM killmails k
           LEFT JOIN type_names tn ON tn.type_id = k.victim_ship_type_id
           WHERE k.system_id = ?
           ORDER BY k.killmail_time DESC
           LIMIT ?""",
        (system_id, limit),
    ).fetchall()

    killmails = []
    for row in rows:
        killmail = _row_to_dict(row)
        killmail["has_capital_attacker"] = bool(killmail["has_capital_attacker"])
        killmail["victim_class"] = _victim_class(
            killmail["victim_ship_type_id"], killmail["victim_group_id"]
        )
        killmail["zkillboard_url"] = f"https://zkillboard.com/kill/{killmail['killmail_id']}/"
        killmails.append(killmail)
    return killmails


def _victim_class(type_id: int | None, group_id: int | None) -> str:
    """Coarse victim bucket for the UI: pod, prey (industrial/mining), or combat."""
    if type_id in CAPSULE_TYPE_IDS or group_id == 29:
        return "pod"
    if group_id in PREY_GROUP_IDS or type_id in PREY_TYPE_IDS:
        return "prey"
    if group_id is None:
        return "unknown"
    return "combat"


# Pod-exclusion predicate reused by the activity histograms. NULL-safe: an
# unresolved group_id must not drop the row.
_NON_POD_CLAUSE = """
    AND (k.victim_ship_type_id IS NULL OR k.victim_ship_type_id NOT IN (670, 33328))
    AND (tn.group_id IS NULL OR tn.group_id != 29)
"""


@app.get("/api/systems/{system_id}/activity")
def get_system_activity(system_id: int, conn: sqlite3.Connection = Depends(get_db_connection)):
    """Daily kill counts for the last 30 days plus an hour-of-day (EVE/UTC time)
    histogram over all cached kills. Pods excluded from both."""
    _get_system_row(conn, system_id)
    now = datetime.now(timezone.utc)
    cutoff_30d = (now - timedelta(days=30)).isoformat()

    daily_rows = conn.execute(
        f"""SELECT date(k.killmail_time) AS day, COUNT(*) AS n
            FROM killmails k
            LEFT JOIN type_names tn ON tn.type_id = k.victim_ship_type_id
            WHERE k.system_id = ? AND k.killmail_time >= ? {_NON_POD_CLAUSE}
            GROUP BY day""",
        (system_id, cutoff_30d),
    ).fetchall()
    by_day = {row["day"]: row["n"] for row in daily_rows}
    daily = []
    for offset in range(29, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        daily.append({"date": day, "kills": by_day.get(day, 0)})

    hourly_rows = conn.execute(
        f"""SELECT CAST(strftime('%H', k.killmail_time) AS INTEGER) AS hour, COUNT(*) AS n
            FROM killmails k
            LEFT JOIN type_names tn ON tn.type_id = k.victim_ship_type_id
            WHERE k.system_id = ? {_NON_POD_CLAUSE}
            GROUP BY hour""",
        (system_id,),
    ).fetchall()
    hourly = [0] * 24
    for row in hourly_rows:
        if row["hour"] is not None:
            hourly[row["hour"]] = row["n"]

    return {"daily": daily, "hourly": hourly}


@app.get("/api/systems/{system_id}/top-attackers")
def get_top_attackers(
    system_id: int,
    window: str = Query(default="30_day", pattern="^(30_day|all_time)$"),
    limit: int = Query(default=8, ge=1, le=25),
    conn: sqlite3.Connection = Depends(get_db_connection),
):
    """Most active attacker corporations in the window (player pilots only),
    with names resolved via ESI and cached locally."""
    _get_system_row(conn, system_id)
    params: list = [system_id]
    time_clause = ""
    if window == "30_day":
        time_clause = "AND k.killmail_time >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
    params.append(limit)

    rows = conn.execute(
        f"""SELECT a.corporation_id, COUNT(DISTINCT a.killmail_id) AS kill_count,
                   MAX(k.killmail_time) AS last_seen
            FROM killmail_attackers a
            JOIN killmails k ON k.killmail_id = a.killmail_id
            WHERE a.system_id = ? AND a.corporation_id IS NOT NULL
              AND a.character_id IS NOT NULL {time_clause}
            GROUP BY a.corporation_id
            ORDER BY kill_count DESC, last_seen DESC
            LIMIT ?""",
        params,
    ).fetchall()

    corp_ids = [row["corporation_id"] for row in rows]
    if corp_ids:
        try:
            async def resolve():
                async with httpx.AsyncClient(headers=HEADERS) as client:
                    await resolve_entity_names(client, conn, corp_ids)
            asyncio.run(resolve())
        except Exception as exc:
            print(f"top-attackers: name resolution skipped: {exc}")

    names = {}
    if corp_ids:
        placeholders = ",".join("?" for _ in corp_ids)
        names = {
            r["entity_id"]: r["name"]
            for r in conn.execute(
                f"SELECT entity_id, name FROM entity_names WHERE entity_id IN ({placeholders})",
                corp_ids,
            ).fetchall()
        }

    return [
        {
            "corporation_id": row["corporation_id"],
            "name": names.get(row["corporation_id"]),
            "kill_count": row["kill_count"],
            "last_seen": row["last_seen"],
        }
        for row in rows
    ]
