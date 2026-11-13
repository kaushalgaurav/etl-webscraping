import socket
from urllib.parse import urlsplit

import requests

from app.modules.scraping import safety
from app.modules.scraping.browser_proxy import FilteringProxy


def _addr(httpserver) -> tuple[str, int]:
    parts = urlsplit(httpserver.url_for("/"))
    return parts.hostname, parts.port


def _connect(proxy: FilteringProxy, host: str, port: int) -> tuple[socket.socket, bytes]:
    proxy_port = urlsplit(proxy.url).port
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1)
        if not chunk:
            break
        head += chunk
    return sock, head


def _read_all(sock: socket.socket) -> bytes:
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return data
        data += chunk


def test_http_request_relayed(httpserver, loopback_ok):
    httpserver.expect_request("/x").respond_with_data("hello")
    with FilteringProxy(loopback_ok, 10**6) as proxy:
        resp = requests.get(httpserver.url_for("/x"), proxies={"http": proxy.url}, timeout=5)
    assert resp.status_code == 200 and resp.text == "hello"


def test_connect_relays_bytes(httpserver, loopback_ok):
    httpserver.expect_request("/x").respond_with_data("tunnelled")
    host, port = _addr(httpserver)
    with FilteringProxy(loopback_ok, 10**6) as proxy:
        sock, head = _connect(proxy, host, port)
        assert head.startswith(b"HTTP/1.1 200")
        sock.sendall(f"GET /x HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode())
        body = _read_all(sock)
        sock.close()
    assert body.split(b"\r\n")[0].endswith(b" 200 OK") and body.endswith(b"tunnelled")


def test_connect_to_private_address_refused(httpserver):
    httpserver.expect_request("/x").respond_with_data("secret")
    host, port = _addr(httpserver)
    with FilteringProxy(safety.is_public_address, 10**6) as proxy:
        sock, head = _connect(proxy, host, port)
        sock.close()
        assert head.startswith(b"HTTP/1.1 403")
        assert proxy.refused == [host]
    assert len(httpserver.log) == 0


def test_http_to_private_address_refused(httpserver):
    httpserver.expect_request("/x").respond_with_data("secret")
    host, _port = _addr(httpserver)
    with FilteringProxy(safety.is_public_address, 10**6) as proxy:
        resp = requests.get(httpserver.url_for("/x"), proxies={"http": proxy.url}, timeout=5)
        assert resp.status_code == 403
        assert proxy.refused == [host]
    assert len(httpserver.log) == 0


def test_byte_budget(httpserver, loopback_ok):
    httpserver.expect_request("/big").respond_with_data("x" * 10_000)
    host, port = _addr(httpserver)
    with FilteringProxy(loopback_ok, 100) as proxy:
        sock, head = _connect(proxy, host, port)
        sock.sendall(f"GET /big HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode())
        body = _read_all(sock)
        sock.close()
        assert len(body) < 10_000
        assert proxy.over_budget


def test_plain_http_assets_are_not_paced_one_by_one(httpserver, loopback_ok):
    from app.modules.scraping.fetcher import HostThrottle
    import time

    httpserver.expect_request("/a").respond_with_data("ok")
    with FilteringProxy(loopback_ok, 10**6, throttle=HostThrottle(1.0)) as proxy:
        started = time.monotonic()
        for _ in range(5):
            assert requests.get(httpserver.url_for("/a"), proxies={"http": proxy.url}, timeout=5).ok
        assert time.monotonic() - started < 2
