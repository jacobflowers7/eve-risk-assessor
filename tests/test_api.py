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
    conn.commit()

    def override_get_db_connection():
        yield conn

    api.app.dependency_overrides[api.get_db_connection] = override_get_db_connection
    yield TestClient(api.app)
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
    assert body[0]["zkillboard_url"] == "https://zkillboard.com/kill/100001/"


def test_refresh_system_endpoint_fetches_and_recomputes(client, monkeypatch):
    calls = {"fetch": 0, "score": 0}

    def fake_fetch(conn, system_id, max_details=10):
        calls["fetch"] += 1
        assert max_details == 10
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


def test_refresh_system_skips_recompute_when_no_new_kills(client, monkeypatch):
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
    assert calls == {"fetch": 1, "score": 0}


def test_get_system_detail_skips_fetch_when_recently_fetched(client, monkeypatch):
    calls = {"count": 0}

    def spy(conn, system_id, max_details=10):
        calls["count"] += 1
        return 0

    monkeypatch.setattr(api, "fetch_and_store_killmails", spy)
    monkeypatch.setattr(api, "recompute_and_store", lambda conn, system_id: None)

    conn = next(iter(client.app.dependency_overrides[api.get_db_connection]()))
    conn.execute(
        "UPDATE systems SET last_fetched_at = ? WHERE system_id = 30001372",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()

    response = client.get("/api/systems/30001372")
    assert response.status_code == 200
    assert calls["count"] == 0


def test_get_system_detail_fetches_when_stale_or_missing(client, monkeypatch):
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
    assert calls["count"] == 1

    conn.execute(
        "UPDATE systems SET last_fetched_at = ? WHERE system_id = 30001372",
        ("2020-01-01T00:00:00+00:00",),
    )
    conn.commit()

    response = client.get("/api/systems/30001372")
    assert response.status_code == 200
    assert calls["count"] == 2


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

    response = client.post("/api/refresh-all?region=Catch")
    assert response.status_code == 200
    body = response.json()
    assert body["attempted"] == 2
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    assert body["inserted"] == 4
    assert sorted(seen) == [30001372, 30001373]
