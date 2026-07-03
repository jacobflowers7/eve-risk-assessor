#!/usr/bin/env python3
"""Fetch fresh killmails, rescore, and export a static copy of the site.

Used by the GitHub Actions publish workflow to build the GitHub Pages version:
every run pulls new kills for all systems, recomputes scores, and writes the
frontend plus pre-rendered JSON (the data the live API would have served) into
an output directory ready for static hosting.

Usage:
    python scripts/publish_static.py --out site            # fetch + export
    python scripts/publish_static.py --out site --skip-fetch  # export only
"""
import argparse
import asyncio
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from backend import api  # noqa: E402  (import runs schema init/migrations)
from backend.fetcher import HEADERS, fetch_and_store_killmails_async  # noqa: E402
from backend.scoring import (  # noqa: E402
    compute_scores,
    recompute_overall_for_all,
    store_scores,
)

FETCH_CONCURRENCY = 8
KILLMAILS_PER_SYSTEM = 100


async def refresh_all_systems(conn) -> tuple[int, int]:
    """Pull new killmails for every tracked system and rescore. Returns (ok, failed)."""
    system_ids = [row["system_id"] for row in conn.execute("SELECT system_id FROM systems")]
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    ok = failed = 0

    async with httpx.AsyncClient(headers=HEADERS) as client:
        async def run_one(sid: int) -> bool:
            async with sem:
                try:
                    await fetch_and_store_killmails_async(
                        client, conn, sid, max_details=KILLMAILS_PER_SYSTEM
                    )
                    for window in ("all_time", "30_day"):
                        store_scores(conn, sid, window, compute_scores(conn, sid, window))
                    return True
                except Exception as exc:
                    print(f"[refresh] system {sid} failed: {type(exc).__name__}: {exc}")
                    return False

        results = await asyncio.gather(*(run_one(sid) for sid in system_ids))

    ok = sum(results)
    failed = len(results) - ok
    recompute_overall_for_all(conn)
    return ok, failed


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")))


def export_site(conn, out: Path) -> int:
    """Write the frontend plus pre-rendered API responses into `out`."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # Frontend, with absolute /static/ URLs rewritten to relative ones (GitHub
    # Pages serves under /<repo>/) and the static-mode flag injected.
    frontend = REPO_ROOT / "frontend"
    for name in ("styles.css", "app.js"):
        shutil.copy(frontend / name, out / name)
    html = (frontend / "index.html").read_text()
    html = html.replace('href="/static/', 'href="').replace('src="/static/', 'src="')
    html = re.sub(
        r"(<script src=\"app\.js)",
        '<script>window.EVE_STATIC = true;</script>\n  \\1',
        html,
    )
    (out / "index.html").write_text(html)

    systems = api.list_systems(region=None, ice_only=False, conn=conn)
    _write_json(out / "data" / "systems.json", systems)

    for system in systems:
        sid = system["system_id"]
        detail = api.get_system_detail(sid, conn=conn)
        killmails = api.get_system_killmails(sid, limit=50, conn=conn)
        activity = api.get_system_activity(sid, conn=conn)
        top_attackers = api.get_top_attackers(sid, window="30_day", limit=8, conn=conn)
        window_label = "last 30 days"
        if not top_attackers:
            top_attackers = api.get_top_attackers(sid, window="all_time", limit=8, conn=conn)
            window_label = "cached history"
        _write_json(out / "data" / "systems" / f"{sid}.json", {
            "system": detail["system"],
            "scores": detail["scores"],
            "killmails": killmails,
            "activity": activity,
            "top_attackers": top_attackers,
            "top_attackers_window": window_label,
        })

    _write_json(out / "data" / "manifest.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system_count": len(systems),
    })
    # Opt out of GitHub Pages' Jekyll processing; serve files verbatim.
    (out / ".nojekyll").touch()
    return len(systems)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="site", help="output directory")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="export from the existing database without fetching")
    args = parser.parse_args()

    conn = api._app_conn  # already initialized/migrated by importing backend.api

    if not args.skip_fetch:
        ok, failed = asyncio.run(refresh_all_systems(conn))
        print(f"[refresh] {ok} systems updated, {failed} failed")
        if ok == 0:
            print("[refresh] every fetch failed; keeping existing data")

    count = export_site(conn, Path(args.out))
    print(f"[export] wrote {count} systems to {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
