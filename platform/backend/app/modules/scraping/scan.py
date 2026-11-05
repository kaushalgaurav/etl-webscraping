"""The Search job: read one page and the files it links to (spec §4).

Stages, each reported through `progress`:
  1. fetch the page (address guard, robots.txt, limits)
  2. classify the answer (bot protection, CAPTCHA, login, HTTP error)
  3. the URL is a file -> read it; else parse the page
  4. when the page is a JavaScript shell or has no tables (or the admin
     asked), open it in a headless browser: rendered tables, background
     data feeds (with their further pages), export-button files
  5. download and read linked files, within the file and byte limits
  6. score every table, mark duplicates, stage tables as Parquet
A blocked page or file is an entry in `scan.blocked`, not a failure; running
out of time stops the scan with what it has. Only an unexpected error fails
the scan (and the job).
"""
import dataclasses
import json
import logging
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

import pandas as pd
from bs4 import UnicodeDammit
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ScrapeFinding, ScrapeScan
from app.models.common import utcnow
from app.modules.scraping import blocked, discover_browser, discover_http, feeds, files, scoring, store
from app.modules.scraping.fetcher import Fetched, Fetcher
from app.modules.scraping.items import TableItem, TextItem
from app.modules.scraping.safety import ScrapeBlocked
from app.modules.scraping.service import TABLE_KINDS, get_scan, hold_until

MB = 1_048_576
log = logging.getLogger(__name__)
# Statuses that mean "nothing here", not "not for you": no browser retry.
NOT_THERE = (404, 410)


def _got_through(rendered: "discover_browser.Rendered") -> bool:
    """The browser reached data: tables, file links, feeds or downloads."""
    page = discover_http.parse_page(rendered.html, rendered.final_url)
    return bool(page.tables or page.file_links or rendered.feeds or rendered.downloads)


def _preview(df: pd.DataFrame) -> list[list]:
    part = df.iloc[: settings.scrape_preview_rows, : settings.scrape_preview_cols]
    return json.loads(part.to_json(orient="values", date_format="iso", force_ascii=False))


def _looks_like_html(fetched: Fetched) -> bool:
    ctype = (fetched.content_type or "").lower()
    if "html" in ctype:  # text/html and application/xhtml+xml
        return True
    head = fetched.content[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or (b"<body" in head or b"<table" in head)


class _Run:
    def __init__(self, db: Session, scan: ScrapeScan, fetcher: Fetcher,
                 progress: Callable[[str], None]):
        self.db = db
        self.scan = scan
        self.fetcher = fetcher
        self.say = progress
        self.blocked: list[dict] = []
        self.position = 0
        self.findings: list[ScrapeFinding] = []
        # Table findings, already staged on disk. Each table is scored and
        # written to Parquet the moment it is read, so only its small summary
        # stays in memory; duplicates and suggestions are settled at the end,
        # since they are judged across the whole scan.
        self.tables: list[ScrapeFinding] = []
        self.bytes_used = 0
        self.rendered_in_browser = False
        # Findings get their UUID before they reach the database (their files
        # are named by it); a child names its parent by that key, and the
        # BIGINT parent_id is filled in at finish(), once ids exist.
        self.parent_of: dict[str, str] = {}

    # bookkeeping ------------------------------------------------------------
    def next_position(self) -> int:
        self.position += 1
        return self.position

    def block(self, code: str, what: str, **fmt) -> None:
        if code == "timeout" and any(b["code"] == "timeout" for b in self.blocked):
            return
        if code == "timeout":
            fmt.setdefault("n", settings.scrape_scan_seconds)
        self.blocked.append(blocked.entry(code, what, **fmt))

    def block_exc(self, exc: ScrapeBlocked) -> None:
        if exc.code == "timeout":
            self.block("timeout", exc.what)
            return
        self.blocked.append({"what": exc.what, "code": exc.code, "message": exc.message,
                             "suggestion": blocked.suggestion_for(exc.what)})

    def _new_key(self, parent_key: str | None) -> uuid.UUID:
        key = uuid.uuid4()
        if parent_key:
            self.parent_of[str(key)] = parent_key
        return key

    def add_text(self, item: TextItem, parent_key: str | None) -> None:
        fid = self._new_key(parent_key)
        locator = {"source": "page"} if item.source == "page" else {"source": "file", "file_url": item.source}
        self.findings.append(ScrapeFinding(
            external_id=fid, scan_id=self.scan.id, position=self.next_position(), kind="text",
            title=item.title, locator=locator,
            details={"source": item.source, "characters": len(item.text),
                     "snippet": item.text[: settings.scrape_snippet_chars]},
            storage_path=store.write_text(self.scan.key, str(fid), item.text)))

    def stage_table(self, item: TableItem, parent_key: str | None) -> None:
        fid = self._new_key(parent_key)
        position = self.next_position()
        df = item.df
        truncated = len(df) > settings.scrape_max_rows
        if truncated:
            df = df.head(settings.scrape_max_rows)
        df = store.prepare_for_parquet(df)
        item.df = None  # let the original go as soon as the staged copy exists
        signals = scoring.score(df)
        signals["truncated"] = truncated
        self.tables.append(ScrapeFinding(
            external_id=fid, scan_id=self.scan.id, position=position, kind=item.kind,
            title=item.title, locator=item.locator,
            rows=int(df.shape[0]), columns=int(df.shape[1]),
            column_names=[str(c) for c in df.columns], preview=_preview(df), signals=signals,
            content_hash=scoring.table_hash(df),
            storage_path=store.write_table(self.scan.key, str(fid), df)))

    def add_file(self, fetched: Fetched) -> None:
        fid = self._new_key(None)
        position = self.next_position()
        self.say(f"Reading {fetched.filename}")
        extract = files.read_file(
            fetched.content, fetched.filename, fetched.final_url, fetched.content_type,
            self.fetcher.deadline, on_table=lambda table: self.stage_table(table, str(fid)),
            byte_budget=max(0, settings.scrape_total_mb * MB - self.bytes_used))
        self.bytes_used += extract.unpacked_bytes
        self.findings.append(ScrapeFinding(
            external_id=fid, scan_id=self.scan.id, position=position, kind="file",
            title=fetched.filename, locator={"source": "file", "file_url": fetched.final_url},
            details={"name": fetched.filename, "url": fetched.final_url,
                     "content_type": fetched.content_type, "bytes": len(fetched.content),
                     "extracted": extract.description, "flags": extract.flags},
            storage_path=store.write_bytes(self.scan.key, position, fetched.filename, fetched.content)))
        for text in extract.texts:
            self.add_text(text, str(fid))
        if "timeout" in extract.flags:
            self.block("timeout", fetched.final_url)

    @staticmethod
    def html_spreadsheet(fetched: Fetched) -> bool:
        """A .xls that is really an HTML table (HMIS reports are) is a file."""
        return (files.file_kind(fetched.filename, None, b"") == "excel"
                and b"<table" in fetched.content[:65536].lower())

    # stages -----------------------------------------------------------------
    def execute(self) -> None:
        file_cap = settings.scrape_file_mb * MB
        self.say("Reading page")
        try:
            page = self.fetcher.get(self.scan.url, max_bytes=file_cap)
        except ScrapeBlocked as exc:
            self.block_exc(exc)
            return
        self.scan.final_url = page.final_url

        is_html = _looks_like_html(page)
        html = UnicodeDammit(page.content, is_html=True).unicode_markup if is_html else None
        code = blocked.detect(page, html)
        refused = code == "bot_protection" or (
            page.status >= 400 and page.status not in NOT_THERE)
        if page.status >= 400 and not refused:
            self.block(code or "http_error", page.final_url,
                       **({} if code else {"status": page.status}))
            return

        if not is_html and not refused:
            self.bytes_used += len(page.content)
            self.add_file(page)
            return

        parsed = discover_http.parse_page(html or "", page.final_url)
        # A CAPTCHA or login box only blocks us when it stands in front of the
        # data. On a page that shows its tables anyway (a feedback form's
        # CAPTCHA, a site search box) the data is read as usual.
        stuck = refused or (code and not (parsed.tables or parsed.file_links))
        if stuck:
            # Second try: a real browser gets past many firewalls, JavaScript
            # challenges and pages whose CAPTCHA only guards a form.
            rendered = self.render_page(page.final_url, parsed, force=True)
            if rendered is None or not _got_through(rendered):
                self.block(code or "http_error", page.final_url,
                           **({} if code else {"status": page.status}))
                return
            parsed = discover_http.parse_page("", page.final_url)
        else:
            rendered = self.render_page(page.final_url, parsed)
        if rendered is not None:
            parsed = self.read_rendered(parsed, rendered)
        for table in parsed.tables:
            self.stage_table(table, None)
        if parsed.text:
            self.add_text(parsed.text, None)
        if parsed.js_shell and rendered is None:
            self.block("needs_browser", page.final_url)
        if parsed.form_gated:
            self.block("form_gated", page.final_url)
        self.say(f"Found {len(parsed.tables)} table(s) and {len(parsed.file_links)} file link(s)")
        if rendered is not None:
            self.add_feeds(rendered.feeds)
            self.add_downloads(rendered.downloads)

        if self.scan.options.get("include_files", True):
            self.download_files(parsed.file_links)

    # browser (phase 2) -----------------------------------------------------
    def render_page(self, url: str, parsed: discover_http.PageScan,
                    force: bool = False) -> "discover_browser.Rendered | None":
        """`force`: the plain read was refused, so try the browser even in
        "auto" mode with nothing else to go on. "never" is still respected."""
        mode = self.scan.options.get("render", "auto")
        if mode == "never" or (mode == "auto" and not force
                               and parsed.tables and not parsed.js_shell):
            return None
        if not settings.scrape_browser_enabled:
            if mode == "always":
                self.block("browser_unavailable", url)
            return None
        try:
            rendered = discover_browser.render(url, policy=self.fetcher.policy,
                                               deadline=self.fetcher.deadline, say=self.say)
        except ScrapeBlocked as exc:
            self.block_exc(exc)
            return None
        except Exception:
            # A browser crash costs the browser stage only, never the Search.
            log.exception("Browser rendering failed for %s", url)
            self.block("browser_failed", url)
            return None
        self.rendered_in_browser = True
        own_host = urlsplit(url).hostname
        for host in rendered.refused:
            if host != own_host:
                self.block("private_address", host)
        if rendered.over_budget:
            self.block("too_large", url, n=settings.scrape_browser_mb)
        return rendered

    @staticmethod
    def read_rendered(plain: discover_http.PageScan,
                      rendered: "discover_browser.Rendered") -> discover_http.PageScan:
        """The rendered page replaces the plain one; file links from both are kept."""
        page = discover_http.parse_page(rendered.html, rendered.final_url)
        tables = [dataclasses.replace(t, kind="rendered_table") if t.kind == "html_table" else t
                  for t in page.tables]
        links = list(dict.fromkeys(plain.file_links + page.file_links))
        return dataclasses.replace(page, tables=tables, file_links=links,
                                   text=page.text or plain.text, js_shell=False)

    def add_feeds(self, found: list) -> None:
        """Feeds count against the scan's byte total like files, and are read
        one at a time, each released once its tables are staged."""
        if not found:
            return
        self.say(f"Found {len(found)} data feed(s)")
        budget = settings.scrape_total_mb * MB
        for i in range(len(found)):
            feed, found[i] = found[i], None
            if self.bytes_used + feed.size > budget:
                self.block("too_large", feed.url, n=settings.scrape_total_mb)
                continue
            self.bytes_used += feed.size
            self.say(f"Reading data feed {i + 1} of {len(found)}")
            result = feeds.collect(feed, self.fetcher, max_pages=settings.scrape_feed_pages,
                                   max_bytes=settings.scrape_feed_mb * MB,
                                   byte_budget=budget - self.bytes_used)
            self.bytes_used += result.bytes
            for table in result.tables:
                self.stage_table(table, None)
            for exc in result.blocked:
                self.block_exc(exc)

    def add_downloads(self, downloads: list[Fetched]) -> None:
        budget = settings.scrape_total_mb * MB
        for got in downloads:
            if self.bytes_used + len(got.content) > budget:
                self.block("too_large", got.final_url, n=settings.scrape_total_mb)
                continue
            self.bytes_used += len(got.content)
            self.add_file(got)

    def download_files(self, links: list[str]) -> None:
        limit = settings.scrape_max_files
        if len(links) > limit:
            self.block("too_many_files", self.scan.url, n=limit)
        budget = settings.scrape_total_mb * MB
        for i, link in enumerate(links[:limit], 1):
            if self.fetcher.deadline.expired():
                self.block("timeout", link)
                return
            self.say(f"Downloading files {i} of {min(len(links), limit)}")
            left = budget - self.bytes_used
            cap = min(settings.scrape_file_mb * MB, left)
            if cap <= 0:
                self.block("too_large", link, n=settings.scrape_total_mb)
                continue
            try:
                got = self.fetcher.get(link, max_bytes=cap, referer=self.scan.final_url)
            except ScrapeBlocked as exc:
                self.block_exc(exc)
                if exc.code == "timeout":
                    return
                continue
            if got.status >= 400:
                self.block("http_error", link, status=got.status)
                continue
            if _looks_like_html(got) and not self.html_spreadsheet(got):
                # Asked for a file, got a web page: a firewall's refusal, a
                # login wall, or a "moved" page. Report it, don't read it.
                html = UnicodeDammit(got.content, is_html=True).unicode_markup
                self.block(blocked.detect(got, html, expected_file=True) or "unsupported", link)
                continue
            self.bytes_used += len(got.content)
            self.add_file(got)

    def finish_tables(self) -> None:
        self.say("Judging tables")
        duplicates = scoring.mark_duplicates([(f.key, f.kind, f.content_hash) for f in self.tables])
        for finding in self.tables:
            finding.signals = {**finding.signals, "duplicate_of": duplicates.get(finding.key)}
            finding.suggested = scoring.suggest(finding.signals, finding.rows, finding.columns)
        self.findings.extend(self.tables)

    def finish(self) -> None:
        self.finish_tables()
        self.db.add_all(self.findings)
        self.db.flush()
        ids = {f.key: f.id for f in self.findings}
        for finding in self.findings:
            parent = self.parent_of.get(finding.key)
            finding.parent_id = ids.get(parent) if parent else None
        kinds = [f.kind for f in self.findings]
        now = utcnow()
        self.scan.blocked = self.blocked
        self.scan.summary = {
            "tables": sum(k in TABLE_KINDS for k in kinds),
            "files": kinds.count("file"),
            "text": kinds.count("text"),
            "blocked": len(self.blocked),
            "suggested_keep": sum(f.suggested == "keep" for f in self.findings),
            "rendered_in_browser": self.rendered_in_browser,
        }
        self.scan.status = "done"
        self.scan.finished_at = now
        self.scan.expires_at = hold_until(now)
        self.db.commit()
        self.say(f"Done: {self.scan.summary['tables']} table(s), {self.scan.summary['files']} file(s), "
                 f"{len(self.blocked)} couldn't be read")


def run_scan(db: Session, scan_id: str, progress: Callable[[str], None] | None = None,
             fetcher: Fetcher | None = None) -> dict:
    scan = get_scan(db, scan_id)
    run = _Run(db, scan, fetcher or Fetcher(), progress or (lambda _message: None))
    try:
        run.execute()
        run.finish()
    except Exception as exc:
        db.rollback()
        scan = get_scan(db, scan_id)
        scan.status = "failed"
        scan.error = str(exc)
        scan.finished_at = utcnow()
        db.commit()
        raise
    return {"scan_id": scan.key, "summary": scan.summary}
