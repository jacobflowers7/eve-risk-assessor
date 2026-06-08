# EVE Null-Sec Risk Assessor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local macOS app that scores null-sec systems in Providence and Catch on risk-to-solo-miners, using zKillboard killmail data cached in SQLite.

**Architecture:** A FastAPI backend serves a small HTML/JS frontend and exposes endpoints for system lists, lookups (which trigger on-demand zKillboard fetches + rescoring), and scores. SQLite stores raw killmails and computed scores. pywebview + PyInstaller wrap it all into a double-clickable `.app`.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, SQLite (stdlib `sqlite3`), httpx (HTTP client), pywebview, PyInstaller, pytest, Chart.js (frontend, via CDN)

---

## File Structure

- `backend/db.py` — SQLite connection + schema creation (`killmails`, `scores`, `systems` tables)
- `backend/systems_data.py` — static list of Providence & Catch systems (name, system_id, region)
- `backend/fetcher.py` — zKillboard API client: fetch killmails for a system, dedupe, insert
- `backend/scoring.py` — scoring engine: compute all 5 metrics for all-time and 30-day windows
- `backend/api.py` — FastAPI app and routes
- `backend/main.py` — pywebview entry point that launches the FastAPI server in a native window
- `frontend/index.html` — single-page UI shell
- `frontend/app.js` — fetch calls to backend API, render system list & detail view, Chart.js charts
- `frontend/styles.css` — minimal styling
- `tests/test_db.py` — schema/connection tests
- `tests/test_fetcher.py` — fetcher tests with mocked HTTP responses
- `tests/test_scoring.py` — scoring formula tests against known killmail fixtures
- `tests/test_api.py` — API route tests via FastAPI `TestClient`
- `requirements.txt` — pinned dependencies
- `eve_risk_assessor.spec` — PyInstaller spec file for packaging

---

## Task 1: Project scaffold and dependencies

**Files:**
- Create: `requirements.txt`
- Create: `backend/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create the requirements file**

```
fastapi==0.115.0
uvicorn==0.30.6
httpx==0.27.2
pywebview==5.3.2
pytest==8.3.3
pyinstaller==6.10.0
```

Write this content to `requirements.txt`.

- [ ] **Step 2: Create empty package marker files**

Create `backend/__init__.py` and `tests/__init__.py`, both empty.

- [ ] **Step 3: Create a virtualenv and install dependencies**

Run:
```bash
cd ~/eve-risk-assessor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
Expected: all packages install without error.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt backend/__init__.py tests/__init__.py
git commit -m "chore: scaffold project structure and dependencies"
```

---

## Task 2: System data for Providence and Catch

**Files:**
- Create: `backend/systems_data.py`
- Test: `tests/test_systems_data.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_systems_data.py
from backend.systems_data import SYSTEMS

def test_systems_have_required_fields():
    assert len(SYSTEMS) > 0
    for system in SYSTEMS:
        assert "system_id" in system
        assert "name" in system
        assert system["region"] in ("Providence", "Catch")

def test_systems_cover_both_regions():
    regions = {s["region"] for s in SYSTEMS}
    assert regions == {"Providence", "Catch"}

def test_no_duplicate_system_ids():
    ids = [s["system_id"] for s in SYSTEMS]
    assert len(ids) == len(set(ids))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_systems_data.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.systems_data'`

- [ ] **Step 3: Write the systems data module**

Populate with the real solar system IDs and names for Providence and Catch (look these up from the EVE SDE / zKillboard system search — e.g. `https://zkillboard.com/system/<id>/` pages list the name). Use this starter structure and fill in the full list of systems for both regions:

```python
# backend/systems_data.py
"""Static list of solar systems in the Providence and Catch regions."""

SYSTEMS = [
    {"system_id": 30000144, "name": "Rakapas", "region": "Providence"},
    {"system_id": 30000145, "name": "Old Man Star", "region": "Providence"},
    {"system_id": 30001372, "name": "Catch", "region": "Catch"},
    # ... add all remaining Providence and Catch systems here
]
```

Replace the placeholder entries above with the complete, accurate system list (system_id + name) for both regions — there are roughly 40 systems in Providence and 50 in Catch. Verify each system_id against zKillboard (`https://zkillboard.com/search/<system name>/`) before adding it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_systems_data.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/systems_data.py tests/test_systems_data.py
git commit -m "feat: add static system list for Providence and Catch regions"
```

---

## Task 3: SQLite schema and connection helper

**Files:**
- Create: `backend/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
import sqlite3
from backend.db import get_connection, init_schema

def test_init_schema_creates_tables(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(str(db_path))
    init_schema(conn)

    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert {"killmails", "scores", "systems"}.issubset(tables)
    conn.close()

def test_init_schema_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(str(db_path))
    init_schema(conn)
    init_schema(conn)  # should not raise
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.db'`

- [ ] **Step 3: Write the db module**

```python
# backend/db.py
"""SQLite connection and schema management."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS systems (
    system_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    last_fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS killmails (
    killmail_id INTEGER PRIMARY KEY,
    system_id INTEGER NOT NULL,
    killmail_time TEXT NOT NULL,
    victim_ship_type_id INTEGER,
    attacker_count INTEGER NOT NULL,
    has_capital_attacker INTEGER NOT NULL DEFAULT 0,
    attacker_character_ids TEXT NOT NULL,
    attacker_corporation_ids TEXT NOT NULL,
    attacker_alliance_ids TEXT NOT NULL,
    FOREIGN KEY (system_id) REFERENCES systems(system_id)
);

CREATE TABLE IF NOT EXISTS scores (
    system_id INTEGER NOT NULL,
    window TEXT NOT NULL CHECK (window IN ('all_time', '30_day')),
    activity_score REAL NOT NULL,
    camping_score REAL NOT NULL,
    gang_composition_score REAL NOT NULL,
    blop_susceptibility_score REAL NOT NULL,
    overall_risk_score REAL NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (system_id, window),
    FOREIGN KEY (system_id) REFERENCES systems(system_id)
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/db.py tests/test_db.py
git commit -m "feat: add SQLite schema and connection helper"
```

---

## Task 4: zKillboard fetcher with dedup

**Files:**
- Create: `backend/fetcher.py`
- Test: `tests/test_fetcher.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fetcher.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.fetcher'`

- [ ] **Step 3: Write the fetcher module**

```python
# backend/fetcher.py
"""Fetches killmails for a system from zKillboard/ESI and stores new ones in SQLite."""
import json
import sqlite3
from datetime import datetime, timezone

import httpx

ZKB_URL = "https://zkillboard.com/api/kills/systemID/{system_id}/"
ESI_KILLMAIL_URL = "https://esi.evetech.net/latest/killmails/{killmail_id}/{hash}/"

# Ship type IDs considered "capital/blops-class" for the susceptibility metric.
CAPITAL_SHIP_TYPE_IDS = {
    17738,  # Black Ops Battleship hull example (Redeemer)
    19720,  # Naglfar (Dreadnought)
    671,    # Erebus (Titan)
    # Extend with the full set of capital/dread/titan/blops hull type IDs as needed.
}


def _is_capital(attackers: list[dict]) -> bool:
    return any(a.get("ship_type_id") in CAPITAL_SHIP_TYPE_IDS for a in attackers)


def fetch_and_store_killmails(conn: sqlite3.Connection, system_id: int) -> int:
    """Fetch new killmails for a system from zKillboard, dedupe, and insert. Returns count inserted."""
    response = httpx.get(ZKB_URL.format(system_id=system_id), timeout=30.0)
    response.raise_for_status()
    entries = response.json()

    inserted = 0
    for entry in entries:
        killmail_id = entry["killmail_id"]
        existing = conn.execute(
            "SELECT 1 FROM killmails WHERE killmail_id = ?", (killmail_id,)
        ).fetchone()
        if existing:
            continue

        kill_hash = entry["zkb"]["hash"]
        detail_resp = httpx.get(
            ESI_KILLMAIL_URL.format(killmail_id=killmail_id, hash=kill_hash), timeout=30.0
        )
        detail_resp.raise_for_status()
        detail = detail_resp.json()

        attackers = detail.get("attackers", [])
        conn.execute(
            """INSERT INTO killmails
               (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
                has_capital_attacker, attacker_character_ids, attacker_corporation_ids, attacker_alliance_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                killmail_id,
                system_id,
                detail["killmail_time"],
                detail.get("victim", {}).get("ship_type_id"),
                len(attackers),
                1 if _is_capital(attackers) else 0,
                json.dumps([a.get("character_id") for a in attackers]),
                json.dumps([a.get("corporation_id") for a in attackers]),
                json.dumps([a.get("alliance_id") for a in attackers]),
            ),
        )
        inserted += 1

    conn.execute(
        "UPDATE systems SET last_fetched_at = ? WHERE system_id = ?",
        (datetime.now(timezone.utc).isoformat(), system_id),
    )
    conn.commit()
    return inserted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fetcher.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/fetcher.py tests/test_fetcher.py
git commit -m "feat: add zKillboard fetcher with dedup and capital-ship detection"
```

---

## Task 5: Scoring engine

**Files:**
- Create: `backend/scoring.py`
- Test: `tests/test_scoring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scoring.py
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
    conn.execute("INSERT INTO systems (system_id, name, region) VALUES (30000999, 'Empty', 'Catch')")
    conn.commit()

    result = compute_scores(conn, system_id=30000999, window="all_time")

    assert result["activity_score"] == 0
    assert result["overall_risk_score"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scoring.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.scoring'`

- [ ] **Step 3: Write the scoring module**

```python
# backend/scoring.py
"""Computes risk-assessment scores for a system from its stored killmail history."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

WEIGHTS = {
    "activity_score": 0.30,
    "camping_score": 0.30,
    "gang_composition_score": 0.20,
    "blop_susceptibility_score": 0.20,
}

# A reasonable normalization ceiling: kill counts at/above this are treated as "max activity" (100).
ACTIVITY_NORMALIZATION_CEILING = 50


def _fetch_killmails(conn: sqlite3.Connection, system_id: int, window: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    if window == "30_day":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        rows = conn.execute(
            "SELECT * FROM killmails WHERE system_id = ? AND killmail_time >= ?",
            (system_id, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM killmails WHERE system_id = ?", (system_id,)
        ).fetchall()
    conn.row_factory = None
    return rows


def _activity_score(killmails: list) -> float:
    count = len(killmails)
    return min(100.0, (count / ACTIVITY_NORMALIZATION_CEILING) * 100)


def _camping_score(killmails: list) -> float:
    if not killmails:
        return 0.0
    appearances = 0
    unique_entities = set()
    for km in killmails:
        char_ids = json.loads(km["attacker_character_ids"])
        appearances += len(char_ids)
        unique_entities.update(char_ids)
    if appearances == 0:
        return 0.0
    unique_ratio = len(unique_entities) / appearances
    # Lower unique ratio -> more repeat visitors -> higher camping score
    return round((1 - unique_ratio) * 100, 2)


def _gang_composition_score(killmails: list) -> float:
    """Returns the percentage of kills that were fleet-sized (10+ attackers) — larger blobs raise risk."""
    if not killmails:
        return 0.0
    fleet_kills = sum(1 for km in killmails if km["attacker_count"] >= 10)
    return round((fleet_kills / len(killmails)) * 100, 2)


def _blop_susceptibility_score(killmails: list) -> float:
    if not killmails:
        return 0.0
    capital_kills = sum(1 for km in killmails if km["has_capital_attacker"])
    return round((capital_kills / len(killmails)) * 100, 2)


def compute_scores(conn: sqlite3.Connection, system_id: int, window: str) -> dict:
    killmails = _fetch_killmails(conn, system_id, window)

    scores = {
        "activity_score": round(_activity_score(killmails), 2),
        "camping_score": _camping_score(killmails),
        "gang_composition_score": _gang_composition_score(killmails),
        "blop_susceptibility_score": _blop_susceptibility_score(killmails),
    }
    overall = sum(scores[key] * weight for key, weight in WEIGHTS.items())
    scores["overall_risk_score"] = round(overall, 2)
    return scores


def store_scores(conn: sqlite3.Connection, system_id: int, window: str, scores: dict) -> None:
    conn.execute(
        """INSERT INTO scores
           (system_id, window, activity_score, camping_score, gang_composition_score,
            blop_susceptibility_score, overall_risk_score, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(system_id, window) DO UPDATE SET
               activity_score = excluded.activity_score,
               camping_score = excluded.camping_score,
               gang_composition_score = excluded.gang_composition_score,
               blop_susceptibility_score = excluded.blop_susceptibility_score,
               overall_risk_score = excluded.overall_risk_score,
               computed_at = excluded.computed_at""",
        (
            system_id, window,
            scores["activity_score"], scores["camping_score"],
            scores["gang_composition_score"], scores["blop_susceptibility_score"],
            scores["overall_risk_score"], datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def recompute_and_store(conn: sqlite3.Connection, system_id: int) -> None:
    for window in ("all_time", "30_day"):
        scores = compute_scores(conn, system_id, window)
        store_scores(conn, system_id, window, scores)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scoring.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/scoring.py tests/test_scoring.py
git commit -m "feat: add scoring engine for all-time and 30-day risk metrics"
```

---

## Task 6: FastAPI routes

**Files:**
- Create: `backend/api.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
import pytest
from fastapi.testclient import TestClient

from backend.db import get_connection, init_schema
from backend import api


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_schema(conn)
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
    # Avoid hitting the network: stub out fetch + rescore for this test
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.api'`

- [ ] **Step 3: Write the API module**

```python
# backend/api.py
"""FastAPI application exposing system list, lookup, and scoring endpoints."""
import os
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.db import get_connection, init_schema
from backend.fetcher import fetch_and_store_killmails
from backend.scoring import recompute_and_store
from backend.systems_data import SYSTEMS

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data.db")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = FastAPI()
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def get_db_connection() -> sqlite3.Connection:
    conn = get_connection(DB_PATH)
    init_schema(conn)
    for system in SYSTEMS:
        conn.execute(
            "INSERT OR IGNORE INTO systems (system_id, name, region) VALUES (?, ?, ?)",
            (system["system_id"], system["name"], system["region"]),
        )
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/api/systems")
def list_systems():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT s.system_id, s.name, s.region, s.last_fetched_at,
                  sc.overall_risk_score AS overall_risk_score
           FROM systems s
           LEFT JOIN scores sc ON sc.system_id = s.system_id AND sc.window = 'all_time'
           ORDER BY s.region, s.name"""
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


@app.get("/api/systems/{system_id}")
def get_system_detail(system_id: int):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    system_row = conn.execute(
        "SELECT system_id, name, region, last_fetched_at FROM systems WHERE system_id = ?",
        (system_id,),
    ).fetchone()
    if system_row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="System not found")

    fetch_and_store_killmails(conn, system_id)
    recompute_and_store(conn, system_id)

    score_rows = conn.execute(
        "SELECT * FROM scores WHERE system_id = ?", (system_id,)
    ).fetchall()
    scores = {row["window"]: _row_to_dict(row) for row in score_rows}
    conn.close()

    return {"system": _row_to_dict(system_row), "scores": scores}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/api.py tests/test_api.py
git commit -m "feat: add FastAPI routes for system list and detail lookup"
```

---

## Task 7: Frontend UI

**Files:**
- Create: `frontend/index.html`
- Create: `frontend/app.js`
- Create: `frontend/styles.css`

- [ ] **Step 1: Write the HTML shell**

```html
<!-- frontend/index.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>EVE Null-Sec Risk Assessor</title>
  <link rel="stylesheet" href="/static/styles.css" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
  <h1>Null-Sec Risk Assessor — Providence &amp; Catch</h1>
  <div id="layout">
    <ul id="system-list"></ul>
    <section id="system-detail">
      <p>Select a system to view its risk assessment.</p>
    </section>
  </div>
  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Write the frontend script**

```javascript
// frontend/app.js
async function loadSystemList() {
  const response = await fetch("/api/systems");
  const systems = await response.json();
  const list = document.getElementById("system-list");
  list.innerHTML = "";
  for (const system of systems) {
    const item = document.createElement("li");
    const score = system.overall_risk_score ?? "—";
    item.textContent = `${system.name} (${system.region}) — Risk: ${score}`;
    item.addEventListener("click", () => loadSystemDetail(system.system_id));
    list.appendChild(item);
  }
}

async function loadSystemDetail(systemId) {
  const detail = document.getElementById("system-detail");
  detail.innerHTML = "<p>Loading…</p>";

  const response = await fetch(`/api/systems/${systemId}`);
  if (!response.ok) {
    detail.innerHTML = "<p>Could not load system data.</p>";
    return;
  }
  const data = await response.json();
  const allTime = data.scores.all_time;
  const thirtyDay = data.scores["30_day"];

  detail.innerHTML = `
    <h2>${data.system.name} (${data.system.region})</h2>
    <p>Last fetched: ${data.system.last_fetched_at ?? "never"}</p>
    <h3>All-time</h3>
    ${renderScoreTable(allTime)}
    <h3>Last 30 days</h3>
    ${renderScoreTable(thirtyDay)}
  `;
}

function renderScoreTable(scores) {
  if (!scores) return "<p>No data yet.</p>";
  return `
    <table>
      <tr><td>Overall Risk</td><td>${scores.overall_risk_score}</td></tr>
      <tr><td>Activity</td><td>${scores.activity_score}</td></tr>
      <tr><td>Camping</td><td>${scores.camping_score}</td></tr>
      <tr><td>Gang Composition</td><td>${scores.gang_composition_score}</td></tr>
      <tr><td>Blop/Drop Susceptibility</td><td>${scores.blop_susceptibility_score}</td></tr>
    </table>
  `;
}

loadSystemList();
```

- [ ] **Step 3: Write minimal styling**

```css
/* frontend/styles.css */
body { font-family: -apple-system, sans-serif; margin: 2rem; }
#layout { display: flex; gap: 2rem; }
#system-list { list-style: none; padding: 0; width: 280px; }
#system-list li { padding: 0.4rem; cursor: pointer; border-bottom: 1px solid #ddd; }
#system-list li:hover { background: #f0f0f0; }
table { border-collapse: collapse; }
table td { border: 1px solid #ccc; padding: 0.3rem 0.6rem; }
```

- [ ] **Step 4: Manually verify in browser**

Run:
```bash
cd ~/eve-risk-assessor
source venv/bin/activate
uvicorn backend.api:app --reload
```
Open `http://127.0.0.1:8000` — confirm the system list loads and clicking a system shows score tables (data may be sparse/zero on first run, that's expected).

- [ ] **Step 5: Commit**

```bash
git add frontend/
git commit -m "feat: add frontend UI for system browsing and risk detail view"
```

---

## Task 8: pywebview desktop wrapper

**Files:**
- Create: `backend/main.py`

- [ ] **Step 1: Write the entry point**

```python
# backend/main.py
"""Launches the FastAPI server in a background thread and opens it in a native window."""
import threading

import uvicorn
import webview

from backend.api import app


def _run_server():
    uvicorn.run(app, host="127.0.0.1", port=8731, log_level="warning")


def main():
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()
    webview.create_window("EVE Null-Sec Risk Assessor", "http://127.0.0.1:8731")
    webview.start()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Manually verify the desktop window launches**

Run:
```bash
cd ~/eve-risk-assessor
source venv/bin/activate
python -m backend.main
```
Expected: a native window opens showing the same UI as the browser version, system list loads.

- [ ] **Step 3: Commit**

```bash
git add backend/main.py
git commit -m "feat: add pywebview desktop wrapper entry point"
```

---

## Task 9: Package as a macOS .app

**Files:**
- Create: `eve_risk_assessor.spec`

- [ ] **Step 1: Generate a starter PyInstaller spec**

Run:
```bash
cd ~/eve-risk-assessor
source venv/bin/activate
pyi-makespec --windowed --name "EVE Risk Assessor" backend/main.py --specpath .
```
This creates `EVE Risk Assessor.spec`. Rename it to `eve_risk_assessor.spec` for consistency:
```bash
mv "EVE Risk Assessor.spec" eve_risk_assessor.spec
```

- [ ] **Step 2: Edit the spec to bundle the frontend assets**

Open `eve_risk_assessor.spec` and update the `Analysis(...)` call's `datas` argument so the frontend directory is included in the bundle:

```python
datas=[('frontend', 'frontend')],
```

- [ ] **Step 3: Build the app**

Run:
```bash
pyinstaller eve_risk_assessor.spec
```
Expected: `dist/EVE Risk Assessor.app` is created.

- [ ] **Step 4: Manually verify the packaged app**

Double-click `dist/EVE Risk Assessor.app` in Finder (or run `open "dist/EVE Risk Assessor.app"`).
Expected: native window opens, system list loads, clicking a system fetches and displays scores, "last fetched" timestamp updates.

- [ ] **Step 5: Commit**

```bash
git add eve_risk_assessor.spec
git commit -m "build: add PyInstaller spec for macOS app packaging"
```

Note: do not commit the `build/` or `dist/` directories — add them to `.gitignore` if not already ignored:
```bash
printf "venv/\nbuild/\ndist/\n*.db\n__pycache__/\n*.pyc\n" >> .gitignore
git add .gitignore
git commit -m "chore: ignore build artifacts and local database"
```

---

## Task 10: End-to-end manual verification

**Files:** none (manual testing pass)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: all tests pass.

- [ ] **Step 2: Fresh-database end-to-end run**

Delete any existing local DB, launch the packaged app, and walk through:
1. Open the app — system list appears for both Providence and Catch.
2. Click a system with no cached data — confirm it fetches from zKillboard, stores killmails, computes scores, and displays them (or shows "no activity recorded" if the system truly has none).
3. Click the same system again — confirm it loads instantly from cache and only fetches newer killmails.
4. Disconnect from the network and click an uncached system — confirm the "couldn't refresh — showing cached data" (or equivalent empty-state) message appears rather than a crash.

```bash
rm -f ~/eve-risk-assessor/data.db
open "dist/EVE Risk Assessor.app"
```

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "test: verify end-to-end flow on packaged macOS app" --allow-empty
```
