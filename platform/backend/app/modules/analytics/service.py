"""ANALYTICS ENGINE.

Exposes a small, fixed set of safe operations over a dataset — used both
directly (dashboard charts, via the API) and indirectly (Claude proposes
*which* computation to run via a structured spec; this module is the only
thing that actually touches the data, so a KPI prompt can never turn into
arbitrary code execution).
"""
import pandas as pd

from app.helpers.dataframe import json_safe_records

SUPPORTED_AGGS = ("sum", "mean", "count", "min", "max", "median", "nunique")


class AnalyticsError(Exception):
    pass


def _require_column(df: pd.DataFrame, column: str) -> None:
    if column not in df.columns:
        raise AnalyticsError(f"Column '{column}' not found. Available: {', '.join(df.columns)}")


def aggregate(df: pd.DataFrame, group_by: str, agg: str, metric: str | None = None) -> list[dict]:
    if agg not in SUPPORTED_AGGS:
        raise AnalyticsError(f"Unsupported aggregation '{agg}'. Use one of: {', '.join(SUPPORTED_AGGS)}")
    _require_column(df, group_by)

    if agg == "count" and metric is None:
        result = df.groupby(group_by, dropna=False).size()
        result.name = "count"
    else:
        if metric is None:
            raise AnalyticsError(f"Aggregation '{agg}' requires a 'metric' column.")
        _require_column(df, metric)
        result = df.groupby(group_by, dropna=False)[metric].agg(agg)

    result = result.reset_index()
    result.columns = [group_by, metric or "count"]
    return json_safe_records(result)


def top_n(df: pd.DataFrame, column: str, n: int = 10, ascending: bool = False) -> list[dict]:
    _require_column(df, column)
    ordered = df.sort_values(column, ascending=ascending, na_position="last").head(n)
    return json_safe_records(ordered)


def correlation(df: pd.DataFrame, column_a: str, column_b: str) -> float | None:
    _require_column(df, column_a)
    _require_column(df, column_b)
    a = pd.to_numeric(df[column_a], errors="coerce")
    b = pd.to_numeric(df[column_b], errors="coerce")
    value = a.corr(b)
    return None if pd.isna(value) else round(float(value), 4)


def describe(df: pd.DataFrame, column: str) -> dict:
    _require_column(df, column)
    series = df[column]
    if pd.api.types.is_numeric_dtype(series):
        d = series.describe()
        return {k: (None if pd.isna(v) else float(v)) for k, v in d.items()}
    return {
        "count": int(series.count()),
        "unique": int(series.nunique()),
        "top": series.mode().iloc[0] if not series.mode().empty else None,
    }


def run_computation(df: pd.DataFrame, spec: dict) -> dict:
    """Execute one Claude-proposed computation spec against `df`.

    Recognized spec["type"]: "aggregate" | "top_n" | "correlation" | "describe".
    Any error is captured and returned rather than raised, since specs come
    from an LLM and a bad column name shouldn't fail the whole KPI request.
    """
    spec_type = spec.get("type")
    try:
        if spec_type == "aggregate":
            return {"ok": True, "result": aggregate(
                df, group_by=spec["group_by"], agg=spec["agg"], metric=spec.get("metric"),
            )}
        if spec_type == "top_n":
            return {"ok": True, "result": top_n(
                df, column=spec["column"], n=int(spec.get("n", 10)),
                ascending=bool(spec.get("ascending", False)),
            )}
        if spec_type == "correlation":
            return {"ok": True, "result": correlation(df, spec["column_a"], spec["column_b"])}
        if spec_type == "describe":
            return {"ok": True, "result": describe(df, spec["column"])}
        return {"ok": False, "error": f"Unknown computation type '{spec_type}'."}
    except (AnalyticsError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}
