"""Website tab endpoints (spec §7: plain pages, files, and JavaScript pages in a browser).

  POST   /scrape/scan                                       Search: 202 + job (scrape_scan)
  GET    /scrape/scans                                      recent searches
  GET    /scrape/scans/{scan_id}                            what was found (the review screen)
  GET    /scrape/scans/{scan_id}/findings/{id}/rows         one table, paged
  GET    /scrape/scans/{scan_id}/findings/{id}/download     table as CSV, file as fetched, text
  POST   /scrape/scans/{scan_id}/import                     Save the ticked tables: 202 + job (scrape_import)
  DELETE /scrape/scans/{scan_id}                            discard an unsaved search

The job id is returned at once; `result.request.scan_id` names the scan, so
the page can poll /jobs/{id} for progress and then read the manifest.
"""
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.jobs.schemas import JobOut
from app.modules.jobs.service import enqueue
from app.modules.scraping import importer, service
from app.modules.scraping.schemas import (
    ImportRequest,
    Manifest,
    RowsPage,
    ScanListItem,
    ScanRequest,
)

router = APIRouter(prefix="/scrape", tags=["website scraping"])

_STATUS = ((service.ScanNotFound, 404), (service.ScanConflict, 409), (service.ScanRequestError, 422))
_ERRORS = tuple(cls for cls, _ in _STATUS)


def _http(exc: Exception) -> HTTPException:
    for cls, status in _STATUS:
        if isinstance(exc, cls):
            return HTTPException(status_code=status, detail=str(exc.args[0] if exc.args else exc))
    raise exc


@router.post("/scan", response_model=JobOut, status_code=202)
def start_scan(body: ScanRequest, db: Session = Depends(get_db)):
    try:
        scan = service.create_scan(db, body.url, body.include_files, body.render)
    except _ERRORS as exc:
        raise _http(exc) from exc
    job = enqueue(db, "scrape_scan", payload={
        "scan_id": scan.key, "url": scan.url, "include_files": body.include_files,
        "render": body.render})
    scan.job_id = job.id
    db.commit()
    db.refresh(job)
    return job


@router.get("/scans", response_model=list[ScanListItem])
def list_scans(limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    return service.list_scans(db, limit)


@router.get("/scans/{scan_id}", response_model=Manifest)
def get_scan(scan_id: str, db: Session = Depends(get_db)):
    try:
        return service.manifest(db, scan_id)
    except _ERRORS as exc:
        raise _http(exc) from exc


@router.get("/scans/{scan_id}/findings/{finding_id}/rows", response_model=RowsPage)
def finding_rows(scan_id: str, finding_id: str,
                 limit: int = Query(default=50, ge=1, le=1000),
                 offset: int = Query(default=0, ge=0),
                 db: Session = Depends(get_db)):
    try:
        return service.table_rows(db, scan_id, finding_id, limit, offset)
    except _ERRORS as exc:
        raise _http(exc) from exc


@router.get("/scans/{scan_id}/findings/{finding_id}/download")
def finding_download(scan_id: str, finding_id: str, db: Session = Depends(get_db)):
    try:
        body, filename, media_type = service.download(db, scan_id, finding_id)
    except _ERRORS as exc:
        raise _http(exc) from exc
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=body, media_type=media_type,
                    headers={"Content-Disposition": disposition})


@router.post("/scans/{scan_id}/import", response_model=JobOut, status_code=202)
def import_tables(scan_id: str, body: ImportRequest, db: Session = Depends(get_db)):
    items = [item.model_dump() for item in body.items]
    try:
        importer.claim_for_import(db, scan_id, items)
    except _ERRORS as exc:
        raise _http(exc) from exc
    return enqueue(db, "scrape_import", payload={
        "scan_id": scan_id, "survey_id": body.survey_id, "items": items})


@router.delete("/scans/{scan_id}", status_code=204)
def delete_scan(scan_id: str, db: Session = Depends(get_db)):
    try:
        service.delete_scan(db, scan_id)
    except _ERRORS as exc:
        raise _http(exc) from exc
    return Response(status_code=204)
