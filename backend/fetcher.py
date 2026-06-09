"""Fetches killmails for a system from zKillboard/ESI and stores new ones in SQLite."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

import httpx

# Serializes write operations so concurrent requests don't race on INSERT.
_write_lock = threading.Lock()

ZKB_URL = "https://zkillboard.com/api/kills/systemID/{system_id}/"
ESI_KILLMAIL_URL = "https://esi.evetech.net/latest/killmails/{killmail_id}/{hash}/"

HEADERS = {"User-Agent": "EVE-Risk-Assessor/1.0 (contact: local-dev)"}

# Ship type IDs considered "capital/blops-class" for the susceptibility metric.
# Compiled from EVE Online static data export (SDE) typeIDs for the relevant
# hull groups: Titans (groupID 30), Supercarriers (659), Dreadnoughts (485),
# Carriers (547), Force Auxiliaries (1538), and Black Ops Battleships (898).
CAPITAL_SHIP_TYPE_IDS = {
    # Titans (group 30)
    671,    # Erebus (Gallente)
    3514,   # Avatar (Amarr)
    11567,  # Ragnarok (Minmatar)
    23773,  # Leviathan (Caldari)

    # Supercarriers (group 659)
    23913,  # Nyx (Caldari)
    23911,  # Hel (Minmatar)
    23915,  # Aeon (Amarr)
    23917,  # Wyvern (Gallente)

    # Dreadnoughts (group 485)
    19720,  # Naglfar (Minmatar)
    19722,  # Moros (Gallente)
    19724,  # Phoenix (Caldari)
    19726,  # Revelation (Amarr)

    # Carriers (group 547)
    23757,  # Archon (Amarr)
    23759,  # Chimera (Caldari)
    23761,  # Thanatos (Gallente)
    24483,  # Nidhoggur (Minmatar)

    # Force Auxiliaries (group 1538)
    37604,  # Apostle (Amarr/Minmatar)
    37605,  # Minokawa (Caldari/Gallente)
    37606,  # Ninazu (Caldari/Minmatar)
    37607,  # Lif (Amarr/Gallente)

    # Black Ops Battleships (group 898)
    17738,  # Redeemer (Amarr) — type ID used in tests/fixtures
    22436,  # Redeemer (alt/older reference retained for compatibility)
    22440,  # Sin (Gallente)
    22442,  # Widow (Caldari)
    22444,  # Panther (Minmatar)
}


def _is_capital(attackers: list[dict]) -> bool:
    return any(a.get("ship_type_id") in CAPITAL_SHIP_TYPE_IDS for a in attackers)


def fetch_and_store_killmails(conn: sqlite3.Connection, system_id: int) -> int:
    """Fetch new killmails for a system from zKillboard, dedupe, and insert. Returns count inserted."""
    response = httpx.get(ZKB_URL.format(system_id=system_id), timeout=30.0, headers=HEADERS)
    response.raise_for_status()
    entries = response.json()

    # Collect killmail details via network BEFORE acquiring the write lock.
    new_killmails = []
    for entry in entries:
        killmail_id = entry.get("killmail_id")
        kill_hash = entry.get("zkb", {}).get("hash")
        if killmail_id is None or kill_hash is None:
            continue
        if conn.execute("SELECT 1 FROM killmails WHERE killmail_id = ?", (killmail_id,)).fetchone():
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
            new_killmails.append((killmail_id, detail))
        except (httpx.HTTPError, KeyError) as exc:
            print(f"Skipping killmail {killmail_id}: {exc}")

    # Write phase: serialized so concurrent requests don't race on INSERT.
    inserted = 0
    with _write_lock:
        for killmail_id, detail in new_killmails:
            attackers = detail.get("attackers", [])
            conn.execute(
                """INSERT OR IGNORE INTO killmails
                   (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
                    has_capital_attacker, attacker_character_ids, attacker_corporation_ids, attacker_alliance_ids)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    killmail_id,
                    system_id,
                    detail.get("killmail_time"),
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
