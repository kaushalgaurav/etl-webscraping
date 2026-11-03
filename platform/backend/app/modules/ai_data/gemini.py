"""Google Gemini client: a plain-language request in, a data table out.

Called over the REST API with `requests` (already a dependency) rather than a
Google SDK. Google Search grounding is on by default so the model can look
figures up; the pages it used come back as `grounding_sources`.

Whatever comes back is model output, not an official dataset — see
app/modules/ai_data/service.py for how it is labelled and checked.
"""
import json
import re
import threading
import time
from collections import deque

import requests

from app.core.config import settings

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM = """You return Indian public data (Census of India, HMIS and similar official statistics) as one table.

Reply with ONLY a JSON object, no prose and no markdown fences, in exactly this shape:
{
  "title": "<what the table holds>",
  "columns": ["<column 1>", "<column 2>", ...],
  "rows": [[<value>, <value>, ...], ...],
  "estimated_columns": ["<every column whose values are projections, estimates or your own calculations rather than published official figures>"],
  "sources": [{"name": "<publication or portal>", "url": "<link or null>", "year": "<reference year>"}],
  "column_sources": {"<column>": {"source": "<publication the column's values come from, e.g. Census of India 2011, NFHS-5, HMIS 2022-23, RHS 2022, or 'Gemini estimate'>", "unit": "<unit, e.g. persons, %, per 1000 live births>"}},
  "notes": "<caveats: reference years, boundary changes such as Telangana 2014 or Ladakh 2019, anything you were unsure of>"
}

Rules:
- One row per place (state, UT or district) or per place and year when a year-wise table is asked for. Include a year column when years vary.
- Name the place columns "State" and, for district tables, "District"; name the time column "Year".
- A request may span several sources (Census, HMIS, NFHS, RHS, ...): put them in one table and say in column_sources which source each value column comes from.
- Numbers as plain numbers: no thousands separators, no % sign (put the unit in the column name).
- Prefer published official figures and name where each comes from. Census of India is decennial (2001, 2011); there is no official 2021 or later census count, so any later figure is an estimate and its column must be listed in estimated_columns.
- Do not fill a value you do not know: use null.
"""


class GeminiNotConfigured(Exception):
    pass


class GeminiError(Exception):
    pass


def vocabulary_instruction(source_label: str, columns: list[str], max_columns: int,
                           exact: bool = False) -> str:
    """Tie Gemini to one source and to that source's exact warehouse column names.

    `exact`: return every listed column (one batch of an all-columns fetch);
    otherwise pick the ones the request needs.
    """
    which = (f"Return ALL {len(columns)} of these value columns, no others — use null for a "
             "value you do not know." if exact else
             f"Pick the ones the request needs, at most {max_columns}.")
    return (
        f"\n\nAnswer ONLY from this one source: {source_label}. Do not include data from any "
        "other source.\n"
        "Value columns MUST use these exact column names (snake_case, character for "
        "character) — the platform's own names for this source; any other name is discarded. "
        f"{which} Place/time columns are named State, District and Year as usual.\n"
        f"{'Value columns' if exact else 'Allowed value columns'} ({len(columns)}): "
        f"{', '.join(columns)}"
    )


def year_instruction(year: str) -> str:
    """Pin an answer to one reporting year (HMIS: financial year April-March)."""
    return (f"\n\nGive the figures for reporting year {year} only (HMIS reporting years are "
            "financial years, April to March). Put that year in the Year column of every row.")


def ask_for_table(prompt: str, use_search: bool = True, extra: str = "") -> dict:
    """Send `prompt` to Gemini and return its parsed table plus grounding sources.

    `extra` is appended to the request — the single-source column list, when given.
    """
    if not settings.gemini_api_key:
        raise GeminiNotConfigured(
            "GEMINI_API_KEY is not set. Add it to the backend's .env to enable /ai-data."
        )

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt + extra}]}],
        "generationConfig": {"temperature": 0},
    }
    if use_search:
        body["tools"] = [{"google_search": {}}]
    else:
        # JSON mode: Google guarantees syntactically valid JSON. (Not combined
        # with search grounding, which does not support it.) Without it a long
        # answer came back unparseable partway through.
        body["generationConfig"]["responseMimeType"] = "application/json"

    resp = _post_with_limits(body)

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise GeminiError(f"Gemini returned no answer{f' (blocked: {reason})' if reason else ''}.")
    candidate = candidates[0]
    text = "".join(p.get("text", "") for p in (candidate.get("content") or {}).get("parts", []))
    table = parse_table(text)

    grounding = candidate.get("groundingMetadata") or {}
    table["grounding_sources"] = [
        {"title": (c.get("web") or {}).get("title"), "url": (c.get("web") or {}).get("uri")}
        for c in grounding.get("groundingChunks", []) if c.get("web")
    ]
    table["model"] = settings.gemini_model
    return table


# --------------------------------------------------------------------------
# Free-tier limits: pace calls, retry "too many requests" / "busy" answers
# --------------------------------------------------------------------------

_calls: deque[float] = deque()
_calls_lock = threading.Lock()
# HTTP codes worth waiting out: rate limit, overloaded, temporarily unavailable.
_RETRYABLE = {429, 500, 503}


def _wait_for_slot() -> None:
    """Block until a call fits within GEMINI_RPM calls per rolling minute."""
    limit = max(1, settings.gemini_requests_per_minute)
    while True:
        with _calls_lock:
            now = time.monotonic()
            while _calls and now - _calls[0] >= 60:
                _calls.popleft()
            if len(_calls) < limit:
                _calls.append(now)
                return
            wait = 60 - (now - _calls[0]) + 0.1
        time.sleep(wait)


def _retry_delay(resp: requests.Response, attempt: int) -> float:
    """Google's suggested wait (Retry-After header or RetryInfo), else backoff."""
    header = resp.headers.get("Retry-After")
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    try:
        for detail in resp.json().get("error", {}).get("details", []):
            delay = detail.get("retryDelay")  # e.g. "17s"
            if delay and delay.rstrip("s").replace(".", "", 1).isdigit():
                return float(delay.rstrip("s"))
    except ValueError:
        pass
    return min(60.0, 5.0 * 2 ** attempt)  # 5s, 10s, 20s, ...


def _is_daily_quota(resp: requests.Response) -> bool:
    """A per-day quota will not clear by waiting seconds — don't retry it."""
    return resp.status_code == 429 and "PerDay" in resp.text


def _post_with_limits(body: dict) -> requests.Response:
    attempts = max(0, settings.gemini_retries) + 1
    for attempt in range(attempts):
        _wait_for_slot()
        try:
            resp = requests.post(
                API.format(model=settings.gemini_model),
                headers={"x-goog-api-key": settings.gemini_api_key,
                         "Content-Type": "application/json"},
                json=body,
                timeout=max(settings.request_timeout_seconds, 180),
            )
        except requests.RequestException as exc:
            if attempt + 1 < attempts:
                time.sleep(min(60.0, 5.0 * 2 ** attempt))
                continue
            raise GeminiError(f"Could not reach Gemini: {exc}") from exc

        if resp.status_code == 200:
            return resp
        if _is_daily_quota(resp):
            raise GeminiError(
                "Gemini's daily free-tier quota for this key is used up. It resets daily "
                "(Pacific time); try again later, or enable billing on the key in Google AI Studio."
            )
        if resp.status_code in _RETRYABLE and attempt + 1 < attempts:
            time.sleep(_retry_delay(resp, attempt))
            continue
        if resp.status_code == 429:
            raise GeminiError(
                f"Gemini is rate-limiting this key (free tier) even after {attempts} attempts. "
                "Wait a minute and retry, or lower GEMINI_RPM in .env."
            )
        raise GeminiError(f"Gemini returned HTTP {resp.status_code}: {resp.text[:500]}")
    raise GeminiError("Gemini request failed.")  # not reached


def parse_table(text: str) -> dict:
    """The JSON object in Gemini's reply, tolerating fences or stray prose."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise GeminiError(f"Gemini did not return a JSON table. It said: {text[:400]}")
    try:
        table = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise GeminiError(f"Gemini's table was not valid JSON ({exc}).") from exc
    if not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list):
        raise GeminiError("Gemini's reply is missing 'columns' or 'rows'.")
    return table
