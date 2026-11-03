"""HMIS indicator dictionary: report label -> short code, programme, grouping.

The reports name an indicator in full prose ("% Pregnant women given 180 IFA
to Total ANC Registration"), which is unusable as a column or a join key and
is not stable across years. `hmis_indicators.tsv` maps each label to a short
`variable` code plus the programme and dashboard grouping it belongs to.

Matching is on a normalised form of the label, because the same indicator is
punctuated differently between years — spacing around brackets, "Sterlisation"
vs "Sterilisation", a stray trailing "#".
"""
import csv
import re
from functools import lru_cache
from pathlib import Path

TSV_PATH = Path(__file__).with_name("hmis_indicators.tsv")


def normalise(label: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace, for matching only."""
    text = str(label).lower().replace("–", "-")
    text = re.sub(r"[^a-z0-9%<>+]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=1)
def indicator_map() -> dict[str, dict]:
    """{normalised label: {variable, programme, grouping, indicator}}."""
    out: dict[str, dict] = {}
    with TSV_PATH.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            label = (row.get("indicator") or "").strip()
            if not label:
                continue
            out[normalise(label)] = {
                "indicator": label,
                "variable": (row.get("variable") or "").strip(),
                "programme": (row.get("programme") or "").strip(),
                "grouping": (row.get("grouping") or "").strip(),
            }
    return out


def lookup(label: str) -> dict | None:
    """Indicator metadata for a report's column label, or None if unmapped."""
    return indicator_map().get(normalise(label))


__all__ = ["indicator_map", "lookup", "normalise", "TSV_PATH"]
