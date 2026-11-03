"""Remove downloaded source files once their rows are in the database.

A crawl of the HMIS tree leaves thousands of `.xls` reports on disk, and an
uploaded CSV is kept beside its dataset. Once the rows are committed, the
database is the record and the file is just disk.

What is kept and what goes:

* The payload (`.xls`, `.csv`, `.xlsx`) is deleted — it is the bulk.
* The `.meta.json` sidecar is kept. It is a few hundred bytes and holds the
  server's own modified-date, size and the sha256 of what was ingested, which
  is the audit trail for *which version* of a report produced a row. Deleting
  the payload loses disk; deleting the sidecar loses provenance.

What this costs, deliberately and knowingly:

* `rebuild_hmis_raw.py` rebuilds `hmis_raw` from the cached reports. With the
  payloads gone it cannot run until they are downloaded again.
* A crawl with `refresh=true` re-downloads rather than reading from disk.

Ordinary re-crawls are unaffected: a branch already ingested is skipped on the
jobs/datasets tables, not on the file cache.
"""
from collections.abc import Iterable
from pathlib import Path

from app.core.config import RAW_DIR
from app.modules.ingestion.connectors import hmis_reports

# Only these are deleted. Anything else under the raw directory was not put
# there by an ingest and is left alone.
SOURCE_SUFFIXES = {".xls", ".xlsx", ".csv", ".tsv"}

# Kept so a purged file still says what it was.
PROVENANCE_SUFFIX = ".meta.json"


def _is_source_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES


def _delete(paths: Iterable[Path]) -> tuple[int, int, list[str]]:
    """(files removed, bytes freed, errors)."""
    removed = freed = 0
    errors: list[str] = []
    for path in paths:
        try:
            size = path.stat().st_size
            path.unlink()
            removed += 1
            freed += size
        except OSError as exc:
            # A file already gone, or locked by another process. Never fatal:
            # the rows are committed, and leaving a file behind is harmless.
            errors.append(f"{path.name}: {exc}")
    return removed, freed, errors


def purge_hmis_branch(remote_path: str, survey_id: int | None = None) -> dict:
    """Delete the cached reports under one branch of the HMIS tree.

    Scoped to the survey that downloaded them: each survey caches into its own
    directory, so clearing one survey's files never touches another's copies
    of the same reports.
    """
    local_root = hmis_reports.cache_path(remote_path, survey_id)
    if not local_root.exists():
        return {"removed": 0, "bytes_freed": 0, "errors": []}

    candidates = (
        [local_root] if _is_source_file(local_root)
        else [p for p in local_root.rglob("*") if _is_source_file(p)]
    )
    removed, freed, errors = _delete(candidates)
    return {"removed": removed, "bytes_freed": freed, "errors": errors}


def purge_raw_file(raw_file_path: str | None) -> dict:
    """Delete one dataset's saved upload/download.

    Confined to the raw directory: a path pointing anywhere else is a bug or
    worse, and is refused rather than followed.
    """
    if not raw_file_path:
        return {"removed": 0, "bytes_freed": 0, "errors": []}

    path = Path(raw_file_path)
    try:
        path.resolve().relative_to(RAW_DIR.resolve())
    except ValueError:
        return {"removed": 0, "bytes_freed": 0,
                "errors": [f"refused: {path} is outside {RAW_DIR}"]}

    if not _is_source_file(path):
        return {"removed": 0, "bytes_freed": 0, "errors": []}
    removed, freed, errors = _delete([path])
    return {"removed": removed, "bytes_freed": freed, "errors": errors}


def purge_all(dry_run: bool = False, survey_id: int | None = None) -> dict:
    """Source files on disk — one survey's, or every survey's.

    With `survey_id`, only that survey's own cache is cleared and no other
    survey loses its copies. Without it, everything under the raw directory
    goes: the one-off catch-up for files downloaded before ingests started
    cleaning up after themselves.
    """
    root = hmis_reports.survey_cache_dir(survey_id) if survey_id is not None else RAW_DIR
    if not root.exists():
        return {"removed": 0, "bytes_freed": 0, "errors": [],
                "would_remove": 0, "would_free": 0}
    candidates = [p for p in root.rglob("*") if _is_source_file(p)]
    if dry_run:
        return {
            "removed": 0, "bytes_freed": 0, "errors": [],
            "would_remove": len(candidates),
            "would_free": sum(p.stat().st_size for p in candidates),
        }
    removed, freed, errors = _delete(candidates)
    return {"removed": removed, "bytes_freed": freed, "errors": errors}


def describe(result: dict) -> str:
    """One line for a job's progress/notes."""
    mb = result.get("bytes_freed", 0) / 1_048_576
    line = f"Removed {result.get('removed', 0):,} source file(s), freed {mb:,.1f} MB"
    if result.get("errors"):
        line += f" ({len(result['errors'])} could not be deleted)"
    return line
