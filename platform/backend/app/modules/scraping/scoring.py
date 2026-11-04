"""Judge each found table, to suggest keep or skip (spec §4.7).

Only suggestions: the admin decides. Every signal here is cheap and
deterministic, computed on the full table before it is staged.
"""
import hashlib
import re

import pandas as pd

from app.constants import district_crosswalk
from app.modules.merging.semantic import match_to_canonical

UNMATCHED_LIMIT = 20
# Layout, number share and personal-data checks look at this many rows; a
# million-row table says nothing more about them than its first few thousand.
SAMPLE_ROWS = 5000
FILE_KINDS = ("file_table", "pdf_table", "docx_table")

NAV_WORDS = {
    "home", "about us", "contact us", "login", "sitemap", "faq", "search",
    "skip to main content", "screen reader access", "feedback", "help",
}
_PHONE = re.compile(r"^(\+?91)?[6-9]\d{9}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
_PERSONAL_HEADER = re.compile(r"contact|mobile|phone|e-?mail")
_NAME_EXEMPT = re.compile(
    r"district|state|block|village|facility|hospital|scheme|indicator|item|phc|chc|school|"
    r"company|city|town|ward|mandal|taluk|tehsil")
_PERSONAL_SHARE = 0.3


def _number(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def _cells(df: pd.DataFrame) -> list:
    return [v for v in df.to_numpy().ravel().tolist()
            if not (v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "")]


def numeric_share(df: pd.DataFrame) -> float:
    cells = _cells(df)
    if not cells:
        return 0.0
    return round(sum(_number(v) is not None for v in cells) / len(cells), 2)


def looks_like_layout(df: pd.DataFrame) -> bool:
    if df.shape[0] < 2 or df.shape[1] < 2:
        return True
    cells = _cells(df)
    if not cells:
        return True
    nav = sum(str(v).strip().lower() in NAV_WORDS for v in cells)
    return nav / len(cells) >= 0.5


def _place_columns(df: pd.DataFrame) -> tuple[str | None, str | None]:
    state = district = None
    for col in df.columns:
        canonical, _score = match_to_canonical(str(col))
        if canonical == "state" and state is None:
            state = col
        elif canonical == "district" and district is None:
            district = col
    return state, district


def _values(series: pd.Series) -> list[str]:
    return [str(v).strip() for v in series.tolist()
            if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip()]


def joinable(df: pd.DataFrame) -> dict | None:
    """Can this table's places be matched to the district master?"""
    state_col, district_col = _place_columns(df)
    if district_col is not None:
        # One check per distinct place, however many rows repeat it.
        places = df[[c for c in (state_col, district_col) if c is not None]].drop_duplicates()
        districts = places[district_col]
        pairs: list[tuple[str | None, str]] = []
        for i, raw in enumerate(districts.tolist()):
            if raw is None or (isinstance(raw, float) and pd.isna(raw)) or not str(raw).strip():
                continue
            name = str(raw).strip()
            if district_crosswalk.is_summary_row(name):
                continue
            state = None
            if state_col is not None:
                s = places[state_col].iloc[i]
                state = None if s is None or (isinstance(s, float) and pd.isna(s)) else str(s).strip()
            pairs.append((state, name))
        pairs = list(dict.fromkeys(pairs))
        unmatched = []
        matched = 0
        for state, name in pairs:
            hit = (district_crosswalk.lookup_district(state, name) if state
                   else district_crosswalk.find_district(name))
            if hit:
                matched += 1
            else:
                unmatched.append(name)
        return {"place_column": district_col, "state_column": state_col, "level": "district",
                "matched": matched, "total": len(pairs), "unmatched": unmatched[:UNMATCHED_LIMIT]}
    if state_col is not None:
        names = [n for n in dict.fromkeys(_values(df[state_col].drop_duplicates()))
                 if not district_crosswalk.is_summary_row(n)]
        unmatched = [n for n in names if district_crosswalk.lookup_state(n) is None]
        return {"place_column": state_col, "state_column": state_col, "level": "state",
                "matched": len(names) - len(unmatched), "total": len(names),
                "unmatched": unmatched[:UNMATCHED_LIMIT]}
    return None


def personal_data_columns(df: pd.DataFrame) -> list[str]:
    flagged = []
    for col in df.columns:
        header = str(col).lower()
        if _PERSONAL_HEADER.search(header) or ("name" in header and not _NAME_EXEMPT.search(header)):
            flagged.append(col)
            continue
        values = _values(df[col])
        if not values:
            continue
        phones = sum(bool(_PHONE.match(re.sub(r"[\s\-]", "", v))) for v in values)
        emails = sum(bool(_EMAIL.match(v)) for v in values)
        if max(phones, emails) / len(values) >= _PERSONAL_SHARE:
            flagged.append(col)
    return [str(c) for c in flagged]


def score(df: pd.DataFrame) -> dict:
    sample = df.head(SAMPLE_ROWS)
    return {
        "looks_like_layout": looks_like_layout(sample),
        "numeric_share": numeric_share(sample),
        "joinable": joinable(df),
        "personal_data": personal_data_columns(sample),
        "duplicate_of": None,
        "truncated": False,
    }


def suggest(signals: dict, rows: int, columns: int) -> str:
    keep = (not signals.get("looks_like_layout")
            and not signals.get("duplicate_of")
            and rows >= 3 and columns >= 2
            and (signals.get("numeric_share") or 0) > 0
            and not signals.get("personal_data"))
    return "keep" if keep else "skip"


def _normal_cell(value) -> str:
    number = _number(value)
    if number is not None:
        return str(int(number)) if number.is_integer() else repr(number)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def table_hash(df: pd.DataFrame) -> str:
    """Hash of the cell values only, so the same table read from HTML and
    from its CSV download (different headers, "1,234" vs 1234) match."""
    digest = hashlib.sha256()
    for row in df.itertuples(index=False, name=None):
        digest.update("\x1f".join(_normal_cell(v) for v in row).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def mark_duplicates(items: list[tuple[str, str, str]]) -> dict[str, str]:
    """{duplicate finding id: kept finding id}. A file's copy wins, then a data
    feed's (raw values), then the first one found."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for finding_id, kind, digest in items:
        groups.setdefault(digest, []).append((finding_id, kind))
    duplicates: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        keeper = next((fid for fid, kind in members if kind in FILE_KINDS),
                      next((fid for fid, kind in members if kind == "feed"), members[0][0]))
        for fid, _kind in members:
            if fid != keeper:
                duplicates[fid] = keeper
    return duplicates
