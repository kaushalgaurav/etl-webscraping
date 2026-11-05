"""Central configuration.

All paths resolve relative to this file so the app can be launched from any
working directory (uvicorn, pytest, scripts).
"""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

BACKEND_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BACKEND_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
EXPORTS_DIR = DATA_DIR / "exports"
WAREHOUSE_DIR = DATA_DIR / "warehouse"
DB_PATH = WAREHOUSE_DIR / "platform.db"

for _dir in (RAW_DIR, EXPORTS_DIR, WAREHOUSE_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    """Runtime settings, overridable via environment variables."""

    # These two intentionally use the plain (unprefixed) env var names since
    # ANTHROPIC_API_KEY is the conventional name used across the ecosystem.
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    claude_model: str = Field(default="claude-opus-5-5", validation_alias="CLAUDE_MODEL")
    # Google Gemini, for POST /ai-data/fetch only. Plain names like the above.
    gemini_api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.8-flash", validation_alias="GEMINI_MODEL")
    # Free-tier pacing: calls allowed per minute from this process, and how
    # often a "too many requests" / "busy" answer is retried before giving up.
    gemini_requests_per_minute: int = Field(default=10, validation_alias="GEMINI_RPM")
    gemini_retries: int = Field(default=3, validation_alias="GEMINI_RETRIES")
    # Set PLATFORM_DATABASE_URL (env var or .env) to use PostgreSQL, e.g.
    # postgresql+psycopg2://user:password@host:5432/dbname
    database_url: str = f"sqlite:///{DB_PATH.as_posix()}"
    max_upload_mb: int = 50
    request_timeout_seconds: int = 30
    max_profile_sample_rows: int = 20
    job_workers: int = 2
    # Delete a downloaded .xls/.csv once its rows are committed; the database
    # is the record from then on, and an HMIS crawl otherwise leaves gigabytes
    # on disk. The .meta.json sidecar is kept either way, so a purged file
    # still says which version of the report produced its rows.
    # Set PLATFORM_DELETE_SOURCE_FILES_AFTER_INGEST=false to keep the files —
    # rebuild_hmis_raw.py needs them to rebuild hmis_raw without re-downloading.
    delete_source_files_after_ingest: bool = True

    # Website tab (app/modules/scraping). Limits from the design spec §4.8.
    # Staging lives outside RAW_DIR so cleanup.purge_all() never touches a
    # scan the admin is still reviewing.
    scrape_dir: Path = DATA_DIR / "scrape_staging"
    # A normal desktop Chrome: many government firewalls refuse unknown agents.
    scrape_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    # robots.txt is ignored unless this is on (PLATFORM_SCRAPE_OBEY_ROBOTS=true):
    # an admin asks for one public page, and pacing (below) keeps the load light.
    scrape_obey_robots: bool = False
    scrape_scan_seconds: int = 600
    scrape_host_interval_seconds: float = 1.0
    scrape_max_files: int = 100
    scrape_file_mb: int = 100
    scrape_total_mb: int = 500
    scrape_zip_max_files: int = 50
    scrape_zip_max_mb: int = 300
    scrape_max_rows: int = 1_000_000
    scrape_preview_rows: int = 20
    scrape_preview_cols: int = 30
    scrape_snippet_chars: int = 1000
    # Unsaved scans (rows + staged files) are deleted this long after Search.
    scrape_hold_hours: int = 24
    # Phase 2: JavaScript pages in a headless Chromium (spec §4.3).
    scrape_browser_enabled: bool = True
    # False only where Chromium's sandbox can't start (e.g. running as root in Docker).
    scrape_browser_sandbox: bool = True
    scrape_browser_seconds: int = 120
    scrape_browser_mb: int = 100
    scrape_feed_max: int = 30
    scrape_feed_mb: int = 10
    scrape_feed_pages: int = 50

    model_config = {
        "env_prefix": "PLATFORM_",
        "env_file": BACKEND_DIR / ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
