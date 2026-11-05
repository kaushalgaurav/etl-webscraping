"""Build saved Website tables straight into web_tables + web_raw, without a Search."""
import itertools

import pandas as pd

from app.models import WebTable
from app.models.web_base import bulk_insert
from app.modules.scraping import importer

_surveys = itertools.count(5000)


def new_survey() -> int:
    """A survey id no other test uses: the test database is shared."""
    return next(_surveys)


def saved_table(db, survey: int, header: list[str], rows: list[list], *, name="Deliveries",
                place="District", state_name="Bihar", state_column=None, year="2019-2020",
                year_column=None, indicator_column=None,
                page="https://health.example.gov.in/stats") -> WebTable:
    df = pd.DataFrame(rows, columns=header)
    table = WebTable(survey_id=survey, name=name, page_url=page, source_locator={"n": name},
                     place_column=place, state_name=None if state_column else state_name,
                     state_column=state_column, year=None if year_column else year,
                     year_column=year_column, indicator_column=indicator_column, status="saved")
    db.add(table)
    db.flush()
    own = {place, state_column, year_column, indicator_column}
    values = [c for c in header if c not in own]
    bulk_insert(db.connection(), importer.WebRaw,
                importer._raw_rows(df, table, survey, values, place, state_column, year_column,
                                   indicator_column))
    db.commit()
    return table
