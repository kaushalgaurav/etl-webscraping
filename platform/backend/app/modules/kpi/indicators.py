"""Compute the template's Key Performance Indicators from dashboard_master.

Deterministic throughout: every value comes from a formula declared in
`app.constants.kpi_catalog`, evaluated with pandas. No model is involved and
no SQL is generated, so the same survey always yields the same numbers and
each one can be traced to its columns.

One row out per (district, indicator), which is what the template's sheets
want — a district down the side, indicators across the top.
"""
import numpy as np
import pandas as pd

from app.constants.kpi_catalog import load_catalog
from app.modules.kpi import aggregation

# Columns that identify the place a value belongs to, in preference order.
IDENTITY_COLUMNS = (
    "unique_id", "state_id_census", "district_id_census",
    "state_name_census", "district_name_census",
)


class IndicatorError(Exception):
    pass


def _series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        raise IndicatorError(f"column '{column}' is not in the merged data")
    return df[column]


def _numeric(df: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(_series(df, column), errors="coerce")


def _divide(numerator: pd.Series, denominator: pd.Series, scale: float) -> pd.Series:
    """numerator / denominator * scale, with a zero denominator giving null.

    A zero denominator is missing data, not infinity — a district with no
    recorded live births has no infant mortality rate, it does not have an
    infinite one.
    """
    denominator = denominator.replace(0, np.nan)
    return (numerator / denominator) * scale


def evaluate(df: pd.DataFrame, formula: dict) -> pd.Series:
    """One indicator's value for every row of the merged frame."""
    kind = formula["kind"]

    if kind == "column":
        return _series(df, formula["column"])

    if kind == "ratio":
        return _divide(
            _numeric(df, formula["numerator"]),
            _numeric(df, formula["denominator"]),
            formula.get("scale", 1),
        )

    if kind == "sum":
        missing = [c for c in formula["columns"] if c not in df.columns]
        if missing:
            raise IndicatorError(f"column(s) {', '.join(missing)} are not in the merged data")
        return sum(_numeric(df, c).fillna(0) for c in formula["columns"])

    if kind == "ratio_over_difference":
        denominator = (
            _numeric(df, formula["denominator_minuend"])
            - _numeric(df, formula["denominator_subtrahend"])
        )
        return _divide(_numeric(df, formula["numerator"]), denominator,
                       formula.get("scale", 1))

    if kind == "ratio_of_difference":
        minuend = _numeric(df, formula["minuend"])
        return _divide(
            minuend - _numeric(df, formula["subtrahend"]),
            _numeric(df, formula["denominator"]),
            formula.get("scale", 1),
        )

    raise IndicatorError(f"unknown formula kind '{kind}'")


def _identity(df: pd.DataFrame) -> pd.DataFrame:
    present = [c for c in IDENTITY_COLUMNS if c in df.columns]
    if not present:
        raise IndicatorError(
            "The merged data has none of the identity columns "
            f"({', '.join(IDENTITY_COLUMNS)}); run POST /datasets/merge first."
        )
    return df[present].rename(columns=lambda c: c.replace("_census", ""))


def compute(df: pd.DataFrame, themes: list[str] | None = None) -> dict:
    """Every computable indicator, valued per district.

    Returns the long form — one record per (place, indicator) — plus a summary
    of what was computed, what was only a proxy, and what no ingested source
    can answer yet.
    """
    identity = _identity(df)
    catalog = load_catalog()
    if themes:
        wanted = {t.lower() for t in themes}
        catalog = [c for c in catalog if c["theme"].lower() in wanted]
        if not catalog:
            raise IndicatorError(f"No indicators for theme(s): {', '.join(themes)}")

    records: list[dict] = []
    computed, proxied, unavailable, failed = [], [], [], []

    for entry in catalog:
        if entry["formula"] is None:
            unavailable.append({"id": entry["id"], "name": entry["name"],
                                "theme": entry["theme"], "reason": entry["reason"]})
            continue
        try:
            values = evaluate(df, entry["formula"])
        except IndicatorError as exc:
            # A declared formula whose column is absent is a catalog/data
            # mismatch, not a missing source — reported separately so it is
            # obvious which one happened.
            failed.append({"id": entry["id"], "name": entry["name"],
                           "theme": entry["theme"], "error": str(exc)})
            continue

        (proxied if entry["status"] == "proxy" else computed).append(entry["id"])
        for position, value in enumerate(values):
            if pd.isna(value):
                value = None
            elif isinstance(value, (np.integer, np.floating)):
                value = round(float(value), 4)
            records.append({
                **identity.iloc[position].to_dict(),
                "indicator_id": entry["id"],
                "indicator": entry["name"],
                "theme": entry["theme"],
                "value": value,
                "unit": entry["unit"],
                "status": entry["status"],
                "basis": entry["basis"],
            })

    return {
        "places": int(identity.shape[0]),
        "summary": {
            "total_indicators": len(catalog),
            "computed": len(computed),
            "proxy": len(proxied),
            "unavailable": len(unavailable),
            "failed": len(failed),
        },
        "values": records,
        "unavailable": unavailable,
        "failed": failed,
    }


def wide(records: list[dict]) -> list[dict]:
    """The long values re-laid as the template's sheets want them.

    One row per place, one column per indicator — the shape you paste into
    `General_Population_Template.xlsx`.
    """
    if not records:
        return []
    frame = pd.DataFrame(records)
    index = [c for c in ("unique_id", "state_id", "district_id",
                         "state_name", "district_name") if c in frame.columns]
    if not index:
        raise IndicatorError("Computed values carry no identity columns to pivot on.")

    # Pivot on one key, then put the other identity columns back. Pivoting on
    # all of them at once builds the cartesian product of every level —
    # 3 districts became 27 rows — because a multi-level index with dropna=False
    # keeps combinations that no row ever had.
    key = index[0]
    pivot = (
        frame.pivot_table(index=key, columns="indicator", values="value", aggfunc="first")
        .reset_index()
    )
    pivot.columns.name = None

    identity = frame[index].drop_duplicates(subset=[key])
    wide_frame = identity.merge(pivot, on=key, how="left")
    return wide_frame.astype(object).where(pd.notnull(wide_frame), None).to_dict(orient="records")


# --------------------------------------------------------------------------
# The 28 fixed dashboard KPIs (app.constants.dashboard_kpis).
# --------------------------------------------------------------------------

SCALES = {"weighted_pct": 100, "per_1000": 1000, "per_100000": 100_000}


def _snapshot(df: pd.DataFrame, kpi: dict, resolved: dict) -> tuple[float | None, str | None]:
    """One dashboard KPI's headline number. Returns (value, unavailable_reason)."""
    from app.constants import dashboard_kpis as registry

    agg = kpi["aggregation"]

    if agg == "difference":
        left, right = resolved.get(kpi["minuend"]), resolved.get(kpi["subtrahend"])
        if left is None or right is None:
            return None, "Depends on another KPI that could not be computed."
        return round(left - right, 4), None

    missing = [c for c in registry.required_columns(kpi) if c not in df.columns]
    if missing:
        # The column is mapped in hmis_indicators.tsv but absent from this
        # survey's merged rows — the reports loaded did not carry it.
        return None, f"Column(s) not in this survey's data: {', '.join(missing)}"

    if agg == "sum":
        value = aggregation.total(df, kpi["column"])
        return (round(value, 4), None) if value is not None else (None, "No numeric values.")

    if agg == "official_pct":
        # HMIS publishes the percentage itself; without its numerator and
        # denominator this can only be averaged, not population-weighted.
        mean = aggregation.series(df, kpi["column"]).mean()
        return (round(float(mean), 4), None) if pd.notna(mean) else (None, "No reported values.")

    if agg == "literacy_pct":
        # Literates over the population aged 7+, which is the official Census
        # definition. Both columns are census, so each is counted once per
        # district before the subtraction.
        base = (aggregation.series(df, kpi["denominator"]).sum()
                - aggregation.series(df, kpi["denominator_subtrahend"]).sum())
        if not base or pd.isna(base):
            return None, f"'{kpi['denominator']}' net of under-7s is zero."
        value = aggregation.series(df, kpi["numerator"]).sum() / base * 100
        return (round(float(value), 4), None) if pd.notna(value) else (None, "No values.")

    scale = SCALES.get(agg)
    if scale is None:
        return None, f"Unknown aggregation '{agg}'."
    return aggregation.ratio(df, kpi["numerator"], kpi["denominator"], scale)


def dashboard_snapshot(df: pd.DataFrame) -> dict:
    """The 28 dashboard KPIs with their latest snapshot values.

    Mirrors the specification sheet's columns — id, name, domain, aggregation,
    source/formula, business meaning, latest snapshot — so the output can be
    read straight across against it.
    """
    from app.constants import dashboard_kpis as registry

    rows, resolved = [], {}
    # Two passes: `difference` KPIs read values the first pass computed.
    for kpi in sorted(registry.KPIS, key=lambda k: k["aggregation"] == "difference"):
        value, reason = _snapshot(df, kpi, resolved)
        resolved[kpi["id"]] = value
        row = {
            "id": kpi["id"], "name": kpi["name"], "domain": kpi["domain"],
            "aggregation": kpi["aggregation"],
            "source_formula": registry.source_formula(kpi),
            "meaning": kpi["meaning"], "unit": kpi["unit"],
            "visualisation": kpi["visualisation"],
            "latest_snapshot": value, "available": value is not None,
        }
        if reason:
            row["reason"] = reason
        # A coverage percentage above 100 is real in HMIS (the denominator is
        # estimated live births, the numerator a facility count) but it is not
        # a coverage level — say so rather than let a card read 124%.
        if value is not None and kpi["unit"] == "%" and value > 100:
            row["data_quality_flag"] = (
                "Exceeds 100% — HMIS counts services against an estimated "
                "denominator, so treat as reporting intensity, not coverage."
            )
        rows.append(row)

    rows.sort(key=lambda r: r["id"])
    available = sum(1 for r in rows if r["available"])
    return {
        "kpis": rows,
        "summary": {"total": len(rows), "available": available,
                    "unavailable": len(rows) - available},
    }


def dashboard_by_place(df: pd.DataFrame) -> list[dict]:
    """The same KPIs computed per district — the dashboard's data table and map."""
    identity = _identity(df)
    out = []
    for position in range(len(df)):
        row_df = df.iloc[[position]]
        snapshot = dashboard_snapshot(row_df)
        out.append({
            **identity.iloc[position].to_dict(),
            "kpis": {k["id"]: k["latest_snapshot"] for k in snapshot["kpis"]},
        })
    return out
