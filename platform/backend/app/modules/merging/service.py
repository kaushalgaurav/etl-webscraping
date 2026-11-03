"""DATA MERGING: combine two or more clean datasets into one master dataset.

Join keys are resolved through the semantic layer, so a key named once
("district") finds the right column in every dataset even when they call it
`District Name`, `dist_name`, or `DISTRICT_NM`.

Strategy (in order of preference):
  1. Explicit join_key given -> resolve it per dataset, then outer merge.
  2. No key given -> try each key-like canonical field (state, district,
     block, year) and use the first one present in every dataset.
  3. Otherwise -> vertical concat (union of columns), tagging each row with
     its source dataset.
"""
import functools

import pandas as pd

from app.modules.merging import semantic
from app.helpers.dataframe import snake_case

AUTO_KEY_CANDIDATES = semantic.KEY_LIKE_FIELDS


class MergeError(Exception):
    pass


def _normalize_key(series: pd.Series) -> pd.Series:
    """Casefold a key column for joining, keeping nulls null.

    `astype(str)` alone would turn NaN into the literal "nan", which then joins
    to every other null key as if they were the same place — and a float id
    into "351.0", which never matches an integer "351".
    """
    missing = series.isna()
    if pd.api.types.is_float_dtype(series):
        # Integer ids arrive as float whenever the column holds a null.
        whole = series.notna() & (series % 1 == 0)
        text = series.astype(object).astype(str)
        text[whole] = series[whole].astype("int64").astype(str)
    else:
        text = series.astype(str)
    text = text.str.strip().str.lower()
    return text.mask(missing)


def _resolve_across_frames(
    frames: list[pd.DataFrame], key: str
) -> tuple[list[str], list[float]] | None:
    """Resolve `key` to one actual column per frame, or None if any frame lacks it."""
    columns, confidences = [], []
    for frame in frames:
        col, confidence = semantic.resolve_key_column(list(frame.columns), key)
        if col is None:
            return None
        columns.append(col)
        confidences.append(confidence)
    return columns, confidences


def merge_dataframes(
    frames: list[pd.DataFrame],
    names: list[str],
    join_key: str | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    if len(frames) < 2:
        raise MergeError("Merging requires at least two datasets.")

    report: list[str] = []
    key: str | None = None
    key_columns: list[str] | None = None
    confidences: list[float] = []

    if join_key:
        key = snake_case(join_key)
        resolved = _resolve_across_frames(frames, join_key)
        if resolved is None:
            missing = [
                n for n, f in zip(names, frames)
                if semantic.resolve_key_column(list(f.columns), join_key)[0] is None
            ]
            raise MergeError(
                f"Join key '{join_key}' could not be resolved in dataset(s): {', '.join(missing)}."
            )
        key_columns, confidences = resolved
    else:
        for candidate in AUTO_KEY_CANDIDATES:
            resolved = _resolve_across_frames(frames, candidate)
            if resolved is not None:
                key = candidate
                key_columns, confidences = resolved
                break

    if key is not None and key_columns is not None:
        prepared = []
        dropped: list[str] = []
        for name, frame, key_col in zip(names, frames, key_columns):
            frame = frame.copy()
            frame["_merge_key"] = _normalize_key(frame[key_col])

            # A row with no key cannot join, but an outer merge would still
            # carry it through — and in this data those rows are the source's
            # own aggregates. HMIS publishes a state total (`_A & N Islands`)
            # beside its districts; the total has no district id, so it would
            # sit in dashboard_master next to the three districts it is the sum
            # of, and every column total would count A & N twice.
            blank = frame["_merge_key"].isna()
            if blank.any():
                dropped.append(f"{name}: {int(blank.sum()):,}")
                frame = frame[~blank]

            rename = {
                c: f"{c}__{name}" for c in frame.columns if c not in (key_col, "_merge_key")
            }
            frame = frame.rename(columns=rename)
            prepared.append(frame.drop(columns=[key_col]))

        if dropped:
            report.append(
                f"Dropped rows with no '{key}' ({'; '.join(dropped)}) — unjoinable, "
                "and typically a source's own subtotal of rows already present."
            )

        master = functools.reduce(
            lambda left, right: pd.merge(left, right, on="_merge_key", how="outer"),
            prepared,
        )
        master = master.rename(columns={"_merge_key": key})
        master = master[[key] + [c for c in master.columns if c != key]]

        resolution = ", ".join(
            f"{name}.{col} ({conf:.0%})"
            for name, col, conf in zip(names, key_columns, confidences)
        )
        report.append(
            f"Outer-merged {len(frames)} dataset(s) on canonical key '{key}' "
            f"[{resolution}] -> {master.shape[0]} rows, {master.shape[1]} columns."
        )
        return master.reset_index(drop=True), report

    # Fallback: vertical concat with a source tag.
    tagged = []
    for name, frame in zip(names, frames):
        frame = frame.copy()
        frame["_source_dataset"] = name
        tagged.append(frame)
    master = pd.concat(tagged, ignore_index=True, sort=False)
    report.append(
        f"No common join key found across {len(frames)} dataset(s); stacked "
        f"rows instead ({master.shape[0]} rows, {master.shape[1]} columns), "
        "tagged by '_source_dataset'."
    )
    return master, report
