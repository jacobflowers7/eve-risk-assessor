"""Computes risk-assessment scores for a system from its stored killmail history."""
import bisect
import math
import sqlite3
from datetime import datetime, timedelta, timezone

from backend.db import write_lock

WEIGHTS = {
    "activity_score": 0.30,
    "camping_score": 0.30,
    "gang_composition_score": 0.20,
    "blop_susceptibility_score": 0.20,
}

# Kills with at least this many attackers are considered "fleet-sized" for gang composition scoring.
FLEET_SIZE_THRESHOLD = 10

# Activity calibration: a kills-per-day rate at/above this saturates the score at 100.
# 10 k/d is "very active" for null-sec — top systems usually sit around 3-5 k/d during prime time.
ACTIVITY_RATE_AT_100 = 10.0


def _window_cutoff(window: str) -> str | None:
    if window == "30_day":
        return (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    return None


def _fetch_killmails(conn: sqlite3.Connection, system_id: int, window: str) -> list[sqlite3.Row]:
    cutoff = _window_cutoff(window)
    if cutoff:
        return conn.execute(
            "SELECT killmail_id, killmail_time, attacker_count, has_capital_attacker "
            "FROM killmails WHERE system_id = ? AND killmail_time >= ?",
            (system_id, cutoff),
        ).fetchall()
    return conn.execute(
        "SELECT killmail_id, killmail_time, attacker_count, has_capital_attacker "
        "FROM killmails WHERE system_id = ?",
        (system_id,),
    ).fetchall()


def _parse_time(value: str) -> datetime:
    # ESI returns ISO with trailing Z; fromisoformat in Py 3.11+ handles Z.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _activity_score(killmails: list, window: str) -> float:
    """Kills per day across the observed span, log-scaled so 10 k/d maps to 100.

    The span is the time between the oldest and newest killmail in the sample,
    floored at 1 day so a single-killmail system doesn't divide by zero. For
    the 30-day window the span is clamped to 30 days to prevent older outliers
    from inflating the divisor.
    """
    if not killmails:
        return 0.0
    times = sorted(_parse_time(km["killmail_time"]) for km in killmails)
    span_seconds = (times[-1] - times[0]).total_seconds()
    span_days = max(span_seconds / 86400, 1.0)
    if window == "30_day":
        span_days = min(span_days, 30.0)
    rate = len(killmails) / span_days
    # log10(1 + rate) / log10(1 + ACTIVITY_RATE_AT_100) → 0..1, then scale to 100.
    score = math.log10(1 + rate) / math.log10(1 + ACTIVITY_RATE_AT_100) * 100
    return round(min(100.0, score), 2)


def _camping_score(conn: sqlite3.Connection, system_id: int, window: str, killmail_count: int) -> float:
    """Herfindahl-Hirschman concentration index over attacking corps in the window.

    Each corp's share = (distinct killmails it appears on) / (sum of all corps'
    distinct-killmail counts). Sum of squared shares × 100. A single corp doing
    every kill scores 100; ten equally-active corps score 10. Captures
    "this system has resident campers" without a magic repeat threshold.
    """
    if killmail_count == 0:
        return 0.0
    cutoff = _window_cutoff(window)
    time_clause = "AND k.killmail_time >= ?" if cutoff else ""

    query = f"""
        SELECT a.corporation_id, COUNT(DISTINCT a.killmail_id) AS n
        FROM killmail_attackers a
        JOIN killmails k ON k.killmail_id = a.killmail_id
        WHERE a.system_id = ? AND a.corporation_id IS NOT NULL {time_clause}
        GROUP BY a.corporation_id
    """
    params: list = [system_id]
    if cutoff:
        params.append(cutoff)
    rows = conn.execute(query, params).fetchall()
    if not rows:
        return 0.0
    total = sum(r["n"] for r in rows)
    if total == 0:
        return 0.0
    hhi = sum((r["n"] / total) ** 2 for r in rows)
    return round(hhi * 100, 2)


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
    return {
        "activity_score": _activity_score(killmails, window),
        "camping_score": _camping_score(conn, system_id, window, len(killmails)),
        "gang_composition_score": _gang_composition_score(killmails),
        "blop_susceptibility_score": _blop_susceptibility_score(killmails),
    }


def _pct_rank(value: float, sorted_values: list[float]) -> float:
    """Midrank percentile (0-100). Ties get the midpoint of their span."""
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return 50.0
    lo = bisect.bisect_left(sorted_values, value)
    hi = bisect.bisect_right(sorted_values, value)
    return (lo + (hi - lo) / 2) / n * 100


def _store_per_metric(conn: sqlite3.Connection, system_id: int, window: str, scores: dict) -> None:
    """Upsert the four per-metric scores. overall_risk_score is set by recompute_overall_for_all."""
    conn.execute(
        """INSERT INTO scores
           (system_id, window, activity_score, camping_score, gang_composition_score,
            blop_susceptibility_score, overall_risk_score, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?)
           ON CONFLICT(system_id, window) DO UPDATE SET
               activity_score = excluded.activity_score,
               camping_score = excluded.camping_score,
               gang_composition_score = excluded.gang_composition_score,
               blop_susceptibility_score = excluded.blop_susceptibility_score,
               computed_at = excluded.computed_at""",
        (
            system_id, window,
            scores["activity_score"], scores["camping_score"],
            scores["gang_composition_score"], scores["blop_susceptibility_score"],
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def store_scores(conn: sqlite3.Connection, system_id: int, window: str, scores: dict) -> None:
    """Persist per-metric scores. Caller should follow up with recompute_overall_for_all
    so this system (and the rest of the cohort) get their percentile composite refreshed."""
    with write_lock:
        _store_per_metric(conn, system_id, window, scores)
        conn.commit()


def recompute_and_store(conn: sqlite3.Connection, system_id: int) -> None:
    """Recompute per-metric scores for one system, then refresh the percentile composite cohort-wide."""
    for window in ("all_time", "30_day"):
        scores = compute_scores(conn, system_id, window)
        store_scores(conn, system_id, window, scores)
    recompute_overall_for_all(conn)


def recompute_overall_for_all(conn: sqlite3.Connection) -> None:
    """For each window, rebuild overall_risk_score as the weighted sum of percentile ranks.

    Why cohort-wide: when one system's raw metric changes, everyone else's percentile
    rank shifts too. Recomputing only the just-updated system would leave the rest stale.
    With ~200 rows this is a few ms.
    """
    with write_lock:
        for window in ("all_time", "30_day"):
            rows = conn.execute(
                """SELECT system_id, activity_score, camping_score,
                          gang_composition_score, blop_susceptibility_score
                   FROM scores WHERE window = ?""",
                (window,),
            ).fetchall()
            if not rows:
                continue
            sorted_by_metric = {
                metric: sorted(r[metric] for r in rows) for metric in WEIGHTS
            }
            updates = []
            for r in rows:
                overall = sum(
                    _pct_rank(r[metric], sorted_by_metric[metric]) * weight
                    for metric, weight in WEIGHTS.items()
                )
                updates.append((round(overall, 2), r["system_id"], window))
            conn.executemany(
                "UPDATE scores SET overall_risk_score = ? "
                "WHERE system_id = ? AND window = ?",
                updates,
            )
        conn.commit()
