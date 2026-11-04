# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

Two separate projects share this repo:

- **`platform/backend/`** — the active codebase: a FastAPI data ingestion and analytics platform for Indian health and demographic data (Census 2011, HMIS, data.gov.in, NFHS). Almost all work happens here. `platform/README.md` is the long-form design doc, but parts of its API table are out of date (see below).
- **Root `run_pipeline.py` + `requirements.txt`** — the older standalone ETL. It imports an `etl/` package (`etl.extract`, `etl.transform`, …) that is **not committed**, so it cannot run from this checkout. The root `venv/` holds only that pipeline's dependencies (pandas, requests, pypdf…), not FastAPI. `app/constants/state_aliases.py` and the `data_gov_in` connector were carried over from that `etl/config.py`.

## Commands (run from `platform/backend/`)

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\python.exe
.venv/bin/python -m uvicorn app.main:app --reload --port 8000       # API docs at http://127.0.0.1:8000/docs
```

- Tests (pytest) cover the Website tab only: `.venv/bin/pip install -r requirements-dev.txt`, then `.venv/bin/python -m pytest`. `tests/conftest.py` points the app at a throwaway SQLite file and staging folder before importing it, so tests never touch the real database. No linter or formatter config exists.
- Config comes from env vars or `platform/backend/.env` (see `.env.example`). Settings use the `PLATFORM_` prefix (e.g. `PLATFORM_DATABASE_URL`), except `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_RPM`, which are unprefixed. With no `PLATFORM_DATABASE_URL`, the app uses SQLite at `data/warehouse/platform.db`.
- Ingesting data.gov.in sources needs `DATA_GOV_IN_API_KEY`; the public sample key gets rate-limited (429/502) almost immediately.
- The app sets `root_path="/etl"` because it is deployed behind a proxy under `/etl`.

### Migrations (Alembic)

- `init_db()` has `run_migrations()` **commented out**, so the app does not migrate on startup. Schema changes are applied by hand.
- Generate a migration after changing an ORM model: `alembic revision --autogenerate -m "<message>"` (run from `platform/backend/`). Apply it with `alembic upgrade head`.
- `alembic/env.py` reads the DB URL from app settings (`alembic.ini`'s `sqlalchemy.url` is intentionally blank). Its `include_object` hides tables starting with `census_`, `hmis_`, `dashboard_`, `ds_`, `gemini_` (the `gemini_*` tables are created with raw SQL in `ai_data/store.py`) so autogenerate doesn't drop them. Still read every generated migration before applying it.

### Standalone scripts (also in `platform/backend/`)

- `extract.py <url-or-file> [--type api|file|web|auto] [--clean]`: one URL in, one CSV out, without the server. `census://districts?state=10` for Census.
- `fetch_census_by_state.py --states 11,30 --level subdistrict`: resumable per-state Census pull (one CSV per state).
- `build_census_levels.py`: derives India and state rows by summing districts (the Census API has no state level) and checks the national total of 1,210,854,977.
- `rebuild_hmis_raw.py --last 5 [--dry-run|--truncate]`: rebuilds `hmis_raw` from cached `.xls` files. This only works if `PLATFORM_DELETE_SOURCE_FILES_AFTER_INGEST=false`, because the default deletes downloaded payloads after ingest.
- `migrate_sqlite_to_postgres.py [--replace]`: copies the SQLite DB into Postgres.

## Backend architecture

### Module structure
- `app/main.py` only builds the app. Every endpoint lives in `app/modules/<section>/` (`router.py`, `service.py`, `schemas.py`).
- `app/routes/__init__.py` combines all routers, and **the order matters**: `merging` (prefix `/datasets`, with static `/datasets/merge` paths) must be included before `datasets` (`/datasets/{dataset_id}`), or "merge" gets read as a dataset id.

### Two storage models (the key thing to understand)
1. **Per-dataset tables `ds_<id>`.** Generic connectors (`rest_api`, `data_gov_in`, `csv_excel`, `html_table`) write each ingest to its own table through `crud/dataset.py`. Each stage (raw → clean → master) creates a *new* `Dataset` row, linked to its inputs through `parent_ids`, with `notes` as the audit trail.
2. **Shared warehouse layer tables**, one table per stage, holding many ingests stacked together:
   - `census_raw/clean/master`: wide, one row per area. Grain differs by `level`; geography columns that don't apply to a level are NULL (`census_warehouse/schema.py`).
   - `hmis_raw/clean/master`: one row per place per reporting window (`WIDE_INDEX`), indicators as columns. The connector parses each report long (`parse_report_long`) and folds it back with `to_record_rows`. The "long" wording in `hmis_warehouse/schema.py`'s docstring predates this.
   - `dashboard_master`: the merge output.
   - `gemini_fetches/raw/clean/master`: long, one value per row. Kept completely separate from the official data.

   The `Dataset` row stays the unit of provenance. Layer rows are stamped with `_dataset_id`, `_ingested_at` and `survey_id`.

**`survey_id`** is what ties sources together. Re-fetching with the same `survey_id` **replaces** that survey's rows in the layer (`crud/warehouse_survey.py`, `dataset_crud.clear_survey`); a new `survey_id` adds a separate set. Census and HMIS fetched under the same survey describe the same places, and `merging/survey.py` joins their **master** layers (never raw/clean, which would multiply rows) into `dashboard_master`.

`/warehouse/{connector}/...` is one generic router. Each connector (`census`, `hmis`) supplies a `service` and `schema` module with the same function names (`summary`, `build_clean`, `build_master`, `export_layer`, `is_layer_table`, `RAW/CLEAN/MASTER`). To add a warehouse connector, follow that interface and register it in `_CONNECTORS` in `modules/warehouse/router.py`.

### Ingestion
- Connectors live in `modules/ingestion/connectors/`. Each returns an `IngestResult` (DataFrame + provenance). API, file, and HTML code paths deliberately share nothing.
- `detector.py` picks a connector using a fixed priority order (official API → downloadable file → raw JSON → HTML table → JS-rendered page). A page that links to a CSV counts as a file source.
- `POST /ingest/fetch` → `ingestion/service.run_fetch` routes by host. `censusindia.gov.in` streams chunks into `census_raw` and skips the full cleaning pass to keep memory flat. `hmis.mohfw.gov.in` crawls the HMIS file tree (`hmis_reports.py`; the `.xls` files are really HTML tables). Everything else goes to a `ds_<id>` table and gets a cleaned copy.
- `dynamic_site` (Playwright) is a stub that raises an error.
- Sources are rows in the `sources` table, seeded on startup from `modules/sources/seed_sources.json`. Adding a source means adding a JSON entry, not code.

### Background jobs
Long work (fetches, HMIS crawls, merges, source runs) is never done inside a request. Endpoints call `jobs/service.enqueue(...)` and return 202 with a job id; clients poll `GET /jobs/{id}`. Jobs run on a `ThreadPoolExecutor(max_workers=2)` hard-coded in `jobs/service.py` (the `job_workers` setting exists but is never read). The cap also limits load on government APIs. Each job opens its own DB session. Job types are dispatched by string in `jobs/service._run` (`source_run`, `source_run_all`, `fetch`, `merge`, `merge_clean`, `hmis_crawl`). An HMIS crawl creates one child job per branch, recorded in `result["children"]`; `jobs/lineage.py` follows that tree back to datasets. On startup, `reap_orphaned_jobs` marks interrupted jobs as failed.

### KPIs: LLMs never compute numbers
There are three KPI surfaces, all deterministic at execution time:
- `constants/kpi_catalog.py` + `.tsv`: the report template's 227 indicators. The TSV holds the template text; `FORMULAS` in Python says how to compute each one. An indicator with no formula is reported `unavailable` rather than guessed.
- `constants/dashboard_kpis.py`: a fixed set of 28 dashboard cards (KPI-01..KPI-28).
- `kpi/dynamic.py` and `kpi/claude_client.py`: Claude proposes specs through structured output (`messages.parse()` with Pydantic models; forcing a tool call returns 400 on current models). The backend checks every column name against the real frame and does the arithmetic in pandas or `analytics/service.run_computation`, which accepts only a fixed vocabulary of operations. Claude never sees the DB or writes SQL.

**Aggregation rule (`kpi/aggregation.py`)**: Census values repeat on every HMIS period row after the merge, so a census column must be summed over one row per place, while an HMIS column is summed over all rows. Ignoring this once produced an all-India population of about 4.4 billion. Every KPI surface must aggregate through this module.

### Geography and semantic matching
- `constants/district_crosswalk.tsv` is the district master. Look districts up **scoped to the state**, because names repeat across states (Aurangabad, Bilaspur, Balrampur). `unique_id` is the join key, not source-specific codes.
- `constants/canonical_fields.py` lists column-name aliases. `merging/semantic.py` matches column names deterministically with difflib in three tiers (exact alias = 1.0, fuzzy canonical, raw fuzzy capped at 0.8). To teach it a new alias, extend the dictionary instead of editing the code.
- `constants/hmis_indicators.tsv` maps HMIS prose labels to short codes, matched on a normalised label.

### Gemini (`modules/ai_data/`)
`POST /ai-data/fetch` asks Gemini (REST via `requests`, with Google Search grounding) for a table. The output is labelled as model output and stored **only** in `gemini_*` tables. It never goes into the census/HMIS warehouse, `datasets`, or `dashboard_master`.

### Website tab (`modules/scraping/`)
`POST /scrape/scan` reads one page plus the files it links to. It runs in the background as job `scrape_scan`. Plain HTTP first; a JavaScript page (`render`: `auto` / `always` / `never`) is then opened in headless Chromium (`discover_browser.py`).
- **What it collects:** tables, embedded JSON, linked files and page text.
- **Staging:** everything found is held in `data/scrape_staging/<scan_id>/` (Parquet per table), with a row per finding in `scrape_findings`.
- **Save:** `POST /scrape/scans/{id}/import`, job `scrape_import`, one request per Save. Each ticked table becomes an ordinary raw `ds_<id>` dataset plus a cleaned copy (connectors `web_table` / `web_file`). Then the staging folder and the unticked findings are deleted.
- **Expiry and restarts:** unsaved scans expire after 24h. Startup reaps interrupted scans and purges expired ones.
- **Network rules:**
  - Every request the scraper makes itself goes through `scraping/fetcher.Fetcher`.
  - Every request the browser makes goes through `browser_proxy.FilteringProxy`, a localhost proxy that applies the same address guard (Chromium is launched with `--proxy-bypass-list=<-loopback>` so even localhost goes through it). One browser at a time.
  - The address guard in `safety.py` pins DNS and refuses private, loopback and metadata addresses.
  - It obeys robots.txt and paces requests to 1 per second per host.
  - Blocks are reported, never worked around.
- **Browser setup:** `pip install -r requirements.txt` brings Playwright; the server also needs Chromium: `.venv/bin/python -m playwright install --with-deps chromium` (about 300–400 MB of disk, 0.5–1 GB of memory while a browser Search runs). Without it, Searches still work and report `browser_unavailable`. Set `PLATFORM_SCRAPE_BROWSER_SANDBOX=false` only where Chromium's sandbox can't start (e.g. running as root in Docker); `PLATFORM_SCRAPE_BROWSER_ENABLED=false` turns browser mode off. Browser tests are marked `browser` and skip when Chromium can't launch.
- **Dashboard:** saved tables do not reach the dashboard KPIs (spec §12, option B). Spec: `docs/superpowers/specs/2026-11-03-website-tab-design.md`.

### Exports
CSV exports are split by acquisition method into `data/exports/{api,web,file,merged}/`, with filenames `<method>__<connector>__<name>__<id>.csv` (`reports/naming.py`). They are written as UTF-8 with BOM so Excel shows Indian place names correctly.

## Stale parts of `platform/README.md`
- Paths `backend/app/sources.py` and `backend/app/semantic.py` now live at `app/modules/sources/` and `app/modules/merging/semantic.py`.
- Several listed endpoints no longer exist: `/datasets/{id}/profile`, `/datasets/{id}/clean`, `/datasets/merge/suggest`, `/datasets/{id}/aggregate`, `/datasets/{id}/export/csv|excel`.
- The README doesn't cover the newer surfaces: `/warehouse/{connector}`, `/ingest/fetch`, `/ai-data`, and `/kpi/{catalog,compute,dashboard,aggregate,dimensions,generate}`.

Check the routers (or `/docs`) for the current API.
