import httpx
import pytest
from backend.db import get_connection, init_schema
from backend.fetcher import fetch_and_store_killmails

ZKB_RESPONSE = [
    {
        "killmail_id": 100001,
        "zkb": {"hash": "abc123"},
    }
]

ESI_KILLMAIL_RESPONSE = {
    "killmail_id": 100001,
    "killmail_time": "2026-05-01T12:00:00Z",
    "solar_system_id": 30001372,
    "victim": {"ship_type_id": 587},
    "attackers": [
        {"character_id": 1, "corporation_id": 10, "alliance_id": 100, "ship_type_id": 17738},
        {"character_id": 2, "corporation_id": 10, "alliance_id": 100, "ship_type_id": 587},
    ],
}


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.db"))
    init_schema(c)
    c.execute(
        "INSERT INTO systems (system_id, name, region) VALUES (?, ?, ?)",
        (30001372, "Catch", "Catch"),
    )
    c.commit()
    return c


def test_fetch_and_store_inserts_new_killmails(conn, monkeypatch):
    def fake_get(url, *args, **kwargs):
        if "zkillboard" in url:
            return httpx.Response(200, json=ZKB_RESPONSE, request=httpx.Request("GET", url))
        return httpx.Response(200, json=ESI_KILLMAIL_RESPONSE, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    inserted = fetch_and_store_killmails(conn, system_id=30001372)

    assert inserted == 1
    row = conn.execute(
        "SELECT killmail_id, attacker_count, has_capital_attacker FROM killmails WHERE killmail_id = 100001"
    ).fetchone()
    assert row == (100001, 2, 1)  # ship_type_id 17738 is a Black Ops Battleship (capital-class)


def test_fetch_and_store_dedupes_existing_killmails(conn, monkeypatch):
    conn.execute(
        """INSERT INTO killmails
           (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
            has_capital_attacker, attacker_character_ids, attacker_corporation_ids, attacker_alliance_ids)
           VALUES (100001, 30001372, '2026-05-01T12:00:00Z', 587, 2, 1, '[1,2]', '[10,10]', '[100,100]')"""
    )
    conn.commit()

    def fake_get(url, *args, **kwargs):
        return httpx.Response(200, json=ZKB_RESPONSE, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    inserted = fetch_and_store_killmails(conn, system_id=30001372)

    assert inserted == 0
    count = conn.execute("SELECT COUNT(*) FROM killmails").fetchone()[0]
    assert count == 1
