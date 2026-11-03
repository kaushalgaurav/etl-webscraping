"""Data merging endpoints: suggest a join, execute it, then clean and list it."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.crud import warehouse_rows
from app.db.session import get_db
from app.models import Dataset
from app.modules.merging import survey
from app.modules.merging.schemas import DashboardCleanRequest, MergeRequest
from app.modules.jobs import service as jobs_service
from app.modules.jobs.schemas import JobOut

router = APIRouter(prefix="/datasets", tags=["merging"])


@router.post("/merge", response_model=JobOut, status_code=202)
def merge_datasets(body: MergeRequest, db: Session = Depends(get_db)):
    """Merge a survey's sources, clean the result, and write dashboard_master.

    Runs as a background job; returns a job id immediately. Poll
    `GET /jobs/{job_id}` until `status` is `succeeded`, then read the result
    with `GET /warehouse/census/rows?layer=dashboard_master` or the dataset id
    in the job's result. A merge reads whole master layers into pandas, which
    outlives a proxy timeout on real data — inline it returned 503 before the
    app had answered.

    With `survey_id`, each named connector's **master** layer is loaded for
    that survey and joined on geography — census beside HMIS, so a dashboard
    has a denominator next to a measure. raw and clean are never used: they
    hold one row per pull, so joining them would multiply rows. The merged
    frame is cleaned and written to `dashboard_master`, replacing only that
    survey's rows. `dataset_ids` still merges two specific datasets.
    """
    return jobs_service.enqueue(db, "merge", payload=body.model_dump(mode="json"))


# Query parameters the row readers own; anything else in the query string is
# treated as a filter on a real column.
_RESERVED_PARAMS = {"limit", "offset", "count", "order_by", "survey_id"}


def _dashboard_data(db: Session, connector: str, request: Request,
                    survey_id: int | None, limit: int, offset: int,
                    count: bool, order_by: list[str] | None) -> dict:
    """Rows of dashboard_master plus the runs of one kind that wrote them.

    Both a merge and a re-clean write this one table, replacing the survey's
    rows, so the rows themselves are whatever ran last. `runs` is what
    separates the two views: it lists only this kind of run, so an empty
    `runs` with rows present means the other kind produced what is there.
    """
    runs = (
        db.query(Dataset)
        .filter(Dataset.connector == connector,
                Dataset.table_name == survey.DASHBOARD_TABLE)
        .order_by(Dataset.created_at.desc())
    )
    if survey_id is not None:
        runs = runs.filter(Dataset.survey_id == survey_id)
    runs = runs.limit(20).all()

    filters: dict = {
        k: v for k, v in request.query_params.items() if k not in _RESERVED_PARAMS
    }
    if survey_id is not None:
        # Passed as an int so it compares against the numeric column, not as text.
        filters[survey.SURVEY_COL] = survey_id

    try:
        page = warehouse_rows.read_rows(
            db, survey.DASHBOARD_TABLE, allowed_layers=survey.LAYERS,
            dataset_col=survey.DATASET_COL, limit=limit, offset=offset,
            filters=filters, count=count, order_by=order_by,
        )
    except warehouse_rows.LayerReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "survey_id": survey_id,
        "runs": [
            {"dataset_id": d.id, "name": d.name, "survey_id": d.survey_id,
             "rows": d.row_count, "columns": d.column_count,
             "created_at": d.created_at, "notes": d.notes}
            for d in runs
        ],
        **page,
    }


OrderByParam = Query(
    default=None,
    description="Columns to sort by. Paging is only reproducible with a stable "
                "sort; omit it and the table's leading columns are used.",
)


@router.get("/merge")
def read_merge_data(
    request: Request,
    survey_id: int | None = Query(default=None, description="Only this survey's rows."),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    count: bool = Query(default=True, description="Set false to skip COUNT(*)."),
    order_by: list[str] | None = OrderByParam,
    db: Session = Depends(get_db),
) -> dict:
    """What `POST /datasets/merge` produced — `dashboard_master` rows for a survey.

    Pass `survey_id` to scope to one survey, or omit it for every survey, and
    filter on any real column by adding it as a query parameter, e.g.
    `?unique_id=353` or `?level_census=district`. An unknown column name is
    rejected rather than passed through to SQL. Paged in SQL, so previewing a
    large table costs nothing. `runs` lists the merge runs behind those rows.
    """
    return _dashboard_data(db, survey.MERGE_CONNECTOR, request, survey_id,
                           limit, offset, count, order_by)


@router.get("/merge/clean")
def read_merge_clean_data(
    request: Request,
    survey_id: int | None = Query(default=None, description="Only this survey's rows."),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    count: bool = Query(default=True, description="Set false to skip COUNT(*)."),
    order_by: list[str] | None = OrderByParam,
    db: Session = Depends(get_db),
) -> dict:
    """What `POST /datasets/merge/clean` produced — the same table after re-cleaning.

    A clean rewrites the survey's rows in place rather than writing a second
    table, so these are the same rows `GET /datasets/merge` returns. The
    difference is `runs`: empty here means no re-clean has run for that survey
    and what you are seeing came straight from the merge. Takes the same
    `survey_id`, column filters, paging and `order_by`.
    """
    return _dashboard_data(db, survey.CLEAN_CONNECTOR, request, survey_id,
                           limit, offset, count, order_by)


@router.post("/merge/clean", response_model=JobOut, status_code=202)
def clean_dashboard(body: DashboardCleanRequest, db: Session = Depends(get_db)):
    """Re-clean the rows already in `dashboard_master`.

    A merge cleans what it writes, so this is for re-running cleaning on its
    own — after changing the operation list, or on rows loaded by something
    other than a merge. The same rows are rewritten in place; with `survey_id`
    only that survey is touched, and the others are left alone.

    A background job for the same reason the merge is one: it reads the whole
    table into pandas, which outlives a proxy timeout on real data. Poll
    `GET /jobs/{job_id}` and read the result with `GET /datasets/merge/clean`.
    """
    return jobs_service.enqueue(db, "merge_clean", payload=body.model_dump(mode="json"))
