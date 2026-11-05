"""Address guard for the Website tab.

An admin-typed URL makes this server fetch things, so every outgoing
connection must refuse private, loopback, link-local and metadata addresses
(spec §4.1, §9). Checking a URL's hostname is not enough: a name can resolve
to a public address when checked and a private one when connected. So the
check lives inside the connection itself: each connection resolves its host
once, checks every address it got, and connects to a checked address. TLS
still verifies the original hostname (urllib3 takes SNI from `self.host`,
which is untouched).

Nothing in app/modules/scraping may call requests.get directly; it goes
through make_session() (via fetcher.Fetcher).
"""
import ipaddress
import socket
import ssl
from collections.abc import Callable
from urllib.parse import urlsplit

import requests
import truststore
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NameResolutionError

from app.core.config import settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
AddressPolicy = Callable[[IPAddress], bool]

# "Couldn't read" wording shown to admins, one per code. Templates take {n}
# (a limit) or {status} (an HTTP status). Kept here, the module every other
# scraping module already imports, so the text lives in one place.
MESSAGES = {
    "robots_disallowed": "The site asks automated tools not to read this page.",
    "bot_protection": "The site blocked automated access.",
    "captcha": "Data is behind a CAPTCHA.",
    "login_required": "The page needs a login.",
    "form_gated": "Data appears only after choosing options in a form.",
    "needs_browser": "This page builds its content with JavaScript; it can only be read "
                     "with browser rendering.",
    "browser_unavailable": "Browser mode is not available on this server, so only a plain "
                           "read was done.",
    "browser_failed": "The page could not be opened in a browser, so only a plain read "
                      "was done.",
    "private_address": "This address points inside a private network and can't be read.",
    "too_large": "File is larger than the {n} MB limit.",
    "timeout": "Search stopped after {n} seconds.",
    "slow_response": "The site took longer than {n} seconds to answer.",
    "too_many_files": "Only the first {n} linked files were read.",
    "unsupported": "This kind of address or file can't be read.",
    "http_error": "The site answered with status {status}.",
    "fetch_failed": "Couldn't connect to the site.",
    "certificate_error": "The site's security certificate couldn't be verified.",
}


def message(code: str, **fmt) -> str:
    return MESSAGES[code].format(**fmt) if fmt else MESSAGES[code]


PRIVATE_MESSAGE = MESSAGES["private_address"]
UNSUPPORTED_MESSAGE = MESSAGES["unsupported"]

class ScrapeBlocked(Exception):
    """Something could not be read, with a code the review screen shows.

    Deliberately not an OSError: urllib3 and requests convert OSErrors into
    their own connection errors, which would hide the code.
    """

    def __init__(self, code: str, message: str, what: str):
        super().__init__(f"{code}: {message} ({what})")
        self.code = code
        self.message = message
        self.what = what


def is_public_address(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> None:
    """Refuse anything that is not an http(s) URL with a host."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ScrapeBlocked("unsupported", UNSUPPORTED_MESSAGE, url) from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ScrapeBlocked("unsupported", UNSUPPORTED_MESSAGE, url)


def resolve_checked(host: str, port: int, policy: AddressPolicy) -> str:
    """Resolve once, refuse if any address fails the policy, return one to use."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = {info[4][0] for info in infos}
    bad = [a for a in addrs if not policy(ipaddress.ip_address(a.split("%")[0]))]
    if not addrs or bad:
        raise ScrapeBlocked("private_address", PRIVATE_MESSAGE, host)
    return sorted(addrs)[0]


def _pool_classes(policy: AddressPolicy) -> dict:
    class _Pinned:
        def _new_conn(self):
            host = self._dns_host
            try:
                ip = resolve_checked(host, self.port, policy)
            except socket.gaierror as exc:
                raise NameResolutionError(self.host, self, exc) from exc
            # Connect to the checked address; self.host (Host header, SNI,
            # certificate check) stays the original name.
            self._dns_host = ip
            try:
                return super()._new_conn()
            finally:
                self._dns_host = host

    class PinnedHTTPConnection(_Pinned, urllib3.connection.HTTPConnection):
        pass

    class PinnedHTTPSConnection(_Pinned, urllib3.connection.HTTPSConnection):
        pass

    class PinnedHTTPPool(HTTPConnectionPool):
        ConnectionCls = PinnedHTTPConnection

    class PinnedHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = PinnedHTTPSConnection

    return {"http": PinnedHTTPPool, "https": PinnedHTTPSPool}


class _GuardedAdapter(HTTPAdapter):
    def __init__(self, policy: AddressPolicy):
        self._policy = policy
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        # Verify certificates with the operating system's checker, as browsers
        # and curl do. Python's own checker fails some valid government sites
        # that also send an old chain through a retired root (www.nfhsiips.in:
        # "self-signed certificate in certificate chain"). Checking stays full:
        # hostname and chain are still verified.
        kwargs["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = _pool_classes(self._policy)


def make_session(policy: AddressPolicy = is_public_address) -> requests.Session:
    """A requests session whose every connection passes the address policy."""
    session = requests.Session()
    # A proxy from the environment would become the host we check and
    # connect to, bypassing the guard for the real target.
    session.trust_env = False
    # Read like a normal browser: some firewalls refuse requests that lack these.
    session.headers.update({
        "User-Agent": settings.scrape_user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
    })
    adapter = _GuardedAdapter(policy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
