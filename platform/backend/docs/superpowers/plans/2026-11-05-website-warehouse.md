# Website Warehouse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Saved Website tables go through shared `web_raw` → `web_clean` → `web_master` layers and can be merged, per table, with Census and HMIS into `dashboard_master`. Every Website-flow table gets the shared base: BIGINT id, UUID `external_id`, `created_at`, `updated_at`.

**Architecture:** A new `app/modules/web_warehouse/` package mirrors `census_warehouse` / `hmis_warehouse` (`schema.py`, `service.py`) and is registered as connector `web` in the generic `/warehouse/{connector}` router. The tables are ORM models built on a `WebTableBase` mixin. Values are stored one per row, and Merge turns them back into columns. Save (`scraping/importer.py`) writes `web_tables` + `web_raw` instead of `ds_<id>` tables. Merge gets a second path (`merging/web_join.py`): it joins on (`unique_id`, year) whenever Website tables are ticked, and leaves today's path untouched otherwise.

**Tech Stack:** Python 3.10, FastAPI, SQLAlchemy 2.0.35 (`Uuid`, `BigInteger().with_variant`), pandas 2.2, Alembic 1.20, pytest (SQLite test DB from `tests/conftest.py`).

**Spec:** `docs/superpowers/specs/2026-11-05-website-warehouse-design.md`

Work on a new branch `feature/website-warehouse`, created from `feature/website-tab-browser`. Run commands from `platform/backend/`. The virtualenv is `venv/`. Baseline: `venv/bin/python -m pytest -q` gives 236 passed. In the test sketches below, `client` means a module-level `TestClient(app)`, as in `tests/scraping/test_router.py` (it is not a fixture), and `db` is the `conftest.py` fixture.

## Global Constraints

- Base columns, on Website-flow tables only (`scrape_scans`, `scrape_findings`, `web_tables`, `web_raw`, `web_clean`, `web_master`):
  - `id` BIGINT PK auto-increment (INTEGER on SQLite);
  - `external_id` UUID, unique, not null;
  - `created_at`, `updated_at` timestamptz.
  - No other table changes shape.
- The API never exposes `id`, only `external_id`, as a string with dashes (`str(uuid)`).
- Years are financial years in HMIS form `YYYY-YYYY` (e.g. `2019-2020`). The Save list runs from `2005-2006` to the financial year containing today (1 April start).
- Website columns in `dashboard_master` start with `web_` and are at most 63 characters.
- Merge refuses when `dashboard_master` would pass **1,400** columns, counting dropped ones.
- Website values are aggregated once per (`unique_id`, `year`), never once per merged row.
- District lookup is always scoped to the state (`district_crosswalk.lookup_district(state, name)`). Summary rows (`is_summary_row`) are dropped.
- Migrations are generated only, never applied (no `alembic upgrade` on any database). Running the pytest suite is fine.
- A merge request without `web_tables` behaves exactly as today.
- CSV exports are UTF-8 with BOM.

## Review Focus

1. **Footnote and blank rows in the district column** ("* Provisional", empty merged cells). They must end up unmatched or skipped, never crash Clean. Test: Task 5 `test_clean_survives_blank_and_footnote_places`.
2. **Indian number formats and null markers** (`1,23,456`, `12.5%`, `-`, `NA`) must parse to `123456`, `12.5`, `None`, `None`. Test: Task 5 `test_clean_parses_indian_numbers`.
3. **Two columns with the same header** ("Total" twice) must become two distinct indicators. Test: Task 5 `test_clean_keeps_duplicate_headers_apart`.
4. **Re-saving a table after it was cleaned** must reset it to "not mergeable" until it is cleaned again. Test: Task 6 `test_resave_resets_mergeable`.
5. **Long or non-English table names** must give unique column names of at most 63 characters. Test: Task 7 `test_web_column_long_and_unicode_names`.
6. **A non-UUID scan id in a URL** must return 404, not 500. Test: Task 2 `test_bad_scan_id_is_404`.

---

### Task 1: Shared base and the four `web_` models

**Files:**
- Create: `app/models/web_base.py`, `app/models/web_warehouse.py`, `tests/web_warehouse/__init__.py`, `tests/web_warehouse/test_models.py`
- Modify: `app/models/__init__.py` (export `WebTable`, `WebRaw`, `WebClean`, `WebMaster`)

**Interfaces:**
- Produces:
  - `app.models.web_base`:
    - `BIGINT_ID` = `BigInteger().with_variant(Integer, "sqlite")`
    - `class WebTableBase` (mixin with `id`, `external_id: Mapped[uuid.UUID]` (`Uuid`, `default=uuid.uuid4`, unique), `created_at`, `updated_at` (`default=utcnow`, `onupdate=utcnow`))
    - `bulk_insert(conn, model: type, rows: list[dict], chunk: int = 5000) -> int`: fills `external_id`, `created_at` and `updated_at` on each dict that lacks them, inserts with Core `insert(model.__table__)`, and returns the count.
  - `app.models.web_warehouse`: `WebTable` (`web_tables`), `WebRaw` (`web_raw`), `WebClean` (`web_clean`), `WebMaster` (`web_master`), with exactly the columns of spec §4.1–4.4.
    - FKs: `finding_id → scrape_findings.id`, `web_table_id → web_tables.id`, `raw_id → web_raw.id` (all `BIGINT_ID`).
    - `survey_id` and `web_table_id` are indexed.
    - `web_master` has `UniqueConstraint("web_table_id", "unique_id", "year", "indicator", name="uq_web_master_key")`.
    - JSON columns: `source_locator`, `kept_columns`, `match_summary`.
    - `value` is `Float`.

- [ ] **Step 1: Write the failing tests**

```python
def test_base_columns_fill_themselves(db):  # WebTable row with only required fields
    t = WebTable(survey_id=7, name="Deliveries", page_url="https://x/y", source_locator={}, status="saved")
    db.add(t); db.commit()
    assert isinstance(t.id, int) and isinstance(t.external_id, uuid.UUID)
    assert t.created_at is not None and t.updated_at is not None

def test_updated_at_moves_on_update(db): ...  # change t.status; updated_at > previous value

def test_bulk_insert_fills_base_columns(db):
    n = bulk_insert(db.connection(), WebRaw, [{"survey_id": 7, "web_table_id": t.id, "row_number": 1,
                     "column_name": "Beds", "value_text": "10"}])
    assert n == 1 and db.query(WebRaw).one().external_id is not None

def test_master_key_is_unique(db): ...  # two WebMaster rows with the same key -> IntegrityError
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/web_warehouse/test_models.py -q`. Expected: ImportError.
- [ ] **Step 3: Implement** `web_base.py` and `web_warehouse.py`, and add them to `app/models/__init__.py` so `conftest`'s `create_all` creates the tables.
- [ ] **Step 4: Run** the same command. Expected: 4 passed.
- [ ] **Step 5: Commit** `feat: web warehouse models on a shared base`

---

### Task 2: Scrape tables on the base; UUIDs at every boundary

**Files:**
- Modify:
  - `app/models/scrape.py`
  - `app/modules/scraping/scan.py` (finding creation, ~lines 88–135; `run_scan`)
  - `app/modules/scraping/service.py`
  - `app/modules/scraping/importer.py` (claim lookups only)
  - `app/modules/scraping/router.py`
  - `app/modules/jobs/service.py` (`_run_scrape_scan_job` payload stays `scan_id: str`)
- Test: `tests/scraping/test_models.py`, `tests/scraping/test_router.py`, `tests/scraping/test_startup.py`, and every scraping test that asserts 12-character ids

**Interfaces:**
- Consumes: `WebTableBase`, `BIGINT_ID` (Task 1).
- Produces:
  - Models:
    - `ScrapeScan(WebTableBase, Base)` and `ScrapeFinding(WebTableBase, Base)`;
    - `ScrapeFinding.scan_id` and `parent_id` are `BIGINT_ID` FKs (`parent_id → scrape_findings.id`, nullable);
    - all other columns unchanged.
  - `service.get_scan(db, scan_id: str) -> ScrapeScan`: looks up by `external_id`. A malformed UUID raises `ScanNotFound` (404).
  - `service.get_finding(db, scan: ScrapeScan, finding_id: str) -> ScrapeFinding`: same rule.
  - `store.scan_dir(key: str)`: the key is `str(scan.external_id)`. Every `store.*` call passes that key.
  - Manifest, `list_scans`, job payloads/results and import results carry `str(external_id)` for `scan_id`, `finding_id`, `parent_id` and `produced`.
  - `service.sweep_orphan_folders(db) -> int`: deletes folders under `settings.scrape_dir` whose name is not the `external_id` of a scan with status `running`, `done` or `importing`. Called from `startup_cleanup`.

- [ ] **Step 1: Write the failing tests**

```python
def test_scan_and_finding_use_the_base(db):  # replaces test_scan_with_finding_round_trips
    assert isinstance(scan.id, int) and isinstance(scan.external_id, uuid.UUID)
    assert got.scan_id == scan.id and got.updated_at is not None

def test_bad_scan_id_is_404(client):
    assert client.get("/scrape/scans/not-a-uuid").status_code == 404
    assert client.get(f"/scrape/scans/{uuid.uuid4()}").status_code == 404

def test_manifest_ids_are_uuids(db, httpserver, loopback_ok):
    m = service.manifest(db, str(scan.external_id))
    uuid.UUID(m["scan_id"]); [uuid.UUID(t["finding_id"]) for t in m["tables"]]
    assert all(uuid.UUID(p) for f in m["files"] for p in f["produced"])  # file -> child tables

def test_startup_sweeps_orphan_folders(db, tmp_path): ...  # folder "old-id-123" removed, live scan's folder kept
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/scraping -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement.** In `scan.py`:
  - each finding gets `external_id=uuid.uuid4()` before its file is staged, and the storage path uses `str(external_id)`;
  - a child finding records its parent's external id in memory (e.g. a `_parent_key` dict keyed by the child's external id);
  - `finish()` flushes all findings, then sets `parent_id` from the external-id → id map before the commit.
- [ ] **Step 4: Run** `venv/bin/python -m pytest tests/scraping -q`. Expected: all pass, after updating the old assertions that relied on 12-character ids (`len(got.id) == 12`, string `scan.id` in URLs) to use `str(external_id)`.
- [ ] **Step 5: Commit** `refactor: scrape tables on the shared base, UUIDs in the API`

---

### Task 3: Web warehouse schema and years

**Files:**
- Create: `app/modules/web_warehouse/__init__.py`, `app/modules/web_warehouse/schema.py`, `app/modules/web_warehouse/years.py`, `tests/web_warehouse/test_years.py`

**Interfaces:**
- Produces:
  - `schema`:
    - `TABLES = "web_tables"`, `RAW = "web_raw"`, `CLEAN = "web_clean"`, `MASTER = "web_master"`, `LAYERS = (RAW, CLEAN, MASTER)`
    - `SURVEY_COL = "survey_id"`, `DATASET_COL = "web_table_id"` (the row reader's scoping column), `COLUMN_PREFIX = "web_"`, `YEAR_COL = "year"`, `MAX_DASHBOARD_COLUMNS = 1400`
    - `is_layer_table(name) -> bool`
  - `years.financial_years(today: date | None = None) -> list[str]`
  - `years.normalise_year(text: str | None) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
def test_financial_years_run_to_the_current_one():
    assert financial_years(date(2026, 11, 5))[0] == "2005-2006"
    assert financial_years(date(2026, 11, 5))[-1] == "2026-2027"
    assert financial_years(date(2026, 3, 31))[-1] == "2025-2026"

@pytest.mark.parametrize("text,want", [
    ("2019-20", "2019-2020"), ("2019–20", "2019-2020"), ("2019-2020", "2019-2020"),
    ("2019/20", "2019-2020"), ("FY 2019-20", "2019-2020"), (" 2019 - 20 ", "2019-2020"),
    ("2019", None), ("2019-21", None), ("", None), (None, None), ("abc", None)])
def test_normalise_year(text, want): assert normalise_year(text) == want
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/web_warehouse/test_years.py -q`. Expected: ImportError.
- [ ] **Step 3: Implement.** `normalise_year` accepts `YYYY` + separator (`-`, `–`, `—`, `/`) + `YY` or `YYYY`, optionally prefixed with `FY`. The second year must be the first + 1. Anything else returns `None`.
- [ ] **Step 4: Run** the same command. Expected: all pass.
- [ ] **Step 5: Commit** `feat: web warehouse schema and financial years`

---

### Task 4: Save writes `web_tables` + `web_raw`

**Files:**
- Modify:
  - `app/modules/scraping/schemas.py` (`ImportItem`)
  - `app/modules/scraping/importer.py`
  - `app/modules/jobs/service.py` (`_run_scrape_import_job`: `dataset_id=items[0]["dataset_id"]` still holds)
- Test: rewrite `tests/scraping/test_importer.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `ImportItem` gains `place_column: str | None`, `state_name: str | None`, `state_column: str | None`, `year: str | None`, `year_column: str | None`, `columns: list[str] | None`.
  - `importer.claim_for_import(db, scan_id: str, items: list[dict]) -> None`: today's rules, plus the 422 checks below, all before the scan is claimed.
  - `importer.run_import(db, scan_id: str, survey_id: int, items: list[dict], progress=None) -> dict`. The result is `{"items": [{"finding_id", "web_table_id", "dataset_id", "rows", "values", "replaced"}]}`; the ids are `str(external_id)` and `dataset_id` is the record card.
  - `importer.value_columns(finding, item) -> list[str]`: the kept columns (`columns` or all) minus the place, state and year columns.

The 422 checks in `claim_for_import` (exact messages):
- Neither or both of `year` / `year_column`: `"Choose a year, or the column that holds the year."`
- `year` not in `financial_years()`: `"Year must be a financial year like 2019-2020."`
- A named column (`place_column`, `state_column`, `year_column`, any of `columns`) not in `finding.column_names`: `"Column '<name>' is not in this table."`
- The place column (`place_column` or, when `signals.joinable.level == "district"`, `signals.joinable.place_column`) is set, but there is no `state_name` and no state column (`state_column` or the guessed `signals.joinable.state_column`): `"Choose the state, or the column that holds it."`
- `state_name` given and `district_crosswalk.lookup_state(state_name)` is None: `"Unknown state '<name>'."`

What `run_import` does per item:
1. Finds the existing `WebTable` with the same `survey_id`, `page_url` (= `scan.final_url or scan.url`), `source_locator` (= `finding.locator`, compared as dicts) and `year` (`None` for a `year_column` table).
   - **If found:** delete its `web_raw` / `web_clean` / `web_master` rows, refresh its fields, `status="saved"`, `match_summary=None`, and set `replaced=True`.
   - **Otherwise:** create a new one.
2. Creates a new record card with `dataset_crud.create_dataset_shell(..., stage="raw", connector=CONNECTOR_FOR_KIND[kind], table_name="web_raw", survey_id=...)`, sets `row_count` and `column_count`, writes `notes` as today, and points `web_table.dataset_id` at it.
3. `bulk_insert`s one `web_raw` row per (source row, value column), with `row_number` 1-based and `place_text` / `state_text` / `year_text` / `value_text` as text exactly as staged (`None` stays `None`).
4. Sets `finding.imported_dataset_id`. The rest is unchanged: unticked findings deleted, scan `imported`, staging folder removed, and a failure leaves the scan `done` and retryable.

- [ ] **Step 1: Write the failing tests** (`standard_site`: CSV `District,Beds`, 38 rows)

```python
def test_save_writes_web_tables_and_raw(db, ...):
    r = save(db, scan, [{"finding_id": csv_id, "name": "Bihar beds", "state_name": "Bihar", "year": "2022-2023"}])
    t = db.query(WebTable).one()
    assert (t.name, t.place_column, t.state_name, t.year, t.status) == ("Bihar beds", "District", "Bihar", "2022-2023", "saved")
    assert db.query(WebRaw).filter_by(web_table_id=t.id).count() == 38  # one value column
    assert db.get(Dataset, t.dataset_id).table_name == "web_raw" and r["items"][0]["replaced"] is False
    assert not db.query(Dataset).filter(Dataset.table_name.like("ds_%")).count()

def test_same_page_same_year_replaces(db, ...): ...    # second scan + save -> one WebTable, replaced True, 38 raw rows
def test_same_page_other_year_adds(db, ...): ...       # 2023-2024 -> two WebTables, 76 raw rows
def test_year_column_rows_keep_their_year(db, ...): ...  # site CSV with Year column -> year_text set per row, t.year None
def test_claim_validation_messages(db, ...): ...       # each message above, scan still "done" afterwards
def test_failed_save_keeps_scan_retryable(db, ..., monkeypatch): ...  # bulk_insert raises on 2nd item
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/scraping/test_importer.py -q`. Expected: failures.
- [ ] **Step 3: Implement** the schema fields, the claim checks and `run_import` as above. Drop the `store_result` / `store_clean_copy` calls.
- [ ] **Step 4: Run** `venv/bin/python -m pytest tests/scraping -q`. Expected: all pass (also fix `test_router`'s import test to send `year` and `state_name`).
- [ ] **Step 5: Commit** `feat: Save writes Website tables into web_raw`

---

### Task 5: Clean (`web_raw` → `web_clean`)

**Files:**
- Create: `app/modules/web_warehouse/service.py` (clean part), `tests/web_warehouse/test_clean.py`, `tests/web_warehouse/helpers.py` (`saved_table(db, rows, columns, **item)`: builds a `WebTable` + `web_raw` directly, without a scan)
- Modify: `app/modules/scraping/scoring.py` (rename `_number` → `parse_number`, keeping the old name as an alias)

**Interfaces:**
- Consumes: Tasks 1, 3; `cleaning.clean_dataframe`, `WAREHOUSE_OPERATIONS`, `district_crosswalk.lookup_district` / `is_summary_row`.
- Produces:
  - `service.select_tables(db, survey_id: int | None, table: str | None) -> list[WebTable]`: raises `ValueError("No Website tables found for survey <id>.")` (or for the table id) when empty.
  - `service.build_clean(db, operations: list[str] | None = None, survey_id: int | None = None, table: str | None = None) -> dict`. The result is `{"table": "web_clean", "survey_id", "rows", "tables": [{"web_table_id", "name", "rows", "match_summary"}]}`.
  - `match_summary` = `{"matched": int, "total": int, "unmatched": [≤20 names], "year_unrecognised": [≤20 texts], "duplicate_places": [≤20 names]}`, where `total` counts distinct non-summary places.

How the clean works, per table:
1. Rebuild the wide frame from `web_raw`: columns `row_number`, `district`, `state`, `year` (only those the table has), and the value columns renamed `v0..vN`, with a `{vK: original header}` map. These reserved names keep `standardize_columns` from renaming them and let `standardize_states` see `state`.
2. Run `clean_dataframe(frame, ops - {"drop_duplicates", "drop_empty_rows"})`, then apply those two (if requested) ignoring `row_number`.
3. Melt back to one row per value:
   - `indicator` = `snake_case(original header)`, de-duplicated within the table as `total`, `total_2`;
   - `value = parse_number(cell)`;
   - `raw_id` from the (`row_number`, original header) → `web_raw.id` map;
   - state = row `state` else `web_table.state_name`;
   - drop summary rows;
   - `lookup_district(state, district)` fills the geography and `matched`;
   - `year` = `web_table.year` or `normalise_year(row year)`.
4. Delete the table's `web_clean` rows, `bulk_insert` the new ones, and set `status="cleaned"` and `match_summary`.

An unknown operation name lets `clean_dataframe`'s `ValueError` through; the router turns it into a 400.

- [ ] **Step 1: Write the failing tests**

```python
def test_clean_matches_districts_and_years(db):  # Patna, Gaya (Bihar), "Nowhere"
    assert summary == {"matched": 2, "total": 3, "unmatched": ["Nowhere"], "year_unrecognised": [], "duplicate_places": []}
    assert {r.unique_id is not None for r in rows} == {True, False} and t.status == "cleaned"

def test_clean_parses_indian_numbers(db):  # "1,23,456" "12.5%" "-" "NA" -> 123456.0, 12.5, None, None
def test_clean_survives_blank_and_footnote_places(db):  # "", None, "* Provisional", "Total" -> no crash; Total dropped
def test_clean_keeps_duplicate_headers_apart(db):  # two "Total" headers -> indicators {"total", "total_2"}
def test_clean_one_table_leaves_others(db):  # table=a -> b's clean rows untouched
def test_clean_year_column_unrecognised_reported(db):  # Year "2019" -> year None, "2019" in year_unrecognised
def test_clean_raw_ids_trace_back(db):  # every clean row's raw_id points at the raw row with the same header + row_number
def test_clean_is_repeatable(db):  # second build_clean -> same row count, no duplicates
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/web_warehouse/test_clean.py -q`. Expected: ImportError.
- [ ] **Step 3: Implement** `select_tables` and `build_clean`.
- [ ] **Step 4: Run** `venv/bin/python -m pytest tests/web_warehouse tests/scraping -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: clean Website tables into web_clean`

---

### Task 6: Master, listing, summary, export, and the `/warehouse/web` routes

**Files:**
- Modify:
  - `app/modules/web_warehouse/service.py`
  - `app/modules/warehouse/router.py`
  - `app/crud/dataset.py` (`_scope`)
- Test: `tests/web_warehouse/test_master.py`, `tests/web_warehouse/test_router.py`

**Interfaces:**
- Consumes: Task 5.
- Produces:
  - `service.build_master(db, survey_id=None, table=None) -> dict`:
    - per cleaned table: delete its master rows, then insert the clean rows with `matched` and non-null `year` and `value`;
    - one row per (`unique_id`, `year`, `indicator`), and the one with the highest raw `row_number` wins;
    - skips tables that aren't cleaned and lists them in `"skipped"`.
  - `service.list_tables(db, survey_id: int) -> list[dict]`: keys `web_table_id`, `name`, `page_url`, `state_name`, `state_column`, `year`, `year_column`, `status`, `rows`, `values`, `match_summary`, `mergeable`, `reason`, `created_at`, `updated_at`.
    - `reason` is one of `"not cleaned"`, `"no district column"`, `"no district matched"`, `"no year recognised"`, or `None`.
    - `mergeable` is true when `reason is None` (judged from `web_clean`; master is rebuilt before a merge).
  - `service.summary(db, survey_id=None) -> dict`: `{layer: {"rows": n}}` plus `"tables": n`.
  - `service.export_layer(db, layer, out_dir=None, dataset_ids=None, survey_id=None) -> dict`: writes `EXPORTS_DIR/web/<layer>/<layer>__survey_<id|all>.csv` (UTF-8 BOM) and returns `{"path", "rows"}`.
  - `service.layer_ids_for_datasets(db, dataset_ids: list[str]) -> list[int]`: the `web_tables.id`s whose `dataset_id` is in the list.
  - Router:
    - `Connector.web`;
    - `_CONNECTORS[web] = (web_service, web_schema)`;
    - optional query `table: str | None` on `clean`, `master` and `rebuild`, answering 400 `"'table' applies only to the web connector."` for other connectors;
    - new `GET /warehouse/web/tables?survey_id=` (required) and `GET /warehouse/web/years`, declared before the `/{connector}` routes;
    - `rows` for web maps job dataset ids through `layer_ids_for_datasets` and defaults `order_by` to `["id"]`.
  - `crud/dataset._scope`: for `table_name == "web_raw"`, returns `WHERE web_table_id IN (SELECT id FROM web_tables WHERE dataset_id = :_ds)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_master_last_row_wins_and_skips_unmatched(db): ...  # Patna twice -> one master row, the later value
def test_master_skips_uncleaned(db): ...                     # "skipped" == [name]
def test_list_tables_reasons(db): ...  # each reason string; cleaned+matched -> mergeable True, reason None
def test_resave_resets_mergeable(db, ...): ...  # clean, then Save same page/year again -> status saved, mergeable False
def test_dataset_record_card_reads_its_raw_rows(db): ...  # dataset_crud.load_page(card) -> only its own web_raw rows
def test_router_web_clean_and_master(client): ...  # POST /warehouse/web/clean?survey_id=7&table=<ext> -> 200
def test_router_table_param_rejected_for_census(client): ...  # 400 with the message above
def test_router_web_tables_and_years(client): ...  # GET /warehouse/web/tables?survey_id=7 lists; /years ends "2026-2027"
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/web_warehouse -q`. Expected: failures.
- [ ] **Step 3: Implement** as described.
- [ ] **Step 4: Run** `venv/bin/python -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: web master, table listing and /warehouse/web routes`

---

### Task 7: Merge with Website tables

**Files:**
- Create: `app/modules/merging/web_join.py`, `tests/web_warehouse/test_merge.py`
- Modify:
  - `app/modules/merging/schemas.py` (`MergeRequest.web_tables: list[str] | None`; the validator requires `survey_id` when it's set)
  - `app/modules/merging/run.py` (branch at the top of `run_merge`; move the write tail into `_write_dashboard(db, df, request, survey_id, parents, report)`, shared by both paths)

**Interfaces:**
- Consumes: Task 6, `survey.load_master`, `survey.available_connectors`.
- Produces (`web_join`):
  - `web_column(table_name: str, indicator: str) -> str`: `web_<snake_case(name)>_<indicator>`. Over 63 characters it becomes `full[:56] + "_" + sha1(full)[:6]`.
  - `load_web_frames(db, survey_id: int, web_table_ids: list[str]) -> list[pd.DataFrame]`:
    - one wide frame per table name, keyed `unique_id` and `year`, plus `web_state_name` and `web_district_name`;
    - tables with the same name (the same series across years) share columns.
    - Raises `MergeRequestError` for:
      - an id not in the survey: `"Website table '<id>' is not part of survey <s>."`
      - no master rows: `"Website table '<name>' has no master rows; rebuild its master first."`
      - same name with overlapping (`unique_id`, `year`, `indicator`): `"Two ticked Website tables are both called '<name>' and both have '<indicator>' for <year>; rename one."`
  - `join_with_web(census: pd.DataFrame | None, hmis: pd.DataFrame | None, web: list[pd.DataFrame]) -> tuple[pd.DataFrame, list[str]]`:
    1. Drop rows without `unique_id` (and report them).
    2. Suffix HMIS columns `__hmis` (except `unique_id`), with `year = report_year`.
    3. Outer-join HMIS and the web frames on (`unique_id`, `year`).
    4. Suffix Census `__census` and outer-join it on `unique_id`.
    5. Order the columns `unique_id`, `year`, then the rest.
  - `check_column_budget(conn, df: pd.DataFrame) -> None`:
    - existing columns: on Postgres `count(*)` from `pg_attribute` for `dashboard_master` with `attnum > 0` (dropped ones included); elsewhere the inspector's column count; 0 if the table doesn't exist;
    - plus the frame's columns not yet in the table;
    - over `MAX_DASHBOARD_COLUMNS` → `MergeRequestError(f"This merge would give dashboard_master {total:,} columns; the limit is 1,400. Untick some Website tables or reuse table names.")`.
    - Called in `_write_dashboard` for both paths.
  - `run_merge`, when `web_tables` is set:
    - the connectors are the requested ones or `available_connectors`;
    - sources = connectors + distinct Website table names, at least 2;
    - then `join_with_web`, then cleaning with `WAREHOUSE_OPERATIONS` as today, then `_write_dashboard`.

- [ ] **Step 1: Write the failing tests** (masters built directly in tiny tables: census `unique_id, total_population`; HMIS `unique_id, report_year, period, deliveries`)

```python
def test_web_joins_hmis_on_district_and_year(db):  # Patna HMIS 2018-19 and 2019-20; web Patna 2019-20 = 5
    row = df[(df.unique_id == PATNA) & (df.year == "2019-2020")]
    assert row["web_deliveries_home_deliveries"].item() == 5
    assert df[(df.unique_id == PATNA) & (df.year == "2018-2019")]["web_deliveries_home_deliveries"].isna().all()

def test_census_attaches_by_district_only(db): ...  # total_population_census equal on both Patna rows
def test_web_row_without_hmis_partner_kept(db): ...  # web Gaya 2020-2021, no HMIS -> own row, year "2020-2021"
def test_same_name_two_years_share_columns(db): ...  # 2 WebTables "Deliveries" -> one column, two rows
def test_same_name_overlap_refused(db): ...          # message as above
def test_web_column_long_and_unicode_names(): ...    # 80-char Hindi/English name -> len <= 63, distinct for 2 indicators
def test_column_budget_refuses(db, monkeypatch): ... # MAX_DASHBOARD_COLUMNS patched to 5 -> message as above
def test_merge_without_web_tables_unchanged(db): ... # census+hmis request -> same columns as before this task
def test_merge_job_end_to_end(db): ...  # run_merge(survey_id, connectors=["census"], web_tables=[ext]) -> dashboard_master rows, Dataset stage master
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/web_warehouse/test_merge.py -q`. Expected: ImportError.
- [ ] **Step 3: Implement** `web_join.py`, the schema field and the `run_merge` branch.
- [ ] **Step 4: Run** `venv/bin/python -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: merge Website tables on district and year`

---

### Task 8: KPIs and Dashboard count Website values once per district and year

**Files:**
- Modify: `app/modules/kpi/aggregation.py`, `app/modules/kpi/explorer.py` (`Frame.source_of`), `app/modules/kpi/router.py` (`aggregate_kpis`)
- Test: `tests/web_warehouse/test_kpi.py`

**Interfaces:**
- Consumes: Task 7's column naming (`web_` prefix, shared `year`).
- Produces:
  - `aggregation`:
    - `WEB_PREFIX = "web_"`
    - `is_web(column) -> bool`
    - `one_row_per_place_year(df) -> pd.DataFrame` (deduplicates on `["unique_id", "year"]` when both exist, else `one_row_per_place`)
    - `frame_for`: web → `one_row_per_place_year`
  - `explorer`: a source dict may carry `"prefix"`; `source_of` checks prefix sources (`column.startswith(prefix)`) before the suffix sources.
  - `kpi/router.with_web_source(df, sources: list[dict]) -> list[dict]`: appends `{"suffix": "", "prefix": "web_", "place_key": ["unique_id"], "time": ["year"]}` when `df` has a `web_` column and a `year` column and no source has that prefix.
  - `kpi/router.with_web_year(time: dict | None) -> dict | None`: when `"year"` is missing and `"report_year_hmis"` is present, adds `year` with that value. Applied to `time`, `previous_time` and every trend time.

- [ ] **Step 1: Write the failing tests**

```python
def test_web_total_counts_once_per_place_year():  # Patna 2019-20 repeated on 3 HMIS period rows, value 5
    assert aggregation.total(df, "web_deliveries_home") == 5

def test_aggregate_endpoint_adds_web_source(client, db): ...  # sources [census, hmis] only, time report_year_hmis=2019-2020
    # KPI sum of web_deliveries_home -> 5 (not 15), no 422
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/web_warehouse/test_kpi.py -q`. Expected: failures.
- [ ] **Step 3: Implement** as described.
- [ ] **Step 4: Run** `venv/bin/python -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: KPIs count Website values once per district and year`

---

### Task 9: Migration and docs

**Files:**
- Create: `alembic/versions/<rev>_website_warehouse_and_scrape_base.py`
- Modify: `API_TESTING.md` (§11 Website, §5 Warehouse, §7 Merging), `CLAUDE.md` (the Website tab section's "Save" and "Dashboard" bullets; Migrations list), `docs/superpowers/specs/2026-11-05-website-warehouse-design.md` (Status: built)

The migration is generated, then hand-edited. **Never applied.**
- `down_revision = "b32eb5b4e8f3"`.
- **upgrade():**
  1. Create the four `web_` tables, indexes and the unique constraint. Autogenerate gives these.
  2. Delete unsaved scans and their findings (`status <> 'imported'`). Their staging folders are removed by Task 2's startup sweep. (Spec §9 says "mark them expired"; deleting them outright has the same result, because their folders couldn't be found under the new ids anyway.)
  3. Convert `scrape_scans`: rename `id` → `old_id`, add `id BIGSERIAL`, `external_id UUID NOT NULL DEFAULT gen_random_uuid()` (unique), `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`, and move the PK to `id`.
  4. Convert `scrape_findings` the same way:
     - rename `id`, `scan_id`, `parent_id` → `old_*`;
     - add the new columns;
     - fill `scan_id` from `scrape_scans.old_id`, and `parent_id` from a self-join on `old_parent_id`;
     - drop the old FK, then the `old_*` columns;
     - add the FKs, the `scan_id` index and `NOT NULL` on `scan_id`.
  5. Drop `scrape_scans.old_id`.
- **downgrade():** drop the four `web_` tables, then `raise NotImplementedError("The scrape id conversion is one-way.")`.

- [ ] **Step 1: Generate** `venv/bin/alembic revision --autogenerate -m "website warehouse and scrape base"` (it only reads the local DB). Expected: a new file creating the four tables.
- [ ] **Step 2: Hand-edit** steps 2–5 in, plus the downgrade.
- [ ] **Step 3: Check offline SQL** `venv/bin/alembic upgrade b32eb5b4e8f3:head --sql > /tmp/...` (run into the session scratchpad). Expected: valid SQL showing the four `CREATE TABLE`s, the deletes and the conversions in the order above; no errors. Don't run it against a database.
- [ ] **Step 4: Update docs**:
  - API changes per spec §7 (UUID ids, the new Save fields, `/warehouse/web/*`, `web_tables` on merge, the 1,400 refusal);
  - CLAUDE.md: Website saves go to `web_raw`, not `ds_`, and can reach the Dashboard through Merge.
- [ ] **Step 5: Run** `venv/bin/python -m pytest -q`. Expected: all pass (236 + the new tests, browser tests may skip).
- [ ] **Step 6: Commit** `feat: migration for the website warehouse; docs`
