"""ETL: Extract (URL, CSV, PDF) → Transform → Load."""

from __future__ import annotations

import argparse

from etl.analyze import run_analyze
from etl.extract import run_extract
from etl.load import run_load
from etl.transform import run_transform


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "ETL for Health and Family Welfare: extract from URLs, CSV and PDF, "
            "transform, then load to SQLite."
        )
    )
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Reuse scraped catalog/resource metadata already in data/raw.",
    )
    args = parser.parse_args()

    extracted = run_extract(from_cache=args.from_cache)
    transformed = run_transform(
        catalogs=extracted["catalogs"],
        resources=extracted["resources"],
        csv_tables=extracted["csv_tables"],
        pdf_pages=extracted["pdf_pages"],
    )
    run_load(
        metadata=transformed["metadata"],
        tables=transformed["tables"],
        indicators=transformed["indicators"],
        pdf_pages=transformed["pdf_pages"],
        downloads=extracted["downloads"],
    )
    run_analyze(
        metadata=transformed["metadata"],
        tables=transformed["tables"],
        indicators=transformed["indicators"],
    )


if __name__ == "__main__":
    main()
