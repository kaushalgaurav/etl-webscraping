# API testing guide

How to call every endpoint of the backend, module by module: method, address, headers, request body or query parameters, a ready-to-run `curl` command, and what comes back.

All 57 endpoints are covered, and every example body below has been checked against the app's own request validation. Field names, types and defaults are taken from the app's own OpenAPI description (`/openapi.json`), so they match the code.

---

## Before you start

### 1. Start the server

From `platform/backend/` (the database tables must exist first, so run `alembic upgrade head` once if you haven't):

```bash
venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

### 2. Base URL

```bash
BASE=http://127.0.0.1:8000
```

Every address works both with and without the `/etl` prefix locally (`/health` and `/etl/health` both answer). On the deployed server behind the proxy, use the `/etl` prefix.

### 3. Headers

There is **no login, API key or token** on any endpoint. The only headers you need:

| When | Header |
|---|---|
| Request has a JSON body | `Content-Type: application/json` |
| File upload (`/ingest/upload`) | `multipart/form-data` (curl sets it for you with `-F`) |
| Everything else | none |

Some features need keys **on the server side**, not in your request: Claude endpoints need `ANTHROPIC_API_KEY`, Gemini endpoints need `GEMINI_API_KEY` (both in `.env`), and data.gov.in needs `DATA_GOV_IN_API_KEY` exported in the terminal that starts the server.

### 4. Easier ways to test

- **Swagger UI:** open `http://127.0.0.1:8000/docs`. Every endpoint has a "Try it out" button with the request form already built. The fetch endpoint even has ready-made examples in a dropdown.
- **Postman or Insomnia:** import `http://127.0.0.1:8000/openapi.json`. It creates one request per endpoint, grouped by module.
- **curl:** the commands below. Add `| python3 -m json.tool` to pretty-print the reply.

### 5. Background jobs: the 202 pattern

Slow endpoints don't wait for the work. They reply **202** straight away with a job, and you check on it:

```json
{
  "id": "3f9c…",
  "job_type": "fetch",
  "status": "queued",
  "source_id": null,
  "dataset_id": null,
  "progress": null,
  "result": null,
  "error": null,
  "created_at": "2026-11-03T07:10:00",
  "started_at": null,
  "finished_at": null
}
```

Poll `GET /jobs/{id}` until `status` is `succeeded` or `failed`. Statuses go `queued` → `running` → `succeeded` | `failed`. While running, `progress` shows text like "page 22 of 33". When finished, `dataset_id` and `result` say what was produced.

Endpoints that work this way: `POST /ingest/fetch`, `POST /sources/{id}/run`, `POST /sources/run-all`, `POST /datasets/merge`, `POST /datasets/merge/clean`. Gemini's `POST /ai-data/fetch` works the same way but has its own status address (see the AI data module).

### 6. Status codes you'll see

| Code | Meaning |
|---|---|
| 200 | Done, answer in the body |
| 202 | Job queued, poll `/jobs/{id}` |
| 400 | Request understood but can't be done (e.g. "build the clean layer first") |
| 404 | Not found (unknown id, or no data for that survey) |
| 422 | Body or parameters don't match the expected shape; `detail` lists which field |
| 502 | An outside service failed: the Census site (for `/census/...`) or Claude (for `/kpi`, `/kpi/generate`) |
| 503 | A key is missing on the server (Claude or Gemini not configured) |

### 7. Recommended order for a full test

1. `GET /health`
2. Census: `POST /ingest/fetch` (survey 1, district level) → poll the job → `POST /warehouse/census/rebuild?survey_id=1`
3. HMIS: `POST /ingest/fetch` (survey 1, one small path) → poll → `POST /warehouse/hmis/rebuild?survey_id=1`
4. `POST /datasets/merge` with `survey_id: 1` → poll → `GET /datasets/merge?survey_id=1`
5. `GET /kpi/dashboard?survey_id=1`, `POST /kpi/compute`
6. `POST /exports/csv?layer=dashboard_master&survey_id=1`

---

## Contents

1. [Meta](#1-meta)
2. [Ingestion](#2-ingestion) (`app/modules/ingestion/router.py`)
3. [Jobs](#3-jobs) (`app/modules/jobs/router.py`)
4. [Sources](#4-sources) (`app/modules/sources/router.py`)
5. [Warehouse](#5-warehouse) (`app/modules/warehouse/router.py`)
6. [Datasets](#6-datasets) (`app/modules/datasets/router.py`)
7. [Merging](#7-merging) (`app/modules/merging/router.py`)
8. [KPI](#8-kpi) (`app/modules/kpi/router.py`)
9. [Reports](#9-reports) (`app/modules/reports/router.py`)
10. [AI data (Gemini)](#10-ai-data-gemini) (`app/modules/ai_data/router.py`)
11. [Website scraping](#11-website-scraping) (`app/modules/scraping/router.py`)

---

## 1. Meta

### `GET /health`

Checks the server is up. Doesn't touch the database.

```bash
curl -s "$BASE/health"
```

Reply: `{"status": "ok"}`

---

## 2. Ingestion

### `GET /connectors`

Lists the connectors the platform has.

```bash
curl -s "$BASE/connectors"
```

### `POST /ingest/probe`

Checks which ways a URL can be read (API, file, HTML table, JavaScript page) and which one would be used. Reads the page but saves nothing.

| Field | Type | Required |
|---|---|---|
| `url` | string | yes |

```bash
curl -s -X POST "$BASE/ingest/probe" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/List_of_states_and_union_territories_of_India_by_population"}'
```

### `POST /ingest/fetch` (202, background job)

**The main way to bring data in.** One body shape for every kind of source.

| Field | Type | Default | Used for |
|---|---|---|---|
| `survey_id` | integer | **required** | Which survey this belongs to. Re-running with the same id **replaces** that survey's rows. |
| `type` | `api` \| `csv` \| `html` | **required** | `api` for Census, HMIS, data.gov.in, JSON APIs; `csv` for a CSV/Excel link; `html` for a web page table |
| `url` | string | **required** | See examples below |
| `name` | string | auto | Dataset name |
| `level` | `district` \| `subdistrict` \| `village_town` \| `ward` | `district` | Census only |
| `state` | integer | all India | Census only. State code (see `GET /census/districts`) |
| `district` | integer | none | Census only. Needs `state` too, or you get 400 |
| `urbrur` | `0` \| `1` \| `2` \| `null` | `0` | Census only. 0 total, 1 rural, 2 urban, null all three |
| `verify_ssl` | boolean | `false` | Census only. The Census site's certificate chain is incomplete |
| `path` | string | whole tree | HMIS only. Folder in the Standard Reports tree |
| `max_files_per_chunk` | integer | no limit | HMIS only. Cap files per folder, for a trial run |
| `file_pattern` | string | all files | HMIS only. Filename filter, e.g. `ALL.xls` |
| `crawl` | boolean | `false` | HMIS only. Split a named `path` into one job per branch |
| `refresh` | boolean | `false` | HMIS only. Re-fetch branches already ingested |
| `batch_by` | `report_type` \| `year` \| `period` | `year` | HMIS whole-tree crawl only. How to split into jobs |
| `report_type` | string | F.1 report | HMIS crawl only. Which report folder |
| `last_years` | integer \| null | `2` | HMIS crawl only. Only the N newest years; null for all years back to 2008-09 |

How the URL decides what happens:

- `censusindia.gov.in` → Census path, rows go into `census_raw`. Must be `type: api`.
- `hmis.mohfw.gov.in` → HMIS path, rows go into `hmis_raw`. With no `path` (or `crawl: true`) you get a **planning job** (`job_type: hmis_crawl`) that queues one child job per branch and succeeds as soon as they're queued. Watch the children in `GET /jobs`.
- Anything with `data.gov.in` and `type: api` → data.gov.in connector.
- Any other `type: api` → generic JSON API connector.
- `type: csv` / `type: html` → file or web page connector. These go into their own `ds_<id>` table plus a cleaned copy.

**Census, all-India districts** (about 33 calls, around a minute):

```bash
curl -s -X POST "$BASE/ingest/fetch" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 1, "type": "api", "url": "https://censusindia.gov.in/nada/index.php/api/", "level": "district"}'
```

**Census, villages and towns of one state** (state 10 = Bihar). Village level for all India is about 11,000 calls and takes hours, so test with one state:

```bash
curl -s -X POST "$BASE/ingest/fetch" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 2, "type": "api", "url": "https://censusindia.gov.in/nada/index.php/api/", "level": "village_town", "state": 10}'
```

**HMIS, one small folder** (good first test, finishes quickly):

```bash
curl -s -X POST "$BASE/ingest/fetch" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 1, "type": "api", "url": "https://hmis.mohfw.gov.in/", "path": "Standard Reports/9~F.1. Performance of Key HMIS Indicators(upto District Level)/2008-2009/MonthUpToDecember/A & N Islands", "file_pattern": "ALL.xls"}'
```

**HMIS, crawl the F.1 report for the last 2 years, one job per year:**

```bash
curl -s -X POST "$BASE/ingest/fetch" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 3, "type": "api", "url": "https://hmis.mohfw.gov.in/", "batch_by": "year", "file_pattern": "ALL.xls", "report_type": "9~F.1. Performance of Key HMIS Indicators(upto District Level)", "last_years": 2}'
```

HMIS figures are cumulative within a year (MonthUpToJune, UpToSeptember, UpToDecember, UpToMarch). Filter to one `period` before adding them up.

**CSV file link:**

```bash
curl -s -X POST "$BASE/ingest/fetch" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 5, "type": "csv", "url": "https://example.com/data.csv"}'
```

**Web page table:**

```bash
curl -s -X POST "$BASE/ingest/fetch" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 6, "type": "html", "url": "https://en.wikipedia.org/wiki/List_of_states_and_union_territories_of_India_by_population"}'
```

Reply: a job (see [the 202 pattern](#5-background-jobs-the-202-pattern)).

The endpoint's own description in `/docs` mentions `GET /datasets/{dataset_id}/export/csv` for downloading the result. That address no longer exists; use `GET /datasets/{id}/rows` or `POST /exports/csv` instead.

### `GET /census/districts`

State and district codes for the Census API. One quick call to the Census site.

| Query | Type | Required |
|---|---|---|
| `state` | integer | no (omit for every state) |

```bash
curl -s "$BASE/census/districts?state=10"
```

### `GET /census/geosearch`

Finds Census area codes (state, district, sub-district, village) by name.

| Query | Type | Default |
|---|---|---|
| `areaname` | string | **required** |
| `limit` | integer | 50 |

```bash
curl -s "$BASE/census/geosearch?areaname=Patna&limit=10"
```

### `POST /ingest/url` (immediate, no job)

Fetches a URL inside the request and saves it as a raw dataset. Fine for small sources; use `/ingest/fetch` for anything big. Doesn't work for Census (it needs area codes; use `/ingest/fetch`).

| Field | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | |
| `connector` | string | no | Force one instead of auto-detecting: `rest_api`, `data_gov_in`, `csv_excel`, `html_table` |
| `name` | string | no | Dataset name |

```bash
curl -s -X POST "$BASE/ingest/url" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/List_of_states_and_union_territories_of_India_by_population", "connector": "html_table", "name": "Wikipedia state population"}'
```

Reply: the dataset (`id`, `name`, `stage`, `connector`, `row_count`, `column_count`, …).

### `POST /ingest/upload` (immediate, multipart)

Uploads a CSV, TSV or Excel file. Largest size is `PLATFORM_MAX_UPLOAD_MB` (default 50).

| Form field | Type | Required |
|---|---|---|
| `file` | file | yes |
| `connector` | string | no |
| `name` | string | no |

```bash
curl -s -X POST "$BASE/ingest/upload" \
  -F "file=@/path/to/your/data.csv" \
  -F "name=My uploaded data"
```

Don't set `Content-Type` yourself here; `-F` sets the multipart header and boundary.

---

## 3. Jobs

### `GET /jobs`

Recent jobs, newest first.

| Query | Type | Default |
|---|---|---|
| `status` | `queued` \| `running` \| `succeeded` \| `failed` | all |
| `limit` | integer | 50 |

```bash
curl -s "$BASE/jobs?status=running&limit=20"
```

### `GET /jobs/{job_id}`

One job's status and progress. This is what you poll.

```bash
curl -s "$BASE/jobs/JOB_ID"
```

A small polling loop:

```bash
JOB=JOB_ID
while true; do
  S=$(curl -s "$BASE/jobs/$JOB" | python3 -c 'import sys,json; j=json.load(sys.stdin); print(j["status"], "|", j["progress"])')
  echo "$S"; case "$S" in succeeded*|failed*) break;; esac; sleep 5
done
```

---

## 4. Sources

### `GET /sources`

The registered sources (14 are loaded from `seed_sources.json` on startup).

| Query | Type | Required |
|---|---|---|
| `theme` | string | no, e.g. `Demographic` |

```bash
curl -s "$BASE/sources?theme=Demographic"
```

### `POST /sources`

Registers a new source you can run again later.

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Must be unique |
| `connector` | string | yes | `census_india`, `data_gov_in`, `data_gov_catalog`, `rest_api`, `csv_excel`, `html_table` |
| `config` | object | no | `{"url": "..."}` for URL connectors, or settings such as `{"state": 10, "level": "district"}` |
| `geo_level`, `theme`, `publisher`, `license`, `source_page`, `notes` | string | no | Descriptive |
| `enabled` | boolean | no, default `true` | Disabled sources are skipped by run-all |

```bash
curl -s -X POST "$BASE/sources" \
  -H "Content-Type: application/json" \
  -d '{"name": "Census 2011 - Bihar districts", "connector": "census_india", "config": {"state": 10, "level": "district", "urbrur": 0, "census_year": 2011}, "geo_level": "district", "theme": "Demographic", "publisher": "Office of the Registrar General & Census Commissioner, India"}'
```

A web page source:

```bash
curl -s -X POST "$BASE/sources" \
  -H "Content-Type: application/json" \
  -d '{"name": "Wikipedia - state population table", "connector": "html_table", "config": {"url": "https://en.wikipedia.org/wiki/List_of_states_and_union_territories_of_India_by_population"}, "theme": "Demographic"}'
```

### `POST /sources/seed`

Loads `seed_sources.json` again. Names already present are skipped. No body.

```bash
curl -s -X POST "$BASE/sources/seed"
```

### `POST /sources/{source_id}/run` (202, background job)

Fetches one source and makes a cleaned copy. Get the id from `GET /sources`. No body.

```bash
curl -s -X POST "$BASE/sources/SOURCE_ID/run"
```

### `POST /sources/run-all` (202, background job)

Runs every enabled source. A failure is recorded and the rest continue. No body. This includes data.gov.in sources, which need `DATA_GOV_IN_API_KEY`.

```bash
curl -s -X POST "$BASE/sources/run-all"
```

---

## 5. Warehouse

One set of addresses for every warehouse: replace `{connector}` with `census`, `hmis` or `web` (saved Website tables). Every endpoint takes an optional `survey_id` query parameter; without it, it acts on every survey.

For `web`, `clean`, `master` and `rebuild` also take `table` (a `web_table_id`) to act on one Website table; other connectors answer 400 to it. Website layers are `web_raw` (each value as found), `web_clean` (numbers, matched to the district list) and `web_master` (matched rows with a year).

### `GET /warehouse/web/tables?survey_id=` and `GET /warehouse/web/years`

`/tables` lists a survey's Website tables for the Fetched Data rows and the Merge tick boxes: `web_table_id`, `name`, `page_url`, `state_name` / `state_column`, `year` / `year_column`, `indicator_column`, `status` (`saved` → `cleaned`), `rows`, `values`, `match_summary` (`matched`, `total`, `unmatched`, `year_unrecognised`, `duplicate_places`), `mergeable` and `reason` (`no district column`, `not cleaned`, `no district matched`, `no year recognised`, or null). `survey_id` is required.

`/years` lists the financial years a table can be saved under (`2005-2006` … the current one), the same form as HMIS's `report_year`.

```bash
curl -s "$BASE/warehouse/web/tables?survey_id=1"
curl -s -X POST "$BASE/warehouse/web/clean?survey_id=1&table=WEB_TABLE_ID"
curl -s -X POST "$BASE/warehouse/web/master?survey_id=1"
```

### `GET /warehouse/{connector}`

Row counts for the raw, clean and master tables.

```bash
curl -s "$BASE/warehouse/census?survey_id=1"
```

### `POST /warehouse/{connector}/rebuild`

Clean, then master. **The usual step after a fetch.** No body.

```bash
curl -s -X POST "$BASE/warehouse/census/rebuild?survey_id=1"
curl -s -X POST "$BASE/warehouse/hmis/rebuild?survey_id=1"
```

### `POST /warehouse/{connector}/clean`

Rebuilds only the clean table from raw. The body is optional. Without it, the standard steps run.

| Body field | Type | Notes |
|---|---|---|
| `operations` | list of strings | Steps in order. Choices: `standardize_columns`, `trim_whitespace`, `normalize_nulls`, `drop_empty_columns`, `drop_empty_rows`, `coerce_numeric`, `drop_duplicates`, `standardize_case`, `standardize_states` |

```bash
# standard steps
curl -s -X POST "$BASE/warehouse/census/clean?survey_id=1"

# chosen steps
curl -s -X POST "$BASE/warehouse/census/clean?survey_id=1" \
  -H "Content-Type: application/json" \
  -d '{"operations": ["standardize_columns", "trim_whitespace", "normalize_nulls", "coerce_numeric", "standardize_states"]}'
```

### `POST /warehouse/{connector}/master`

Rebuilds only the master table from clean (one row per place, newest fetch wins). Needs the clean table to exist. No body.

```bash
curl -s -X POST "$BASE/warehouse/hmis/master?survey_id=1"
```

### `POST /warehouse/{connector}/export`

Writes the tables to CSV files under `data/exports/`, a folder per table. The body is optional.

| Body field | Type | Notes |
|---|---|---|
| `layers` | list of strings | e.g. `["census_master"]`; default all three |
| `job_id` | string | Only rows produced by this job (and its child jobs) |
| `dataset_ids` | list of strings | Only rows from these datasets |

```bash
curl -s -X POST "$BASE/warehouse/census/export?survey_id=1" \
  -H "Content-Type: application/json" \
  -d '{"layers": ["census_master"]}'
```

### `GET /warehouse/{connector}/rows`

Browse rows of one table, a page at a time.

| Query | Type | Default | Notes |
|---|---|---|---|
| `layer` | string | the raw table | `census_raw`, `census_clean`, `census_master` (or the `hmis_` ones) |
| `survey_id` | integer | all | |
| `job_id` | string | all | Rows produced by one job |
| `limit` | integer | 50 | Up to 1000 |
| `offset` | integer | 0 | |
| `count` | boolean | `true` | Also return the total row count. `false` is faster on big tables |
| `order_by` | string, repeatable | the table's key columns | `&order_by=state_name&order_by=district_name` |
| *any column name* | string | | Filters on that column, e.g. `level=district`, `state_name=Bihar`. An unknown column gives 400 |

```bash
curl -s "$BASE/warehouse/census/rows?layer=census_master&survey_id=1&level=district&state_name=Bihar&limit=5"
```

---

## 6. Datasets

Browse the per-dataset tables (`ds_<id>`) and every dataset's description.

### `GET /datasets`

| Query | Type | Notes |
|---|---|---|
| `stage` | string | `raw`, `clean`, `master` |

```bash
curl -s "$BASE/datasets?stage=raw"
```

### `GET /datasets/{dataset_id}`

```bash
curl -s "$BASE/datasets/DATASET_ID"
```

### `GET /datasets/{dataset_id}/rows`

| Query | Type | Default |
|---|---|---|
| `limit` | integer | 50 |
| `offset` | integer | 0 |

```bash
curl -s "$BASE/datasets/DATASET_ID/rows?limit=10&offset=0"
```

---

## 7. Merging

Joins Census, HMIS and (optionally) chosen Website tables' master tables for one survey into `dashboard_master`. Each has to be built (rebuild / master) for that survey first.

With `web_tables`, HMIS and Website rows join on district **and year** (one shared `year` column), and Census joins on district. Website columns are named `web_<table name>_<indicator>`; the same table saved for several years fills the same columns. Merge refuses when `dashboard_master` would pass 1,400 columns, or when two ticked tables with the same name share an indicator for the same year.

### `POST /datasets/merge` (202, background job)

Give **either** `survey_id` (with optional `connectors`) **or** `dataset_ids`, not both.

| Field | Type | Notes |
|---|---|---|
| `survey_id` | integer | The survey to merge |
| `connectors` | list of strings | e.g. `["census", "hmis"]`; default every warehouse with rows for that survey |
| `dataset_ids` | list of strings | Merge these specific datasets instead |
| `web_tables` | list of strings | Website tables to include (`web_table_id`s from `GET /warehouse/web/tables`). Needs `survey_id` |
| `join_key` | string | Column or field name to join on, e.g. `district`. Default: worked out automatically |
| `name` | string | Name for the record of this run |

```bash
curl -s -X POST "$BASE/datasets/merge" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 1, "connectors": ["census", "hmis"]}'
```

Two specific datasets:

```bash
curl -s -X POST "$BASE/datasets/merge" \
  -H "Content-Type: application/json" \
  -d '{"dataset_ids": ["DATASET_ID_1", "DATASET_ID_2"], "join_key": "district"}'
```

### `GET /datasets/merge`

The merged rows for a survey. Same paging and filtering as warehouse rows: `limit`, `offset`, `count`, `order_by`, plus any column name as a filter.

```bash
curl -s "$BASE/datasets/merge?survey_id=1&limit=5"
```

Use `limit=1` once to see the real column names; you need them for `/kpi/aggregate`.

### `POST /datasets/merge/clean` (202, background job)

Re-cleans the rows already in `dashboard_master`. A body is required, but every field is optional, so `{}` works.

| Field | Type | Notes |
|---|---|---|
| `survey_id` | integer | Only this survey; omit for all |
| `operations` | list of strings | Same choices as warehouse clean |
| `name` | string | |

```bash
curl -s -X POST "$BASE/datasets/merge/clean" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 1}'
```

### `GET /datasets/merge/clean`

Rows after the re-clean. Same parameters as `GET /datasets/merge`.

```bash
curl -s "$BASE/datasets/merge/clean?survey_id=1&limit=5"
```

---

## 8. KPI

All of these read `dashboard_master`, so run the merge for the survey first.

### `GET /kpi/catalog`

The 227 indicators from the report template. Doesn't need any data.

| Query | Type | Notes |
|---|---|---|
| `theme` | string | `Demographic`, `RMNCH`, `NCDs`, `Socio-economic`, `Communicable diseases`, `HRH`, `Infrastructire` (spelled this way in the template), `Nutrition` |
| `status` | string | `computed`, `proxy`, `unavailable` |

```bash
curl -s "$BASE/kpi/catalog?theme=Demographic&status=computed"
```

### `POST /kpi/compute`

Calculates the template indicators for a survey, district by district. No AI.

| Field | Type | Default | Notes |
|---|---|---|---|
| `survey_id` | integer | **required** | |
| `themes` | list of strings | all | e.g. `["Demographic", "RMNCH"]` |
| `shape` | `long` \| `wide` | `long` | `wide` = one row per district, one column per indicator, like the template sheets |

```bash
curl -s -X POST "$BASE/kpi/compute" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 1, "themes": ["Demographic"], "shape": "wide"}'
```

### `GET /kpi/dashboard`

The 28 dashboard cards (KPI-01 to KPI-28). No AI.

| Query | Type | Default |
|---|---|---|
| `survey_id` | integer | **required** |
| `by_place` | boolean | `false` (`true` = also per district) |

```bash
curl -s "$BASE/kpi/dashboard?survey_id=1"
```

### `POST /kpi/aggregate`

For a configurable dashboard: you describe each KPI by column names and one aggregation, plus filters and grouping, and get values overall and per group. No AI, and nothing you send is run as code.

| Field | Type | Notes |
|---|---|---|
| `survey_id` | integer | **required** |
| `kpis` | list | **required**. Each: `id`, `aggregation` (`sum`, `mean`, `median`, `min`, `max`, `count`, `ratio_of_sums`), `numerator` (column), optional `denominator` (column) and `scale` (e.g. 100, 1000) |
| `sources` | list | **required**. Each: `suffix` (columns ending with this belong to the source; `""` = fallback), `place_key` (columns that identify one place), `time` (columns it varies over) |
| `dimensions` | object | Name → `{"columns": [...], "tag": false}`. The first non-empty column wins |
| `filters` | object | Dimension name → list of allowed values |
| `time` | object | Time slice, e.g. `{"report_year_hmis": "2019-2020", "period_hmis": "MonthUpToMarch"}` |
| `group_by` | list of strings | Dimension names |
| `labels` | object | Label name → columns to show per group |
| `pair_on` | string | Dimension on which ratio numerators and denominators are paired |
| `previous_time` | object | A comparison slice, e.g. the same period last year |
| `trend_times` | list of objects | Slices for a trend line (overall values only) |
| `trend_kpis` | list of strings | KPI ids for the trend; empty = all |

Example: total population and female share, per state, for Bihar and Uttar Pradesh. The column names below are from the Census side of `dashboard_master` (suffix `_census`). Check yours with `GET /datasets/merge?survey_id=1&limit=1`.

```bash
curl -s -X POST "$BASE/kpi/aggregate" \
  -H "Content-Type: application/json" \
  -d '{
    "survey_id": 1,
    "kpis": [
      {"id": "population", "aggregation": "sum", "numerator": "total_population_both_sexes_census"},
      {"id": "female_share", "aggregation": "ratio_of_sums", "numerator": "total_population_females_census", "denominator": "total_population_both_sexes_census", "scale": 100}
    ],
    "sources": [
      {"suffix": "_census", "place_key": ["unique_id"], "time": []},
      {"suffix": "_hmis", "place_key": ["unique_id"], "time": ["report_year_hmis", "period_hmis"]}
    ],
    "dimensions": {"state": {"columns": ["state_name_census", "state_name_hmis"]}},
    "filters": {"state": ["Bihar", "Uttar Pradesh"]},
    "group_by": ["state"]
  }'
```

An unknown column or aggregation gives 422 with the reason.

### `POST /kpi/dimensions`

What a dashboard can filter on: the distinct values of each dimension and the available time slices.

| Field | Type | Notes |
|---|---|---|
| `survey_id` | integer | **required** |
| `dimensions` | object | Same shape as in `/kpi/aggregate` |
| `labels` | object | Same shape as in `/kpi/aggregate` |
| `time_columns` | list of strings | Up to 4 |

```bash
curl -s -X POST "$BASE/kpi/dimensions" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 1, "dimensions": {"state": {"columns": ["state_name_census", "state_name_hmis"]}, "district": {"columns": ["district_name_census", "district_name_hmis"]}}, "time_columns": ["report_year_hmis", "period_hmis"]}'
```

### `POST /kpi/generate` (uses Claude)

Claude studies the merged data and proposes ranked dashboard KPIs. Every column it names is checked, and the numbers are computed by the backend. Needs `ANTHROPIC_API_KEY`.

| Field | Type | Default | Notes |
|---|---|---|---|
| `survey_id` | integer | **required** | |
| `max_kpis` | integer | 10 | Between 3 and 20 |
| `focus` | string | none | e.g. `"maternal health"` |

```bash
curl -s -X POST "$BASE/kpi/generate" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 1, "max_kpis": 8, "focus": "maternal health"}'
```

### `POST /kpi` (uses Claude)

Ask a question in plain words. Claude picks calculations from a fixed menu and the backend runs them. The request is saved, so you can read it again and export it as a PDF. Needs `ANTHROPIC_API_KEY`.

| Field | Type | Required |
|---|---|---|
| `survey_id` | integer | yes |
| `prompt` | string | yes |

```bash
curl -s -X POST "$BASE/kpi" \
  -H "Content-Type: application/json" \
  -d '{"survey_id": 1, "prompt": "Which 10 districts have the lowest female literacy?"}'
```

The reply includes an `id`; use it with `GET /kpi/{id}` and the PDF export.

### `GET /kpi`

Past KPI requests, newest first.

| Query | Type | Default |
|---|---|---|
| `survey_id` | integer | all |
| `limit` | integer | 50 |
| `offset` | integer | 0 |

```bash
curl -s "$BASE/kpi?survey_id=1&limit=10"
```

### `GET /kpi/{kpi_id}`

One KPI request with its full results.

```bash
curl -s "$BASE/kpi/KPI_ID"
```

---

## 9. Reports

### `POST /exports/csv`

Writes CSV files under `data/exports/` on the server and returns the list of files written. Query parameters only, no body. There are two modes:

| Query | Notes |
|---|---|
| `layer` | Give it to export one whole table as one CSV: `census_raw`, `census_clean`, `census_master`, `hmis_raw`, `hmis_clean`, `hmis_master`, `dashboard_master`. Extra column filters work here, e.g. `&state_name=Bihar` |
| `survey_id` | Scope to one survey (both modes) |
| `stage` | Without `layer` only: `raw`, `clean`, `master` |
| `category` | Without `layer` only: `api`, `web`, `file`, `merged` |

```bash
# one file for the whole merged table of survey 1
curl -s -X POST "$BASE/exports/csv?layer=dashboard_master&survey_id=1"

# one file per dataset, foldered by how it was fetched
curl -s -X POST "$BASE/exports/csv?survey_id=1&category=api"
```

### `GET /kpi/{kpi_id}/export/pdf`

A PDF of one `POST /kpi` request. Save it with `-o`:

```bash
curl -s "$BASE/kpi/KPI_ID/export/pdf" -o kpi-report.pdf
```

---

## 10. AI data (Gemini)

Ask Google Gemini for a table in plain words. Results are labelled as AI output and stored only in the `gemini_*` tables, never mixed with official data. All of these need `GEMINI_API_KEY` on the server.

### `POST /ai-data/fetch` (202)

Starts a fetch and returns a `fetch_id` at once. Gemini runs in the background; poll `GET /ai-data/fetches/{fetch_id}` (not `/jobs`).

| Field | Type | Default | Notes |
|---|---|---|---|
| `prompt` | string | **required** | What you want, in plain words |
| `survey_id` | integer | **required** | Survey to file the values under |
| `use_search` | boolean | `false` | Let Gemini look figures up with Google Search. About 3× slower |
| `source` | `census` \| `hmis` | none | Answer only from this source, using its exact column names |
| `columns` | list of strings | none | With `source`: exactly these columns (names from `GET /ai-data/columns`) |
| `all_columns` | boolean | `false` | With `source`: every column (Census 85, HMIS 385), in batches |
| `max_columns` | integer | 20 | 1 to 100. Columns per Gemini answer |
| `years` | list of strings | none | One set of calls per year. HMIS years are financial years, e.g. `2019-20` |

Free-form:

```bash
curl -s -X POST "$BASE/ai-data/fetch" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Population and literacy rate of every district of Bihar, Census 2011", "survey_id": 101, "use_search": true}'
```

Tied to the Census columns:

```bash
curl -s -X POST "$BASE/ai-data/fetch" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "District-level figures for Bihar", "survey_id": 101, "source": "census", "columns": ["total_population_both_sexes", "literate_population_both_sexes"]}'
```

HMIS, year by year:

```bash
curl -s -X POST "$BASE/ai-data/fetch" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Institutional deliveries by district in Bihar", "survey_id": 102, "source": "hmis", "max_columns": 10, "years": ["2019-20", "2020-21"]}'
```

For `columns`, use real names from `GET /ai-data/columns?source=...`; the two above are examples.

### `GET /ai-data/columns`

The exact column names Gemini must use for one source.

| Query | Type | Required |
|---|---|---|
| `source` | `census` \| `hmis` | yes |

```bash
curl -s "$BASE/ai-data/columns?source=census"
```

### `GET /ai-data/fetches`

Every fetch, newest first.

```bash
curl -s "$BASE/ai-data/fetches?survey_id=101"
```

### `GET /ai-data/fetches/{fetch_id}`

Progress while running; the data once it has succeeded. A fetch still "running" after 15 minutes is treated as lost to a server restart.

| Query | Type | Default |
|---|---|---|
| `limit` | integer | 1000 |
| `offset` | integer | 0 |
| `details` | boolean | `false` (`true` adds sources and checks) |

```bash
curl -s "$BASE/ai-data/fetches/FETCH_ID?details=true"
```

### `GET /ai-data/fetches/{fetch_id}/download`

The table exactly as Gemini returned it.

| Query | Type | Default |
|---|---|---|
| `format` | `xlsx` \| `csv` | `xlsx` (adds an "About" sheet) |

```bash
curl -s "$BASE/ai-data/fetches/FETCH_ID/download?format=xlsx" -o gemini-fetch.xlsx
```

### `DELETE /ai-data/fetches/{fetch_id}`

Removes a bad answer everywhere and rebuilds its survey.

```bash
curl -s -X DELETE "$BASE/ai-data/fetches/FETCH_ID"
```

### `POST /ai-data/rebuild`

Rebuilds a survey's `gemini_clean` and `gemini_master` from `gemini_raw`. No body.

| Query | Type | Required |
|---|---|---|
| `survey_id` | integer | yes |

```bash
curl -s -X POST "$BASE/ai-data/rebuild?survey_id=101"
```

### `GET /ai-data/indicators`

What a survey holds: each indicator per source, with unit, number of places and years.

| Query | Type | Required |
|---|---|---|
| `survey_id` | integer | yes |
| `source_key` | string | no |

```bash
curl -s "$BASE/ai-data/indicators?survey_id=101"
```

### `GET /ai-data/warehouse`

Row and column counts per Gemini table, in the same shape as `GET /warehouse/{connector}`.

```bash
curl -s "$BASE/ai-data/warehouse?survey_id=101"
```

### `GET /ai-data/warehouse/rows`

Rows of one Gemini table, in the same shape as `GET /warehouse/{connector}/rows`.

| Query | Type | Default |
|---|---|---|
| `survey_id` | integer | **required** |
| `layer` | `gemini_raw` \| `gemini_clean` \| `gemini_master` | `gemini_master` |
| `limit`, `offset`, `count`, `order_by` | | as for warehouse rows |
| `source` | `census` \| `hmis` | all |
| *any column name* | | filter on that column |

```bash
curl -s "$BASE/ai-data/warehouse/rows?survey_id=101&layer=gemini_master&limit=10"
```

### `GET /ai-data/export`

A survey's Gemini values as a file.

| Query | Type | Default | Notes |
|---|---|---|---|
| `survey_id` | integer | **required** | |
| `format` | `xlsx` \| `csv` | `xlsx` | |
| `shape` | `long` \| `wide` | `wide` | long = one row per value |
| `indicator` | string, repeatable | all | |
| `source_key` | string | all | |
| `place_level` | string | all | e.g. `district` |

```bash
curl -s "$BASE/ai-data/export?survey_id=101&format=csv&shape=wide" -o gemini-survey-101.csv
```

---

## 11. Website scraping

The **Website** tab: give any page address, see every table, file and text found on it, then save only the ticked tables. It reads plain pages and the files they link to (CSV, TSV, Excel, JSON, PDF, Word, ZIP). Pages that build their content with JavaScript are opened in a headless browser, which also captures the data the page loads in the background ("data feeds") and files from Export / Download buttons.

Flow: `POST /scrape/scan` → poll `GET /jobs/{id}` → `GET /scrape/scans/{scan_id}` (the review screen) → `POST /scrape/scans/{scan_id}/import` → poll `GET /jobs/{id}` → `POST /warehouse/web/clean` (the Clean button) → `POST /datasets/merge` with `web_tables`.

All scan and finding ids are UUIDs. A malformed id is answered 404.

- An unsaved search is deleted 24 hours after it finishes (`PLATFORM_SCRAPE_HOLD_HOURS`).
- After a successful Save, each ticked table is a Website table: one `web_tables` row plus its values in `web_raw`, and a record card in `datasets` (`table_name` `web_raw`). The staged files and unticked findings are deleted.
- Saving the same table (same survey, page, place on the page and year) again replaces that year; another year is added beside it.
- Needs migrations `b32eb5b4e8f3` and `05f280b1545e`: run `venv/bin/alembic upgrade head`.

### `POST /scrape/scan` (202, background job)

| Field | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | Any public `http`/`https` page, or a direct link to a data file, including Census, HMIS and data.gov.in addresses. |
| `include_files` | boolean | no (default `true`) | Also download and read files the page links to (at most 100, 100 MB each, 500 MB in all). |
| `render` | string | no (default `auto`) | `auto`: open the page in a browser only when the plain read finds a JavaScript shell or no tables. `always` or `never`. Anything else gets 422. |

```bash
curl -s -X POST "$BASE/scrape/scan" -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/List_of_districts_of_Bihar"}'

# A JavaScript page (opens in the browser automatically; finds its data feeds)
curl -s -X POST "$BASE/scrape/scan" -H "Content-Type: application/json" \
  -d '{"url": "https://ndap.niti.gov.in/", "render": "auto"}'
```

Reply: a job (`job_type: "scrape_scan"`). `result.request.scan_id` is the scan's id, available at once. When the job succeeds, `result.summary` holds counts like `{"tables": 11, "files": 0, "text": 1, "blocked": 0, "suggested_keep": 2, "rendered_in_browser": false}`.

### `GET /scrape/scans`

Recent searches, newest first. Query: `limit` (1–100, default 20).

```bash
curl -s "$BASE/scrape/scans?limit=10"
```

### `GET /scrape/scans/{scan_id}`

Everything the review screen draws:
- `rendered_in_browser`: whether the page was opened in the browser.
- `tables`: each has a `kind`: `html_table`, `rendered_table` (from the page after it ran in the browser), `feed` (data the page loaded in the background; `locator.feed_url` and `locator.pages` say where from and how many pages were followed), `embedded`, `file_table`, `pdf_table` or `docx_table`. Each table also has `finding_id`, `title`, `rows`, `columns`, `column_names`, `preview` (first 20 rows × 30 columns), `signals`, `suggested` (`keep`/`skip`) and `default_name`.
  - `signals` covers: looks like layout, numeric share, `joinable` (districts matched against the district master), personal data, duplicate of another finding, truncated.
- `files`: what was downloaded and what was extracted from each.
- `text`: page and file text, with a snippet.
- `blocked`: anything that couldn't be read, with a `code` and a plain `message`.

```bash
curl -s "$BASE/scrape/scans/SCAN_ID"
```

404 for an unknown scan.

### `GET /scrape/scans/{scan_id}/findings/{finding_id}/rows`

All rows of one table, paged. Query: `limit` (1–1000, default 50), `offset` (default 0). Reply: `{"total", "columns", "rows"}`.

```bash
curl -s "$BASE/scrape/scans/SCAN_ID/findings/FINDING_ID/rows?limit=50&offset=0"
```

Status codes:
- 404: unknown finding.
- 422: not a table.
- 409: the search was already saved, so its staged rows are gone. Open the saved dataset instead.

### `GET /scrape/scans/{scan_id}/findings/{finding_id}/download`

A table comes back as CSV (UTF-8 with BOM, opens correctly in Excel). A file comes back exactly as downloaded. Text comes back as `.txt`.

```bash
curl -s -OJ "$BASE/scrape/scans/SCAN_ID/findings/FINDING_ID/download"
```

Status codes: 404 for an unknown finding, 409 once the search has been saved.

### `POST /scrape/scans/{scan_id}/import` (202, background job)

Save: send **every ticked table in one request**.

| Field | Type | Required | Notes |
|---|---|---|---|
| `survey_id` | integer | yes | The survey the tables are filed under. |
| `items` | array | yes (at least 1) | One object per ticked table (tables only), fields below. |

Each item:

| Field | Required | Notes |
|---|---|---|
| `finding_id` | yes | |
| `name` | no | Defaults to `<site> - <title>`. Also names the table's columns after Merge, so keep it the same across years |
| `place_column` | no | Column holding the districts; defaults to the review screen's guess (`signals.joinable.place_column`) |
| `state_name` or `state_column` | one, when there is a district column | `state_column` defaults to the guessed `signals.joinable.state_column` |
| `year` or `year_column` | exactly one | `year` from `GET /warehouse/web/years`, e.g. `2019-2020` |
| `columns` | no | Value columns to keep (e.g. one year's columns when years are in the headings); omitted = all |
| `indicator_column` | no | Long tables only (one row per district **per indicator**, like NFHS district files): the column naming each row's indicator. Each value is filed under that name, so every indicator becomes its own column after Merge. Keep one value column with `columns` (e.g. `["NFHS-5"]`); with several, each value is named `<indicator> · <column>`. Rows with no indicator name are skipped |

```bash
curl -s -X POST "$BASE/scrape/scans/SCAN_ID/import" -H "Content-Type: application/json" \
  -d '{"survey_id": 7, "items": [{"finding_id": "FINDING_ID", "name": "Bihar districts",
                                   "state_name": "Bihar", "year": "2019-2020"}]}'
```

Reply: a job (`job_type: "scrape_import"`). When it succeeds, `result.items` lists, for each table:
- `finding_id`
- `web_table_id`
- `dataset_id` (the record card, connector `web_table`, `web_feed` or `web_file`)
- `rows`, `values`
- `replaced` (true when the same table and year was saved before)

Nothing is cleaned yet: the next step is `POST /warehouse/web/clean`.

Status codes:
- 404: unknown scan.
- 409: the scan isn't finished, is already being saved (a double click), or was already saved.
- 422: no items, a duplicate item, a finding from another scan, a finding that isn't a table, or a bad item:
  - "Choose a year, or the column that holds the year."
  - "Year must be a financial year like 2019-2020."
  - "Column '<name>' is not in this table."
  - "Choose the state, or the column that holds it."
  - "Unknown state '<name>'."

### `DELETE /scrape/scans/{scan_id}` (204)

Discard an unsaved search and its staged files.

```bash
curl -s -X DELETE "$BASE/scrape/scans/SCAN_ID" -o /dev/null -w "%{http_code}\n"
```

409 while the search is running or being saved, and for a saved search, which is kept as the record of where its datasets came from.

### "Couldn't read" codes

| Code | Meaning |
|---|---|
| `robots_disallowed` | The site's robots.txt asks automated tools not to read it. Only when `PLATFORM_SCRAPE_OBEY_ROBOTS=true` (off by default). |
| `bot_protection` / `captcha` / `login_required` | The site blocked us, and a second try in the headless browser didn't reach data either. No CAPTCHA solving or logins. |
| `certificate_error` | The site's security certificate couldn't be verified (checked against the operating system's store plus certifi). |
| `form_gated` | Data appears only after choosing options in a form. |
| `needs_browser` | The page builds its content with JavaScript and wasn't opened in the browser (`render: never`, or the browser couldn't run). |
| `browser_unavailable` | Browser mode isn't installed or is turned off on this server; only a plain read was done. |
| `browser_failed` | The page couldn't be opened in the browser; only a plain read was done. |
| `private_address` | The address points inside a private network. |
| `too_large` / `too_many_files` / `timeout` | A limit was hit (`timeout`: the whole Search ran out of time). |
| `slow_response` | One page or file took longer than 30 seconds to answer; the rest of the Search carried on. |
| `http_error` / `fetch_failed` / `unsupported` | The site errored, couldn't be reached, or sent something we can't read. |
