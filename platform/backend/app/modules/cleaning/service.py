"""DATA CLEANING: fix bad data, without restructuring the table.

Cleaning repairs *values* — missing markers, stray whitespace, duplicates,
numbers trapped in text, inconsistent casing. Reshaping the table (renaming,
selecting, deriving, aggregating) is the transform stage's job, so the two can
be run, reviewed and re-run independently.

Every operation is individually selectable and reports what it changed, so a
clean is never a black box.
"""
import pandas as pd

from app.constants.state_aliases import STATE_ALIASES
from app.helpers.dataframe import snake_case

NULL_LIKE = {"", "na", "n/a", "null", "none", "-", "--", "nil", "not available"}

# Applied in this order when no explicit list is given. Order matters:
# nulls must be normalized before empty rows/columns are detected, and
# numeric coercion must come after that.
DEFAULT_OPERATIONS = (
    "standardize_columns",
    "trim_whitespace",
    "normalize_nulls",
    "drop_empty_columns",
    "drop_empty_rows",
    "coerce_numeric",
    "drop_duplicates",
)

# The warehouse layers keep every column: a column empty in one fetch is still
# part of the shared schema, and dropping it changes the table's header.
WAREHOUSE_OPERATIONS = tuple(op for op in DEFAULT_OPERATIONS if op != "drop_empty_columns")

ALL_OPERATIONS =DEFAULT_OPERATIONS + ("standardize_case", "standardize_states")


def _standardize_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    original = list(df.columns)
    new_cols, seen = [], {}
    for col in original:
        base = snake_case(col)
        seen[base] = seen.get(base, 0) + 1
        new_cols.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    df.columns = new_cols
    changed = sum(1 for a, b in zip(original, new_cols) if str(a) != b)
    return df, f"Standardized {changed} column name(s) to snake_case." if changed else None


def _trim_whitespace(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    cols = df.select_dtypes(include="object").columns
    touched = 0
    for col in cols:
        before = df[col]
        df[col] = before.map(lambda v: v.strip() if isinstance(v, str) else v)
        touched += int((before.astype(str) != df[col].astype(str)).sum())
    return df, f"Trimmed whitespace on {touched:,} value(s)." if touched else None


def _normalize_nulls(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    before = int(df.isna().sum().sum())
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].map(
            lambda v: None if isinstance(v, str) and v.strip().lower() in NULL_LIKE else v
        )
    gained = int(df.isna().sum().sum()) - before
    return df, (
        f"Converted {gained:,} placeholder value(s) (e.g. 'NA', '-') to missing."
        if gained else None
    )


def _drop_empty_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    empty = [c for c in df.columns if df[c].isna().all()]
    if not empty:
        return df, None
    return df.drop(columns=empty), f"Dropped {len(empty)} empty column(s): {', '.join(map(str, empty))}."


def _drop_empty_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    before = len(df)
    df = df.dropna(how="all")
    removed = before - len(df)
    return df, f"Dropped {removed:,} empty row(s)." if removed else None


def _coerce_numeric(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    coerced = []
    for col in df.select_dtypes(include="object").columns:
        non_null = df[col].dropna()
        if non_null.empty:
            continue
        stripped = non_null.astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False)
        if pd.to_numeric(stripped, errors="coerce").notna().mean() >= 0.9:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False),
                errors="coerce",
            )
            coerced.append(str(col))
    return df, (
        f"Converted {len(coerced)} text column(s) to numeric: {', '.join(coerced)}."
        if coerced else None
    )


def _drop_duplicates(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    count = int(df.duplicated().sum())
    if not count:
        return df, None
    return df.drop_duplicates(), f"Removed {count:,} duplicate row(s)."


def _standardize_case(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    """Collapse values differing only by case/spacing onto their commonest form.

    'Bihar' and 'bihar' are one place; left alone they group and join as two.
    """
    fixed = 0
    for col in df.select_dtypes(include="object").columns:
        non_null = df[col].dropna().astype(str)
        if non_null.empty:
            continue
        key = non_null.str.strip().str.lower()
        if key.nunique() == non_null.nunique():
            continue
        # Most frequent original spelling wins for each normalized key.
        canonical = (
            pd.DataFrame({"key": key, "value": non_null.str.strip()})
            .groupby(["key", "value"]).size().reset_index(name="n")
            .sort_values("n", ascending=False).drop_duplicates("key")
            .set_index("key")["value"]
        )
        mapped = df[col].map(
            lambda v: canonical.get(str(v).strip().lower(), v) if isinstance(v, str) else v
        )
        fixed += int((df[col].astype(str) != mapped.astype(str)).sum())
        df[col] = mapped
    return df, f"Unified {fixed:,} value(s) that differed only by case or spacing." if fixed else None


def _standardize_states(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    """Map state/UT name variants onto one official spelling.

    Sources disagree ('Orissa' vs 'Odisha', 'NCT of Delhi' vs 'Delhi'), which
    silently breaks joins between Census, NFHS and HMIS data.
    """
    targets = [c for c in df.columns if snake_case(str(c)) in ("state", "state_name", "state_ut")]
    if not targets:
        return df, None

    changed = 0
    for col in targets:
        mapped = df[col].map(
            lambda v: STATE_ALIASES.get(str(v).strip().lower(), v) if isinstance(v, str) else v
        )
        changed += int((df[col].astype(str) != mapped.astype(str)).sum())
        df[col] = mapped
    return df, f"Standardized {changed:,} state/UT name(s) to their official spelling." if changed else None


OPERATIONS = {
    "standardize_columns": _standardize_columns,
    "trim_whitespace": _trim_whitespace,
    "normalize_nulls": _normalize_nulls,
    "drop_empty_columns": _drop_empty_columns,
    "drop_empty_rows": _drop_empty_rows,
    "coerce_numeric": _coerce_numeric,
    "drop_duplicates": _drop_duplicates,
    "standardize_case": _standardize_case,
    "standardize_states": _standardize_states,
}


def clean_dataframe(
    df: pd.DataFrame, operations: list[str] | None = None
) -> tuple[pd.DataFrame, list[str]]:
    """Apply cleaning operations, returning the result and what each one did."""
    selected = list(operations) if operations else list(DEFAULT_OPERATIONS)
    unknown = [op for op in selected if op not in OPERATIONS]
    if unknown:
        raise ValueError(
            f"Unknown cleaning operation(s): {', '.join(unknown)}. "
            f"Available: {', '.join(OPERATIONS)}"
        )

    df = df.copy()
    report: list[str] = []
    for name in selected:
        df, message = OPERATIONS[name](df)
        if message:
            report.append(message)

    if not report:
        report.append("No cleaning changes were necessary.")
    return df.reset_index(drop=True), report
