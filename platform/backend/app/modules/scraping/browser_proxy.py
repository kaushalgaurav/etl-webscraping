"""A small filtering forward proxy that the headless browser must use (spec §9).

Checking URLs inside Playwright is not enough: Chromium does its own DNS
lookup after any such check, so a name could resolve to a public address
when checked and a private one when connected. Instead, every connection
the browser makes goes through this proxy, on 127.0.0.1, for one scan only.
For each one (HTTPS `CONNECT` tunnels and plain-HTTP requests alike) it
resolves the host once, applies the same address policy as the plain path
(safety.resolve_checked), and connects to the checked address, or refuses
with 403. HTTPS is relayed as opaque bytes; nothing is decrypted.

It also paces the browser's first connection to each host (the shared 1
request/second throttle): opening the page counts as one visit, as for a
person's browser, and what the page then loads by itself is not paced one
by one (spec §4.1). Plain-HTTP requests each need their own connection
here, so per-connection pacing would add a second per asset. It caps the
bytes relayed to the browser for the whole scan.
"""
import http.server
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

from app.core.config import settings
from app.modules.scraping.fetcher import THROTTLE, HostThrottle
from app.modules.scraping.safety import AddressPolicy, ScrapeBlocked, resolve_checked

_HOP_HEADERS = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}
_CHUNK = 65536


class FilteringProxy:
    def __init__(self, policy: AddressPolicy, byte_budget: int, throttle: HostThrottle = THROTTLE):
        self.policy = policy
        self.byte_budget = byte_budget
        self.throttle = throttle
        self.refused: list[str] = []
        self.over_budget = False
        self._relayed = 0
        self._visited: set[str] = set()
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self._closed = threading.Event()
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _handler_for(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "FilteringProxy":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._server.shutdown()
        self._server.server_close()
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

    # used by the handler ----------------------------------------------------
    def refuse(self, host: str) -> None:
        with self._lock:
            if host not in self.refused:
                self.refused.append(host)

    def count(self, n: int) -> bool:
        """Count bytes going to the browser; False once over the budget."""
        with self._lock:
            self._relayed += n
            if self._relayed > self.byte_budget:
                self.over_budget = True
            return not self.over_budget

    def first_visit(self, host: str) -> bool:
        with self._lock:
            if host in self._visited:
                return False
            self._visited.add(host)
            return True

    def track(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.add(sock)

    def untrack(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(sock)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()


def _handler_for(proxy: FilteringProxy):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:  # no request log on stderr
            pass

        def _reply(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def _open(self, host: str, port: int) -> socket.socket | None:
            """A connection to a checked address of host, or None (already answered)."""
            try:
                ip = resolve_checked(host, port, proxy.policy)
            except ScrapeBlocked:
                proxy.refuse(host)
                self._reply(403)
                return None
            except OSError:
                self._reply(502)
                return None
            if proxy.first_visit(host):
                proxy.throttle.wait(host)
            try:
                upstream = socket.create_connection((ip, port), timeout=settings.request_timeout_seconds)
            except OSError:
                self._reply(502)
                return None
            upstream.settimeout(None)
            proxy.track(upstream)
            return upstream

        def _relay(self, upstream: socket.socket, both_ways: bool) -> None:
            client = self.connection
            proxy.track(client)
            socks = [client, upstream] if both_ways else [upstream]
            try:
                while not proxy.closed:
                    ready, _, _ = select.select(socks, [], [], 1.0)
                    for sock in ready:
                        data = sock.recv(_CHUNK)
                        if not data:
                            return
                        if sock is upstream:
                            if not proxy.count(len(data)):
                                return
                            client.sendall(data)
                        else:
                            upstream.sendall(data)
            except OSError:
                return
            finally:
                proxy.untrack(client)
                proxy.untrack(upstream)
                upstream.close()
                self.close_connection = True

        def do_CONNECT(self) -> None:
            host, _, port = self.path.rpartition(":")
            host = host.strip("[]")
            if not host or not port.isdigit():
                self._reply(400)
                return
            upstream = self._open(host, int(port))
            if upstream is None:
                return
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            self._relay(upstream, both_ways=True)

        def _forward(self) -> None:
            parts = urlsplit(self.path)
            if parts.scheme != "http" or not parts.hostname:
                self._reply(400)
                return
            if self.headers.get("Transfer-Encoding"):
                self._reply(501)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            upstream = self._open(parts.hostname, parts.port or 80)
            if upstream is None:
                return
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            lines = [f"{self.command} {target} HTTP/1.1"]
            lines += [f"{k}: {v}" for k, v in self.headers.items() if k.lower() not in _HOP_HEADERS]
            lines.append("Connection: close")
            try:
                upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
            except OSError:
                upstream.close()
                self._reply(502)
                return
            self._relay(upstream, both_ways=False)

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = _forward

    return Handler
