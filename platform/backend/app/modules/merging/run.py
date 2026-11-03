"""Execute a merge request.

Lives apart from the router because a merge runs too long to answer inside an
HTTP request: it reads whole master layers into pandas and joins them, which
on production-sized data takes minutes and had the proxy returning 503 before
the app ever replied. The endpoint enqueues a job and this is what the worker
runs.
"""
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.crud import dataset as dataset_crud
from app.models import Dataset
from app.modules.census_warehouse import schema as census_schema
from app.modules.cleaning.service import WAREHOUSE_OPERATIONS, clean_dataframe
from app.modules.merging import survey
from app.modules.merging.service import MergeError, merge_dataframes


class MergeRequestError(Exception):
    """A request that cannot be satisfied — reported back as the job's error."""


def run_clean(
    db: Session, request: dict, progress_callback: Callable[[str], None] | None = None
) -> tuple[Dataset, dict]:
    """Re-clean dashboard_master in place. Returns (dataset, summary).

    A merge already cleans what it writes, so this is for re-running cleaning
    after the operation list changes, or after rows were loaded by something
    other than a merge. It rewrites the same rows it read — scoped to one
    survey when given — so the other surveys are untouched.
    """
    def report_progress(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    survey_id = request.get("survey_id")
    operations = request.get("operations") or list(WAREHOUSE_OPERATIONS)

    report_progress(f"loading {survey.DASHBOARD_TABLE}")
    try:
        df = survey.load_dashboard(db, survey_id)
    except survey.SurveyMergeError as exc:
        raise MergeRequestError(str(exc)) from exc

    before = df.shape
    # The bookkeeping columns are dropped before cleaning and re-stamped on
    # write. Left in, `standardize_columns` strips their leading underscore
    # (`_dataset_id` -> `dataset_id`), so the write adds a fresh `_dataset_id`
    # beside the renamed copy and the table gains two dead columns per clean.
    df = df.drop(columns=list(survey.META_COLUMNS), errors="ignore")

    report_progress(f"cleaning {before[0]:,} row(s)")
    try:
        df, report = clean_dataframe(df, operations)
    except ValueError as exc:
        # An unknown operation name — a request problem, not a failure.
        raise MergeRequestError(str(exc)) from exc

    # The frame was read from the table it is about to replace, so the delete
    # has to happen after a successful clean, never before: a clean that raises
    # would otherwise have already destroyed the rows.
    report_progress("rewriting")
    cleared = survey.clear_dashboard(db, survey_id)
    report.append(f"Rewrote {cleared:,} {survey.DASHBOARD_TABLE} row(s).")

    dataset = dataset_crud.persist_dataframe(
        db,
        df,
        name=request.get("name") or (
            f"Dashboard master cleaned - survey {survey_id}" if survey_id is not None
            else "Dashboard master cleaned - all surveys"
        ),
        stage="master",
        connector=survey.CLEAN_CONNECTOR,
        source_ref=None,
        parent_ids=[],
        table_name=survey.DASHBOARD_TABLE,
        survey_id=survey_id,
    )
    dataset.notes = " | ".join(report)
    db.add(dataset)
    db.commit()
    db.refresh(dataset)

    return dataset, {
        "dataset_id": dataset.id,
        "table": dataset.table_name,
        "survey_id": survey_id,
        "operations": operations,
        "rows_before": before[0],
        "rows_after": dataset.row_count,
        "columns_before": before[1],
        "columns_after": dataset.column_count,
        "steps": report,
    }


def run_merge(
    db: Session, request: dict, progress_callback: Callable[[str], None] | None = None
) -> tuple[Dataset, dict]:
    """Merge, clean, and persist. Returns (dataset, summary)."""
    def report_progress(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    report: list[str] = []
    survey_id = request.get("survey_id")
    join_key = request.get("join_key")

    if survey_id:
        chosen = request.get("connectors") or survey.available_connectors(db, survey_id)
        unknown = [c for c in chosen if c not in survey.WAREHOUSES]
        if unknown:
            raise MergeRequestError(
                f"Unknown connector(s): {', '.join(unknown)}. "
                f"Available: {', '.join(survey.WAREHOUSES)}"
            )
        if len(chosen) < 2:
            raise MergeRequestError(
                f"Survey {survey_id} has master data in {chosen or 'no'} warehouse(s); "
                "merging needs at least two. Fetch the other source under the same "
                "survey_id and build its master."
            )

        frames = []
        for index, connector in enumerate(chosen, start=1):
            report_progress(f"[{index}/{len(chosen)}] loading {connector} master")
            try:
                frames.append(survey.load_master(db, connector, survey_id))
            except survey.SurveyMergeError as exc:
                raise MergeRequestError(str(exc)) from exc
        names = list(chosen)
        parents: list[str] = []
        report.append(f"Survey {survey_id}: merged master layers of {', '.join(chosen)}.")
    else:
        dataset_ids = request.get("dataset_ids") or []
        datasets = [dataset_crud.get_dataset_or_404(db, did) for did in dataset_ids]
        frames = [dataset_crud.load_dataframe(d) for d in datasets]
        names = [d.name for d in datasets]
        parents = list(dataset_ids)

    report_progress(f"joining {len(frames)} source(s)")
    try:
        master_df, merge_report = merge_dataframes(frames, names, join_key=join_key)
    except MergeError as exc:
        raise MergeRequestError(str(exc)) from exc
    report += merge_report

    # Clean after merging, not before: the join introduces its own problems —
    # suffixed duplicate columns, nulls where a source had no row for a place.
    report_progress("cleaning merged frame")
    master_df, clean_report = clean_dataframe(master_df, list(WAREHOUSE_OPERATIONS))
    report += clean_report

    table = survey.DASHBOARD_TABLE if survey_id else None
    if survey_id:
        master_df[census_schema.SURVEY_COL] = survey_id
        cleared = survey.replace_survey(db, survey_id)
        if cleared:
            report.append(f"Replaced {cleared:,} existing {table} row(s) for this survey.")

    report_progress("writing")
    dataset = dataset_crud.persist_dataframe(
        db,
        master_df,
        name=request.get("name") or (
            f"Dashboard master - survey {survey_id}" if survey_id
            else f"Master ({' + '.join(names)})"
        ),
        stage="master",
        connector=survey.MERGE_CONNECTOR,
        source_ref=None,
        parent_ids=parents,
        table_name=table,
        survey_id=survey_id,
    )
    dataset.notes = " | ".join(report)
    db.add(dataset)
    db.commit()
    db.refresh(dataset)

    return dataset, {
        "dataset_id": dataset.id,
        "table": dataset.table_name,
        "rows": dataset.row_count,
        "columns": dataset.column_count,
        "steps": report,
    }
