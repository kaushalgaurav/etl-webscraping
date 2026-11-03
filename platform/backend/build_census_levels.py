#!/usr/bin/env python
"""Build India- and State-level Census 2011 tables from the district table.

The Census NADA API's PC11_PCA-TV table only serves geo_level 2 and below —
`geo_level=1` returns zero rows. India and State figures are therefore derived
here by summing districts, which partition states exactly. The check at the
end verifies the national total against the published 1,210,854,977.

    python build_census_levels.py                 # uses the stored district dataset
    python build_census_levels.py --csv FILE.csv  # or an exported district CSV
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

from app.core.config import EXPORTS_DIR
from app.crud import dataset as dataset_crud
from app.db.session import SessionLocal
from app.models import Dataset

# Published Census 2011 population of India — used as an integrity check.
OFFICIAL_INDIA_POPULATION = 1_210_854_977

# Identifier columns that must not be summed.
ID_COLUMNS = {"state", "district", "state_name", "district_name", "urbrur"}


def load_district_frame(csv_path: str | None) -> pd.DataFrame:
    if csv_path:
        return pd.read_csv(csv_path)

    db = SessionLocal()
    try:
        dataset = (
            db.query(Dataset)
            .filter(Dataset.connector == "census_india", Dataset.stage == "clean")
            .order_by(Dataset.row_count.desc(), Dataset.created_at.desc())
            .first()
        )
        if dataset is None:
            raise SystemExit(
                "No cleaned census dataset found. Run the 'Census 2011 - All India "
                "districts (PCA)' source first, or pass --csv."
            )
        print(f"[source]  {dataset.name} ({dataset.row_count} rows)")
        return dataset_crud.load_dataframe(dataset)
    finally:
        db.close()


def population_column(df: pd.DataFrame) -> str:
    """Find the total-population column in raw or cleaned naming."""
    for candidate in ("total_population_both_sexes", "Total population - Both sexes"):
        if candidate in df.columns:
            return candidate
    matches = [c for c in df.columns if "population" in c.lower() and "both" in c.lower()]
    if not matches:
        raise SystemExit(f"No total-population column found in: {list(df.columns)[:12]}")
    return matches[0]


def numeric_indicator_columns(df: pd.DataFrame) -> list[str]:
    return [
        c for c in df.columns
        if c not in ID_COLUMNS and pd.api.types.is_numeric_dtype(df[c])
    ]


def build_state_level(df: pd.DataFrame, state_col: str) -> pd.DataFrame:
    indicators = numeric_indicator_columns(df)
    state = df.groupby(state_col, dropna=False)[indicators].sum().reset_index()
    state.insert(1, "districts_included", df.groupby(state_col, dropna=False).size().values)
    return state.sort_values(state_col).reset_index(drop=True)


def build_india_level(df: pd.DataFrame) -> pd.DataFrame:
    indicators = numeric_indicator_columns(df)
    india = df[indicators].sum().to_frame().T
    india.insert(0, "area_name", "India")
    india.insert(1, "districts_included", len(df))
    return india


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", default=None, help="District CSV to aggregate instead of the DB.")
    parser.add_argument("--out", default=str(EXPORTS_DIR / "api"), help="Output directory.")
    args = parser.parse_args(argv)

    df = load_district_frame(args.csv)
    state_col = "state_name" if "state_name" in df.columns else "state"
    pop_col = population_column(df)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- State level -----------------------------------------------------
    state_df = build_state_level(df, state_col)
    state_path = out_dir / "api__census2011_india_state_level.csv"
    state_path.write_bytes(state_df.to_csv(index=False).encode("utf-8-sig"))
    print(f"[state]   {state_df.shape[0]} states/UTs x {state_df.shape[1]} cols -> {state_path.name}")

    # ---- India level -----------------------------------------------------
    india_df = build_india_level(df)
    india_path = out_dir / "api__census2011_india_national_level.csv"
    india_path.write_bytes(india_df.to_csv(index=False).encode("utf-8-sig"))
    print(f"[india]   1 row x {india_df.shape[1]} cols -> {india_path.name}")

    # ---- Integrity check -------------------------------------------------
    total = int(india_df[pop_col].iloc[0])
    delta = total - OFFICIAL_INDIA_POPULATION
    print(f"\n[check]   summed population : {total:,}")
    print(f"[check]   official (Census)  : {OFFICIAL_INDIA_POPULATION:,}")
    if delta == 0:
        print("[check]   EXACT MATCH - districts sum correctly to the national total.")
        return 0

    print(f"[check]   MISMATCH of {delta:+,} - district data may be incomplete.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
