# Data Ingestion & Analytics Platform

AI-assisted platform to ingest data from APIs, portals, web pages and files,
then profile → clean → merge → analyze it, with Claude proposing KPIs that a
deterministic engine executes.

Not a "web scraper": ingestion runs through a connector framework that prefers
official APIs and downloadable files over HTML, and falls back to browser
automation only when nothing else is available.

## Pipeline

```
URL / Upload -> SOURCE DETECTION -> RAW STORAGE -> PROFILING -> CLEANING -> MERGING
             -> MASTER DATASET (SQL + files) -> ANALYTICS ENGINE
             -> KPI prompt -> CLAUDE (structured JSON) -> validated + executed
             -> Dashboard / PDF / Excel
```

## Code layout

One module per section under `app/modules/`, with shared layers around them:

```
app/
  main.py                    app factory only — no business logic
  core/config.py             settings
  db/                        base, session, init_db
  models/                    one file per table (dataset, source, job, kpi)
  crud/dataset.py            dataset persistence (files + SQL + metadata)
  constants/                 canonical_fields.py (the semantic dictionary)
  helpers/dataframe.py       snake_case, json_safe_records
  routes/__init__.py         aggregates every module router
  modules/
    ingestion/               connectors/, detector.py, service.py, router.py
    datasets/                browse datasets and rows
    profiling/               column stats
    cleaning/                deterministic cleaning
    merging/                 semantic.py + join suggestion/execution
    analytics/               deterministic aggregations
    kpi/                     claude_client.py + execution
    reports/                 Excel / PDF export
    sources/                 registry + seed_sources.json
    jobs/                    background worker pool
```

Each module holds its own `router.py`, `service.py` and `schemas.py`, so a
section can be read, changed or replaced on its own.

### Connectors (`backend/app/modules/ingestion/connectors/`)

| Connector | Handles | Status |
| --- | --- | --- |
| `rest_api` | Generic JSON REST endpoints | Working |
| `data_gov_in` | data.gov.in resource API (`DATA_GOV_IN_API_KEY`) | Working |
| `census_india` | Census of India NADA API — PCA indicators, long→wide | Working |
| `csv_excel` | CSV/TSV/XLSX uploads and direct links | Working |
| `html_table` | Static pages — picks the largest `<table>` | Working |
| `dynamic_site` | JS-rendered pages via Playwright | Interface only, not installed |

### Source detection and the priority ladder (`ingestion/detector.py`)

API access, file download and HTML parsing are separate code paths that never
mix. The detector only chooses between them, and never starts with a browser:

```
1. Official API           -> rest_api / data_gov_in / census_india
2. Downloadable CSV/Excel -> csv_excel
3. Raw JSON/XML payload   -> rest_api
4. HTML table on the page -> html_table
5. JS-rendered page       -> dynamic_site
```

A page that merely *links* to a CSV is a file source, not a scraping target,
so the probe looks for download links before considering table parsing.

`POST /ingest/probe` returns the full capability report — content type, download
links found, table count, whether the markup is unrendered JS templates, whether
TLS verification failed — plus which connector it recommends and why.

### Source registry (`backend/app/sources.py`)

Data for a real report never comes from one URL. The registry makes each
source a row — connector, config, geography, publisher, licence — so many
different portals run through one pipeline:

```
POST /sources          register a source
POST /sources/seed     load seed_sources.json (version-controlled)
POST /sources/{id}/run enqueue ingest + auto-clean -> returns a job id
POST /sources/run-all  enqueue every enabled source, collecting failures
```

Each run records `last_status` / `last_error` / `last_dataset_id` on the
source, so a batch of 30 sources shows exactly which ones broke.

Adding a source is a JSON entry, not code — see
`app/modules/sources/seed_sources.json`.

### Background jobs (`backend/app/modules/jobs/`)

A national Census pull is ~33 paginated calls, so ingestion never blocks an
HTTP request. `POST /sources/{id}/run` returns a job id immediately; poll
`GET /jobs/{id}` for live progress (`"Fetching page 22/33 (110,000/163,200
records)"`). Work runs on a small thread pool — capped at 2 workers, which is
also politeness toward the source portals. Jobs persist in SQLite, so status
survives a restart; the shape matches a real queue if this outgrows threads.

### Semantic layer (`backend/app/semantic.py`)

Canonical field dictionary + fuzzy matching, so `District`, `district_name`
and `DISTRICT_NM` resolve to the same field and datasets merge correctly.
Matching is deterministic (difflib, no LLM call) in three tiers: exact alias
(confidence 1.0) → fuzzy canonical (ratio) → raw-name fuzzy (capped at 0.8).

### Claude's role

Claude **never** touches the database or writes SQL. It receives the schema,
profile and a row sample, and returns a structured tool call naming *which*
computation to run. `analytics.run_computation` — a fixed vocabulary of
`aggregate` / `top_n` / `correlation` / `describe` — executes it. Analytics
stay reproducible and auditable; a prompt can't become code execution.

## Run

```bash
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

Interactive API docs at http://127.0.0.1:8000/docs

Set `ANTHROPIC_API_KEY` to enable KPI generation; every other endpoint works
without it (`/kpi` returns 503 with a clear message when it's unset).

**Set `DATA_GOV_IN_API_KEY` before ingesting any data.gov.in source.** The
public sample key from their docs is shared by every caller and rate-limits
(429/502) almost immediately on a real ingest. Registration is free at
https://data.gov.in.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/connectors` | List available connectors |
| POST | `/ingest/probe` | Capability report for a URL + recommended connector |
| GET | `/sources` | List registered sources (filter by `?theme=`) |
| POST | `/sources` | Register a new source |
| POST | `/sources/{id}/run` | Enqueue ingest + clean → job id |
| POST | `/sources/run-all` | Enqueue every enabled source → job id |
| GET | `/jobs` / `/jobs/{id}` | Job status and live progress |
| POST | `/ingest/url` | Ingest from a URL (auto-detects connector) |
| POST | `/ingest/upload` | Ingest an uploaded CSV/Excel/JSON file |
| GET | `/datasets` | List datasets (filter by `?stage=raw\|clean\|master`) |
| GET | `/datasets/{id}/rows` | Paginated rows |
| GET | `/datasets/{id}/profile` | Row/column stats, nulls, duplicates, distributions |
| POST | `/datasets/{id}/clean` | Produce a cleaned dataset (new row, original untouched) |
| POST | `/datasets/merge/suggest` | Preview join matches with confidence scores |
| POST | `/datasets/merge` | Merge into a master dataset |
| POST | `/datasets/{id}/aggregate` | Run one aggregation (dashboard charts) |
| GET | `/datasets/{id}/export/csv` | Export one dataset as CSV |
| POST | `/exports/csv` | Write every dataset to its own CSV, foldered by method |
| GET | `/datasets/{id}/export/excel` | Export data + profile as XLSX |
| POST | `/kpi` | KPI prompt → Claude → validated → executed |
| GET | `/kpi/{id}/export/pdf` | KPI report as PDF |

## Storage & lineage

Raw payloads land in `backend/data/raw/<dataset_id>/` and are never modified.
Each stage creates a *new* dataset row (`raw` → `clean` → `master`) linked to
its inputs via `parent_ids`, with `notes` recording what the stage did — so any
number traces back to the source file and URL it came from.

Dataset rows live in SQLite (`backend/data/warehouse/platform.db`), one table
per dataset (`ds_<id>`). For production scale this is the seam where PostgreSQL
(metadata) + DuckDB/Parquet (analytical data) would slot in.

## extract.py — one URL in, one CSV out

A standalone CLI for when you just want data from a URL, without running the
server. Each acquisition method is a **separate function** in the script:
`extract_from_api`, `extract_from_file`, `extract_from_web`,
`extract_from_census` — nothing shared between them beyond the result shape.

```bash
# auto-detect: probes the URL and follows the priority ladder
python extract.py https://example.gov.in/data.json

# force a specific extractor
python extract.py https://example.gov.in/report          --type web
python extract.py https://example.gov.in/sheet.xlsx      --type file
python extract.py https://api.data.gov.in/resource/<id>  --type api

# a local file
python extract.py ./downloads/districts.csv

# Census needs area codes, not a URL
python extract.py census://districts             # all 640 districts
python extract.py census://districts?state=10    # one state

# normalize headers/types before writing
python extract.py <target> --clean
```

Flags: `--type` (force an extractor), `--out` (output dir), `--name`
(filename), `--table-index` (which HTML table), `--clean` (snake_case headers,
trim, numeric coercion, drop empties/duplicates — off by default so the CSV
mirrors the source).

If auto-detect finds a page that *links* to a spreadsheet, it follows that
download link rather than trying to parse the page.

## CSV output

Each acquisition method exports to its **own file**, in its own folder — an API
pull is never blended with a scraped page or an uploaded file:

```
data/exports/
  api/     api__census_2011_bihar_districts_pca__74419dc6.csv     (REST, data.gov.in, Census)
  web/     web__list_of_states_by_population__3bf4c7ab.csv        (HTML tables, headless browser)
  file/    file__pop_csv__4438fb08.csv                            (CSV/Excel upload or download)
  merged/  merged__master_census_nfhs__9a8249cf.csv               (master datasets)
```

Filenames follow `<method>__<name>__<dataset id>.csv`, so provenance is visible
without opening the file, and two sources with similar names never collide.

- `GET /datasets/{id}/export/csv` — download one dataset (filename carries the method)
- `POST /exports/csv` — write them all to disk; filter with `?category=api|web|file|merged`
  or `?stage=raw|clean|master`

CSVs are written UTF-8 with BOM so Excel renders Indian place names correctly.

## Registered sources

| Source | Theme | Coverage | Status |
| --- | --- | --- | --- |
| Census 2011 PCA (all India) | Demographic | 640 districts x 85 indicators | **Verified end-to-end** — national total matches the official 1,210,854,977 |
| Census 2011 PCA (Bihar) | Demographic | 38 districts x 85 indicators | Verified; disabled (superseded) |
| NFHS-5 district factsheets | RMNCH/Nutrition/NCDs | 707 districts x 109 fields | Schema verified; rows blocked on API key |
| NFHS-5 state/UT factsheets | RMNCH/Nutrition/NCDs | State level | Resource id confirmed; blocked on API key |
| India TB Report 2023 | Communicable diseases | 32 resources | Catalog verified; blocked on API key |
| NACO adult HIV prevalence | Communicable diseases | State/UT | Catalog verified; blocked on API key |
| NACO HIV infections (PLHIV) | Communicable diseases | State/UT | Catalog verified; blocked on API key |
| NACO AIDS-related deaths | Communicable diseases | State/UT | Catalog verified; blocked on API key |
| AYUSH hospitals/beds/dispensaries | Infrastructure | State/UT | Catalog verified; blocked on API key |
| Blood Bank Directory | Infrastructure | Facility level | Catalog verified; blocked on API key |
| Road Accidents in India 2022 | NCDs | 58 resources | Catalog verified; **disabled** — MoRTH 2022, template cites NCRB 2021 |

"Blocked on API key" means the resource ids and schemas are confirmed real, but
`api.data.gov.in` refuses bulk reads on the shared sample key. Set
`DATA_GOV_IN_API_KEY` and re-run.

NFHS-5 keys on `State_UT` / `District_Names`, which the semantic layer resolves
to `state` / `district` at 1.0 confidence — so it merges with Census without
manual column mapping.

### HMIS

HMIS is not one dataset. data.gov.in publishes it as **one catalog per state**,
each holding one resource *per district per month* ("Item wise Cumulative
Comparison Report for Nicobar District…") — 876 API-enabled HMIS resources in
the first 1,200 health records alone, and 43 resources in the Andaman catalog
alone. The `data_gov_catalog` connector handles this shape: enumerate a
catalog's resources, fetch each, stack them, and tag every row with
`_resource_title` / `_resource_id` for lineage. Enumeration is verified; row
fetching needs an API key. Use `max_resources` to cap these catalogs.

### Sources NOT found on data.gov.in

Scanning all 1,976 health-sector catalogs turned up nothing for these, so they
need a non-data.gov.in connector (their own portal, or a published PDF/Excel):

- **RHS (Rural Health Statistics)** — SHC/PHC/CHC/SDH/DH/HWC counts, i.e. most
  of the Infrastructure sheet. Published by MoHFW, not in the OGD catalog.
- **MoTA / tribal** — tribal PHCs, tribal facility counts, tribal HRH.
- **ABDM / ABHA** — ABHA IDs, registered professionals, registered facilities.
- **e-Rakthkosh** — the blood-bank source the template actually cites (the
  Blood Bank Directory above is a different, National Health Portal dataset).
- **Anganwadi (AWC) counts**, **government medical college counts**.

## Not built yet

Multi-user auth/projects, scheduled + incremental ingestion, dataset
versioning, source license/compliance capture, fuzzy *value* matching
(`New Delhi` vs `New Delhi District`), and the React frontend.
