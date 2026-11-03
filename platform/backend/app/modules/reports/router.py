"""Export endpoints: CSV (one file per acquisition method) and KPI PDF."""
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

import pandas as pd

from app.core.config import EXPORTS_DIR
from app.crud import dataset as dataset_crud
from app.crud import warehouse_rows
from app.modules.census_warehouse import schema as census_schema
from app.modules.hmis_warehouse import schema as hmis_schema
from app.modules.merging import schema as merging_schema
from app.db.session import get_db
from app.models import Dataset, KpiRequest
from app.modules.reports.naming import CATEGORY_LABELS, category_for, safe_slug
from app.modules.reports.service import build_kpi_pdf, write_csv_exports

router = APIRouter(tags=["reports"])

# Rows pulled per round-trip when streaming a layer to disk.
LAYER_PAGE_SIZE = 1000



# Which connector's schema owns each layer, so `layer` alone is enough to
# locate it — the caller should not have to name the connector as well.
LAYER_OWNERS = {
    **{l: census_schema for l in census_schema.LAYERS},
    **{l: hmis_schema for l in hmis_schema.LAYERS},
    **{l: merging_schema for l in merging_schema.LAYERS},
}


def _export_layer(
    db: Session, layer: str, survey_id: int | None,
    column_filters: dict[str, str] | None = None,
) -> dict:
    """One warehouse layer, whole, as a single CSV.

    Paged out of SQL rather than loaded at once: hmis_raw runs to millions of
    rows, and reading it into one DataFrame to write a file would exhaust
    memory for no benefit.
    """
    schema = LAYER_OWNERS.get(layer)
    if schema is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown layer '{layer}'. Use one of: {', '.join(sorted(LAYER_OWNERS))}",
        )

    filters = dict(column_filters or {})
    if survey_id is not None:
        # As an int, so it compares against the numeric column, not as text.
        filters[schema.SURVEY_COL] = survey_id

    scope = f"_survey_{survey_id}" if survey_id is not None else "_all_surveys"
    # Filtered exports get their own filename, or two different selections
    # would silently overwrite each other's file.
    if column_filters:
        scope += "_" + "_".join(
            f"{k}-{safe_slug(str(v), 20)}" for k, v in sorted(column_filters.items())
        )
    target = EXPORTS_DIR / "layers" / f"{layer}{scope}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)

    written = header_done = 0
    try:
        while True:
            page = warehouse_rows.read_rows(
                db, layer, allowed_layers=schema.LAYERS,
                dataset_col=schema.DATASET_COL, limit=LAYER_PAGE_SIZE,
                offset=written, filters=filters, count=False,
            )
            if not page["rows"]:
                break
            frame = pd.DataFrame(page["rows"], columns=page["columns"])
            frame.to_csv(target, mode="a" if header_done else "w",
                         header=not header_done, index=False)
            header_done = 1
            written += len(page["rows"])
            if len(page["rows"]) < page["limit"]:
                break
    except warehouse_rows.LayerReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not written:
        scope_text = f" for survey {survey_id}" if survey_id is not None else ""
        raise HTTPException(status_code=404, detail=f"{layer} has no rows{scope_text}.")

    return {
        "survey_id": survey_id,
        "layer": layer,
        "export_dir": str(target.parent),
        "files_written": 1,
        "rows": written,
        "files": [{"filename": target.name, "path": str(target),
                   "rows": written, "bytes": target.stat().st_size}],
    }


@router.post("/exports/csv")
def export_all_csv(
    request: Request,
    survey_id: int | None = Query(
        default=None,
        description="Export only this survey's datasets. Omit to export every "
                    "survey, which on real data writes the whole warehouse.",
        examples=[1],
    ),
    stage: str | None = Query(
        default=None, description="raw | clean | master.", examples=["master"]
    ),
    category: str | None = Query(
        default=None, description="api | web | file | merged.", examples=["api"]
    ),
    layer: str | None = Query(
        default=None,
        description="Export a warehouse layer as one CSV instead of a file per "
                    "dataset — census_raw | census_clean | census_master | "
                    "hmis_raw | hmis_clean | hmis_master | dashboard_master. "
                    "Reads exactly what GET /warehouse/{connector}/rows returns "
                    "for the same layer and survey_id, so one file holds the whole "
                    "layer rather than one file per ingest.",
        examples=["census_master"],
    ),
    db: Session = Depends(get_db),
):
    """Write the warehouse to CSV under data/exports/.

    Two modes:

    * **`layer`given** — one CSV for that whole warehouse layer, the same rows
      `GET /warehouse/{connector}/rows` returns for the same `layer` and
      `survey_id`. Use this when you want the data, not the ingest history:
      twelve merge runs produce twelve dataset files but one `dashboard_master`.
    * **`layer` omitted** — one CSV per dataset, foldered by how the data was
      acquired: api/ (REST, data.gov.in, Census, HMIS), web/ (HTML tables,
      headless browser), file/ (CSV/Excel), merged/ (master datasets). This is
      the provenance view — it keeps each ingest separate on purpose.

    Scope with `survey_id` either way. `stage` and `category` narrow the
    per-dataset mode only.
    """
    if layer:
        # Anything not a named parameter is a filter on a real column, exactly
        # as GET /warehouse/{connector}/rows treats it. An unknown name is
        # rejected by the reader rather than reaching SQL.
        reserved = {"survey_id", "stage", "category", "layer"}
        column_filters = {
            k: v for k, v in request.query_params.items() if k not in reserved
        }
        return _export_layer(db, layer, survey_id, column_filters)

    stmt = select(Dataset).order_by(Dataset.created_at)
    if survey_id is not None:
        stmt = stmt.where(Dataset.survey_id == survey_id)
    if stage:
        stmt = stmt.where(Dataset.stage == stage)
    datasets = db.execute(stmt).scalars().all()

    if category:
        datasets = [d for d in datasets if category_for(d.connector) == category]
    if not datasets:
        scope = f" for survey {survey_id}" if survey_id is not None else ""
        raise HTTPException(
            status_code=404, detail=f"No datasets matched that filter{scope}."
        )

    pairs = [(d, dataset_crud.load_dataframe(d)) for d in datasets]
    written = write_csv_exports(pairs, EXPORTS_DIR)

    by_category: dict[str, list[dict]] = {}
    for entry in written:
        by_category.setdefault(entry["category"], []).append(entry)

    return {
        "survey_id": survey_id,
        "export_dir": str(EXPORTS_DIR),
        "files_written": len(written),
        "by_category": {
            cat: {
                "label": CATEGORY_LABELS.get(cat, cat),
                "count": len(items),
                "files": [i["filename"] for i in items],
            }
            for cat, items in by_category.items()
        },
        "files": written,
    }


@router.get("/kpi/{kpi_id}/export/pdf")
def export_kpi_pdf(kpi_id: str, db: Session = Depends(get_db)):
    record = db.get(KpiRequest, kpi_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"KPI request '{kpi_id}' not found.")
    dataset = dataset_crud.get_dataset_or_404(db, record.dataset_id)
    content = build_kpi_pdf(
        dataset.name, record.prompt, record.narrative or "", record.kpi_json or []
    )
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="kpi_report_{kpi_id}.pdf"'},
    )
