import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.db import get_connection, init_schema
from backend.scoring import compute_scores

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

    # Activity: 3 vs 2 kills, ceiling 50
    assert all_time["activity_score"] > thirty_day["activity_score"]
    # Blops: 1/2 (30d) vs 1/3 (all)
    assert thirty_day["blop_susceptibility_score"] > all_time["blop_susceptibility_score"]
    # Camping: corp 10 appears in every killmail -> 100% camping in both windows
    assert all_time["camping_score"] == pytest.approx(100.0)
    assert thirty_day["camping_score"] == pytest.approx(100.0)
    assert 0 <= all_time["overall_risk_score"] <= 100
    assert 0 <= thirty_day["overall_risk_score"] <= 100


def test_camping_score_ignores_one_off_blob(tmp_path):
    """A single large fleet wipe by a corp that never returns is not camping."""
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn)

    # One big blob, then nothing else from that corp
    _insert_killmail(conn, 1, 30001372, 5, 50, False,
                     list(range(1, 51)), [42]*50, [500]*50)
    # Two unrelated kills by a different corp, only one each
    _insert_killmail(conn, 2, 30001372, 4, 1, False, [99], [77], [None])
    _insert_killmail(conn, 3, 30001372, 2, 1, False, [98], [88], [None])
    conn.commit()

    scores = compute_scores(conn, system_id=30001372, window="30_day")
    # No corp shows up across 2+ distinct killmails -> camping_score is 0
    assert scores["camping_score"] == pytest.approx(0.0)


def test_compute_scores_handles_system_with_no_kills(tmp_path):
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn, system_id=30000999)
    conn.commit()

    result = compute_scores(conn, system_id=30000999, window="all_time")

    assert result["activity_score"] == 0
    assert result["overall_risk_score"] == 0
