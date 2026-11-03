"""SOURCE REGISTRY: run many different sources through one pipeline.

Data for a report template never comes from one place — Census, NFHS, HMIS,
TB Report, NACO, NCRB, RHS each publish separately, via different URLs, APIs
and formats. This module turns "a source" into a row you can register once
and re-run: connector + config + geography + licence.

Running a source ingests it (raw) and immediately cleans it, so every source
lands in a comparable, mergeable state.
"""
import json
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crud import dataset as dataset_crud
from app.modules.cleaning import service as cleaning
from pathlib import Path
from app.modules.ingestion.connectors import PARAMETERIZED_CONNECTORS
from app.modules.ingestion.service import ingest_from_url, ingest_with_params
from app.modules.ingestion.connectors.base import ConnectorError
from app.models import Dataset, Source

SEED_FILE = Path(__file__).resolve().parent / "seed_sources.json"


def seed_sources(db: Session) -> int:
    """Load the version-controlled seed registry. Existing names are skipped,
    so this is safe to re-run and won't clobber edits made through the API."""
    if not SEED_FILE.exists():
        return 0

    entries = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    existing = {name for (name,) in db.execute(select(Source.name)).all()}

    added = 0
    for entry in entries:
        if entry["name"] in existing:
            continue
        db.add(Source(**entry))
        added += 1
    if added:
        db.commit()
    return added


def run_source(
    db: Session,
    source: Source,
    auto_clean: bool = True,
    progress_callback: Callable[[str], None] | None = None,
) -> Dataset:
    """Ingest one registered source, then (by default) clean it.

    Failures are recorded on the source row rather than only raised, so a
    batch run can continue and the UI can show which sources are broken.
    `progress_callback` is forwarded to connectors that report page-by-page
    progress, so long fetches aren't opaque.
    """
    def report(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    try:
        if source.connector in PARAMETERIZED_CONNECTORS:
            config = dict(source.config or {})
            if progress_callback:
                config["progress_callback"] = progress_callback
            result = ingest_with_params(source.connector, config)
        else:
            url = (source.config or {}).get("url")
            if not url:
                raise ConnectorError(f"Source '{source.name}' has no 'url' in its config.")
            result = ingest_from_url(url, connector_override=source.connector)
    except ConnectorError as exc:
        source.last_run_at = datetime.now(timezone.utc)
        source.last_status = "error"
        source.last_error = str(exc)
        db.add(source)
        db.commit()
        raise

    report("Storing raw data")
    dataset = dataset_crud.persist_dataframe(
        db,
        result.dataframe,
        name=source.name,
        stage="raw",
        connector=result.connector,
        source_ref=result.source_ref,
    )
    if result.warnings:
        dataset.notes = " | ".join(result.warnings)
        db.add(dataset)

    final = dataset
    if auto_clean:
        report("Cleaning")
        cleaned_df, clean_report = cleaning.clean_dataframe(dataset_crud.load_dataframe(dataset))
        final = dataset_crud.persist_dataframe(
            db,
            cleaned_df,
            name=f"{source.name} (cleaned)",
            stage="clean",
            connector=result.connector,
            source_ref=result.source_ref,
            parent_ids=[dataset.id],
        )
        final.notes = " | ".join(clean_report)
        db.add(final)

    source.last_run_at = datetime.now(timezone.utc)
    source.last_status = "ok"
    source.last_error = None
    source.last_dataset_id = final.id
    db.add(source)
    db.commit()
    db.refresh(final)
    return final


def run_all(
    db: Session,
    theme: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict:
    """Run every enabled source (optionally one theme), collecting failures."""
    stmt = select(Source).where(Source.enabled.is_(True))
    if theme:
        stmt = stmt.where(Source.theme == theme)
    sources = db.execute(stmt).scalars().all()

    succeeded, failed = [], []
    for index, source in enumerate(sources, start=1):
        if progress_callback:
            progress_callback(f"[{index}/{len(sources)}] {source.name}")
        try:
            dataset = run_source(db, source, progress_callback=progress_callback)
            succeeded.append({
                "source": source.name,
                "dataset_id": dataset.id,
                "rows": dataset.row_count,
                "columns": dataset.column_count,
            })
        except Exception as exc:  # keep going; one bad source shouldn't stop a batch
            failed.append({"source": source.name, "error": str(exc)})

    return {
        "total": len(sources),
        "succeeded": succeeded,
        "failed": failed,
    }
