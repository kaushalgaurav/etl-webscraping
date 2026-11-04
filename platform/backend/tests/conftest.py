"""Shared test setup.

The environment is set before any `app` import: settings are read once at
import time, so a throwaway SQLite file and staging folder must already be in
place, or the tests would touch the real database and data/ folder.
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="platform-tests-"))
os.environ["PLATFORM_DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["PLATFORM_SCRAPE_DIR"] = str(_TMP / "staging")
os.environ["PLATFORM_SCRAPE_HOST_INTERVAL_SECONDS"] = "0"
# Browser tests opt in (the `browser_ok` fixture); the rest stay plain HTTP.
os.environ["PLATFORM_SCRAPE_BROWSER_ENABLED"] = "false"

import pytest  # noqa: E402

import app.models  # noqa: E402,F401  (registers every table)
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402

Base.metadata.create_all(engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def loopback_ok():
    """Address policy that also allows loopback, for the local test server."""
    from app.modules.scraping import safety

    return lambda ip: ip.is_loopback or safety.is_public_address(ip)


@pytest.fixture(scope="session")
def _chromium_problem():
    """None when Playwright's Chromium launches here, else why not."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:  # not installed, browser not downloaded, ...
        return str(exc).splitlines()[0]
    return None


@pytest.fixture
def browser_ok(_chromium_problem, monkeypatch):
    """Browser tests run only where Chromium can launch; they turn browser mode on."""
    if _chromium_problem:
        pytest.skip(f"Chromium unavailable: {_chromium_problem}")
    from app.core.config import settings

    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
