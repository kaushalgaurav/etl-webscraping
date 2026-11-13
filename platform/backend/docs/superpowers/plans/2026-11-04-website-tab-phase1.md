# Website Tab (Phase 1: plain pages and files) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin gives any web page address. The backend reads that page and the files it links to, and shows every table, file and text it found. The admin's ticked tables are then saved the way an upload is saved today: one raw dataset plus one cleaned copy. Everything not ticked is deleted.

**Architecture:**
- A new module `app/modules/scraping/` is added, following the same router/service layout as the other modules.
- **Search** is a background job (`scrape_scan`). It fetches the page over a guarded HTTP session that refuses private addresses and obeys robots.txt. It pulls out tables, embedded JSON, linked files and the main text. Each found table is held as a Parquet file in a staging folder, with a row in `scrape_findings`.
- **Save** is a second job (`scrape_import`). It turns each ticked table into a `ds_<id>` raw dataset and a cleaned copy, using the existing `store_result` / `store_clean_copy`. After that it deletes the staging folder and the findings that weren't ticked.
- No browser is used in phase 1.

**Tech Stack:** FastAPI, SQLAlchemy 2, pandas, requests/urllib3 2.x, BeautifulSoup/lxml, pdfplumber, python-docx, trafilatura, pyarrow, pytest + pytest-httpserver. Python 3.10 (`platform/backend/venv`).

**Spec:** `docs/superpowers/specs/2026-11-03-website-tab-design.md`. Only phase 1 is in scope (spec §11.1): §4.1, §4.2, §4.4–4.8, §5, §6, §7 and §9 minus everything browser-related.

**Decisions made after the spec (they override it):**
- **Storage of ticked tables.** They are stored like uploads (raw `ds_` + clean `ds_`). The database redesign is parked.
- **Dashboard (spec §12).** Option **B**: imported tables do not reach the dashboard KPIs in phase 1.
- **Staging folder.** It is `data/scrape_staging/<scan_id>/`, outside `data/raw/`, so `cleanup.purge_all()` never touches it and needs no change.
- **One Save = one `POST /scrape/scans/{id}/import`** carrying every ticked table, run as one job.
  - The spec's "one import per finding" (§8) would let the first job delete the staging folder while the others still need it.
  - After a successful import the scan is closed, and any later import returns 409.
- **No `render` field in phase 1.** It is added in phase 2 as an optional field, which is backwards compatible.
- **Expired-scan cleanup** runs at startup and at the start of every new scan. There is no hourly timer thread.
- **A page that only works with JavaScript** is reported in "Couldn't read" with the code `needs_browser`.

## Global Constraints

- **Never apply migrations.** Do not run `alembic upgrade`, `run_migrations()`, or any migrate command against any database. Only generate the migration file, then report its path and the apply command `venv/bin/alembic upgrade head`. `Base.metadata.create_all` on the tests' own throwaway SQLite file is allowed.
- **Python and commands.** Python 3.10. Run everything from `platform/backend/` with `venv/bin/python`. Tests run with `venv/bin/python -m pytest`.
- **Branch.** Work on the branch `feature/website-tab`, never on `main`.
- **User-Agent.** Every outgoing request sends `User-Agent: NimbliDataBot/1.0`. The robots.txt agent token is `NimbliDataBot`.
- **Settings.** New settings use the `PLATFORM_` prefix and live in `app/core/config.py`. Defaults (spec §4.8):

  | Setting | Default |
  |---|---|
  | `scrape_scan_seconds` | 180 |
  | `scrape_host_interval_seconds` | 1.0 |
  | `scrape_max_files` | 25 |
  | `scrape_file_mb` | 30 |
  | `scrape_total_mb` | 200 |
  | `scrape_zip_max_files` | 50 |
  | `scrape_zip_max_mb` | 300 |
  | `scrape_max_rows` | 1_000_000 |
  | `scrape_preview_rows` | 20 |
  | `scrape_preview_cols` | 30 |
  | `scrape_snippet_chars` | 1000 |
  | `scrape_hold_hours` | 24 |
  | `scrape_dir` | `DATA_DIR / "scrape_staging"` |

  The per-request timeout is the existing `request_timeout_seconds` (30), capped by the time left in the scan.
- **No getting around blocks.** No CAPTCHA solving, no disguised User-Agent, no proxy rotation, and robots.txt is always obeyed.
- **Address guard.** Every outgoing connection (first page, every redirect hop, robots.txt, every file) goes through the address guard from Task 2. Nothing in `app/modules/scraping/` may call `requests.get` directly.
- **JSON safety.** Cells in JSON responses go through `app.helpers.dataframe.json_safe_records` (NaN → null). CSV downloads are UTF-8 with a BOM.
- **Saved datasets.** Connector names are `web_table` (HTML tables and embedded page data) and `web_file` (tables from downloaded files). The survey is stamped on both the raw and the clean dataset.
- **Wording shown to admins.** The "Couldn't read" messages are exactly these:

  | Code | Message |
  |---|---|
  | `robots_disallowed` | The site asks automated tools not to read this page. |
  | `bot_protection` | The site blocked automated access. |
  | `captcha` | Data is behind a CAPTCHA. |
  | `login_required` | The page needs a login. |
  | `form_gated` | Data appears only after choosing options in a form. |
  | `needs_browser` | This page builds its content with JavaScript; reading it needs browser mode, which is not available yet. |
  | `private_address` | This address points inside a private network and can't be read. |
  | `dedicated_connector` | This source has a dedicated connector; use the URL tab. |
  | `too_large` | File is larger than the {n} MB limit. |
  | `timeout` | Search stopped after {n} seconds. |
  | `too_many_files` | Only the first {n} linked files were read. |
  | `unsupported` | This kind of address or file can't be read. |
  | `http_error` | The site answered with status {code}. |
  | `fetch_failed` | Couldn't connect to the site. |

## Review Focus

1. **Private addresses in disguise.** A link that points, directly or through a redirect or DNS, at a private, loopback or link-local address must end up in "Couldn't read" as `private_address` without any connection being made. This includes IPv4 written inside IPv6 (`::ffff:127.0.0.1`), `0.0.0.0`, and the cloud metadata address `169.254.169.254`. (Task 2)
2. **CSV encodings.** A CSV saved in Windows-1252 or Latin-1, or one with a UTF-8 BOM, must still read. Hindi text in UTF-8 must come through intact. (Task 5)
3. **Mixed columns.** A column mixing numbers with text such as "NA", "-" or "1,234" must not crash Parquet staging, and its values must survive into the preview. (Task 8)
4. **Restarts.** If the server restarts during a Search or a Save, the scan must not stay "running" or "importing" forever. Startup sets running → failed and importing → done. (Task 8)
5. **Double Save.** Clicking Save twice must produce exactly one import job. The second call gets 409 and no duplicate datasets are made. (Task 9)

---

## File structure

| File | Responsibility |
|---|---|
| `app/models/scrape.py` (new) | ORM `ScrapeScan`, `ScrapeFinding` |
| `app/modules/scraping/safety.py` (new) | URL checks, the private-address policy, guarded `requests.Session`, `ScrapeBlocked`, known hosts |
| `app/modules/scraping/fetcher.py` (new) | `Fetcher`: redirects, robots.txt, per-host pacing, deadline, size caps |
| `app/modules/scraping/blocked.py` (new) + `suggestions.json` | Classify blockers, messages, host → official-alternative lookup |
| `app/modules/scraping/items.py` (new) | Plain dataclasses passed between stages |
| `app/modules/scraping/files.py` (new) | Read CSV/TSV/Excel/JSON/PDF/DOCX/ZIP bytes into tables + text |
| `app/modules/scraping/discover_http.py` (new) | Parse one HTML page: tables, embedded JSON, file links, main text, JS-shell / form flags |
| `app/modules/scraping/scoring.py` (new) | Signals per table (layout, numeric share, joinable, personal data), suggestion, duplicates |
| `app/modules/scraping/store.py` (new) | Staging folder: Parquet tables, files, text; safe path resolution; delete |
| `app/modules/scraping/scan.py` (new) | `run_scan` orchestration |
| `app/modules/scraping/importer.py` (new) | Claim for import, `run_import` |
| `app/modules/scraping/service.py` (new) | Create/list/manifest/rows/download/delete, expiry purge, restart reaping, error types |
| `app/modules/scraping/schemas.py`, `router.py` (new) | HTTP contract (spec §7) |
| `app/core/config.py` | New settings |
| `app/models/__init__.py` | Export new models |
| `app/constants/district_crosswalk.py` | `find_district(district)` across all states |
| `app/modules/jobs/service.py`, `jobs/lineage.py` | Two job types; lineage follows `result["items"]` |
| `app/modules/reports/naming.py` | `web_table` → web, `web_file` → file |
| `app/routes/__init__.py`, `app/db/init_db.py` | Register router; reap + purge at startup |
| `alembic/env.py` | Add `gemini_` to `DATA_TABLE_PREFIXES` |
| `alembic/versions/<rev>_scrape_scans_and_findings.py` | Generated, not applied |
| `requirements.txt`, `requirements-dev.txt` (new), `pytest.ini` (new), `tests/` (new) | Dependencies and tests |

---

### Task 1: Foundations (branch, dependencies, settings, models, test harness, migration file)

**Files:**
- Modify: `requirements.txt`, `app/core/config.py`, `app/models/__init__.py`, `alembic/env.py:28`
- Create:
  - `requirements-dev.txt`
  - `pytest.ini`
  - `tests/__init__.py`
  - `tests/conftest.py`
  - `app/models/scrape.py`
  - `app/modules/scraping/__init__.py`
  - `tests/scraping/__init__.py`
  - `tests/scraping/test_models.py`
- Generate: `alembic/versions/<rev>_scrape_scans_and_findings.py`

**Interfaces:**
- Produces:
  - `ScrapeScan` with fields:
    - `id: str` (`new_id`)
    - `url: str`
    - `final_url: str|None`
    - `status: str` (`running|done|failed|importing|imported`)
    - `job_id: str|None`
    - `options: dict` (default `{}`)
    - `summary: dict|None`
    - `blocked: list` (default `[]`)
    - `error: str|None`
    - `created_at`, `finished_at`, `expires_at` (tz-aware, `expires_at` nullable)
  - `ScrapeFinding` with fields:
    - `id: str`
    - `scan_id: str` (FK `scrape_scans.id`, indexed)
    - `position: int`
    - `kind: str` (`html_table|embedded|file_table|pdf_table|docx_table|text|file`)
    - `title: str`
    - `locator: dict`
    - `parent_id: str|None`
    - `rows`, `columns: int|None`
    - `column_names`, `preview`, `signals`, `details: dict|list|None` (JSON)
    - `suggested: str|None`
    - `storage_path: str|None`
    - `content_hash: str|None`
    - `imported_dataset_id: str|None`
  - Fixtures in `tests/conftest.py`:
    - `db` (a Session on the throwaway SQLite)
    - `loopback_ok` (an address policy that also allows loopback, for local test servers)
    - `httpserver` (from pytest-httpserver)

- [ ] **Step 1: Create the branch.** Run `git checkout -b feature/website-tab`. Expected: "Switched to a new branch".

- [ ] **Step 2: Add the dependencies.**
  - Append to `requirements.txt`: `pdfplumber==0.11.4`, `python-docx==1.1.2`, `trafilatura==1.12.2`, `pyarrow==17.0.0`, `xlrd==2.0.1`.
  - Create `requirements-dev.txt` with `-r requirements.txt`, `pytest==8.3.3`, `pytest-httpserver==1.1.0`.
  - Run `venv/bin/pip install -r requirements-dev.txt`. Expected: installs without resolver errors. If a pin conflicts with the existing `lxml==5.3.0`, pick the nearest compatible version of the new package and note it in the commit message.

- [ ] **Step 3: Add the settings to `Settings` in `app/core/config.py`.** Use the names and defaults from Global Constraints, plus `scrape_user_agent: str = "NimbliDataBot/1.0"`. Type `scrape_dir` as `Path`.

- [ ] **Step 4: Write the test harness.**
  - `pytest.ini`:

    ```ini
    [pytest]
    testpaths = tests
    ```

  - `tests/conftest.py` must set its environment variables **before** any `app` import:
    - `PLATFORM_DATABASE_URL=sqlite:///<tmpdir>/test.db`
    - `PLATFORM_SCRAPE_DIR=<tmpdir>/staging`
    - `PLATFORM_SCRAPE_HOST_INTERVAL_SECONDS=0`

    It then imports `app.models` and `app.db.session.engine` and runs `Base.metadata.create_all(engine)`. Fixtures:
    - `db`: yields a `SessionLocal()` and closes it afterwards.
    - `loopback_ok`: `lambda ip: ip.is_loopback or safety.is_public_address(ip)`. Import `safety` lazily inside the fixture so Task 1 runs before Task 2 exists.

- [ ] **Step 5: Write the failing test** in `tests/scraping/test_models.py`:

```python
from app.models import ScrapeFinding, ScrapeScan

def test_scan_with_finding_round_trips(db):
    scan = ScrapeScan(url="https://example.gov.in/x", status="running")
    db.add(scan); db.flush()
    db.add(ScrapeFinding(scan_id=scan.id, position=0, kind="html_table", title="T",
                         locator={"source": "page", "table_index": 0},
                         signals={"numeric_share": 0.5}))
    db.commit()
    got = db.query(ScrapeFinding).filter_by(scan_id=scan.id).one()
    assert got.locator["table_index"] == 0 and len(got.id) == 12
    assert scan.blocked == [] and scan.options == {}
```

- [ ] **Step 6: Run it.** `venv/bin/python -m pytest tests/scraping/test_models.py -v`. Expected: FAIL with an ImportError on `ScrapeFinding`.

- [ ] **Step 7: Implement the models.**
  - Write `app/models/scrape.py` with the fields listed above, following the style of `app/models/job.py`.
  - Export both models from `app/models/__init__.py`.
  - Create an empty `app/modules/scraping/__init__.py`.

- [ ] **Step 8: Run it again.** Same command. Expected: PASS.

- [ ] **Step 9: Generate the migration (do not apply it).**
  - Change `DATA_TABLE_PREFIXES` in `alembic/env.py` to `("census_", "hmis_", "dashboard_", "ds_", "gemini_")`. The `gemini_*` tables are created by raw SQL in `ai_data/store.py`, so autogenerate would otherwise drop them.
  - Run `venv/bin/alembic revision --autogenerate -m "scrape scans and findings"`. Expected: a new file in `alembic/versions/`.
  - Open the file. It must contain only `create_table("scrape_scans")`, `create_table("scrape_findings")`, their indexes, and the matching drops in `downgrade()`. Delete any other operation and list what you deleted in your report.
  - If the command fails (the database is unreachable, or "Target database is not up to date"), stop this step and report the exact error. Never run `upgrade` to fix it.

- [ ] **Step 10: Commit.**

```bash
git add requirements.txt requirements-dev.txt pytest.ini tests app/core/config.py app/models alembic app/modules/scraping/__init__.py docs/superpowers/specs/2026-11-03-website-tab-design.md docs/superpowers/plans/2026-11-04-website-tab-phase1.md
git commit -m "feat(scraping): models, settings, test harness, scrape tables migration (not applied)"
```

---

### Task 2: Address guard (`safety.py`)

**Files:**
- Create: `app/modules/scraping/safety.py`, `tests/scraping/test_safety.py`

**Interfaces:**
- Produces:
  - `class ScrapeBlocked(Exception)`, built as `ScrapeBlocked(code: str, message: str, what: str)`, with attributes `.code`, `.message`, `.what`.
  - `AddressPolicy = Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool]`
  - `is_public_address(ip) -> bool`
  - `check_url(url: str) -> None`. Raises `ScrapeBlocked("unsupported", ...)` when the scheme isn't http/https or the host is missing.
  - `dedicated_connector(url: str) -> bool`. True for hosts ending in `censusindia.gov.in`, `hmis.mohfw.gov.in`, `data.gov.in`.
  - `make_session(policy: AddressPolicy = is_public_address) -> requests.Session`

- [ ] **Step 1: Write the failing tests:**

```python
import ipaddress, socket, pytest, requests
from app.modules.scraping import safety

@pytest.mark.parametrize("ip", ["10.1.2.3", "172.16.0.1", "192.168.1.1", "127.0.0.1",
    "169.254.169.254", "0.0.0.0", "100.64.0.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1"])
def test_private_addresses_refused(ip):
    assert safety.is_public_address(ipaddress.ip_address(ip)) is False

def test_public_address_allowed():
    assert safety.is_public_address(ipaddress.ip_address("8.8.8.8")) is True

@pytest.mark.parametrize("url", ["ftp://x.gov.in/a", "file:///etc/passwd", "http:///nohost"])
def test_check_url_rejects(url):
    with pytest.raises(safety.ScrapeBlocked) as e: safety.check_url(url)
    assert e.value.code == "unsupported"

def test_dedicated_hosts():
    assert safety.dedicated_connector("https://hmis.mohfw.gov.in/#!/standard")
    assert safety.dedicated_connector("https://api.data.gov.in/resource/x")
    assert not safety.dedicated_connector("https://rbi.org.in/")

def test_session_refuses_loopback_server(httpserver):
    httpserver.expect_request("/").respond_with_data("hi")
    with pytest.raises(safety.ScrapeBlocked) as e:
        safety.make_session().get(httpserver.url_for("/"), timeout=5)
    assert e.value.code == "private_address"
    assert len(httpserver.log) == 0          # no connection was made

def test_hostname_resolving_to_private_is_refused(monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k:
        real("127.0.0.1", *a, **k) if h == "innocent.example" else real(h, *a, **k))
    with pytest.raises(safety.ScrapeBlocked):
        safety.make_session().get("http://innocent.example/", timeout=5)

def test_permissive_policy_allows_local(httpserver, loopback_ok):
    httpserver.expect_request("/").respond_with_data("hi")
    assert safety.make_session(loopback_ok).get(httpserver.url_for("/"), timeout=5).text == "hi"
```

- [ ] **Step 2: Run them.** `venv/bin/python -m pytest tests/scraping/test_safety.py -v`. Expected: FAIL (module not found).

- [ ] **Step 3: Implement `safety.py`.**
  - **`is_public_address`.** Unwrap IPv4 written inside IPv6 (`ip.ipv4_mapped`). Allow only `ip.is_global and not ip.is_multicast`.
  - **Pinning the address.** Each connection resolves its host once, checks every resolved address against the policy, and connects to a checked address. TLS keeps the original hostname for SNI and certificate checks, which urllib3 does by default because `server_hostname` is taken from `self.host`. Sketch:

```python
def _resolve_checked(host, port, policy):
    addrs = {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    bad = [a for a in addrs if not policy(ipaddress.ip_address(a.split("%")[0]))]
    if not addrs or bad:
        raise ScrapeBlocked("private_address", MESSAGE, host)
    return sorted(addrs)[0]

def _connection_classes(policy):
    class _Pinned:
        def _new_conn(self):
            ip = _resolve_checked(self._dns_host, self.port, policy)
            return urllib3.util.connection.create_connection(
                (ip, self.port), self.timeout,
                source_address=self.source_address, socket_options=self.socket_options)
    class HTTP(_Pinned, urllib3.connection.HTTPConnection): pass
    class HTTPS(_Pinned, urllib3.connection.HTTPSConnection): pass
    return HTTP, HTTPS
```

  - **`make_session`.** Mount an `HTTPAdapter` subclass on both schemes. Its `init_poolmanager` sets `self.poolmanager.pool_classes_by_scheme` to `HTTPConnectionPool` / `HTTPSConnectionPool` subclasses whose `ConnectionCls` are the classes above.
  - Set `session.trust_env = False`. Otherwise a proxy from an environment variable would become the checked host.
  - Set the User-Agent header from settings.
  - `ScrapeBlocked` must reach the caller unwrapped. It subclasses `Exception`, not `OSError`, so urllib3 and requests don't convert it. The loopback test proves this.

- [ ] **Step 4: Run the tests.** Same command. Expected: all PASS.

- [ ] **Step 5: Commit.** `git add app/modules/scraping/safety.py tests/scraping/test_safety.py && git commit -m "feat(scraping): address guard with pinned DNS"`

---

### Task 3: `Fetcher` (redirects, robots.txt, pacing, deadline, size caps)

**Files:**
- Create: `app/modules/scraping/fetcher.py`, `tests/scraping/test_fetcher.py`

**Interfaces:**
- Consumes: `safety.make_session`, `safety.check_url`, `safety.ScrapeBlocked`, `safety.is_public_address`
- Produces:
  - `class Deadline(seconds: float, clock=time.monotonic)`, with `.remaining() -> float` and `.expired() -> bool`
  - `class HostThrottle(interval: float, clock=time.monotonic, sleep=time.sleep)`, with `.wait(host: str) -> None`. Thread-safe. Module instance `THROTTLE`.
  - `@dataclass Fetched`, with fields `url: str`, `final_url: str`, `status: int`, `content_type: str | None`, `content: bytes`, `filename: str`
  - `class Fetcher(policy=is_public_address, deadline: Deadline | None = None, throttle: HostThrottle = THROTTLE)`. A `None` deadline becomes `Deadline(settings.scrape_scan_seconds)`, and the deadline is exposed as `.deadline`. Methods:
    - `.get(url: str, *, max_bytes: int, check_robots: bool = True) -> Fetched`
    - `.allowed_by_robots(url: str) -> bool`

- [ ] **Step 1: Write the failing tests.** Every test that uses `httpserver` builds `Fetcher(policy=loopback_ok)`. Register `/robots.txt` with a 404 response unless the test sets its own.

```python
def test_follows_redirect_and_reports_final_url(httpserver, loopback_ok): ...
    # "/a" 302 -> "/b" (200 "ok"); f.get(url_a, max_bytes=1000)
    # assert r.final_url.endswith("/b") and r.content == b"ok" and r.status == 200

def test_redirect_to_private_address_is_blocked(httpserver):
    # Fetcher(policy=lambda ip: ip.is_loopback and str(ip) == "127.0.0.1" or safety.is_public_address(ip))
    # "/a" 302 -> "http://[::1]:<port>/b"  -> ScrapeBlocked code "private_address"

def test_robots_disallow_blocks(httpserver, loopback_ok):
    # robots.txt "User-agent: *\nDisallow: /private" -> get("/private/x") raises code "robots_disallowed"
    # and "/public" succeeds

def test_robots_specific_agent_rule(httpserver, loopback_ok):
    # "User-agent: NimbliDataBot\nDisallow: /" -> blocked even though "*" allows

def test_missing_robots_allows(httpserver, loopback_ok): ...   # robots 404 -> allowed

def test_user_agent_sent(httpserver, loopback_ok):
    # httpserver.expect_request("/", headers={"User-Agent": "NimbliDataBot/1.0"})

def test_content_length_over_cap_is_too_large(httpserver, loopback_ok):
    # 2000-byte body, max_bytes=1000 -> code "too_large"

def test_streamed_body_over_cap_is_too_large(httpserver, loopback_ok):
    # chunked response without Content-Length, 5000 bytes, max_bytes=1000 -> "too_large"

def test_expired_deadline_is_timeout(loopback_ok):
    # Deadline(0) -> get(any) raises code "timeout" without any request

def test_throttle_spaces_same_host():
    # fake clock + recorded sleeps; HostThrottle(1.0).wait("a"); wait("a") -> slept ~1.0; wait("b") -> no sleep

def test_filename_from_content_disposition(httpserver, loopback_ok):
    # header 'attachment; filename="report 2023.xlsx"' -> r.filename == "report 2023.xlsx"
    # without header, "/files/data.csv?x=1" -> "data.csv"; "/" -> "download"

def test_non_2xx_is_returned_not_raised(httpserver, loopback_ok):
    # 403 body "Access Denied" -> r.status == 403 (blocked.py classifies it later)
```

- [ ] **Step 2: Run them.** `venv/bin/python -m pytest tests/scraping/test_fetcher.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `fetcher.py`.**
  - **`get` loop.** Call `check_url`. Stop if the deadline has expired. Call `throttle.wait(host)`. Check robots (for every hop when `check_robots` is on). Send the request with `allow_redirects=False`, `stream=True` and `timeout=min(settings.request_timeout_seconds, deadline.remaining())`. Follow `Location` (via `urljoin`) for at most 5 hops; more than that raises `fetch_failed`.
  - **Size cap.** Check `Content-Length` against `max_bytes` first, then count bytes while reading `iter_content(65536)`.
  - **Errors.** `requests.Timeout` → `timeout`. Any other `requests.RequestException` → `fetch_failed`.
  - **robots.txt.**
    - Fetch it once per `scheme://host:port`, through this same fetcher, with `check_robots=False` and `max_bytes=512_000`, and cache the result.
    - Parse it with `urllib.robotparser.RobotFileParser.parse(lines)`, and check `can_fetch("NimbliDataBot", url)`.
    - Any non-200 answer, or a failed fetch, counts as "allowed". **(Plan's call:** the admin asked for this one page and the pace is 1 request/second.)

- [ ] **Step 4: Run the tests.** Same command. Expected: PASS.

- [ ] **Step 5: Commit.** `git add app/modules/scraping/fetcher.py tests/scraping/test_fetcher.py && git commit -m "feat(scraping): fetcher with robots, pacing, limits"`

---

### Task 4: Recognising blocks (`blocked.py`)

**Files:**
- Create:
  - `app/modules/scraping/blocked.py`
  - `app/modules/scraping/suggestions.json`
  - `tests/scraping/test_blocked.py`

**Interfaces:**
- Consumes: `Fetched` (Task 3)
- Produces:
  - `MESSAGES: dict[str, str]`, the exact texts in Global Constraints. Templates use `{n}` / `{code}`.
  - `entry(code: str, what: str, **fmt) -> dict`. Returns `{"what", "code", "message", "suggestion"}`, where `suggestion = suggestion_for(what)`.
  - `suggestion_for(url: str) -> str | None`, matching the host by suffix against `suggestions.json`.
  - `detect(fetched: Fetched, html: str | None) -> str | None`. Returns one of `bot_protection`, `captcha`, `login_required`, or `None`.
  - `is_form_gated(html: str, table_count: int) -> bool`

- [ ] **Step 1: Write the failing tests.** Each builds a `Fetched` by hand.
  - `test_403_with_challenge_text_is_bot_protection`: status 403, body containing "Just a moment...". Expected `"bot_protection"`. Same for 429 with "Access Denied", and 503 with `cf-challenge` / "Verify you are human".
  - `test_recaptcha_div_is_captcha`: status 200, `<div class="g-recaptcha">`. Expected `"captcha"`. Also `h-captcha`, `cf-turnstile`, and an `<img>` whose src or alt contains "captcha".
  - `test_redirect_to_login_is_login_required`: `final_url` contains `/login` or `signin`. Expected `"login_required"`. Also a 200 page whose only form has `<input type="password">` and fewer than 200 characters of other text.
  - `test_plain_page_is_not_blocked`: a normal table page. Expected `None`.
  - `test_form_gated`: 2 `<select>` + submit + 0 tables → True. The same with 1 table → False.
  - `test_entry_adds_suggestion`: `entry("captcha", "https://lgdirectory.gov.in/x")["suggestion"]` mentions data.gov.in.
  - `test_entry_formats_limit`: `entry("too_large", "u", n=30)["message"] == "File is larger than the 30 MB limit."`

- [ ] **Step 2: Run them.** `venv/bin/python -m pytest tests/scraping/test_blocked.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `blocked.py`** and `suggestions.json` (`{"lgdirectory.gov.in": "District and sub-district lists are also published on data.gov.in; use the URL tab."}`). Detection checks run in this order: captcha → bot_protection (status in {403, 429, 503} plus challenge markers) → login_required.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git add app/modules/scraping/blocked.py app/modules/scraping/suggestions.json tests/scraping/test_blocked.py && git commit -m "feat(scraping): blocker detection and messages"`

---

### Task 5: Reading files (`items.py`, `files.py`)

**Files:**
- Create:
  - `app/modules/scraping/items.py`
  - `app/modules/scraping/files.py`
  - `tests/scraping/test_files.py`

**Interfaces:**
- Consumes: `Deadline` (Task 3); `html_table._flatten_columns`
- Produces:
  - `items.py`:
    - `TableItem(kind: str, title: str, locator: dict, df: pd.DataFrame)`
    - `TextItem(title: str, source: str, text: str)`
    - `FileExtract(description: str, tables: list[TableItem], texts: list[TextItem], flags: list[str])`
  - `files.py`:
    - `FILE_EXTS = {".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".json", ".pdf", ".docx", ".zip"}`
    - `file_kind(filename: str, content_type: str | None, head: bytes) -> str | None`, returning one of `csv|tsv|excel|json|pdf|docx|zip`
    - `record_arrays(obj, path: str = "$", max_depth: int = 6) -> Iterator[tuple[str, list[dict]]]`
    - `read_file(content: bytes, filename: str, url: str, content_type: str | None = None, deadline: Deadline | None = None) -> FileExtract`

- [ ] **Step 1: Write the failing tests.** All fixtures are built in the test: CSV bytes, openpyxl workbooks, python-docx documents, a reportlab PDF with a gridded `Table`, and zipfile archives.

```python
def test_csv_one_table():                      # kind "file_table", locator {"source":"file","file_url":u}
def test_csv_cp1252_and_bom_and_hindi():       # b"\xef\xbb\xbfDistrict,Value\n..." ; "Pune\xe9".encode("cp1252"); "पटना" utf-8
    # all three read; Hindi value == "पटना"; first column name == "District" (BOM stripped)
def test_tsv_by_extension():
def test_excel_every_nonempty_sheet():         # 3 sheets, one empty -> 2 tables, locator["sheet"] names, description "Excel, 3 sheets → 2 tables"
def test_xls_that_is_really_html():            # b"<table><tr><th>A</th>..." named x.xls -> 1 table
def test_json_record_arrays_nested():          # {"props":{"pageProps":{"rows":[{"a":1,"b":2},{"a":3,"b":4}]}}} -> 1 table, locator["path"]=="$.props.pageProps.rows"
def test_json_ignores_single_field_or_single_record_lists():
def test_pdf_tables_and_text():                # 1 table with header row, texts[0].text contains a known sentence; kind "pdf_table", locator["page"]==1
def test_pdf_without_text_is_flagged_scanned():# reportlab page with only a drawn rectangle -> flags == ["scanned"], description endswith "scanned, no text"
def test_docx_tables_and_text():               # kind "docx_table"
def test_zip_one_level():                      # zip with a.csv + inner.zip + b.exe -> 1 table (locator["inner"]=="a.csv"), nested zip and exe ignored
def test_zip_limits():                         # 51 tiny csvs with scrape_zip_max_files=50 (monkeypatch settings) -> 50 tables + flag "zip_truncated"
def test_zip_bomb_by_declared_size():          # an entry whose uncompressed size exceeds scrape_zip_max_mb -> skipped + flag "zip_truncated", no MemoryError
def test_unknown_type():                       # b"\x89PNG..." x.png -> description "Not read (unsupported type)", no tables
def test_multiheader_flattened():              # Excel with 2 header rows read via HTML fallback -> no "Unnamed:" in columns
```

- [ ] **Step 2: Run them.** `venv/bin/python -m pytest tests/scraping/test_files.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **`file_kind`.** Go by extension first. Then by content type. Then sniff the first bytes: `%PDF` → pdf, `PK\x03\x04` → zip, a leading `{` or `[` → json.
  - **CSV.** Use `pd.read_csv(BytesIO, sep=None|"\t", engine="python", encoding=e)` for `e` in `("utf-8-sig", "cp1252")`, in that order.
  - **Excel.** Use `pd.read_excel(..., sheet_name=None)`. If that raises, fall back to `pd.read_html(BytesIO)`, as HMIS does.
  - **PDF.** Use `pdfplumber`. A table needs at least 2 rows, and its first row becomes the header. If no page has text, the file is "scanned". Check the deadline between pages.
  - **ZIP.**
    - Stop once the number of entries passes `scrape_zip_max_files`, or the declared total size passes `scrape_zip_max_mb`.
    - Read each entry with a capped `read`, so a header that lies about its size can't blow up memory.
    - Ignore nested `.zip` files and unknown types.
  - **Column names.** Pass every DataFrame through `_flatten_columns` and turn the column names into strings.
  - **Descriptions** look like:
    - "CSV → 1 table"
    - "Excel, 3 sheets → 2 tables"
    - "PDF, 12 pages, 2 tables, text"
    - "PDF, 12 pages, scanned, no text"
    - "Word, 2 tables, text"
    - "ZIP, 4 files → 3 tables"
    - "Not read (unsupported type)"

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git add app/modules/scraping/items.py app/modules/scraping/files.py tests/scraping/test_files.py && git commit -m "feat(scraping): file readers"`

---

### Task 6: Reading one HTML page (`discover_http.py`)

**Files:**
- Create: `app/modules/scraping/discover_http.py`, `tests/scraping/test_discover_http.py`

**Interfaces:**
- Consumes:
  - `TableItem`, `TextItem` (Task 5)
  - `files.FILE_EXTS`, `files.record_arrays` (Task 5)
  - `blocked.is_form_gated` (Task 4)
  - `html_table._flatten_columns`
- Produces:
  - `@dataclass PageScan`, with fields:
    - `title: str | None`
    - `tables: list[TableItem]`
    - `file_links: list[str]`
    - `text: TextItem | None`
    - `js_shell: bool`
    - `form_gated: bool`
  - `parse_page(html: str, base_url: str) -> PageScan`

- [ ] **Step 1: Write the failing tests** (HTML as inline strings):
  - `test_tables_with_titles`:
    - `<h3>District beds</h3><table>…38 rows…</table>` gives `kind == "html_table"`, `title == "District beds"`, and `locator == {"source": "page", "table_index": 0}`.
    - A `<caption>` takes priority over a heading.
    - With neither, the title is `"Table 1"`.
  - `test_outer_layout_table_skipped_inner_kept`: a table that contains a nested table gives exactly 1 TableItem, the inner one.
  - `test_empty_or_unparseable_table_skipped`.
  - `test_embedded_next_data_and_json_ld`: `__NEXT_DATA__` holding a records array gives `kind == "embedded"` with `locator["path"]`. A JSON-LD object with no array gives no table.
  - `test_file_links`:
    - Relative `.pdf`, absolute `.xlsx`, `download.php?f=x.csv` (extension only in the query, so ignored), `mailto:`, `#top`, and `.jpg` give exactly the 2 file URLs, made absolute.
    - Same-site links come before other sites.
    - Duplicates and `#fragment` are removed.
  - `test_main_text`: an article page gives `text.source == "page"` and contains the article sentence but not the footer menu words. Uses trafilatura.
  - `test_js_shell`: `<div id="root"></div><script src=app.js>` with almost no text gives `js_shell is True`. A normal table page gives False.
  - `test_form_gated_flag`: wired to `blocked.is_form_gated(html, len(tables))`.

- [ ] **Step 2: Run them.** `venv/bin/python -m pytest tests/scraping/test_discover_http.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `parse_page`.**
  - **Tables.** Use BeautifulSoup (`lxml`). Run `pd.read_html(StringIO(str(tag)))[0]` on each `<table>` that has no descendant `<table>`.
  - **Titles.** The title comes from `<caption>`, else the nearest previous `h1`–`h4`, else `f"Table {n}"`.
  - **Embedded JSON.** Scan `<script type="application/ld+json">`, `<script id="__NEXT_DATA__">` and `<script type="application/json">` through `record_arrays`.
  - **JS shell.** True when the visible text (after removing scripts and styles) is under 200 characters and there are no tables, or when an empty `#root`, `#app` or `#__next` element exists. **(Plan's call:** the spec leaves the exact rule to the builder.)

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git add app/modules/scraping/discover_http.py tests/scraping/test_discover_http.py && git commit -m "feat(scraping): HTML page discovery"`

---

### Task 7: Judging tables (`scoring.py`)

**Files:**
- Create: `app/modules/scraping/scoring.py`, `tests/scraping/test_scoring.py`
- Modify: `app/constants/district_crosswalk.py`. Add `find_district(district: str) -> list[dict]` (every state's matches for a normalised name) and add it to `__all__`.

**Interfaces:**
- Consumes: `semantic.match_to_canonical`, `district_crosswalk.lookup_district`, `lookup_state`, `find_district`, `is_summary_row`
- Produces:
  - `table_hash(df) -> str`. The sha256 of the normalised cell values only. Headers are ignored. Cells are stripped, thousands commas removed, and `3100.0` → `"3100"`.
  - `numeric_share(df) -> float`, rounded to 2 places.
  - `looks_like_layout(df) -> bool`
  - `joinable(df) -> dict | None`. Returns `{"place_column", "state_column", "level", "matched", "total", "unmatched"}`, with `unmatched` capped at 20 names.
  - `personal_data_columns(df) -> list[str]`
  - `score(df) -> dict`. Returns `{"looks_like_layout", "numeric_share", "joinable", "personal_data", "duplicate_of": None, "truncated": False}`.
  - `suggest(signals: dict, rows: int, columns: int) -> str`, returning `"keep"` or `"skip"`.
  - `mark_duplicates(items: list[tuple[str, str, str]]) -> dict[str, str]`. Input tuples are `(finding_id, kind, hash)`. Output maps each duplicate id to the id that was kept.

- [ ] **Step 1: Write the failing tests:**

```python
def test_layout_one_column_nav():       # ["Home","About Us","Contact Us"] -> True
def test_layout_single_row():           # 1 data row -> True
def test_real_table_not_layout():       # 38x4 numbers -> False
def test_numeric_share_handles_commas_and_percent(): # ["1,234","56%","x","NA"] col -> 0.5
def test_joinable_with_state_column():  # State/District = Bihar/Patna, Bihar/Gaya, Bihar/Nowhere -> level "district", matched 2, total 3, unmatched ["Nowhere"]
def test_joinable_district_only():      # "District" col Patna, Gaya -> state_column None, matched 2
def test_joinable_state_level():        # "State Name" Bihar, Kerala -> level "state", matched 2
def test_joinable_none():               # no place columns -> None
def test_summary_row_not_counted():     # "_Bihar" excluded from total
def test_personal_data():               # "Mobile" 9876543210 / "Email" a@b.in / "Name of Officer" -> all 3 flagged; "District Name" and "Facility Name" not flagged
def test_suggest_rules():
    # keep: not layout, no duplicate, rows>=3, cols>=2, numeric_share>0, no personal data
    # skip when any of those fails (one assert per rule)
def test_hash_html_vs_csv_equal():      # same values, one with "1,234" strings and different headers -> equal hashes
def test_duplicates_prefer_files():     # [("h1","html_table",X),("f1","file_table",X),("h2","html_table",Y)] -> {"h1":"f1"}
```

- [ ] **Step 2: Run them.** `venv/bin/python -m pytest tests/scraping/test_scoring.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **Place columns.** A column is a place column when `match_to_canonical(col)` gives `state` or `district`.
  - **Personal data.**
    - Phone: a column where at least 30% of non-null values match `^(\+?91[\s-]?)?[6-9]\d{9}$` after removing spaces and dashes.
    - Email: a column where at least 30% of values match an email regex.
    - Names: headers containing `contact`, `mobile`, `phone` or `email`, or `name` without any of `district|state|block|village|facility|hospital|scheme|indicator|item|phc|chc|school|company|city|town|ward|mandal|taluk|tehsil`.
  - **Layout.**
    - fewer than 2 rows, or fewer than 2 columns, or
    - at least 50% of non-empty cells are navigation words (`home, about us, contact us, login, sitemap, faq, search, skip to main content, screen reader access, feedback, help`).
  - **Duplicate preference.** File kinds (`file_table`, `pdf_table`, `docx_table`) win over `html_table` and `embedded`. When the kind is equal, the first one seen wins.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git add app/modules/scraping/scoring.py app/constants/district_crosswalk.py tests/scraping/test_scoring.py && git commit -m "feat(scraping): table scoring and duplicates"`

---

### Task 8: Staging and the Search job (`store.py`, `scan.py`, part of `service.py`)

**Files:**
- Create:
  - `app/modules/scraping/store.py`
  - `app/modules/scraping/scan.py`
  - `app/modules/scraping/service.py`
  - `tests/scraping/test_store.py`
  - `tests/scraping/test_scan.py`
- Modify: `app/db/init_db.py`, which calls `service.reap_interrupted_scans(db)` and `service.purge_expired(db)` after `reap_orphaned_jobs`.

**Interfaces:**
- Consumes: every earlier task; `dataset_crud.safe_column_names`; `hmis_reports.safe_filename`
- Produces:
  - **`store.py`:**
    - `scan_dir(scan_id) -> Path`
    - `prepare_for_parquet(df) -> pd.DataFrame`. Uses `safe_column_names`. Object columns become `str`, keeping `None`.
    - `write_table(scan_id, finding_id, df) -> str`. Returns the relative path `tables/<fid>.parquet`.
    - `read_table(scan_id, rel) -> pd.DataFrame`
    - `write_bytes(scan_id, position: int, filename, content) -> str`. Returns `files/<position:03d>_<safe_filename>`.
    - `write_text(scan_id, finding_id, text) -> str`
    - `resolve(scan_id, rel) -> Path`. Raises `ValueError` when the result falls outside `scan_dir`.
    - `delete_scan_dir(scan_id) -> None`
  - **`service.py`:**
    - `class ScanNotFound(LookupError)`, `class ScanConflict(Exception)`, `class ScanRequestError(ValueError)`
    - `TABLE_KINDS = {"html_table", "embedded", "file_table", "pdf_table", "docx_table"}`
    - `create_scan(db, url: str, include_files: bool) -> ScrapeScan`. Raises `ScanRequestError` for bad URLs and dedicated hosts, using the message for `dedicated_connector`. Runs `purge_expired` first.
    - `reap_interrupted_scans(db) -> int`
    - `purge_expired(db, now: datetime | None = None) -> int`
  - **`scan.py`:**
    - `run_scan(db, scan_id: str, progress: Callable[[str], None] | None = None, fetcher: Fetcher | None = None) -> dict`. Returns `{"scan_id", "summary"}`.

- [ ] **Step 1: Write the failing store tests** (`test_store.py`):
  - `test_mixed_column_round_trips`: a column `[1, "NA", "-", "1,234", None]` writes and reads back as `["1", "NA", "-", "1,234", None]`.
  - `test_duplicate_and_long_column_names`: two columns named `"A"` and one 80-byte name write without error.
  - `test_resolve_rejects_escape`: `resolve(sid, "../../etc/passwd")` raises ValueError.
  - `test_write_bytes_safe_name`: `"../a b?.csv"` lands inside `scan_dir`.

- [ ] **Step 2: Write the failing scan tests** (`test_scan.py`). They use `httpserver` and `Fetcher(policy=loopback_ok)`, call `create_scan`, then `run_scan`. Each test asserts on the DB rows:
  - `test_page_with_table_csv_and_layout`:
    - The page holds a 38-row district table, a nav table and a link to `data.csv` containing the same values as the HTML table.
    - Expected findings: 2 table findings (`html_table` and `file_table`); the nav table is a finding with `suggested == "skip"` and `looks_like_layout`; 1 `file` finding whose `details` hold `name`, `url`, `content_type`, `bytes` and `extracted`; 1 `text` finding.
    - The html table's `signals["duplicate_of"]` is the file table's id, and the file table has `suggested == "keep"`.
    - Every table finding has `preview` capped at 20×30, `column_names`, `rows`, `columns`, `content_hash`, and an existing parquet file.
    - The file-derived table has `parent_id` set to the file finding's id.
    - `scan.status == "done"`, `summary == {"tables": 3, "files": 1, "text": 1, "blocked": 0, "suggested_keep": 1}`, and `expires_at ≈ finished_at + 24h`.
  - `test_url_is_a_file`: the scan URL itself is a CSV, giving one file finding and one table finding with no page parsing.
  - `test_robots_disallowed_page`: status done, 0 findings, `blocked[0]["code"] == "robots_disallowed"`.
  - `test_disallowed_file_only`: the page is allowed and `/files/` is disallowed. The table is found and the file shows up in `blocked`.
  - `test_private_address_page`: uses the default policy, giving `blocked[0]["code"] == "private_address"` and status done.
  - `test_challenge_page`: a 403 page with "Just a moment" gives `bot_protection`.
  - `test_js_shell_reports_needs_browser`.
  - `test_too_many_files`: `scrape_max_files=2` with 3 links gives 2 file findings plus a `too_many_files` entry.
  - `test_total_budget`: `scrape_total_mb` is monkeypatched tiny. Later files become `too_large`.
  - `test_deadline_stops_cleanly`: an already-expired `Deadline` is injected through the fetcher. Status is done, with a `timeout` entry.
  - `test_row_cap_truncates`: `scrape_max_rows=10` with a 50-row CSV gives `rows == 10` and `signals["truncated"] is True`.
  - `test_unexpected_error_marks_scan_failed`: monkeypatch `discover_http.parse_page` to raise. Then `run_scan` re-raises, the scan has `status == "failed"`, and `error` is set.
  - `test_reap_interrupted_scans`: rows with status running and importing become failed and done.
  - `test_purge_expired`: an expired `done` scan has its rows and folder deleted. An unexpired one and an `imported` one (`expires_at` None) are kept.

- [ ] **Step 3: Run them.** `venv/bin/python -m pytest tests/scraping/test_store.py tests/scraping/test_scan.py -v`. Expected: FAIL.

- [ ] **Step 4: Implement `store.py`, the `service.py` functions listed above, and `scan.py`.**
  - `run_scan` stages, each followed by `progress(...)` with wording close to the spec's examples:
    1. **Fetch the page.** Use `max_bytes = scrape_file_mb * 1MB`. A `ScrapeBlocked` becomes a blocked entry and the scan finishes.
    2. **Classify the answer.** If `blocked.detect` finds something, record it and finish. A status of 400 or above with nothing detected becomes `http_error`.
    3. **Branch on the kind of answer.** If `file_kind(...)` recognises the response as a file, it is handled as a single linked file. Otherwise run `parse_page`, which:
       - records `needs_browser` and `form_gated` entries (the scan continues);
       - adds the page text as a text finding.
    4. **Linked files**, when `options["include_files"]` is on:
       - Read the first `scrape_max_files` links.
       - Keep a running byte total against `scrape_total_mb`.
       - For each file: write it, add a `file` finding, run `read_file`, and add its tables and texts with `parent_id` set.
    5. **Finish the tables.** Apply the row cap, run `score`, `mark_duplicates` and `suggest`, write the Parquet file and the preview.
    6. **Finish the scan.** Set status `done`, the summary, `finished_at`, and `expires_at = finished_at + scrape_hold_hours`.
  - **Deadline.** Build a new `Fetcher()` per scan when none is passed in, and use `fetcher.deadline` for every stage, including `read_file`.
  - **Running out of time.** Hitting the deadline at any stage adds a `timeout` entry and finishes with what was found. It is not a failure.
  - **Unexpected errors.** Set the scan to `failed` with the error text, commit, and re-raise so the job fails too.
  - **`position`** increases in the order findings are created, and the manifest order follows it.

- [ ] **Step 5: Run the tests.** Expected: PASS.

- [ ] **Step 6: Commit.** `git add app/modules/scraping/store.py app/modules/scraping/scan.py app/modules/scraping/service.py app/db/init_db.py tests/scraping/test_store.py tests/scraping/test_scan.py && git commit -m "feat(scraping): staging and scan orchestration"`

---

### Task 9: Saving ticked tables (`importer.py`), plus the manifest, rows, download and delete in `service.py`

**Files:**
- Create: `app/modules/scraping/importer.py`, `tests/scraping/test_importer.py`, `tests/scraping/test_service_reads.py`
- Modify: `app/modules/scraping/service.py`

**Interfaces:**
- Consumes:
  - `ingestion.service.store_result`, `ingestion.service.store_clean_copy`
  - `IngestResult`
  - `store.*`, `service` error types
- Produces:
  - **`service.py`:**
    - `manifest(db, scan_id) -> dict`. Same shape as spec §7.2, without `rendered_in_browser`.
      - Each table carries `finding_id`, `kind`, `title`, `locator`, `rows`, `columns`, `column_names`, `preview`, `signals`, `suggested`, `default_name`, and `imported_dataset_id`.
      - `default_name = f"{host} - {title}"`.
      - Each file carries `finding_id`, `name`, `url`, `content_type`, `bytes`, `extracted`, and `produced` (the ids of findings whose `parent_id` is this file).
      - Each text item carries `finding_id`, `title`, `source`, `characters`, and `snippet` (the first `scrape_snippet_chars` characters).
      - `blocked` is passed through from the scan.
    - `list_scans(db, limit: int) -> list[dict]`. Returns `scan_id`, `url`, `status`, `created_at`, `expires_at`, `summary`, newest first.
    - `table_rows(db, scan_id, finding_id, limit: int, offset: int) -> dict`. Returns `{"total", "columns", "rows"}`.
    - `download(db, scan_id, finding_id) -> tuple[bytes, str, str]`. Returns `(body, filename, media_type)`.
    - `delete_scan(db, scan_id) -> None`
  - **`importer.py`:**
    - `CONNECTOR_FOR_KIND = {"html_table": "web_table", "embedded": "web_table", "file_table": "web_file", "pdf_table": "web_file", "docx_table": "web_file"}`
    - `claim_for_import(db, scan_id, items: list[dict]) -> None`. Each item is `{"finding_id", "name"?}`.
    - `run_import(db, scan_id, survey_id: int, items: list[dict], progress=None) -> dict`. Returns `{"items": [{"finding_id", "raw_dataset_id", "dataset_id", "rows", "columns"}]}`, where `dataset_id` is the clean dataset, the same as fetch jobs.

- [ ] **Step 1: Write the failing read tests** (`test_service_reads.py`). Build a done scan using the Task 8 helper from `test_scan.py`, moved into `tests/scraping/helpers.py`.
  - `manifest` groups the findings and keeps their order. `summary` matches. Cells are JSON-safe (NaN → None).
  - `table_rows`: `limit=10, offset=30` on 38 rows gives 8 rows and `total == 38`.
  - A text or file finding raises `ScanRequestError`. An unknown id raises `ScanNotFound`.
  - `download`:
    - a table gives CSV starting with `﻿` (BOM), `text/csv`, and filename `<safe default_name>.csv`;
    - a file gives the original bytes, its name, and its content type;
    - text gives `text/plain; charset=utf-8`.
  - Reading rows or downloading from a scan whose folder is gone (an imported scan) raises `ScanConflict` with the text "This search was saved; open the saved dataset instead."
  - `delete_scan` on a done scan removes its rows and folder. On a running, importing or imported scan it raises `ScanConflict`.

- [ ] **Step 2: Write the failing import tests** (`test_importer.py`):
  - **`test_import_stores_raw_and_clean`:**
    - Each picked table becomes a raw `Dataset` and a clean `Dataset` with `connector == "web_table"` or `"web_file"`, and `survey_id == 7` on both.
    - `source_ref` starts with the page URL (file URL for file tables) and contains the locator.
    - `raw_file_path is None`.
    - `notes` contain the scan id, the finding id and the row count.
    - The clean dataset's `parent_ids == [raw.id]`.
    - The custom name is used for the raw dataset and `"<name> (cleaned)"` for the clean one.
  - **`test_after_import_unpicked_are_gone`:**
    - The scan status is `imported` and `expires_at is None`.
    - The scan folder is deleted.
    - The unpicked findings' rows are deleted.
    - The picked findings remain, with `imported_dataset_id` set.
  - **`test_claim_rules`.** Each case is one assert:
    - an unknown scan raises `ScanNotFound`;
    - a scan whose status isn't `done` raises `ScanConflict`;
    - an empty `items` list raises `ScanRequestError`;
    - a finding from another scan raises `ScanRequestError`;
    - a text or file finding raises `ScanRequestError`;
    - a duplicate `finding_id` in `items` raises `ScanRequestError`.
  - **`test_claim_is_atomic`.** Two `claim_for_import` calls in a row on the same scan: the first succeeds (status → `importing`) and the second raises `ScanConflict`. Use `UPDATE scrape_scans SET status='importing' WHERE id=:id AND status='done'` and check `rowcount == 1`.
  - **`test_failed_import_keeps_scan_retryable`.** Monkeypatch `store_clean_copy` to raise on the 2nd item. Then:
    - `run_import` raises;
    - the 1st finding keeps its `imported_dataset_id`;
    - the scan status goes back to `done` and the folder still exists;
    - a new claim with only the 2nd item succeeds.

- [ ] **Step 3: Run them.** `venv/bin/python -m pytest tests/scraping/test_importer.py tests/scraping/test_service_reads.py -v`. Expected: FAIL.

- [ ] **Step 4: Implement.**
  - **Building each raw dataset.**
    - Build `IngestResult(dataframe=store.read_table(...), connector=CONNECTOR_FOR_KIND[kind], source_ref=f"{base} [{k=v, ...}]")`, where `base` is `locator["file_url"]` or `scan.final_url or scan.url`, and the brackets list the locator keys other than `source` and `file_url`.
    - Pass no `raw_bytes`, so no file is written.
    - Call `store_result(db, result, name)`. Then set `survey_id` and `notes` and commit.
  - **Clean copy.** Call `store_clean_copy(db, raw, name)`, set its `survey_id`, and commit.
  - **Per item.** Set `finding.imported_dataset_id = raw.id`, commit, and report progress as `"Saved {i}/{n}: {name}"`.
  - **At the end.** Delete the unpicked findings and the folder. Set status `imported` and `expires_at=None`.
  - **On any exception.** Set status back to `done`, commit, and re-raise.

- [ ] **Step 5: Run the tests.** Expected: PASS.

- [ ] **Step 6: Commit.** `git add app/modules/scraping/importer.py app/modules/scraping/service.py tests/scraping && git commit -m "feat(scraping): import picked tables, manifest, rows, download, delete"`

---

### Task 10: HTTP endpoints, job wiring and documentation

**Files:**
- Create: `app/modules/scraping/schemas.py`, `app/modules/scraping/router.py`, `tests/scraping/test_router.py`
- Modify:
  - `app/modules/jobs/service.py`. In `_run`, add the `scrape_scan` and `scrape_import` branches with their `_run_*` functions.
  - `app/modules/jobs/lineage.py`. Also collect `item["dataset_id"]` from `result.get("items")`.
  - `app/modules/reports/naming.py`. Add `"web_table": "web"` and `"web_file": "file"` to `CONNECTOR_CATEGORY`.
  - `app/routes/__init__.py`. Include `scraping_router` before `merging_router`.
  - `docs/superpowers/specs/2026-11-03-website-tab-design.md`, `API_TESTING.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: Tasks 8–9; `jobs.service.enqueue`; `JobOut`
- Produces the HTTP contract under the router prefix `/scrape`, tag `"website scraping"`:

  | Method | Path | Body / query | Success | Errors |
  |---|---|---|---|---|
  | POST | `/scrape/scan` | `{"url": str, "include_files": bool = true}` | 202 `JobOut` (`job_type "scrape_scan"`, `result.request.scan_id` set at once; on success `result = {"request", "scan_id", "summary"}`) | 422 bad scheme / dedicated host |
  | GET | `/scrape/scans` | `?limit=20` (1–100) | 200 list | |
  | GET | `/scrape/scans/{scan_id}` | | 200 manifest | 404 |
  | GET | `/scrape/scans/{scan_id}/findings/{finding_id}/rows` | `?limit=50&offset=0` (limit 1–1000) | 200 | 404, 409 saved, 422 not a table |
  | GET | `/scrape/scans/{scan_id}/findings/{finding_id}/download` | | 200 file (`Content-Disposition: attachment`) | 404, 409 |
  | POST | `/scrape/scans/{scan_id}/import` | `{"survey_id": int, "items": [{"finding_id": str, "name": str?}]}` (min 1 item) | 202 `JobOut` (`job_type "scrape_import"`) | 404, 409, 422 |
  | DELETE | `/scrape/scans/{scan_id}` | | 204 | 404, 409 |

  Error mapping: `ScanNotFound` → 404, `ScanConflict` → 409, `ScanRequestError` → 422, each with `detail = str(exc)`.

- [ ] **Step 1: Write the failing tests** in `test_router.py`, using `TestClient(app)` without the `with` form, so startup doesn't seed anything.
  - **`test_scan_returns_202_with_scan_id`:**
    - Monkeypatch `jobs.service._executor.submit` to a no-op.
    - Expected: 202, `job_type == "scrape_scan"`, and the `scan_id` from `result["request"]` exists in `scrape_scans` with status `running` and `job_id` set.
  - **`test_scan_rejects`:** `ftp://…` gives 422. `https://hmis.mohfw.gov.in/` gives 422 with the dedicated-connector message.
  - **`test_job_runs_scan_end_to_end`:**
    - Call `jobs.service._run(job.id, payload)` directly, with `scan.run_scan` monkeypatched to use `Fetcher(policy=loopback_ok)` against `httpserver`.
    - Expected: the job is `succeeded`, `result["summary"]["tables"] >= 1`, and `GET /scrape/scans/{id}` gives 200 with tables.
  - **`test_import_double_click`:**
    - Run two `POST …/import` calls one after the other on a done scan, with `submit` patched to a no-op.
    - Expected: the first gives 202 and the second gives 409. Only one `scrape_import` job row exists.
  - **`test_import_job_and_lineage`:** run the import job through `_run`. `lineage.dataset_ids_for_job(db, job.id)` equals the clean dataset ids.
  - **`test_rows_download_delete_status_codes`:** one assert per row of the error table above.
  - **`test_naming_categories`:** `category_for("web_table") == "web"` and `category_for("web_file") == "file"`.

- [ ] **Step 2: Run them.** `venv/bin/python -m pytest tests/scraping/test_router.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **Scan endpoint.** It calls `service.create_scan` and then `enqueue(db, "scrape_scan", payload={"scan_id": scan.id, "url": ..., "include_files": ...})`. It stores `job.id` on the scan.
  - **Import endpoint.** It calls `importer.claim_for_import` and then `enqueue(db, "scrape_import", payload={"scan_id", "survey_id", "items"})`.
  - **Job functions.** `_run_scrape_scan_job` and `_run_scrape_import_job` follow `_run_fetch_job`: a progress callback under `_lock`, then `status="succeeded"`, `result={"request": payload, **summary}`. For import jobs, `dataset_id` is the first item's clean dataset id.
  - **Response models.** The manifest and list responses use Pydantic models in `schemas.py`, so `/docs` shows the shapes from spec §7.2.

- [ ] **Step 4: Run the whole suite.** `venv/bin/python -m pytest -v`. Expected: all PASS.

- [ ] **Step 5: Smoke test against the live app** (no migration needed, because it uses a throwaway SQLite database):
  - Start the app on a throwaway SQLite file in your scratch directory (`SMOKE=sqlite:///<scratch>/scrape-smoke.db`). First create its tables with `PLATFORM_DATABASE_URL=$SMOKE venv/bin/python -c "from app.db.base import Base; from app.db.session import engine; import app.models; Base.metadata.create_all(engine)"`. Then run `PLATFORM_DATABASE_URL=$SMOKE venv/bin/python -m uvicorn app.main:app --port 8001`.
  - `POST http://127.0.0.1:8001/scrape/scan` with `{"url": "https://rbi.org.in/Scripts/AnnualPublications.aspx"}`.
  - Poll `/jobs/{id}` until it succeeds, then `GET /scrape/scans/{scan_id}`.
  - Expected: at least one table or file finding, or a clear blocked entry.
  - Paste the summary into your report. A live site may block us, which is acceptable; report what happened. Stop the server afterwards.

- [ ] **Step 6: Update the documentation.**
  - **Spec**, matching "Decisions made after the spec" at the top of this plan:
    - §6.1: the staging path and the cleanup timing.
    - §6.2: the connector names and the survey stamp on both datasets.
    - §7.1: no `render` field in phase 1.
    - §7.5: one import per Save, and the result fields `raw_dataset_id` / `dataset_id`.
    - §8: the portal sends one import per Save.
    - §12: option B chosen for phase 1.
  - **`API_TESTING.md`:** add a "Website scraping" section with a curl example per endpoint.
  - **`CLAUDE.md`:**
    - Replace "No test suite…" with the pytest command and `requirements-dev.txt`.
    - Add a short "Website scraping (`modules/scraping/`)" paragraph: staging folder, the two job types, and that ticked tables become ordinary `ds_` raw and clean datasets.

- [ ] **Step 7: Commit.** `git add app tests docs API_TESTING.md CLAUDE.md && git commit -m "feat(scraping): /scrape endpoints, job wiring, docs"`

- [ ] **Step 8: Report the migration.** Tell the owner the migration file path from Task 1 and the command to apply it: `venv/bin/alembic upgrade head`. The new endpoints return 500 on the real database until it is applied.
