import uuid

from app.models import ScrapeFinding, ScrapeScan


def test_scan_and_finding_use_the_base(db):
    scan = ScrapeScan(url="https://example.gov.in/x", status="running")
    db.add(scan)
    db.flush()
    db.add(ScrapeFinding(scan_id=scan.id, position=0, kind="html_table", title="T",
                         locator={"source": "page", "table_index": 0},
                         signals={"numeric_share": 0.5}))
    db.commit()
    got = db.query(ScrapeFinding).filter_by(scan_id=scan.id).one()
    assert isinstance(scan.id, int) and isinstance(scan.external_id, uuid.UUID)
    assert got.scan_id == scan.id and got.updated_at is not None
    assert scan.key == str(scan.external_id) and got.locator["table_index"] == 0
    assert scan.blocked == [] and scan.options == {}
