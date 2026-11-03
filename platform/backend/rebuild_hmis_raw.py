#!/usr/bin/env python
"""Rebuild hmis_raw from already-downloaded reports, one row per record.

hmis_raw previously stored the melted long form: a report listing four areas
with 162 indicators became 1,296 rows. This rebuilds it in the shape the
report itself has — one row per area per reporting period, indicators as
columns — by re-reading the cached .xls files rather than re-crawling.

Nothing is downloaded. Files absent from the cache are skipped, not fetched.

    python rebuild_hmis_raw.py --years 2015-2016,2016-2017 --dry-run
    python rebuild_hmis_raw.py --last 5
    python rebuild_hmis_raw.py --last 5 --truncate
"""
import argparse
from collections import Counter
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from app.crud import dataset as dataset_crud
from app.db.session import SessionLocal, engine
from app.models import Dataset, KpiRequest, Profile
from app.modules.hmis_warehouse import schema as S
from app.modules.ingestion.connectors import hmis_reports as hmis

REPORT_ROOT = hmis.CACHE_DIR / "Standard Reports"
DEFAULT_REPORT_TYPE = "9~F.1. Performance of Key HMIS Indicators(upto District Level)"


def discover(report_type: str, years: list[str] | None, last: int | None) -> tuple[list[Path], list[str]]:
    """Cached report files for the chosen report type and years."""
    root = REPORT_ROOT / report_type
    if not root.exists():
        raise SystemExit(f"Not cached: {root}")

    files = sorted(root.rglob("*.xls")) + sorted(root.rglob("*.xlsx"))
    available = sorted({p.relative_to(root).parts[0] for p in files})

    chosen = years if years else (available[-last:] if last else available)
    unknown = [y for y in chosen if y not in available]
    if unknown:
        raise SystemExit(f"No cached files for year(s): {', '.join(unknown)}. Have: {', '.join(available)}")

    return [p for p in files if p.relative_to(root).parts[0] in chosen], chosen


def parse_file(path: Path, root: Path) -> pd.DataFrame | None:
    """One cached report -> record rows, carrying its folder's dimensions."""
    parts = path.relative_to(root).parts  # <year>/<period>/<area>/<file>
    if len(parts) < 4:
        return None
    year, period, parent_area = parts[0], parts[1], parts[2]

    df = hmis.parse_report_long(path.read_bytes(), path.name)
    if df.empty:
        return None

    # `area` is the row label — the real geography. The folder names its
    # parent, and which of the two is a state or district follows from that.
    df["parent_area"] = parent_area
    df["level"] = hmis._row_levels(df["area"], parent_area)
    df["report_type"] = root.name
    df["report_year"] = year
    df["period"] = period
    df["source_file"] = path.name
    return hmis.to_record_rows(df)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--survey-id", type=int, required=True,
        help="Which survey these rows belong to. Required, matching POST /ingest/fetch: "
             "re-running the same id replaces that survey's rows, a different one adds "
             "a separate set.",
    )
    parser.add_argument("--report-type", default=DEFAULT_REPORT_TYPE)
    parser.add_argument("--years", default=None, help="Comma-separated report years.")
    parser.add_argument("--last", type=int, default=None, help="Use only the N most recent years.")
    parser.add_argument(
        "--recreate", action="store_true",
        help="DROP hmis_raw and the layers built from it, so the table is recreated "
             "with the new columns. TRUNCATE alone would keep the old long-format "
             "schema and graft the new columns beside the stale ones.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing.")
    args = parser.parse_args(argv)

    years = [y.strip() for y in args.years.split(",")] if args.years else None
    files, chosen = discover(args.report_type, years, args.last)
    print(f"report type : {args.report_type}")
    print(f"years       : {', '.join(chosen)}")
    print(f"cached files: {len(files)}\n")
    if not files:
        raise SystemExit("Nothing to do.")

    root = REPORT_ROOT / args.report_type
    db = SessionLocal()
    try:
        if args.recreate and not args.dry_run:
            # Every layer is derived from hmis_raw, so all of them are stale the
            # moment its shape changes. Dropping them together avoids leaving a
            # clean/master table that silently describes the old layout.
            print("dropping HMIS layers (they are all rebuilt from raw) ...")
            with engine.begin() as conn:
                for layer in S.LAYERS:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{layer}" CASCADE'))
                    print(f"   dropped {layer}")
            # The metadata rows pointed at tables that no longer exist. Their
            # profiles and KPI requests have to go first — both carry a foreign
            # key to datasets, so deleting the parent alone is rejected.
            stale = [d.id for d in db.query(Dataset.id)
                     .filter(Dataset.table_name.in_(S.LAYERS)).all()]
            if stale:
                for model in (Profile, KpiRequest):
                    db.query(model).filter(model.dataset_id.in_(stale)).delete(
                        synchronize_session=False)
                db.query(Dataset).filter(Dataset.id.in_(stale)).delete(
                    synchronize_session=False)
            db.commit()
            print(f"   removed {len(stale)} stale dataset row(s)\n")

        if not args.dry_run:
            cleared = dataset_crud.clear_survey(db, S.RAW, args.survey_id)
            if cleared:
                print(f"replacing survey {args.survey_id}: removed {cleared:,} existing row(s)\n")

        dataset = None
        if not args.dry_run:
            dataset = dataset_crud.create_dataset_shell(
                db,
                name=f"HMIS F.1 records - survey {args.survey_id} - {chosen[0]}..{chosen[-1]}",
                stage="raw", connector="hmis_reports",
                source_ref=f"cache:{args.report_type}:{','.join(chosen)}",
                table_name=S.RAW,
                survey_id=args.survey_id,
            )
            print(f"dataset {dataset.id} -> table {S.RAW}\n")

        rows = ok = 0
        skipped: Counter = Counter()
        for i, path in enumerate(files, 1):
            try:
                frame = parse_file(path, root)
            except Exception as exc:
                skipped[str(exc)[:60]] += 1
                continue
            if frame is None or frame.empty:
                skipped["empty after parse"] += 1
                continue

            ok += 1
            rows += len(frame)
            if dataset is not None:
                dataset_crud.append_dataframe(db, dataset, frame)
            if i % 50 == 0 or i == len(files):
                print(f"  [{i}/{len(files)}] files ok={ok} rows={rows:,}")

        print(f"\nparsed {ok}/{len(files)} files -> {rows:,} rows")
        if skipped:
            print("skipped:")
            for reason, n in skipped.most_common(5):
                print(f"   {n:>4}  {reason}")

        if dataset is not None:
            dataset.notes = (f"Rebuilt from cache: {ok} F.1 report(s), "
                             f"years {chosen[0]}..{chosen[-1]}, one row per record.")
            db.add(dataset)
            db.commit()
            db.refresh(dataset)
            print(f"\nhmis_raw now holds {dataset.row_count:,} rows x {dataset.column_count} columns")
        else:
            print("\n(dry run — nothing written)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
