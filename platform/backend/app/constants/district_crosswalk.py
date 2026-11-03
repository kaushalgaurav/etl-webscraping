"""District master: state/district name -> stable state, district and unique ids.

Every source names a district differently — HMIS writes "Kaimur (Bhabua)",
the census "Kaimur (Bhabua)", PLFS "Kaimur". Joining on the name alone is
therefore unreliable, and joining on a source's own district code is worse:
the codes differ between sources entirely. `district_crosswalk.tsv` is the
one list all of them resolve to, giving `unique_id` as the join key.

Matching is on a normalised name and is scoped to the state, because district
names repeat across states — "Aurangabad" is in both Bihar and Maharashtra,
"Bilaspur" in both Himachal Pradesh and Chhattisgarh, "Balrampur" in both
Uttar Pradesh and Chhattisgarh. A name-only lookup would silently pick one.
"""
import csv
import re
from functools import lru_cache
from pathlib import Path

from app.constants.state_aliases import STATE_ALIASES

TSV_PATH = Path(__file__).with_name("district_crosswalk.tsv")

# Punctuation and the words sources add or drop around a district name.
_NOISE = re.compile(
    r"\b(district|distt|dist|zila|zilla|division|city|rural|urban)\b", re.I
)


def normalise(name: str) -> str:
    """Lowercase, drop punctuation and filler words, collapse whitespace."""
    text = str(name).strip().lstrip("_")
    text = text.replace("&", " and ")
    text = _NOISE.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def canonical_state(name: str) -> str:
    """Canonical state spelling, via the shared alias table."""
    key = str(name).strip().lstrip("_").lower()
    return STATE_ALIASES.get(key, str(name).strip().lstrip("_"))


@lru_cache(maxsize=1)
def _tables() -> tuple[dict, dict]:
    """(by (state, district), by state) lookups built from the TSV."""
    by_pair: dict[tuple[str, str], dict] = {}
    by_state: dict[str, dict] = {}
    with TSV_PATH.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            state, district = row.get("state", ""), row.get("district", "")
            if not state or not district:
                continue
            entry = {
                "state": state,
                "district": district,
                "state_id": int(row["state_id"]),
                "district_id": int(row["district_id"]),
                "unique_id": int(row["unique_id"]),
            }
            state_key = normalise(canonical_state(state))
            by_pair[(state_key, normalise(district))] = entry
            by_state.setdefault(state_key, {"state": state, "state_id": entry["state_id"]})
    return by_pair, by_state


# Names a source spells differently from the master list. Only genuine
# synonyms for the same district belong here — a district that did not exist
# when the master was drawn must stay unmatched rather than be mapped to a
# neighbour.
DISTRICT_ALIASES = {
    "cuddapah": "Y.S.R. (Cuddapah)",
    "ysr": "Y.S.R. (Cuddapah)",
    "ysr kadapa": "Y.S.R. (Cuddapah)",
    "dahod": "Dohad",
    "bagpat": "Baghpat",
    "ranga reddy": "Rangareddy",
    "barabanki": "Bara Banki",
    "siddharth nagar": "Siddharthnagar",
    "baloda bazar": "Balodabazar",
    "kaimur": "Kaimur (Bhabua)",
    "bhabua": "Kaimur (Bhabua)",
    "leh": "Leh (Ladakh)",
    "sant ravidas nagar": "Sant Ravidas Nagar (Bhadohi)",
    "bhadohi": "Sant Ravidas Nagar (Bhadohi)",
    "sri potti sriramulu nellore": "Sri Potti Sriramulu Nellore",
    "nellore": "Sri Potti Sriramulu Nellore",
    "bangalore urban": "Bangalore",
    "bengaluru": "Bangalore",
    "bangalore rural": "Bangalore (Rural)",
}


def _squash(name: str) -> str:
    """Normalised name with every separator removed: "Baloda Bazar" -> "balodabazar"."""
    return re.sub(r"[^a-z0-9]", "", normalise(name))


def is_summary_row(district: str) -> bool:
    """True for a report's own state-total row, which is not a district.

    HMIS writes the state total as "_Bihar" inside the state's own file.
    Matching it against the district list would either fail or, worse,
    match a same-named district.
    """
    return str(district).strip().startswith("_")


def lookup_district(state: str, district: str) -> dict | None:
    """Crosswalk row for a (state, district) pair, or None if unmatched.

    Tried in order, always scoped to the state: exact normalised name, a known
    alias, the name with separators removed, then the master name that
    contains it ("Cuddapah" inside "Y.S.R. (Cuddapah)"). A district created
    after this master was drawn has no id and returns None — the caller must
    see that rather than get a wrong one.
    """
    if is_summary_row(district):
        return None
    by_pair, _ = _tables()
    state_key = normalise(canonical_state(state))

    hit = by_pair.get((state_key, normalise(district)))
    if hit:
        return hit

    alias = DISTRICT_ALIASES.get(normalise(district))
    if alias:
        hit = by_pair.get((state_key, normalise(alias)))
        if hit:
            return hit

    target = _squash(district)
    in_state = {d: v for (st, d), v in by_pair.items() if st == state_key}
    for name, entry in in_state.items():
        if _squash(name) == target:
            return entry

    # "South West" for "South West Delhi": the source drops the state suffix.
    suffixed = _squash(f"{district} {canonical_state(state)}")
    for name, entry in in_state.items():
        if _squash(name) == suffixed:
            return entry

    # A parenthesised master name containing the source's shorter name.
    candidates = [e for n, e in in_state.items()
                  if target and target in _squash(n) and len(target) >= 5]
    return candidates[0] if len(candidates) == 1 else None


def lookup_state(state: str) -> dict | None:
    """State name and id, or None."""
    _, by_state = _tables()
    return by_state.get(normalise(canonical_state(state)))


__all__ = ["lookup_district", "lookup_state", "normalise", "canonical_state", "TSV_PATH"]
