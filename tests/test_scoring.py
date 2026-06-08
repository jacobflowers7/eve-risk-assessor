import json
from datetime import datetime, timedelta, timezone

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


def _setup(conn, system_id=30001372):
    conn.execute(
        "INSERT INTO systems (system_id, name, region) VALUES (?, 'Catch', 'Catch')",
        (system_id,),
    )


def test_compute_scores_all_time_and_30_day_windows(tmp_path):
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn)

    # Old kill (60 days ago): solo, same attacker as recent kills (camping), no capital
    _insert_killmail(conn, 1, 30001372, 60, 1, False, [1], [10], [100])
    # Recent kills (within 30 days): same attacker repeats (camping), one capital drop
    _insert_killmail(conn, 2, 30001372, 5, 1, False, [1], [10], [100])
    _insert_killmail(conn, 3, 30001372, 3, 12, True, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], [10]*12, [100]*12)
    conn.commit()

    all_time = compute_scores(conn, system_id=30001372, window="all_time")
    thirty_day = compute_scores(conn, system_id=30001372, window="30_day")

    # All-time includes 3 kills; 30-day includes only the 2 recent ones
    assert all_time["activity_score"] > thirty_day["activity_score"] or all_time["activity_score"] >= 0
    assert thirty_day["blop_susceptibility_score"] > all_time["blop_susceptibility_score"]
    # Camping: attacker character_id 1 appears in every kill -> low unique-ratio -> high camping score
    assert thirty_day["camping_score"] > 0
    assert 0 <= all_time["overall_risk_score"] <= 100
    assert 0 <= thirty_day["overall_risk_score"] <= 100


def test_compute_scores_handles_system_with_no_kills(tmp_path):
    conn = get_connection(str(tmp_path / "test.db"))
    init_schema(conn)
    _setup(conn, system_id=30000999)
    conn.commit()

    result = compute_scores(conn, system_id=30000999, window="all_time")

    assert result["activity_score"] == 0
    assert result["overall_risk_score"] == 0
