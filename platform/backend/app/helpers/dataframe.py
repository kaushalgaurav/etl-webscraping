"""DataFrame helpers shared across modules."""
import re

import pandas as pd


def snake_case(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name)).strip()
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name.lower() or "column"


def json_safe_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list[dict] with NaN/NaT replaced by None.

    float('nan') isn't valid JSON and Starlette's JSONResponse renders with
    allow_nan=False, so any NaN reaching a response body raises
    "Out of range float values are not JSON compliant".
    """
    safe = df.astype(object).where(pd.notnull(df), None)
    return safe.to_dict(orient="records")
