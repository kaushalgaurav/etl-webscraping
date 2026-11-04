"""Request and response shapes for /scrape (spec §7)."""
from typing import Literal

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    url: str = Field(description="Any public web page, or a direct link to a data file.",
                     examples=["https://rbi.org.in/Scripts/AnnualPublications.aspx"])
    include_files: bool = Field(default=True,
                                description="Also download and read files the page links to.")
    render: Literal["auto", "always", "never"] = Field(
        default="auto",
        description="Open the page in a headless browser: 'auto' only when the plain read "
                    "finds a JavaScript shell or no tables, 'always', or 'never'.")


class ImportItem(BaseModel):
    finding_id: str
    name: str | None = Field(default=None, max_length=200,
                             description="Dataset name; defaults to '<site> - <title>'.")


class ImportRequest(BaseModel):
    survey_id: int = Field(description="Survey both saved datasets are filed under.", examples=[7])
    items: list[ImportItem] = Field(min_length=1, description="Every ticked table, in one Save.")


class BlockedEntry(BaseModel):
    what: str
    code: str
    message: str
    suggestion: str | None = None


class TableFinding(BaseModel):
    finding_id: str
    kind: str
    title: str
    locator: dict
    parent_id: str | None = None
    rows: int | None = None
    columns: int | None = None
    column_names: list[str] | None = None
    preview: list[list] | None = None
    signals: dict | None = None
    suggested: str | None = None
    default_name: str
    imported_dataset_id: str | None = None


class FileFinding(BaseModel):
    finding_id: str
    name: str | None = None
    url: str | None = None
    content_type: str | None = None
    bytes: int | None = None
    extracted: str | None = None
    flags: list[str] = []
    produced: list[str] = []


class TextFinding(BaseModel):
    finding_id: str
    title: str
    source: str | None = None
    characters: int | None = None
    snippet: str | None = None


class Manifest(BaseModel):
    scan_id: str
    url: str
    final_url: str | None = None
    status: str
    rendered_in_browser: bool = False
    error: str | None = None
    job_id: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    expires_at: str | None = None
    summary: dict | None = None
    tables: list[TableFinding]
    files: list[FileFinding]
    text: list[TextFinding]
    blocked: list[BlockedEntry]


class ScanListItem(BaseModel):
    scan_id: str
    url: str
    status: str
    created_at: str | None = None
    expires_at: str | None = None
    summary: dict | None = None


class RowsPage(BaseModel):
    total: int
    columns: list[str]
    rows: list[dict]
