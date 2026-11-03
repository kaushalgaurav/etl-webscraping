"""Source registry endpoints.

Runs are enqueued as background jobs — a national pull takes minutes and must
not block the request.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Source
from app.modules.ingestion.connectors import CONNECTORS
from app.modules.jobs import service as jobs_service
from app.modules.jobs.schemas import JobOut
from app.modules.sources import service
from app.modules.sources.schemas import SourceIn, SourceOut

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=list[SourceOut])
def list_sources(theme: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Source).order_by(Source.created_at)
    if theme:
        stmt = stmt.where(Source.theme == theme)
    return db.execute(stmt).scalars().all()


@router.post("", response_model=SourceOut)
def create_source(body: SourceIn, db: Session = Depends(get_db)):
    if body.connector not in CONNECTORS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown connector '{body.connector}'. Available: {', '.join(CONNECTORS)}",
        )
    source = Source(**body.model_dump())
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@router.post("/seed")
def seed_registry(db: Session = Depends(get_db)):
    """Load the version-controlled seed registry (skips names already present)."""
    return {"added": service.seed_sources(db)}


@router.post("/run-all", response_model=JobOut, status_code=202)
def run_all_sources(db: Session = Depends(get_db)):
    """Enqueue a run of every enabled source. Failures are collected, not fatal."""
    return jobs_service.enqueue(db, "source_run_all")


@router.post("/{source_id}/run", response_model=JobOut, status_code=202)
def run_source(source_id: str, db: Session = Depends(get_db)):
    """Enqueue an ingest. Returns a job id immediately — poll GET /jobs/{id}."""
    if db.get(Source, source_id) is None:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found.")
    return jobs_service.enqueue(db, "source_run", source_id=source_id)
