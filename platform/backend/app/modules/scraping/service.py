"""Website tab: scan lifecycle around the Search and Save jobs.

  create_scan          validate the URL, open a scan row (the job runs scan.run_scan)
  reap_interrupted_scans  after a restart: running -> failed, importing -> done
  purge_expired        delete unsaved scans past their hold (rows + staged files)
  manifest / list_scans / table_rows / download / delete_scan   the review screen

Every id that leaves this module is a row's `key` (its external UUID as text);
the BIGINT `id` stays inside the database.
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.helpers.dataframe import json_safe_records
from app.models import ScrapeFinding, ScrapeScan
from app.models.common import utcnow
from app.models.web_base import parse_key
from app.modules.ingestion.connectors.hmis_reports import safe_filename
from app.modules.scraping import safety, store

logger = logging.getLogger(__name__)

TABLE_KINDS = {"html_table", "rendered_table", "feed", "embedded",
               "file_table", "pdf_table", "docx_table"}


class ScanNotFound(LookupError):
    """404."""


class ScanConflict(Exception):
    """409: the scan is not in a state that allows this."""


class ScanRequestError(ValueError):
    """422: the request itself is wrong."""


def hold_until(start: datetime) -> datetime:
    return start + timedelta(hours=settings.scrape_hold_hours)


def create_scan(db: Session, url: str, include_files: bool, render: str = "auto") -> ScrapeScan:
    url = (url or "").strip()
    try:
        safety.check_url(url)
    except safety.ScrapeBlocked as exc:
        raise ScanRequestError(exc.message) from exc
    purge_expired(db)
    now = utcnow()
    scan = ScrapeScan(url=url, status="running", options={"include_files": include_files, "render": render},
                      blocked=[], created_at=now, expires_at=hold_until(now))
    db.add(scan)
    db.commit()
    db.refresh(scan)
    return scan


def has_saved_tables(db: Session, scan: ScrapeScan) -> bool:
    """True once any of the scan's tables was saved: from then on the scan is the
    record of where those tables came from, and must never expire."""
    return db.query(ScrapeFinding.id).filter(
        ScrapeFinding.scan_id == scan.id, ScrapeFinding.imported_dataset_id.is_not(None)).first() is not None


def reap_interrupted_scans(db: Session) -> int:
    """Called at startup: the worker pool died with the process."""
    count = 0
    for scan in db.query(ScrapeScan).filter(ScrapeScan.status.in_(("running", "importing"))):
        if scan.status == "running":
            scan.status = "failed"
            scan.error = "Interrupted: the server restarted during this search."
            scan.finished_at = utcnow()
        else:
            # Items saved before the restart keep their dataset ids; the rest
            # can be saved again from the same scan, which is now provenance.
            scan.status = "done"
            if has_saved_tables(db, scan):
                scan.expires_at = None
        count += 1
    if count:
        db.commit()
    return count


def startup_cleanup(db: Session) -> bool:
    """Reap interrupted scans and purge expired ones; never block startup.

    Before `alembic upgrade head` creates the scrape_* tables, these queries
    fail. That must cost the Website tab only, not the whole app, so the
    error is logged and the rest of startup carries on.
    """
    try:
        reap_interrupted_scans(db)
        purge_expired(db)
        sweep_orphan_folders(db)
        return True
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Website tab tables are missing or unreadable (%s); "
                       "run `alembic upgrade head`. The rest of the app is unaffected.",
                       exc.__class__.__name__)
        return False


def _delete_scan_rows(db: Session, scan: ScrapeScan) -> None:
    db.query(ScrapeFinding).filter(ScrapeFinding.scan_id == scan.id).delete(
        synchronize_session=False)
    db.delete(scan)


def purge_expired(db: Session, now: datetime | None = None) -> int:
    """Delete unsaved scans whose hold has passed. Saved scans have no expiry."""
    now = now or utcnow()
    expired = (db.query(ScrapeScan)
               .filter(ScrapeScan.expires_at.is_not(None), ScrapeScan.expires_at < now,
                       ScrapeScan.status != "importing")
               .all())
    for scan in expired:
        if has_saved_tables(db, scan):  # partly saved: provenance, never purged
            scan.expires_at = None
            db.commit()
            continue
        key = scan.key
        _delete_scan_rows(db, scan)
        db.commit()
        store.delete_scan_dir(key)
    return len(expired)


LIVE_STATUSES = ("running", "done", "importing")


def sweep_orphan_folders(db: Session) -> int:
    """Delete staging folders no live scan owns.

    A folder outlives its scan when the server stops mid-delete, or after the
    id migration (folders were named by the old text ids). Only scans that
    can still be reviewed or saved keep a folder.
    """
    root = Path(settings.scrape_dir)
    if not root.is_dir():
        return 0
    live = {s.key for s in db.query(ScrapeScan).filter(ScrapeScan.status.in_(LIVE_STATUSES))}
    removed = 0
    for folder in root.iterdir():
        if folder.is_dir() and folder.name not in live:
            store.delete_scan_dir(folder.name)
            removed += 1
    return removed


# Reads -----------------------------------------------------------------------
SAVED_MESSAGE = "This search was saved; open the saved dataset instead."


def get_scan(db: Session, scan_id: str) -> ScrapeScan:
    """The scan whose key is `scan_id`; a malformed id is simply not found."""
    key = parse_key(scan_id)
    scan = db.query(ScrapeScan).filter(ScrapeScan.external_id == key).first() if key else None
    if scan is None:
        raise ScanNotFound(f"Scan '{scan_id}' not found.")
    return scan


def find_finding(db: Session, finding_id: str) -> ScrapeFinding | None:
    key = parse_key(finding_id)
    return db.query(ScrapeFinding).filter(ScrapeFinding.external_id == key).first() if key else None


def get_finding(db: Session, scan: ScrapeScan, finding_id: str) -> ScrapeFinding:
    finding = find_finding(db, finding_id)
    if finding is None or finding.scan_id != scan.id:
        raise ScanNotFound(f"Finding '{finding_id}' not found in scan '{scan.key}'.")
    return finding


def default_name(scan: ScrapeScan, finding: ScrapeFinding) -> str:
    host = urlsplit(scan.final_url or scan.url).hostname or "website"
    return f"{host} - {finding.title}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def manifest(db: Session, scan_id: str) -> dict:
    """Everything the review screen draws for one scan (spec §7.2)."""
    scan = get_scan(db, scan_id)
    rows = (db.query(ScrapeFinding).filter(ScrapeFinding.scan_id == scan.id)
            .order_by(ScrapeFinding.position).all())
    keys = {f.id: f.key for f in rows}
    tables, files, texts = [], [], []
    for f in rows:
        if f.kind in TABLE_KINDS:
            tables.append({
                "finding_id": f.key, "kind": f.kind, "title": f.title, "locator": f.locator,
                "parent_id": keys.get(f.parent_id), "rows": f.rows, "columns": f.columns,
                "column_names": f.column_names, "preview": f.preview, "signals": f.signals,
                "suggested": f.suggested, "default_name": default_name(scan, f),
                "imported_dataset_id": f.imported_dataset_id,
            })
        elif f.kind == "file":
            details = f.details or {}
            files.append({
                "finding_id": f.key, "name": details.get("name"), "url": details.get("url"),
                "content_type": details.get("content_type"), "bytes": details.get("bytes"),
                "extracted": details.get("extracted"), "flags": details.get("flags") or [],
                "produced": [r.key for r in rows if r.parent_id == f.id],
            })
        elif f.kind == "text":
            details = f.details or {}
            texts.append({
                "finding_id": f.key, "title": f.title, "source": details.get("source"),
                "characters": details.get("characters"), "snippet": details.get("snippet"),
            })
    return {
        "scan_id": scan.key, "url": scan.url, "final_url": scan.final_url, "status": scan.status,
        "rendered_in_browser": bool((scan.summary or {}).get("rendered_in_browser")),
        "error": scan.error, "job_id": scan.job_id, "created_at": _iso(scan.created_at),
        "finished_at": _iso(scan.finished_at), "expires_at": _iso(scan.expires_at),
        "summary": scan.summary, "tables": tables, "files": files, "text": texts,
        "blocked": scan.blocked or [],
    }


def list_scans(db: Session, limit: int) -> list[dict]:
    scans = db.query(ScrapeScan).order_by(ScrapeScan.created_at.desc()).limit(limit).all()
    return [{"scan_id": s.key, "url": s.url, "status": s.status, "created_at": _iso(s.created_at),
             "expires_at": _iso(s.expires_at), "summary": s.summary} for s in scans]


def _staged(scan: ScrapeScan) -> None:
    """Staged files exist only until Save; after that they are gone."""
    if scan.status == "imported" or not store.scan_dir(scan.key).exists():
        raise ScanConflict(SAVED_MESSAGE)


def table_rows(db: Session, scan_id: str, finding_id: str, limit: int, offset: int) -> dict:
    scan = get_scan(db, scan_id)
    finding = get_finding(db, scan, finding_id)
    if finding.kind not in TABLE_KINDS:
        raise ScanRequestError("Only tables have rows.")
    _staged(scan)
    df = store.read_table(scan.key, finding.storage_path)
    part = df.iloc[offset: offset + limit]
    return {"total": int(len(df)), "columns": [str(c) for c in df.columns],
            "rows": json_safe_records(part)}


def download(db: Session, scan_id: str, finding_id: str) -> tuple[bytes, str, str]:
    """(body, filename, media type) for a table (CSV), a file (as fetched) or text."""
    scan = get_scan(db, scan_id)
    finding = get_finding(db, scan, finding_id)
    _staged(scan)
    path = store.resolve(scan.key, finding.storage_path)
    if finding.kind in TABLE_KINDS:
        csv = store.read_table(scan.key, finding.storage_path).to_csv(index=False)
        return ("﻿" + csv).encode("utf-8"), \
            safe_filename(default_name(scan, finding)) + ".csv", "text/csv; charset=utf-8"
    if finding.kind == "file":
        details = finding.details or {}
        name = safe_filename(details.get("name") or "download")
        return path.read_bytes(), name, details.get("content_type") or "application/octet-stream"
    return path.read_bytes(), safe_filename(finding.title) + ".txt", "text/plain; charset=utf-8"


def delete_scan(db: Session, scan_id: str) -> None:
    """Discard a search the admin does not want. A saved scan is provenance."""
    scan = get_scan(db, scan_id)
    if scan.status in ("running", "importing", "imported"):
        raise ScanConflict(f"This search is {scan.status} and can't be deleted.")
    if has_saved_tables(db, scan):
        raise ScanConflict("Some of this search's tables were saved, so it is kept as the "
                           "record of where they came from.")
    key = scan.key
    _delete_scan_rows(db, scan)
    db.commit()
    store.delete_scan_dir(key)
