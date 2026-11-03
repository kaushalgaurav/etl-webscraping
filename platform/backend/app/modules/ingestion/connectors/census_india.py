"""Census of India connector (censusindia.gov.in).

The Population Finder page is a Vue app — its static HTML holds only
`{{place.state_name}}` placeholders, so scraping it yields nothing. The page
calls a NADA API instead, which this connector uses directly:

    {API}/tables/data/2011/PC11_PCA-TV/255?_source=dt&labels=indicator
        &state=<id>&district=<id>&geo_level=<1..5>

geo_level: 2=district 3=subdistrict 4=village/town 5=ward. Level 1 (state)
returns nothing from this table — state figures must be summed from districts.
Omitting `state` fetches the whole country (all 640 districts at level 2).
Responses are *long* (one row per indicator/area/urbrur) and carry a
`codelist` naming every indicator, state and district — so no separate
lookup table is needed. This connector pivots that to wide, one row per
area, one column per indicator, which is the layout report templates want.

SSL note: censusindia.gov.in serves an incomplete certificate chain, so
verification fails with the default CA bundle. `verify_ssl=False` is
supported per-source rather than disabling verification globally.
"""
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pandas as pd
import requests
import urllib3

from app.core.config import settings
from app.modules.ingestion.connectors.base import ConnectorError, IngestResult

API_BASE = "https://censusindia.gov.in/nada/index.php/api/"
TABLE_ID = "PC11_PCA-TV/255"  # Primary Census Abstract, town/village table
PAGE_LIMIT = 5000
MAX_RETRIES = 4
BACKOFF_SECONDS = 2

# geo_level=1 (state) returns zero rows for this table — PC11_PCA-TV holds
# district level and below only. State totals must be summed from districts.
GEO_LEVELS = {"district": 2, "subdistrict": 3, "village_town": 4, "ward": 5}

# Columns that uniquely identify an area at each level. Used as the pivot
# index — too short and finer areas get aggregated into their parent.
# `tv_seq_no` is required below sub-district: some towns share a town_village
# code and are distinguished only by their sequence number. Leaving it out
# makes pivot_table's aggfunc silently keep one and drop the other.
LEVEL_INDEX_COLUMNS = {
    "district": ("state", "district"),
    "subdistrict": ("state", "district", "subdistrict"),
    "village_town": ("state", "district", "subdistrict", "town_village", "tv_seq_no"),
    "ward": ("state", "district", "subdistrict", "town_village", "tv_seq_no", "ward"),
}
# urbrur split codes used by the Census PCA.
URBRUR_LABELS = {0: "total", 1: "rural", 2: "urban"}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DataIngestionEngine/1.0)"}


def _get(url: str, params: dict, verify_ssl: bool, retries: int = MAX_RETRIES) -> dict:
    """GET with retry/backoff.

    A full national pull is ~33 paginated calls and this API intermittently
    drops connections mid-run ("Response ended prematurely"), so one failed
    page must not abort the whole ingest.
    """
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url, params=params, headers=HEADERS, verify=verify_ssl,
                timeout=settings.request_timeout_seconds * 2,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.SSLError as exc:
            raise ConnectorError(
                f"SSL verification failed for {url}. censusindia.gov.in serves an "
                f"incomplete certificate chain; set verify_ssl=false for this source. ({exc})"
            ) from exc
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(BACKOFF_SECONDS * (2 ** attempt))

    raise ConnectorError(
        f"Census API request failed after {retries} attempts: {last_error}"
    ) from last_error


def geosearch(areaname: str, census_year: int = 2011, limit: int = 50,
              verify_ssl: bool = False) -> list[dict]:
    """Look up area codes (state/district/village) by name."""
    payload = _get(
        f"{API_BASE}tables/geosearch/{census_year}",
        {"areaname": areaname, "limit": limit},
        verify_ssl,
    )
    return payload.get("data", [])


def _merge_code_map(target: dict, codelist: dict, key: str, label_field: str) -> None:
    """Accumulate {code: label} from one page's codelist into `target`.

    Each page only describes the codes appearing *in that page*, so the maps
    must be merged across every page — keeping only page 1's codelist leaves
    names null for all later areas.
    """
    entry = codelist.get(key) or {}
    for item in entry.get("code_list", []):
        code = item.get("code", item.get(key))
        label = item.get("label", item.get(label_field))
        if code is not None and label is not None:
            target[code] = label


def _data_url(census_year: int) -> str:
    return f"{API_BASE}tables/data/{census_year}/{TABLE_ID}"


def _base_params(level: str, state: int | None, district: int | None) -> dict:
    if level not in GEO_LEVELS:
        raise ConnectorError(f"Unknown level '{level}'. Use one of: {', '.join(GEO_LEVELS)}")
    params = {"_source": "dt", "labels": "indicator", "geo_level": GEO_LEVELS[level]}
    if state:
        params["state"] = state
    if district:
        params["district"] = district
    return params


def _fetch_long(
    url: str,
    params: dict,
    verify_ssl: bool,
    progress_callback: Callable[[str], None] | None = None,
    progress_prefix: str = "",
) -> tuple[list[dict], dict, dict, dict]:
    """Page through one query. Returns (records, indicators, states, districts)."""
    records: list[dict] = []
    indicators: dict = {}
    states: dict = {}
    districts: dict = {}
    offset = 0
    while True:
        payload = _get(url, {**params, "limit": PAGE_LIMIT, "offset": offset}, verify_ssl)
        codelist = payload.get("codelist", {})
        # Merge every page's codelist — each only names the codes it contains.
        _merge_code_map(indicators, codelist, "indicator", "label")
        _merge_code_map(states, codelist, "state", "areaname")
        _merge_code_map(districts, codelist, "district", "areaname")

        batch = payload.get("data", [])
        records.extend(batch)
        found = payload.get("found", len(records))
        offset += len(batch)

        if progress_callback and found:
            pages = -(-found // PAGE_LIMIT)  # ceiling division
            page = -(-offset // PAGE_LIMIT)
            progress_callback(
                f"{progress_prefix}Fetching page {min(page, pages)}/{pages} "
                f"({len(records):,}/{found:,} records)"
            )

        if not batch or offset >= found:
            break
    return records, indicators, states, districts


def ingest(
    state: int | None = None,
    district: int | None = None,
    level: str = "district",
    census_year: int = 2011,
    urbrur: int | None = 0,
    verify_ssl: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Fetch Census PCA indicators and pivot them to one row per area.

    Holds every record in memory, so it suits district level or a single
    state/district. For all-India sub-district and below use iter_chunks().

    Args:
        state / district: Census area codes (find them via geosearch()).
        level: geography to return — district, subdistrict, village_town, ward.
        urbrur: 0=total, 1=rural, 2=urban; None keeps all three as separate rows.
    """
    params = _base_params(level, state, district)
    url = _data_url(census_year)
    records, indicators, states, districts = _fetch_long(url, params, verify_ssl, progress_callback)

    if not records:
        raise ConnectorError(
            f"Census API returned no records for state={state} district={district} level={level}."
        )

    wide, warnings = _pivot(records, level, urbrur, indicators, states, districts)
    if not verify_ssl:
        warnings.append(
            "SSL verification was disabled for censusindia.gov.in (incomplete cert chain)."
        )

    return IngestResult(
        dataframe=wide,
        connector="census_india",
        source_ref=f"{url}?state={state}&district={district}&geo_level={GEO_LEVELS[level]}",
        warnings=warnings,
    )


def list_districts(
    state: int | None = None, census_year: int = 2011, verify_ssl: bool = False
) -> list[dict]:
    """Every district with its state, in one call.

    Filters the district-level table to a single indicator, so the response
    is ~1.9k records (640 districts x 3 urbrur splits) instead of 163k.
    """
    params = {**_base_params("district", state, None), "indicator": 1}
    records, _, states, districts = _fetch_long(_data_url(census_year), params, verify_ssl)
    seen: dict[tuple, dict] = {}
    for rec in records:
        key = (rec["state"], rec["district"])
        if key not in seen:
            seen[key] = {
                "state": rec["state"],
                "state_name": states.get(rec["state"]),
                "district": rec["district"],
                "district_name": districts.get(rec["district"]),
            }
    return sorted(seen.values(), key=lambda d: (d["state"], d["district"]))


# How iter_chunks() splits an all-India pull. The API orders records
# indicator-first, so a single page can't be pivoted on its own; each chunk is
# fetched completely, pivoted, and handed back before the next one starts.
# Chunk sizes keep peak memory to a few hundred thousand records.
CHUNK_BY = {"district": None, "subdistrict": "state", "village_town": "district", "ward": "district"}
CHUNK_RETRY_WAIT_SECONDS = 30


@dataclass
class Chunk:
    label: str
    state: int | None
    district: int | None
    dataframe: pd.DataFrame | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def plan_chunks(
    level: str,
    state: int | None = None,
    district: int | None = None,
    census_year: int = 2011,
    verify_ssl: bool = False,
) -> list[tuple[str, int | None, int | None]]:
    """[(label, state, district)] — the queries iter_chunks() will run."""
    if level not in GEO_LEVELS:
        raise ConnectorError(f"Unknown level '{level}'. Use one of: {', '.join(GEO_LEVELS)}")
    by = CHUNK_BY[level]
    if district or by is None or (by == "state" and state):
        scope = f"district {district}" if district else f"state {state}" if state else "All India"
        return [(scope, state, district)]

    areas = list_districts(state, census_year, verify_ssl)
    if not areas:
        raise ConnectorError(f"No districts found for state={state}.")
    if by == "state":
        states = dict.fromkeys((a["state"], a["state_name"]) for a in areas)
        return [(f"{name or code}", code, None) for code, name in states]
    return [
        (f"{a['district_name'] or a['district']}, {a['state_name'] or a['state']}",
         a["state"], a["district"])
        for a in areas
    ]


def iter_chunks(
    level: str = "district",
    state: int | None = None,
    district: int | None = None,
    census_year: int = 2011,
    urbrur: int | None = 0,
    verify_ssl: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> Iterator[Chunk]:
    """Yield one pivoted DataFrame per state/district, for all-India pulls.

    A chunk that still fails after _get()'s retries is retried once more after
    a pause, then yielded with `error` set rather than aborting the run —
    an all-India village pull is ~11k calls and hours long.
    """
    plan = plan_chunks(level, state, district, census_year, verify_ssl)
    url = _data_url(census_year)

    for index, (label, c_state, c_district) in enumerate(plan, start=1):
        prefix = f"[{index}/{len(plan)}] {label}: "
        params = _base_params(level, c_state, c_district)
        chunk = Chunk(label=label, state=c_state, district=c_district)
        for attempt in range(2):
            try:
                records, indicators, states, districts = _fetch_long(
                    url, params, verify_ssl, progress_callback, prefix
                )
                chunk.error = None
                break
            except ConnectorError as exc:
                chunk.error = str(exc)
                if attempt == 0:
                    if progress_callback:
                        progress_callback(f"{prefix}failed, retrying in {CHUNK_RETRY_WAIT_SECONDS}s")
                    time.sleep(CHUNK_RETRY_WAIT_SECONDS)

        if chunk.error is None:
            if records:
                chunk.dataframe, chunk.warnings = _pivot(
                    records, level, urbrur, indicators, states, districts
                )
            else:
                chunk.warnings = [f"{label}: no records at level '{level}'."]
        yield chunk


def _pivot(
    records: list[dict],
    level: str,
    urbrur: int | None,
    indicators: dict,
    states: dict,
    districts: dict,
) -> tuple[pd.DataFrame, list[str]]:
    """Long API records -> one row per area, one column per indicator."""
    long_df = pd.DataFrame(records)
    warnings: list[str] = []

    if urbrur is not None:
        filtered = long_df[long_df["urbrur"] == urbrur]
        if filtered.empty:
            # Only district level carries an urbrur=0 "total" row. Villages are
            # rural (1) and towns urban (2), with no combined row — so asking
            # for a total below district level must keep the split, not
            # silently return nothing.
            available = sorted(long_df["urbrur"].unique())
            warnings.append(
                f"urbrur={urbrur} ({URBRUR_LABELS.get(urbrur)}) does not exist at "
                f"level '{level}'; kept all rows instead "
                f"(present: {', '.join(f'{v}={URBRUR_LABELS.get(v, v)}' for v in available)}). "
                "The 'urbrur' column distinguishes them."
            )
            urbrur = None
        else:
            long_df = filtered

    long_df["indicator_name"] = long_df["indicator"].map(indicators).fillna(
        long_df["indicator"].astype(str)
    )

    # The index must reach the level being fetched, or finer areas collapse
    # into their parent — every village in a district would become one row.
    index_cols = list(LEVEL_INDEX_COLUMNS[level])
    if urbrur is None:
        index_cols.append("urbrur")

    wide = long_df.pivot_table(
        index=index_cols, columns="indicator_name", values="value", aggfunc="first"
    ).reset_index()
    wide.columns.name = None

    wide.insert(0, "state_name", wide["state"].map(states))
    if "district" in wide.columns:
        wide.insert(1, "district_name", wide["district"].map(districts))
    if urbrur is None:
        wide["urbrur"] = wide["urbrur"].map(URBRUR_LABELS)

    # A town can appear under one town_village code more than once, split by
    # tv_seq_no — the higher sequence is the whole town and the lower one a
    # part of it, so naively summing every row double-counts. Flag them rather
    # than dropping rows: the caller keeps full fidelity and can exclude
    # `_is_duplicate_area` when totalling.
    if "town_village" in wide.columns and "tv_seq_no" in wide.columns:
        dup_codes = wide.loc[wide.duplicated("town_village", keep=False), "town_village"]
        if not dup_codes.empty:
            is_part = wide["town_village"].isin(set(dup_codes)) & (
                wide["tv_seq_no"] < wide.groupby("town_village")["tv_seq_no"].transform("max")
            )
            wide["_is_duplicate_area"] = is_part
            warnings.append(
                f"{int(is_part.sum())} row(s) share a town_village code with a "
                "higher tv_seq_no entry and are parts of it, not separate areas. "
                "They are flagged in '_is_duplicate_area'; exclude them when summing "
                "or totals will exceed the district figures."
            )
        else:
            wide["_is_duplicate_area"] = False

    if wide["state_name"].isna().any():
        warnings.append("Some state codes had no name in the API codelist.")
    return wide, warnings
