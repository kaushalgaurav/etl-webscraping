"""Dynamic (JavaScript-rendered) website connector — STUBBED.

For sites where the data only appears after JS execution (SPA dashboards,
infinite-scroll tables, XHR-populated grids), a real implementation drives
a headless browser (Playwright), waits for network idle, and either:
  (a) sniffs the underlying XHR/fetch calls and hands the JSON response to
      the rest_api connector, or
  (b) waits for the DOM to render and extracts the resulting table(s), the
      same way html_table does for static pages.

Not wired up yet: Playwright needs `pip install playwright` plus a
~300MB `playwright install chromium` browser download, which we're
deferring until a real dynamic-site target justifies the install. The
interface below is the real one — the ingestion engine's connector
registry can route to this today; it just raises until implemented.
"""
from app.modules.ingestion.connectors.base import ConnectorError, IngestResult


def ingest(url: str, wait_for_selector: str | None = None, sniff_network: bool = True) -> IngestResult:
    """Render `url` in a headless browser and extract its data.

    Args:
        url: page to render.
        wait_for_selector: CSS selector to wait for before extracting
            (use when the target table/grid appears after a known element).
        sniff_network: if True, prefer capturing the page's own XHR/fetch
            JSON responses over scraping the rendered DOM.
    """
    raise ConnectorError(
        "Dynamic-site ingestion (Playwright) is not installed yet. "
        "Run `pip install playwright` and `playwright install chromium` "
        "in the backend venv, then implement app.connectors.dynamic_site.ingest."
    )
