import pytest

from app.models import Dataset, ScrapeFinding, ScrapeScan
from app.modules.ingestion import service as ingestion_service
from app.modules.scraping import importer, service, store

from tests.scraping import helpers


def done_scan(db, httpserver, loopback_ok):
    return helpers.run(db, helpers.standard_site(httpserver), loopback_ok)


def tables(db, scan):
    return (db.query(ScrapeFinding).filter(ScrapeFinding.scan_id == scan.id,
                                           ScrapeFinding.kind.in_(service.TABLE_KINDS))
            .order_by(ScrapeFinding.position).all())


def save(db, scan, items, survey_id=7):
    importer.claim_for_import(db, scan.id, items)
    return importer.run_import(db, scan.id, survey_id, items)


def test_import_stores_raw_and_clean(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    found = tables(db, scan)
    html = [t for t in found if t.title == "District beds"][0]
    csv = [t for t in found if t.kind == "file_table"][0]
    result = save(db, scan, [{"finding_id": csv.id, "name": "Bihar beds"}, {"finding_id": html.id}])
    by_finding = {i["finding_id"]: i for i in result["items"]}
    assert set(by_finding[csv.id]) == {"finding_id", "raw_dataset_id", "dataset_id", "rows", "columns"}

    raw = db.get(Dataset, by_finding[csv.id]["raw_dataset_id"])
    clean = db.get(Dataset, by_finding[csv.id]["dataset_id"])
    assert raw.stage == "raw" and clean.stage == "clean"
    assert raw.connector == "web_file" and clean.connector == "web_file"
    assert raw.survey_id == 7 and clean.survey_id == 7
    assert raw.source_ref.endswith("/files/data.csv") and raw.raw_file_path is None
    assert raw.name == "Bihar beds" and clean.name == "Bihar beds (cleaned)"
    assert clean.parent_ids == [raw.id] and raw.row_count == 38
    assert scan.id in raw.notes and csv.id in raw.notes and "38 rows" in raw.notes

    html_raw = db.get(Dataset, by_finding[html.id]["raw_dataset_id"])
    assert html_raw.connector == "web_table" and html_raw.name == "localhost - District beds"
    assert html_raw.source_ref.endswith("/stats [table_index=1]")


def test_after_import_unpicked_are_gone(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    csv = [t for t in tables(db, scan) if t.kind == "file_table"][0]
    save(db, scan, [{"finding_id": csv.id}])
    db.refresh(scan)
    assert scan.status == "imported" and scan.expires_at is None
    assert not store.scan_dir(scan.id).exists()
    left = db.query(ScrapeFinding).filter_by(scan_id=scan.id).all()
    assert [f.id for f in left] == [csv.id] and left[0].imported_dataset_id


def test_claim_rules(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    other = done_scan(db, httpserver, loopback_ok)
    table = tables(db, scan)[0]
    text = db.query(ScrapeFinding).filter_by(scan_id=scan.id, kind="text").one()
    with pytest.raises(service.ScanNotFound):
        importer.claim_for_import(db, "nope", [{"finding_id": table.id}])
    with pytest.raises(service.ScanRequestError):
        importer.claim_for_import(db, scan.id, [])
    with pytest.raises(service.ScanRequestError):
        importer.claim_for_import(db, scan.id, [{"finding_id": tables(db, other)[0].id}])
    with pytest.raises(service.ScanRequestError):
        importer.claim_for_import(db, scan.id, [{"finding_id": text.id}])
    with pytest.raises(service.ScanRequestError):
        importer.claim_for_import(db, scan.id, [{"finding_id": table.id}, {"finding_id": table.id}])
    db.refresh(scan)
    assert scan.status == "done"  # bad requests never claim the scan
    scan.status = "running"
    db.commit()
    with pytest.raises(service.ScanConflict):
        importer.claim_for_import(db, scan.id, [{"finding_id": table.id}])


def test_claim_is_atomic(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    items = [{"finding_id": tables(db, scan)[0].id}]
    importer.claim_for_import(db, scan.id, items)
    db.refresh(scan)
    assert scan.status == "importing"
    with pytest.raises(service.ScanConflict):
        importer.claim_for_import(db, scan.id, items)


def test_already_imported_finding_is_conflict(db, httpserver, loopback_ok):
    scan = done_scan(db, httpserver, loopback_ok)
    table = tables(db, scan)[0]
    table.imported_dataset_id = "abc"
    db.commit()
    with pytest.raises(service.ScanConflict) as e:
        importer.claim_for_import(db, scan.id, [{"finding_id": table.id}])
    assert "abc" in str(e.value)


def test_failed_import_keeps_scan_retryable(db, httpserver, loopback_ok, monkeypatch):
    scan = done_scan(db, httpserver, loopback_ok)
    first, second = tables(db, scan)[:2]
    real = ingestion_service.store_clean_copy
    calls = []

    def flaky(db_, raw, name):
        calls.append(name)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(db_, raw, name)

    monkeypatch.setattr(ingestion_service, "store_clean_copy", flaky)
    with pytest.raises(RuntimeError):
        save(db, scan, [{"finding_id": first.id}, {"finding_id": second.id}])
    db.expire_all()
    scan = db.get(ScrapeScan, scan.id)
    assert scan.status == "done" and store.scan_dir(scan.id).exists()
    assert db.get(ScrapeFinding, first.id).imported_dataset_id
    monkeypatch.setattr(ingestion_service, "store_clean_copy", real)
    result = save(db, scan, [{"finding_id": second.id}])
    assert len(result["items"]) == 1


def test_long_titles_fit_the_dataset_name_column(db, httpserver, loopback_ok):
    """datasets.name is VARCHAR(255): Postgres rejects anything longer."""
    scan = done_scan(db, httpserver, loopback_ok)
    table = tables(db, scan)[0]
    table.title = "Very long heading " * 20  # 360 characters
    db.commit()
    item = save(db, scan, [{"finding_id": table.id}])["items"][0]
    raw = db.get(Dataset, item["raw_dataset_id"])
    clean = db.get(Dataset, item["dataset_id"])
    assert len(raw.name) <= 255 and len(clean.name) <= 255
    assert clean.name.endswith(" (cleaned)")


def test_source_ref_for_feed():
    scan = ScrapeScan(url="https://x/page", final_url="https://x/page")
    finding = ScrapeFinding(kind="feed", locator={
        "source": "feed", "feed_url": "https://x/api?page=1", "path": "$.data", "pages": 3})
    assert importer._source_ref(scan, finding) == "https://x/api?page=1 [path=$.data, pages=3]"
