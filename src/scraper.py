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

from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)


class ScraperError(Exception):
    """Raised when the scraper cannot produce a confident reading."""


def fetch_url(
    url: str,
    cfg: dict,
    watch_class_numbers: list[str] | None = None,
    headless: bool = True,
) -> Any:
    """Load the given catalog URL in headless Chromium, intercept the
    search/classes XHR, and return its JSON. If watch_class_numbers is
    provided, prefer captures whose body contains any of them."""
    captured: list[tuple[str, Any]] = []
    host_substr = cfg["api_host_substring"]
    path_substr = cfg.get("api_path_substring", "search/classes")
    watch = [str(c) for c in (watch_class_numbers or [])]

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


# ASU catalog API field locations.
# The API wraps each section as { "CLAS": {...PeopleSoft fields...}, "seatInfo": {...} }.
# CLAS.ENRLCAP / ENRLTOT is a slightly STALE snapshot. seatInfo.ENRL_CAP /
# ENRL_TOT is the LIVE value the catalog page actually renders. Always prefer
# seatInfo when present.
PS_FIELD_CLASS_NBR = "CLASSNBR"
PS_CLAS_KEY = "CLAS"
PS_SEATINFO_KEY = "seatInfo"
PS_FIELD_CAP_CLAS = "ENRLCAP"
PS_FIELD_ENR_CLAS = "ENRLTOT"
PS_FIELD_CAP_SEATINFO = "ENRL_CAP"
PS_FIELD_ENR_SEATINFO = "ENRL_TOT"


def _find_class_containers(api_json) -> dict[str, dict]:
    """Find every ASU class container — a dict with a 'CLAS' sub-dict that has
    CLASSNBR. Returns {class_nbr: container_dict}. The container is the right
    object to pass to _extract_one because it carries both 'CLAS' and the
    sibling 'seatInfo'."""
    out: dict[str, dict] = {}
    for node in _walk(api_json):
        if not isinstance(node, dict):
            continue
        clas = node.get(PS_CLAS_KEY)
        if not isinstance(clas, dict):
            continue
        cnum = str(clas.get(PS_FIELD_CLASS_NBR, "")).strip()
        if not cnum or not cnum.isdigit():
            continue
        if cnum not in out:
            out[cnum] = node
    return out


def auto_detect_schema(container_or_rec: dict) -> dict | None:
    """Pick a schema for a container or raw record. The 'asu_catalog' mode is
    the only one written by current verifications; the others remain
    supported so existing data/verified.flag files keep working."""
    if not isinstance(container_or_rec, dict):
        return None
    # Container with seatInfo (best) or with a CLAS subdict (fallback inside
    # asu_catalog mode).
    seat = container_or_rec.get(PS_SEATINFO_KEY)
    clas = container_or_rec.get(PS_CLAS_KEY)
    if isinstance(seat, dict) and PS_FIELD_CAP_SEATINFO in seat and PS_FIELD_ENR_SEATINFO in seat:
        return {"mode": "asu_catalog"}
    if isinstance(clas, dict) and PS_FIELD_CAP_CLAS in clas and PS_FIELD_ENR_CLAS in clas:
        return {"mode": "asu_catalog"}
    # Legacy: a raw CLAS dict.
    if PS_FIELD_CAP_CLAS in container_or_rec and PS_FIELD_ENR_CLAS in container_or_rec:
        return {
            "mode": "subtract",
            "field_class_nbr": PS_FIELD_CLASS_NBR,
            "field_cap": PS_FIELD_CAP_CLAS,
            "field_enrolled": PS_FIELD_ENR_CLAS,
        }
    return None


def _seat_from_container(container: dict) -> tuple[int | None, int | None, str | None]:
    """Try seatInfo first, then CLAS. Returns (cap, enrolled, source_label)."""
    seat = container.get(PS_SEATINFO_KEY)
    if isinstance(seat, dict):
        cap = _coerce_int(seat.get(PS_FIELD_CAP_SEATINFO))
        enr = _coerce_int(seat.get(PS_FIELD_ENR_SEATINFO))
        if cap is not None and enr is not None:
            return cap, enr, "seatInfo"
    clas = container.get(PS_CLAS_KEY)
    if isinstance(clas, dict):
        cap = _coerce_int(clas.get(PS_FIELD_CAP_CLAS))
        enr = _coerce_int(clas.get(PS_FIELD_ENR_CLAS))
        if cap is not None and enr is not None:
            return cap, enr, "CLAS"
    return None, None, None


def _extract_one(container_or_rec: dict, schema: dict, cnum: str) -> dict:
    mode = schema.get("mode", "asu_catalog")
    if mode == "asu_catalog":
        if not isinstance(container_or_rec, dict):
            raise ScraperError(f"class {cnum}: container not a dict")
        cap, enr, source = _seat_from_container(container_or_rec)
        if cap is None or enr is None:
            raise ScraperError(f"class {cnum}: no usable seat data in container")
        open_val = cap - enr
        if open_val < 0:
            # Possible when waitlisted students are also counted; clamp to 0.
            log.warning(
                "class %s: %s reports enrolled (%s) > cap (%s); clamping open to 0",
                cnum, source, enr, cap,
            )
            open_val = 0
        return {
            "open": open_val,
            "total": cap,
            "raw_record": container_or_rec,
            "fields": {"mode": "asu_catalog", "source": source},
        }

    # Legacy paths — used by existing verified.flag files that pre-date
    # the seatInfo discovery. They operate on a flat record (the CLAS dict).
    rec = container_or_rec.get(PS_CLAS_KEY) if (
        isinstance(container_or_rec, dict) and PS_CLAS_KEY in container_or_rec
    ) else container_or_rec

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
        return {
            "open": max(0, cap - enr),
            "total": cap,
            "raw_record": container_or_rec,
            "fields": {"mode": "subtract", "cap": cap_field, "enrolled": enr_field},
        }
    if mode == "direct":
        f_open = schema.get("field_open")
        f_total = schema.get("field_total")
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
            "raw_record": container_or_rec,
            "fields": {"mode": "direct", "open": f_open, "total": f_total},
        }
    raise ScraperError(f"unknown schema mode: {mode!r}")


def find_all_class_records(api_json, schema: dict | None = None) -> dict[str, dict]:
    """Return {class_nbr: container_dict} for every class section in the
    payload. (Schema arg kept for API compatibility; not used.)"""
    return _find_class_containers(api_json)


def extract_seats(
    api_json,
    class_numbers: list[str] | None,
    field_overrides: dict | None = None,
) -> dict[str, dict]:
    """Return {class_nbr: {open, total, raw_record, fields}}.

    If class_numbers is non-empty, only those classes are returned (and
    missing ones raise ScraperError). Otherwise every class section in the
    payload is returned.

    field_overrides — a schema dict (from data/verified.flag) or None for
    auto-detect.
    """
    containers = _find_class_containers(api_json)
    if not containers:
        raise ScraperError(
            "no class records found in the response — is the URL a valid catalog search?"
        )

    if class_numbers:
        wanted = {str(c) for c in class_numbers}
        missing = wanted - containers.keys()
        if missing:
            raise ScraperError(
                f"these watched class numbers are missing from the response: {sorted(missing)}"
            )
        candidates = {c: containers[c] for c in wanted}
    else:
        candidates = containers

    schema = field_overrides
    if not schema or not schema.get("mode"):
        first = next(iter(candidates.values()))
        schema = auto_detect_schema(first)
        if schema is None:
            raise ScraperError("could not auto-detect a seat-count schema")

    out: dict[str, dict] = {}
    for cnum, container in candidates.items():
        try:
            out[cnum] = _extract_one(container, schema, cnum)
        except ScraperError as e:
            if class_numbers:
                raise
            log.warning("skipping class %s: %s", cnum, e)
    return out
