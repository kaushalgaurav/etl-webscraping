from datetime import date

import pytest

from app.modules.web_warehouse import schema
from app.modules.web_warehouse.years import financial_years, normalise_year


def test_financial_years_run_to_the_current_one():
    assert financial_years(date(2026, 11, 5))[0] == "2005-2006"
    assert financial_years(date(2026, 11, 5))[-1] == "2026-2027"
    assert financial_years(date(2026, 3, 31))[-1] == "2025-2026"
    assert financial_years(date(2026, 4, 1))[-1] == "2026-2027"


@pytest.mark.parametrize("text,want", [
    ("2019-20", "2019-2020"), ("2019–20", "2019-2020"), ("2019-2020", "2019-2020"),
    ("2019/20", "2019-2020"), ("FY 2019-20", "2019-2020"), (" 2019 - 20 ", "2019-2020"),
    ("2099-00", "2099-2100"), ("1999-00", "1999-2000"),
    ("2019", None), ("2019-21", None), ("", None), (None, None), ("abc", None)])
def test_normalise_year(text, want):
    assert normalise_year(text) == want


def test_schema_names():
    assert schema.LAYERS == ("web_raw", "web_clean", "web_master")
    assert schema.is_layer_table("web_clean") and not schema.is_layer_table("web_tables")
    assert schema.COLUMN_PREFIX == "web_" and schema.MAX_DASHBOARD_COLUMNS == 1400
