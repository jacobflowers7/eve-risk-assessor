"""Computes risk-assessment scores for a system from its stored killmail history."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

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


def _fetch_killmails(conn: sqlite3.Connection, system_id: int, window: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    if window == "30_day":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        rows = conn.execute(
            "SELECT * FROM killmails WHERE system_id = ? AND killmail_time >= ?",
            (system_id, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM killmails WHERE system_id = ?", (system_id,)
        ).fetchall()
    return rows


def _activity_score(killmails: list) -> float:
    count = len(killmails)
    return min(100.0, (count / ACTIVITY_NORMALIZATION_CEILING) * 100)


def _camping_score(killmails: list) -> float:
    if not killmails:
        return 0.0
    appearances = 0
    unique_entities = set()
    for km in killmails:
        char_ids = json.loads(km["attacker_character_ids"])
        appearances += len(char_ids)
        unique_entities.update(char_ids)
    if appearances == 0:
        return 0.0
    unique_ratio = len(unique_entities) / appearances
    # Lower unique ratio -> more repeat visitors -> higher camping score
    return round((1 - unique_ratio) * 100, 2)


def _gang_composition_score(killmails: list) -> float:
    """Returns the percentage of kills that were fleet-sized (10+ attackers) — larger blobs raise risk."""
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
        "camping_score": _camping_score(killmails),
        "gang_composition_score": _gang_composition_score(killmails),
        "blop_susceptibility_score": _blop_susceptibility_score(killmails),
    }
    overall = sum(scores[key] * weight for key, weight in WEIGHTS.items())
    scores["overall_risk_score"] = round(overall, 2)
    return scores


def store_scores(conn: sqlite3.Connection, system_id: int, window: str, scores: dict) -> None:
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
