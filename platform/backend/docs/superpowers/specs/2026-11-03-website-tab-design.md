# "Website" tab: get data from any web page

Date: 3 November 2026
Status: phase 1 built (plain pages and files) on branch `feature/website-tab`; phase 2 (browser) built on `feature/website-tab-browser`.

> **Phase 1 as built (4 November 2026).** These decisions override the text below where they differ:
> - Ticked tables are stored like uploads: one raw `ds_<id>` dataset plus a cleaned copy each, with the survey on both.
> - Staging lives in `data/scrape_staging/<scan_id>/`, outside `data/raw/`, so `cleanup.purge_all()` never touches it.
> - Expired unsaved scans are deleted at startup and at the start of every new scan, not hourly.
> - One Save is one `POST /scrape/scans/{id}/import` carrying every ticked table, run as one job. A second Save on the same scan gets 409.
> - `POST /scrape/scan` has no `render` field yet; a page that needs JavaScript is reported as `needs_browser`.
> - A linked file that comes back as a web page (a firewall's CAPTCHA, a login, a "moved" page) is reported in "Couldn't read", not read as the file. A `.xls` that is an HTML table is still read.
> - §12: option **B** for phase 1. Saved tables do not reach the dashboard KPIs.
> - Connectors are `web_table` and `web_file` only (`web_feed` arrives with the browser in phase 2).
>
> **Phase 2 as built (4 November 2026).** These override the text below, and the phase 1 notes above, where they differ:
> - `POST /scrape/scan` takes `render` (`auto` / `always` / `never`, default `auto`). It is stored in `scrape_scans.options`; `rendered_in_browser` is in `summary` and at the top of the manifest. No new columns, so no migration.
> - In `auto` mode, the browser opens when the plain read found a JavaScript shell or no tables. A page blocked by bot protection, a CAPTCHA or a login is never opened in the browser.
> - New kinds: `rendered_table` (connector `web_table`) and `feed` (connector `web_feed`, export folder `api/`). A file's copy of a duplicate table still wins; otherwise a feed's copy wins over the rendered table.
> - New "Couldn't read" codes: `browser_unavailable` (Playwright or Chromium missing, or browser mode turned off with `render=always`) and `browser_failed`. Both keep the plain results.
> - The browser proxy paces the browser's first connection to each host (the shared 1 request/second throttle); what the page then loads by itself is not paced one by one. It caps everything the browser downloads at `PLATFORM_SCRAPE_BROWSER_MB` (100 MB).
> - A watchdog ends any render at its budget plus 5 seconds (closes the proxy, kills Chromium), so a stuck page can't hold a job worker; that is reported as `slow_response`. Pages with a CAPTCHA or login box are never rendered. Data feeds count against the scan's 200 MB total. `start`/`from` count as paging only next to a page-size parameter.
> - New settings: `SCRAPE_BROWSER_ENABLED` (true), `SCRAPE_BROWSER_SANDBOX` (true), `SCRAPE_BROWSER_SECONDS` (60, inside the 180 s scan), `SCRAPE_BROWSER_MB` (100), `SCRAPE_FEED_MAX` (30 feeds), `SCRAPE_FEED_MB` (10 per feed page), `SCRAPE_FEED_PAGES` (50), all with the `PLATFORM_` prefix.
> - Still not done: pages whose data appears only after a click or a choice (e.g. the SDG India Index), feeds sent by POST are read but not paged, no OCR.
Scope: the ETL backend in `platform/backend/`, plus the contract the nimbli.ai portal (its backend at `research.nimbli.ai/etl-ingest/...` and its frontend) needs to call it.

Statements marked **(my call)** are decisions made while writing this, not things the product owner asked for. They can be changed.

---

## 1. Intent

The portal's **Data Dashboard** lets an admin bring data in through two tabs today:

- **URL:** pick a known source (Census India, HMIS), which fills in the URL, set Type / Level / State, click Save.
- **Uploads:** upload a file.

Save calls `POST /etl-ingest/fetch` on the portal backend, which starts a **batch** with one ETL job per source and replies 202. The page then polls to follow each job. Everything after that (cleaning, master tables, merging with other sources, KPIs) is the existing pipeline.

The new **third tab, "Website"**, sits next to URL and Uploads. The admin pastes **any** web page address and clicks **Search**. The platform finds the data on that page and shows what it found. The admin **reviews and picks** what is worth keeping. Only the picked items go into the same batch → job → pipeline flow the other two tabs use.

Why: data the platform needs is spread over many government and institutional pages that have no dedicated connector. Today each one needs code. The Website tab makes "this page has a table I need" a few clicks, without letting junk into the warehouse.

## 2. Goals, non-goals, anti-goals

**Goals**

1. Paste one URL, click Search, and get back a reviewable list of everything usable on that page within about a minute for a typical page.
2. Find data wherever the page keeps it:
   - HTML tables,
   - tables that only appear after the page's JavaScript runs,
   - the page's own background data feeds (JSON it loads), including further pages of that feed,
   - data embedded in the page source,
   - downloadable files the page links to (CSV, Excel, PDF, Word, ZIP),
   - the page's readable text.
3. Show the admin, for each finding, enough to decide: a preview, its size, where on the page it came from, whether it can be joined to places (states/districts), and a suggestion (keep or skip).
4. Import only what the admin ticks, as ordinary raw datasets with a cleaned copy, under the chosen `survey_id`, through the existing job and batch tracking.
5. When something can't be read, say exactly why (blocked by bot protection, CAPTCHA, login, robots.txt, data behind a form) and, where possible, where the same data is officially available.

**Non-goals for the first version**

- Crawling a whole site. Scope is **the given page, plus files it links to, plus the data feeds it loads**. **(my call)**
- Turning free text or report prose into rows with AI. Text is kept, shown and downloadable, not imported into the pipeline. **(my call; candidate for a later phase)**
- OCR for scanned PDFs. They are listed and flagged "scanned, text not readable". **(my call; later phase)**
- Filling in search forms or dropdowns (state → district) to reach data. Detected and reported, not automated. **(my call; later phase)**
- Scheduled re-scraping of a page. **(my call; later phase)**
- Logging in to sites.

**Anti-goals (what would make this a failure even if it "works")**

- Junk in the warehouse: layout tables, menus, contact lists and duplicates imported because nothing stopped them. The review step exists to prevent this.
- Getting past access controls: no CAPTCHA solving, no logins, no proxy rotation to dodge blocks. (Changed 2026-11-12, user's decision to read almost any URL an admin gives: robots.txt is ignored unless `PLATFORM_SCRAPE_OBEY_ROBOTS=true`; requests look like a desktop Chrome; a refused page gets one second try in the headless browser; a report-filter form is submitted once with its defaults. What the browser can't get past either is reported.)
- The server fetching internal addresses because an admin typed one. This includes the cloud metadata service and the database host (see section 9).
- An import that silently loses where data came from. Every imported row must trace back to the page, the finding and the time.

## 3. The admin's flow

1. Open **Data Dashboard**. Choose **Website** (the third option, beside URL and Uploads).
2. Paste a URL. Optional: tick **"Render the page in a browser"** (default: automatic, see 4.3).
3. Click **Search**. The page shows progress messages from the scan, e.g. "Reading page", "Rendering page in a browser", "Found 3 data feeds", "Downloading files 4 of 9".
4. The **review screen** shows what was found, in four groups:
   - **Tables**: one card per table (HTML table, data feed, file sheet, PDF table). Each card shows:
     - a title, where it came from, and rows × columns,
     - the first rows as a preview,
     - badges: "Joinable: 36 of 38 districts matched", "Has numbers", "Looks like page layout", "Duplicate of table 3", "Personal data (phone numbers)",
     - a **Keep** checkbox, pre-ticked when the scan suggests keeping it,
     - an editable dataset name,
     - "See all rows" for a full, paged view.
   - **Files**: every file downloaded, with type, size and what was extracted (e.g. "Excel, 3 sheets → 3 tables above", "PDF, 12 pages, 2 tables", "PDF, scanned, no text"). Each can be downloaded.
   - **Text**: the page's main text, and text pulled from PDFs and Word files. Shown and downloadable. Not imported.
   - **Couldn't read**: anything blocked or skipped, with the reason and a suggestion.
5. Choose the **survey** (same meaning as on the other tabs), adjust ticks and names, click **Save**.
6. Save starts a batch exactly like the other tabs: one job per kept table, tracked with the existing progress UI. When jobs finish, each kept table is a raw dataset plus a cleaned copy, ready for the rest of the pipeline.

A scan that found nothing usable says so plainly, lists what it tried, and lists anything in "Couldn't read".

## 4. How Search works

Search starts a background job (`job_type = "scrape_scan"`) that runs these stages in order and writes progress text after each. Every stage has hard limits (section 4.8) so one bad page cannot hold a worker forever.

### 4.1 Safety and permission check

- Only `http` and `https`.
- Resolve the host and **refuse private, loopback, link-local and metadata addresses** (10/8, 172.16/12, 192.168/16, 127/8, ::1, 169.254/16 including 169.254.169.254, fc00::/7). Check again after every redirect and for every file download and browser request (section 9).
- (Since 2026-11-12 only when `PLATFORM_SCRAPE_OBEY_ROBOTS=true`; off by default.) Read the site's `robots.txt`. If the page is disallowed for crawlers, stop and report "This site asks automated tools not to read this page", with the official alternative if known.
- **Which requests the robots.txt check covers (my call):**
  - Checked, against the robots.txt of the host being requested: the page itself, every file download (on any host), and every feed page the scraper requests on its own when following a feed's further pages (4.3). A disallowed file or feed page becomes a `robots_disallowed` entry in "Couldn't read".
  - Not checked: requests the page makes by itself while it renders in the browser. Those are part of displaying the page the admin asked for, the same as a person's browser. So a feed the page loads on its own is still captured even when robots.txt disallows `/api/`. Only fetching *further* pages of it is subject to robots.txt.
- **Known hosts** (`censusindia.gov.in`, `hmis.mohfw.gov.in`, `data.gov.in`/`api.data.gov.in`): don't scrape. Reply "This source has a dedicated connector; use the URL tab", so the admin gets the better path. **(my call)** *(Removed 2026-11-12: these addresses can be searched too.)*

### 4.2 Plain download (always, first)

Fetch the URL with an ordinary HTTP request.

- **If the URL itself is a file** (by `Content-Type` or extension: CSV, TSV, XLS/XLSX, PDF, DOCX, ZIP, JSON), skip to file handling (4.5). There is no page to read.
- If it is HTML, extract:
  - **Tables**: every `<table>`, through pandas with the existing multi-row header flattening from `connectors/html_table.py`.
  - **Embedded data**: JSON-LD (`<script type="application/ld+json">`), Next.js `__NEXT_DATA__`, and other `<script type="application/json">` blocks. Arrays of records inside them become table findings, using the existing `rest_api.find_records` heuristic.
  - **File links**: `<a href>` pointing at the file types above, resolved to absolute URLs.
  - **Main text**: readable article text without menus and footers.
  - **"Is this an empty JavaScript shell?"**: very little visible text, few or no tables, heavy scripts, or a known app root such as an empty `<div id="root">`. This is the signal for 4.3.

### 4.3 Browser rendering (only when needed)

Open the page in a headless Chromium (Playwright) when:
- the plain download looked like a JavaScript shell, or
- it found no tables and no embedded data, or
- the admin ticked "Render the page in a browser".

In the browser:
- Load the page, wait for network quiet, scroll to the bottom a few times to trigger lazy loading.
- **Capture background data feeds**: every JSON response the page receives, minus analytics and tracking noise (the filter list from the `data_first.py` prototype). For each, find arrays of records (`find_records`). Each array with at least 2 records and at least 2 fields becomes a feed finding.
- **Follow a feed's further pages**: if the feed request has a page or offset parameter (`page`, `pageNo`, `offset`, `start`, `skip`, …), request the next pages directly until a page comes back empty or repeats, up to the page limit. Stack the records. The `data_first.py` prototype did this on quotes.toscrape.com: 100 of 100 records in 10 requests.
- Read tables again from the rendered page.
- **Export buttons**: find buttons or links whose text says Export / Download / Excel / CSV. Click up to 3, and treat any file that downloads as a linked file (4.5).
- Detect blockers (4.6).

### 4.4 Choosing what to download

- Download files linked from the page (4.2) or produced by export buttons (4.3).
- Same-site links first, then other sites. Up to the file limit (4.8). Skip images, scripts, styles and media.
- **(my call)** Files on other sites are allowed (government pages often link to a CDN or a different ministry domain) but go through the same safety check.

### 4.5 Reading files

| File | What is extracted |
|---|---|
| CSV / TSV | One table |
| XLSX / XLS | One table per non-empty sheet (via the existing `csv_excel` reader) |
| JSON | Arrays of records → tables (`find_records`) |
| PDF with a text layer | Text of every page, plus tables detected on each page (pdfplumber) |
| PDF without a text layer (scanned) | Listed and flagged "scanned, no text"; nothing extracted (OCR is out of scope) |
| DOCX | Text, plus every Word table as a table |
| ZIP | One level deep: each inner file handled by its type, within the file limits; nested ZIPs ignored |
| Anything else | Listed as a file, downloadable, nothing extracted |

Many government `.xls` files are really HTML tables; HMIS ones are. The reader must try HTML when Excel parsing fails, as the HMIS connector already does.

### 4.6 Recognising what blocked us

Record a "Couldn't read" entry, with a reason code, for:

| Code | Detected by | Message to the admin |
|---|---|---|
| `robots_disallowed` | robots.txt | The site asks automated tools not to read this page. |
| `bot_protection` | 403/429/503 with challenge text ("Access Denied", "Just a moment", "Verify you are human") or a known challenge script | The site blocked automated access. |
| `captcha` | reCAPTCHA / hCaptcha / Turnstile / image CAPTCHA on the page or in front of data | Data is behind a CAPTCHA. |
| `login_required` | redirect to a login page, or a password field covering the content | The page needs a login. |
| `form_gated` | `<select>` elements plus a submit button and no data until submitted | Data appears only after choosing options in a form. |
| `too_large` / `timeout` / `unsupported` | limits in 4.8 | Self-explanatory, with the limit that was hit. |

Where a known official alternative exists, add it as a suggestion, e.g. LGD district lists are behind a CAPTCHA, but the same data is on data.gov.in. **(my call)** Keep these suggestions in a small editable lookup (host → suggestion text), not in code.

### 4.7 Judging each table (suggestions only; the admin decides)

For each table finding compute:

- `rows`, `columns`, share of numeric cells.
- **Looks like layout**: fewer than 2 data rows, or one column, or mostly links/navigation words, or a single cell spanning the table → suggest skip.
- **Duplicate**: same content hash as another finding (e.g. the HTML table and the CSV it links to) → suggest keeping only one, preferring the file.
- **Joinable**: find state/district columns with the existing `merging/semantic.py` matcher, then look the values up in the district master (`constants/district_crosswalk.py`, matching within each state). Report "matched X of Y places". This tells the admin whether the table can combine with Census/HMIS.
- **Personal data**: columns that look like phone numbers, email addresses or personal names → badge "Personal data". **(my call)** Not blocked, but never pre-ticked.
- **Suggestion**: keep if not layout, not a duplicate, at least 3 rows and 2 columns, and some numbers; otherwise skip.

### 4.8 Limits (defaults; all settings with the `PLATFORM_` prefix) **(my call on the numbers)**

| Limit | Default |
|---|---|
| Whole scan | 180 s (600 s since 2026-11-12) |
| Single page load / request | 30 s (the existing `request_timeout_seconds`) |
| Requests to one host | 1 per second |
| Feed pages followed per feed | 50 |
| Files downloaded | 25 (100 since 2026-11-12) |
| One file | 30 MB (100 MB since 2026-11-12) |
| All files in a scan | 200 MB (500 MB since 2026-11-12) |
| ZIP: inner files / unpacked size | 50 files / 300 MB |
| Table rows kept per finding | 1,000,000 (larger is truncated and flagged) |
| Preview | first 20 rows × 30 columns |
| Text per finding shown in the manifest | first 1,000 characters (full text downloadable) |

## 5. Structured and unstructured data

| What | Kind | In the review | Imported into the pipeline? |
|---|---|---|---|
| HTML table (plain or rendered) | structured | table card | yes, if ticked |
| Background data feed | structured | table card | yes, if ticked |
| Embedded page data (JSON-LD, Next.js) | structured | table card | yes, if ticked |
| CSV / Excel sheet / JSON from a file | structured | table card | yes, if ticked |
| Table inside a PDF or Word file | structured, less reliable | table card, badge "from PDF, check headers" | yes, if ticked |
| Page main text | unstructured | text block | no (kept with the scan) |
| Text of PDFs / Word files | unstructured | text block per file | no (kept with the scan) |
| Scanned PDF | unstructured, unreadable | file entry, flagged | no |
| Other files (PPT, images in documents, …) | unstructured | file entry | no |

Unstructured items stay attached to the scan and remain downloadable for as long as the scan is kept (section 6). Turning them into rows (AI extraction, OCR) is a later phase and would produce table findings that go through the same review.

## 6. Storage

Two stages, so nothing reaches the warehouse before the admin picks it.

### 6.1 Staging (after Search, before Save)

- **New ORM tables** (an Alembic migration is generated; the owner applies it by hand, as with every schema change here):
  - `scrape_scans`: `id`, `url`, `final_url` (after redirects), `status` (`running` / `done` / `failed`), `job_id`, `options` (JSON), `summary` (JSON: counts per group), `blocked` (JSON list), `created_at`, `finished_at`, `expires_at`.
  - `scrape_findings`: `id`, `scan_id`, `kind` (`html_table` / `rendered_table` / `feed` / `embedded` / `file_table` / `pdf_table` / `docx_table` / `text` / `file`), `title`, `locator` (where it came from: table index, feed request URL, file URL + sheet/page), `rows`, `columns`, `column_names` (JSON), `signals` (JSON: layout, duplicate_of, joinable, personal_data, numeric_share), `suggested` (`keep` / `skip`), `storage_path`, `content_hash`, `imported_dataset_id` (null until imported).
- **Files on disk**, under `data/scrape_staging/<scan_id>/` (setting `PLATFORM_SCRAPE_DIR`):
  - each downloaded file as fetched, with a `.meta.json` beside it (URL, time, size, sha256, content type), the same convention as HMIS;
  - each table finding's full data as a Parquet file (keeps column types; needs `pyarrow`) **(my call)**;
  - each text finding as a `.txt` file.
- **Only what the admin picks is stored (owner's decision).** Everything Search finds is held only for as long as the review takes, and is then thrown away. The staging area is a short-lived holding area, never an archive. Storage cost is the reason.
  - **On Save**, once the import jobs finish: delete the whole `data/scrape_staging/<scan_id>/` folder (every downloaded file, every Parquet file, every text file, picked or not), and delete the `scrape_findings` rows that were not picked. What stays is the `scrape_scans` row and the picked findings' rows. Both are small metadata (URL, locator, row/column counts, `content_hash`), kept so every imported table can say exactly where it came from.
  - **If the admin never saves:** the whole scan (rows and files) is deleted 24 hours after Search finishes (setting `PLATFORM_SCRAPE_HOLD_HOURS`, default 24) **(my call on the number)**. A cleanup runs at startup and at the start of every new scan. The admin can also discard a scan straight away with `DELETE /scrape/scans/{id}`.
  - Text findings, files that couldn't be read, and anything in "Couldn't read" are never stored beyond that hold.
  - The original downloaded file behind an imported table is **not** kept. This matches the platform's default `PLATFORM_DELETE_SOURCE_FILES_AFTER_INGEST=true`: the rows are in the database and the file's sha256 is recorded, so the file itself isn't needed.
- **`ingestion/cleanup.purge_all()` never sees the staging folder**, because it lives outside `data/raw/`. The rules above manage that folder instead.

### 6.2 Import (after Save)

For each ticked table finding, an import job (`job_type = "scrape_import"`):

1. Loads the finding's Parquet file.
2. Applies the admin's dataset name.
3. Saves it as a **raw dataset** in its own `ds_<id>` table, with the existing `store_result` path:
   - `Dataset.survey_id` = the chosen survey,
   - `source_ref` = page URL + finding locator,
   - `raw_file_path` = none (the file is not kept; see 6.1),
   - `notes` = scan id, finding id, kind, row and column counts, and the source file's sha256.
4. Makes the **cleaned copy** with the existing `store_clean_copy`, so it lands in the same state as a CSV or HTML fetch today.
5. Records `imported_dataset_id` on the finding.

**New connector names** for provenance and export foldering (`reports/naming.py`) **(my call)**:

| Connector | Covers | Export folder |
|---|---|---|
| `web_table` | HTML and rendered tables, embedded data | `web/` |
| `web_feed` (phase 2) | Background data feeds | `api/` |
| `web_file` | Tables from downloaded files (CSV, Excel, PDF, Word, ZIP) | `file/` |

## 7. Backend API (ETL service)

All under the existing app (behind the proxy as `/etl/...`). No authentication, the same as every other endpoint here. The portal backend is the caller and is responsible for who may use it.

### 7.1 `POST /scrape/scan` → 202, a job

```json
{
  "url": "https://example.gov.in/health/district-statistics",
  "render": "auto",
  "include_files": true
}
```

`render` (**phase 2; not accepted in phase 1**): `auto` (default; browser only when needed) | `always` | `never`. Phase 1 takes `url` and `include_files` only. Reply: the standard `JobOut`, with `job_type: "scrape_scan"`. When the job succeeds, `result` holds `{"scan_id": "...", "summary": {...}}`. Progress text is updated after every stage.

### 7.2 `GET /scrape/scans/{scan_id}` → the manifest (what the review screen draws)

```json
{
  "scan_id": "a1b2…",
  "url": "https://example.gov.in/health/district-statistics",
  "final_url": "https://example.gov.in/health/district-statistics/",
  "status": "done",
  "rendered_in_browser": true,
  "created_at": "2026-11-03T09:40:12Z",
  "expires_at": "2026-11-04T09:40:12Z",
  "summary": {"tables": 7, "files": 5, "text": 3, "blocked": 1, "suggested_keep": 3},
  "tables": [
    {
      "finding_id": "f01",
      "kind": "html_table",
      "title": "District-wise institutional deliveries 2023-24",
      "locator": {"source": "page", "table_index": 2},
      "rows": 38, "columns": 9,
      "column_names": ["District", "Total deliveries", "Institutional", "…"],
      "preview": [["Araria", 41230, 39876, "…"], ["Arwal", 9120, 8800, "…"]],
      "signals": {
        "looks_like_layout": false,
        "duplicate_of": null,
        "numeric_share": 0.78,
        "joinable": {"place_column": "District", "level": "district", "matched": 36, "total": 38, "unmatched": ["Kaimur Bhabua", "Purba Champaran"]},
        "personal_data": []
      },
      "suggested": "keep",
      "default_name": "example.gov.in - District-wise institutional deliveries 2023-24"
    }
  ],
  "files": [
    {"finding_id": "f09", "name": "annual_report_2023.pdf", "url": "https://…/annual_report_2023.pdf",
     "content_type": "application/pdf", "bytes": 2481203, "extracted": "PDF, 48 pages, 2 tables, text",
     "produced": ["f10", "f11", "t02"]}
  ],
  "text": [
    {"finding_id": "t01", "title": "Page text", "source": "page", "characters": 6120, "snippet": "The district health society…"}
  ],
  "blocked": [
    {"what": "https://example.gov.in/reports/district-wise", "code": "form_gated",
     "message": "Data appears only after choosing options in a form.", "suggestion": null}
  ]
}
```

Rules for the reply: cells are JSON-safe (NaN → `null`, the existing `json_safe_records` helper). Previews are capped (4.8). Everything is UTF-8, so Hindi and other scripts come through intact.

### 7.3 `GET /scrape/scans/{scan_id}/findings/{finding_id}/rows?limit=50&offset=0`

Full rows of one table finding, paged: `{"total": 38, "columns": [...], "rows": [...]}`. Used by "See all rows".

### 7.4 `GET /scrape/scans/{scan_id}/findings/{finding_id}/download`

- A file finding → the original file.
- A table → CSV (UTF-8 with BOM, as other exports).
- Text → `.txt`.

### 7.5 `POST /scrape/scans/{scan_id}/import` → 202, a job

```json
{
  "survey_id": 7,
  "items": [
    {"finding_id": "f01", "name": "Bihar institutional deliveries 2023-24"},
    {"finding_id": "f10"}
  ]
}
```

Only table findings can be imported; others → 422. A finding already imported → 409 with its `dataset_id`. **(my call)** Reply: `JobOut` with `job_type: "scrape_import"`. On success, `result`:

```json
{"items": [{"finding_id": "f01", "raw_dataset_id": "…", "dataset_id": "…", "rows": 38, "columns": 9}]}
```

`dataset_id` is the cleaned dataset, the same as a fetch job's. A Save is one request with every ticked table; once it succeeds the scan is `imported` and another Save gets 409.

### 7.6 Also

- `GET /scrape/scans?limit=20`: recent scans (history list).
- `DELETE /scrape/scans/{scan_id}`: remove a scan that was not imported, with its files.

### 7.7 Job integration

Add `scrape_scan` and `scrape_import` to the dispatcher in `jobs/service._run`.

- They share the existing pool of 2 workers. **(my call)**
- At most one browser runs at a time in the process (a lock), because Chromium is memory-heavy.
- `jobs/lineage.py` must resolve an import job to its datasets, so existing "export what this job produced" keeps working.

## 8. Portal side (nimbli.ai backend and frontend)

This code is not in this repository. The design only fixes the contract it needs.

**Portal backend.** Add pass-through routes beside `/etl-ingest/fetch`, e.g.:

- `/etl-ingest/scrape/scan`
- `/etl-ingest/scrape/scans/{id}`
- `…/findings/{id}/rows`
- `…/findings/{id}/download`
- `…/import`

**Save** on the Website tab should create a **batch** exactly as the other tabs do, with **one result entry per kept table**:

```json
{
  "status": true,
  "batch_id": "…",
  "message": "3 of 3 source(s) started",
  "results": [
    {"status": true, "source": "Website", "finding_id": "f01", "job": {"job_id": "…", "status": "queued", "…": "…"}}
  ]
}
```

**(my call)** One import job can carry several findings (7.5). The portal can either:
- send all ticked findings in one import job and show one result entry, or
- send one import per finding to keep "one entry per source" like today.

**Phase 1 decision: the first.** One Save is one import job carrying every ticked table, because the job deletes the staging folder when it finishes; separate imports per table would delete files the others still need. The portal shows one batch entry for the Save, and the job's `result.items` lists each saved table.

**Frontend states for the tab:** idle → scanning (show the job's `progress` text) → review (draw the manifest) → saving → tracked in the existing batch view. "See all rows" pages through 7.3. File and text downloads go through the portal backend to 7.4.

**Known bug to fix in the same area:** on the URL tab, Save gets **202 Accepted** with a valid batch reply, but the page shows "Network error, please try again." The page treats this success as a failure, so admins may click Save again and start duplicate jobs. The Website tab will hit the same handling.

## 9. Security and responsibility

- **Server-side request forgery.** The single most important rule here: an admin-typed URL makes the server fetch things.
  - Every outgoing request (first page, redirects, file downloads, feed pages, and every request the headless browser makes) must pass the private-address check in 4.1.
  - Resolve DNS once per request and connect to the checked address, so a hostname cannot resolve to a public address at check time and a private one at connect time.
  - **Plain HTTP requests (phase 1):** a custom `requests` transport (or connection adapter) resolves, checks, and connects to that exact address. The Host header and TLS name stay those of the original hostname.
  - **Browser requests (phase 2), my call:** checking URLs in Playwright's `route` is not enough, because Chromium does its own DNS lookup after the check. Instead, launch Chromium with **all its traffic going through a small filtering forward proxy** run by the scraper, on localhost and only for that browser. For every connection (including HTTPS `CONNECT`), the proxy resolves the host, applies the private-address check, and connects to the checked address, or refuses. The proxy only relays; it does not decrypt HTTPS. This gives the same guarantee as the plain-HTTP path without serving every browser request from Python. Playwright `route` is still used, but only to log requests and capture feed responses.
- **Downloaded files are data, never code.**
  - Store them under the scan folder with safe names (reuse `hmis_reports.safe_filename`).
  - Never execute or open them with anything but the parsers listed in 4.5.
  - Enforce the size and ZIP limits in 4.8 while streaming, not after.
- **Browser:** headless Chromium in its normal sandbox. No extensions, no stored profile, a fresh context per scan, downloads only into the scan folder.
- **Honest identity:**
  - A fixed User-Agent naming the platform generically (e.g. `NimbliDataBot/1.0`), not pretending to be a person's browser. **(my call)** *(Changed 2026-11-12: a desktop Chrome User-Agent, because government firewalls refuse unknown agents.)*
  - No contact email in it unless the owner chooses one.
  - The plain-HTTP and browser stages use the same identity.
- **Polite rate:** 1 request per second per host (4.8). Obey robots.txt (4.1). *(Since 2026-11-12 robots.txt only when turned on; a 429/503 is retried once, at most twice per host per Search.)*
- **Personal data:** flagged in review (4.7) and never pre-ticked. India's DPDP Act applies to personal data even when it is publicly visible.

## 10. What's required to build it

**Python packages** (added to `requirements.txt`):

| Package | Why |
|---|---|
| `playwright` | Headless browser for JavaScript pages and capturing data feeds |
| `pdfplumber` | PDF text and table extraction |
| `python-docx` | Word text and tables |
| `trafilatura` | Main page text without menus and footers |
| `pyarrow` | Parquet staging of findings |

`beautifulsoup4`, `lxml`, `html5lib`, `openpyxl`, `pandas` and `requests` are already there. `.xls` reading may also need `xlrd` if any real Excel 97 files appear (HMIS `.xls` are HTML, so not today).

**Server setup**

- Install Chromium and its system libraries on the server (`playwright install --with-deps chromium`), about 300–400 MB of disk.
- Budget roughly 0.5–1 GB of extra memory while a browser scan runs.
- Disk for `data/scrape_staging/` (temporary only: bounded by the per-scan limit of 200 MB, emptied on Save or after 24 hours).
- The server needs outbound internet access.

**Database:** two new tables (6.1). The migration is generated with `alembic revision --autogenerate`. Check the generated file before applying, because autogenerate currently also tries to drop the `gemini_*` tables (they are missing from `alembic/env.py`'s exclusion list). Add `scrape_` handling to that list only if the tables are managed outside the ORM; here they are ORM tables, so no exclusion is needed.

**Backend code** (new module `app/modules/scraping/`, following the existing router / service / schemas layout):

- `router.py`, `schemas.py`, `service.py`: the endpoints in section 7.
- `safety.py`: URL and address checks, robots.txt.
- `discover_http.py`: stage 4.2.
- `discover_browser.py`: stage 4.3 (feed capture, paging, export clicks). This also fills in the empty `connectors/dynamic_site.py` placeholder or replaces it.
- `files.py`: 4.4–4.5.
- `blocked.py`: 4.6.
- `scoring.py`: 4.7.
- `store.py`: staging files, expiry.
- ORM models for the two tables in `app/models/`.
- Two job types in `jobs/service.py`, lineage support in `jobs/lineage.py`.
- New connector names in `reports/naming.py`.
- Registration in `routes/__init__.py`. The `/scrape` prefix doesn't clash with `/datasets/{id}`.

**Reused as is:**
- `html_table._flatten_columns`, `rest_api.find_records`, `csv_excel._read_table`,
- `hmis_reports.safe_filename`,
- `crud/dataset.store_result` path, `ingestion.service.store_clean_copy`,
- `cleaning.clean_dataframe`, `merging/semantic.py`, `constants/district_crosswalk.py`,
- `helpers/dataframe.json_safe_records`.

**Tests:** the repo has no test suite. Add `pytest` with:

- saved HTML fixtures served by a local test server, covering:
  - a plain table page,
  - a JavaScript-rendered table,
  - a paged JSON feed,
  - a page linking CSV/XLSX/PDF/ZIP,
  - a layout-table page,
  - a CAPTCHA page,
  - a login redirect,
  - a robots.txt disallow,
  - a redirect to a private address;
- unit tests for the address check (including DNS rebinding and redirects), scoring, and ZIP limits;
- one optional live smoke list (e.g. rbi.org.in, quotes.toscrape.com) not run in CI.

**Portal side:** the proxy routes and batch handling in section 8, the new tab UI, and the 202 handling fix.

## 11. Phases

1. **Static pages and files.** 4.1, 4.2, 4.4–4.7 without the browser, plus staging, the manifest, import and the portal tab. This alone covers ordinary government pages with tables and downloadable files, as in the earlier RBI test (42 tables, 6 PDFs).
2. **Browser.** 4.3: JavaScript pages, data feeds and feed paging, export buttons.
3. **Later (each its own decision):** OCR for scanned PDFs; AI extraction from text into rows; form-gated data; scheduled re-scrapes.

Rough effort, for planning only: phase 1 about 2 weeks for one backend developer, plus the portal work; phase 2 about 1–1.5 weeks. Real sites will need follow-up fixes after launch.

## 12. Getting imported tables into the dashboard (phase 1: option B)

The owner's expectation is that after import "the rest of the flow is the same": processing, combining, KPIs. **That is not true today for any generic table**, whether from this tab, the Uploads tab, or a CSV/HTML fetch:

- A **survey merge** (`POST /datasets/merge` with `survey_id`) only reads the Census and HMIS **master** tables (`merging/survey.WAREHOUSES`). Datasets in their own `ds_<id>` tables are not included, even with the same `survey_id`.
- A merge by **`dataset_ids`** can include them, but its result is a plain dataset, not `dashboard_master`. All KPI endpoints read `dashboard_master`.

So an imported web table can be cleaned, viewed and exported, but won't reach the dashboard KPIs without a change. Options:

- **A. Include a survey's generic datasets in the survey merge (recommended).**
  - At import, resolve the table's place column to `unique_id` with the district master (4.7 already finds it).
  - In the survey merge, add every clean dataset of that survey that has `unique_id`, with its columns suffixed (e.g. `_web1`).
  - Treat such tables like Census in `kpi/aggregation.py` (one row per place) unless they carry a time column.
  - This also fixes the same gap for the Uploads tab.
  - Two details if A is chosen **(my calls)**:
    - A table with districts but no state column can't be matched safely, because district names repeat across states. The review card then asks the admin to pick the state (one state for the whole table). Without a state, the table imports but is marked "not joinable" and stays out of the merge.
    - Importing the same page and table (same URL and finding locator) again under the same survey **replaces** the earlier import. The old dataset is retired the way `clear_survey` retires old datasets, so the dashboard never counts the same figures twice. This matches how re-fetching under a survey works everywhere else.
- **B. Leave the pipeline as it is.** Imported tables stay standalone datasets, usable through dataset-id merges and exports but not the dashboard.
- **C. A separate "web" warehouse** (`web_raw/clean/master`) like Census/HMIS. Not recommended: tables from different sites share no columns, so one shared table would grow without limit.

## 13. Left to the builder

- Exact HTML heuristics for "looks like layout" and "JavaScript shell", tuned on the fixtures.
- The noise-filter list for background requests (start from the `data_first.py` prototype).
- How a feed's paging parameter is detected and stepped, within the page limit.
- Table titles: nearest caption, heading or file name.
- Progress message wording.
- The internal split of the scraping module, as long as the endpoints and data shapes in section 7 hold.

## Reference: earlier prototypes

Two throwaway prototypes from an earlier research session, `oneclick.py` (crawling) and `data_first.py` (find the page's own data feeds), showed what works and where real sites push back.

- **Worked:**
  - RBI: tables and PDFs.
  - Y Combinator's company directory: 1,000 records from one feed request.
  - quotes.toscrape.com: feed paging.
- **Blocked:**
  - mohfw.gov.in: bot protection.
  - LGD district lists: CAPTCHA.
  - data.gov.in: robots.txt (it has an official API instead).

They live in a temporary folder that the OS may clear (`/private/tmp/claude-501/-Users-admin-Desktop-live-projects/ccbf023f-268f-424e-b7a9-e861b2907e19/scratchpad/poc/`). Copy them somewhere permanent if they are to be reused.
