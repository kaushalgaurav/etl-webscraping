import pytest

from app.modules.scraping import blocked
from app.modules.scraping.fetcher import Fetched


def page(body: str, status: int = 200, final_url: str = "https://x.gov.in/p") -> Fetched:
    return Fetched(url="https://x.gov.in/p", final_url=final_url, status=status,
                   content_type="text/html", content=body.encode(), filename="p")


TABLE_PAGE = "<html><body><h1>Stats</h1><table><tr><th>A</th></tr><tr><td>1</td></tr></table>" + \
    "<p>" + "Plenty of ordinary words on the page. " * 20 + "</p></body></html>"


@pytest.mark.parametrize("status,body", [
    (403, "<title>Just a moment...</title>"),
    (429, "<h1>Access Denied</h1>"),
    (503, "<div id='cf-challenge'>Verify you are human</div>"),
])
def test_challenge_pages_are_bot_protection(status, body):
    assert blocked.detect(page(body, status), body) == "bot_protection"


def test_plain_403_is_not_bot_protection():
    body = "<h1>Forbidden</h1>"
    assert blocked.detect(page(body, 403), body) is None


@pytest.mark.parametrize("body", [
    '<div class="g-recaptcha" data-sitekey="x"></div>',
    '<div class="h-captcha"></div>',
    '<div class="cf-turnstile"></div>',
    '<img src="/Captcha.aspx?id=1" alt="code">',
    '<img src="/x.png" alt="Captcha image">',
])
def test_captcha_markers(body):
    assert blocked.detect(page(body), body) == "captcha"


def test_redirect_to_login_is_login_required():
    for final in ("https://x.gov.in/login?next=/p", "https://x.gov.in/Account/SignIn"):
        assert blocked.detect(page("<html></html>", final_url=final), "<html></html>") == "login_required"


def test_password_form_page_is_login_required():
    body = '<form><input name="u"><input type="password" name="p"><button>Log in</button></form>'
    assert blocked.detect(page(body), body) == "login_required"


def test_password_box_on_a_big_page_is_not_login():
    body = TABLE_PAGE.replace("</body>", '<form><input type="password"></form></body>')
    assert blocked.detect(page(body), body) is None


def test_plain_page_is_not_blocked():
    assert blocked.detect(page(TABLE_PAGE), TABLE_PAGE) is None


def test_non_html_file_is_not_checked():
    assert blocked.detect(page("a,b\n1,2"), None) is None


def test_form_gated():
    body = ("<form><select name='s'><option>Bihar</option></select>"
            "<select name='d'><option>Patna</option></select><input type='submit'></form>")
    assert blocked.is_form_gated(body, 0) is True
    assert blocked.is_form_gated(body, 1) is False
    assert blocked.is_form_gated("<p>nothing</p>", 0) is False


def test_entry_adds_suggestion():
    e = blocked.entry("captcha", "https://lgdirectory.gov.in/x")
    assert e["code"] == "captcha" and e["what"] == "https://lgdirectory.gov.in/x"
    assert e["message"] == "Data is behind a CAPTCHA."
    assert "data.gov.in" in e["suggestion"]
    assert blocked.entry("captcha", "https://other.in/")["suggestion"] is None


def test_entry_formats_limit():
    assert blocked.entry("too_large", "u", n=30)["message"] == "File is larger than the 30 MB limit."
    assert blocked.entry("http_error", "u", status=500)["message"] == "The site answered with status 500."


def test_every_code_has_exact_message():
    assert blocked.MESSAGES["robots_disallowed"] == "The site asks automated tools not to read this page."
    assert blocked.MESSAGES["needs_browser"] == (
        "This page builds its content with JavaScript; it can only be read with "
        "browser rendering.")
    assert blocked.MESSAGES["browser_unavailable"] == (
        "Browser mode is not available on this server, so only a plain read was done.")
    assert blocked.MESSAGES["browser_failed"] == (
        "The page could not be opened in a browser, so only a plain read was done.")
    assert blocked.MESSAGES["certificate_error"] == (
        "The site's security certificate couldn't be verified.")
    assert set(blocked.MESSAGES) == {
        "robots_disallowed", "bot_protection", "captcha", "login_required", "form_gated",
        "needs_browser", "private_address", "too_large", "timeout",
        "too_many_files", "unsupported", "http_error", "fetch_failed", "slow_response",
        "browser_unavailable", "browser_failed", "certificate_error"}


def test_captcha_element_id():
    body = '<audio id="captcha_audio"><source src="x.mp3"></audio>'
    assert blocked.detect(page(body), body) == "captcha"


REJECTED = ("<html><body>The requested URL was rejected. Please consult with your administrator."
            "<br>Your support ID is: 1234567890</body></html>")


def test_rejection_page_instead_of_file_is_bot_protection():
    assert blocked.detect(page(REJECTED), REJECTED, expected_file=True) == "bot_protection"
    assert blocked.detect(page(REJECTED, 403), REJECTED) == "bot_protection"


def test_support_id_on_ordinary_200_page_is_not_flagged():
    body = TABLE_PAGE.replace("</body>", "<p>Your support ID is: 42</p></body>")
    assert blocked.detect(page(body), body) is None
