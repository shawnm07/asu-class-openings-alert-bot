"""Interactive correctness gate.

Iterates every watch in config["watches"], fetches each catalog URL in a
visible Chromium window, prints the detected open/total per section, and
asks the user to confirm against the live page. Writes data/verified.flag
with the auto-detected schema once the user confirms.

The watcher refuses to run until this flag exists.
"""

from __future__ import annotations

import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

_THIS = Path(__file__).resolve()
_PROJECT_ROOT = _THIS.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src import scraper  # noqa: E402

CONFIG_PATH = _PROJECT_ROOT / "config.json"
DATA_DIR = _PROJECT_ROOT / "data"
SNAPSHOT_PATH = DATA_DIR / "verify_snapshot.json"
FLAG_PATH = DATA_DIR / "verified.flag"


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


def _verify_one_watch(watch: dict, cfg: dict, snapshots: dict) -> tuple[dict | None, dict] | None:
    """Returns ((schema, observed_per_class)) on success, None on abort."""
    name = watch.get("name") or watch.get("url") or "<unnamed>"
    url = watch.get("url")
    if not url:
        print(f"[!] watch {name!r} has no 'url' — skipping.")
        return None
    class_numbers = [str(c) for c in (watch.get("class_numbers") or [])]

    print("=" * 60)
    print(f"WATCH: {name}")
    print(f"  URL: {url}")
    if class_numbers:
        print(f"  Filtered to class numbers: {class_numbers}")
    else:
        print("  Watching ALL sections returned by this search.")
    print("=" * 60)
    print("Launching Chromium (non-headless)…")

    try:
        api_json = scraper.fetch_url(url, cfg, watch_class_numbers=class_numbers, headless=False)
    except scraper.ScraperError as e:
        print(f"  FAILED: {e}")
        return None

    snapshots[name] = api_json

    try:
        parsed = scraper.extract_seats(api_json, class_numbers if class_numbers else None, field_overrides=None)
    except scraper.ScraperError as e:
        print(f"  Extraction failed: {e}")
        return None

    if not parsed:
        print("  No class records found.")
        return None

    # Re-run with explicit schema so we can save it.
    first_rec = next(iter(parsed.values()))["raw_record"]
    schema = scraper.auto_detect_schema(first_rec)
    if schema is None:
        print("  Could not auto-detect schema. Aborting this watch.")
        return None

    print(f"  Schema: {_describe_schema(schema)}")
    print(f"  Sections found ({len(parsed)}):")
    for cnum, rec in sorted(parsed.items()):
        marker = "  ← FILTERED" if class_numbers and cnum in class_numbers else ""
        print(f"    Class {cnum}: open={rec['open']}, total={rec['total']}{marker}")

    print()
    print(f"Opening live URL so you can compare…")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print()

    ans = _prompt(
        f"Do the open/total numbers for {name!r} match the live catalog page? (y/n): "
    ).lower()
    if ans != "y":
        print(f"  Watch {name!r} NOT confirmed. Aborting overall verification.")
        return None

    observed = {c: {"open": rec["open"], "total": rec["total"]} for c, rec in parsed.items()}
    return schema, observed


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    watches = cfg.get("watches") or []
    if not watches:
        print("config.json has no 'watches'. Add at least one watch (url + name).")
        return 1

    print(f"Verifying {len(watches)} watch(es)…\n")
    snapshots: dict = {}
    schema: dict | None = None
    observed_all: dict = {}
    for watch in watches:
        result = _verify_one_watch(watch, cfg, snapshots)
        if result is None:
            print("\nVerification aborted. No flag written.")
            return 1
        watch_schema, observed = result
        if schema is None:
            schema = watch_schema
        elif schema != watch_schema:
            # Different schemas across watches would be weird — surface it.
            print(
                f"  WARNING: schema for {watch['name']!r} differs from prior. "
                f"Using the first watch's schema for all."
            )
        observed_all[watch.get("name") or watch.get("url")] = observed

    if schema is None:
        print("No schema detected. No flag written.")
        return 1

    # Persist debug snapshot of the LAST watch (kept for back-compat).
    last_snapshot = next(reversed(snapshots.values()), None)
    if last_snapshot is not None:
        with SNAPSHOT_PATH.open("w", encoding="utf-8") as f:
            json.dump(last_snapshot, f, indent=2, sort_keys=True)

    flag = {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema": schema,
        "observed": observed_all,
    }
    with FLAG_PATH.open("w", encoding="utf-8") as f:
        json.dump(flag, f, indent=2, sort_keys=True)

    print()
    print("=" * 60)
    print(f"All {len(watches)} watch(es) confirmed.")
    print(f"Verified flag written: {FLAG_PATH}")
    print("The watcher is now allowed to run.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
