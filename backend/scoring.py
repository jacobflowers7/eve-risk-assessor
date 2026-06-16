"""Computes risk-assessment scores for a system from its stored killmail history."""
import sqlite3
from datetime import datetime, timedelta, timezone

from backend.db import write_lock

WEIGHTS = {
    "activity_score": 0.30,
    "camping_score": 0.30,
    "gang_composition_score": 0.20,
    "blop_susceptibility_score": 0.20,
}

# A reasonable normalization ceiling: kill counts at/above this are treated as "max activity" (100).
ACTIVITY_NORMALIZATION_CEILING = 50

# Kills with at least this many attackers are considered "fleet-sized" for gang composition scoring.
FLEET_SIZE_THRESHOLD = 10

# A corp counts as "camping" if it appears in at least this many distinct killmails in the window.
CAMPING_REPEAT_THRESHOLD = 2


def _window_cutoff(window: str) -> str | None:
    if window == "30_day":
        return (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    return None


def _fetch_killmails(conn: sqlite3.Connection, system_id: int, window: str) -> list[sqlite3.Row]:
    cutoff = _window_cutoff(window)
    if cutoff:
        return conn.execute(
            "SELECT killmail_id, attacker_count, has_capital_attacker "
            "FROM killmails WHERE system_id = ? AND killmail_time >= ?",
            (system_id, cutoff),
        ).fetchall()
    return conn.execute(
        "SELECT killmail_id, attacker_count, has_capital_attacker "
        "FROM killmails WHERE system_id = ?",
        (system_id,),
    ).fetchall()


def _activity_score(killmails: list) -> float:
    count = len(killmails)
    return min(100.0, (count / ACTIVITY_NORMALIZATION_CEILING) * 100)


def _camping_score(conn: sqlite3.Connection, system_id: int, window: str, killmail_count: int) -> float:
    """Share of killmails attributable to corps that show up across multiple distinct kills.

    A 50-pilot one-time blob no longer looks like camping; a small corp that
    keeps appearing on different killmails does. Computed at the corp level
    because that's the unit of "who lives here" in EVE.
    """
    if killmail_count == 0:
        return 0.0
    cutoff = _window_cutoff(window)
    time_clause = "AND k.killmail_time >= ?" if cutoff else ""

    query = f"""
        WITH window_attackers AS (
            SELECT DISTINCT a.killmail_id, a.corporation_id
            FROM killmail_attackers a
            JOIN killmails k ON k.killmail_id = a.killmail_id
            WHERE a.system_id = ? AND a.corporation_id IS NOT NULL {time_clause}
        ),
        repeat_corps AS (
            SELECT corporation_id
            FROM window_attackers
            GROUP BY corporation_id
            HAVING COUNT(*) >= ?
        )
        SELECT COUNT(DISTINCT killmail_id) AS camped
        FROM window_attackers
        WHERE corporation_id IN (SELECT corporation_id FROM repeat_corps)
    """
    params: list = [system_id]
    if cutoff:
        params.append(cutoff)
    params.append(CAMPING_REPEAT_THRESHOLD)

    row = conn.execute(query, params).fetchone()
    camped = (row["camped"] if row else 0) or 0
    return round((camped / killmail_count) * 100, 2)


def _gang_composition_score(killmails: list) -> float:
    """Returns the percentage of kills that were fleet-sized (10+ attackers) -- larger blobs raise risk."""
    if not killmails:
        return 0.0
    fleet_kills = sum(1 for km in killmails if km["attacker_count"] >= FLEET_SIZE_THRESHOLD)
    return round((fleet_kills / len(killmails)) * 100, 2)


def _blop_susceptibility_score(killmails: list) -> float:
    if not killmails:
        return 0.0
    capital_kills = sum(1 for km in killmails if km["has_capital_attacker"])
    return round((capital_kills / len(killmails)) * 100, 2)


def compute_scores(conn: sqlite3.Connection, system_id: int, window: str) -> dict:
    killmails = _fetch_killmails(conn, system_id, window)
    scores = {
        "activity_score": round(_activity_score(killmails), 2),
        "camping_score": _camping_score(conn, system_id, window, len(killmails)),
        "gang_composition_score": _gang_composition_score(killmails),
        "blop_susceptibility_score": _blop_susceptibility_score(killmails),
    }
    overall = sum(scores[key] * weight for key, weight in WEIGHTS.items())
    scores["overall_risk_score"] = round(overall, 2)
    return scores


def store_scores(conn: sqlite3.Connection, system_id: int, window: str, scores: dict) -> None:
    with write_lock:
        conn.execute(
            """INSERT INTO scores
               (system_id, window, activity_score, camping_score, gang_composition_score,
                blop_susceptibility_score, overall_risk_score, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(system_id, window) DO UPDATE SET
                   activity_score = excluded.activity_score,
                   camping_score = excluded.camping_score,
                   gang_composition_score = excluded.gang_composition_score,
                   blop_susceptibility_score = excluded.blop_susceptibility_score,
                   overall_risk_score = excluded.overall_risk_score,
                   computed_at = excluded.computed_at""",
            (
                system_id, window,
                scores["activity_score"], scores["camping_score"],
                scores["gang_composition_score"], scores["blop_susceptibility_score"],
                scores["overall_risk_score"], datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def recompute_and_store(conn: sqlite3.Connection, system_id: int) -> None:
    for window in ("all_time", "30_day"):
        scores = compute_scores(conn, system_id, window)
        store_scores(conn, system_id, window, scores)
