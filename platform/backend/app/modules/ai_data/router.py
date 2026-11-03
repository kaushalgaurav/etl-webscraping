"""Gemini data endpoints — one prompt in, values in Gemini's own tables.

  POST   /ai-data/fetch                        prompt -> Gemini (background) -> raw -> clean -> master
  GET    /ai-data/fetches/{id}                 poll a run: status, progress, sources
  GET    /ai-data/fetches                      every prompt run (per survey)
  GET    /ai-data/fetches/{id}/download        that run's table as Gemini returned it
  DELETE /ai-data/fetches/{id}                 remove a bad run, rebuild its survey
  GET    /ai-data/warehouse?survey_id=         layer summary  (same shape as /warehouse/{c})
  GET    /ai-data/warehouse/rows?survey_id=    layer rows     (same shape as /warehouse/{c}/rows)
  GET    /ai-data/indicators?survey_id=        what a survey holds: indicator x source
  GET    /ai-data/export?survey_id=            Excel/CSV of the master table
  POST   /ai-data/rebuild?survey_id=           rebuild a survey's clean/master from raw

Everything lives in gemini_fetches / gemini_raw / gemini_clean / gemini_master;
nothing here touches the census/HMIS warehouse or dashboard_master.
"""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.db.session import engine
from app.helpers.dataframe import json_safe_records
from app.modules.ai_data import service, store

router = APIRouter(prefix="/ai-data", tags=["AI data (Gemini)"])

# Most rows returned per page, as in the warehouse row reader.
MAX_LIMIT = 1000



class FetchRequest(BaseModel):
    prompt: str = Field(
        description="Any government data, in plain language — one prompt may span several "
                    "sources (Census, HMIS, NFHS, RHS, ...).",
        examples=["District-wise population (Census 2011), institutional deliveries "
                  "(HMIS 2022-23) and PHC count (RHS 2022) for Bihar"],
    )
    survey_id: int = Field(description="Survey to file the values under.", examples=[10])
    use_search: bool = Field(
        default=False,
        description="Let Gemini use Google Search to look figures up. About 3x slower "
                    "(a 38-district table took ~85 s against ~30 s without).",
    )
    source: Literal["census", "hmis"] | None = Field(
        default=None,
        description="One source per fetch: Gemini answers only from it and must use that "
                    "source's exact warehouse column names (GET /ai-data/columns?source=...); "
                    "any other column is discarded. Omit for a free-form table.",
    )
    max_columns: int = Field(
        default=20, ge=1, le=100,
        description="Most value columns per Gemini answer — the batch size for `columns` / "
                    "`all_columns`. A batch whose answer comes back cut off is retried as "
                    "two halves.",
    )
    columns: list[str] | None = Field(
        default=None,
        description="With `source`: exactly these warehouse columns "
                    "(names from GET /ai-data/columns?source=...).",
        examples=[["anc_reg", "anc_reg_1st_tri", "inst_del", "total_del", "bcg"]],
    )
    all_columns: bool = Field(
        default=False,
        description="With `source`: ALL of that source's columns (census 85, hmis 385), in "
                    "batches of max_columns, combined into one table.",
    )
    years: list[str] | None = Field(
        default=None,
        description="Year by year: one set of calls per year, every row stamped with its year "
                    "(one row per place per year). HMIS years are financial years, e.g. "
                    "2019-20. Calls = years x column batches — e.g. 3 years x 20 columns = 3 "
                    "calls; 3 years x all 385 HMIS columns = 60. Follow `progress`.",
        examples=[["2019-20", "2020-21", "2021-22"]],
    )


def _filters(source_key, place_level, state, district, year, indicator) -> dict:
    return {"source_key": source_key, "place_level": place_level, "state_name": state,
            "district_name": district, "year": year, "indicator": indicator}


@router.post("/fetch", status_code=202)
def fetch(body: FetchRequest):
    """Start a Gemini fetch; returns a `fetch_id` at once — Gemini runs in the background.

    Gemini takes ~30 s for a district table (~85 s with `use_search`), so the
    request does not wait for it. Poll `GET /ai-data/fetches/{fetch_id}`:
    `status` goes `running` -> `succeeded` (with preview, `progress` and the
    survey's new totals under `result` / `provenance`) or `failed` (with `error`).

    Values are stored one row each with their source and indicator, so a new
    prompt adds rows, never columns, and never replaces another prompt's values.
    Fetching the same source/indicator/place/year again replaces only that value.
    """
    if not settings.gemini_api_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not set. Add it to the backend's .env to enable /ai-data.")
    try:
        started = service.start_fetch(body.prompt, body.survey_id, body.use_search,
                                      source=body.source, max_columns=body.max_columns,
                                      all_columns=body.all_columns, columns=body.columns,
                                      years=body.years)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**started, "warning": service.WARNING,
            "poll": f"/ai-data/fetches/{started['fetch_id']}",
            "download_when_done": f"/ai-data/fetches/{started['fetch_id']}/download?format=xlsx"}


@router.get("/columns")
def source_columns(source: Literal["census", "hmis"] = Query(...)):
    """The exact column names Gemini must use for a one-source fetch.

    Read live from that source's warehouse master table, so they are the same
    names `GET /warehouse/{source}/rows` returns.
    """
    columns = service.source_columns(source)
    return {"source": source, "table": service.SINGLE_SOURCES[source]["table"],
            "total": len(columns), "columns": columns}


@router.post("/rebuild")
def rebuild(survey_id: int = Query(...)):
    """Rebuild a survey's gemini_clean / gemini_master from gemini_raw."""
    return service.rebuild(survey_id)


@router.get("/fetches")
def list_fetches(survey_id: int | None = Query(default=None)):
    """Every prompt run, newest first."""
    return service.list_fetches(survey_id)


@router.get("/fetches/{fetch_id}")
def get_fetch(
    fetch_id: str,
    limit: int = Query(default=1000, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    details: bool = Query(default=False,
                          description="Also return prompt, sources, notes and model."),
):
    """Poll a run; once it has succeeded, its final data.

    While running: `status` and `progress`. When `succeeded`: the final rows
    (gemini_master) for the places and years this run covered, in the same
    shape as GET /warehouse/{connector}/rows — `total, limit, offset, returned,
    columns, rows`. When `failed`: `error`. `details=true` adds the prompt,
    sources, notes and model.
    """
    fetch_row = service.get_fetch(fetch_id)
    if fetch_row is None:
        raise HTTPException(status_code=404, detail=f"Fetch '{fetch_id}' not found.")
    out = {"fetch_id": fetch_id, "survey_id": fetch_row["survey_id"],
           "status": fetch_row["status"], "progress": fetch_row.get("progress")}
    if fetch_row["status"] == "failed":
        out["error"] = fetch_row.get("error")
    if fetch_row["status"] == "succeeded":
        # A one-source fetch shows that source's full warehouse column list,
        # empty where Gemini gave no value.
        provenance = fetch_row.get("provenance") or {}
        source = (provenance.get("restriction") or {}).get("source")
        full = service.source_columns(source) if source else None
        with engine.begin() as conn:
            df = store.warehouse_frame(conn, store.MASTER, fetch_row["survey_id"], fetch_id,
                                       full_columns=full)
        page = df.iloc[offset:offset + limit]
        out |= {"layer": store.MASTER, "total": int(len(df)), "limit": limit, "offset": offset,
                "returned": int(len(page)), "columns": list(page.columns),
                "rows": json_safe_records(page)}
    if details:
        out["details"] = {k: fetch_row.get(k) for k in
                          ("prompt", "title", "model", "use_search", "created_at",
                           "finished_at", "sources", "provenance")}
    return out


@router.get("/fetches/{fetch_id}/download")
def download_fetch(fetch_id: str, format: Literal["xlsx", "csv"] = Query(default="xlsx")):
    """That run's table as Gemini returned it; Excel adds an 'About' sheet."""
    fetch_row = service.get_fetch(fetch_id)
    if fetch_row is None:
        raise HTTPException(status_code=404, detail=f"Fetch '{fetch_id}' not found.")
    content, media, filename = service.export_fetch(fetch_row, format)
    return Response(content=content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.delete("/fetches/{fetch_id}")
def delete_fetch(fetch_id: str):
    """Remove a run's values (e.g. a bad answer) and rebuild its survey."""
    result = service.delete_fetch(fetch_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Fetch '{fetch_id}' not found.")
    return result


@router.get("/indicators")
def list_indicators(survey_id: int = Query(...), source_key: str | None = Query(default=None)):
    """What the survey holds: each indicator per source, with unit, places, years."""
    with engine.begin() as conn:
        store.ensure_tables(conn)
        df = store.indicators(conn, survey_id, source_key)
    return {"survey_id": survey_id, "indicators": int(len(df)),
            "sources": sorted(df["source_key"].unique().tolist()) if not df.empty else [],
            "items": json_safe_records(df)}


WarehouseLayer = Literal["gemini_raw", "gemini_clean", "gemini_master"]
_RESERVED = {"survey_id", "layer", "limit", "offset", "count", "order_by", "source"}


@router.get("/warehouse")
def warehouse_summary(survey_id: int = Query(...)):
    """Row and column counts per layer — the same shape as GET /warehouse/{connector}."""
    out = {}
    with engine.begin() as conn:
        store.ensure_tables(conn)
        for layer in store.LAYERS:
            df = store.warehouse_frame(conn, layer, survey_id)
            out[layer] = {
                "survey_id": survey_id, "rows": int(len(df)), "columns": int(df.shape[1]),
                "rows_by_level": ({str(k): int(v) for k, v in df["level"].value_counts().items()}
                                  if "level" in df and len(df) else {}),
            }
    return out


@router.get("/warehouse/rows")
def warehouse_rows(
    request: Request,
    survey_id: int = Query(...),
    layer: WarehouseLayer = Query(default="gemini_master"),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    count: bool = Query(default=True),
    order_by: list[str] | None = Query(default=None),
    source: Literal["census", "hmis"] | None = Query(
        default=None,
        description="Show that source's full warehouse column list (census 85, hmis 385), "
                    "empty where no value has been fetched yet."),
):
    """Rows of a layer — the same shape as GET /warehouse/{connector}/rows.

    One row per place (and year), every column: `_dataset_id` (the fetch),
    `_ingested_at`, `survey_id`, `level`, `state_name`, `state_id`,
    `district_name`, `district_id`, `unique_id`, `year`, then one column per
    indicator. `gemini_raw` keeps Gemini's own column names, one row per row it
    returned; `gemini_clean` / `gemini_master` use standard names and
    district-master ids, and master holds the newest value of each indicator
    for each place across all fetches. Filter on any column by adding it as a
    query parameter, e.g. `?level=district&state_name=Bihar`.
    """
    filters = {k: v for k, v in request.query_params.items() if k not in _RESERVED}
    with engine.begin() as conn:
        store.ensure_tables(conn)
        df = store.warehouse_frame(
            conn, layer, survey_id,
            full_columns=service.source_columns(source) if source and layer != store.RAW else None)
    unknown = [k for k in filters if k not in df.columns]
    if unknown:
        raise HTTPException(status_code=400, detail=(
            f"Unknown column(s): {', '.join(unknown)}. Available: "
            f"{', '.join(list(df.columns)[:25])}{' …' if df.shape[1] > 25 else ''}"))
    for key, value in filters.items():
        df = df[df[key].astype(str).str.removesuffix(".0") == value.removesuffix(".0")]
    if order_by:
        bad = [c for c in order_by if c not in df.columns]
        if bad:
            raise HTTPException(status_code=400,
                                detail=f"Cannot order by unknown column(s): {', '.join(bad)}.")
        df = df.sort_values(order_by, kind="mergesort")
    page = df.iloc[offset:offset + limit]
    return {
        "layer": layer,
        "total": int(len(df)) if count else None,
        "limit": limit,
        "offset": offset,
        "returned": int(len(page)),
        "columns": list(page.columns),
        "rows": json_safe_records(page),
    }


@router.get("/export")
def export(
    survey_id: int = Query(...),
    format: Literal["xlsx", "csv"] = Query(default="xlsx"),
    shape: Literal["long", "wide"] = Query(default="wide"),
    indicator: list[str] | None = Query(default=None),
    source_key: str | None = None, place_level: str | None = None,
):
    """A survey's Gemini values as Excel (Data + Indicators + About) or CSV."""
    content, media, filename = service.export_survey(
        survey_id, format, shape,
        _filters(source_key, place_level, None, None, None, indicator))
    return Response(content=content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
