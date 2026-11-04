"""Open a JavaScript page in a headless Chromium (spec §4.3).

Used only when the plain read (discover_http) found an empty JavaScript
shell or no tables, or the admin asked for it. Returns the rendered HTML,
which the scan reads with the same parse_page as a plain page, plus:
  - feeds: JSON the page fetched by itself while loading (its data API),
  - downloads: files produced by up to 3 Export / Download buttons.

Safety (spec §9): every connection Chromium makes goes through a
FilteringProxy on 127.0.0.1, which applies the same address policy as the
plain path, so the browser can't reach private addresses. Loopback is not
exempt (`<-loopback>`). A fresh browser and context per call, no profile,
service workers blocked, the scraper's own User-Agent, Chromium's sandbox
on unless the server can't run it. One browser at a time (BROWSER_LOCK).

Time (spec §4.8): every Playwright wait takes its timeout from the render
budget and never 0, which Playwright reads as "no limit". Some calls take
no timeout at all (a script stuck in a loop blocks page.evaluate; a feed or
download that never ends blocks response.body / download.path), so a
watchdog thread closes the proxy and kills Chromium once the budget plus a
grace period has passed. One page can therefore never hold a job worker or
the browser lock.
"""
import json
import os
import re
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from app.core.config import settings
from app.modules.scraping.browser_proxy import FilteringProxy
from app.modules.scraping.fetcher import Deadline, Fetched
from app.modules.scraping.files import FILE_EXTS
from app.modules.scraping.safety import AddressPolicy, ScrapeBlocked, message

MB = 1_048_576
BROWSER_LOCK = threading.Lock()

# JSON from these is tracking, not data: analytics domains (and their
# subdomains), and beacon endpoints named by their last path segment.
NOISE_DOMAINS = ("google-analytics.com", "googletagmanager.com", "doubleclick.net",
                 "facebook.com", "facebook.net", "hotjar.com", "hotjar.io", "clarity.ms",
                 "sentry.io", "newrelic.com", "nr-data.net", "segment.io", "segment.com",
                 "mixpanel.com", "amplitude.com", "cloudflareinsights.com")
NOISE_LAST_SEGMENTS = {"collect", "beacon", "track"}
EXPORT_PATTERN = r"\b(export|download|excel|csv)\b"
MAX_EXPORT_CLICKS = 3
QUIET_MS = 10_000
SCROLLS = 3
LAUNCH_MS = 30_000
WATCHDOG_GRACE_SECONDS = 5

# One pass in the page: the indexes of up to `max` visible buttons/links whose
# text says export/download, skipping plain links to data files.
_FIND_EXPORTS = """([pattern, exts, max]) => {
  const re = new RegExp(pattern, "i");
  const found = [];
  const all = document.querySelectorAll("button, a, [role=button]");
  for (let i = 0; i < all.length && found.length < max; i++) {
    const el = all[i];
    if (!el.getClientRects().length) continue;
    const text = (el.innerText || "").trim();
    if (!text || text.length > 60 || !re.test(text)) continue;
    const href = el.getAttribute("href");
    if (href) {
      let path = "";
      try { path = new URL(href, location.href).pathname.toLowerCase(); } catch (e) {}
      if (exts.some(x => path.endsWith(x))) continue;
    }
    found.push(i);
  }
  return found;
}"""


@dataclass
class Feed:
    url: str
    method: str
    payload: object
    size: int = 0  # bytes of the body, counted against the scan's total


@dataclass
class Rendered:
    final_url: str
    html: str
    feeds: list[Feed] = field(default_factory=list)
    downloads: list[Fetched] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    over_budget: bool = False


def _noop(_message: str) -> None:
    pass


def _is_noise(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if any(host == d or host.endswith("." + d) for d in NOISE_DOMAINS):
        return True
    segments = [s for s in parts.path.lower().split("/") if s]
    return bool(segments) and (segments[-1] in NOISE_LAST_SEGMENTS or "recaptcha" in segments)


def _looks_like_feed(response) -> bool:
    if response.request.resource_type not in ("xhr", "fetch") or response.status != 200:
        return False
    ctype = (response.headers.get("content-type") or "").lower()
    is_json = "json" in ctype or urlsplit(response.url).path.lower().endswith(".json")
    return is_json and not _is_noise(response.url)


class _Clock:
    """Milliseconds left of the render budget, for Playwright timeouts."""

    def __init__(self, deadline: Deadline):
        self._end = time.monotonic() + min(settings.scrape_browser_seconds, deadline.remaining())

    def ms(self, cap: float | None = None) -> float:
        """Never 0: Playwright reads a timeout of 0 as "wait forever"."""
        left = max(1.0, (self._end - time.monotonic()) * 1000)
        return min(left, cap) if cap is not None else left

    def seconds(self) -> float:
        return max(0.0, self._end - time.monotonic())


class _Watchdog:
    """Ends a render that a call without a timeout has stuck: closes the
    proxy (unblocks streaming bodies and downloads) and kills Chromium
    (unblocks a page stuck in its own script)."""

    def __init__(self, seconds: float, proxy: FilteringProxy):
        self.fired = False
        self._pid: int | None = None
        self._proxy = proxy
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def start(self, pid: int | None) -> None:
        self._pid = pid
        self._timer.start()

    def cancel(self) -> None:
        self._timer.cancel()

    def _fire(self) -> None:
        self.fired = True
        self._proxy.close()
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGKILL)
            except OSError:
                pass


def _browser_pid(browser) -> int | None:
    try:
        info = browser.new_browser_cdp_session().send("SystemInfo.getProcessInfo")
    except Exception:
        return None
    return next((p["id"] for p in info.get("processInfo", []) if p.get("type") == "browser"), None)


def render(url: str, *, policy: AddressPolicy, deadline: Deadline,
           say: Callable[[str], None] = _noop, click_exports: bool = True) -> Rendered:
    if deadline.expired():
        raise ScrapeBlocked("timeout", message("timeout", n=settings.scrape_scan_seconds), url)
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeBlocked("browser_unavailable", message("browser_unavailable"), url) from exc

    if not BROWSER_LOCK.acquire(timeout=max(0.0, deadline.remaining())):
        raise ScrapeBlocked("browser_failed", message("browser_failed"), url)
    try:
        if deadline.expired():
            raise ScrapeBlocked("timeout", message("timeout", n=settings.scrape_scan_seconds), url)
        with FilteringProxy(policy, settings.scrape_browser_mb * MB) as proxy:
            clock = _Clock(deadline)
            watchdog = _Watchdog(clock.seconds() + WATCHDOG_GRACE_SECONDS, proxy)
            try:
                with sync_playwright() as pw:
                    try:
                        browser = pw.chromium.launch(
                            headless=True,
                            timeout=clock.ms(LAUNCH_MS),
                            chromium_sandbox=settings.scrape_browser_sandbox,
                            proxy={"server": proxy.url},
                            args=["--proxy-bypass-list=<-loopback>",
                                  "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
                    except PlaywrightError as exc:
                        raise ScrapeBlocked("browser_unavailable",
                                            message("browser_unavailable"), url) from exc
                    watchdog.start(_browser_pid(browser))
                    try:
                        rendered = _render_page(browser, url, clock, proxy, say, click_exports)
                    finally:
                        watchdog.cancel()
                        try:
                            browser.close()
                        except PlaywrightError:
                            pass  # already killed by the watchdog
            except PlaywrightError as exc:
                if watchdog.fired:
                    raise ScrapeBlocked("slow_response", message(
                        "slow_response", n=settings.scrape_browser_seconds), url) from exc
                raise ScrapeBlocked("browser_failed", message("browser_failed"), url) from exc
            rendered.refused = list(proxy.refused)
            rendered.over_budget = proxy.over_budget
            return rendered
    finally:
        BROWSER_LOCK.release()


def _render_page(browser, url: str, clock: _Clock, proxy: FilteringProxy,
                 say: Callable[[str], None], click_exports: bool) -> Rendered:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    context = browser.new_context(user_agent=settings.scrape_user_agent,
                                  accept_downloads=True, service_workers="block")
    try:
        page = context.new_page()
        responses = []
        page.on("response", lambda r: responses.append(r)
                if len(responses) < settings.scrape_feed_max and _looks_like_feed(r) else None)
        say("Opening the page in a browser")
        host = urlsplit(url).hostname or ""
        refused = ScrapeBlocked("private_address", message("private_address"), host)
        try:
            page.goto(url, wait_until="load", timeout=clock.ms())
        except PlaywrightTimeout:
            pass  # read whatever has loaded so far
        except PlaywrightError as exc:
            if host in proxy.refused:
                raise refused from exc
            raise
        # A refused plain-HTTP page arrives as the proxy's empty 403 answer,
        # not as an error, so ask the proxy.
        if host in proxy.refused or (urlsplit(page.url).hostname or "") in proxy.refused:
            raise refused
        try:
            page.wait_for_load_state("networkidle", timeout=clock.ms(QUIET_MS))
        except PlaywrightTimeout:
            pass  # a page that keeps polling never goes quiet
        for _ in range(SCROLLS):
            if clock.ms() <= 1000:
                break
            page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
            page.wait_for_timeout(1000)
        html = page.content()
        final_url = page.url
        feeds = _read_feeds(responses)
        downloads = _click_exports(page, clock) if click_exports else []
        return Rendered(final_url=final_url, html=html, feeds=feeds, downloads=downloads)
    finally:
        try:
            context.close()
        except PlaywrightError:
            pass  # the watchdog killed the browser; what was read is kept


def _read_feeds(responses) -> list[Feed]:
    feeds = []
    cap = settings.scrape_feed_mb * MB
    total = 0  # all feed bodies held at once stay within the scan's total
    for response in responses:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            continue
        try:
            body = response.body()
        except Exception:
            continue  # evicted or failed; nothing to read
        if len(body) > cap or total + len(body) > settings.scrape_total_mb * MB:
            continue
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            continue
        total += len(body)
        feeds.append(Feed(url=response.url, method=response.request.method, payload=payload,
                          size=len(body)))
    return feeds


def _export_candidates(page) -> list:
    try:
        indexes = page.evaluate(_FIND_EXPORTS, [EXPORT_PATTERN, sorted(FILE_EXTS), MAX_EXPORT_CLICKS])
    except Exception:
        return []
    everything = page.locator("button, a, [role=button]")
    return [everything.nth(i) for i in indexes]


def _click_exports(page, clock: _Clock) -> list[Fetched]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    downloads = []
    cap = settings.scrape_file_mb * MB
    start_url = page.url
    for element in _export_candidates(page):
        if clock.ms() <= 1000:
            break
        try:
            with page.expect_download(timeout=clock.ms(5000)) as info:
                element.click(timeout=clock.ms(3000))
            download = info.value
            path = download.path()
            if path and Path(path).stat().st_size <= cap:
                downloads.append(Fetched(url=download.url, final_url=download.url, status=200,
                                         content_type=None, content=Path(path).read_bytes(),
                                         filename=download.suggested_filename or "download"))
        except PlaywrightTimeout:
            pass
        except Exception:
            pass
        if page.url != start_url:
            break  # the click took us to another page; stop clicking here
    return downloads
