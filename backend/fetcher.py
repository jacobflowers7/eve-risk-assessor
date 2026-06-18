"""Fetches killmails for a system from zKillboard/ESI and stores new ones in SQLite."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import httpx

from backend.db import write_lock

ZKB_URL = "https://zkillboard.com/api/kills/systemID/{system_id}/"
ESI_KILLMAIL_URL = "https://esi.evetech.net/latest/killmails/{killmail_id}/{hash}/"
ESI_NAMES_URL = "https://esi.evetech.net/latest/universe/names/"

# ESI /universe/names accepts up to 1000 IDs per call.
NAMES_BATCH_SIZE = 1000
NAMES_TIMEOUT_SECONDS = 10.0

HEADERS = {"User-Agent": "EVE-Risk-Assessor/1.0 (contact: local-dev)"}
DEFAULT_MAX_DETAILS = 100
ZKB_TIMEOUT_SECONDS = 12.0
ESI_TIMEOUT_SECONDS = 6.0
ESI_CONCURRENCY = 5  # parallel ESI requests per system

# Ship type IDs considered "capital/blops-class" for the susceptibility metric.
# Source: EVE Online Static Data Export (SDE), hull groups Titans (30),
# Supercarriers (659), Dreadnoughts (485), Carriers (547), Force Auxiliaries
# (1538), and Black Ops Battleships (898).
CAPITAL_SHIP_TYPE_IDS = {
    # Titans (group 30)
    671,    # Erebus (Gallente)
    3514,   # Avatar (Amarr)
    11567,  # Ragnarok (Minmatar)
    23773,  # Leviathan (Caldari)

    # Supercarriers (group 659)
    23913,  # Nyx (Gallente)
    23911,  # Hel (Minmatar)
    23917,  # Wyvern (Caldari)
    23919,  # Aeon (Amarr) -- prior code had 23915 here, which is a different type

    # Dreadnoughts (group 485)
    19720,  # Revelation (Amarr)
    19722,  # Moros (Gallente)
    19724,  # Phoenix (Caldari)
    19726,  # Naglfar (Minmatar)

    # Carriers (group 547)
    23757,  # Archon (Amarr)
    23759,  # Chimera (Caldari)
    23761,  # Thanatos (Gallente)
    24483,  # Nidhoggur (Minmatar)

    # Force Auxiliaries (group 1538)
    37604,  # Apostle (Amarr)
    37605,  # Minokawa (Caldari)
    37606,  # Lif (Minmatar)
    37607,  # Ninazu (Gallente)

    # Black Ops Battleships (group 898)
    22436,  # Redeemer (Amarr)
    22440,  # Sin (Gallente)
    22442,  # Widow (Caldari)
    22444,  # Panther (Minmatar)
}


def _is_capital(attackers: list[dict]) -> bool:
    return any(a.get("ship_type_id") in CAPITAL_SHIP_TYPE_IDS for a in attackers)


async def _fetch_esi_detail(client: httpx.AsyncClient, killmail_id: int, kill_hash: str) -> dict | None:
    """Fetch one killmail detail from ESI. Returns None on transport error or missing time."""
    try:
        resp = await client.get(
            ESI_KILLMAIL_URL.format(killmail_id=killmail_id, hash=kill_hash),
            timeout=ESI_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        detail = resp.json()
        if detail.get("killmail_time") is None:
            return None
        return detail
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Skipping killmail {killmail_id}: {exc}")
        return None


async def _gather_new_killmails(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    system_id: int,
    max_details: int,
) -> list[tuple[int, dict]]:
    """Hit zKB for the recent kill list, dedupe against the DB, then fetch ESI details concurrently."""
    resp = await client.get(ZKB_URL.format(system_id=system_id), timeout=ZKB_TIMEOUT_SECONDS)
    resp.raise_for_status()
    entries = resp.json()

    candidates: list[tuple[int, str]] = []
    for entry in entries:
        killmail_id = entry.get("killmail_id")
        kill_hash = entry.get("zkb", {}).get("hash")
        if killmail_id is None or kill_hash is None:
            continue
        if conn.execute("SELECT 1 FROM killmails WHERE killmail_id = ?", (killmail_id,)).fetchone():
            continue
        candidates.append((killmail_id, kill_hash))
        if len(candidates) >= max_details:
            break

    sem = asyncio.Semaphore(ESI_CONCURRENCY)

    async def fetch_one(kid: int, khash: str) -> tuple[int, dict] | None:
        async with sem:
            detail = await _fetch_esi_detail(client, kid, khash)
        return (kid, detail) if detail is not None else None

    results = await asyncio.gather(*(fetch_one(kid, h) for kid, h in candidates))
    return [r for r in results if r is not None]


def _insert_killmails(
    conn: sqlite3.Connection,
    system_id: int,
    new_killmails: list[tuple[int, dict]],
) -> int:
    """Write phase, serialized via write_lock. Returns the *actually* inserted row count."""
    inserted = 0
    with write_lock:
        for killmail_id, detail in new_killmails:
            attackers = detail.get("attackers", [])
            cursor = conn.execute(
                """INSERT OR IGNORE INTO killmails
                   (killmail_id, system_id, killmail_time, victim_ship_type_id, attacker_count,
                    has_capital_attacker, attacker_character_ids, attacker_corporation_ids,
                    attacker_alliance_ids)
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
            if cursor.rowcount > 0:
                inserted += 1
                conn.executemany(
                    """INSERT INTO killmail_attackers
                       (killmail_id, system_id, character_id, corporation_id, alliance_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    [
                        (
                            killmail_id,
                            system_id,
                            a.get("character_id"),
                            a.get("corporation_id"),
                            a.get("alliance_id"),
                        )
                        for a in attackers
                    ],
                )
        conn.execute(
            "UPDATE systems SET last_fetched_at = ? WHERE system_id = ?",
            (datetime.now(timezone.utc).isoformat(), system_id),
        )
        conn.commit()
    return inserted


async def _resolve_type_names(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    type_ids: list[int],
) -> None:
    """POST distinct uncached type_ids to ESI /universe/names; store in type_names."""
    distinct = list({tid for tid in type_ids if tid is not None})
    if not distinct:
        return
    placeholders = ",".join("?" for _ in distinct)
    known = {r[0] for r in conn.execute(
        f"SELECT type_id FROM type_names WHERE type_id IN ({placeholders})",
        tuple(distinct),
    ).fetchall()}
    unknown = [tid for tid in distinct if tid not in known]
    if not unknown:
        return

    resolved: list[tuple[int, str]] = []
    for batch_start in range(0, len(unknown), NAMES_BATCH_SIZE):
        batch = unknown[batch_start:batch_start + NAMES_BATCH_SIZE]
        try:
            resp = await client.post(ESI_NAMES_URL, json=batch, timeout=NAMES_TIMEOUT_SECONDS)
            resp.raise_for_status()
            for item in resp.json():
                if "id" in item and "name" in item:
                    resolved.append((item["id"], item["name"]))
        except (httpx.HTTPError, ValueError) as exc:
            print(f"Failed to resolve {len(batch)} type names: {exc}")

    if resolved:
        with write_lock:
            conn.executemany(
                "INSERT OR IGNORE INTO type_names (type_id, name) VALUES (?, ?)",
                resolved,
            )
            conn.commit()


async def fetch_and_store_killmails_async(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    system_id: int,
    max_details: int = DEFAULT_MAX_DETAILS,
) -> int:
    new_killmails = await _gather_new_killmails(client, conn, system_id, max_details)
    inserted = _insert_killmails(conn, system_id, new_killmails)
    if inserted > 0:
        victim_type_ids = [
            detail.get("victim", {}).get("ship_type_id")
            for _, detail in new_killmails
        ]
        await _resolve_type_names(client, conn, victim_type_ids)
    return inserted


def backfill_type_names(conn: sqlite3.Connection) -> int:
    """One-shot: resolve names for every victim_ship_type_id already in the DB
    but missing from type_names. Returns the count of names added."""
    rows = conn.execute(
        """SELECT DISTINCT k.victim_ship_type_id
           FROM killmails k
           LEFT JOIN type_names tn ON tn.type_id = k.victim_ship_type_id
           WHERE k.victim_ship_type_id IS NOT NULL AND tn.type_id IS NULL"""
    ).fetchall()
    missing = [r[0] for r in rows]
    if not missing:
        return 0

    async def run():
        async with httpx.AsyncClient(headers=HEADERS) as client:
            await _resolve_type_names(client, conn, missing)

    before = conn.execute("SELECT COUNT(*) FROM type_names").fetchone()[0]
    asyncio.run(run())
    after = conn.execute("SELECT COUNT(*) FROM type_names").fetchone()[0]
    return after - before


def fetch_and_store_killmails(
    conn: sqlite3.Connection,
    system_id: int,
    max_details: int = DEFAULT_MAX_DETAILS,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Sync wrapper. Pass an httpx.AsyncClient in tests to inject a MockTransport."""
    async def run():
        if client is not None:
            return await fetch_and_store_killmails_async(client, conn, system_id, max_details)
        async with httpx.AsyncClient(headers=HEADERS) as new_client:
            return await fetch_and_store_killmails_async(new_client, conn, system_id, max_details)

    return asyncio.run(run())
