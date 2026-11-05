"""Financial years in HMIS's form, `2019-2020`, so Website years join HMIS's report_year."""
import re
from datetime import date

FIRST_YEAR = 2005

_YEAR = re.compile(r"^(?:FY\s*)?(\d{4})\s*[-–—/]\s*(\d{2}|\d{4})$", re.IGNORECASE)


def financial_years(today: date | None = None) -> list[str]:
    """2005-2006 up to the financial year containing `today` (years start 1 April)."""
    today = today or date.today()
    last = today.year if today.month >= 4 else today.year - 1
    return [f"{y}-{y + 1}" for y in range(FIRST_YEAR, last + 1)]


def normalise_year(text: str | None) -> str | None:
    """'2019-20', '2019–20', 'FY 2019-20', '2019/2020' -> '2019-2020'; anything else None.

    A bare '2019' could be a calendar or a financial year, so it is never
    guessed.
    """
    if text is None:
        return None
    match = _YEAR.match(str(text).strip())
    if not match:
        return None
    first, second = int(match.group(1)), match.group(2)
    following = first + 1
    if len(second) == 2:
        if int(second) != following % 100:
            return None
    elif int(second) != following:
        return None
    return f"{first}-{following}"
