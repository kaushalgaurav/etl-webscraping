"""Warehouse endpoints for every connector: build and inspect raw/clean/master.

One set of routes, `/warehouse/{connector}/...`, instead of a copy per source.
Each connector plugs in its own service and schema module, and every service
exposes the same functions, so every route works for every connector.
"""
from enum import Enum
from types import ModuleType

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from sqlalchemy.orm import Session

from app.crud import warehouse_rows
from app.crud import warehouse_survey as survey
from app.db.session import get_db
from app.modules.census_warehouse import schema as census_schema
from app.modules.census_warehouse import service as census_service
from app.modules.hmis_warehouse import schema as hmis_schema
from app.modules.hmis_warehouse import service as hmis_service
from app.modules.jobs import lineage as job_lineage
from app.modules.web_warehouse import schema as web_schema
from app.modules.web_warehouse import service as web_service
from app.modules.web_warehouse.years import financial_years

router = APIRouter(prefix="/warehouse", tags=["warehouse"])


class Connector(str, Enum):
    census = "census"
    hmis = "hmis"
    web = "web"


# connector -> (service module, schema module)
_CONNECTORS: dict[Connector, tuple[ModuleType, ModuleType]] = {
    Connector.census: (census_service, census_schema),
    Connector.hmis: (hmis_service, hmis_schema),
    Connector.web: (web_service, web_schema),
}

ConnectorParam = Path(..., description="Which source's warehouse to act on.")
SurveyParam = Query(
    default=None,
    description="Only act on this survey's rows. Omit to act on every survey.",
)
TableParam = Query(
    default=None,
    description="web only: act on this one Website table (its web_table_id).",
)


def _table_kwargs(connector: Connector, table: str | None) -> dict:
    """`table` is passed through for web and refused for every other connector."""
    if table is None:
        return {}
    if connector is not Connector.web:
        raise HTTPException(status_code=400, detail="'table' applies only to the web connector.")
    return {"table": table}


# Static web routes first, so "/web/tables" never reaches "/{connector}/...".
@router.get("/web/tables")
def web_tables(survey_id: int = Query(..., description="The survey whose Website tables to list."),
               db: Session = Depends(get_db)) -> list[dict]:
    """A survey's saved Website tables: status, match summary, and whether Merge can use them."""
    return web_service.list_tables(db, survey_id)


@router.get("/web/years")
def web_years() -> list[str]:
    """The financial years a Website table can be saved under, in HMIS's form."""
    return financial_years()


def _service(connector: Connector) -> ModuleType:
    return _CONNECTORS[connector][0]


def _schema(connector: Connector) -> ModuleType:
    return _CONNECTORS[connector][1]


def _job_dataset_ids(db: Session, job_id: str) -> list[str]:
    try:
        ids = job_lineage.dataset_ids_for_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not ids:
        raise HTTPException(
            status_code=400,
            detail=f"Job '{job_id}' produced no dataset yet — check it has succeeded.",
        )
    return ids


@router.get("/{connector}")
def warehouse_summary(connector: Connector = ConnectorParam,
                      survey_id: int | None = SurveyParam,
                      db: Session = Depends(get_db)) -> dict:
    """Row counts and breakdowns of the connector's raw / clean / master layers."""
    return _service(connector).summary(db, survey_id=survey_id)


@router.post("/{connector}/clean")
def warehouse_clean(
    connector: Connector = ConnectorParam,
    survey_id: int | None = SurveyParam,
    operations: list[str] | None = Body(default=None, embed=True),
    table: str | None = TableParam,
    db: Session = Depends(get_db),
) -> dict:
    """Rebuild the clean layer from raw. `operations` defaults to the standard set.

    With `survey_id`, only that survey's rows are rebuilt.
    """
    try:
        extra = _table_kwargs(connector, table)
        return _service(connector).build_clean(db, operations, survey_id=survey_id, **extra)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{connector}/master")
def warehouse_master(connector: Connector = ConnectorParam,
                     survey_id: int | None = SurveyParam,
                     table: str | None = TableParam,
                     db: Session = Depends(get_db)) -> dict:
    """Rebuild the master layer from clean — one row per entity per survey, newest wins.

    With `survey_id`, only that survey's rows are rebuilt.
    """
    try:
        extra = _table_kwargs(connector, table)
        return _service(connector).build_master(db, survey_id=survey_id, **extra)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{connector}/rebuild")
def warehouse_rebuild(connector: Connector = ConnectorParam,
                      survey_id: int | None = SurveyParam,
                      table: str | None = TableParam,
                      db: Session = Depends(get_db)) -> dict:
    """Run clean and master back to back, the usual step after a new fetch."""
    service = _service(connector)
    extra = _table_kwargs(connector, table)
    try:
        return {"clean": service.build_clean(db, survey_id=survey_id, **extra),
                "master": service.build_master(db, survey_id=survey_id, **extra)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{connector}/export")
def warehouse_export(
    connector: Connector = ConnectorParam,
    survey_id: int | None = SurveyParam,
    layers: list[str] | None = Body(default=None, embed=True),
    job_id: str | None = Body(default=None, embed=True),
    dataset_ids: list[str] | None = Body(default=None, embed=True),
    db: Session = Depends(get_db),
) -> dict:
    """Write the warehouse to CSV — a directory per layer.

    `layers` defaults to all of the connector's layers. Pass `survey_id` to
    export one survey, `job_id` to export only what that job fetched (a crawl
    job follows its child jobs too), or `dataset_ids` when you already know
    the datasets. Omit all of them and the full layer is written, which on a
    multi-million-row table is slow.

    Every column is written, so all files of a layer share the same header.
    """
    ids = list(dataset_ids or [])
    if job_id:
        ids += _job_dataset_ids(db, job_id)

    service, schema = _CONNECTORS[connector]
    try:
        return {l: service.export_layer(db, l, dataset_ids=ids or None, survey_id=survey_id)
                for l in (layers or list(schema.LAYERS))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{connector}/rows")
def read_warehouse_rows(
    request: Request,
    connector: Connector = ConnectorParam,
    survey_id: int | None = SurveyParam,
    layer: str | None = Query(default=None,
                              description="Which layer table to read. Defaults to the raw layer."),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    job_id: str | None = Query(default=None, description="Only rows this job produced."),
    count: bool = Query(default=True, description="Set false to skip COUNT(*) on huge layers."),
    order_by: list[str] | None = Query(
        default=None,
        description="Columns to sort by. Paging is only reproducible with a stable "
                    "sort; omit it and the layer's key columns are used.",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """Read raw rows from a warehouse layer — every column, one object per row.

    Paged in SQL, so a preview of a multi-million-row layer costs nothing.
    Scope to one survey with `survey_id` or one ingest with `job_id`, and
    filter on any real column by adding it as a query parameter, e.g.
    `?level=district` or `?area=Bihar`. An unknown column name is rejected
    rather than passed through to SQL.
    """
    schema = _schema(connector)
    reserved = {"layer", "limit", "offset", "job_id", "count", "order_by", "survey_id"}
    filters: dict = {k: v for k, v in request.query_params.items() if k not in reserved}
    if survey_id is not None:
        # Passed as an int, not the raw query string, so it compares against
        # the BIGINT column rather than as text.
        filters[survey.SURVEY_COL] = survey_id
    ids = _job_dataset_ids(db, job_id) if job_id else None
    if connector is Connector.web:
        # Web layer rows are scoped by their table, not by a dataset id.
        if ids is not None:
            ids = web_service.layer_ids_for_datasets(db, ids) or [-1]
        order_by = order_by or ["id"]
    try:
        return warehouse_rows.read_rows(
            db, layer or schema.RAW, allowed_layers=schema.LAYERS,
            dataset_col=schema.DATASET_COL, limit=limit, offset=offset,
            dataset_ids=ids, filters=filters, count=count, order_by=order_by,
        )
    except warehouse_rows.LayerReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
