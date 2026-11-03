"""Background job runner.

Ingesting a national dataset takes minutes (the Census pull is ~33 paginated
calls), so endpoints enqueue a Job and return its id immediately rather than
holding an HTTP connection open.

Execution uses a small thread pool — the work is I/O-bound (HTTP + SQLite),
and capping workers also avoids hammering government APIs with parallel
requests. Each job opens its own DB session; request-scoped sessions are gone
by the time a job runs.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Dataset, Job, Source

# 2 workers: enough to overlap a slow fetch with a quick one, low enough to
# stay polite to the source portals.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _update(db: Session, job: Job, **fields) -> None:
    for key, value in fields.items():
        setattr(job, key, value)
    db.add(job)
    db.commit()


def reap_orphaned_jobs(db: Session) -> int:
    """Fail any job left 'running' by a previous process.

    The worker pool lives in-process, so a restart kills its jobs — but their
    rows still say running, and they'd stay that way forever. Called on
    startup so job status reflects reality.
    """
    orphans = db.query(Job).filter(Job.status.in_(("running", "queued"))).all()
    for job in orphans:
        job.status = "failed"
        job.error = "Interrupted: the server restarted while this job was running."
        job.finished_at = _now()
        db.add(job)
    if orphans:
        db.commit()
    return len(orphans)


def enqueue(
    db: Session, job_type: str, source_id: str | None = None, payload: dict | None = None
) -> Job:
    """Create a queued Job row and hand it to the worker pool.

    `payload` carries request parameters for jobs not tied to a registered
    source (e.g. POST /ingest/fetch). It is echoed into `result.request` so
    the job shows what it was asked to do.
    """
    job = Job(
        job_type=job_type, source_id=source_id, status="queued",
        result={"request": payload} if payload else None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    _executor.submit(_run, job.id, payload)
    return job


def _run(job_id: str, payload: dict | None = None) -> None:
    """Worker entry point. Owns its own session and never raises."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        _update(db, job, status="running", started_at=_now())

        try:
            if job.job_type == "source_run":
                _run_source_job(db, job)
            elif job.job_type == "source_run_all":
                _run_all_job(db, job)
            elif job.job_type == "fetch":
                _run_fetch_job(db, job, payload or {})
            elif job.job_type == "merge":
                _run_merge_job(db, job, payload or {})
            elif job.job_type == "merge_clean":
                _run_merge_clean_job(db, job, payload or {})
            elif job.job_type == "hmis_crawl":
                _run_hmis_crawl_job(db, job, payload or {})
            else:
                raise ValueError(f"Unknown job type '{job.job_type}'")
        except Exception as exc:
            _update(db, job, status="failed", error=str(exc), finished_at=_now())
    finally:
        db.close()


def _run_source_job(db: Session, job: Job) -> None:
    # Imported here to avoid a circular import (sources -> jobs -> sources).
    from app.modules.sources import service as sources_service

    source = db.get(Source, job.source_id)
    if source is None:
        raise ValueError(f"Source '{job.source_id}' not found.")

    def progress(message: str) -> None:
        with _lock:
            _update(db, job, progress=message)

    _update(db, job, progress=f"Starting '{source.name}'")
    dataset = sources_service.run_source(db, source, progress_callback=progress)
    _update(
        db, job,
        status="succeeded",
        dataset_id=dataset.id,
        progress=f"Done: {dataset.row_count} rows x {dataset.column_count} columns",
        result={
            "dataset_id": dataset.id,
            "rows": dataset.row_count,
            "columns": dataset.column_count,
        },
        finished_at=_now(),
    )


def _run_all_job(db: Session, job: Job) -> None:
    from app.modules.sources import service as sources_service

    def progress(message: str) -> None:
        with _lock:
            _update(db, job, progress=message)

    summary = sources_service.run_all(db, progress_callback=progress)
    _update(
        db, job,
        status="succeeded",
        result=summary,
        progress=f"{len(summary['succeeded'])} succeeded, {len(summary['failed'])} failed",
        finished_at=_now(),
    )


def _run_hmis_crawl_job(db: Session, job: Job, payload: dict) -> None:
    """Plan an HMIS subtree and enqueue one fetch job per branch.

    Lets a caller pass only the HMIS URL: this job does the discovery, names
    each branch after its own place in the tree, and queues the children. It
    finishes as soon as they are queued — the pool runs them, so one slow
    branch never blocks the rest, and each can be retried on its own.
    """
    from app.crud import dataset as dataset_crud
    from app.modules.hmis_warehouse import schema as hmis_schema
    from app.modules.ingestion.connectors import hmis_reports

    path = payload.get("path") or hmis_reports.ROOT
    batch_by = payload.get("batch_by") or "year"
    # Absent from the payload means "use the default scope" (F.1, latest 2
    # years); an explicit null means "no limit". `payload.get` cannot tell
    # those apart, so the keys are checked for presence.
    report_type = (
        payload["report_type"] if "report_type" in payload
        else hmis_reports.DEFAULT_REPORT_TYPE
    )
    last_years = (
        payload["last_years"] if "last_years" in payload
        else hmis_reports.DEFAULT_LAST_YEARS
    )
    scope = ", ".join(
        filter(None, [
            report_type.split("~", 1)[-1][:40] if report_type else "all report types",
            f"latest {last_years} year(s)" if last_years else "all years",
        ])
    )
    _update(db, job, progress=f"Planning '{path}' by {batch_by} ({scope})")

    branches = hmis_reports.plan_branches(
        path, batch_by, report_type=report_type, last_years=last_years
    )
    if not branches:
        raise ValueError(f"No crawlable branches found under '{path}'.")

    # A branch already ingested is skipped, so a later run picks up only the
    # years that have appeared since. `source_ref` is derived from the branch
    # path, which makes it the stable identity to match on — the dataset name
    # is not, because a caller can override it.
    survey_id = payload.get("survey_id")
    skipped: list[dict] = []
    if not payload.get("refresh"):
        # Completion is judged by the job, not the dataset. A crawl killed
        # mid-run (a restart, a crash) leaves a dataset with rows and no
        # error set — it never reached the code that records one — so
        # trusting the dataset alone would skip a half-ingested year forever.
        # Scoped to this survey: a year ingested under another survey_id is
        # not in this one, so it still has to be fetched.
        done = {
            row[0] for row in db.query(Dataset.source_ref)
            .join(Job, Job.dataset_id == Dataset.id)
            .filter(Dataset.connector == "hmis_reports",
                    Dataset.survey_id == survey_id,
                    Dataset.error.is_(None),
                    Dataset.row_count > 0,
                    Job.status == "succeeded")
            .all()
        }
        fresh = []
        for branch in branches:
            ref = hmis_reports.source_ref(branch["path"], payload.get("file_pattern"))
            if ref in done:
                skipped.append({"name": branch["name"], "path": branch["path"]})
            else:
                fresh.append(branch)
        branches = fresh

    if not branches:
        _update(
            db, job, status="succeeded",
            progress=f"Nothing new — {len(skipped)} branch(es) already ingested",
            result={"request": payload, "branches": 0, "children": [],
                    "skipped": len(skipped), "skipped_branches": skipped},
            finished_at=_now(),
        )
        return

    # A refresh replaces the survey, so it is cleared once here, before any
    # child runs. The children must not clear it themselves: they share the
    # survey_id and run in parallel, so each would delete its siblings' rows.
    # Without refresh the crawl is incremental — new years are added beside
    # the ones already in the survey.
    if payload.get("refresh") and survey_id is not None:
        cleared = dataset_crud.clear_survey(db, hmis_schema.RAW, survey_id)
        if cleared:
            _update(db, job, progress=f"Replacing survey '{survey_id}': removed {cleared:,} row(s)")

    children = []
    for i, branch in enumerate(branches, 1):
        child_payload = {
            "type": "api",
            "url": payload.get("url") or hmis_reports.BASE,
            "path": branch["path"],
            "name": branch["name"],
            "survey_id": survey_id,
            "replace_survey": False,
        }
        # Anything that changes *what* a branch ingests has to reach the child,
        # or the children quietly crawl something other than what was asked for.
        for key in ("file_pattern", "max_files_per_chunk", "refresh"):
            if payload.get(key):
                child_payload[key] = payload[key]
        child = enqueue(db, "fetch", payload=child_payload)
        children.append({"job_id": child.id, "name": branch["name"], "path": branch["path"]})
        _update(db, job, progress=f"Queued {i}/{len(branches)}: {branch['label']}")

    _update(
        db, job,
        status="succeeded",
        progress=f"Queued {len(children)} new branch job(s)"
                 + (f", skipped {len(skipped)} already ingested" if skipped else ""),
        result={"request": payload, "branches": len(children), "children": children,
                "skipped": len(skipped), "skipped_branches": skipped},
        finished_at=_now(),
    )


def _run_fetch_job(db: Session, job: Job, payload: dict) -> None:
    from app.modules.ingestion import service as ingestion_service

    def progress(message: str) -> None:
        with _lock:
            _update(db, job, progress=message)

    dataset, summary = ingestion_service.run_fetch(db, payload, progress_callback=progress)
    failed = summary.get("failed_chunks") or []
    _update(
        db, job,
        status="succeeded",
        dataset_id=dataset.id,
        progress=f"Done: {dataset.row_count} rows x {dataset.column_count} columns"
                 + (f", {len(failed)} chunk(s) failed" if failed else ""),
        result={"request": payload, **summary},
        finished_at=_now(),
    )


def _run_merge_job(db: Session, job: Job, payload: dict) -> None:
    """Merge a survey's sources into dashboard_master.

    A job rather than an inline response: the merge reads whole master layers
    into pandas, which outlives any sensible proxy timeout on real data.
    """
    from app.modules.merging.run import MergeRequestError, run_merge

    def progress(message: str) -> None:
        with _lock:
            _update(db, job, progress=message)

    try:
        dataset, summary = run_merge(db, payload, progress_callback=progress)
    except MergeRequestError as exc:
        # A request that cannot be satisfied — a bad connector, a survey with
        # only one source. Recorded as the job's error rather than raised, so
        # the caller reads the reason instead of a bare failure.
        _update(db, job, status="failed", error=str(exc),
                progress="Nothing merged", result={"request": payload},
                finished_at=_now())
        return

    _update(
        db, job, status="succeeded", dataset_id=dataset.id,
        progress=f"Done: {dataset.row_count:,} rows x {dataset.column_count} columns",
        result={"request": payload, **summary}, finished_at=_now(),
    )


def _run_merge_clean_job(db: Session, job: Job, payload: dict) -> None:
    """Re-clean dashboard_master. A job for the same reason the merge is one."""
    from app.modules.merging.run import MergeRequestError, run_clean

    def progress(message: str) -> None:
        with _lock:
            _update(db, job, progress=message)

    try:
        dataset, summary = run_clean(db, payload, progress_callback=progress)
    except MergeRequestError as exc:
        _update(db, job, status="failed", error=str(exc),
                progress="Nothing cleaned", result={"request": payload},
                finished_at=_now())
        return

    _update(
        db, job, status="succeeded", dataset_id=dataset.id,
        progress=f"Done: {dataset.row_count:,} rows x {dataset.column_count} columns",
        result={"request": payload, **summary}, finished_at=_now(),
    )


def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)
