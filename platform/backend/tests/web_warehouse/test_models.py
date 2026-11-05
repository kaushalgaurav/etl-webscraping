import time
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import WebMaster, WebRaw, WebTable
from app.models.web_base import bulk_insert


def make_table(db, **extra):
    t = WebTable(survey_id=7, name="Deliveries", page_url="https://x/y", source_locator={},
                 status="saved", **extra)
    db.add(t)
    db.commit()
    return t


def test_base_columns_fill_themselves(db):
    t = make_table(db)
    assert isinstance(t.id, int) and isinstance(t.external_id, uuid.UUID)
    assert t.created_at is not None and t.updated_at is not None


def test_updated_at_moves_on_update(db):
    t = make_table(db)
    before = t.updated_at
    time.sleep(0.01)
    t.status = "cleaned"
    db.commit()
    db.refresh(t)
    assert t.updated_at > before


def test_bulk_insert_fills_base_columns(db):
    t = make_table(db)
    n = bulk_insert(db.connection(), WebRaw, [{
        "survey_id": 7, "web_table_id": t.id, "row_number": 1,
        "column_name": "Beds", "value_text": "10"}])
    db.commit()
    row = db.query(WebRaw).filter_by(web_table_id=t.id).one()
    assert n == 1 and isinstance(row.external_id, uuid.UUID)
    assert row.created_at is not None and row.updated_at is not None


def test_master_key_is_unique(db):
    t = make_table(db)
    key = dict(survey_id=7, web_table_id=t.id, unique_id=101, year="2019-2020", indicator="beds")
    db.add(WebMaster(value=1.0, **key))
    db.commit()
    db.add(WebMaster(value=2.0, **key))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
