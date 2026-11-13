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
                             description="Table name; defaults to '<site> - <title>'. Also names its "
                                         "columns after Merge, so the same table across years keeps one name.")
    place_column: str | None = Field(default=None, description=(
        "Column holding the districts. Defaults to the review screen's guess "
        "(signals.joinable.place_column)."))
    state_name: str | None = Field(default=None, description="The state, when the table has no state column.")
    state_column: str | None = Field(default=None, description=(
        "Column holding each row's state. Defaults to the guessed signals.joinable.state_column."))
    year: str | None = Field(default=None, description=(
        "Financial year like 2019-2020 (see GET /warehouse/web/years). Give this or year_column."),
        examples=["2019-2020"])
    year_column: str | None = Field(default=None, description="Column holding each row's year.")
    indicator_column: str | None = Field(default=None, description=(
        "Long tables only (one row per district per indicator, e.g. NFHS district files): the "
        "column naming each row's indicator. Each value is then filed under that name instead "
        "of its column heading, so every indicator survives Clean and Master."))
    columns: list[str] | None = Field(default=None, description=(
        "Value columns to keep (e.g. one year's columns when years are in the headings). "
        "Omit to keep all."))


class ImportRequest(BaseModel):
    survey_id: int = Field(description="Survey the saved tables are filed under.", examples=[7])
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
