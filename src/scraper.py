"""Playwright-based fetch of the ASU catalog microservice JSON.

The catalog page is an SPA. A direct GET to the microservice returns 401
without a bearer token that the SPA obtains at load time. We load the catalog
page in headless Chromium, intercept the XHR to the microservice host, and
parse the JSON.

ASU's payload is PeopleSoft-shaped: numeric fields are sent as STRINGS, and
there is no "open seats" field — open seats is computed as
``ENRLCAP - ENRLTOT`` (capacity minus currently-enrolled). We support both
that "subtract" mode and a generic "direct" mode where one field is the
open-seat count.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)


class ScraperError(Exception):
    """Raised when the scraper cannot produce a confident reading."""


def fetch_classes(cfg: dict, headless: bool = True) -> Any:
    """Load the catalog page and return the JSON body from the search/classes
    XHR. The catalog host serves many endpoints — subjects dropdown, terms,
    etc — so we filter on path, then prefer captures that contain a watched
    class number. Raises ScraperError on failure."""
    captured: list[tuple[str, Any]] = []
    url = cfg["catalog_url"] + "?" + urlencode(cfg["query"])
    host_substr = cfg["api_host_substring"]
    path_substr = cfg.get("api_path_substring", "search/classes")
    watch = [str(c) for c in cfg.get("watch_class_numbers", [])]

    log.info("Launching Chromium (headless=%s) for %s", headless, url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()

            def on_response(resp):
                if host_substr not in resp.url:
                    return
                try:
                    body = resp.json()
                except Exception:
                    return
                if not body:
                    return
                captured.append((resp.url, body))
                log.info("Captured XHR (%s bytes-ish) from %s", len(str(body)), resp.url)

            page.on("response", on_response)

            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                log.warning("page.goto raised %s; checking captures anyway", e)
        finally:
            browser.close()

    if not captured:
        raise ScraperError(
            f"No response captured from microservice host '{host_substr}'"
        )

    def body_has_watch(body) -> bool:
        if not watch:
            return False
        text = str(body)
        return any(w in text for w in watch)

    path_matches = [(u, b) for (u, b) in captured if path_substr in u]
    for u, b in reversed(path_matches):
        if body_has_watch(b):
            log.info("Selected capture (path+watch match): %s", u)
            return b
    if path_matches:
        u, b = path_matches[-1]
        log.info("Selected capture (path match, no watch hit yet): %s", u)
        return b
    for u, b in reversed(captured):
        if body_has_watch(b):
            log.info("Selected capture (watch match, no path filter): %s", u)
            return b
    u, b = captured[-1]
    log.warning(
        "No capture matched path '%s' or contained a watched class number; "
        "falling back to last capture from %s",
        path_substr,
        u,
    )
    return b


def _walk(node):
    """Yield every dict found anywhere in a nested JSON structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _find_records(api_json, class_numbers: list[str]) -> dict[str, dict]:
    """Find the dict record for each class number by scanning all dicts in the
    payload for any field whose stringified value equals the class number."""
    wanted = {str(c) for c in class_numbers}
    found: dict[str, dict] = {}
    for record in _walk(api_json):
        for v in record.values():
            if isinstance(v, (str, int)) and str(v) in wanted:
                if len(record) < 3:
                    continue
                key = str(v)
                if key not in found:
                    found[key] = record
    return found


def _coerce_int(value) -> int | None:
    """Best-effort conversion of int/float/numeric-string to int."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            try:
                return int(float(s))
            except ValueError:
                return None
    return None


def numeric_fields(rec: dict) -> list[tuple[str, int]]:
    """Return [(field_name, int_value)] for every field that can be coerced
    to an int. String numerics count."""
    out = []
    for k, v in rec.items():
        coerced = _coerce_int(v)
        if coerced is not None:
            out.append((k, coerced))
    return out


def _pick_numeric_field(record: dict, name_hints: tuple[str, ...]) -> tuple[str | None, int | None]:
    """Pick the first field whose name contains any of the given lowercased
    substrings AND whose value is int-coercible. Returns (field, value) or
    (None, None)."""
    for k, v in record.items():
        coerced = _coerce_int(v)
        if coerced is None:
            continue
        kl = k.lower()
        if any(h in kl for h in name_hints):
            return k, coerced
    return None, None


# Known PeopleSoft field names ASU's catalog API uses.
PS_FIELD_CLASS_NBR = "CLASSNBR"
PS_FIELD_CAP = "ENRLCAP"
PS_FIELD_ENROLLED = "ENRLTOT"


def auto_detect_schema(rec: dict) -> dict | None:
    """Inspect one record and decide how to compute open seats. Returns a
    schema dict suitable for extract_seats(field_overrides=...) or None."""
    # Preferred: PeopleSoft subtract pattern (ASU catalog).
    if PS_FIELD_CAP in rec and PS_FIELD_ENROLLED in rec:
        cap = _coerce_int(rec.get(PS_FIELD_CAP))
        enr = _coerce_int(rec.get(PS_FIELD_ENROLLED))
        if cap is not None and enr is not None:
            return {
                "mode": "subtract",
                "field_class_nbr": PS_FIELD_CLASS_NBR if PS_FIELD_CLASS_NBR in rec else None,
                "field_cap": PS_FIELD_CAP,
                "field_enrolled": PS_FIELD_ENROLLED,
            }
    # Fallback: any field whose name says "open".
    open_field, open_val = _pick_numeric_field(rec, ("open",))
    total_field, total_val = _pick_numeric_field(rec, ("cap", "enroll", "total"))
    if open_val is not None and total_val is not None:
        return {
            "mode": "direct",
            "field_class_nbr": None,
            "field_open": open_field,
            "field_total": total_field,
        }
    return None


def _extract_one(rec: dict, schema: dict, cnum: str) -> dict:
    mode = schema.get("mode", "direct")
    if mode == "subtract":
        cap_field = schema["field_cap"]
        enr_field = schema["field_enrolled"]
        cap = _coerce_int(rec.get(cap_field))
        enr = _coerce_int(rec.get(enr_field))
        if cap is None or enr is None:
            raise ScraperError(
                f"class {cnum}: subtract mode but "
                f"{cap_field}={rec.get(cap_field)!r}, {enr_field}={rec.get(enr_field)!r}"
            )
        open_val = cap - enr
        total = cap
        if open_val < 0:
            raise ScraperError(
                f"class {cnum}: computed open seats negative ({cap} - {enr} = {open_val})"
            )
        return {
            "open": open_val,
            "total": total,
            "raw_record": rec,
            "fields": {"mode": "subtract", "enrolled": enr_field, "cap": cap_field},
        }
    elif mode == "direct":
        f_open = schema.get("field_open")
        f_total = schema.get("field_total")
        if not f_open or not f_total:
            raise ScraperError(f"class {cnum}: direct mode missing field_open/field_total")
        open_val = _coerce_int(rec.get(f_open))
        total_val = _coerce_int(rec.get(f_total))
        if open_val is None or total_val is None:
            raise ScraperError(
                f"class {cnum}: direct mode but "
                f"{f_open}={rec.get(f_open)!r}, {f_total}={rec.get(f_total)!r}"
            )
        return {
            "open": open_val,
            "total": total_val,
            "raw_record": rec,
            "fields": {"mode": "direct", "open": f_open, "total": f_total},
        }
    else:
        raise ScraperError(f"unknown schema mode: {mode!r}")


def extract_seats(
    api_json,
    class_numbers: list[str],
    total_expected: int,
    field_overrides: dict | None = None,
) -> dict[str, dict]:
    """Return {class_nbr: {open, total, raw_record, fields}} for every
    watched class.

    field_overrides — a schema dict as produced by auto_detect_schema() or
    stored in data/verified.flag. If None, auto-detection runs on the first
    matched record.
    """
    records = _find_records(api_json, class_numbers)
    if not records:
        raise ScraperError(
            f"none of the watched class numbers {class_numbers} appear in the response"
        )

    schema = field_overrides
    if not schema or not schema.get("mode"):
        first_rec = next(iter(records.values()))
        schema = auto_detect_schema(first_rec)
        if schema is None:
            raise ScraperError(
                "could not auto-detect a seat-count schema on the matched record"
            )

    out: dict[str, dict] = {}
    for cnum in class_numbers:
        rec = records.get(str(cnum))
        if rec is None:
            raise ScraperError(f"class {cnum} missing from response")
        out[str(cnum)] = _extract_one(rec, schema, str(cnum))
    return out
