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
