"""Computes risk-assessment scores for a system from its stored killmail history."""
import bisect
import math
import sqlite3
from datetime import datetime, timedelta, timezone

from backend.db import write_lock

WEIGHTS = {
    "activity_score": 0.25,
    "hunter_score": 0.20,
    "prey_score": 0.20,
    "camping_score": 0.15,
    "gang_composition_score": 0.10,
    "blop_susceptibility_score": 0.10,
}

# Kills with at least this many *player* attackers are "fleet-sized" for gang composition scoring.
FLEET_SIZE_THRESHOLD = 10

# Kills with this many or fewer player attackers count as solo/small-gang "hunter" kills --
# the profile that actually catches miners and ratters.
HUNTER_GANG_MAX = 3

# Activity calibration: a kills-per-day rate at/above this saturates the score at 100.
# 10 k/d is "very active" for null-sec — top systems usually sit around 3-5 k/d during prime time.
ACTIVITY_RATE_AT_100 = 10.0

# Share-based metrics scream 100% far too easily from tiny samples (2 solo kills
# = "hunter score 100"). Scores are multiplied by n / (n + PRIOR), pulling them
# toward 0 until enough kills accumulate: 2 kills keeps 17% of the raw score,
# 10 kills keeps 50%, 90 kills keeps 90%.
SMALL_SAMPLE_PRIOR = 10


def _shrink(score: float, sample_size: int) -> float:
    """Confidence-weight a share-based score by the number of kills behind it."""
    return score * (sample_size / (sample_size + SMALL_SAMPLE_PRIOR))

# Capsules. Every successful gank tends to produce a ship kill *and* a pod kill,
# so pods are excluded from kill-rate and composition metrics to avoid double counting.
CAPSULE_TYPE_IDS = {670, 33328}

# Victim ship groups that mark a kill as "prey": non-combat industrial/mining hulls.
# Group IDs verified against ESI /universe/types (see fetcher.CAPITAL_SHIP_TYPE_IDS for
# the analogous attacker-side list).
PREY_GROUP_IDS = {
    28,    # Hauler (industrials, incl. Noctis)
    380,   # Deep Space Transport
    463,   # Mining Barge
    513,   # Freighter (incl. Bowhead)
    543,   # Exhumer
    883,   # Capital Industrial Ship (Rorqual)
    902,   # Jump Freighter
    941,   # Industrial Command Ship (Orca, Porpoise)
    1202,  # Blockade Runner
    1283,  # Expedition Frigate (Prospect, Endurance)
}
# The Venture sits in the generic Frigate group (25), so it's matched by type instead.
PREY_TYPE_IDS = {32880}


def _window_cutoff(window: str) -> str | None:
    if window == "30_day":
        return (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    return None


_KILLMAIL_SELECT = """
    SELECT k.killmail_id, k.killmail_time, k.attacker_count,
           COALESCE(k.player_attacker_count, k.attacker_count) AS player_attacker_count,
           k.has_capital_attacker, k.victim_ship_type_id,
           tn.group_id AS victim_group_id
    FROM killmails k
    LEFT JOIN type_names tn ON tn.type_id = k.victim_ship_type_id
    WHERE k.system_id = ?
"""


def _fetch_killmails(conn: sqlite3.Connection, system_id: int, window: str) -> list[sqlite3.Row]:
    cutoff = _window_cutoff(window)
    if cutoff:
        return conn.execute(
            _KILLMAIL_SELECT + " AND k.killmail_time >= ?", (system_id, cutoff)
        ).fetchall()
    return conn.execute(_KILLMAIL_SELECT, (system_id,)).fetchall()


def _is_pod(km: sqlite3.Row) -> bool:
    return km["victim_ship_type_id"] in CAPSULE_TYPE_IDS or km["victim_group_id"] == 29


def _parse_time(value: str) -> datetime:
    # ESI returns ISO with trailing Z; fromisoformat in Py 3.11+ handles Z.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _activity_score(killmails: list, window: str) -> float:
    """Kills per day, log-scaled so 10 k/d maps to 100.

    For the 30-day window the denominator is the full 30 days: the window is
    completely observed (refreshes always pull the newest kills), so quiet days
    must count. Dividing by the first-to-last-kill span instead would let two
    kills a day apart read as a "2 kills/day" system.

    For all-time, the cache is a truncated sample (zKillboard serves only recent
    kills until history accumulates locally), so the observed span between the
    oldest and newest cached kill is the honest denominator, floored at 1 day.
    """
    if not killmails:
        return 0.0
    if window == "30_day":
        span_days = 30.0
    else:
        times = sorted(_parse_time(km["killmail_time"]) for km in killmails)
        span_seconds = (times[-1] - times[0]).total_seconds()
        span_days = max(span_seconds / 86400, 1.0)
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

    # character_id IS NOT NULL filters out NPC rats, whose NPC-corp rows would
    # otherwise pollute the concentration measure.
    query = f"""
        SELECT a.corporation_id, COUNT(DISTINCT a.killmail_id) AS n
        FROM killmail_attackers a
        JOIN killmails k ON k.killmail_id = a.killmail_id
        WHERE a.system_id = ? AND a.corporation_id IS NOT NULL
          AND a.character_id IS NOT NULL {time_clause}
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
    # A "resident camper" claim needs a body of evidence, not one lucky corp on 3 mails.
    return round(_shrink(hhi * 100, killmail_count), 2)


def _gang_composition_score(killmails: list) -> float:
    """Returns the percentage of kills that were fleet-sized (10+ player attackers) -- larger blobs raise risk."""
    if not killmails:
        return 0.0
    fleet_kills = sum(
        1 for km in killmails if km["player_attacker_count"] >= FLEET_SIZE_THRESHOLD
    )
    return round(_shrink((fleet_kills / len(killmails)) * 100, len(killmails)), 2)


def _hunter_score(killmails: list) -> float:
    """Percentage of kills made by solo/small-gang (1-3 player) attackers.

    This is the danger profile that actually catches miners and ratters: cloaky
    hunters, lone tackle, two-man bomber pairs. Big fleet fights barely touch a
    careful krab; a resident Sabre pilot does.
    """
    if not killmails:
        return 0.0
    hunter_kills = sum(
        1 for km in killmails if 1 <= km["player_attacker_count"] <= HUNTER_GANG_MAX
    )
    return round(_shrink((hunter_kills / len(killmails)) * 100, len(killmails)), 2)


def _prey_score(killmails: list) -> float:
    """Percentage of victims that were industrial/mining hulls.

    Fleet battles inflate raw kill counts without saying anything about PvE
    safety; dead barges and haulers are direct evidence that people like the
    tool's audience are being caught here.
    """
    if not killmails:
        return 0.0
    prey_kills = sum(
        1 for km in killmails
        if km["victim_group_id"] in PREY_GROUP_IDS
        or km["victim_ship_type_id"] in PREY_TYPE_IDS
    )
    return round(_shrink((prey_kills / len(killmails)) * 100, len(killmails)), 2)


def _blop_susceptibility_score(killmails: list) -> float:
    if not killmails:
        return 0.0
    capital_kills = sum(1 for km in killmails if km["has_capital_attacker"])
    return round(_shrink((capital_kills / len(killmails)) * 100, len(killmails)), 2)


def compute_scores(conn: sqlite3.Connection, system_id: int, window: str) -> dict:
    killmails = _fetch_killmails(conn, system_id, window)
    # Pods are excluded everywhere: a gank produces a ship kill and usually a pod
    # kill, so counting both double-weights every gank.
    ship_kills = [km for km in killmails if not _is_pod(km)]
    return {
        "activity_score": _activity_score(ship_kills, window),
        "camping_score": _camping_score(conn, system_id, window, len(killmails)),
        "gang_composition_score": _gang_composition_score(ship_kills),
        "hunter_score": _hunter_score(ship_kills),
        "prey_score": _prey_score(ship_kills),
        "blop_susceptibility_score": _blop_susceptibility_score(ship_kills),
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
            hunter_score, prey_score, blop_susceptibility_score, overall_risk_score, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
           ON CONFLICT(system_id, window) DO UPDATE SET
               activity_score = excluded.activity_score,
               camping_score = excluded.camping_score,
               gang_composition_score = excluded.gang_composition_score,
               hunter_score = excluded.hunter_score,
               prey_score = excluded.prey_score,
               blop_susceptibility_score = excluded.blop_susceptibility_score,
               computed_at = excluded.computed_at""",
        (
            system_id, window,
            scores["activity_score"], scores["camping_score"],
            scores["gang_composition_score"],
            scores.get("hunter_score", 0.0), scores.get("prey_score", 0.0),
            scores["blop_susceptibility_score"],
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
                          gang_composition_score, hunter_score, prey_score,
                          blop_susceptibility_score
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
