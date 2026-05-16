"""Interactive correctness gate.

Launches Playwright in NON-headless mode so the user can watch the browser
fetch the catalog. Captures the microservice JSON, identifies records for
the watched class numbers, prints every numeric field on each record (with
ASU's PeopleSoft "string-numerics" coerced), auto-detects the seat-count
schema (subtract or direct), and asks the user to confirm the detected
open/total values match the live page.

On confirmation, writes data/verified.flag with the resolved schema. The
watcher refuses to run until that flag exists, so a misconfigured field
mapping can never silently produce wrong alerts.
"""

from __future__ import annotations

import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

_THIS = Path(__file__).resolve()
_PROJECT_ROOT = _THIS.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src import scraper  # noqa: E402

CONFIG_PATH = _PROJECT_ROOT / "config.json"
DATA_DIR = _PROJECT_ROOT / "data"
SNAPSHOT_PATH = DATA_DIR / "verify_snapshot.json"
FLAG_PATH = DATA_DIR / "verified.flag"


def _truncate(value, depth=0, max_depth=4, max_list=6, max_str=200):
    if depth >= max_depth:
        return "…"
    if isinstance(value, dict):
        return {k: _truncate(v, depth + 1, max_depth, max_list, max_str) for k, v in value.items()}
    if isinstance(value, list):
        head = [_truncate(v, depth + 1, max_depth, max_list, max_str) for v in value[:max_list]]
        if len(value) > max_list:
            head.append(f"… (+{len(value) - max_list} more)")
        return head
    if isinstance(value, str) and len(value) > max_str:
        return value[:max_str] + "…"
    return value


def _prompt(msg: str) -> str:
    print(msg, end="", flush=True)
    return sys.stdin.readline().strip()


def _describe_schema(schema: dict) -> str:
    mode = schema.get("mode")
    if mode == "subtract":
        return (
            f"mode=subtract  open = {schema['field_cap']} - {schema['field_enrolled']}, "
            f"total = {schema['field_cap']}"
        )
    if mode == "direct":
        return f"mode=direct  open = {schema['field_open']}, total = {schema['field_total']}"
    return f"mode={mode!r}"


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    live_url = cfg["catalog_url"] + "?" + urlencode(cfg["query"])
    print("=" * 60)
    print("ASU Seat Watcher — Verification")
    print("=" * 60)
    print(f"Live catalog URL:\n  {live_url}")
    print(f"Watched class numbers: {cfg['watch_class_numbers']}")
    print(f"Expected total seats:  {cfg['expected_total_seats']}")
    print()
    print("Launching Chromium in NON-headless mode so you can watch it work…")
    print()

    try:
        api_json = scraper.fetch_classes(cfg, headless=False)
    except scraper.ScraperError as e:
        print(f"\nFAILED: {e}")
        print("Verification cannot proceed. Check your network / config.")
        return 2

    with SNAPSHOT_PATH.open("w", encoding="utf-8") as f:
        json.dump(api_json, f, indent=2, sort_keys=True)
    print(f"Captured JSON snapshot written to: {SNAPSHOT_PATH}")

    print("\n--- Truncated JSON preview ---")
    print(json.dumps(_truncate(api_json), indent=2)[:4000])
    print("--- (truncated) ---\n")

    records = scraper._find_records(api_json, cfg["watch_class_numbers"])
    if not records:
        print("No records found for any watched class number. Check the snapshot file.")
        return 2

    for cnum in cfg["watch_class_numbers"]:
        rec = records.get(str(cnum))
        if rec is None:
            print(f"\n[!] Class {cnum} was NOT FOUND in the API response.")
            continue
        print("-" * 60)
        print(f"Class {cnum} — int-coercible fields on the matched record:")
        for k, v in scraper.numeric_fields(rec):
            print(f"   {k:30s} = {v}")

    # Auto-detect schema, then run extraction.
    schema = None
    parsed = {}
    first_rec = next(iter(records.values()))
    schema = scraper.auto_detect_schema(first_rec)
    if schema is None:
        print("\nAuto-detection could not pick a schema from the record.")
    else:
        try:
            parsed = scraper.extract_seats(
                api_json,
                cfg["watch_class_numbers"],
                cfg["expected_total_seats"],
                field_overrides=schema,
            )
        except scraper.ScraperError as e:
            print(f"\nExtraction with auto-detected schema failed: {e}")
            parsed = {}

    print()
    print("=" * 60)
    print("DETECTED VALUES")
    print("=" * 60)
    if schema:
        print(_describe_schema(schema))
    if parsed:
        for cnum, rec in parsed.items():
            print(f"Class {cnum}: open={rec['open']}, total={rec['total']}")
    else:
        print("(no values — see numeric fields printed above)")
    print("=" * 60)
    print()

    print("Opening the live catalog page in your default browser so you can compare…")
    try:
        webbrowser.open(live_url)
    except Exception:
        pass
    print()

    if parsed and schema:
        ans = _prompt(
            "Do the open/total numbers above match what you see on the catalog page? (y/n): "
        ).lower()
    else:
        ans = "n"

    if ans == "y":
        observed = {
            c: {"open": parsed[c]["open"], "total": parsed[c]["total"]}
            for c in parsed
        }
        flag = {
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "schema": schema,
            "observed": observed,
        }
        with FLAG_PATH.open("w", encoding="utf-8") as f:
            json.dump(flag, f, indent=2, sort_keys=True)
        print(f"\nVerified flag written: {FLAG_PATH}")
        print("The watcher is now allowed to run.")
        return 0

    if ans == "n":
        print()
        print("OK — let's identify the correct field names by hand.")
        print("Choose schema mode:")
        print("  1) subtract   open = cap_field - enrolled_field   (PeopleSoft style)")
        print("  2) direct     open = open_field directly")
        print("  type 'abort' at any prompt to bail without writing a flag")
        choice = _prompt("Mode (1/2): ").strip()
        if choice.lower() == "abort":
            print("Aborted. No flag written.")
            return 1
        if choice == "1":
            cap_field = _prompt("Field name for CAP / TOTAL seats (e.g. ENRLCAP): ").strip()
            if cap_field.lower() == "abort" or not cap_field:
                return 1
            enr_field = _prompt("Field name for ENROLLED count (e.g. ENRLTOT): ").strip()
            if enr_field.lower() == "abort" or not enr_field:
                return 1
            manual = {
                "mode": "subtract",
                "field_class_nbr": "CLASSNBR" if "CLASSNBR" in first_rec else None,
                "field_cap": cap_field,
                "field_enrolled": enr_field,
            }
        elif choice == "2":
            open_field = _prompt("Field name for OPEN seats: ").strip()
            if open_field.lower() == "abort" or not open_field:
                return 1
            total_field = _prompt("Field name for TOTAL / CAP: ").strip()
            if total_field.lower() == "abort" or not total_field:
                return 1
            manual = {
                "mode": "direct",
                "field_class_nbr": "CLASSNBR" if "CLASSNBR" in first_rec else None,
                "field_open": open_field,
                "field_total": total_field,
            }
        else:
            print("Unrecognized choice. No flag written.")
            return 1

        try:
            parsed = scraper.extract_seats(
                api_json,
                cfg["watch_class_numbers"],
                cfg["expected_total_seats"],
                field_overrides=manual,
            )
        except scraper.ScraperError as e:
            print(f"Extraction with your schema failed: {e}")
            return 2

        print()
        print("Re-detected with your schema:")
        print(_describe_schema(manual))
        for cnum, rec in parsed.items():
            print(f"Class {cnum}: open={rec['open']}, total={rec['total']}")
        confirm = _prompt("Do THESE numbers match the live page? (y/n): ").lower()
        if confirm != "y":
            print("Aborted. No flag written.")
            return 1

        flag = {
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "schema": manual,
            "observed": {
                c: {"open": parsed[c]["open"], "total": parsed[c]["total"]}
                for c in parsed
            },
        }
        with FLAG_PATH.open("w", encoding="utf-8") as f:
            json.dump(flag, f, indent=2, sort_keys=True)
        print(f"\nVerified flag written: {FLAG_PATH}")
        return 0

    print("Unrecognized answer. No flag written.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
