import ipaddress
import socket

import pytest

from app.core.config import settings
from app.modules.scraping import safety


@pytest.mark.parametrize("ip", [
    "10.1.2.3", "172.16.0.1", "192.168.1.1", "127.0.0.1", "169.254.169.254",
    "0.0.0.0", "100.64.0.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1",
    "::ffff:169.254.169.254", "224.0.0.1",
])
def test_private_addresses_refused(ip):
    assert safety.is_public_address(ipaddress.ip_address(ip)) is False


def test_public_address_allowed():
    assert safety.is_public_address(ipaddress.ip_address("8.8.8.8")) is True
    assert safety.is_public_address(ipaddress.ip_address("2001:4860:4860::8888")) is True


@pytest.mark.parametrize("url", ["ftp://x.gov.in/a", "file:///etc/passwd", "http:///nohost",
                                 "javascript:alert(1)", "not a url"])
def test_check_url_rejects(url):
    with pytest.raises(safety.ScrapeBlocked) as e:
        safety.check_url(url)
    assert e.value.code == "unsupported"


def test_check_url_accepts_http_and_https():
    safety.check_url("http://rbi.org.in/")
    safety.check_url("https://rbi.org.in/a?b=1")


def test_session_refuses_loopback_server(httpserver):
    httpserver.expect_request("/").respond_with_data("hi")
    with pytest.raises(safety.ScrapeBlocked) as e:
        safety.make_session().get(httpserver.url_for("/"), timeout=5)
    assert e.value.code == "private_address"
    assert len(httpserver.log) == 0  # no connection was made


def test_hostname_resolving_to_private_is_refused(monkeypatch, httpserver):
    httpserver.expect_request("/").respond_with_data("hi")
    real = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        return real("127.0.0.1" if host == "innocent.example" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(safety.ScrapeBlocked) as e:
        safety.make_session().get(f"http://innocent.example:{httpserver.port}/", timeout=5)
    assert e.value.code == "private_address"
    assert len(httpserver.log) == 0


def test_permissive_policy_allows_local(httpserver, loopback_ok):
    httpserver.expect_request("/").respond_with_data("hi")
    resp = safety.make_session(loopback_ok).get(httpserver.url_for("/"), timeout=5)
    assert resp.text == "hi"


def test_session_sends_honest_user_agent(httpserver, loopback_ok):
    httpserver.expect_request("/", headers={"User-Agent": settings.scrape_user_agent}).respond_with_data("ok")
    assert safety.make_session(loopback_ok).get(httpserver.url_for("/"), timeout=5).text == "ok"


def test_session_ignores_proxy_env(monkeypatch, httpserver, loopback_ok):
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
    httpserver.expect_request("/").respond_with_data("direct")
    assert safety.make_session(loopback_ok).get(httpserver.url_for("/"), timeout=5).text == "direct"
