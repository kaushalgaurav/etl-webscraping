"""KPI endpoints.

Two kinds, deliberately separate:

* The template's own 227 Key Performance Indicators — `GET /kpi/catalog` and
  `POST /kpi/compute`. Fixed definitions, deterministic formulas, no model.
* Ad-hoc exploration — `POST /kpi`, where Claude proposes which computations
  to run over a survey and the backend executes them.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.constants.kpi_catalog import load_catalog
from app.core.config import settings
from app.db.session import get_db
from app.modules.kpi import dynamic, explorer, indicators
from app.helpers.dataframe import json_safe_records
from app.models import Dataset, KpiRequest
from app.modules.kpi import claude_client
from app.modules.kpi.service import execute_proposal
from app.modules.merging import survey
from app.modules.profiling.service import profile_dataframe

router = APIRouter(prefix="/kpi", tags=["kpi"])


class KpiPromptRequest(BaseModel):
    survey_id: int = Field(
        description="Which survey to compute KPIs for. The merged dashboard_master "
                    "rows for this survey are what Claude is shown and what the "
                    "computations run against.",
        examples=[1],
    )
    prompt: str = Field(
        description="What you want to know, in plain language.",
        examples=["Which districts have the lowest institutional delivery rate per capita?"],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"survey_id": 1, "prompt": "Top 5 districts by ANC registrations per capita."},
                {"survey_id": 1, "prompt": "Is population correlated with institutional deliveries?"},
            ]
        }
    }


class KpiComputeRequest(BaseModel):
    """Compute the template's indicators for one survey."""

    survey_id: int = Field(
        description="Which survey's merged dashboard_master rows to compute over.",
        examples=[1],
    )
    themes: list[str] | None = Field(
        default=None,
        description="Limit to these template themes, e.g. ['Demographic','RMNCH']. "
                    "Omit for all 227 indicators.",
        examples=[["Demographic", "RMNCH"]],
    )
    shape: Literal["long", "wide"] = Field(
        default="long",
        description="long = one record per (district, indicator). "
                    "wide = one row per district, one column per indicator — "
                    "the layout the template's sheets use.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"survey_id": 1},
                {"survey_id": 1, "shape": "wide"},
                {"survey_id": 1, "themes": ["Demographic"], "shape": "wide"},
            ]
        }
    }


def _merge_dataset(db: Session, survey_id: int) -> Dataset:
    """The most recent run that wrote this survey's dashboard_master rows.

    Recorded as the KPI's lineage, so a result can always be traced back to the
    merge it was computed from.
    """
    dataset = (
        db.query(Dataset)
        .filter(Dataset.table_name == survey.DASHBOARD_TABLE,
                Dataset.survey_id == survey_id)
        .order_by(Dataset.created_at.desc())
        .first()
    )
    if dataset is None:
        raise HTTPException(
            status_code=404,
            detail=f"No merged data for survey {survey_id}. "
                   "Run POST /datasets/merge for it first.",
        )
    return dataset


@router.post("")
def request_kpi(body: KpiPromptRequest, db: Session = Depends(get_db)):
    """Ask Claude for KPIs over a survey's merged data, then compute them.

    Claude never touches the data: it is shown the column profile and a sample
    of rows and returns which computations to run, which this backend executes
    deterministically. That keeps results reproducible and auditable.
    """
    dataset = _merge_dataset(db, body.survey_id)
    try:
        df = survey.load_dashboard(db, body.survey_id)
    except survey.SurveyMergeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    profile = profile_dataframe(df, max_top_values=5)
    sample_rows = json_safe_records(df.head(settings.max_profile_sample_rows))

    record = KpiRequest(dataset_id=dataset.id, survey_id=body.survey_id, prompt=body.prompt)
    try:
        proposal = claude_client.propose_kpis(body.prompt, profile, sample_rows)
    except claude_client.ClaudeNotConfigured as exc:
        record.error = str(exc)
        db.add(record)
        db.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # anthropic SDK errors, refusals, etc.
        record.error = str(exc)
        db.add(record)
        db.commit()
        raise HTTPException(status_code=502, detail=f"Claude request failed: {exc}") from exc

    executed = execute_proposal(df, proposal)
    record.narrative = proposal.get("narrative")
    record.kpi_json = executed
    db.add(record)
    db.commit()
    db.refresh(record)

    return {
        "id": record.id,
        "survey_id": record.survey_id,
        "dataset_id": dataset.id,
        "prompt": body.prompt,
        "narrative": record.narrative,
        "kpis": executed,
    }


class KpiGenerateRequest(BaseModel):
    """Generate dashboard KPIs dynamically from whatever the merged data holds."""

    survey_id: int = Field(
        description="Which survey's merged dashboard_master rows to analyse.",
        examples=[1],
    )
    max_kpis: int = Field(
        default=10, ge=3, le=20,
        description="Roughly how many KPIs to return, ranked most useful first.",
    )
    focus: str | None = Field(
        default=None,
        description="Optional steer, e.g. 'maternal health' or 'geographic disparities'. "
                    "Omit to let the selection follow the data.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"survey_id": 1},
                {"survey_id": 1, "max_kpis": 8, "focus": "geographic disparities"},
            ]
        }
    }


@router.post("/generate")
def generate_kpis(body: KpiGenerateRequest, db: Session = Depends(get_db)) -> dict:
    """Analyse a survey's merged data and propose ranked dashboard KPIs.

    Assumes nothing about the domain or the columns: Claude is shown the real
    structure of `dashboard_master` — every column's type, null share, range
    and sample values — and returns about ten dashboard-ready KPIs, each with
    an exact formula, numerator, denominator, unit, aggregation, recommended
    visualisation and filter behaviour.

    Two things are enforced here rather than merely asked of the model. Every
    column a KPI names is checked against the real frame, so a KPI referencing
    a column that does not exist is **rejected** with the reason instead of
    being computed on a guess. And the arithmetic is done by pandas from the
    declared formula, so Claude never produces a number and every value can be
    recomputed from its spec.

    KPIs that would be valuable but lack a valid denominator come back under
    `unavailable` with the specific reason — never with an invented base.

    For the report template's own fixed 227 indicators, use
    `GET /kpi/catalog` and `POST /kpi/compute` instead.
    """
    try:
        df = survey.load_dashboard(db, body.survey_id)
    except survey.SurveyMergeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    record = KpiRequest(
        dataset_id=_merge_dataset(db, body.survey_id).id,
        survey_id=body.survey_id,
        prompt=f"[dynamic KPI generation] max_kpis={body.max_kpis}"
               + (f", focus={body.focus}" if body.focus else ""),
    )
    try:
        proposal = dynamic.propose(df, max_kpis=body.max_kpis, focus=body.focus)
    except claude_client.ClaudeNotConfigured as exc:
        record.error = str(exc)
        db.add(record)
        db.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        record.error = str(exc)
        db.add(record)
        db.commit()
        raise HTTPException(status_code=502, detail=f"Claude request failed: {exc}") from exc

    result = dynamic.validate_and_compute(df, proposal)
    record.narrative = result.get("dataset_understanding")
    record.kpi_json = result["kpis"]
    db.add(record)
    db.commit()
    db.refresh(record)

    return {"id": record.id, "survey_id": body.survey_id,
            "rows_analysed": int(df.shape[0]), "columns_analysed": int(df.shape[1]),
            **result}


@router.get("/dashboard")
def dashboard_kpis(
    survey_id: int = Query(description="Which survey's merged rows to compute over."),
    by_place: bool = Query(
        default=False,
        description="Also return each KPI per district — the dashboard's map and data table.",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """The 28 dashboard KPIs with their latest snapshot values.

    The fixed card set behind the Public Sector Dashboard: KPI-01..KPI-28, each
    with its domain, aggregation, source formula, business meaning and snapshot
    — the same columns as the specification sheet, so the two read across.

    Nothing here is generated, so a card shows the same number on every run.
    A KPI whose column is absent from this survey's merged rows comes back
    `available: false` with the reason, rather than a zero that looks like data.
    Percentages above 100 carry a quality flag: HMIS counts services against an
    estimated denominator, so those are reporting intensity, not coverage.
    """
    try:
        df = survey.load_dashboard(db, survey_id)
    except survey.SurveyMergeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = indicators.dashboard_snapshot(df)
    payload = {"survey_id": survey_id, "places": int(df.shape[0]), **result}
    if by_place:
        payload["by_place"] = indicators.dashboard_by_place(df)
    return payload


class AggregateKpi(BaseModel):
    id: str = Field(max_length=100)
    aggregation: Literal[explorer.AGGREGATIONS]  # type: ignore[valid-type]
    numerator: str = Field(max_length=200)
    denominator: str | None = Field(default=None, max_length=200)
    scale: float | None = None


class AggregateSource(BaseModel):
    suffix: str = Field(default="", max_length=50,
                        description="Columns ending with this belong to the source ('' = fallback).")
    place_key: list[str] = Field(default_factory=list, max_length=4,
                                 description="Columns identifying one place of this source.")
    time: list[str] = Field(default_factory=list, max_length=4,
                            description="Columns this source varies over (e.g. year, period).")


class AggregateDimension(BaseModel):
    columns: list[str] = Field(min_length=1, max_length=6,
                               description="Coalesced in order: the first non-null wins.")
    tag: bool = Field(default=False,
                      description="Prefix values with their column when sources number places differently.")


class KpiAggregateRequest(BaseModel):
    """Declarative KPIs over one filtered, time-sliced, grouped selection of a survey."""

    survey_id: int
    kpis: list[AggregateKpi] = Field(min_length=1, max_length=200)
    sources: list[AggregateSource] = Field(min_length=1, max_length=10)
    dimensions: dict[str, AggregateDimension] = Field(default_factory=dict)
    filters: dict[str, list[str | int | float]] = Field(
        default_factory=dict, description="Dimension name -> allowed values (IN).")
    time: dict[str, str | int | float] | None = Field(
        default=None,
        description="Time slice for time-varying sources, e.g. "
                    "{'report_year_hmis': '2019-2020', 'period_hmis': 'MonthUpToMarch'}.")
    group_by: list[str] = Field(default_factory=list, max_length=3, description="Dimension names.")
    labels: dict[str, list[str]] = Field(default_factory=dict,
                                         description="Label name -> columns coalesced per group, for display.")
    pair_on: str | None = Field(default=None,
                                description="Dimension on which ratio numerators and denominators are paired.")
    previous_time: dict[str, str | int | float] | None = Field(
        default=None, description="A comparison slice (e.g. same period last year), computed with the same grouping.")
    trend_times: list[dict[str, str | int | float]] = Field(
        default_factory=list, max_length=60, description="Slices for a trend: overall values only.")
    trend_kpis: list[str] = Field(default_factory=list, max_length=20,
                                  description="KPI ids for the trend; empty = all.")

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "survey_id": 1,
                "kpis": [{"id": "anc_per_1000", "aggregation": "ratio_of_sums", "numerator": "anc_reg_hmis",
                          "denominator": "total_population_both_sexes_census", "scale": 1000}],
                "sources": [
                    {"suffix": "_census", "place_key": ["district_census"]},
                    {"suffix": "_hmis", "place_key": ["state_id_hmis", "district_id_hmis"],
                     "time": ["report_year_hmis", "period_hmis"]},
                ],
                "dimensions": {
                    "state": {"columns": ["state_census", "state_id_hmis"]},
                    "district": {"columns": ["district_census", "district_id_hmis"], "tag": True},
                },
                "filters": {"state": ["10"]},
                "time": {"report_year_hmis": "2019-2020", "period_hmis": "MonthUpToMarch"},
                "group_by": ["district"],
                "labels": {"name": ["district_name_census", "district_name_hmis"]},
                "pair_on": "district",
            }]
        }
    }


@router.post("/aggregate")
def aggregate_kpis(body: KpiAggregateRequest, db: Session = Depends(get_db)) -> dict:
    """Compute declarative KPIs for a filtered selection, overall and per group.

    Built for configurable dashboards: KPI cards (no grouping), maps and
    rankings (group by a geography dimension), trends (one call per time
    slice). Only column names and a fixed aggregation list are accepted — no
    formulas are evaluated. Each column is aggregated over its own source,
    one row per that source's place and, for a time-varying source, within the
    requested slice, so repeated census values and cumulative HMIS periods are
    never double-counted.
    """
    try:
        df = survey.load_dashboard(db, body.survey_id)
    except survey.SurveyMergeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    kpis = [k.model_dump() for k in body.kpis]
    sources = [s.model_dump() for s in body.sources]
    common = {
        "dimensions": {n: d.model_dump() for n, d in body.dimensions.items()},
        "filters": body.filters, "pair_on": body.pair_on,
    }
    try:
        result = explorer.aggregate(df, kpis, sources, time=body.time, group_by=body.group_by,
                                    labels=body.labels, **common)
        previous = None
        if body.previous_time:
            previous = explorer.aggregate(df, kpis, sources, time=body.previous_time,
                                          group_by=body.group_by, **common)
        trend_specs = [k for k in kpis if not body.trend_kpis or k["id"] in body.trend_kpis]
        trend = [
            {"time": t, "overall": explorer.aggregate(df, trend_specs, sources, time=t, **common)["overall"]}
            for t in body.trend_times
        ] if trend_specs else []
    except explorer.ExplorerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "survey_id": body.survey_id, "time": body.time, "group_by": body.group_by, **result,
        "previous": previous and {"time": body.previous_time, "overall": previous["overall"],
                                  "groups": previous["groups"]},
        "trend": trend,
        "kpi_meta": {k["id"]: {"time_varying": explorer.time_varying(sources, k)} for k in kpis},
    }


class KpiDimensionsRequest(BaseModel):
    survey_id: int
    dimensions: dict[str, AggregateDimension] = Field(default_factory=dict)
    labels: dict[str, list[str]] = Field(default_factory=dict)
    time_columns: list[str] = Field(default_factory=list, max_length=4)


@router.post("/dimensions")
def kpi_dimensions(body: KpiDimensionsRequest, db: Session = Depends(get_db)) -> dict:
    """What a dashboard can filter and measure for a survey.

    Every column with whether it is numeric; the distinct combinations of the
    requested (coalesced) dimensions and labels, so filters can cascade (the
    districts of a state are the combos carrying it); and the distinct time
    slices.
    """
    try:
        df = survey.load_dashboard(db, body.survey_id)
    except survey.SurveyMergeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return {"survey_id": body.survey_id, **explorer.dimensions(
            df, {n: d.model_dump() for n, d in body.dimensions.items()}, body.labels, body.time_columns,
        )}
    except explorer.ExplorerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/catalog")
def kpi_catalog(
    theme: str | None = Query(default=None, description="Filter to one theme."),
    status: str | None = Query(
        default=None,
        description="computed | proxy | unavailable — filter by whether we can "
                    "answer the indicator from ingested data.",
    ),
) -> dict:
    """The 227 Key Performance Indicators of the report template.

    The definitive list, straight from `General_Population_Template.xlsx`, each
    with its theme, the template's own definition, and whether this platform
    can compute it today:

    * `computed` — produced from the source the template names.
    * `proxy` — the template names a survey we have not ingested (usually
      NFHS-5) but HMIS reports a service-based equivalent; the value is real
      and labelled so a facility-reported rate is never mistaken for a
      household-survey one.
    * `unavailable` — nothing ingested answers it; `reason` says what is missing.
    """
    entries = load_catalog()
    if theme:
        entries = [e for e in entries if e["theme"].lower() == theme.lower()]
    if status:
        entries = [e for e in entries if e["status"] == status.lower()]

    by_theme: dict[str, dict] = {}
    for e in load_catalog():
        bucket = by_theme.setdefault(e["theme"], {"computed": 0, "proxy": 0, "unavailable": 0})
        bucket[e["status"]] += 1

    return {
        "total": len(load_catalog()),
        "returned": len(entries),
        "by_theme": by_theme,
        "indicators": [
            {
                "id": e["id"], "theme": e["theme"], "name": e["name"],
                "definition": e["definition"], "unit": e["unit"],
                "status": e["status"], "basis": e["basis"],
                "template_source": e["template_source"],
                "reason": e.get("reason"),
            }
            for e in entries
        ],
    }


@router.post("/compute")
def compute_kpis(body: KpiComputeRequest, db: Session = Depends(get_db)) -> dict:
    """Compute the template's indicators for a survey, district by district.

    Deterministic: every value comes from a declared formula over
    `dashboard_master` — no model is involved, so the same survey always gives
    the same numbers. `shape=wide` returns one row per district with an
    indicator per column, which is the layout the template's sheets use.
    """
    try:
        df = survey.load_dashboard(db, body.survey_id)
    except survey.SurveyMergeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        result = indicators.compute(df, themes=body.themes)
    except indicators.IndicatorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = {"survey_id": body.survey_id, **result}
    if body.shape == "wide":
        payload["rows"] = indicators.wide(result["values"])
        del payload["values"]
    return payload


@router.get("")
def list_kpis(
    survey_id: int | None = Query(default=None, description="Only this survey's KPI requests."),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """Every KPI request, newest first — the listing for a survey's generated KPIs.

    Rows carry a `kpi_count` rather than the full results; fetch one with
    `GET /kpi/{kpi_id}` to see the computations and their values. A row with
    `error` set is a request that failed before any KPI was computed.
    """
    query = db.query(KpiRequest)
    if survey_id is not None:
        query = query.filter(KpiRequest.survey_id == survey_id)
    total = query.count()
    records = (
        query.order_by(KpiRequest.created_at.desc()).limit(limit).offset(offset).all()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(records),
        "kpis": [
            {
                "id": r.id,
                "survey_id": r.survey_id,
                "dataset_id": r.dataset_id,
                "prompt": r.prompt,
                "narrative": r.narrative,
                "kpi_count": len(r.kpi_json or []),
                "error": r.error,
                "created_at": r.created_at,
            }
            for r in records
        ],
    }


@router.get("/{kpi_id}")
def get_kpi(kpi_id: str, db: Session = Depends(get_db)):
    """One KPI request with its full computed results."""
    record = db.get(KpiRequest, kpi_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"KPI request '{kpi_id}' not found.")
    return {
        "id": record.id,
        "survey_id": record.survey_id,
        "dataset_id": record.dataset_id,
        "prompt": record.prompt,
        "narrative": record.narrative,
        "kpis": record.kpi_json or [],
        "error": record.error,
        "created_at": record.created_at,
    }
