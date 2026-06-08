import pytest
from fastapi.testclient import TestClient

from backend.db import get_connection, init_schema
from backend import api


@pytest.fixture
def client(tmp_path, monkeypatch):
    import sqlite3

    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_schema(conn)
    conn.close()
    # Reopen with check_same_thread=False since TestClient runs requests in a
    # threadpool thread different from the one that created the connection.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO systems (system_id, name, region) VALUES (30001372, 'Catch', 'Catch')"
    )
    conn.execute(
        """INSERT INTO scores
           (system_id, window, activity_score, camping_score, gang_composition_score,
            blop_susceptibility_score, overall_risk_score, computed_at)
           VALUES (30001372, 'all_time', 10, 20, 30, 40, 25, '2026-06-01T00:00:00+00:00')"""
    )
    conn.commit()

    monkeypatch.setattr(api, "get_db_connection", lambda: conn)
    return TestClient(api.app)


def test_list_systems_returns_systems_with_scores(client):
    response = client.get("/api/systems")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Catch"
    assert body[0]["region"] == "Catch"


def test_get_system_detail_returns_scores_for_both_windows(client, monkeypatch):
    monkeypatch.setattr(api, "fetch_and_store_killmails", lambda conn, system_id: 0)
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
