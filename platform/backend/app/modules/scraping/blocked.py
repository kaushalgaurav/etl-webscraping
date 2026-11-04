"""Recognise what stopped us reading a page, and say so plainly (spec §4.6).

A block is reported, never worked around: no CAPTCHA solving, no disguised
browser identity. Where the same data is known to be published officially
elsewhere, the entry carries that as a suggestion; those live in
suggestions.json (host suffix -> text) so they can be edited without code.
"""
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.modules.scraping.fetcher import Fetched
from app.modules.scraping.safety import MESSAGES, message

SUGGESTIONS_PATH = Path(__file__).with_name("suggestions.json")

_CAPTCHA_CLASSES = ("g-recaptcha", "h-captcha", "cf-turnstile")
_CAPTCHA_SCRIPTS = re.compile(r"recaptcha/api|hcaptcha\.com|challenges\.cloudflare\.com/turnstile", re.I)
_CHALLENGE_TEXT = re.compile(
    r"just a moment|access denied|verify you are human|cf-challenge|attention required|"
    r"checking your browser|request unsuccessful\. incapsula|ddos protection|"
    r"the requested url was rejected|your support id is", re.I)
_CAPTCHA_ATTR = re.compile("captcha", re.I)
_LOGIN_URL = re.compile(r"/(log-?in|sign-?in|signon|auth/|account/log)", re.I)
# A password box only means "login wall" when the page has little else on it.
_LOGIN_PAGE_MAX_TEXT = 200
_FORM_SUBMIT = re.compile(r"""<(input|button)[^>]*type=["']?submit|<button""", re.I)

__all__ = ["MESSAGES", "detect", "entry", "is_form_gated", "suggestion_for"]


@lru_cache(maxsize=1)
def _suggestions() -> dict[str, str]:
    return json.loads(SUGGESTIONS_PATH.read_text(encoding="utf-8"))


def suggestion_for(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    for suffix, text in _suggestions().items():
        if host == suffix or host.endswith("." + suffix):
            return text
    return None


def entry(code: str, what: str, **fmt) -> dict:
    """One "Couldn't read" entry for the manifest."""
    return {"what": what, "code": code, "message": message(code, **fmt),
            "suggestion": suggestion_for(what)}


def _has_captcha(soup: BeautifulSoup, html: str) -> bool:
    for cls in _CAPTCHA_CLASSES:
        if soup.find(class_=cls):
            return True
    if _CAPTCHA_SCRIPTS.search(html):
        return True
    if soup.find(attrs={"id": _CAPTCHA_ATTR}) or soup.find(class_=_CAPTCHA_ATTR):
        return True
    for img in soup.find_all("img"):
        if "captcha" in f"{img.get('src', '')} {img.get('alt', '')}".lower():
            return True
    return False


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def detect(fetched: Fetched, html: str | None, expected_file: bool = False) -> str | None:
    """bot_protection | captcha | login_required | None.

    `expected_file`: we asked for a data file and got a web page back. A
    challenge page then counts even with status 200, which is how some
    firewalls answer a file download they refuse.
    """
    if _LOGIN_URL.search(urlsplit(fetched.final_url).path) and fetched.final_url != fetched.url:
        return "login_required"
    if html is None:
        return None
    soup = BeautifulSoup(html, "lxml")
    if _has_captcha(soup, html):
        return "captcha"
    if (fetched.status in (403, 429, 503) or expected_file) and _CHALLENGE_TEXT.search(html):
        return "bot_protection"
    if soup.find("input", attrs={"type": re.compile("^password$", re.I)}):
        if len(_visible_text(soup)) < _LOGIN_PAGE_MAX_TEXT:
            return "login_required"
    return None


def is_form_gated(html: str, table_count: int) -> bool:
    """Dropdowns plus a submit button, and no data until the form is sent."""
    if table_count:
        return False
    soup = BeautifulSoup(html, "lxml")
    return bool(soup.find("select")) and bool(_FORM_SUBMIT.search(html))
