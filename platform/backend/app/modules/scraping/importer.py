"""Save: turn the admin's ticked tables into ordinary datasets.

Each ticked table becomes a raw dataset in its own ds_<id> table plus a
cleaned copy, through the same store_result / store_clean_copy path an
upload or a CSV link takes, with the survey stamped on both. Then everything
not ticked is deleted: the staging folder and the unpicked finding rows. The
scan row and the picked findings stay as provenance.

One Save is one claim: claim_for_import flips the scan from done to
importing in a single conditional UPDATE, so a double click gets a 409
instead of a second set of datasets.
"""
from collections.abc import Callable

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import ScrapeFinding, ScrapeScan
from app.modules.ingestion import service as ingestion_service
from app.modules.ingestion.connectors.base import IngestResult
from app.modules.scraping import store
from app.modules.scraping.service import (
    TABLE_KINDS,
    ScanConflict,
    ScanRequestError,
    default_name,
    get_scan,
)

# datasets.name is VARCHAR(255) and the clean copy adds " (cleaned)".
MAX_NAME = 240

CONNECTOR_FOR_KIND = {
    "html_table": "web_table",
    "rendered_table": "web_table",
    "feed": "web_feed",
    "embedded": "web_table",
    "file_table": "web_file",
    "pdf_table": "web_file",
    "docx_table": "web_file",
}


def claim_for_import(db: Session, scan_id: str, items: list[dict]) -> None:
    scan = get_scan(db, scan_id)
    if not items:
        raise ScanRequestError("Pick at least one table to save.")
    ids = [item["finding_id"] for item in items]
    if len(set(ids)) != len(ids):
        raise ScanRequestError("The same table was picked twice.")
    for finding_id in ids:
        finding = db.get(ScrapeFinding, finding_id)
        if finding is None or finding.scan_id != scan.id:
            raise ScanRequestError(f"Finding '{finding_id}' is not part of scan '{scan.id}'.")
        if finding.kind not in TABLE_KINDS:
            raise ScanRequestError(f"Finding '{finding_id}' is not a table; only tables can be saved.")
        if finding.imported_dataset_id:
            raise ScanConflict(
                f"Finding '{finding_id}' was already saved as dataset '{finding.imported_dataset_id}'.")
    claimed = db.execute(
        update(ScrapeScan)
        .where(ScrapeScan.id == scan.id, ScrapeScan.status == "done")
        .values(status="importing")
    ).rowcount
    db.commit()
    if claimed != 1:
        db.refresh(scan)
        raise ScanConflict(f"This search is {scan.status}; it can be saved only once it is done.")


def _source_ref(scan: ScrapeScan, finding: ScrapeFinding) -> str:
    locator = finding.locator or {}
    base = locator.get("file_url") or locator.get("feed_url") or scan.final_url or scan.url
    extra = ", ".join(f"{k}={v}" for k, v in locator.items()
                      if k not in ("source", "file_url", "feed_url"))
    return f"{base} [{extra}]" if extra else base


def run_import(db: Session, scan_id: str, survey_id: int, items: list[dict],
               progress: Callable[[str], None] | None = None) -> dict:
    say = progress or (lambda _message: None)
    scan = get_scan(db, scan_id)
    saved = []
    try:
        for i, item in enumerate(items, 1):
            finding = db.get(ScrapeFinding, item["finding_id"])
            name = ((item.get("name") or "").strip() or default_name(scan, finding))[:MAX_NAME]
            df = store.read_table(scan.id, finding.storage_path)
            raw = ingestion_service.store_result(db, IngestResult(
                dataframe=df,
                connector=CONNECTOR_FOR_KIND[finding.kind],
                source_ref=_source_ref(scan, finding),
            ), name)
            raw.survey_id = survey_id
            raw.notes = (f"Website scan {scan.id}, finding {finding.id} ({finding.kind}), "
                         f"{finding.rows} rows x {finding.columns} columns, "
                         f"table hash {finding.content_hash}")
            db.add(raw)
            db.commit()
            clean = ingestion_service.store_clean_copy(db, raw, name)
            clean.survey_id = survey_id
            db.add(clean)
            finding.imported_dataset_id = raw.id
            db.add(finding)
            db.commit()
            saved.append({"finding_id": finding.id, "raw_dataset_id": raw.id,
                          "dataset_id": clean.id, "rows": clean.row_count,
                          "columns": clean.column_count})
            say(f"Saved {i}/{len(items)}: {name}")
    except Exception:
        db.rollback()
        scan = get_scan(db, scan_id)
        scan.status = "done"  # what was saved stays saved; the rest can be retried
        db.commit()
        raise

    # Only what the admin picked is kept.
    (db.query(ScrapeFinding)
     .filter(ScrapeFinding.scan_id == scan.id, ScrapeFinding.imported_dataset_id.is_(None))
     .delete(synchronize_session=False))
    scan.status = "imported"
    scan.expires_at = None
    db.commit()
    store.delete_scan_dir(scan.id)
    return {"items": saved}
