# Website Tab Phase 2 (Browser) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pages that build their content with JavaScript are opened in a headless Chromium, so their tables, background data feeds (with their further pages) and export-button files are found, instead of being reported as `needs_browser`.

**Architecture:** The plain read (phase 1) always runs first. When it looks like a JavaScript shell or found no tables (or the admin asked), `discover_browser.render()` opens the page in Playwright Chromium whose every connection goes through a localhost filtering proxy (`browser_proxy.py`) that applies the same address guard as the plain path. The rendered HTML goes through the existing `parse_page`; captured JSON responses become feed tables (`feeds.py`), with further pages fetched by the existing `Fetcher`; export downloads go through the existing `add_file`. If the browser can't run, the scan keeps the plain results and says why.

**Tech Stack:** Python 3.10, Playwright (sync API) + Chromium, stdlib `socketserver`/`http.server` for the proxy, pytest + pytest-httpserver.

**Spec:** `docs/superpowers/specs/2026-11-03-website-tab-design.md` (§4.1 robots coverage, §4.3, §4.8, §6.2 connector names, §7.1 `render`, §7.2 `rendered_in_browser`, §7.7 lock, §9 browser proxy, §10).

## Global Constraints

- `render`: `auto` (default; browser only when needed) | `always` | `never`.
- Browser when (auto): the plain download looked like a JavaScript shell, or found no tables and no embedded data.
- Every browser connection passes the private-address check: Chromium's traffic all goes through a scraper-run forward proxy on localhost that resolves, checks, and connects to the checked address, or refuses. It relays only; no HTTPS decryption.
- Requests the page makes by itself while rendering are not robots-checked; further feed pages the scraper requests itself are (a disallowed one becomes a `robots_disallowed` entry).
- Feed pages followed per feed: 50. Whole scan: 180 s. Page/request: 30 s. 1 request/second/host.
- Feed finding: an array with at least 2 records and at least 2 fields. Paging params: `page`, `pageNo`, `offset`, `start`, `skip`, …; stop when a page comes back empty or repeats, or at the limit.
- Export buttons: text says Export / Download / Excel / CSV; click up to 3; a downloaded file is treated as a linked file (4.5).
- At most one browser runs at a time in the process (a lock).
- Headless Chromium in its normal sandbox, no extensions, no stored profile, fresh context per scan. Same User-Agent as the plain path (`NimbliDataBot/1.0`).
- New kinds `rendered_table` and `feed`; connector `web_feed` → export folder `api/`; `rendered_table` → `web_table`.
- No new database columns: `render` lives in `scrape_scans.options`, `rendered_in_browser` in `summary`. No migration.
- Never apply migrations (owner's rule). Blocks are reported, never worked around (a CAPTCHA/login/bot-protection page is not rendered).

## Review Focus

1. Chromium (or the `playwright` package) missing on the server → the scan still finishes with the plain results plus a `browser_unavailable` entry; it does not fail. (Task 5 test `test_browser_unavailable_keeps_plain_results`.)
2. A page that never goes network-quiet (polls every second) → render stops within its budget and the scan finishes. (Task 3 test `test_render_page_that_never_goes_quiet`.)
3. A rendered page that tries to reach a private address (script fetch / redirect) → the proxy refuses it and it is listed; the browser cannot bypass the proxy for loopback. (Task 2 `test_connect_to_private_address_refused`, Task 3 `test_browser_cannot_reach_loopback_without_proxy_approval`.)
4. A JSON response that is huge, not valid JSON, or analytics noise → skipped silently, no crash. (Task 3 `test_feed_capture_skips_noise_and_bad_json`.)
5. An export button that navigates away instead of downloading → clicking stops, what was found is kept. (Task 3 `test_export_click_that_navigates_stops_clicking`.)

---

### Task 1: Contract, settings, kinds and messages

**Files:**
- Modify: `app/core/config.py` (after `scrape_hold_hours`), `app/modules/scraping/safety.py` (`MESSAGES`), `app/modules/scraping/service.py` (`TABLE_KINDS`, `create_scan`, `manifest`), `app/modules/scraping/schemas.py`, `app/modules/scraping/router.py`, `app/modules/scraping/importer.py` (`CONNECTOR_FOR_KIND`, `_source_ref`), `app/modules/scraping/scoring.py` (`mark_duplicates` preference), `app/modules/reports/naming.py`, `requirements.txt` (CRLF!), `pytest.ini`, `tests/conftest.py`
- Test: `tests/scraping/test_router.py`, `tests/scraping/test_blocked.py`, `tests/scraping/test_scoring.py`, `tests/scraping/test_importer.py`

**Interfaces:**
- Produces settings: `scrape_browser_enabled: bool = True`, `scrape_browser_sandbox: bool = True`, `scrape_browser_seconds: int = 60`, `scrape_browser_mb: int = 100`, `scrape_feed_max: int = 30`, `scrape_feed_mb: int = 10`, `scrape_feed_pages: int = 50`.
- Produces message codes `browser_unavailable` ("Browser mode is not available on this server, so only a plain read was done."), `browser_failed` ("The page could not be opened in a browser, so only a plain read was done."); `needs_browser` becomes "This page builds its content with JavaScript; it can only be read with browser rendering."
- `service.TABLE_KINDS` gains `rendered_table`, `feed`. `create_scan(db, url, include_files, render="auto")` stores `options["render"]`.
- `Manifest.rendered_in_browser: bool` (from `summary.get("rendered_in_browser", False)`). `ScanRequest.render: Literal["auto", "always", "never"] = "auto"`.
- `CONNECTOR_FOR_KIND["rendered_table"] = "web_table"`, `["feed"] = "web_feed"`; `naming.CONNECTOR_CATEGORY["web_feed"] = "api"`. `_source_ref` uses `locator["feed_url"]` as the base for feeds.
- `scoring.mark_duplicates` keeper preference: file kinds, then `feed`, then first.
- `tests/conftest.py` sets `PLATFORM_SCRAPE_BROWSER_ENABLED=false` (browser tests opt in); `pytest.ini` registers marker `browser`.

- [ ] **Step 1: Write failing tests**
  - `test_router.py::test_scan_accepts_render_option`: POST `{"url": ..., "render": "always"}` → 202 and `scan.options["render"] == "always"`; `{"render": "sometimes"}` → 422; omitted → `"auto"`.
  - `test_router.py::test_naming_categories` add `category_for("web_feed") == "api"`.
  - `test_blocked.py`: update the `needs_browser` text assertion to the new copy; add `browser_unavailable`, `browser_failed` to the code list.
  - `test_scoring.py::test_feed_wins_over_rendered_table_duplicate`: `mark_duplicates([("a","rendered_table","h"),("b","feed","h")]) == {"a": "b"}`; with a `file_table` member present the file still wins.
  - `test_importer.py::test_source_ref_for_feed`: locator `{"source":"feed","feed_url":"https://x/api?page=1","path":"$.data","pages":3}` → `"https://x/api?page=1 [path=$.data, pages=3]"`.
- [ ] **Step 2:** `venv/bin/python -m pytest tests/scraping -q` → the new tests FAIL.
- [ ] **Step 3:** Implement the interfaces above. Router passes `body.render` to `create_scan` and into the job payload. Add `playwright>=1.49,<2` to `requirements.txt` keeping CRLF.
- [ ] **Step 4:** `venv/bin/python -m pytest -q` → all pass.
- [ ] **Step 5:** Commit `feat(scraping): render option, feed/rendered kinds and browser settings`.

### Task 2: Filtering forward proxy

**Files:**
- Create: `app/modules/scraping/browser_proxy.py`
- Modify: `app/modules/scraping/safety.py` (rename `_resolve_checked` → `resolve_checked`, keep behaviour)
- Test: `tests/scraping/test_browser_proxy.py`

**Interfaces:**
- Produces `class FilteringProxy(policy: AddressPolicy, byte_budget: int, throttle: HostThrottle = THROTTLE)`, a context manager; `.url -> str` (`"http://127.0.0.1:<port>"`), `.refused: list[str]` (hosts refused, in order, unique), `.over_budget: bool`, `.close()`.
- Behaviour: `CONNECT host:port` → `resolve_checked(host, port, policy)`; refused → `403` and host recorded; else `THROTTLE.wait(host)`, connect to the checked IP, reply `200 Connection established`, relay bytes both ways (select loop) until either side closes. Absolute-URI HTTP requests (`GET http://host/...`) → same check, then forward in origin-form with `Connection: close`, dropping `Proxy-*` headers, relay the response raw. Request with `Transfer-Encoding` → `501`. Bytes relayed towards the browser are counted across all connections; past `byte_budget` set `over_budget` and close connections. Server is a daemon `ThreadingTCPServer` bound to `127.0.0.1:0`.

- [ ] **Step 1: Write failing tests** (use `httpserver`, `loopback_ok`):
  - `test_http_request_relayed`: `requests.get(httpserver.url_for("/x"), proxies={"http": proxy.url})` through `FilteringProxy(loopback_ok, 10**6)` → 200 and body.
  - `test_connect_relays_bytes`: raw socket `CONNECT 127.0.0.1:<port> HTTP/1.1` → `200`; then send a plain `GET /x` over the tunnel → the server's body comes back.
  - `test_connect_to_private_address_refused`: with `safety.is_public_address`, CONNECT to the test server → `403`, `proxy.refused == ["127.0.0.1"]`, and the server got no request.
  - `test_http_to_private_address_refused`: same for an absolute-URI GET → `403`.
  - `test_byte_budget`: budget 100 bytes, body 10 000 bytes → client gets < 10 000 bytes and `proxy.over_budget`.
- [ ] **Step 2:** `venv/bin/python -m pytest tests/scraping/test_browser_proxy.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement `browser_proxy.py`.
- [ ] **Step 4:** Tests pass; full suite passes.
- [ ] **Step 5:** Commit `feat(scraping): localhost filtering proxy for the browser`.

### Task 3: Render a page in Chromium

**Files:**
- Create: `app/modules/scraping/discover_browser.py`
- Modify: `app/modules/scraping/fetcher.py` (`Fetcher.policy` attribute), `tests/conftest.py` (session fixture `browser_ok` that skips when Playwright/Chromium can't launch, and enables `scrape_browser_enabled` via monkeypatch)
- Test: `tests/scraping/test_discover_browser.py` (all marked `browser`)

**Interfaces:**
- Consumes: `FilteringProxy` (Task 2), `Deadline`, `Fetched`, `ScrapeBlocked`, `message`, settings from Task 1.
- Produces:
  - `@dataclass Feed(url: str, method: str, payload: object)`
  - `@dataclass Rendered(final_url: str, html: str, feeds: list[Feed], downloads: list[Fetched], refused: list[str], over_budget: bool)`
  - `BROWSER_LOCK = threading.Lock()`
  - `render(url: str, *, policy: AddressPolicy, deadline: Deadline, say: Callable[[str], None] = noop, click_exports: bool = True) -> Rendered`
  - Raises `ScrapeBlocked("browser_unavailable", ...)` when `playwright` can't be imported or Chromium can't launch; `ScrapeBlocked("private_address", ...)` when the proxy refused the page's own host; `ScrapeBlocked("browser_failed", ...)` for any other navigation error or when the lock isn't free within the deadline; `ScrapeBlocked("timeout", ...)` when the deadline is already spent.
- Behaviour:
  - Budget `min(settings.scrape_browser_seconds, deadline.remaining())`; every Playwright timeout is taken from what is left of it.
  - `chromium.launch(headless=True, chromium_sandbox=settings.scrape_browser_sandbox, proxy={"server": proxy.url}, args=["--proxy-bypass-list=<-loopback>", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])`. `<-loopback>` makes Chromium send localhost through the proxy too, where the guard refuses it.
  - `new_context(user_agent=settings.scrape_user_agent, accept_downloads=True, service_workers="block")`.
  - `goto(wait_until="load")`; then `wait_for_load_state("networkidle", timeout=min(10 s, left))` with timeouts swallowed; scroll to the bottom 3 times, 1 s apart; `html = page.content()`.
  - Feed capture: keep `Response`s whose `request.resource_type` is `xhr`/`fetch`, status 200, content type contains `json` (or URL path ends `.json`), host/path not in the noise list (`google-analytics`, `googletagmanager`, `doubleclick`, `facebook`, `hotjar`, `clarity.ms`, `sentry`, `newrelic`, `nr-data`, `segment`, `mixpanel`, `amplitude`, `cloudflareinsights`, `recaptcha`; paths containing `/collect`, `/track`, `/beacon`, `/analytics`), at most `scrape_feed_max`; read bodies after the page settles; skip bodies over `scrape_feed_mb` or that aren't JSON.
  - Export clicks (when `click_exports`): visible `button, a, [role=button]` whose text matches `\b(export|download|excel|csv)\b` (case-insensitive), at most 3, skipping `<a href>` to a `files.FILE_EXTS` file (the plain path downloads those). Each click inside `page.expect_download(timeout=5000)`; a download up to `scrape_file_mb` becomes `Fetched(url, url, 200, None, bytes, suggested_filename)`. If `page.url` changed after a click, stop clicking.
  - Proxy byte budget `scrape_browser_mb`.

- [ ] **Step 1: Write failing tests** (serve pages with `httpserver`; render with `policy=loopback_ok`):
  - `test_render_builds_table_from_script`: page `<div id="root"></div><script>` that `fetch('/api/rows?page=1')` and writes a `<table>` → `"<table" in rendered.html`, one `Feed` whose payload has the rows.
  - `test_render_page_that_never_goes_quiet`: script `setInterval(() => fetch('/ping'), 300)`; `Deadline(20)` → `render` returns within 20 s with the HTML.
  - `test_feed_capture_skips_noise_and_bad_json`: page fetches `/api/good` (JSON records), `/collect?x=1` (JSON), `/api/bad` (`application/json`, body `not json`) → exactly one feed (`/api/good`).
  - `test_browser_cannot_reach_loopback_without_proxy_approval`: `render(url, policy=safety.is_public_address, ...)` → `ScrapeBlocked` with code `private_address`.
  - `test_export_button_download`: `<button onclick="location='/export.csv'">Export CSV</button>`, `/export.csv` answers with `Content-Disposition: attachment; filename=rows.csv` → one download named `rows.csv` with the CSV bytes.
  - `test_export_click_that_navigates_stops_clicking`: first button navigates to `/other` (HTML), second would download → no downloads, no error.
  - `test_browser_unavailable` (not marked `browser`): monkeypatch the import so it raises `ImportError` → `ScrapeBlocked` code `browser_unavailable`.
- [ ] **Step 2:** `venv/bin/python -m pytest tests/scraping/test_discover_browser.py -q` → FAIL.
- [ ] **Step 3:** Implement `discover_browser.py`; add `self.policy = policy` to `Fetcher.__init__`.
- [ ] **Step 4:** Tests pass; full suite passes.
- [ ] **Step 5:** Commit `feat(scraping): open JavaScript pages in a guarded headless Chromium`.

### Task 4: Feed tables and feed paging

**Files:**
- Create: `app/modules/scraping/feeds.py`
- Test: `tests/scraping/test_feeds.py`

**Interfaces:**
- Consumes: `Feed` (Task 3), `files.record_arrays`, `Fetcher.get`, `TableItem`.
- Produces:
  - `paging_param(url: str) -> tuple[str, str] | None`: the query key (original spelling) and `"page"` or `"offset"`. Page keys (lowercased): `page`, `pageno`, `page_no`, `pagenumber`, `pagenum`, `pageindex`, `page_index`. Offset keys: `offset`, `start`, `skip`, `from`.
  - `with_param(url: str, key: str, value: int) -> str`
  - `@dataclass FeedResult(tables: list[TableItem], pages: int, blocked: list[ScrapeBlocked])`
  - `collect(feed: Feed, fetcher: Fetcher | None, *, max_pages: int, max_bytes: int) -> FeedResult`
- Behaviour: arrays = `dict(record_arrays(feed.payload))`; none → empty result. If `fetcher` is given, `feed.method == "GET"` and a paging param exists: request next pages through `fetcher.get(url, max_bytes=max_bytes)` (robots-checked), step +1 for page keys, + first-page record count for offset keys; for each path stack the records found at the same path; stop when no path has records, the page's bytes equal the previous page's, `max_pages` pages in total, a non-200 status, invalid JSON, or `ScrapeBlocked` (recorded in `blocked`; `timeout` also stops). One `TableItem("feed", title, locator, pd.json_normalize(records))` per path; title `"Data feed " + <url path>` plus `" " + path` when path isn't `"$"`; locator `{"source": "feed", "feed_url": feed.url, "path": path, "pages": pages}`.

- [ ] **Step 1: Write failing tests:**
  - `test_paging_param`: `?pageNo=1&size=10` → `("pageNo", "page")`; `?start=0` → `("start", "offset")`; `?q=x` → `None`.
  - `test_collect_without_paging`: payload `{"data": [{a,b}×3]}`, no paging param → one table, 3 rows, title `"Data feed /api/rows $.data"`.
  - `test_collect_follows_pages`: httpserver serves `/api/rows?page=2` and `3` with 10 records each, `4` with `{"data": []}`; first page 10 records → 30 rows, `pages == 3`.
  - `test_collect_offset_step`: `?offset=0` first page 5 records; server answers `offset=5` (5 records) and `offset=10` (empty) → 10 rows.
  - `test_collect_stops_on_repeat`: server ignores `page` and always returns page 1 → 10 rows, `pages == 1`.
  - `test_collect_respects_robots`: robots `Disallow: /api/` → first-page rows kept, `blocked[0].code == "robots_disallowed"`.
  - `test_collect_page_limit`: endless pages, `max_pages=4` → 40 rows.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `feeds.py`.
- [ ] **Step 4:** Tests pass; full suite passes.
- [ ] **Step 5:** Commit `feat(scraping): turn captured feeds into tables and follow their pages`.

### Task 5: Use the browser in the Search job

**Files:**
- Modify: `app/modules/scraping/scan.py`
- Test: `tests/scraping/test_scan.py`, `tests/scraping/test_scan_browser.py` (marked `browser`)

**Interfaces:**
- Consumes: `discover_browser.render`, `BROWSER_LOCK` (inside `render`), `feeds.collect`, `discover_http.parse_page`, `_Run.stage_table/add_file/download_files/add_text`.
- Produces: `summary["rendered_in_browser"]: bool`.
- Behaviour, in `_Run.execute` after the CAPTCHA/login check and before staging anything:
  - `mode = scan.options.get("render", "auto")`; `want = mode == "always" or (mode == "auto" and (parsed.js_shell or not parsed.tables))`; never when `settings.scrape_browser_enabled` is false and mode is `auto` (then no extra entry); with `always` and the setting off → `browser_unavailable` entry.
  - Rendering says "Opening the page in a browser". On `ScrapeBlocked` → `block_exc`, keep the plain parse (and its `needs_browser` entry if it was a shell).
  - On success: parse `rendered.html` with `parse_page(rendered.html, rendered.final_url)`; its `html_table` items become `rendered_table`; text = rendered text or plain text; file links = plain links then new rendered links (order kept, no repeats); `form_gated` from the rendered parse; no `needs_browser` entry. Each refused host other than the page's own → `private_address` entry; `over_budget` → `too_large` entry with `n=scrape_browser_mb`.
  - Feeds: "Found N data feed(s)"; for each, `feeds.collect(feed, self.fetcher, max_pages=scrape_feed_pages, max_bytes=scrape_feed_mb * MB)`, stage its tables, `block_exc` its blocked entries.
  - Downloads: each counts against the scan's byte total and goes through `add_file`.
  - Then linked files exactly as today.

- [ ] **Step 1: Write failing tests:**
  - `test_scan.py::test_js_shell_reports_needs_browser` keeps passing (browser disabled in conftest) with the new message.
  - `test_scan.py::test_browser_unavailable_keeps_plain_results`: settings enabled, `render="always"`, monkeypatch `discover_browser.render` to raise `ScrapeBlocked("browser_unavailable", ...)` on the standard site → scan `done`, 3 tables as in phase 1, codes `["browser_unavailable"]`, `summary["rendered_in_browser"] is False`.
  - `test_scan.py::test_render_never_skips_browser`: `render="never"` with a spy on `render` → not called.
  - `test_scan_browser.py::test_js_page_end_to_end`: React-style page (`<div id="root">`, script fetches `/api/districts?page=1` with 38 districts split over pages 1–2, page 3 empty, and draws them as a table) + `/robots.txt` 404 → `rendered_in_browser`, one `rendered_table` with 19 rows, one `feed` with 38 rows, `needs_browser` absent; the feed table's `joinable` is not None.
  - `test_scan_browser.py::test_export_button_file_read`: page with Export CSV button → a `file` finding and a `file_table` with the CSV's rows.
  - `test_scan_browser.py::test_import_feed_creates_web_feed_dataset`: save the feed finding via `importer.claim_for_import` + `run_import` → raw dataset connector `web_feed`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement in `scan.py` (a `render_page()` helper on `_Run` that returns `Rendered | None`).
- [ ] **Step 4:** `venv/bin/python -m pytest -q` → all pass.
- [ ] **Step 5:** Commit `feat(scraping): Search opens JavaScript pages in the browser`.

### Task 6: Live check and docs

**Files:**
- Modify: `docs/superpowers/specs/2026-11-03-website-tab-design.md` (status line + "Phase 2 as built" note), `API_TESTING.md` §11 (`render`, `rendered_in_browser`, new kinds/codes), `CLAUDE.md` (Website tab section: browser, proxy, settings, server install `python -m playwright install --with-deps chromium`, `PLATFORM_SCRAPE_BROWSER_SANDBOX=false` only where Chromium's sandbox can't run, e.g. as root in Docker)

- [ ] **Step 1:** Live smoke (not a test): run `run_scan` on `https://ndap.niti.gov.in/` and on `https://en.wikipedia.org/wiki/List_of_districts_of_Bihar` with `render="auto"`; record tables/feeds found and time taken; Wikipedia must not open the browser.
- [ ] **Step 2:** Update the docs above.
- [ ] **Step 3:** Full suite passes.
- [ ] **Step 4:** Commit `docs: website tab phase 2 (browser)`.
