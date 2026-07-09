from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.db import get_connection, init_schema
from backend import api


@pytest.fixture
def client(tmp_path):
    now = datetime.now(timezone.utc)
    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute(
        """INSERT INTO systems (system_id, name, region, last_fetched_at)
           VALUES (30001372, 'Catch', 'Catch', ?)""",
        (now.isoformat(),),
    )
    conn.executemany(
        """INSERT INTO scores
           (system_id, window, activity_score, camping_score, gang_composition_score,
            blop_susceptibility_score, overall_risk_score, computed_at)
           VALUES (30001372, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("all_time", 10, 20, 30, 40, 25, now.isoformat()),
            ("30_day", 50, 40, 30, 20, 35, now.isoformat()),
        ],
    )
    conn.execute(
        """INSERT INTO killmails
           (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
            has_capital_attacker, attacker_character_ids, attacker_corporation_ids,
            attacker_alliance_ids)
           VALUES (100001, 30001372, ?, 587, 4, 1, '[1, 2, 3, 4]',
                   '[10, 11, 12, 13]', '[100, 101, 102, 103]')""",
        ((now - timedelta(hours=3)).isoformat(),),
    )
    conn.executemany(
        """INSERT INTO killmail_attackers
           (killmail_id, system_id, character_id, corporation_id, alliance_id)
           VALUES (100001, 30001372, ?, ?, ?)""",
        [(1, 10, 100), (2, 11, 101), (3, 12, 102), (4, 13, 103)],
    )
    conn.execute("INSERT INTO type_names (type_id, name, group_id) VALUES (587, 'Rifter', 25)")
    conn.commit()

    def override_get_db_connection():
        yield conn

    api.app.dependency_overrides[api.get_db_connection] = override_get_db_connection
    # refresh-all's streaming body opens its own connection from DB_PATH, so
    # point it at the test database too.
    original_db_path = api.DB_PATH
    api.DB_PATH = db_path
    yield TestClient(api.app)
    api.DB_PATH = original_db_path
    api.app.dependency_overrides.clear()


def test_list_systems_returns_systems_with_scores(client):
    response = client.get("/api/systems")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Catch"
    assert body[0]["region"] == "Catch"
    assert body[0]["overall_risk_score"] == 25
    assert body[0]["all_time_activity_score"] == 10
    assert body[0]["thirty_day_overall_risk_score"] == 35
    assert body[0]["kill_count_24h"] == 1
    assert body[0]["kill_count_7d"] == 1
    assert body[0]["kill_count_30d"] == 1
    assert body[0]["kill_count_all_time"] == 1
    assert body[0]["last_killmail_time"] is not None
    assert body[0]["data_confidence"] == "low"


def test_list_systems_filters_by_region(client):
    response = client.get("/api/systems?region=Catch")
    assert response.status_code == 200
    assert len(response.json()) == 1

    response = client.get("/api/systems?region=Providence")
    assert response.status_code == 200
    assert response.json() == []


def test_list_systems_filters_by_ice_only(client):
    # Add a second system, flag it as ice-bearing
    conn = next(iter(client.app.dependency_overrides[api.get_db_connection]()))
    conn.execute(
        "INSERT INTO systems (system_id, name, region, has_ice_belt) "
        "VALUES (30001999, 'ICE-1', 'Providence', 1)"
    )
    conn.commit()

    # Without the filter, both systems come back
    assert len(client.get("/api/systems").json()) == 2
    # With ice_only, only the flagged one
    iced = client.get("/api/systems?ice_only=true").json()
    assert [s["name"] for s in iced] == ["ICE-1"]
    assert iced[0]["has_ice_belt"] is True


def test_get_system_detail_returns_scores_for_both_windows(client, monkeypatch):
    monkeypatch.setattr(api, "fetch_and_store_killmails", lambda conn, system_id, max_details=10: 0)
    monkeypatch.setattr(api, "recompute_and_store", lambda conn, system_id: None)

    response = client.get("/api/systems/30001372")
    assert response.status_code == 200
    body = response.json()
    assert body["system"]["name"] == "Catch"
    assert "all_time" in body["scores"]
    assert "30_day" in body["scores"]


def test_get_unknown_system_returns_404(client):
    response = client.get("/api/systems/99999999")
    assert response.status_code == 404


def test_get_system_killmails_returns_recent_killmail_rows(client):
    response = client.get("/api/systems/30001372/killmails")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["killmail_id"] == 100001
    assert body[0]["attacker_count"] == 4
    assert body[0]["has_capital_attacker"] is True
    assert body[0]["victim_ship_name"] == "Rifter"
    assert body[0]["victim_class"] == "combat"
    assert body[0]["player_attacker_count"] == 4
    assert body[0]["zkillboard_url"] == "https://zkillboard.com/kill/100001/"


def test_refresh_system_endpoint_fetches_and_recomputes(client, monkeypatch):
    calls = {"fetch": 0, "score": 0}

    def fake_fetch(conn, system_id, max_details=10):
        calls["fetch"] += 1
        assert max_details == 100
        return 3

    def fake_score(conn, system_id):
        calls["score"] += 1

    monkeypatch.setattr(api, "fetch_and_store_killmails", fake_fetch)
    monkeypatch.setattr(api, "recompute_and_store", fake_score)

    response = client.post("/api/systems/30001372/refresh?force=true")

    assert response.status_code == 200
    body = response.json()
    assert body["system"]["name"] == "Catch"
    assert body["fetched"] is True
    assert body["inserted"] == 3
    assert calls == {"fetch": 1, "score": 1}


def test_refresh_system_recomputes_even_when_no_new_kills(client, monkeypatch):
    """Kills aging out of the 30-day window must still refresh scores, so a
    fetch that inserts nothing still triggers a recompute."""
    calls = {"fetch": 0, "score": 0}

    monkeypatch.setattr(
        api, "fetch_and_store_killmails",
        lambda conn, system_id, max_details=10: (calls.__setitem__("fetch", calls["fetch"] + 1) or 0),
    )
    monkeypatch.setattr(
        api, "recompute_and_store",
        lambda conn, system_id: calls.__setitem__("score", calls["score"] + 1),
    )

    response = client.post("/api/systems/30001372/refresh?force=true")
    assert response.status_code == 200
    assert calls == {"fetch": 1, "score": 1}


def test_get_system_detail_never_fetches(client, monkeypatch):
    """Detail is a cached read: row clicks must render instantly even when the
    system is stale. Refreshing is the explicit POST /refresh endpoint's job."""
    calls = {"count": 0}

    def spy(conn, system_id, max_details=10):
        calls["count"] += 1
        return 0

    monkeypatch.setattr(api, "fetch_and_store_killmails", spy)
    monkeypatch.setattr(api, "recompute_and_store", lambda conn, system_id: None)

    conn = next(iter(client.app.dependency_overrides[api.get_db_connection]()))
    conn.execute(
        "UPDATE systems SET last_fetched_at = NULL WHERE system_id = 30001372"
    )
    conn.commit()

    response = client.get("/api/systems/30001372")
    assert response.status_code == 200
    assert calls["count"] == 0


def test_get_system_activity_returns_daily_and_hourly_histograms(client):
    conn = next(iter(client.app.dependency_overrides[api.get_db_connection]()))
    now = datetime.now(timezone.utc)
    # Add a pod kill at the same hour: it must not count in either histogram.
    conn.execute(
        """INSERT INTO killmails
           (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
            player_attacker_count, has_capital_attacker, attacker_character_ids,
            attacker_corporation_ids, attacker_alliance_ids)
           VALUES (100002, 30001372, ?, 670, 1, 1, 0, '[1]', '[10]', '[100]')""",
        ((now - timedelta(hours=3)).isoformat(),),
    )
    conn.commit()

    response = client.get("/api/systems/30001372/activity")
    assert response.status_code == 200
    body = response.json()

    assert len(body["daily"]) == 30
    assert sum(d["kills"] for d in body["daily"]) == 1  # ship kill only, pod excluded
    assert body["daily"][-1]["kills"] + body["daily"][-2]["kills"] == 1

    assert len(body["hourly"]) == 24
    assert sum(body["hourly"]) == 1
    kill_hour = (now - timedelta(hours=3)).hour
    assert body["hourly"][kill_hour] == 1


def test_get_system_killmails_since_hours_filters_by_time(client):
    conn = next(iter(client.app.dependency_overrides[api.get_db_connection]()))
    now = datetime.now(timezone.utc)
    # An old killmail well outside a 24h window (fixture killmail is 3h old).
    conn.execute(
        """INSERT INTO killmails
           (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
            player_attacker_count, has_capital_attacker, attacker_character_ids,
            attacker_corporation_ids, attacker_alliance_ids)
           VALUES (100010, 30001372, ?, 587, 1, 1, 0, '[9]', '[90]', '[900]')""",
        ((now - timedelta(days=5)).isoformat(),),
    )
    conn.commit()

    all_rows = client.get("/api/systems/30001372/killmails").json()
    assert {km["killmail_id"] for km in all_rows} == {100001, 100010}

    recent = client.get("/api/systems/30001372/killmails?since_hours=24").json()
    assert [km["killmail_id"] for km in recent] == [100001]


def test_get_corporation_stats_builds_dossier(client, monkeypatch):
    async def no_network(client_arg, conn_arg, ids):
        return None

    monkeypatch.setattr(api, "resolve_entity_names", no_network)
    conn = next(iter(client.app.dependency_overrides[api.get_db_connection]()))
    now = datetime.now(timezone.utc)
    # Corp 10 already has the fixture kill (4 players, capital, Rifter victim in
    # 30001372). Add a second system and a solo Procurer gank there by corp 10,
    # plus a pod kill that must not count.
    conn.execute(
        "INSERT INTO systems (system_id, name, region) VALUES (30001373, 'F-YH5B', 'Catch')"
    )
    conn.execute(
        "INSERT INTO type_names (type_id, name, group_id) VALUES (17480, 'Procurer', 463)"
    )
    conn.executemany(
        """INSERT INTO killmails
           (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
            player_attacker_count, has_capital_attacker, attacker_character_ids,
            attacker_corporation_ids, attacker_alliance_ids)
           VALUES (?, ?, ?, ?, 1, 1, 0, '[1]', '[10]', '[100]')""",
        [
            (100020, 30001373, (now - timedelta(hours=6)).isoformat(), 17480),
            (100021, 30001373, (now - timedelta(hours=5)).isoformat(), 670),  # pod
        ],
    )
    conn.executemany(
        """INSERT INTO killmail_attackers
           (killmail_id, system_id, character_id, corporation_id, alliance_id)
           VALUES (?, ?, 1, 10, 100)""",
        [(100020, 30001373), (100021, 30001373)],
    )
    conn.execute(
        "INSERT INTO entity_names (entity_id, name, category) VALUES (10, 'Red Corp', 'corporation')"
    )
    conn.commit()

    body = client.get("/api/corporations/10").json()
    assert body["name"] == "Red Corp"
    assert body["totals"]["killmails"] == 2  # fixture Rifter + Procurer; pod excluded
    assert body["totals"]["systems_active"] == 2
    assert body["tactics"]["solo_pct"] == 50.0  # Procurer kill was solo
    assert body["tactics"]["prey_pct"] == 50.0  # Procurer is a mining barge
    assert body["tactics"]["capital_pct"] == 50.0  # fixture kill had a capital
    assert len(body["hourly"]) == 24 and sum(body["hourly"]) == 2
    top_names = [s["name"] for s in body["top_systems"]]
    assert set(top_names) == {"Catch", "F-YH5B"}

    # Unknown corp -> 404
    assert client.get("/api/corporations/424242").status_code == 404


def test_get_top_attackers_ranks_corps_and_uses_cached_names(client, monkeypatch):
    async def no_network(client_arg, conn_arg, ids):
        return None

    monkeypatch.setattr(api, "resolve_entity_names", no_network)
    conn = next(iter(client.app.dependency_overrides[api.get_db_connection]()))
    now = datetime.now(timezone.utc)
    # Second killmail: corp 10 appears again (2 killmails), NPC row must not count.
    conn.execute(
        """INSERT INTO killmails
           (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
            player_attacker_count, has_capital_attacker, attacker_character_ids,
            attacker_corporation_ids, attacker_alliance_ids)
           VALUES (100003, 30001372, ?, 587, 2, 1, 0, '[5, null]', '[10, 999999]', '[100, null]')""",
        ((now - timedelta(hours=1)).isoformat(),),
    )
    conn.executemany(
        """INSERT INTO killmail_attackers
           (killmail_id, system_id, character_id, corporation_id, alliance_id)
           VALUES (100003, 30001372, ?, ?, ?)""",
        [(5, 10, 100), (None, 999999, None)],  # second row is an NPC rat
    )
    conn.execute(
        "INSERT INTO entity_names (entity_id, name, category) VALUES (10, 'Red Corp', 'corporation')"
    )
    conn.commit()

    response = client.get("/api/systems/30001372/top-attackers")
    assert response.status_code == 200
    body = response.json()

    corp_ids = [row["corporation_id"] for row in body]
    assert 999999 not in corp_ids  # NPC-only corp excluded
    assert corp_ids[0] == 10  # on 2 distinct killmails, everyone else on 1
    assert body[0]["kill_count"] == 2
    assert body[0]["name"] == "Red Corp"


def test_refresh_all_fans_out_across_systems(client, monkeypatch):
    """Stub the async fetcher; ensure refresh-all iterates the eligible systems."""
    seen: list[int] = []

    async def fake_async_fetch(client_arg, conn, system_id, max_details=10):
        seen.append(system_id)
        return 2

    monkeypatch.setattr(api, "fetch_and_store_killmails_async", fake_async_fetch)
    monkeypatch.setattr(api, "recompute_and_store", lambda conn, system_id: None)

    conn = next(iter(client.app.dependency_overrides[api.get_db_connection]()))
    # Add a second system, stale enough to trigger a fetch
    conn.execute(
        "INSERT INTO systems (system_id, name, region, last_fetched_at) "
        "VALUES (30001373, 'X', 'Catch', NULL)"
    )
    conn.execute(
        "UPDATE systems SET last_fetched_at = NULL WHERE system_id = 30001372"
    )
    conn.commit()

    import json as _json
    response = client.post("/api/refresh-all?region=Catch")
    assert response.status_code == 200
    events = [_json.loads(line) for line in response.text.splitlines() if line.strip()]
    start = next(e for e in events if e["type"] == "start")
    complete = next(e for e in events if e["type"] == "complete")
    progress = [e for e in events if e["type"] == "progress"]

    assert start["total"] == 2
    assert complete["succeeded"] == 2
    assert complete["failed"] == 0
    assert complete["inserted"] == 4
    assert len(progress) == 2
    assert sorted(seen) == [30001372, 30001373]
