"""Filtered, grouped KPI aggregation for configurable dashboards.

A dashboard sends declarative KPI specs (numerator / denominator column names,
one aggregation from a fixed list, a scale), plus how its merged frame is
built, and gets values overall and per group. Nothing executable is accepted:
every column is checked against the real frame and every aggregation against
AGGREGATIONS.

Why each column is aggregated over its own *source* frame
---------------------------------------------------------
dashboard_master is an outer join of several warehouses. Census describes a
district once; HMIS describes it once per reporting period, and its values are
cumulative year-to-date (MonthUpToJune ... MonthUpToMarch). The join repeats
census values on every HMIS row, and where the crosswalk key collides it even
cross-joins two districts. So no single row key identifies a place.

Each source therefore declares its own column suffix and place key, and
optionally its time columns. A column is aggregated over the rows of its
source, de-duplicated to one row per that source's place, and — for a
time-varying source — restricted to the one requested time slice. Census
population is counted once per census district, HMIS events once per HMIS
district for the chosen period, and the two never multiply each other.

Dimensions (state, district, ...) are coalesced across columns, so a filter or
grouping works whichever source a row came from; `tag` prefixes values with
the column they came from when the sources number places differently.
"""
from __future__ import annotations

import pandas as pd

AGGREGATIONS = ("sum", "mean", "median", "min", "max", "count", "ratio_of_sums")
MAX_GROUPS = 5000
DIM_PREFIX = "__dim__"


class ExplorerError(ValueError):
    """A request that names unknown columns or unsupported operations."""


def norm(value) -> str | None:
    """Comparable text for a cell: 10, 10.0 and '10' all become '10'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text == "" or text.lower() == "nan":
        return None
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except ValueError:
        pass
    return text


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value))


def _clean(value):
    """JSON-safe cell: NaN -> None."""
    return None if _missing(value) else value


def _check(df: pd.DataFrame, columns, what: str) -> None:
    unknown = sorted({c for c in columns if c and c not in df.columns})
    if unknown:
        raise ExplorerError(f"Unknown {what} column(s): {', '.join(unknown)}")


class Frame:
    """The merged frame plus its sources and coalesced dimensions."""

    def __init__(self, df: pd.DataFrame, sources: list[dict], dimensions: dict[str, dict]):
        if not sources:
            raise ExplorerError("At least one source is required.")
        for source in sources:
            _check(df, source.get("place_key") or [], "place key")
            _check(df, source.get("time") or [], "time")
        for name, dim in dimensions.items():
            _check(df, dim.get("columns") or [], f"dimension '{name}'")
        self.sources = sorted(sources, key=lambda s: len(s.get("suffix") or ""), reverse=True)
        self.dimensions = dimensions
        self.df = df.copy()
        for name, dim in dimensions.items():
            self.df[DIM_PREFIX + name] = self._coalesce(dim)
        self._frames: dict = {}

    def _coalesce(self, dim: dict) -> pd.Series:
        out = pd.Series([None] * len(self.df), index=self.df.index, dtype=object)
        for column in dim["columns"]:
            values = self.df[column].map(norm)
            if dim.get("tag"):
                values = values.map(lambda v, c=column: None if _missing(v) else f"{c}:{v}")
            out = out.where(out.notna(), values)
        return out

    def filter(self, filters: dict[str, list]) -> None:
        for name, values in (filters or {}).items():
            if name not in self.dimensions:
                raise ExplorerError(f"Unknown filter dimension '{name}'.")
            if not values:
                continue
            wanted = {norm(v) for v in values}
            self.df = self.df[self.df[DIM_PREFIX + name].isin(wanted)]
        self._frames = {}

    def source_of(self, column: str) -> dict:
        for source in self.sources:
            if column.endswith(source.get("suffix") or ""):
                return source
        return self.sources[-1]

    def frame(self, source: dict, time: dict | None) -> pd.DataFrame:
        """Rows of `source`: one per its place, within `time` if it varies in time."""
        time_cols = source.get("time") or []
        slice_ = tuple(norm((time or {}).get(c)) for c in time_cols)
        key = (id(source), slice_)
        if key not in self._frames:
            place = source.get("place_key") or []
            rows = self.df.dropna(subset=place, how="all") if place else self.df
            if time_cols:
                if any(v is None for v in slice_):
                    raise ExplorerError(
                        f"Columns ending '{source.get('suffix')}' vary over {', '.join(time_cols)}; "
                        "pass a value for each in `time`."
                    )
                for column, value in zip(time_cols, slice_):
                    rows = rows[rows[column].map(norm) == value]
            if place:
                rows = rows.drop_duplicates(subset=place)
            self._frames[key] = rows
        return self._frames[key]

    def stats(self, column: str, by: list[str], time: dict | None) -> pd.DataFrame:
        """sum/count/mean/median/min/max of `column` over its source frame, per `by`."""
        frame = self.frame(self.source_of(column), time)
        values = pd.to_numeric(frame[column], errors="coerce")
        keys = [frame[DIM_PREFIX + b] for b in by] if by else [pd.Series("all", index=frame.index)]
        grouped = values.groupby(keys, dropna=True)
        return pd.DataFrame({
            "sum": grouped.sum(min_count=1), "count": grouped.count(),
            "mean": grouped.mean(), "median": grouped.median(),
            "min": grouped.min(), "max": grouped.max(),
        })


def _validate(df: pd.DataFrame, kpis: list[dict]) -> None:
    for spec in kpis:
        aggregation = spec.get("aggregation")
        if aggregation not in AGGREGATIONS:
            raise ExplorerError(f"KPI '{spec.get('id')}': unsupported aggregation '{aggregation}'.")
        if not spec.get("numerator"):
            raise ExplorerError(f"KPI '{spec.get('id')}': a numerator column is required.")
        if aggregation == "ratio_of_sums" and not spec.get("denominator"):
            raise ExplorerError(f"KPI '{spec.get('id')}': ratio_of_sums needs a denominator column.")
        _check(df, [spec.get("numerator"), spec.get("denominator")], f"KPI '{spec.get('id')}'")


def _kpi(frame: Frame, spec: dict, by: list[str], time: dict | None, pair_on: str | None) -> dict:
    """{group key tuple: result} for one KPI."""
    aggregation, scale = spec["aggregation"], float(spec.get("scale") or 1)
    out: dict = {}

    if aggregation != "ratio_of_sums":
        stats = frame.stats(spec["numerator"], by, time)
        for key, row in stats.iterrows():
            reporting = int(row["count"])
            if aggregation == "count":
                value = reporting
            else:
                raw = row[aggregation]
                value = None if pd.isna(raw) else round(float(raw) * scale, 4)
            out[key] = {
                "value": value,
                "numerator": None if aggregation != "sum" or pd.isna(row["sum"]) else float(row["sum"]),
                "denominator": None,
                "reporting": reporting,
                "note": None if value is not None else "No numeric values for this selection.",
            }
        return out

    # Ratio: pair numerator and denominator per unit (e.g. district) so a place
    # that reports no numerator does not inflate the base.
    inner = by + ([pair_on] if pair_on and pair_on not in by else [])
    num = frame.stats(spec["numerator"], inner, time)
    den = frame.stats(spec["denominator"], inner, time)
    both = num[["sum", "count"]].join(den[["sum"]], lsuffix="_n", rsuffix="_d", how="inner")
    both = both[(both["count"] > 0) & both["sum_n"].notna() & both["sum_d"].notna()]
    if by and len(inner) > len(by):
        both = both.groupby(level=list(range(len(by)))).sum()
    elif not by:
        both = pd.DataFrame([both.sum()], index=["all"]) if len(both) else both
    for key, row in both.iterrows():
        n, d = float(row["sum_n"]), float(row["sum_d"])
        value = round(n / d * scale, 4) if d else None
        out[key] = {
            "value": value, "numerator": n, "denominator": d, "reporting": int(row["count"]),
            "note": None if d else "Denominator sums to zero; no value is defined.",
        }
    return out


EMPTY = {"value": None, "numerator": None, "denominator": None, "reporting": 0,
         "note": "No data for this selection."}


def aggregate(
    df: pd.DataFrame,
    kpis: list[dict],
    sources: list[dict],
    dimensions: dict[str, dict] | None = None,
    filters: dict[str, list] | None = None,
    time: dict | None = None,
    group_by: list[str] | None = None,
    labels: dict[str, list[str]] | None = None,
    pair_on: str | None = None,
) -> dict:
    """Overall and per-group values of every KPI for one filtered selection."""
    dimensions, group_by, labels = dict(dimensions or {}), list(group_by or []), dict(labels or {})
    for name in group_by + ([pair_on] if pair_on else []):
        if name not in dimensions:
            raise ExplorerError(f"Unknown dimension '{name}'.")
    for name, columns in labels.items():
        dimensions.setdefault(f"label:{name}", {"columns": columns})
    _validate(df, kpis)

    frame = Frame(df, sources, dimensions)
    frame.filter(filters or {})

    overall = {spec["id"]: _kpi(frame, spec, [], time, pair_on).get("all", EMPTY) for spec in kpis}
    result = {"rows_matched": int(len(frame.df)), "overall": overall, "groups": []}
    if not group_by:
        return result

    per_kpi = {spec["id"]: _kpi(frame, spec, group_by, time, pair_on) for spec in kpis}
    keys = sorted({k for values in per_kpi.values() for k in values}, key=lambda k: str(k))
    if len(keys) > MAX_GROUPS:
        raise ExplorerError(f"Grouping yields {len(keys)} groups; the limit is {MAX_GROUPS}.")

    label_cols = [DIM_PREFIX + f"label:{n}" for n in labels]
    lookup = (frame.df[[DIM_PREFIX + g for g in group_by] + label_cols]
              .dropna(subset=[DIM_PREFIX + g for g in group_by])
              .drop_duplicates(subset=[DIM_PREFIX + g for g in group_by]))
    lookup = {tuple(r[:len(group_by)]): r[len(group_by):] for r in lookup.itertuples(index=False)}
    for key in keys:
        tup = key if isinstance(key, tuple) else (key,)
        names = lookup.get(tup, [None] * len(labels))
        result["groups"].append({
            "key": {g: _clean(v) for g, v in zip(group_by, tup)},
            "labels": {n: _clean(v) for n, v in zip(labels, names)},
            "kpis": {kid: values.get(key, EMPTY) for kid, values in per_kpi.items()},
        })
    return result


def time_varying(sources: list[dict], spec: dict) -> bool:
    """Whether any column of the KPI comes from a source that varies in time."""
    ordered = sorted(sources, key=lambda s: len(s.get("suffix") or ""), reverse=True)

    def source(column):
        return next((s for s in ordered if column.endswith(s.get("suffix") or "")), ordered[-1])

    return any(source(c).get("time") for c in (spec.get("numerator"), spec.get("denominator")) if c)


def dimensions(
    df: pd.DataFrame,
    dims: dict[str, dict],
    labels: dict[str, list[str]] | None = None,
    time_columns: list[str] | None = None,
    max_combos: int = 20000,
) -> dict:
    """Distinct coalesced dimension combos (for cascading filters), time slices,
    and every column with whether it is numeric."""
    labels, time_columns = dict(labels or {}), list(time_columns or [])
    _check(df, time_columns, "time")
    all_dims = {**dims, **{f"label:{n}": {"columns": c} for n, c in labels.items()}}
    frame = Frame(df, [{"suffix": ""}], all_dims)

    kinds = []
    for name in df.columns:
        series = df[name]
        non_null = int(series.notna().sum())
        numeric = pd.to_numeric(series, errors="coerce").notna().sum()
        kinds.append({"name": name, "numeric": bool(non_null and numeric == non_null), "non_null": non_null})

    cols = [DIM_PREFIX + n for n in all_dims]
    combos = []
    if cols:
        distinct = frame.df[cols].dropna(how="all").drop_duplicates().head(max_combos)
        rename = {DIM_PREFIX + n: n for n in all_dims}
        combos = [{rename[c]: _clean(v) for c, v in row.items()} for row in distinct.to_dict(orient="records")]

    slices = []
    if time_columns:
        distinct = df[time_columns].dropna(how="any").drop_duplicates()
        slices = [{c: norm(v) for c, v in row.items()} for row in distinct.to_dict(orient="records")]

    return {"rows": int(len(df)), "columns": kinds, "combos": combos, "time_slices": slices,
            "truncated": len(combos) >= max_combos}
