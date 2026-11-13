# Website data as a warehouse: raw → clean → master → merge

Date: 5 November 2026
Status: built on `feature/website-warehouse` (plan `docs/superpowers/plans/2026-11-05-website-warehouse.md`); migration `05f280b1545e` generated, not applied
Builds on: `2026-11-03-website-tab-design.md` (the Website tab: Search → review → Save), already built on `feature/website-tab-browser`.

Statements marked **(my call)** were decided while writing this, not asked for by the product owner. They can be changed. Everything else was decided by the product owner in conversation on 5 November 2026.

---

## 1. Intent

Today a table saved from the Website tab becomes a standalone dataset: a raw `ds_<id>` table plus a cleaned `ds_<id>` copy. It can be viewed, cleaned and downloaded, but it never reaches **Merge Data**, **KPIs** or the **Dashboard**, because Merge only knows the Census and HMIS warehouses.

The product owner wants Website data to work **like Census and HMIS**: stored in shared raw / clean / master layers, cleaned with the same **Clean** button, and merged with Census and HMIS on the **Merge Data** screen so it can feed KPIs and the Dashboard.

The admin's journey in the portal (screens they already know):

1. **Fetch Data → Website → Search → review → Save.** At Save, for each ticked table, the admin also says which column holds the districts, which state, and which year (or which column holds the year).
2. **Fetched Data** (survey page): one new row **per saved Website table**, with Jobs / Quality / Clean, like the HMIS and Census rows.
3. **Clean** pop-up → the table's raw values become clean values, matched to the district list. The row then shows "cleaned" and how many districts matched.
4. **Merge Data**: under Census India and HMIS, a list of the survey's Website tables, each with its own tick box. Tables that can't be merged are greyed out with the reason.
5. **KPIs** (generated / prompt) and the **Dashboard** can then use Website figures, including trends across years.

## 2. Goals, non-goals, anti-goals

**Goals**
- Saved Website tables go into shared tables `web_raw` → `web_clean` → `web_master`, mirroring the Census / HMIS layers.
- Each saved Website table is its own unit: its own row on Fetched Data, its own Clean, its own tick box in Merge.
- The same table saved for different years keeps every year, and its years line up as one series (same columns, different years), so trends work.
- Tables that carry several years inside them keep each row's own year.
- Merge joins chosen Website tables with Census and HMIS master data into `dashboard_master`.
- Every Website-flow table uses one shared base: `id` BIGINT auto-increment primary key, `external_id` UUID, `created_at`, `updated_at`. This includes the existing `scrape_scans` and `scrape_findings`.

**Non-goals**
- No change to Search, the review screen, the safety rules, or the staging folder's role.
- No change to the columns of the Census, HMIS, `datasets`, or other existing non-Website tables. The base is **only** for Website-flow tables. (`dashboard_master` gains new columns through Merge, as it always does: the Website columns and one shared year column, §5.4.)
- The 28 fixed Dashboard KPI cards stay Census + HMIS only.
- State-only Website tables (no district column) don't merge in this version.
- Tables saved before this change (the `ds_<id>` copies) are left as they are, not moved into the new layers **(my call)**.
- No "count or rate" question at Save. Percentages and ratios are handled when a KPI is defined (choosing a weighted or ratio aggregation), not by the Website flow **(my call)**.

**Anti-goals (would make this a failure even if it technically works)**
- Website figures multiplying totals. A Website value must be counted once per district per year, never once per merged row. The same mistake once gave India a population of about 4.4 billion (see `kpi/aggregation.py`).
- Pairing the wrong years: a 2018-2019 Website value sitting on a 2019-2020 HMIS row, or a "2018-19" column stamped with the year 2019-2020.
- Two different Website tables' columns silently overwriting each other.
- Rows that didn't match a district (or a year) quietly disappearing without the admin being told which ones.
- A merge that pushes `dashboard_master` past Postgres's column limit and breaks it for every survey.

## 3. The shared base (Website-flow tables only)

A mixin, e.g. `WebTableBase`, used by every Website-flow table (`scrape_scans`, `scrape_findings`, `web_tables`, `web_raw`, `web_clean`, `web_master`) and nothing else:

| Column | Type | Rule |
|---|---|---|
| `id` | BIGINT | primary key, auto-increment |
| `external_id` | UUID | unique, not null, generated automatically. The id the API, the portal, job payloads and the staging folder use |
| `created_at` | timestamp with time zone | set on insert |
| `updated_at` | timestamp with time zone | set on insert, refreshed on every update |

Builder notes:
- SQLite (local runs and the test suite) only auto-increments a plain `INTEGER PRIMARY KEY`, so `id` must be BIGINT on Postgres and INTEGER on SQLite (e.g. `BigInteger().with_variant(Integer, "sqlite")`).
- Rows in `web_raw` / `web_clean` / `web_master` are written in bulk (pandas or Core inserts). The defaults must work for bulk inserts too: either database-side defaults, or filled in by the code before inserting. ORM `onupdate` doesn't fire for Core bulk updates, so code that updates in bulk sets `updated_at` itself.
- Foreign keys between Website tables use `id`; the API never exposes `id`, only `external_id`.

### 3.1 What the base changes for `scrape_scans` / `scrape_findings`

Their 16-character text ids become `id` BIGINT + `external_id` UUID. As a result:
- `/scrape/scans/{id}` and `/scrape/scans/{id}/findings/{finding_id}/...` take the **UUIDs**. The portal hasn't built on these yet.
- Job payloads and results (`scan_id`, `finding_id`) carry the UUIDs.
- The staging folder becomes `data/scrape_staging/<scan external_id>/`.
- `scrape_findings.scan_id` and `parent_id` become BIGINT foreign keys to `id`.
- `scrape_scans.created_at` already exists; `updated_at` is new. `finished_at` and `expires_at` stay.

## 4. The tables

All four new tables are ORM models with fixed columns, plus the base's four columns. Because columns are fixed, values are stored one per row (as HMIS raw is), and Merge turns them into columns.

### 4.1 `web_tables`: one row per saved Website table (per year, see §5.1)

| Column | Type | Notes |
|---|---|---|
| `survey_id` | int, indexed | |
| `finding_id` | → `scrape_findings.id` | which page and which table on it (the newest Save's finding) |
| `dataset_id` | string | the record card in `datasets` (§6) |
| `name` | text | the admin's name for it, default from the finding. Also names its columns in Merge (§5.4) |
| `page_url` | text | the page searched (final address after redirects) |
| `source_locator` | JSON | where on the page: file link, sheet, PDF page, table index, feed address (the finding's `locator`) |
| `place_column` | text | the column holding districts, chosen at Save |
| `state_name` | text, nullable | chosen at Save when the table has no state column |
| `state_column` | text, nullable | used instead of `state_name` when the table has its own state column |
| `year` | text, nullable | chosen at Save, e.g. `2019-2020`. Empty when `year_column` is used |
| `year_column` | text, nullable | the column holding each row's year, when the table has one |
| `kept_columns` | JSON list, nullable | the value columns the admin ticked; empty means all |
| `indicator_column` | text, nullable | long tables only: the column naming each row's indicator (added 2026-11-10, migration `29f1437fff2d`) |
| `status` | text | `saved` → `cleaned` (back to `saved` when re-saved) |
| `match_summary` | JSON, nullable | after Clean: districts matched / total, unmatched district names, rows whose year wasn't recognised, districts listed twice |

### 4.2 `web_raw`: one row per value, exactly as found

| Column | Type | Notes |
|---|---|---|
| `survey_id` | int, indexed | |
| `web_table_id` | → `web_tables.id`, indexed | |
| `row_number` | int | row position in the source table |
| `place_text` | text | the place as written |
| `state_text` | text, nullable | the state as written, when the table has a state column |
| `year_text` | text, nullable | the year as written, when the table has a year column |
| `column_name` | text | the column header as written |
| `value_text` | text | the value as written, untouched |

The district, state and year columns themselves aren't stored as values; every other kept column is. Nothing is changed here, so the original table can always be rebuilt.

### 4.3 `web_clean`: cleaned and matched to the district list

| Column | Type | Notes |
|---|---|---|
| `survey_id` | int, indexed | |
| `web_table_id` | → `web_tables.id`, indexed | |
| `raw_id` | → `web_raw.id` | traces each value back to its raw line |
| `state_name`, `state_id` | | from `district_crosswalk.tsv` |
| `district_name`, `district_id`, `unique_id` | | from the crosswalk; `unique_id` empty when not matched |
| `matched` | boolean | district matched |
| `year` | text, nullable | in `YYYY-YYYY` form; empty when a year column's value wasn't recognised |
| `indicator` | text | cleaned column name (snake_case via the existing cleaning helpers) |
| `value` | double, nullable | parsed number ("85,000" → 85000); empty when not a number |

### 4.4 `web_master`: one row per table + district + year + indicator

| Column | Type | Notes |
|---|---|---|
| `survey_id` | int, indexed | |
| `web_table_id` | → `web_tables.id`, indexed | |
| `state_name`, `state_id`, `district_name`, `district_id` | | |
| `unique_id` | not null | only matched rows reach master |
| `year` | not null | only rows with a recognised year reach master |
| `indicator`, `value` | | |

Unique together: (`web_table_id`, `unique_id`, `year`, `indicator`). If one table lists the same district twice for one year, master keeps one value **(my call: the last row in the source table wins, and Clean reports it in `match_summary`)**.

## 5. The flow, step by step

### 5.1 Save (`POST /scrape/scans/{id}/import`)

Each item in the Save request gains:

| Field | Required | Notes |
|---|---|---|
| `place_column` | no | defaults to the column the review screen already guessed (`signals.joinable.place_column`) |
| `state_name` or `state_column` | one of them, unless the table has no place column | `state_column` defaults to the guessed `signals.joinable.state_column` |
| `year` or `year_column` | one of them | `year` is picked from a fixed list of financial years in HMIS's format (`2005-2006` … current), so it matches HMIS's `report_year` **(my call: a generated list, not read from HMIS data, so it works when the survey has no HMIS)**. `year_column` is for a table with a Year column: each row keeps its own year |
| `columns` | no | the value columns to keep. Used when years are in the column headings ("2018-19 \| 2019-20"): the admin ticks one year's columns, and they all get the chosen `year`. Omitted means all columns |
| `indicator_column` | no | **(added 2026-11-10)** for long tables, one row per district per indicator (NFHS district files). Each value is filed in `web_raw.column_name` under its row's indicator (plus the value column's heading when several are kept); rows with no indicator are skipped. Clean then folds the table to one row per district + state + year, so the repeats aren't reported as duplicate places and every indicator reaches master. Without it, ~100 NFHS indicators per district collapsed into one |

Per item, not per Save, because tables from one page can differ in year **(my call)**.

What Save now does, per ticked table:
1. Creates or reuses the `web_tables` row (see re-save below) and its `datasets` record card.
2. Writes the table's values into `web_raw`.
3. Sets status to `saved`. **No automatic clean.** Clean is the admin's next step, as with Census and HMIS **(my call)**.

Save no longer creates `ds_<id>` tables. Everything else about Save stays: one request, one background job (`scrape_import`), a second click gets 409, unticked findings and the staging folder are deleted afterwards.

**Same table, different years.** Government pages often show only the latest year at the same address. "The same table" means same survey + same `page_url` + same `source_locator` **+ same `year`** (for a table saved with `year_column`, without the year):
- Saving it **for a year not saved before** creates a new `web_tables` row. The old year stays.
- Saving it **again for the same year** replaces that year only: the row is reused (its `updated_at` changes), its `web_raw` rows are replaced, its `web_clean` / `web_master` rows are deleted, and its status goes back to `saved`.

This also fixes today's "searched and saved twice gives two copies".

A table with no place column can still be saved. It goes into `web_raw` and can be cleaned, but never reaches master (§5.3).

### 5.2 Clean (`POST /warehouse/web/clean`)

Register **`web`** as a third warehouse connector beside `census` and `hmis` (`_CONNECTORS` in `modules/warehouse/router.py`), with the same module interface: `summary`, `build_clean`, `build_master`, `export_layer`, `is_layer_table`, `RAW` / `CLEAN` / `MASTER`.

- The Clean button on a Website row cleans **that table only**. The endpoint takes `survey_id` and an optional Website table `external_id`; without one, it cleans all of the survey's Website tables.
- The pop-up's tick boxes (`operations`) apply as they do for Census and HMIS. They work on wide tables, so each Website table is rebuilt in its original wide form, cleaned, and turned back into one value per row.
- On top of them, Clean always:
  - parses values into numbers;
  - matches places to `district_crosswalk.tsv` **scoped to the state** (district names repeat across states), reusing the review screen's `scoring` district lookup (`lookup_district(state, name)`), and drops "Total"-style summary rows with its `is_summary_row`;
  - for a table with a year column, turns each row's year into `YYYY-YYYY` form ("2019-20", "2019–20", "2019-2020" → "2019-2020"). A year it can't read with certainty (e.g. a bare "2019", which could be a calendar or financial year) is left empty and reported, never guessed **(my call)**.
- Afterwards `web_tables.status = cleaned` and `match_summary` is filled, so the Fetched Data row can show it.

### 5.3 Master (`POST /warehouse/web/master`)

Builds `web_master` from `web_clean`: rows with a matched district and a recognised year only, one row per table + district + year + indicator. Runs when the Merge screen's "Rebuild master data from clean data before merging" is ticked, as for Census and HMIS.

A Website table is **mergeable** when it is cleaned and has at least one row in master. Otherwise the Merge screen greys it out with the reason: not cleaned, no district column, no district matched, or no year recognised.

### 5.4 Merge (`POST /datasets/merge`)

The merge request gains `web_tables`: a list of Website table `external_id`s. `connectors` keeps working for `census` / `hmis`. A request without `web_tables` includes no Website tables, so today's callers see no change. At least two sources in total, counting each Website table name as one source (§ column names below).

**Join rules:**
- **Census** varies by place only: it joins on `unique_id`, as today.
- **HMIS and Website** vary by place and year: they join on `unique_id` + year (HMIS's `report_year`, Website's `year`). A Website value for 2019-2020 lands on Patna's 2019-2020 HMIS rows, never on 2018-2019.
- Order: join the year-varying sources with each other on (`unique_id`, year), outer; then attach Census on `unique_id`.
- Outer join throughout: a district in only one source still gets a row. Rows with no `unique_id` are dropped, as today.
- Each Website table is turned from rows (indicator, value) into columns before joining.

**One shared year column.** `dashboard_master` gets a `year` column holding HMIS's `report_year` or, on a row with no HMIS match, the Website year. The Dashboard's Year filter uses it, so Website rows without an HMIS partner aren't dropped by the filter.

**Column names come from the table's name, so they're reused.** A Website column's name is built from the `web_tables.name` plus the indicator, plus a marker that says "Website" (exact pattern left to the builder; it must stay within Postgres's 63-character limit). Consequences:
- The same table saved for 2018-2019 and 2019-2020 has one name, so both years fill **the same columns** on different rows. Trends and the Year filter treat it as one series.
- The same name across surveys reuses the same columns too, which keeps `dashboard_master` from growing for every Save.
- If one merge ticks **two different tables with the same name** that share an indicator, Merge refuses with a message asking the admin to rename one. Nothing is overwritten silently.

**Column limit guard.** `dashboard_master` is shared by all surveys; Postgres allows at most 1,600 columns per table, and columns that were dropped still count. Before writing, Merge counts the columns the table already has (including dropped ones, e.g. from `pg_attribute`) plus the new ones it would add. If the total would pass **1,400**, it refuses with a clear message instead of writing **(the 1,400 threshold is the owner's choice; the counting method is the builder's)**.

HMIS values are year-to-date by period (MonthUpToJune … MonthUpToMarch), so one HMIS year has several rows per district. An annual Website value repeats on each of them, as Census values repeat today. §5.5 deals with that.

### 5.5 KPIs and Dashboard

- `kpi/aggregation.py` and `kpi/explorer.py` must treat Website columns as their own source: counted **once per district per year**, never once per merged row. Today the rule recognises Census columns by their `_census` ending; it needs to recognise Website columns by their marker too.
- **The backend declares Website sources itself.** Today the caller of `/kpi/aggregate` declares each source (column ending, place key, time columns; at most 10). When the merged frame has Website columns, the backend adds their source declaration automatically (place key `unique_id`, time column = the shared `year`), so the portal doesn't have to change its Dashboard calls **(my call)**. The 10-source limit counts Website tables as one source.
- **Generated KPIs** and **Ask with a prompt** read the merged frame's real columns, so they can use Website columns once aggregation handles them.
- The **28 fixed Dashboard cards** are unchanged.
- The Dashboard's State / District / Year filters work on Website rows: every Website row in `dashboard_master` has a district id and a value in the shared `year` column.

## 6. Provenance and reading data back

- Each saved Website table (per year) gets **one record card in `datasets`** (stage `raw`, connector `web_table` / `web_file` / `web_feed`, the survey stamped, `notes` naming the scan and finding), as Census and HMIS pulls do. `table_name` points at the shared `web_raw`. Whether clean / master also get record cards follows what the Census / HMIS warehouses do today.
- Shared Census / HMIS layers are read back per dataset through their `_dataset_id` column (`crud/dataset.py`). The web layers have no such column; the row reader, export and job tracing find a dataset's rows through `web_tables.dataset_id` → `web_table_id` (builder's mapping).
- The kept `scrape_scans` / `scrape_findings` rows remain the record of which Search and which table on the page each saved table came from.

## 7. API changes (for the portal team)

| Change | Endpoint |
|---|---|
| Scan and finding ids become UUIDs (§3.1) | all `/scrape/...` routes, job payloads and results |
| Save items gain `place_column`, `state_name` / `state_column`, `year` / `year_column`, `columns` | `POST /scrape/scans/{id}/import` |
| Save job result lists each saved Website table's `external_id` (no more `raw_dataset_id` / cleaned `dataset_id`) | `GET /jobs/{id}` |
| List a survey's Website tables, for the Fetched Data rows and the Merge tick boxes: name, page, state, year, status, rows, match summary, mergeable + reason | new, e.g. `GET /warehouse/web/tables?survey_id=` |
| Clean one Website table or all of a survey's | `POST /warehouse/web/clean` |
| Rebuild master | `POST /warehouse/web/master` |
| Merge chosen Website tables | `POST /datasets/merge` with `web_tables` |
| Row readers, summary, export for the web layers | the generic `/warehouse/web/...` routes |

`API_TESTING.md` section 11 and the warehouse / merge sections get updated.

## 8. Portal changes (other team)

- Search links use the scan's UUID.
- Save step, per ticked table: a district-column dropdown (pre-filled), a state dropdown or state column, a year dropdown **or** a year-column dropdown, and optionally which columns to keep.
- Fetched Data: one row per Website table (per year), with Jobs / Quality / Clean and the match summary.
- Merge Data: a "Website tables" list under Census India and HMIS, one tick box each, greyed out with the reason when not mergeable.
- No Dashboard call changes (§5.5).

## 9. Database migration

One new Alembic migration, after `b32eb5b4e8f3` (which is already applied on the owner's local Postgres, so it isn't rewritten). It:
1. Creates `web_tables`, `web_raw`, `web_clean`, `web_master`.
2. Converts `scrape_scans` and `scrape_findings` to the base, **keeping existing rows**: every row gets a new BIGINT `id` and a generated `external_id`, and `scrape_findings.scan_id` / `parent_id` are re-pointed to the new ids. Saved scans and their kept findings survive as provenance.
3. Unsaved scans can't keep their staging folders (named by the old id). The migration marks them expired so the existing startup purge removes them **(my call: they would have expired within 24 hours anyway)**.

**Generated only, never applied by us**; the owner applies it. `alembic/env.py` hides tables starting with `census_`, `hmis_`, `dashboard_`, `ds_`, `gemini_`; `web_` is not hidden and must not be, since these are ORM-managed. Read the generated migration before applying it; the id conversion in step 2 has to be hand-written, autogenerate won't produce it.

## 10. Left to the builder

- Module layout (a `web_warehouse` module mirroring `census_warehouse` / `hmis_warehouse` is the obvious fit).
- The exact Website column naming pattern in `dashboard_master` (§5.4 constraints apply).
- The parameter name for "this one Website table" on the generic warehouse routes.
- How the Save job reports progress.
- What the Quality button shows for a Website table: whatever it shows for Census / HMIS, applied to `web_clean`.
- How the column-limit guard counts existing columns.

## 11. Decisions made on 5 November 2026

By the product owner:
- Website data gets raw / clean / master layers and merges like Census and HMIS.
- The admin chooses the state and year at Save; Website figures join HMIS on district + year.
- Every Website-flow table, including `scrape_scans` / `scrape_findings`, gets the shared base (BIGINT id, UUID `external_id`, `created_at`, `updated_at`); no other table does.
- One tick box per Website table in Merge.
- Tables with no district column are greyed out in Merge; the admin can pick the district column at Save.
- The same table saved for another year is kept as another year of the same series, not replaced.
- Tables with a Year column keep each row's year; tables with years in the headings keep only the columns the admin ticks, under the chosen year.
- Website columns are named from the table's name so they're reused; Merge refuses past 1,400 columns.
