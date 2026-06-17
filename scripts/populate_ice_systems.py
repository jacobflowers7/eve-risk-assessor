#!/usr/bin/env python3
"""Find ice-bearing systems in Providence + Catch from Fuzzwork's SDE dump.

Note on null-sec ice: CCP removed *persistent* ice asteroid belts from null-sec
around 2014, replacing them with cosmic ice anomalies that spawn cyclically in
specific designated systems. The SDE does not export anomaly spawn tables, so
for Providence and Catch this script may return zero results — that's expected.
When that happens, populate ICE_BELT_SYSTEM_IDS in systems_data.py from a
community-curated source (DOTLAN, EVE Survival, or in-game observation).

Usage:
    venv/bin/python scripts/populate_ice_systems.py
"""

import gzip
import sqlite3
import sys
from pathlib import Path

import httpx

SDE_URL = "https://www.fuzzwork.co.uk/dump/latest-sqlite.db.gz"
TMP_DIR = Path("/tmp/eve-sde")
COMPRESSED_PATH = TMP_DIR / "latest-sqlite.db.gz"
SDE_PATH = TMP_DIR / "latest-sqlite.db"

REGION_IDS = {
    10000047: "Providence",
    10000014: "Catch",
}


def download_if_needed() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if SDE_PATH.exists():
        print(f"[cache] Using existing SDE at {SDE_PATH}")
        return

    print(f"[download] {SDE_URL}")
    with httpx.stream("GET", SDE_URL, timeout=120.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        with COMPRESSED_PATH.open("wb") as out:
            total = 0
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                out.write(chunk)
                total += len(chunk)
                sys.stdout.write(f"\r  fetched {total // (1024 * 1024)} MB")
                sys.stdout.flush()
    print()

    print(f"[decompress] -> {SDE_PATH}")
    with gzip.open(COMPRESSED_PATH) as src, SDE_PATH.open("wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    COMPRESSED_PATH.unlink()


def find_ice_systems() -> list[sqlite3.Row]:
    conn = sqlite3.connect(SDE_PATH)
    conn.row_factory = sqlite3.Row
    region_placeholders = ",".join("?" for _ in REGION_IDS)
    rows = conn.execute(
        f"""SELECT DISTINCT m.solarSystemID, s.solarSystemName, s.regionID, t.typeName
            FROM mapDenormalize m
            JOIN invTypes t ON t.typeID = m.typeID
            JOIN mapSolarSystems s ON s.solarSystemID = m.solarSystemID
            WHERE s.regionID IN ({region_placeholders})
              AND lower(t.typeName) LIKE '%ice%'
            ORDER BY s.regionID, s.solarSystemName""",
        tuple(REGION_IDS),
    ).fetchall()
    return rows


def main() -> int:
    download_if_needed()
    rows = find_ice_systems()

    if not rows:
        print()
        print("No static ice-related items found in Providence or Catch.")
        print("This is expected: null-sec ice spawns as cosmic anomalies, not")
        print("persistent belts. Populate ICE_BELT_SYSTEM_IDS by hand from a")
        print("community list (DOTLAN, EVE Survival, in-game survey).")
        return 0

    by_system: dict[int, list[tuple[str, str, int]]] = {}
    for r in rows:
        by_system.setdefault(r["solarSystemID"], []).append(
            (r["solarSystemName"], r["typeName"], r["regionID"])
        )

    print()
    print(f"Found {len(by_system)} system(s) with ice-related items:")
    for sid, items in sorted(by_system.items()):
        name = items[0][0]
        region = REGION_IDS[items[0][2]]
        types = ", ".join(sorted({t for _, t, _ in items}))
        print(f"  {sid}  {name:10s}  [{region}]  -> {types}")

    print()
    print("Paste this into backend/systems_data.py:")
    print()
    print("ICE_BELT_SYSTEM_IDS: set[int] = {")
    for sid in sorted(by_system):
        print(f"    {sid},")
    print("}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
