"""The one rule for aggregating a merged column, shared by every KPI surface.

Census describes a district once; HMIS describes it once per reporting period.
The merge therefore repeats each district's census values across that
district's HMIS rows, so summing a census column over the merged frame counts
every district as many times as it has periods — the error that put total
population near 4.4 billion against India's actual 1,210,854,977.

The rule is one sentence: **sum each column over the frame that column belongs
in.** A census column is summed over one row per district; an HMIS column is
summed over every row, because its periods genuinely add up.

The three KPI surfaces — the 28 dashboard cards, the template's 227
indicators, and the dynamic recommender — all aggregate through this module,
so the rule is stated once and cannot drift between them.
"""
import pandas as pd

# The merge suffixes each warehouse's columns; the suffix is what says which
# frame a column belongs in.
CENSUS_SUFFIX = "_census"

# Preference order for the key identifying one place. unique_id is the
# crosswalk id both warehouses share; the others are fallbacks for a frame
# that predates it.
PLACE_KEYS = ("unique_id", "district_id_census", "district_name_census")


def is_census(column: str) -> bool:
    return column.endswith(CENSUS_SUFFIX)


def one_row_per_place(df: pd.DataFrame) -> pd.DataFrame:
    """The frame reduced to a single row per district.

    Returned unchanged when no key identifies a place — better to aggregate a
    frame we cannot deduplicate than to silently drop rows on a guess.
    """
    for key in PLACE_KEYS:
        if key in df.columns:
            return df.drop_duplicates(subset=[key])
    return df


def frame_for(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """The frame `column` should be aggregated over."""
    return one_row_per_place(df) if is_census(column) else df


def series(df: pd.DataFrame, column: str) -> pd.Series:
    """`column` as numbers, over the frame it belongs in."""
    return pd.to_numeric(frame_for(df, column)[column], errors="coerce")


def total(df: pd.DataFrame, column: str) -> float | None:
    """Sum of `column`, counting each district once if it is a census column."""
    value = series(df, column).sum()
    return float(value) if pd.notna(value) else None


def ratio(
    df: pd.DataFrame, numerator: str, denominator: str, scale: float = 1
) -> tuple[float | None, str | None]:
    """sum(numerator) / sum(denominator) * scale. Returns (value, error).

    Each side is summed over its own frame, which matters only when the two
    come from different sources: OPD utilisation is HMIS events over census
    population, so the events must keep every reporting period while the
    population is counted once. Two columns from the same source repeat
    together and cancel either way.
    """
    denominator_total = series(df, denominator).sum()
    if pd.isna(denominator_total) or not denominator_total:
        return None, f"Denominator '{denominator}' sums to zero — no value is defined."
    numerator_total = series(df, numerator).sum()
    if pd.isna(numerator_total):
        return None, f"Numerator '{numerator}' has no numeric values."
    return round(float(numerator_total / denominator_total * scale), 4), None


def note(column: str, operation: str) -> str:
    """How a value was reached, for the audit trail shown beside it."""
    suffix = " over one row per district" if is_census(column) else ""
    return f"{operation}({column}){suffix}"
