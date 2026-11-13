import itertools

import pytest

from app.models import Dataset, ScrapeFinding, ScrapeScan, WebRaw, WebTable
from app.models import web_base
from app.modules.scraping import importer, service, store

from tests.scraping import helpers

YEARLY_CSV = ("State,District,Year,Beds\n"
              "Bihar,Patna,2019-20,10\nBihar,Patna,2020-21,12\nBihar,Gaya,2019-20,7\n").encode()


# Long shape: one row per district per indicator (as NFHS district files are).
LONG_CSV = ("State,District,Indicator,NFHS-5,NFHS-4\n"
            "Bihar,Patna,Women literate (%),70.1,60.2\n"
            "Bihar,Patna,Children stunted (%),30.5,35.0\n"
            "Bihar,Gaya,Women literate (%),55.0,50.1\n"
            "Bihar,Gaya,,1,2\n").encode()


def done_scan(db, httpserver, loopback_ok):
    return helpers.run(db, helpers.standard_site(httpserver), loopback_ok)


def yearly_scan(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/yearly").respond_with_data(
        helpers.page('<a href="/files/yearly.csv">CSV</a>', helpers.ARTICLE), content_type="text/html")
    httpserver.expect_request("/files/yearly.csv").respond_with_data(YEARLY_CSV, content_type="text/csv")
    return helpers.run(db, httpserver.url_for("/yearly"), loopback_ok)


def long_scan(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/long").respond_with_data(
        helpers.page('<a href="/files/long.csv">CSV</a>', helpers.ARTICLE), content_type="text/html")
    httpserver.expect_request("/files/long.csv").respond_with_data(LONG_CSV, content_type="text/csv")
    return helpers.run(db, httpserver.url_for("/long"), loopback_ok)


def tables(db, scan):
    return (db.query(ScrapeFinding).filter(ScrapeFinding.scan_id == scan.id,
                                           ScrapeFinding.kind.in_(service.TABLE_KINDS))
            .order_by(ScrapeFinding.position).all())


def csv_table(db, scan):
    return [t for t in tables(db, scan) if t.kind == "file_table"][0]


def item(finding, **extra):
    return {"finding_id": finding.key, "state_name": "Bihar", "year": "2022-2023", **extra}


_surveys = itertools.count(1000)


@pytest.fixture
def survey():
    """A survey id no other test uses: the test database is shared."""
    return next(_surveys)


def save(db, scan, items, survey_id):
    importer.claim_for_import(db, scan.key, items)
    return importer.run_import(db, scan.key, survey_id, items)


def test_save_writes_web_tables_and_raw(db, httpserver, loopback_ok, survey):
    scan = done_scan(db, httpserver, loopback_ok)
    csv = csv_table(db, scan)
    result = save(db, scan, [item(csv, name="Bihar beds")], survey)
    t = db.query(WebTable).filter_by(survey_id=survey).one()
    assert (t.name, t.place_column, t.state_name, t.year, t.status) == (
        "Bihar beds", "District", "Bihar", "2022-2023", "saved")
    raw = db.query(WebRaw).filter_by(web_table_id=t.id).order_by(WebRaw.row_number).all()
    assert len(raw) == 38  # 38 rows x one value column (Beds)
    assert (raw[0].row_number, raw[0].place_text, raw[0].column_name, raw[0].value_text) == (
        1, "District 0", "Beds", "0")
    card = db.get(Dataset, t.dataset_id)
    assert card.table_name == "web_raw" and card.stage == "raw" and card.connector == "web_file"
    assert card.survey_id == survey and card.row_count == 38 and scan.key in card.notes
    got = result["items"][0]
    assert got == {"finding_id": csv.key, "web_table_id": t.key, "dataset_id": card.id,
                   "rows": 38, "values": 38, "replaced": False}
    assert not db.query(Dataset).filter(Dataset.table_name.like("ds_%")).count()


def test_same_page_same_year_replaces(db, httpserver, loopback_ok, survey):
    first = done_scan(db, httpserver, loopback_ok)
    save(db, first, [item(csv_table(db, first))], survey)
    second = done_scan(db, httpserver, loopback_ok)
    result = save(db, second, [item(csv_table(db, second))], survey)
    t = db.query(WebTable).filter_by(survey_id=survey).one()
    assert result["items"][0]["replaced"] is True and t.finding_id == csv_table(db, second).id
    assert db.query(WebRaw).filter_by(survey_id=survey).count() == 38


def test_same_page_other_year_adds(db, httpserver, loopback_ok, survey):
    first = done_scan(db, httpserver, loopback_ok)
    save(db, first, [item(csv_table(db, first))], survey)
    second = done_scan(db, httpserver, loopback_ok)
    save(db, second, [item(csv_table(db, second), year="2023-2024")], survey)
    assert sorted(t.year for t in db.query(WebTable).filter_by(survey_id=survey)) == ["2022-2023", "2023-2024"]
    assert db.query(WebRaw).filter_by(survey_id=survey).count() == 76


def test_other_survey_is_separate(db, httpserver, loopback_ok, survey):
    first = done_scan(db, httpserver, loopback_ok)
    save(db, first, [item(csv_table(db, first))], survey)
    second = done_scan(db, httpserver, loopback_ok)
    save(db, second, [item(csv_table(db, second))], survey + 5000)
    assert db.query(WebTable).filter(WebTable.survey_id.in_([survey, survey + 5000])).count() == 2


def test_year_column_rows_keep_their_year(db, httpserver, loopback_ok, survey):
    scan = yearly_scan(db, httpserver, loopback_ok)
    csv = csv_table(db, scan)
    save(db, scan, [{"finding_id": csv.key, "year_column": "Year"}], survey)
    t = db.query(WebTable).filter_by(survey_id=survey).one()
    assert t.year is None and t.year_column == "Year" and t.state_column == "State"
    raw = db.query(WebRaw).filter_by(web_table_id=t.id).order_by(WebRaw.row_number).all()
    assert [(r.place_text, r.state_text, r.year_text, r.column_name, r.value_text) for r in raw] == [
        ("Patna", "Bihar", "2019-20", "Beds", "10"), ("Patna", "Bihar", "2020-21", "Beds", "12"),
        ("Gaya", "Bihar", "2019-20", "Beds", "7")]


def test_kept_columns_only(db, httpserver, loopback_ok, survey):
    scan = yearly_scan(db, httpserver, loopback_ok)
    csv = csv_table(db, scan)
    save(db, scan, [{"finding_id": csv.key, "year": "2019-2020", "columns": ["Beds"]}], survey)
    assert {r.column_name for r in db.query(WebRaw).filter_by(survey_id=survey)} == {"Beds"}
    assert db.query(WebTable).filter_by(survey_id=survey).one().kept_columns == ["Beds"]


def test_indicator_column_names_each_value(db, httpserver, loopback_ok, survey):
    scan = long_scan(db, httpserver, loopback_ok)
    csv = csv_table(db, scan)
    item = {"finding_id": csv.key, "state_column": "State", "place_column": "District",
            "year": "2019-2020", "indicator_column": "Indicator", "columns": ["NFHS-5"]}
    result = save(db, scan, [item], survey)
    t = db.query(WebTable).filter_by(survey_id=survey).one()
    assert t.indicator_column == "Indicator"
    raw = db.query(WebRaw).filter_by(web_table_id=t.id).order_by(WebRaw.row_number).all()
    # The row with no indicator name has nothing to file its value under.
    assert [(r.place_text, r.column_name, r.value_text) for r in raw] == [
        ("Patna", "Women literate (%)", "70.1"), ("Patna", "Children stunted (%)", "30.5"),
        ("Gaya", "Women literate (%)", "55.0")]
    assert result["items"][0]["values"] == 3


def test_indicator_column_with_several_value_columns(db, httpserver, loopback_ok, survey):
    scan = long_scan(db, httpserver, loopback_ok)
    csv = csv_table(db, scan)
    item = {"finding_id": csv.key, "state_column": "State", "place_column": "District",
            "year": "2019-2020", "indicator_column": "Indicator"}
    save(db, scan, [item], survey)
    names = [r.column_name for r in db.query(WebRaw).filter_by(survey_id=survey)
             .order_by(WebRaw.row_number, WebRaw.id)][:2]
    assert names == ["Women literate (%) · NFHS-5", "Women literate (%) · NFHS-4"]


@pytest.mark.parametrize("extra,message", [
    ({"indicator_column": "Nope"}, "Column 'Nope' is not in this table."),
    ({"indicator_column": "District"},
     "The indicator column can't also be the district, state or year column."),
])
def test_indicator_column_validation(db, httpserver, loopback_ok, extra, message):
    scan = long_scan(db, httpserver, loopback_ok)
    bad = {"finding_id": csv_table(db, scan).key, "state_column": "State",
           "place_column": "District", "year": "2019-2020", **extra}
    with pytest.raises(service.ScanRequestError) as e:
        importer.claim_for_import(db, scan.key, [bad])
    assert str(e.value) == message


@pytest.mark.parametrize("extra,message", [
    ({"year": None}, "Choose a year, or the column that holds the year."),
    ({"year_column": "Year"}, "Choose a year, or the column that holds the year."),
    ({"year": "2019-20"}, "Year must be a financial year like 2019-2020."),
    ({"place_column": "Nope"}, "Column 'Nope' is not in this table."),
    ({"columns": ["Beds", "Nope"]}, "Column 'Nope' is not in this table."),
    ({"state_name": None}, "Choose the state, or the column that holds it."),
    ({"state_name": "Atlantis"}, "Unknown state 'Atlantis'."),
])
def test_claim_validation_messages(db, httpserver, loopback_ok, extra, message):
    scan = done_scan(db, httpserver, loopback_ok)
    bad = {k: v for k, v in item(csv_table(db, scan), **extra).items() if v is not None}
    with pytest.raises(service.ScanRequestError) as e:
        importer.claim_for_import(db, scan.key, [bad])
    assert str(e.value) == message
    db.refresh(scan)
    assert scan.status == "done"


def test_table_without_place_needs_no_state(db, httpserver, loopback_ok, survey):
    scan = done_scan(db, httpserver, loopback_ok)
    nav = [t for t in tables(db, scan) if t.title != "District beds" and t.kind == "html_table"][0]
    save(db, scan, [{"finding_id": nav.key, "year": "2022-2023"}], survey)
    t = db.query(WebTable).filter_by(survey_id=survey).one()
    assert t.place_column is None and db.query(WebRaw).filter_by(survey_id=survey, place_text=None).count() > 0


def test_after_import_unpicked_are_gone(db, httpserver, loopback_ok, survey):
    scan = done_scan(db, httpserver, loopback_ok)
    csv = csv_table(db, scan)
    save(db, scan, [item(csv)], survey)
    db.refresh(scan)
    assert scan.status == "imported" and scan.expires_at is None
    assert not store.scan_dir(scan.key).exists()
    left = db.query(ScrapeFinding).filter_by(scan_id=scan.id).all()
    assert [f.key for f in left] == [csv.key] and left[0].imported_dataset_id
    assert left[0].parent_id is None


def test_claim_rules(db, httpserver, loopback_ok, survey):
    scan = done_scan(db, httpserver, loopback_ok)
    other = done_scan(db, httpserver, loopback_ok)
    table = csv_table(db, scan)
    text = db.query(ScrapeFinding).filter_by(scan_id=scan.id, kind="text").one()
    with pytest.raises(service.ScanNotFound):
        importer.claim_for_import(db, "nope", [item(table)])
    with pytest.raises(service.ScanRequestError):
        importer.claim_for_import(db, scan.key, [])
    with pytest.raises(service.ScanRequestError):
        importer.claim_for_import(db, scan.key, [item(csv_table(db, other))])
    with pytest.raises(service.ScanRequestError):
        importer.claim_for_import(db, scan.key, [item(text)])
    with pytest.raises(service.ScanRequestError):
        importer.claim_for_import(db, scan.key, [item(table), item(table)])
    db.refresh(scan)
    assert scan.status == "done"
    scan.status = "running"
    db.commit()
    with pytest.raises(service.ScanConflict):
        importer.claim_for_import(db, scan.key, [item(table)])


def test_claim_is_atomic(db, httpserver, loopback_ok, survey):
    scan = done_scan(db, httpserver, loopback_ok)
    items = [item(csv_table(db, scan))]
    importer.claim_for_import(db, scan.key, items)
    db.refresh(scan)
    assert scan.status == "importing"
    with pytest.raises(service.ScanConflict):
        importer.claim_for_import(db, scan.key, items)


def test_already_imported_finding_is_conflict(db, httpserver, loopback_ok, survey):
    scan = done_scan(db, httpserver, loopback_ok)
    table = csv_table(db, scan)
    table.imported_dataset_id = "abc"
    db.commit()
    with pytest.raises(service.ScanConflict) as e:
        importer.claim_for_import(db, scan.key, [item(table)])
    assert "abc" in str(e.value)


def test_failed_save_keeps_scan_retryable(db, httpserver, loopback_ok, monkeypatch, survey):
    scan = done_scan(db, httpserver, loopback_ok)
    first, second = tables(db, scan)[:2]
    real = web_base.bulk_insert
    calls = []

    def flaky(conn, model, rows, chunk=5000):
        calls.append(model)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(conn, model, rows, chunk)

    monkeypatch.setattr(importer, "bulk_insert", flaky)
    with pytest.raises(RuntimeError):
        save(db, scan, [item(first, place_column=None), item(second, place_column=None)], survey)
    db.expire_all()
    scan = db.get(ScrapeScan, scan.id)
    assert scan.status == "done" and store.scan_dir(scan.key).exists()
    assert db.get(ScrapeFinding, first.id).imported_dataset_id
    monkeypatch.setattr(importer, "bulk_insert", real)
    result = save(db, scan, [item(second, place_column=None)], survey)
    assert len(result["items"]) == 1


def test_long_titles_fit_the_dataset_name_column(db, httpserver, loopback_ok, survey):
    """datasets.name is VARCHAR(255): Postgres rejects anything longer."""
    scan = done_scan(db, httpserver, loopback_ok)
    table = csv_table(db, scan)
    table.title = "Very long heading " * 20  # 360 characters
    db.commit()
    got = save(db, scan, [item(table)], survey)["items"][0]
    assert len(db.get(Dataset, got["dataset_id"]).name) <= 255


def test_source_ref_for_feed():
    scan = ScrapeScan(url="https://x/page", final_url="https://x/page")
    finding = ScrapeFinding(kind="feed", locator={
        "source": "feed", "feed_url": "https://x/api?page=1", "path": "$.data", "pages": 3})
    assert importer._source_ref(scan, finding) == "https://x/api?page=1 [path=$.data, pages=3]"


def _partial_save(db, scan, survey, monkeypatch):
    """Save two tables where the second fails: the first stays saved."""
    first, second = csv_table(db, scan), tables(db, scan)[1]
    real = web_base.bulk_insert
    calls = []

    def flaky(conn, model, rows, chunk=5000):
        calls.append(model)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(conn, model, rows, chunk)

    monkeypatch.setattr(importer, "bulk_insert", flaky)
    with pytest.raises(RuntimeError):
        save(db, scan, [item(first), item(second)], survey)
    monkeypatch.setattr(importer, "bulk_insert", real)
    db.expire_all()
    return db.get(ScrapeScan, scan.id)


def test_partly_saved_scan_is_kept_and_never_purged(db, httpserver, loopback_ok, monkeypatch, survey):
    from datetime import timedelta
    from app.models.common import utcnow
    scan = _partial_save(db, done_scan(db, httpserver, loopback_ok), survey, monkeypatch)
    assert scan.status == "done" and scan.expires_at is None  # provenance now
    service.purge_expired(db, utcnow() + timedelta(days=3))  # must not hit the FK
    assert db.get(ScrapeScan, scan.id) is not None
    with pytest.raises(service.ScanConflict):
        service.delete_scan(db, scan.key)
    assert service.create_scan(db, "https://example.gov.in/next", True).key


def test_restart_mid_save_keeps_partly_saved_scan(db, httpserver, loopback_ok, monkeypatch, survey):
    scan = _partial_save(db, done_scan(db, httpserver, loopback_ok), survey, monkeypatch)
    scan.status, scan.expires_at = "importing", scan.created_at
    db.commit()
    service.reap_interrupted_scans(db)
    db.refresh(scan)
    assert scan.status == "done" and scan.expires_at is None


def test_resave_removes_the_old_record_card(db, httpserver, loopback_ok, survey):
    first = done_scan(db, httpserver, loopback_ok)
    old = save(db, first, [item(csv_table(db, first))], survey)["items"][0]["dataset_id"]
    second = done_scan(db, httpserver, loopback_ok)
    new = save(db, second, [item(csv_table(db, second))], survey)["items"][0]["dataset_id"]
    assert db.get(Dataset, old) is None and db.get(Dataset, new) is not None
