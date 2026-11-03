#!/usr/bin/env python
"""Fetch Census 2011 data one state at a time, resumably.

A national village-level pull is ~55.2M records / 11,039 API calls / ~63 hours,
and the API caps pages at 5,000 rows, so there is no bulk shortcut. Doing it as
one job means a failure 50 hours in costs everything. This script splits the
work by state: one CSV per state, and states already written are skipped, so an
interrupted run resumes where it stopped.

    python fetch_census_by_state.py --states 11,30,35,34
    python fetch_census_by_state.py --states all --level village_town
    python fetch_census_by_state.py --states 11 --level subdistrict

Levels: district | subdistrict | village_town | ward   (state level is not
served by this table — derive it with build_census_levels.py).
"""
import argparse
import sys
import time
from pathlib import Path

from app.core.config import EXPORTS_DIR
from app.modules.ingestion.connectors import census_india
from app.modules.ingestion.connectors.base import ConnectorError

# Census 2011 state/UT codes.
STATES: dict[int, tuple[str, str]] = {
    1: ("Jammu & Kashmir", "State"), 2: ("Himachal Pradesh", "State"),
    3: ("Punjab", "State"), 4: ("Chandigarh", "UT"),
    5: ("Uttarakhand", "State"), 6: ("Haryana", "State"),
    7: ("NCT of Delhi", "UT"), 8: ("Rajasthan", "State"),
    9: ("Uttar Pradesh", "State"), 10: ("Bihar", "State"),
    11: ("Sikkim", "State"), 12: ("Arunachal Pradesh", "State"),
    13: ("Nagaland", "State"), 14: ("Manipur", "State"),
    15: ("Mizoram", "State"), 16: ("Tripura", "State"),
    17: ("Meghalaya", "State"), 18: ("Assam", "State"),
    19: ("West Bengal", "State"), 20: ("Jharkhand", "State"),
    21: ("Odisha", "State"), 22: ("Chhattisgarh", "State"),
    23: ("Madhya Pradesh", "State"), 24: ("Gujarat", "State"),
    25: ("Daman & Diu", "UT"), 26: ("Dadra & Nagar Haveli", "UT"),
    27: ("Maharashtra", "State"), 28: ("Andhra Pradesh", "State"),
    29: ("Karnataka", "State"), 30: ("Goa", "State"),
    31: ("Lakshadweep", "UT"), 32: ("Kerala", "State"),
    33: ("Tamil Nadu", "State"), 34: ("Puducherry", "UT"),
    35: ("Andaman & Nicobar Islands", "UT"),
}


def slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")


def parse_states(value: str) -> list[int]:
    if value.strip().lower() == "all":
        return sorted(STATES)
    codes = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        code = int(part)
        if code not in STATES:
            raise SystemExit(f"Unknown state code {code}. Valid: 1-35.")
        codes.append(code)
    return codes


def fetch_state(code: int, level: str, out_dir: Path, overwrite: bool) -> dict:
    name, kind = STATES[code]
    path = out_dir / f"census2011_{level}_{code:02d}_{slug(name)}.csv"

    if path.exists() and not overwrite:
        print(f"  [skip]  {name} ({kind}) - already written: {path.name}")
        return {"state": name, "status": "skipped", "path": path}

    started = time.time()
    try:
        result = census_india.ingest(
            state=code, level=level, urbrur=0,
            progress_callback=lambda m: print(f"          {m}", end="\r", flush=True),
        )
    except ConnectorError as exc:
        print(f"\n  [fail]  {name} ({kind}): {exc}")
        return {"state": name, "status": "failed", "error": str(exc)}

    df = result.dataframe
    path.write_bytes(df.to_csv(index=False).encode("utf-8-sig"))
    elapsed = time.time() - started
    print(f"\n  [done]  {name} ({kind}): {df.shape[0]:,} rows x {df.shape[1]} cols "
          f"in {elapsed:.0f}s -> {path.name}")
    return {
        "state": name, "kind": kind, "status": "ok",
        "rows": int(df.shape[0]), "columns": int(df.shape[1]),
        "seconds": round(elapsed, 1), "path": path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--states", required=True,
                        help="Comma-separated Census state codes, or 'all'.")
    parser.add_argument("--level", default="village_town",
                        choices=["district", "subdistrict", "village_town", "ward"])
    parser.add_argument("--out", default=str(EXPORTS_DIR / "api" / "census_by_state"))
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-fetch states whose CSV already exists.")
    args = parser.parse_args(argv)

    codes = parse_states(args.states)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"level={args.level}  states={len(codes)}  out={out_dir}\n")
    results = [fetch_state(c, args.level, out_dir, args.overwrite) for c in codes]

    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]

    print(f"\nsummary: {len(ok)} fetched, {len(skipped)} skipped, {len(failed)} failed")
    if ok:
        print(f"  total rows: {sum(r['rows'] for r in ok):,}")
    for r in failed:
        print(f"  FAILED {r['state']}: {r['error'][:120]}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
