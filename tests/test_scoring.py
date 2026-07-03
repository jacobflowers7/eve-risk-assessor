import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.db import get_connection, init_schema
from backend.scoring import compute_scores, recompute_overall_for_all, store_scores

NOW = datetime.now(timezone.utc)


def _insert_killmail(conn, kid, system_id, days_ago, attacker_count, has_capital,
                     char_ids, corp_ids, alliance_ids, victim_type_id=587):
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    player_count = sum(1 for c in char_ids if c is not None)
    conn.execute(
        """INSERT INTO killmails
           (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
            player_attacker_count, has_capital_attacker, attacker_character_ids,
            attacker_corporation_ids, attacker_alliance_ids)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kid, system_id, ts, victim_type_id, attacker_count, player_count, int(has_capital),
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

    # Activity is a rate over the full window (30d) or observed span (all-time).
    # 30d: 2 kills / 30 days beats all-time's 3 kills / 57 days.
    assert thirty_day["activity_score"] > all_time["activity_score"]
    # Blops: 1/2 (30d) vs 1/3 (all), both discounted for sample size
    assert thirty_day["blop_susceptibility_score"] > all_time["blop_susceptibility_score"]
    # HHI: corp 10 is the only attacking corp in both windows -> raw 100, then
    # discounted by n/(n+10): 3 kills keeps 3/13, 2 kills keeps 2/12.
    assert all_time["camping_score"] == pytest.approx(100.0 * 3 / 13, abs=0.01)
    assert thirty_day["camping_score"] == pytest.approx(100.0 * 2 / 12, abs=0.01)


def test_camping_hhi_differentiates_concentrated_from_diffuse(tmp_path):
    """HHI should be high when one corp dominates and low when many corps share kills."""
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn)

    # Diffuse: 10 killmails, each by a different corp -> shares 1/10, HHI = 10,
    # then the 10-kill sample keeps 10/20 of it.
    for i in range(10):
        _insert_killmail(conn, i + 1, 30001372, i + 1, 1, False, [i + 100], [200 + i], [None])
    conn.commit()
    diffuse = compute_scores(conn, system_id=30001372, window="30_day")
    assert diffuse["camping_score"] == pytest.approx(10.0 * 10 / 20, abs=0.01)

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
    # 3 corps each on 1 killmail -> HHI = 3 * (1/3)^2 * 100 = 33.33,
    # discounted to 3/13 of that for the 3-kill sample.
    assert scores["camping_score"] == pytest.approx(33.33 * 3 / 13, abs=0.01)


def test_recompute_overall_spreads_scores_across_cohort(tmp_path):
    """The percentile composite should produce a 0-100 spread, not a clumped distribution."""
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    # Five systems with deliberately different per-metric profiles.
    profiles = [
        # (sid,  activity, camping, gang, hunter, prey, blops)
        (30000001,    5.0,    10.0,   0.0,   5.0,   0.0,   0.0),  # quiet, diffuse
        (30000002,   20.0,    30.0,  10.0,  15.0,  10.0,   0.0),
        (30000003,   45.0,    55.0,  20.0,  35.0,  25.0,  20.0),  # middle of the pack
        (30000004,   70.0,    75.0,  40.0,  60.0,  45.0,  40.0),
        (30000005,   95.0,    95.0,  80.0,  85.0,  70.0,  70.0),  # very hot
    ]
    for sid, *_ in profiles:
        conn.execute(
            "INSERT INTO systems (system_id, name, region) VALUES (?, ?, 'Catch')",
            (sid, f"SYS-{sid}"),
        )
    for sid, act, camp, gang, hunter, prey, blops in profiles:
        for window in ("all_time", "30_day"):
            store_scores(conn, sid, window, {
                "activity_score": act, "camping_score": camp,
                "gang_composition_score": gang, "hunter_score": hunter,
                "prey_score": prey, "blop_susceptibility_score": blops,
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
    assert result["hunter_score"] == 0
    assert result["prey_score"] == 0
    assert result["blop_susceptibility_score"] == 0


def test_pod_kills_do_not_inflate_activity(tmp_path):
    """A gank produces a ship kill and a pod kill; the pod must not double the rate."""
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn, system_id=30001372)
    conn.execute(
        "INSERT INTO systems (system_id, name, region) VALUES (30001373, 'B', 'Catch')"
    )

    # System A: barge kill + pod kill (type 670 = Capsule) on the same day.
    _insert_killmail(conn, 1, 30001372, 2, 1, False, [1], [10], [100], victim_type_id=587)
    _insert_killmail(conn, 2, 30001372, 2, 1, False, [1], [10], [100], victim_type_id=670)
    # System B: just the ship kill.
    _insert_killmail(conn, 3, 30001373, 2, 1, False, [1], [10], [100], victim_type_id=587)
    conn.commit()

    with_pod = compute_scores(conn, system_id=30001372, window="30_day")
    without_pod = compute_scores(conn, system_id=30001373, window="30_day")
    assert with_pod["activity_score"] == without_pod["activity_score"]


def test_hunter_and_prey_scores(tmp_path):
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn)
    # Procurer = Mining Barge group 463 -> prey.
    conn.execute(
        "INSERT INTO type_names (type_id, name, group_id) VALUES (17480, 'Procurer', 463)"
    )
    conn.execute(
        "INSERT INTO type_names (type_id, name, group_id) VALUES (587, 'Rifter', 25)"
    )

    # Kill 1: two-man gang catches a Procurer -> hunter kill AND prey kill.
    _insert_killmail(conn, 1, 30001372, 5, 2, False, [1, 2], [10, 10], [100, 100],
                     victim_type_id=17480)
    # Kill 2: 15-pilot fleet kills a Rifter -> fleet kill, not hunter, not prey.
    _insert_killmail(conn, 2, 30001372, 3, 15, False,
                     list(range(1, 16)), [10] * 15, [100] * 15, victim_type_id=587)
    conn.commit()

    scores = compute_scores(conn, system_id=30001372, window="30_day")
    # Raw shares are 50% each; the 2-kill sample keeps 2/12 of that.
    assert scores["hunter_score"] == pytest.approx(50.0 * 2 / 12, abs=0.01)
    assert scores["prey_score"] == pytest.approx(50.0 * 2 / 12, abs=0.01)
    assert scores["gang_composition_score"] == pytest.approx(50.0 * 2 / 12, abs=0.01)


def test_npc_rats_do_not_make_a_solo_hunter_look_like_a_gang(tmp_path):
    """A lone hunter finishing a ratting ship shares the mail with NPC attackers;
    only the player should count toward gang size."""
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn)

    # 5 attackers on the mail, but only one has a character_id (the player).
    _insert_killmail(conn, 1, 30001372, 2, 5, False,
                     [1, None, None, None, None],
                     [10, 999001, 999001, 999001, 999001],
                     [100, None, None, None, None])
    conn.commit()

    scores = compute_scores(conn, system_id=30001372, window="30_day")
    assert scores["hunter_score"] == pytest.approx(100.0 * 1 / 11, abs=0.01)
    assert scores["gang_composition_score"] == pytest.approx(0.0)
    # And the NPC corp must not register as a resident camper alongside corp 10.
    assert scores["camping_score"] == pytest.approx(100.0 * 1 / 11, abs=0.01)


def test_sparse_solo_kills_score_below_active_hunting_ground(tmp_path):
    """Regression (the MVCJ-E problem): two solo kills a day apart must not
    out-score a system where solo hunters kill something almost daily."""
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn, system_id=30001372)
    conn.execute(
        "INSERT INTO systems (system_id, name, region) VALUES (30001373, 'HOT', 'Catch')"
    )

    # Sparse system: 2 solo kills, one day apart, then silence.
    _insert_killmail(conn, 1, 30001372, 25, 1, False, [1], [10], [100])
    _insert_killmail(conn, 2, 30001372, 24, 1, False, [1], [10], [100])
    # Active hunting ground: a solo kill on each of 20 different days.
    for day in range(1, 21):
        _insert_killmail(conn, 100 + day, 30001373, day, 1, False, [2], [20], [200])
    conn.commit()

    sparse = compute_scores(conn, system_id=30001372, window="30_day")
    hot = compute_scores(conn, system_id=30001373, window="30_day")

    assert hot["activity_score"] > sparse["activity_score"]
    assert hot["hunter_score"] > sparse["hunter_score"]
    # The 30-day rate divides by the whole window, so 2 kills a day apart
    # reads as ~0.07 kills/day, not 2 kills/day.
    assert sparse["activity_score"] < 5.0
