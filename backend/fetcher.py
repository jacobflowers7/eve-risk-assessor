"""Fetches killmails for a system from zKillboard/ESI and stores new ones in SQLite."""
import json
import sqlite3
from datetime import datetime, timezone

import httpx

ZKB_URL = "https://zkillboard.com/api/kills/systemID/{system_id}/"
ESI_KILLMAIL_URL = "https://esi.evetech.net/latest/killmails/{killmail_id}/{hash}/"

HEADERS = {"User-Agent": "EVE-Risk-Assessor/1.0 (contact: local-dev)"}

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
    response = httpx.get(ZKB_URL.format(system_id=system_id), timeout=30.0, headers=HEADERS)
    response.raise_for_status()
    entries = response.json()

    inserted = 0
    for entry in entries:
        killmail_id = entry.get("killmail_id")
        kill_hash = entry.get("zkb", {}).get("hash")
        if killmail_id is None or kill_hash is None:
            continue

        existing = conn.execute(
            "SELECT 1 FROM killmails WHERE killmail_id = ?", (killmail_id,)
        ).fetchone()
        if existing:
            continue

        try:
            detail_resp = httpx.get(
                ESI_KILLMAIL_URL.format(killmail_id=killmail_id, hash=kill_hash),
                timeout=30.0,
                headers=HEADERS,
            )
            detail_resp.raise_for_status()
            detail = detail_resp.json()

            killmail_time = detail.get("killmail_time")
            if killmail_time is None:
                raise KeyError("killmail_time")

            attackers = detail.get("attackers", [])
            conn.execute(
                """INSERT INTO killmails
                   (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
                    has_capital_attacker, attacker_character_ids, attacker_corporation_ids, attacker_alliance_ids)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    killmail_id,
                    system_id,
                    killmail_time,
                    detail.get("victim", {}).get("ship_type_id"),
                    len(attackers),
                    1 if _is_capital(attackers) else 0,
                    json.dumps([a.get("character_id") for a in attackers]),
                    json.dumps([a.get("corporation_id") for a in attackers]),
                    json.dumps([a.get("alliance_id") for a in attackers]),
                ),
            )
            inserted += 1
        except (httpx.HTTPError, KeyError) as exc:
            print(f"Skipping killmail {killmail_id}: {exc}")
            continue

    conn.execute(
        "UPDATE systems SET last_fetched_at = ? WHERE system_id = ?",
        (datetime.now(timezone.utc).isoformat(), system_id),
    )
    conn.commit()
    return inserted
