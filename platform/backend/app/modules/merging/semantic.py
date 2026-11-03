"""SEMANTIC DATA LAYER: canonical field dictionary + column matching.

The hard part of this platform isn't downloading data — it's recognizing
that `District`, `district_name`, `Dist Name` and `DISTRICT_NM` all mean
the same thing so two datasets can actually be joined.

Matching runs in three tiers, most trustworthy first:
  1. exact alias hit in CANONICAL_FIELDS          -> confidence 1.0
  2. fuzzy match against a canonical name/alias   -> confidence = ratio
  3. direct fuzzy match between two raw column
     names, when neither maps to a canonical field -> capped confidence

Deliberately dependency-free (difflib, not rapidfuzz) and deterministic —
no LLM call is needed for the common cases. Claude can be layered on later
for the genuinely ambiguous ones.

The dictionary itself lives in app/constants/canonical_fields.py — extend it
there, no code changes needed here.
"""
from difflib import SequenceMatcher

from app.constants.canonical_fields import (
    CANONICAL_FIELDS,
    CANONICAL_THRESHOLD,
    # Re-exported, not used here: merging.service reads it as
    # `semantic.KEY_LIKE_FIELDS` to pick its auto join-key candidates.
    KEY_LIKE_FIELDS,  # noqa: F401
)
from app.helpers.dataframe import snake_case


def match_to_canonical(column_name: str) -> tuple[str | None, float]:
    """Map one column name onto a canonical field, with confidence."""
    normalized = snake_case(column_name)

    for canonical, aliases in CANONICAL_FIELDS.items():
        if normalized == canonical or normalized in aliases:
            return canonical, 1.0

    best_canonical: str | None = None
    best_score = 0.0
    for canonical, aliases in CANONICAL_FIELDS.items():
        score = max(
            SequenceMatcher(None, normalized, candidate).ratio()
            for candidate in [canonical, *aliases]
        )
        if score > best_score:
            best_canonical, best_score = canonical, score

    if best_score >= CANONICAL_THRESHOLD:
        return best_canonical, round(best_score, 3)
    return None, 0.0


def resolve_key_column(columns: list[str], key: str) -> tuple[str | None, float]:
    """Find which of `columns` represents `key` (a column name OR a canonical
    field name), so a join key can be named once and resolved per dataset."""
    if key in columns:
        return key, 1.0

    canonical_of_key, _ = match_to_canonical(key)
    target = canonical_of_key or snake_case(key)

    best_col, best_score = None, 0.0
    for col in columns:
        canonical, score = match_to_canonical(col)
        if canonical == target and score > best_score:
            best_col, best_score = col, score

    if best_col is not None:
        return best_col, round(best_score, 3)
    return None, 0.0
