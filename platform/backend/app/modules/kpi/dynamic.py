"""Dynamic KPI generation: analyse a merged dataset, propose dashboard KPIs.

The counterpart to `app.constants.kpi_catalog`, which holds the report
template's fixed 227 indicators. This module assumes nothing about the domain
or the columns: Claude is shown the real structure of the merged frame and
returns roughly ten dashboard-ready KPIs with exact formulas.

Two safeguards sit between the proposal and the answer, because a prompt rule
is a request and not a guarantee:

1. Every column a KPI names is checked against the real frame. "Never invent a
   column" is enforced here, not hoped for — a KPI referencing a column that
   does not exist is rejected with the reason, never silently computed.
2. The arithmetic is done by pandas from the declared numerator, denominator
   and aggregation. Claude never produces a number, so a value can always be
   recomputed from the spec.
"""
import json
from typing import Literal

import anthropic
import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.core.config import settings
from app.modules.kpi import aggregation as agg
from app.modules.kpi.claude_client import ClaudeNotConfigured

AGGREGATIONS = ("sum", "mean", "median", "count", "min", "max", "ratio_of_sums", "none")
VISUALISATIONS = (
    "kpi_card", "line_chart", "bar_chart", "stacked_bar_chart", "map",
    "table", "donut_chart", "scatter_plot",
)
FILTER_BEHAVIOURS = (
    "filter_responsive", "fixed_reference",
    "fixed_denominator", "filtered_denominator",
)


class KpiSpec(BaseModel):
    rank: int = Field(description="1 = most useful for a dashboard.")
    name: str
    definition: str = Field(description="What it measures, in plain language.")
    formula: str = Field(description="The exact formula, written out.")
    numerator: str | None = Field(
        description="Column name for the numerator, exactly as in the schema. "
                    "Null when the KPI needs no numerator."
    )
    denominator: str | None = Field(
        description="Column name for the denominator, exactly as in the schema. "
                    "Null when not applicable — never invent one to make a percentage."
    )
    scale: float | None = Field(
        description="Standardisation factor: 100 for a percentage, 1000 for a "
                    "per-1,000 rate, null otherwise."
    )
    unit: str = Field(description="e.g. '%', 'per 1,000 live births', 'persons'.")
    required_columns: list[str] = Field(
        description="Every column this KPI reads, exactly as given in the schema."
    )
    aggregation: Literal[AGGREGATIONS] = Field(  # type: ignore[valid-type]
        description="ratio_of_sums = sum(numerator)/sum(denominator)*scale, which is "
                    "the correct way to aggregate a rate across places. Never average "
                    "an already-computed per-row rate."
    )
    geography_level: str | None = Field(description="e.g. 'district'. Null if not applicable.")
    time_period: str | None = Field(description="The period covered. Null if not applicable.")
    visualisation: Literal[VISUALISATIONS] = Field(  # type: ignore[valid-type]
        description="Recommended dashboard visualisation."
    )
    filter_behaviour: Literal[FILTER_BEHAVIOURS] = Field(  # type: ignore[valid-type]
        description="How the KPI should recalculate when dashboard filters change."
    )
    data_quality_flags: list[str] = Field(
        description="Concerns affecting reliability — nulls, zero denominators, "
                    "possible double counting, incompatible units. Empty if none."
    )


class UnavailableKpi(BaseModel):
    name: str
    reason: str = Field(description="Specifically what the dataset lacks.")


class DynamicKpiProposal(BaseModel):
    dataset_understanding: str = Field(
        description="What this dataset is: its measures, dimensions, geography, "
                    "time periods and merged sources."
    )
    kpis: list[KpiSpec] = Field(description="Roughly 10, ranked most useful first.")
    unavailable: list[UnavailableKpi] = Field(
        description="KPIs that would be valuable but cannot be calculated correctly, "
                    "usually for want of a valid denominator."
    )


SYSTEM_PROMPT = """You are an expert in data analytics, KPI design, statistical analysis, and dashboard development.
Your task is to analyse a merged dataset and dynamically generate the most useful KPIs for a dashboard.

The dataset, its columns, dimensions, indicators, geography, time periods, and data sources change from one input to another. Do not assume a fixed domain, dataset structure, indicator list, geography, time period, or naming convention.

UNDERSTAND THE DATASET FIRST. Before generating any KPI, read all column headers and infer the meaning and type of each variable: measures, dimensions, categorical and numerical variables, dates/time periods, geographic variables, population/denominator variables, counts, rates, percentages, ratios, indexes, and source-specific variables. Work out the relationships between variables, which sources have been merged and what each contributes, which variables are duplicated or overlapping, and where missing, null, zero or invalid values would affect a calculation.

SELECT ABOUT 10 KPIs. Adjust the number if the dataset does not support exactly ten meaningful ones. Prioritise KPIs that are relevant to what the data actually contains, useful for monitoring performance or status, actionable, easy to interpret, statistically valid, meaningful for comparison, non-redundant, suitable for visualisation, based only on columns that exist, and useful for identifying gaps, disparities, trends or changes. Do not select a KPI merely because it is easy to calculate, or because it is commonly used elsewhere.

Consider these types where the data supports them: overall level/status, coverage, performance, outcome, utilisation, ratio, rate, percentage, average/median, growth or period-on-period change, geographic disparity, population-group disparity, resource or capacity, service delivery, gap, and trend indicators. Do not force every category into the list.

FORMULAS must be exact and unambiguous. Identify the numerator, the denominator where applicable, the aggregation method, the unit, the required columns, any filtering conditions, the time period, and the geographic level. Percentage = (numerator / denominator) x 100. Rate = (events / relevant population) x K, with K a standardisation factor the dataset supports. Growth = ((current - previous) / previous) x 100. Do not apply these generically — derive the correct denominator, comparison period and aggregation from the dataset.

DENOMINATOR VALIDATION IS CRITICAL. Before proposing any percentage, rate, ratio or coverage indicator, confirm the denominator exists in the dataset, represents the appropriate base, and is compatible with the numerator in geography, time period, population and units. Do not create a denominator by assumption. Do not use total population unless it is conceptually correct. Do not calculate a percentage when the right denominator is unavailable. If a useful KPI cannot be calculated correctly, put it in `unavailable` with the specific reason rather than inventing a denominator.

AGGREGATION: to aggregate a rate across places, use ratio_of_sums — sum the numerator and sum the denominator, then divide. Never average per-row rates; that weights a small district the same as a large one.

GEOGRAPHY AND TIME: do not assume any particular administrative level or period. Detect what is present and state it.

AVOID REDUNDANCY: do not propose several KPIs carrying essentially the same information. Prefer a balanced, complementary set.

DATA QUALITY: flag missing values, zero denominators, duplicates, impossible values, inconsistent units, inconsistent geographic names or time formats, distorting outliers, numerator/denominator incompatibility, double counting and aggregation errors. Flag a KPI rather than presenting it as fully reliable.

RANK the KPIs by overall dashboard usefulness, weighing decision-making value, relevance, interpretability, data reliability, actionability, comparability, coverage of important aspects, and ability to reveal gaps or trends.

STRICT RULES, without exception: never invent a column, a value or a denominator; never assume a variable exists; never assume the dataset belongs to a particular sector; never use a formula the data cannot support; never use an inappropriate denominator merely to produce a percentage; never combine incompatible measures; never hide an assumption. Use only what the supplied schema supports.

FINAL VALIDATION, for every KPI before you return it: column -> concept -> numerator -> denominator -> formula -> unit -> aggregation -> time -> geography -> dashboard suitability. Return only KPIs that pass."""


def _column_digest(df: pd.DataFrame, max_samples: int = 3) -> list[dict]:
    """Every column with the facts needed to judge it as a measure or a base."""
    total = len(df)
    digest = []
    for name in df.columns:
        series = df[name]
        non_null = int(series.notna().sum())
        entry = {
            "name": name,
            "dtype": str(series.dtype),
            "non_null": non_null,
            "null_pct": round(100 * (1 - non_null / total), 1) if total else 100.0,
            "unique": int(series.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(series) and non_null:
            entry |= {
                "min": _py(series.min()), "max": _py(series.max()),
                "mean": _py(series.mean()), "zeros": int((series == 0).sum()),
            }
        else:
            entry["sample_values"] = [
                str(v) for v in series.dropna().unique()[:max_samples]
            ]
        digest.append(entry)
    return digest


def _py(value):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (np.integer, np.floating)):
        return round(float(value), 4)
    return value


def propose(df: pd.DataFrame, max_kpis: int = 10, focus: str | None = None) -> dict:
    """Ask Claude for ranked dashboard KPIs over this frame."""
    if not settings.anthropic_api_key:
        raise ClaudeNotConfigured(
            "ANTHROPIC_API_KEY is not set. Set it in the backend's environment "
            "to enable dynamic KPI generation."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    content = (
        f"The merged dataset has {df.shape[0]} rows and {df.shape[1]} columns.\n\n"
        f"Column structure:\n{json.dumps(_column_digest(df), indent=2, default=str)}\n\n"
        f"Sample rows:\n"
        f"{json.dumps(df.head(5).astype(object).where(pd.notnull(df.head(5)), None).to_dict('records'), indent=2, default=str)}\n\n"
        f"Return about {max_kpis} KPIs, ranked."
        + (f"\n\nFocus the selection on: {focus}" if focus else "")
    )

    response = client.messages.parse(
        model=settings.claude_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_format=DynamicKpiProposal,
    )

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None) or "no explanation given"
        raise RuntimeError(f"Claude declined this request ({detail}).")
    if response.parsed_output is None:
        raise RuntimeError(
            f"Claude returned no usable KPI proposal (stop_reason={response.stop_reason})."
        )
    return response.parsed_output.model_dump()


def validate_and_compute(df: pd.DataFrame, proposal: dict) -> dict:
    """Enforce the strict rules, then compute each surviving KPI.

    A proposal is not trusted because the prompt asked it to behave: every
    column named is checked against the frame, and a KPI referencing one that
    does not exist is rejected rather than computed on a guess.
    """
    columns = set(df.columns)
    accepted, rejected = [], []

    for spec in proposal.get("kpis", []):
        named = [c for c in (
            *(spec.get("required_columns") or []),
            spec.get("numerator"), spec.get("denominator"),
        ) if c]
        unknown = sorted({c for c in named if c not in columns})
        if unknown:
            rejected.append({
                **spec,
                "rejected_because": f"references column(s) not in the dataset: {', '.join(unknown)}",
            })
            continue

        value, note = _compute(df, spec)
        accepted.append({**spec, "value": value, "computation_note": note})

    accepted.sort(key=lambda k: k.get("rank") or 999)
    return {
        "dataset_understanding": proposal.get("dataset_understanding"),
        "kpis": accepted,
        "rejected": rejected,
        "unavailable": proposal.get("unavailable", []),
        "summary": {
            "proposed": len(proposal.get("kpis", [])),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "unavailable": len(proposal.get("unavailable", [])),
        },
    }


def _compute(df: pd.DataFrame, spec: dict) -> tuple[float | None, str]:
    """The KPI's headline value across the whole frame."""
    aggregation = spec.get("aggregation") or "none"
    numerator, denominator = spec.get("numerator"), spec.get("denominator")
    scale = spec.get("scale") or 1

    if aggregation == "none":
        return None, "No aggregation declared; this KPI is a per-row series."

    if aggregation == "ratio_of_sums":
        if not numerator or not denominator:
            return None, "ratio_of_sums needs both a numerator and a denominator."
        value, error = agg.ratio(df, numerator, denominator, scale)
        if error:
            return None, error
        return value, f"sum({numerator})/sum({denominator})*{scale:g}"

    if not numerator:
        return None, f"'{aggregation}' needs a numerator column."
    values = agg.series(df, numerator)
    if aggregation == "count":
        return int(values.notna().sum()), f"count of non-null {numerator}"
    result = getattr(values, aggregation)()
    if pd.isna(result):
        return None, f"'{numerator}' has no numeric values to aggregate."
    scaled = f"*{scale:g}" if scale != 1 else ""
    return round(float(result) * scale, 4), agg.note(numerator, aggregation) + scaled
