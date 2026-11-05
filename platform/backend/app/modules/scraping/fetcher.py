"""One polite, guarded HTTP GET for the Website tab.

Fetcher.get() is the only way scraping code reaches the network. On top of
the address guard (safety.make_session) it adds, per spec §4.1, §4.8, §9:
  - manual redirects (at most 5), each hop re-checked: URL, robots.txt, and
    the address guard again at connect time;
  - robots.txt, only when PLATFORM_SCRAPE_OBEY_ROBOTS is on (off by default);
  - one retry after a 429/503, waiting as the site's Retry-After asks (capped);
  - one request per second per host, shared by every scan in the process;
  - a whole-scan deadline that also caps each request's timeout;
  - a byte cap, checked against Content-Length and again while streaming.
Non-2xx answers are returned, not raised, so blocked.py can classify them.
"""
import re
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from app.core.config import settings
from app.modules.scraping.safety import (
    AddressPolicy,
    ScrapeBlocked,
    check_url,
    is_public_address,
    make_session,
    message,
)

ROBOTS_AGENT = "NimbliDataBot"
MAX_REDIRECTS = 5
ROBOTS_MAX_BYTES = 512_000


class Deadline:
    def __init__(self, seconds: float, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0


class HostThrottle:
    """At most one request per `interval` seconds to any one host."""

    def __init__(self, interval: float, clock=time.monotonic, sleep=time.sleep):
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + self._interval
        if start > now:
            self._sleep(start - now)


THROTTLE = HostThrottle(settings.scrape_host_interval_seconds)


@dataclass
class Fetched:
    url: str
    final_url: str
    status: int
    content_type: str | None
    content: bytes
    filename: str


_DISPOSITION = re.compile(
    r"""filename\*\s*=\s*(?:UTF-8|utf-8)''(?P<star>[^;]+)|filename\s*=\s*"?(?P<plain>[^";]+)"?""")


def _filename(url: str, disposition: str | None) -> str:
    if disposition:
        match = _DISPOSITION.search(disposition)
        if match:
            name = unquote(match.group("star") or match.group("plain") or "").strip()
            if name:
                return PurePosixPath(name.replace("\\", "/")).name or "download"
    name = PurePosixPath(unquote(urlsplit(url).path)).name
    return name or "download"


RETRY_STATUSES = (429, 503)
RETRY_WAIT_DEFAULT = 5.0
RETRY_WAIT_MAX = 30.0


def _retry_after(header: str | None) -> float:
    """Seconds to wait from a Retry-After header (seconds form), capped."""
    try:
        wait = float(header) if header is not None else RETRY_WAIT_DEFAULT
    except ValueError:
        wait = RETRY_WAIT_DEFAULT  # an HTTP date: not worth parsing for one retry
    return min(max(wait, 0.0), RETRY_WAIT_MAX)


def _seconds(n: float) -> int:
    return int(round(n))


class Fetcher:
    def __init__(self, policy: AddressPolicy = is_public_address,
                 deadline: Deadline | None = None, throttle: HostThrottle = THROTTLE):
        self.policy = policy
        self.deadline = deadline or Deadline(settings.scrape_scan_seconds)
        self._session = make_session(policy)
        self._throttle = throttle
        self._robots: dict[str, RobotFileParser | None] = {}

    # robots.txt -------------------------------------------------------------
    def allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = None
            try:
                got = self.get(origin + "/robots.txt", max_bytes=ROBOTS_MAX_BYTES, check_robots=False)
                if got.status == 200:
                    parser = RobotFileParser()
                    parser.parse(got.content.decode("utf-8", "replace").splitlines())
            except ScrapeBlocked:
                # Unreadable robots.txt counts as "allowed" (plan's call): the
                # admin asked for this one page, and the pace is 1 request/s.
                parser = None
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(ROBOTS_AGENT, url)

    # GET --------------------------------------------------------------------
    def get(self, url: str, *, max_bytes: int, check_robots: bool = True,
            referer: str | None = None) -> Fetched:
        """`referer`: the page a file link was found on. Some sites only serve a
        download to a request that says where it came from."""
        current = url
        retried = False
        for _hop in range(MAX_REDIRECTS + 1):
            check_url(current)
            if check_robots and settings.scrape_obey_robots and not self.allowed_by_robots(current):
                raise ScrapeBlocked("robots_disallowed", message("robots_disallowed"), current)
            resp = self._send(current, referer)
            if resp.status_code in RETRY_STATUSES and not retried:
                # "Too many requests" / "busy": wait as asked (capped), try once more.
                retried = True
                wait = _retry_after(resp.headers.get("Retry-After"))
                resp.close()
                if wait < self.deadline.remaining():
                    time.sleep(wait)
                    resp = self._send(current, referer)
            if resp.is_redirect and resp.headers.get("Location"):
                resp.close()
                current = urljoin(current, resp.headers["Location"])
                continue
            content = self._read(resp, current, max_bytes)
            return Fetched(
                url=url,
                final_url=current,
                status=resp.status_code,
                content_type=resp.headers.get("Content-Type"),
                content=content,
                filename=_filename(current, resp.headers.get("Content-Disposition")),
            )
        raise ScrapeBlocked("fetch_failed", message("fetch_failed"), url)

    def _timed_out(self, url: str) -> ScrapeBlocked:
        """A slow answer is that request's problem; only an expired deadline
        means the whole Search ran out of time."""
        if self.deadline.expired():
            return ScrapeBlocked("timeout", message("timeout", n=settings.scrape_scan_seconds), url)
        return ScrapeBlocked(
            "slow_response", message("slow_response", n=settings.request_timeout_seconds), url)

    def _send(self, url: str, referer: str | None = None) -> requests.Response:
        if self.deadline.expired():
            raise ScrapeBlocked(
                "timeout", message("timeout", n=settings.scrape_scan_seconds), url)
        self._throttle.wait(urlsplit(url).netloc)
        timeout = min(settings.request_timeout_seconds, self.deadline.remaining())
        if timeout <= 0:
            raise ScrapeBlocked(
                "timeout", message("timeout", n=settings.scrape_scan_seconds), url)
        try:
            headers = {"Referer": referer} if referer else None
            return self._session.get(url, allow_redirects=False, stream=True, timeout=timeout,
                                     headers=headers)
        except requests.Timeout as exc:
            raise self._timed_out(url) from exc
        except requests.exceptions.SSLError as exc:
            raise ScrapeBlocked("certificate_error", message("certificate_error"), url) from exc
        except requests.RequestException as exc:
            raise ScrapeBlocked("fetch_failed", message("fetch_failed"), url) from exc

    def _read(self, resp: requests.Response, url: str, max_bytes: int) -> bytes:
        limit_mb = max(1, _seconds(max_bytes / 1_048_576))
        too_large = ScrapeBlocked("too_large", message("too_large", n=limit_mb), url)
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            resp.close()
            raise too_large
        chunks, total = [], 0
        try:
            for chunk in resp.iter_content(65536):
                total += len(chunk)
                if total > max_bytes:
                    raise too_large
                chunks.append(chunk)
        except requests.Timeout as exc:
            raise self._timed_out(url) from exc
        except requests.RequestException as exc:
            raise ScrapeBlocked("fetch_failed", message("fetch_failed"), url) from exc
        finally:
            resp.close()
        return b"".join(chunks)
