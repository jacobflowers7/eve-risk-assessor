# EVE Online Null-Sec Risk Assessor — Design

## Purpose

A local Mac app that helps solo miners assess how risky a null-sec system is to mine in, using public PvP activity data (kills/losses) from zKillboard/ESI. Initial scope: the **Providence** and **Catch** regions.

## Stack

- **Backend**: Python + FastAPI
- **Database**: SQLite (local file)
- **Frontend**: HTML/JS + Chart.js (served by FastAPI, simple browseable UI)
- **Packaging**: pywebview wraps the FastAPI server + frontend into a double-clickable macOS `.app`, built with PyInstaller

## Architecture & Components

- **Data layer (SQLite)**: stores raw killmail records, per-system metadata, and pre-computed scores (all-time + rolling 30-day).
- **Fetcher module**: on-demand puller — when a user looks up a system, queries zKillboard for killmails in that system, dedupes by killmail ID against existing records, inserts new ones. Includes a "refresh all" bulk mode with rate-limiting.
- **Scoring engine**: runs whenever new killmail data lands for a system; (re)computes all metrics for both all-time and 30-day windows and writes to a `scores` table.
- **API layer (FastAPI)**: serves region/system lists, killmail summaries, and scores; triggers fetch+rescore on lookup.
- **Frontend**: region/system browser + system detail view showing scores, charts, and recent activity.
- **Wrapper**: pywebview hosts the FastAPI server and frontend inside a native window, packaged as a `.app` via PyInstaller.

## Scoring Metrics

Each metric is computed for **all-time** and **last-30-days**:

1. **Activity Score** — kill count in the system, normalized against the region average (comparable across systems).
2. **Camping Score** — repeat-visitor ratio: unique attacker entities (pilots/corps/alliances) ÷ total killmail appearances. Lower ratio = same group repeatedly present = more "camped."
3. **Gang Composition Score** — proportion of solo (1 attacker) vs. small-gang (2–10) vs. fleet (10+) kills, indicating whether threats tend to be lone wolves or blobs.
4. **Blop/Drop Susceptibility Score** — frequency of killmails where attackers include capital/black-ops-class ships (Black Ops Battleships, Dreadnoughts, Titans, etc.), as a proxy for surprise drop risk.
5. **Overall Risk Score** — weighted composite (default weights: Activity 30%, Camping 30%, Gang Composition 20%, Blop Susceptibility 20%) producing a single 0–100 "risk to solo miners" rating. Weights configurable.

## Data Flow

1. App opens → list of Providence/Catch systems shown with cached scores from local DB (instant).
2. User selects a system → background fetch from zKillboard for killmails since the last stored timestamp for that system.
3. New killmails deduped (by killmail ID) and inserted into SQLite.
4. If new data was added, scoring engine recalculates that system's all-time and 30-day scores, updates `scores` table.
5. Frontend reflects updated scores/charts; a "last updated" timestamp shows data freshness.
6. A **"refresh all"** action bulk-updates every system in both regions, sequentially, respecting zKillboard rate limits.

## Error Handling

- API failures (rate limits, timeouts, downtime): retry with backoff; on persistent failure, show "couldn't refresh — showing cached data from [timestamp]" rather than failing the view.
- Malformed/unexpected API responses: log and skip the record; don't crash the fetch.
- Systems with no killmail history: show a "no activity recorded" state with a neutral/unknown score rather than a misleading 0.

## Testing

- **Unit tests** for scoring formulas — feed known killmail datasets, assert expected scores. Highest priority; easiest to test in isolation.
- **Integration tests** for the fetcher — mocked API responses, verify correct dedup/insertion into SQLite.
- **Manual end-to-end testing** of the packaged `.app` on macOS: launch, browse, look up a system, refresh.
