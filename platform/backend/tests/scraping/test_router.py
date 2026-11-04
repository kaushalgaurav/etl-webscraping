import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Job, ScrapeFinding, ScrapeScan
from app.modules.jobs import lineage
from app.modules.jobs import service as jobs_service
from app.modules.reports.naming import category_for
from app.modules.scraping import scan as scan_module
from app.modules.scraping import service
from app.modules.scraping.fetcher import Fetcher

from tests.scraping import helpers

client = TestClient(app)


@pytest.fixture
def no_workers(monkeypatch):
    """Queue jobs without running them, so the test drives each one itself."""
    monkeypatch.setattr(jobs_service._executor, "submit", lambda *a, **k: None)


@pytest.fixture
def local_fetcher(monkeypatch, loopback_ok):
    real = scan_module.run_scan
    monkeypatch.setattr(scan_module, "run_scan",
                        lambda db, scan_id, progress=None, fetcher=None:
                        real(db, scan_id, progress, Fetcher(policy=loopback_ok)))


def start_scan(url: str) -> dict:
    resp = client.post("/scrape/scan", json={"url": url})
    assert resp.status_code == 202, resp.text
    return resp.json()


def finish_scan(db, httpserver) -> str:
    job = start_scan(helpers.standard_site(httpserver))
    jobs_service._run(job["id"], job["result"]["request"])
    return job["result"]["request"]["scan_id"]


def table_ids(db, scan_id):
    return [f.id for f in db.query(ScrapeFinding).filter(
        ScrapeFinding.scan_id == scan_id, ScrapeFinding.kind.in_(service.TABLE_KINDS))
        .order_by(ScrapeFinding.position)]


def test_scan_returns_202_with_scan_id(db, no_workers):
    job = start_scan("https://example.gov.in/stats")
    assert job["job_type"] == "scrape_scan" and job["status"] == "queued"
    scan = db.get(ScrapeScan, job["result"]["request"]["scan_id"])
    assert scan.status == "running" and scan.job_id == job["id"]


def test_scan_rejects(no_workers):
    assert client.post("/scrape/scan", json={"url": "ftp://x.gov.in/a"}).status_code == 422
    resp = client.post("/scrape/scan", json={"url": "https://hmis.mohfw.gov.in/"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "This source has a dedicated connector; use the URL tab."
    assert client.post("/scrape/scan", json={}).status_code == 422


def test_job_runs_scan_end_to_end(db, httpserver, no_workers, local_fetcher):
    scan_id = finish_scan(db, httpserver)
    job = db.query(Job).filter(Job.job_type == "scrape_scan").order_by(Job.created_at.desc()).first()
    db.refresh(job)
    assert job.status == "succeeded" and job.result["summary"]["tables"] >= 1
    assert job.result["scan_id"] == scan_id
    m = client.get(f"/scrape/scans/{scan_id}")
    assert m.status_code == 200 and len(m.json()["tables"]) == 3
    listed = client.get("/scrape/scans?limit=5").json()
    assert listed[0]["scan_id"] == scan_id


def test_import_double_click(db, httpserver, no_workers, local_fetcher):
    scan_id = finish_scan(db, httpserver)
    body = {"survey_id": 7, "items": [{"finding_id": table_ids(db, scan_id)[0]}]}
    first = client.post(f"/scrape/scans/{scan_id}/import", json=body)
    second = client.post(f"/scrape/scans/{scan_id}/import", json=body)
    assert first.status_code == 202 and first.json()["job_type"] == "scrape_import"
    assert second.status_code == 409
    jobs = [j for j in db.query(Job).filter(Job.job_type == "scrape_import")
            if j.result["request"]["scan_id"] == scan_id]
    assert len(jobs) == 1


def test_import_job_and_lineage(db, httpserver, no_workers, local_fetcher):
    scan_id = finish_scan(db, httpserver)
    picks = table_ids(db, scan_id)[:2]
    job = client.post(f"/scrape/scans/{scan_id}/import",
                      json={"survey_id": 7, "items": [{"finding_id": f} for f in picks]}).json()
    jobs_service._run(job["id"], job["result"]["request"])
    stored = db.get(Job, job["id"])
    db.refresh(stored)
    assert stored.status == "succeeded", stored.error
    clean_ids = [i["dataset_id"] for i in stored.result["items"]]
    assert stored.dataset_id == clean_ids[0]
    assert lineage.dataset_ids_for_job(db, job["id"]) == clean_ids
    assert client.get(f"/scrape/scans/{scan_id}").json()["status"] == "imported"


def test_import_request_errors(db, httpserver, no_workers, local_fetcher):
    scan_id = finish_scan(db, httpserver)
    assert client.post("/scrape/scans/nope/import",
                       json={"survey_id": 7, "items": [{"finding_id": "x"}]}).status_code == 404
    assert client.post(f"/scrape/scans/{scan_id}/import",
                       json={"survey_id": 7, "items": []}).status_code == 422
    text_id = db.query(ScrapeFinding).filter_by(scan_id=scan_id, kind="text").one().id
    assert client.post(f"/scrape/scans/{scan_id}/import",
                       json={"survey_id": 7, "items": [{"finding_id": text_id}]}).status_code == 422


def test_rows_download_delete_status_codes(db, httpserver, no_workers, local_fetcher):
    scan_id = finish_scan(db, httpserver)
    table = table_ids(db, scan_id)[0]
    text_id = db.query(ScrapeFinding).filter_by(scan_id=scan_id, kind="text").one().id
    base = f"/scrape/scans/{scan_id}"

    rows = client.get(f"{base}/findings/{table}/rows?limit=5&offset=0")
    assert rows.status_code == 200 and len(rows.json()["rows"]) <= 5
    assert client.get(f"{base}/findings/{table}/rows?limit=5000").status_code == 422
    assert client.get(f"{base}/findings/{text_id}/rows").status_code == 422
    assert client.get(f"{base}/findings/nope/rows").status_code == 404

    dl = client.get(f"{base}/findings/{table}/download")
    assert dl.status_code == 200 and dl.headers["content-disposition"].startswith("attachment")
    assert dl.content.startswith("﻿".encode("utf-8"))
    assert client.get(f"{base}/findings/nope/download").status_code == 404

    assert client.get("/scrape/scans/nope").status_code == 404
    assert client.delete(base).status_code == 204
    assert client.get(base).status_code == 404
    assert client.delete(base).status_code == 404


def test_saved_scan_conflicts(db, httpserver, no_workers, local_fetcher):
    scan_id = finish_scan(db, httpserver)
    table = table_ids(db, scan_id)[0]
    scan = db.get(ScrapeScan, scan_id)
    scan.status = "imported"
    db.commit()
    assert client.get(f"/scrape/scans/{scan_id}/findings/{table}/rows").status_code == 409
    assert client.get(f"/scrape/scans/{scan_id}/findings/{table}/download").status_code == 409
    assert client.delete(f"/scrape/scans/{scan_id}").status_code == 409


def test_naming_categories():
    assert category_for("web_table") == "web"
    assert category_for("web_file") == "file"
    assert category_for("web_feed") == "api"


def test_import_rejects_overlong_name(db, httpserver, no_workers, local_fetcher):
    scan_id = finish_scan(db, httpserver)
    resp = client.post(f"/scrape/scans/{scan_id}/import",
                       json={"survey_id": 7, "items": [{"finding_id": table_ids(db, scan_id)[0],
                                                        "name": "x" * 300}]})
    assert resp.status_code == 422


def test_scan_accepts_render_option(db, no_workers):
    job = client.post("/scrape/scan", json={"url": "https://example.gov.in/a", "render": "always"}).json()
    assert db.get(ScrapeScan, job["result"]["request"]["scan_id"]).options["render"] == "always"
    assert job["result"]["request"]["render"] == "always"
    assert client.post("/scrape/scan", json={"url": "https://example.gov.in/a",
                                             "render": "sometimes"}).status_code == 422
    job = start_scan("https://example.gov.in/b")
    scan_id = job["result"]["request"]["scan_id"]
    assert db.get(ScrapeScan, scan_id).options["render"] == "auto"
    assert client.get(f"/scrape/scans/{scan_id}").json()["rendered_in_browser"] is False
