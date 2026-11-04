import pytest
from werkzeug.wrappers import Response

from app.modules.scraping import safety
from app.modules.scraping.fetcher import Deadline, Fetcher, HostThrottle


def no_robots(httpserver):
    httpserver.expect_request("/robots.txt").respond_with_data("", status=404)


def robots(httpserver, text):
    httpserver.expect_request("/robots.txt").respond_with_data(text, content_type="text/plain")


def test_follows_redirect_and_reports_final_url(httpserver, loopback_ok):
    no_robots(httpserver)
    httpserver.expect_request("/a").respond_with_response(Response(status=302, headers={"Location": "/b"}))
    httpserver.expect_request("/b").respond_with_data("ok")
    r = Fetcher(policy=loopback_ok).get(httpserver.url_for("/a"), max_bytes=1000)
    assert r.final_url.endswith("/b") and r.url.endswith("/a")
    assert r.content == b"ok" and r.status == 200


def test_redirect_to_private_address_is_blocked(httpserver):
    no_robots(httpserver)
    target = f"http://127.0.0.2:{httpserver.port}/b"
    httpserver.expect_request("/a").respond_with_response(Response(status=302, headers={"Location": target}))
    policy = lambda ip: str(ip) in ("127.0.0.1", "::1") or safety.is_public_address(ip)  # noqa: E731
    with pytest.raises(safety.ScrapeBlocked) as e:
        Fetcher(policy=policy).get(httpserver.url_for("/a"), max_bytes=1000)
    assert e.value.code == "private_address"


def test_too_many_redirects(httpserver, loopback_ok):
    no_robots(httpserver)
    httpserver.expect_request("/loop").respond_with_response(Response(status=302, headers={"Location": "/loop"}))
    with pytest.raises(safety.ScrapeBlocked) as e:
        Fetcher(policy=loopback_ok).get(httpserver.url_for("/loop"), max_bytes=1000)
    assert e.value.code == "fetch_failed"


def test_robots_disallow_blocks(httpserver, loopback_ok):
    robots(httpserver, "User-agent: *\nDisallow: /private\n")
    httpserver.expect_request("/public").respond_with_data("yes")
    f = Fetcher(policy=loopback_ok)
    with pytest.raises(safety.ScrapeBlocked) as e:
        f.get(httpserver.url_for("/private/x"), max_bytes=1000)
    assert e.value.code == "robots_disallowed"
    assert f.get(httpserver.url_for("/public"), max_bytes=1000).content == b"yes"


def test_robots_specific_agent_rule(httpserver, loopback_ok):
    robots(httpserver, "User-agent: NimbliDataBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    with pytest.raises(safety.ScrapeBlocked) as e:
        Fetcher(policy=loopback_ok).get(httpserver.url_for("/page"), max_bytes=1000)
    assert e.value.code == "robots_disallowed"


def test_robots_checked_on_redirect_target(httpserver, loopback_ok):
    robots(httpserver, "User-agent: *\nDisallow: /secret\n")
    httpserver.expect_request("/a").respond_with_response(Response(status=302, headers={"Location": "/secret"}))
    with pytest.raises(safety.ScrapeBlocked) as e:
        Fetcher(policy=loopback_ok).get(httpserver.url_for("/a"), max_bytes=1000)
    assert e.value.code == "robots_disallowed"


def test_missing_robots_allows(httpserver, loopback_ok):
    no_robots(httpserver)
    httpserver.expect_request("/x").respond_with_data("fine")
    assert Fetcher(policy=loopback_ok).get(httpserver.url_for("/x"), max_bytes=1000).content == b"fine"


def test_robots_fetched_once_per_host(httpserver, loopback_ok):
    no_robots(httpserver)
    httpserver.expect_request("/x").respond_with_data("fine")
    f = Fetcher(policy=loopback_ok)
    for _ in range(3):
        f.get(httpserver.url_for("/x"), max_bytes=1000)
    assert sum(1 for req, _ in httpserver.log if req.path == "/robots.txt") == 1


def test_user_agent_sent(httpserver, loopback_ok):
    no_robots(httpserver)
    httpserver.expect_request("/", headers={"User-Agent": "NimbliDataBot/1.0"}).respond_with_data("ok")
    assert Fetcher(policy=loopback_ok).get(httpserver.url_for("/"), max_bytes=1000).content == b"ok"


def test_content_length_over_cap_is_too_large(httpserver, loopback_ok):
    no_robots(httpserver)
    httpserver.expect_request("/big").respond_with_data(b"x" * 2000)
    with pytest.raises(safety.ScrapeBlocked) as e:
        Fetcher(policy=loopback_ok).get(httpserver.url_for("/big"), max_bytes=1000)
    assert e.value.code == "too_large"


def test_streamed_body_over_cap_is_too_large(httpserver, loopback_ok):
    no_robots(httpserver)

    def handler(request):
        return Response((b"x" * 500 for _ in range(10)), direct_passthrough=True)

    httpserver.expect_request("/stream").respond_with_handler(handler)
    with pytest.raises(safety.ScrapeBlocked) as e:
        Fetcher(policy=loopback_ok).get(httpserver.url_for("/stream"), max_bytes=1000)
    assert e.value.code == "too_large"


def test_expired_deadline_is_timeout(httpserver, loopback_ok):
    no_robots(httpserver)
    httpserver.expect_request("/x").respond_with_data("fine")
    with pytest.raises(safety.ScrapeBlocked) as e:
        Fetcher(policy=loopback_ok, deadline=Deadline(0)).get(httpserver.url_for("/x"), max_bytes=1000)
    assert e.value.code == "timeout"
    assert len(httpserver.log) == 0


def test_default_deadline_from_settings():
    f = Fetcher()
    assert 170 < f.deadline.remaining() <= 180


def test_throttle_spaces_same_host():
    now = [100.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    t = HostThrottle(1.0, clock=lambda: now[0], sleep=sleep)
    t.wait("a")
    t.wait("a")
    t.wait("b")
    assert slept == [pytest.approx(1.0)]


def test_filename_from_content_disposition(httpserver, loopback_ok):
    no_robots(httpserver)
    httpserver.expect_request("/dl").respond_with_data(
        "a,b", headers={"Content-Disposition": 'attachment; filename="report 2023.xlsx"'})
    httpserver.expect_request("/files/data.csv").respond_with_data("a,b")
    httpserver.expect_request("/").respond_with_data("<html></html>")
    f = Fetcher(policy=loopback_ok)
    assert f.get(httpserver.url_for("/dl"), max_bytes=1000).filename == "report 2023.xlsx"
    assert f.get(httpserver.url_for("/files/data.csv") + "?x=1", max_bytes=1000).filename == "data.csv"
    assert f.get(httpserver.url_for("/"), max_bytes=1000).filename == "download"


def test_non_2xx_is_returned_not_raised(httpserver, loopback_ok):
    no_robots(httpserver)
    httpserver.expect_request("/denied").respond_with_data("Access Denied", status=403)
    r = Fetcher(policy=loopback_ok).get(httpserver.url_for("/denied"), max_bytes=1000)
    assert r.status == 403 and r.content == b"Access Denied"


def test_connection_refused_is_fetch_failed(loopback_ok):
    with pytest.raises(safety.ScrapeBlocked) as e:
        Fetcher(policy=loopback_ok).get("http://127.0.0.1:9/", max_bytes=1000, check_robots=False)
    assert e.value.code == "fetch_failed"


def test_slow_answer_is_not_a_scan_timeout(httpserver, loopback_ok, monkeypatch):
    import time as _time

    from app.core.config import settings

    monkeypatch.setattr(settings, "request_timeout_seconds", 1)
    no_robots(httpserver)

    def slow(request):
        _time.sleep(2)
        return Response("late")

    httpserver.expect_request("/slow").respond_with_handler(slow)
    with pytest.raises(safety.ScrapeBlocked) as e:
        Fetcher(policy=loopback_ok).get(httpserver.url_for("/slow"), max_bytes=1000)
    assert e.value.code == "slow_response"
    assert e.value.message == "The site took longer than 1 seconds to answer."
