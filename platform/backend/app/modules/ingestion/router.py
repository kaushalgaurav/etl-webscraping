"""Ingestion endpoints: source detection, URL ingest, file upload."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.modules.ingestion import service
from app.modules.ingestion.connectors import CONNECTORS, census_india
from app.modules.ingestion.connectors.base import ConnectorError
from app.modules.ingestion.detector import probe_url
from app.modules.ingestion.schemas import FetchRequest, IngestUrlRequest, ProbeRequest
from app.modules.jobs import service as jobs_service
from app.modules.jobs.schemas import JobOut
from app.schemas import DatasetOut

router = APIRouter(tags=["ingestion"])


@router.get("/connectors")
def list_connectors() -> dict:
    return CONNECTORS


@router.post("/ingest/probe")
def probe(body: ProbeRequest) -> dict:
    """Report what acquisition methods a URL supports, and which is preferred.

    Follows the priority ladder: official API > downloadable CSV/Excel >
    JSON/XML > HTML table > headless browser.
    """
    return probe_url(body.url)


@router.post("/ingest/fetch", response_model=JobOut, status_code=202)
def ingest_fetch(body: FetchRequest, db: Session = Depends(get_db)):
    """Fetch data by source type: `api`, `csv` or `html`.

    Runs as a background job; returns a job id immediately. Poll
    `GET /jobs/{job_id}` until `status` is `succeeded`, then read the data with
    `GET /datasets/{dataset_id}/rows` or download `GET /datasets/{dataset_id}/export/csv`.

    For the Census of India API (`type=api`, url `https://censusindia.gov.in/nada/index.php/api/`)
    pick `level` = district | subdistrict | village_town | ward. Leave `state` empty
    for All India; the pull is split per state/district and saved as it goes.

    For HMIS Standard Reports (`type=api`, url `https://hmis.mohfw.gov.in/`) use `path`
    to name a folder in the Standard Reports tree. Leave `path` empty to crawl the whole
    tree: this returns a *planning* job that queues one child job per branch — grouped by
    `batch_by` = year | report_type | period — and succeeds as soon as the children are
    queued, so its own success means "work scheduled", not "data arrived". Watch the
    children in `GET /jobs`. Branches already ingested are skipped, so a later run picks
    up only new years; pass `refresh=true` to re-fetch them. `crawl=true` plans a named
    `path` the same way. Narrow a run with `file_pattern` (e.g. `ALL.xls` for just the
    combined report in each folder) and `max_files_per_chunk` for a trial.

    Note HMIS values are cumulative year-to-date — MonthUpToJune, UpToSeptember,
    UpToDecember, UpToMarch — so filter to one `period` when totalling; summing them
    counts the year several times over.
    """
    if body.district and not body.state:
        raise HTTPException(status_code=400, detail="'district' requires 'state' (district codes repeat across states).")
    payload = body.model_dump(mode="json")
    # An HMIS URL with no explicit path means "crawl it": plan the tree and
    # queue a job per branch, rather than one job over 100k files. `crawl`
    # asks for the same treatment of a subtree the caller did name.
    if "hmis.mohfw.gov.in" in (body.url or "") and (not body.path or body.crawl):
        return jobs_service.enqueue(db, "hmis_crawl", payload=payload)
    return jobs_service.enqueue(db, "fetch", payload=payload)


@router.get("/census/districts", tags=["census"])
def census_districts(state: int | None = None):
    """State and district codes for the Census API (one lightweight call)."""
    try:
        return census_india.list_districts(state)
    except ConnectorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/census/geosearch", tags=["census"])
def census_geosearch(areaname: str, limit: int = 50):
    """Find Census area codes (state/district/sub-district/village) by name."""
    try:
        return census_india.geosearch(areaname, limit=limit)
    except ConnectorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/ingest/url", response_model=DatasetOut)
def ingest_url(body: IngestUrlRequest, db: Session = Depends(get_db)):
    try:
        result = service.ingest_from_url(body.url, connector_override=body.connector)
    except ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.store_result(db, result, body.name)


@router.post("/ingest/upload", response_model=DatasetOut)
async def ingest_upload(
    file: UploadFile = File(...),
    connector: str | None = Form(default=None),
    name: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    raw = await file.read()
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail=f"File exceeds {settings.max_upload_mb}MB limit."
        )
    try:
        result = service.ingest_from_upload(file.filename, raw, connector_override=connector)
    except ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.store_result(db, result, name)
