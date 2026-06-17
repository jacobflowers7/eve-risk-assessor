import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.db import get_connection, init_schema
from backend.scoring import compute_scores, recompute_overall_for_all, store_scores

NOW = datetime.now(timezone.utc)


def _insert_killmail(conn, kid, system_id, days_ago, attacker_count, has_capital,
                     char_ids, corp_ids, alliance_ids):
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    conn.execute(
        """INSERT INTO killmails
           (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
            has_capital_attacker, attacker_character_ids, attacker_corporation_ids, attacker_alliance_ids)
           VALUES (?, ?, ?, 587, ?, ?, ?, ?, ?)""",
        (kid, system_id, ts, attacker_count, int(has_capital),
         json.dumps(char_ids), json.dumps(corp_ids), json.dumps(alliance_ids)),
    )
    # Mirror the JSON columns into the normalized attackers table so the corp-level
    # camping query has data to work with.
    conn.executemany(
        """INSERT INTO killmail_attackers
           (killmail_id, system_id, character_id, corporation_id, alliance_id)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (kid, system_id,
             char_ids[i] if i < len(char_ids) else None,
             corp_ids[i] if i < len(corp_ids) else None,
             alliance_ids[i] if i < len(alliance_ids) else None)
            for i in range(max(len(char_ids), len(corp_ids), len(alliance_ids)))
        ],
    )


def _setup(conn, system_id=30001372):
    conn.execute(
        "INSERT INTO systems (system_id, name, region) VALUES (?, 'Catch', 'Catch')",
        (system_id,),
    )


def test_compute_scores_all_time_and_30_day_windows(tmp_path):
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn)

    # Kill 1 (60d ago): solo, corp 10
    _insert_killmail(conn, 1, 30001372, 60, 1, False, [1], [10], [100])
    # Kill 2 (5d ago): solo, corp 10 again
    _insert_killmail(conn, 2, 30001372, 5, 1, False, [1], [10], [100])
    # Kill 3 (3d ago): 12 attackers from corp 10, with a capital
    _insert_killmail(conn, 3, 30001372, 3, 12, True,
                     list(range(1, 13)), [10]*12, [100]*12)
    conn.commit()

    all_time = compute_scores(conn, system_id=30001372, window="all_time")
    thirty_day = compute_scores(conn, system_id=30001372, window="30_day")

    # Activity is now a rate. The 30-day window concentrates 2 kills into 2 days
    # (1 k/d), while all-time spreads 3 kills over 57 days (0.05 k/d) -> 30-day
    # is the more active window despite having fewer kills.
    assert thirty_day["activity_score"] > all_time["activity_score"]
    # Blops: 1/2 (30d) vs 1/3 (all)
    assert thirty_day["blop_susceptibility_score"] > all_time["blop_susceptibility_score"]
    # HHI: corp 10 is the only attacking corp in both windows -> 100
    assert all_time["camping_score"] == pytest.approx(100.0)
    assert thirty_day["camping_score"] == pytest.approx(100.0)


def test_camping_hhi_differentiates_concentrated_from_diffuse(tmp_path):
    """HHI should be high when one corp dominates and low when many corps share kills."""
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn)

    # Diffuse: 10 killmails, each by a different corp -> shares 1/10, HHI = 10
    for i in range(10):
        _insert_killmail(conn, i + 1, 30001372, i + 1, 1, False, [i + 100], [200 + i], [None])
    conn.commit()
    diffuse = compute_scores(conn, system_id=30001372, window="30_day")
    assert diffuse["camping_score"] == pytest.approx(10.0)

    # Now add 10 more killmails all attributed to one resident corp -> concentration rises
    for i in range(10):
        _insert_killmail(conn, i + 100, 30001372, i + 1, 1, False, [i + 500], [999], [None])
    conn.commit()
    concentrated = compute_scores(conn, system_id=30001372, window="30_day")
    assert concentrated["camping_score"] > diffuse["camping_score"]


def test_camping_score_treats_one_blob_same_as_one_solo(tmp_path):
    """A 50-pilot one-off blob counts as one killmail, not 50, in concentration math."""
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn)

    # 3 killmails by 3 different corps; one happens to be a 50-pilot blob.
    _insert_killmail(conn, 1, 30001372, 5, 50, False,
                     list(range(1, 51)), [42]*50, [500]*50)
    _insert_killmail(conn, 2, 30001372, 4, 1, False, [99], [77], [None])
    _insert_killmail(conn, 3, 30001372, 2, 1, False, [98], [88], [None])
    conn.commit()

    scores = compute_scores(conn, system_id=30001372, window="30_day")
    # 3 corps each on 1 killmail -> HHI = 3 * (1/3)^2 * 100 = 33.33
    assert scores["camping_score"] == pytest.approx(33.33, abs=0.01)


def test_recompute_overall_spreads_scores_across_cohort(tmp_path):
    """The percentile composite should produce a 0-100 spread, not a clumped distribution."""
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    # Five systems with deliberately different per-metric profiles.
    profiles = [
        # (sid,  activity, camping, gang, blops)
        (30000001,    5.0,    10.0,   0.0,   0.0),  # quiet, diffuse
        (30000002,   20.0,    30.0,  10.0,   0.0),
        (30000003,   45.0,    55.0,  20.0,  20.0),  # middle of the pack
        (30000004,   70.0,    75.0,  40.0,  40.0),
        (30000005,   95.0,    95.0,  80.0,  70.0),  # very hot
    ]
    for sid, *_ in profiles:
        conn.execute(
            "INSERT INTO systems (system_id, name, region) VALUES (?, ?, 'Catch')",
            (sid, f"SYS-{sid}"),
        )
    for sid, act, camp, gang, blops in profiles:
        store_scores(conn, sid, "all_time", {
            "activity_score": act, "camping_score": camp,
            "gang_composition_score": gang, "blop_susceptibility_score": blops,
        })
        store_scores(conn, sid, "30_day", {
            "activity_score": act, "camping_score": camp,
            "gang_composition_score": gang, "blop_susceptibility_score": blops,
        })

    recompute_overall_for_all(conn)

    overalls = [r["overall_risk_score"] for r in conn.execute(
        "SELECT overall_risk_score FROM scores WHERE window='all_time' "
        "ORDER BY system_id"
    ).fetchall()]
    # Strictly monotonic by construction: each system dominates the next on every metric.
    assert overalls == sorted(overalls)
    # The hottest system should be in the top quintile; the coldest in the bottom.
    assert overalls[-1] >= 80
    assert overalls[0] <= 20


def test_compute_scores_handles_system_with_no_kills(tmp_path):
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn, system_id=30000999)
    conn.commit()

    result = compute_scores(conn, system_id=30000999, window="all_time")

    assert result["activity_score"] == 0
    assert result["camping_score"] == 0
    assert result["gang_composition_score"] == 0
    assert result["blop_susceptibility_score"] == 0
