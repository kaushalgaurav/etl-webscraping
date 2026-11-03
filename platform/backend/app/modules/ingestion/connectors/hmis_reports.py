"""HMIS Standard Reports connector (hmis.mohfw.gov.in).

The Standard Reports page is an AngularJS app — `#!/standardReports` is a
client-side route, so fetching the URL returns only the shell. Its controller
calls two endpoints, which this connector uses directly instead of driving a
browser:

    GET getFiles?fpath=<base64(path)>        -> JSON directory listing
    GET downloadfiles?filepath=<urlencoded>  -> the file itself

Neither needs a login. The listing returns one object per entry:

    {"path": "BASEDIR/Standard Reports/.../Arunachal Pradesh_29012016.xls",
     "name": "...", "fileSize": "0.11 MB", "isdirectory": false, ...}

`path` is prefixed with the literal "BASEDIR", which the page strips before
building a download link (`f.encodedpath.replace('BASEDIR','')`).

Two traps worth naming:

* The `.xls` files are not Excel. They are HTML tables with an `.xls`
  extension (they begin `<html`), so they parse with `read_html`, never
  `read_excel`.
* They are formatted reports, not datasets: several rows of merged header
  cells above the data. `_flatten_columns` collapses those into one row.

PORTABILITY: every path here is a *remote* POSIX path from the HMIS server,
so it is handled with str/posixpath and never with pathlib or os.path —
on Windows those would rewrite "a/b" as "a\\b" and the server would 404.
Local writes use pathlib, and any server name reaching the filesystem goes
through `safe_filename` first, because names like "12~H. RCH Reports -
State Wise" are legal on Linux but can carry characters Windows forbids.
"""
import base64
import fnmatch
import hashlib
import io
import json
import posixpath
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from app.constants import hmis_indicators
from app.core.config import RAW_DIR
from app.modules.ingestion.connectors.base import ConnectorError, IngestResult

# Downloaded reports are kept so a re-crawl only fetches what changed; the
# tree is >100k files, so re-downloading it to fix a parse bug is not viable.
CACHE_DIR = RAW_DIR / "hmis" 

BASE = "https://hmis.mohfw.gov.in"
LIST_API = f"{BASE}/getFiles"
DOWNLOAD_API = f"{BASE}/downloadfiles"
ROOT = "Standard Reports"
BASEDIR_PREFIX = "BASEDIR"

# The site is slow and rejects a default python-requests agent.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
REQUEST_TIMEOUT = 120
MAX_RETRIES = 3
BACKOFF_SECONDS = 2
DATA_EXTENSIONS = (".xls", ".xlsx", ".csv")

# Windows forbids \ / : * ? " < > | in a filename; Linux allows all but "/".
# Applied on every platform so a crawl produces identical names on both.
_UNSAFE = re.compile(r'[\\/:*?"<>|]+')


def safe_filename(name: str, max_length: int = 120) -> str:
    """A server-supplied name made safe to write on Windows and Linux alike."""
    cleaned = _UNSAFE.sub("_", name).strip().strip(".")
    return (cleaned or "file")[:max_length]


def encode_path(path: str) -> str:
    """base64 of the UTF-8 path — the page's btoa(unescape(encodeURIComponent(p)))."""
    return base64.b64encode(path.encode("utf-8")).decode("ascii")


def strip_basedir(path: str) -> str:
    """Drop the listing's literal "BASEDIR" prefix, as the page does."""
    if path.startswith(BASEDIR_PREFIX):
        return path[len(BASEDIR_PREFIX):]
    return path


@dataclass
class Entry:
    """One row of a getFiles listing."""

    name: str
    path: str
    is_directory: bool
    size: str | None = None
    modified: str | None = None

    @property
    def extension(self) -> str:
        return posixpath.splitext(self.name)[1].lower()

    @property
    def is_data_file(self) -> bool:
        return not self.is_directory and self.extension in DATA_EXTENSIONS


def _get(url: str, params: dict, expect_json: bool) -> requests.Response:
    """GET with retry/backoff — this host times out under load."""
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            if expect_json and "json" not in (resp.headers.get("Content-Type") or "").lower():
                # A signed-out or errored response comes back as HTML.
                raise ConnectorError(f"Expected JSON from {url}, got {resp.headers.get('Content-Type')}.")
            return resp
        except (requests.RequestException, ConnectorError) as exc:
            last = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
    raise ConnectorError(f"HMIS request failed after {MAX_RETRIES} attempts: {last}")


def list_directory(path: str = ROOT) -> list[Entry]:
    """One directory listing. `path` is a remote POSIX path, no leading slash."""
    resp = _get(LIST_API, {"fpath": encode_path(path)}, expect_json=True)
    try:
        rows = resp.json()
    except ValueError as exc:
        raise ConnectorError(f"HMIS listing for '{path}' was not valid JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ConnectorError(f"HMIS listing for '{path}' was not a list.")
    return [
        Entry(
            name=r.get("name", ""),
            path=r.get("path", ""),
            is_directory=bool(r.get("isdirectory")),
            size=r.get("fileSize"),
            modified=r.get("lastModifiedDate"),
        )
        for r in rows
    ]


def walk(path: str = ROOT, max_depth: int = 8, _depth: int = 0) -> Iterator[Entry]:
    """Yield every data file under `path`, descending depth-first.

    `max_depth` is a guard, not a tuning knob: a symlink loop on the server
    would otherwise recurse forever.
    """
    if _depth > max_depth:
        return
    for entry in list_directory(path):
        if entry.is_directory:
            # Descend using the listing's own path (minus BASEDIR), never a
            # locally joined one — the server's separators are authoritative.
            child = strip_basedir(entry.path).lstrip("/")
            if child and child != path:
                yield from walk(child, max_depth, _depth + 1)
        elif entry.is_data_file:
            yield entry


def download(path: str) -> bytes:
    """Fetch one file. `path` is the listing's path, with or without BASEDIR."""
    target = strip_basedir(path)
    if not target.startswith("/"):
        target = "/" + target
    # quote() with an empty safe list, so spaces and "~" survive intact.
    resp = _get(DOWNLOAD_API, {"filepath": target}, expect_json=False)
    if not resp.content:
        raise ConnectorError(f"HMIS returned an empty file for '{target}'.")
    return resp.content


def survey_cache_dir(survey_id: int | None) -> Path:
    """The cache root for one survey.

    Each survey downloads into its own directory, so a survey that deletes its
    source files after ingest can never remove another survey's copies. The
    same report fetched under two surveys is downloaded twice — that is the
    price of each survey owning, and being able to clear, its own files.

    survey_id=None keeps the old shared root, for callers outside a survey.
    """
    return CACHE_DIR if survey_id is None else CACHE_DIR / f"survey_{survey_id}"


def cache_path(remote_path: str, survey_id: int | None = None) -> Path:
    """Local file for a remote report, inside that survey's own cache.

    Each remote segment is sanitised separately, so the cache mirrors the
    HMIS tree and lands on identical paths on Windows and Linux. The remote
    path is split on "/" only — it is a server path, never a local one.
    """
    segments = [s for s in strip_basedir(remote_path).strip("/").split("/") if s]
    local = survey_cache_dir(survey_id)
    for segment in segments:
        local = local / safe_filename(segment)
    return local


def _meta_path(local: Path) -> Path:
    return local.with_suffix(local.suffix + ".meta.json")


def is_cached(entry: "Entry", survey_id: int | None = None) -> bool:
    """True when this exact version of the file is already on disk.

    Identity is the server's own (lastModifiedDate, fileSize) for the path.
    A re-published report changes its modified date, so it downloads again;
    an unchanged one never does.
    """
    local = cache_path(entry.path, survey_id)
    meta = _meta_path(local)
    if not local.exists() or not meta.exists():
        return False
    try:
        saved = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return saved.get("modified") == entry.modified and saved.get("size") == entry.size


def cached_download(
    entry: "Entry", refresh: bool = False, survey_id: int | None = None
) -> tuple[bytes, bool]:
    """(content, downloaded) — read from cache when unchanged, else fetch.

    Returns whether a request was actually made, so a crawl can report how
    much it skipped.
    """
    local = cache_path(entry.path, survey_id)
    if not refresh and is_cached(entry, survey_id):
        return local.read_bytes(), False

    content = download(entry.path)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(content)
    _meta_path(local).write_text(
        json.dumps({
            "remote_path": strip_basedir(entry.path),
            "name": entry.name,
            "modified": entry.modified,
            "size": entry.size,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2),
        encoding="utf-8",
    )
    return content, True


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse a MultiIndex header into one row of readable names.

    These reports stack three or four rows of merged cells. pandas turns that
    into a MultiIndex full of repeated labels and "Unnamed: n_level_m"
    placeholders; joining the distinct parts gives one usable name per column.
    """
    if not isinstance(df.columns, pd.MultiIndex):
        return df
    names = []
    for col in df.columns:
        parts, seen = [], set()
        for level in col:
            text = str(level).strip()
            if not text or text.startswith("Unnamed") or text in seen:
                continue
            seen.add(text)
            parts.append(text)
        names.append(" - ".join(parts) or "column")
    return df.set_axis(names, axis=1)


def parse_report(content: bytes, filename: str) -> pd.DataFrame:
    """Parse one downloaded report into a DataFrame.

    HMIS serves `.xls` that is really HTML, so the extension cannot be
    trusted — the content is sniffed instead.
    """
    head = content[:512].lstrip()[:64].lower()
    is_html = head.startswith(b"<html") or head.startswith(b"<!doctype") or b"<table" in head

    if filename.lower().endswith(".csv") and not is_html:
        return pd.read_csv(io.BytesIO(content))
    if is_html:
        tables = pd.read_html(io.BytesIO(content))
        if not tables:
            raise ConnectorError(f"No HTML table found in '{filename}'.")
        return _flatten_columns(max(tables, key=lambda t: t.size))
    # A genuine binary workbook.
    return pd.read_excel(io.BytesIO(content))


def parse_report_long(content: bytes, filename: str) -> pd.DataFrame:
    """One report -> long rows: one per (area, indicator, metric year).

    The wide layout cannot be stacked across years. Its columns are
    (number, indicator label, metric year) and the metric year is part of the
    name — "...tt2 given to pregnant women_2019_20" beside "..._2018_19" —
    so every year contributes its own columns and a stacked table grows
    without bound (one F.1 year alone produced 1,257).

    Melting fixes that: the metric year becomes a value, the indicator becomes
    a row, and the schema is the same handful of columns for every report,
    every year and every level.
    """
    tables = pd.read_html(io.BytesIO(content))
    if not tables:
        raise ConnectorError(f"No HTML table found in '{filename}'.")
    df = max(tables, key=lambda t: t.size)
    if not isinstance(df.columns, pd.MultiIndex):
        raise ConnectorError(f"'{filename}' has no indicator header to melt.")

    # The label columns carry "Indicators" at the top level; everything else
    # is a measured column, named (number, indicator label, metric year).
    id_cols = [c for c in df.columns if str(c[0]).startswith("Indicators")]
    value_cols = [c for c in df.columns if c not in id_cols]
    if not value_cols or not id_cols:
        raise ConnectorError(f"'{filename}' has no measured columns to melt.")

    # The area name is the last label column; earlier ones are serial numbers.
    areas = df[id_cols[-1]].astype("string").str.strip()

    blocks = []
    for col in value_cols:
        label = str(col[1]).strip()
        meta = hmis_indicators.lookup(label) or {}
        blocks.append(pd.DataFrame({
            "area": areas,
            "indicator": label,
            "variable": meta.get("variable"),
            "programme": meta.get("programme"),
            "grouping": meta.get("grouping"),
            "metric_year": str(col[2]).strip(),
            "value": pd.to_numeric(df[col], errors="coerce"),
        }))

    long = pd.concat(blocks, ignore_index=True)
    # Rows with no area are the report's own spacer/footer lines.
    long = long[long["area"].notna() & (long["area"] != "")]
    return long.reset_index(drop=True)


def _row_levels(areas: "pd.Series", folder: str | None) -> "pd.Series":
    """The level of each row, which is not uniform within a file.

    Every report opens with a total row for the folder it sits in, written
    with a leading underscore ("_A & N Islands" inside the A & N folder) or
    as "All India" in the national file. Those are totals of the rows below,
    not another area at the same level, and labelling them as such would
    double-count any sum over the table.

        _ALL INDIA/All.xls   "All India" -> national, the rest -> state
        <State>/ALL.xls      "_<State>"  -> state,    the rest -> district
    """
    names = areas.astype("string").fillna("")
    if _is_national(folder):
        return names.where(~names.str.strip().str.lower().str.replace(" ", "", regex=False)
                           .isin({"allindia", "_allindia"}), "national").mask(
            ~names.str.strip().str.lower().str.replace(" ", "", regex=False)
            .isin({"allindia", "_allindia"}), "state")
    return names.str.startswith("_").map({True: "state", False: "district"})


def _is_national(folder: str | None) -> bool:
    """True for the "_ALL INDIA" folder, whose rows are states not districts."""
    return bool(folder) and folder.strip().lstrip("_").upper().startswith("ALL INDIA")


def path_dimensions(path: str) -> dict:
    """Pull the report's dimensions out of its own directory path.

    The tree encodes them, so the file needs no extra lookup:

        Standard Reports/12~H. RCH Reports - State Wise/2014-2015/
            MonthUpToDecember/Arunachal Pradesh/Arunachal Pradesh_29012016.xls
                 report_type              year      period        area
    """
    parts = [p for p in strip_basedir(path).strip("/").split("/") if p]
    if parts and parts[0] == ROOT:
        parts = parts[1:]
    folders = parts[:-1] if parts else []
    dims: dict[str, str | None] = {
        "report_type": None, "year": None, "period": None, "area": None,
    }
    if folders:
        # "12~H. RCH Reports - State Wise" -> "H. RCH Reports - State Wise"
        dims["report_type"] = folders[0].split("~", 1)[-1]
    for folder in folders[1:]:
        if re.fullmatch(r"\d{4}(-\d{2,4})?", folder):
            dims["year"] = folder
        elif folder.lower().startswith(("month", "quarter", "annual", "upto")):
            dims["period"] = folder
        else:
            dims["area"] = folder
    return dims


@dataclass
class Chunk:
    """One leaf folder's worth of reports, stacked."""

    label: str
    path: str
    dataframe: pd.DataFrame | None = None
    files: int = 0
    downloaded: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def to_record_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Long rows -> one row per record, indicators as columns.

    A report lists an area once with 162 indicators across two metric years;
    melting it produces 324 rows for that one area. This folds those back so a
    file's four areas become four records per metric year, matching how the
    report reads.

    The metric year stays a *column value* rather than part of a column name.
    The source spreadsheet names its columns
    "...Registered for ANC - 2008-09", so storing that layout verbatim would
    add a fresh set of columns for every report year — one F.1 year alone
    yields 1,257, and PostgreSQL stops at 1,600 per table. Keying on
    (area, metric_year) instead holds the schema at the ~200 indicator codes
    however many years are loaded.

    Pivoted on the short `variable` code, not the prose `indicator`: the
    labels run past 100 characters and PostgreSQL truncates identifiers at 63,
    which would silently collide two indicators into one column.
    """
    if not {"variable", "value"} <= set(df.columns):
        return df

    index = [c for c in (
        "area", "parent_area", "level", "report_type", "report_year",
        "period", "source_file",
    ) if c in df.columns]
    if not index:
        return df

    mapped = df[df["variable"].notna()].copy()
    if mapped.empty:
        return df

    # Columns are named by the metric year's *position*, not its value. A
    # report carries its own year beside the previous one as a comparison, so
    # naming them "anc_reg_2008_09" would mint a fresh set of columns for every
    # report year — 1,614 across the archive, past PostgreSQL's 1,600 limit.
    # Ranking newest-first gives every report the same names: anc_reg,
    # anc_reg_prev, …, holding the schema at (indicators x slots).
    years = sorted(mapped["metric_year"].dropna().unique(), reverse=True)
    rank = {year: i for i, year in enumerate(years)}
    mapped["_slot"] = mapped["metric_year"].map(rank)
    mapped["_column"] = mapped.apply(
        lambda r: r["variable"] if r["_slot"] == 0
        else f"{r['variable']}_prev" if r["_slot"] == 1
        else f"{r['variable']}_prev{int(r['_slot'])}",
        axis=1,
    )

    # Pivot only the combinations that exist. `dropna=False` would instead
    # produce the cartesian product of every index level, inventing an
    # all-empty duplicate of each area for every other file in the folder.
    wide = mapped.pivot_table(
        index=index, columns="_column", values="value", aggfunc="max"
    ).reset_index()
    wide.columns.name = None

    # An area the report lists with no figures at all drops out of that pivot,
    # which would read as "never reported" rather than "reported empty". Add
    # those rows back from the index the file actually contains.
    present = mapped[index].drop_duplicates()
    if len(present) > len(wide):
        wide = present.merge(wide, on=index, how="left")

    # Record which actual years those slots were, so the positional naming
    # stays interpretable without consulting the source file.
    for slot, year in enumerate(years):
        wide["metric_year" if slot == 0 else
             "metric_year_prev" if slot == 1 else f"metric_year_prev{slot}"] = year
    return wide


def plan_chunks(path: str = ROOT, max_depth: int = 8) -> list[tuple[str, str]]:
    """[(label, folder path)] — every folder that directly holds data files.

    The unit of work is a folder, not a file: one state-year-period folder is
    a handful of reports that share a schema, which is the smallest batch
    worth appending to a table on its own.
    """
    found: list[tuple[str, str]] = []

    def descend(current: str, depth: int) -> None:
        if depth > max_depth:
            return
        entries = list_directory(current)
        if any(e.is_data_file for e in entries):
            label = " / ".join(current.split("/")[1:]) or current
            found.append((label, current))
        for entry in entries:
            if entry.is_directory:
                child = strip_basedir(entry.path).lstrip("/")
                if child and child != current:
                    descend(child, depth + 1)

    descend(path, 0)
    return found


BATCH_LEVELS = {"report_type": 1, "year": 2, "period": 3}

# The only report type this platform loads. The tree holds a dozen more (RCH,
# state-wise variants); they carry different indicators in a different layout,
# so a crawl that took them all would spend hours fetching reports nothing
# reads, and widen hmis_raw with columns from an incompatible shape.
DEFAULT_REPORT_TYPE = "9~F.1. Performance of Key HMIS Indicators(upto District Level)"

# How many of the most recent report years a crawl takes when not told otherwise.
DEFAULT_LAST_YEARS = 2


def _short_label(folder: str) -> str:
    """"9~F.1. Performance of Key HMIS Indicators(upto District Level)" -> "F.1"."""
    name = folder.split("~", 1)[-1]
    head = name.split(" ", 1)[0].rstrip(".")
    return head or name[:12]


def plan_branches(
    path: str = ROOT,
    batch_by: str = "year",
    report_type: str | None = DEFAULT_REPORT_TYPE,
    last_years: int | None = DEFAULT_LAST_YEARS,
) -> list[dict]:
    """Split a subtree into the jobs that should crawl it.

    Crawling the whole tree as one job gives a single status line for 100k
    files and one dataset to lose if it breaks late. Splitting on the tree's
    own shape — report type, then year — gives jobs of a few hundred files
    each, independently named, retryable and reportable.

    The tree is also pruned as it is walked, not after: `report_type` keeps one
    report-type folder (default F.1) and `last_years` keeps only the N most
    recent year folders under it. Pass None to either for the unpruned tree.
    Pruning during the walk matters — descending the others just to discard
    their branches would be thousands of directory listings on its own.
    """
    if batch_by not in BATCH_LEVELS:
        raise ConnectorError(
            f"Unknown batch_by '{batch_by}'. Use one of: {', '.join(BATCH_LEVELS)}"
        )
    depth = BATCH_LEVELS[batch_by]
    branches: list[dict] = []

    def descend(current: str, level: int, labels: list[str]) -> None:
        entries = list_directory(current)
        # Stop at the requested depth, or earlier if this folder already holds
        # files — a shallower tree must not be skipped entirely.
        if level >= depth or any(e.is_data_file for e in entries):
            branches.append({
                "name": "hmis-" + "-".join(labels).lower().replace(" ", "-")[:60],
                "path": current,
                "label": " / ".join(labels),
            })
            return

        directories = [e for e in entries if e.is_directory]
        if level == 0 and report_type:
            directories = [e for e in directories if e.name == report_type]
            if not directories:
                raise ConnectorError(
                    f"Report type '{report_type}' not found under '{current}'. "
                    f"Found: {', '.join(e.name for e in entries if e.is_directory) or 'nothing'}"
                )
        if level == 1 and last_years:
            # Year folders are named "2008-2009", so a plain sort is
            # chronological and the newest are at the end.
            directories = sorted(directories, key=lambda e: e.name)[-last_years:]

        for entry in directories:
            child = strip_basedir(entry.path).lstrip("/")
            if not child or child == current:
                continue
            piece = _short_label(entry.name) if level == 0 else entry.name
            descend(child, level + 1, labels + [piece])

    # `level` counts folders below "Standard Reports", so BATCH_LEVELS means the
    # same thing whether the caller starts at the root or partway down.
    segments = [p for p in path.strip("/").split("/") if p]
    start_level = max(len(segments) - 1, 0)
    start_labels = [_short_label(s) for s in segments[1:]]
    descend(path, start_level, start_labels)
    return branches


def source_ref(path: str, file_pattern: str | None = None) -> str:
    """Stable identity of a crawl: the branch plus what it was filtered to.

    The filter belongs in the identity — crawling a branch for "ALL.xls" and
    crawling it for every file produce different datasets, and keying on the
    path alone would let the second be skipped as already done.
    """
    ref = f"{LIST_API}?fpath={encode_path(path)}"
    return f"{ref}&file={file_pattern.lower()}" if file_pattern else ref


def matches(name: str, pattern: str | None) -> bool:
    """Case-insensitive glob against a filename.

    Case matters here: the same report is "ALL.xls" in a state folder and
    "All.xls" under "_ALL INDIA", so an exact match would silently drop the
    national rows.
    """
    if not pattern:
        return True
    return fnmatch.fnmatch(name.lower(), pattern.lower())


def iter_chunks(
    path: str = ROOT,
    max_files_per_chunk: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    plan: list[tuple[str, str]] | None = None,
    refresh: bool = False,
    file_pattern: str | None = None,
    long_form: bool = True,
    record_form: bool = True,
    survey_id: int | None = None,
) -> Iterator[Chunk]:
    """Yield one stacked DataFrame per leaf folder.

    Downloads are per chunk rather than up front, so memory stays flat over a
    crawl of thousands of files and a caller can persist as it goes. A folder
    that fails is yielded with `error` set instead of aborting the crawl —
    over a tree this size some requests will time out.
    """
    plan = plan if plan is not None else plan_chunks(path)
    for i, (label, folder) in enumerate(plan, 1):
        chunk = Chunk(label=label, path=folder)
        try:
            entries = [e for e in list_directory(folder)
                       if e.is_data_file and matches(e.name, file_pattern)]
            if max_files_per_chunk:
                entries = entries[:max_files_per_chunk]
            frames = []
            for entry in entries:
                try:
                    content, fetched = cached_download(entry, refresh=refresh, survey_id=survey_id)
                    if fetched:
                        chunk.downloaded += 1
                    else:
                        chunk.skipped += 1
                    if progress_callback:
                        mark = "downloaded" if fetched else "cached"
                        progress_callback(f"[{i}/{len(plan)}] {label}: {entry.name} ({mark})")
                    df = (parse_report_long(content, entry.name) if long_form
                          else parse_report(content, entry.name))
                except Exception as exc:
                    chunk.warnings.append(f"{entry.name}: {exc}")
                    continue
                dims = path_dimensions(entry.path)
                if long_form:
                    # `area` is already a column here — it is the row label, the
                    # real geography. The folder names its parent, and which of
                    # the two is a state or a district follows from that: a file
                    # under "_ALL INDIA" lists states, one under a state lists
                    # its districts.
                    parent = dims.get("area")
                    df["parent_area"] = parent
                    df["level"] = _row_levels(df["area"], parent)
                    df["report_type"] = dims.get("report_type")
                    df["report_year"] = dims.get("year")
                    df["period"] = dims.get("period")
                else:
                    for key, value in dims.items():
                        df[key] = value
                df["source_file"] = entry.name
                if long_form and record_form:
                    # Fold the melted rows back into one row per record, after
                    # the file's dimensions are on the frame so they survive
                    # into the pivot index.
                    df = to_record_rows(df)
                frames.append(df)
            chunk.files = len(frames)
            if frames:
                chunk.dataframe = pd.concat(frames, ignore_index=True, sort=False)
        except Exception as exc:
            chunk.error = str(exc)
        yield chunk


def ingest(
    path: str = ROOT,
    max_files: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Crawl `path`, download every report under it and stack them.

    Every row carries the dimensions of the file it came from, so the stacked
    frame stays traceable to a single report.
    """
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    files = list(walk(path))
    if not files:
        raise ConnectorError(f"No .xls/.xlsx/.csv files found under '{path}'.")
    if max_files:
        files = files[:max_files]

    for i, entry in enumerate(files, 1):
        if progress_callback:
            progress_callback(f"[{i}/{len(files)}] {entry.name}")
        try:
            df = parse_report(download(entry.path), entry.name)
        except Exception as exc:  # one bad report must not lose the rest
            warnings.append(f"{entry.name}: {exc}")
            continue
        for key, value in path_dimensions(entry.path).items():
            df[key] = value
        df["source_file"] = entry.name
        frames.append(df)

    if not frames:
        raise ConnectorError(f"Every file under '{path}' failed to parse. {warnings[:3]}")
    stacked = pd.concat(frames, ignore_index=True, sort=False)
    if len(frames) < len(files):
        warnings.append(f"{len(files) - len(frames)} of {len(files)} file(s) failed.")
    return IngestResult(
        dataframe=stacked,
        connector="hmis_reports",
        source_ref=f"{LIST_API}?fpath={encode_path(path)}",
        warnings=warnings,
    )
