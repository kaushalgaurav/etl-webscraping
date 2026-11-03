"""Ingestion service: route a source to its connector, store the result."""
import json
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud import dataset as dataset_crud
from app.helpers.dataframe import snake_case
from app.models import Dataset
from app.modules.cleaning import service as cleaning
from app.modules.ingestion import cleanup
from app.modules.ingestion.connectors import (
    census_india,
    hmis_reports,
    csv_excel,
    data_gov_catalog,
    data_gov_in,
    dynamic_site,
    html_table,
    rest_api,
)
from app.modules.ingestion.connectors.base import ConnectorError, IngestResult
from app.modules.hmis_warehouse import schema as hmis_schema
from app.modules.ingestion.detector import TABULAR_EXTS, detect_connector
from app.modules.census_warehouse import schema as census_schema


def ingest_from_url(url: str, connector_override: str | None = None) -> IngestResult:
    connector = connector_override or detect_connector(url)

    if connector == "census_india":
        raise ConnectorError(
            "censusindia.gov.in needs area codes, not just a URL. Register it as a "
            "source with config like {'state': 10, 'level': 'district'}, or call "
            "census_india.geosearch() to find the codes first."
        )
    if connector == "data_gov_in":
        return data_gov_in.ingest(url)
    if connector == "csv_excel":
        return csv_excel.ingest_url(url)
    if connector == "rest_api":
        return rest_api.ingest_url(url)
    if connector == "html_table":
        return html_table.ingest(url)
    if connector == "dynamic_site":
        return dynamic_site.ingest(url)
    raise ConnectorError(f"Unknown connector '{connector}'.")


def ingest_with_params(connector: str, config: dict) -> IngestResult:
    """Run a parameterized connector from a registry source's `config` blob."""
    if connector == "census_india":
        return census_india.ingest(**config)
    if connector == "data_gov_catalog":
        return data_gov_catalog.ingest(**config)
    raise ConnectorError(f"'{connector}' is not a parameterized connector.")


def ingest_from_upload(
    filename: str, raw: bytes, connector_override: str | None = None
) -> IngestResult:
    suffix = Path(filename).suffix.lower()
    connector = connector_override or (
        "csv_excel" if suffix in TABULAR_EXTS else "rest_api" if suffix == ".json" else None
    )
    if connector is None:
        raise ConnectorError(
            f"Cannot detect a connector for uploaded file '{filename}' "
            f"(supported: {', '.join(sorted(TABULAR_EXTS | {'.json'}))})."
        )

    if connector == "csv_excel":
        return csv_excel.ingest_upload(raw, filename)

    if connector == "rest_api":
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ConnectorError(f"'{filename}' is not valid JSON: {exc}") from exc
        records = rest_api.find_records(payload)
        df = pd.json_normalize(records if records is not None else payload)
        if df.empty:
            raise ConnectorError(f"'{filename}' produced an empty table.")
        return IngestResult(
            dataframe=df, connector="rest_api", source_ref=filename,
            raw_bytes=raw, raw_filename=filename,
        )

    raise ConnectorError(f"Unsupported connector '{connector}' for uploads.")


def store_clean_copy(db: Session, raw: Dataset, name: str) -> Dataset:
    """Clean a raw dataset and persist the result as its 'clean' child."""
    cleaned_df, clean_report = cleaning.clean_dataframe(dataset_crud.load_dataframe(raw))
    clean = dataset_crud.persist_dataframe(
        db,
        cleaned_df,
        name=f"{name} (cleaned)",
        stage="clean",
        connector=raw.connector,
        source_ref=raw.source_ref,
        parent_ids=[raw.id],
    )
    clean.notes = " | ".join(clean_report)
    db.add(clean)
    db.commit()
    db.refresh(clean)
    return clean


FETCH_TYPE_CONNECTORS = {"csv": "csv_excel", "html": "html_table"}


def _run_hmis_fetch(
    db: Session, request: dict, progress_callback: Callable[[str], None] | None = None
) -> tuple[Dataset, dict]:
    """Crawl the HMIS Standard Reports tree, appending one folder at a time.

    The tree holds thousands of reports, so the plan is built first and each
    leaf folder is persisted as it arrives — the same shape as the census
    pull, for the same reason: a failure late in a long crawl must not throw
    away what already succeeded.
    """
    path = request.get("path") or hmis_reports.ROOT
    survey_id = request["survey_id"]
    # Re-running a survey replaces it; a new survey_id adds a separate set.
    # A crawl's child jobs pass replace_survey=False: the parent has already
    # decided whether to clear, and siblings must not wipe each other's rows.
    cleared = (dataset_crud.clear_survey(db, hmis_schema.RAW, survey_id)
               if request.get("replace_survey", True) else 0)
    if cleared and progress_callback:
        progress_callback(f"Replacing survey '{survey_id}': removed {cleared:,} existing row(s)")
    if progress_callback:
        progress_callback(f"Planning crawl of '{path}'")
    plan = hmis_reports.plan_chunks(path)
    if not plan:
        raise ConnectorError(f"No folders containing .xls/.xlsx/.csv found under '{path}'.")

    dataset = dataset_crud.create_dataset_shell(
        db,
        name=request.get("name") or f"HMIS Standard Reports - {path.split('/')[-1]}",
        stage="raw",
        connector="hmis_reports",
        source_ref=hmis_reports.source_ref(path, request.get("file_pattern")),
        table_name=hmis_schema.RAW,
        survey_id=survey_id,
    )

    warnings: dict[str, str] = {}
    failed: list[dict] = []
    chunks = files = 0
    try:
        for chunk in hmis_reports.iter_chunks(
            path,
            max_files_per_chunk=request.get("max_files_per_chunk"),
            progress_callback=progress_callback,
            plan=plan,
            file_pattern=request.get("file_pattern"),
            survey_id=request.get("survey_id"),
        ):
            chunks += 1
            if chunk.error:
                failed.append({"chunk": chunk.label, "path": chunk.path, "error": chunk.error})
                continue
            for w in chunk.warnings:
                warnings.setdefault(re.sub(r"\d+", "N", w), w)
            if chunk.dataframe is not None and not chunk.dataframe.empty:
                files += chunk.files
                dataset_crud.append_dataframe(db, dataset, chunk.dataframe)
    except Exception as exc:
        dataset.error = f"Stopped after {chunks} folder(s): {exc}"
        db.add(dataset)
        db.commit()
        raise

    notes = [f"Crawled '{path}': {files} report(s) from {chunks} folder(s)."]
    notes += list(warnings.values())[:10]
    if failed:
        notes.append(f"{len(failed)} folder(s) failed: "
                     + ", ".join(f["chunk"] for f in failed[:10])
                     + (" ..." if len(failed) > 10 else ""))
    if dataset.row_count == 0:
        dataset.error = "No rows were fetched."

    # Only once the rows are safely committed, and only when nothing failed:
    # a branch that errored still has retryable files on disk, and deleting
    # them would lose both the data and the means to try again.
    purged = None
    if (settings.delete_source_files_after_ingest
            and dataset.row_count > 0 and not dataset.error and not failed):
        purged = cleanup.purge_hmis_branch(path, survey_id=request.get("survey_id"))
        if purged["removed"]:
            notes.append(cleanup.describe(purged))

    dataset.notes = " | ".join(notes)
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset, {
        "dataset_id": dataset.id, "rows": dataset.row_count,
        "columns": dataset.column_count, "folders": chunks,
        "files": files, "failed_chunks": failed,
        "source_files_removed": (purged or {}).get("removed", 0),
    }


def run_fetch(
    db: Session, request: dict, progress_callback: Callable[[str], None] | None = None
) -> tuple[Dataset, dict]:
    """Execute a POST /ingest/fetch request. Returns (final dataset, job summary)."""
    kind, url = request["type"], request["url"]
    host = (urlparse(url).hostname or "").lower()

    if host.endswith("censusindia.gov.in"):
        if kind != "api":
            raise ConnectorError(
                "censusindia.gov.in has no CSV/HTML export to read; use type='api'."
            )
        return _run_census_fetch(db, request, progress_callback)

    if host.endswith("hmis.mohfw.gov.in"):
        return _run_hmis_fetch(db, request, progress_callback)

    if kind == "api":
        connector = "data_gov_in" if "data.gov.in" in host else "rest_api"
    else:
        connector = FETCH_TYPE_CONNECTORS[kind]

    if progress_callback:
        progress_callback(f"Fetching {url} with {connector}")
    result = ingest_from_url(url, connector_override=connector)
    name = request.get("name") or result.source_ref
    raw = store_result(db, result, name)
    # These land in their own ds_<id> table, so there is nothing to replace —
    # the survey is recorded on the dataset for lineage and filtering.
    raw.survey_id = request["survey_id"]
    db.add(raw)
    db.commit()
    if progress_callback:
        progress_callback("Cleaning")
    clean = store_clean_copy(db, raw, name)
    return clean, {"raw_dataset_id": raw.id, "dataset_id": clean.id,
                   "rows": clean.row_count, "columns": clean.column_count}


def _run_census_fetch(
    db: Session, request: dict, progress_callback: Callable[[str], None] | None
) -> tuple[Dataset, dict]:
    """Stream an all-India (or one state/district) Census pull into one table.

    Each chunk is snake_cased and appended as it arrives, so memory stays flat
    however large the level. The heavy cleaning pass is skipped: API values
    are already numeric, and it would need the whole table in memory. Chunks
    that fail are listed in the summary so they can be re-fetched by code.
    """
    level = request.get("level") or "district"
    state, district = request.get("state"), request.get("district")
    if district and not state:
        raise ConnectorError("'district' codes are only unique within a state; pass 'state' too.")

    survey_id = request["survey_id"]
    # Re-running a survey replaces it; a new survey_id adds a separate set.
    cleared = dataset_crud.clear_survey(db, census_schema.RAW, survey_id)
    if cleared and progress_callback:
        progress_callback(f"Replacing survey '{survey_id}': removed {cleared:,} existing row(s)")

    scope = f"district {district}" if district else f"state {state}" if state else "All India"
    source_ref = (
        f"{census_india.API_BASE}tables/data/2011/{census_india.TABLE_ID}"
        f"?level={level}&state={state}&district={district}&urbrur={request.get('urbrur')}"
    )
    # Every census fetch lands in the shared raw layer rather than its own
    # ds_<id> table; the Dataset row stays the unit of provenance, and its rows
    # are separable because append_dataframe stamps them with the dataset id.
    dataset = dataset_crud.create_dataset_shell(
        db,
        name=request.get("name") or f"Census 2011 PCA - {scope} - {level}",
        stage="raw",
        connector="census_india",
        source_ref=source_ref,
        table_name=census_schema.RAW,
        survey_id=survey_id,
    )

    warnings: dict[str, str] = {}
    failed: list[dict] = []
    chunks = 0
    try:
        for chunk in census_india.iter_chunks(
            level=level, state=state, district=district,
            urbrur=request.get("urbrur", 0), verify_ssl=request.get("verify_ssl", False),
            progress_callback=progress_callback,
        ):
            chunks += 1
            if chunk.error:
                failed.append({"chunk": chunk.label, "state": chunk.state,
                               "district": chunk.district, "error": chunk.error})
                continue
            # Per-chunk counts would make every warning unique; keep the first
            # of each kind, and skip "no records" notes for empty areas.
            for w in chunk.warnings:
                if not w.endswith(f"no records at level '{level}'."):
                    warnings.setdefault(re.sub(r"\d+", "N", w), w)
            if chunk.dataframe is not None and not chunk.dataframe.empty:
                # The raw layer holds every level, so each row has to carry its own.
                frame = _snake_columns(chunk.dataframe)
                frame[census_schema.LEVEL_COL] = level
                dataset_crud.append_dataframe(db, dataset, frame)
    except Exception as exc:
        dataset.error = f"Stopped after {chunks} chunk(s): {exc}"
        db.add(dataset)
        db.commit()
        raise

    notes = [f"Fetched {scope} at level '{level}' in {chunks} chunk(s)."]
    notes += list(warnings.values())
    if failed:
        notes.append(
            f"{len(failed)} chunk(s) failed and are missing: "
            + ", ".join(f["chunk"] for f in failed[:20])
            + (" ..." if len(failed) > 20 else "")
        )
    if not request.get("verify_ssl", False):
        notes.append("SSL verification was disabled for censusindia.gov.in (incomplete cert chain).")
    dataset.notes = " | ".join(notes)
    if dataset.row_count == 0:
        dataset.error = "No rows were fetched."
    db.add(dataset)
    db.commit()
    db.refresh(dataset)

    if dataset.row_count == 0:
        raise ConnectorError(f"Census API returned no rows for {scope} at level '{level}'. {failed[:3]}")
    return dataset, {"dataset_id": dataset.id, "rows": dataset.row_count,
                     "columns": dataset.column_count, "chunks": chunks, "failed_chunks": failed}


def _snake_columns(df: pd.DataFrame) -> pd.DataFrame:
    """snake_case column names, same rule as the cleaning stage."""
    seen: dict[str, int] = {}
    cols = []
    for col in df.columns:
        base = snake_case(col)
        seen[base] = seen.get(base, 0) + 1
        cols.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return df.set_axis(cols, axis=1)


def store_result(db: Session, result: IngestResult, name: str | None = None) -> Dataset:
    """Persist an IngestResult as a raw dataset (+ its raw payload on disk)."""
    dataset = dataset_crud.persist_dataframe(
        db,
        result.dataframe,
        name=name or result.source_ref,
        stage="raw",
        connector=result.connector,
        source_ref=result.source_ref,
    )
    if result.raw_bytes is not None and result.raw_filename:
        if settings.delete_source_files_after_ingest:
            # The rows are committed, so the payload would be written only to
            # be deleted. Not writing it is the same outcome without the IO.
            dataset.raw_file_path = None
        else:
            # Saved after persist assigns an id, so files are namespaced by dataset.
            dataset.raw_file_path = dataset_crud.save_raw_file(
                dataset.id, result.raw_bytes, result.raw_filename
            )
    if result.warnings:
        dataset.notes = " | ".join(result.warnings)
    if result.raw_bytes is not None or result.warnings:
        db.add(dataset)
        db.commit()
        db.refresh(dataset)
    return dataset
