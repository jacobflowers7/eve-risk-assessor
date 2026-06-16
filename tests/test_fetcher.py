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

# ship_type_id 22436 = Redeemer (Black Ops Battleship); has_capital_attacker should flip to 1.
ESI_KILLMAIL_RESPONSE = {
    "killmail_id": 100001,
    "killmail_time": "2026-05-01T12:00:00Z",
    "solar_system_id": 30001372,
    "victim": {"ship_type_id": 587},
    "attackers": [
        {"character_id": 1, "corporation_id": 10, "alliance_id": 100, "ship_type_id": 22436},
        {"character_id": 2, "corporation_id": 10, "alliance_id": 100, "ship_type_id": 587},
    ],
}


def _make_client(handler) -> httpx.AsyncClient:
    """An AsyncClient backed by a MockTransport that routes via the test handler."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


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


def test_fetch_and_store_inserts_new_killmails(conn):
    def handler(request: httpx.Request) -> httpx.Response:
        if "zkillboard" in str(request.url):
            return httpx.Response(200, json=ZKB_RESPONSE)
        return httpx.Response(200, json=ESI_KILLMAIL_RESPONSE)

    inserted = fetch_and_store_killmails(conn, system_id=30001372, client=_make_client(handler))

    assert inserted == 1
    row = conn.execute(
        "SELECT killmail_id, attacker_count, has_capital_attacker FROM killmails WHERE killmail_id = 100001"
    ).fetchone()
    assert tuple(row) == (100001, 2, 1)
    attackers = conn.execute(
        "SELECT character_id, corporation_id, alliance_id FROM killmail_attackers "
        "WHERE killmail_id = 100001 ORDER BY character_id"
    ).fetchall()
    assert [tuple(r) for r in attackers] == [(1, 10, 100), (2, 10, 100)]


def test_fetch_and_store_dedupes_existing_killmails(conn):
    conn.execute(
        """INSERT INTO killmails
           (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
            has_capital_attacker, attacker_character_ids, attacker_corporation_ids, attacker_alliance_ids)
           VALUES (100001, 30001372, '2026-05-01T12:00:00Z', 587, 2, 1, '[1,2]', '[10,10]', '[100,100]')"""
    )
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ZKB_RESPONSE)

    inserted = fetch_and_store_killmails(conn, system_id=30001372, client=_make_client(handler))

    assert inserted == 0
    count = conn.execute("SELECT COUNT(*) FROM killmails").fetchone()[0]
    assert count == 1


def test_fetch_and_store_skips_failed_killmail_and_keeps_others(conn):
    zkb_response = [
        {"killmail_id": 100001, "zkb": {"hash": "bad"}},
        {"killmail_id": 100002, "zkb": {"hash": "good"}},
    ]
    esi_response_2 = dict(ESI_KILLMAIL_RESPONSE, killmail_id=100002)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "zkillboard" in url:
            return httpx.Response(200, json=zkb_response)
        if "100001" in url:
            return httpx.Response(500)
        return httpx.Response(200, json=esi_response_2)

    inserted = fetch_and_store_killmails(conn, system_id=30001372, client=_make_client(handler))

    assert inserted == 1
    row = conn.execute(
        "SELECT killmail_id FROM killmails WHERE killmail_id = 100002"
    ).fetchone()
    assert tuple(row) == (100002,)
    assert conn.execute(
        "SELECT killmail_id FROM killmails WHERE killmail_id = 100001"
    ).fetchone() is None


def test_fetch_and_store_limits_new_killmail_details(conn):
    zkb_response = [
        {"killmail_id": 100001, "zkb": {"hash": "one"}},
        {"killmail_id": 100002, "zkb": {"hash": "two"}},
        {"killmail_id": 100003, "zkb": {"hash": "three"}},
    ]
    detail_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "zkillboard" in url:
            return httpx.Response(200, json=zkb_response)
        detail_calls.append(url)
        killmail_id = int(url.split("/killmails/")[1].split("/")[0])
        return httpx.Response(200, json=dict(ESI_KILLMAIL_RESPONSE, killmail_id=killmail_id))

    inserted = fetch_and_store_killmails(
        conn, system_id=30001372, max_details=2, client=_make_client(handler)
    )

    assert inserted == 2
    assert len(detail_calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM killmails").fetchone()[0] == 2
