"""DATA PROFILING: per-column statistics used by the UI's dataset summary,
by the cleaning stage (to decide what needs fixing), and as context handed
to Claude for KPI generation.
"""
import math

import pandas as pd


def _to_native(value):
    """numpy/pandas scalars -> plain Python, so the profile is JSON-safe."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            return str(value)
    return value


def profile_dataframe(df: pd.DataFrame, max_top_values: int = 5, max_samples: int = 5) -> dict:
    columns = []
    for col in df.columns:
        series = df[col]
        non_null = series.dropna()
        col_profile = {
            "name": str(col),
            "dtype": str(series.dtype),
            "null_count": int(series.isna().sum()),
            "null_pct": round(float(series.isna().mean() * 100), 2) if len(series) else 0.0,
            "unique_count": int(non_null.nunique()),
            "sample_values": [_to_native(v) for v in non_null.head(max_samples).tolist()],
        }

        if pd.api.types.is_numeric_dtype(series) and len(non_null):
            desc = non_null.describe()
            col_profile["numeric_stats"] = {
                "min": _to_native(desc.get("min")),
                "max": _to_native(desc.get("max")),
                "mean": round(_to_native(desc.get("mean")) or 0.0, 4),
                "median": _to_native(non_null.median()),
                "std": round(_to_native(desc.get("std")) or 0.0, 4),
            }
        elif len(non_null):
            top = non_null.astype(str).value_counts().head(max_top_values)
            col_profile["top_values"] = [
                {"value": str(idx), "count": int(cnt)} for idx, cnt in top.items()
            ]

        columns.append(col_profile)

    duplicate_rows = int(df.duplicated().sum())
    return {
        "row_count": int(df.shape[0]),
        "column_count": int(df.shape[1]),
        "duplicate_row_count": duplicate_rows,
        "columns": columns,
    }
