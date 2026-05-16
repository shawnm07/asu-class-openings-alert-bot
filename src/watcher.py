"""Main orchestration for the ASU seat watcher.

Loads config + state, runs the scraper, diffs open-seat counts against the
last observed state, and fires Telegram alerts on any change. Refuses to run
unless data/verified.flag exists (written by scripts/verify.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

# Make repo root importable when this script is launched directly.
_THIS = Path(__file__).resolve()
_PROJECT_ROOT = _THIS.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from src import notifier, scraper  # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402

CONFIG_PATH = _PROJECT_ROOT / "config.json"
ENV_PATH = _PROJECT_ROOT / ".env"
DATA_DIR = _PROJECT_ROOT / "data"
STATE_PATH = DATA_DIR / "state.json"
VERIFIED_FLAG = DATA_DIR / "verified.flag"


def _load_json(path: Path, default=None):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _build_live_url(cfg: dict) -> str:
    return cfg["catalog_url"] + "?" + urlencode(cfg["query"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(dry_run: bool = False, force_alert: bool = False) -> int:
    log = setup_logging("watcher")
    load_dotenv(ENV_PATH)

    cfg = _load_json(CONFIG_PATH)
    if cfg is None:
        log.error("config.json not found at %s", CONFIG_PATH)
        return 1

    flag = _load_json(VERIFIED_FLAG)
    if flag is None:
        msg = (
            "data/verified.flag is missing. The watcher refuses to run until "
            "you have completed `python scripts/verify.py` and confirmed the "
            "detected seat numbers against the live catalog page."
        )
        log.error(msg)
        print(msg, file=sys.stderr)
        return 1

    live_url = _build_live_url(cfg)
    schema = flag.get("schema")
    if not schema or not schema.get("mode"):
        msg = (
            "data/verified.flag has no 'schema' block. Re-run "
            "`python scripts/verify.py` to regenerate it."
        )
        log.error(msg)
        print(msg, file=sys.stderr)
        return 1

    try:
        api_json = scraper.fetch_classes(cfg)
        parsed = scraper.extract_seats(
            api_json,
            cfg["watch_class_numbers"],
            cfg["expected_total_seats"],
            field_overrides=schema,
        )
    except scraper.ScraperError as e:
        log.exception("Scraper error")
        if not dry_run:
            notifier.scraper_broken_alert(str(e))
        return 2
    except Exception as e:
        log.exception("Unexpected scraper failure")
        if not dry_run:
            notifier.scraper_broken_alert(f"unexpected: {e}")
        return 2

    # Sanity gates
    for c in cfg["watch_class_numbers"]:
        c = str(c)
        rec = parsed.get(c)
        if rec is None:
            reason = f"class {c} missing from parsed result"
            log.error(reason)
            if not dry_run:
                notifier.scraper_broken_alert(reason)
            return 2
        if rec["total"] != cfg["expected_total_seats"]:
            reason = (
                f"class {c}: total seats {rec['total']} != expected "
                f"{cfg['expected_total_seats']} — ASU may have changed the cap"
            )
            log.error(reason)
            if not dry_run:
                notifier.scraper_broken_alert(reason)
            return 2
        if not isinstance(rec["open"], int) or not (0 <= rec["open"] <= rec["total"]):
            reason = (
                f"class {c}: open seats {rec['open']} not in [0, {rec['total']}]"
            )
            log.error(reason)
            if not dry_run:
                notifier.scraper_broken_alert(reason)
            return 2

    # Diff vs state
    state = _load_json(STATE_PATH, default={}) or {}
    if "classes" not in state:
        state = {"classes": {}, "last_heartbeat_date": None}

    changes: list[str] = []
    for c in cfg["watch_class_numbers"]:
        c = str(c)
        curr = parsed[c]["open"]
        total = parsed[c]["total"]
        prev_entry = state["classes"].get(c, {})
        prev = prev_entry.get("open") if isinstance(prev_entry, dict) else None

        sent_change = False
        if prev is None:
            log.info("first observation: %s open=%s total=%s", c, curr, total)
        elif prev != curr:
            log.info(
                "CHANGE: class %s open %s -> %s (total=%s)",
                c,
                prev,
                curr,
                total,
            )
            changes.append(c)
            if not dry_run:
                notifier.seat_change_alert(c, prev, curr, total, live_url)
            else:
                log.info("[dry-run] would send seat_change_alert(%s, %s, %s)", c, prev, curr)
            sent_change = True
        else:
            log.info("no change: class %s open=%s total=%s", c, curr, total)

        # Persistent reminder: every run while seats are open (and we did NOT
        # already send a change alert this run), re-ping the user. Prevents
        # a missed first alert from causing a missed registration.
        if curr > 0 and not sent_change:
            if not dry_run:
                notifier.seats_open_alert(c, curr, total, live_url)
            else:
                log.info("[dry-run] would send seats_open_alert(%s, %s/%s)", c, curr, total)

        state["classes"][c] = {
            "open": curr,
            "total": total,
            "last_seen": _now_iso(),
            "fields": parsed[c]["fields"],
        }

    if force_alert and not changes:
        log.info("force_alert=True: sending a test alert")
        sample = cfg["watch_class_numbers"][0]
        rec = parsed[str(sample)]
        if not dry_run:
            notifier.send(
                f"🧪 <b>Test alert</b> — watcher is alive.\n"
                f"Current: class {sample} open={rec['open']} of {rec['total']}"
            )

    # Heartbeat: once per day at the configured local hour.
    try:
        now_local = datetime.now().astimezone()
        today = now_local.date().isoformat()
        hb_hour = int(cfg.get("heartbeat_hour_local", 9))
        last_hb = state.get("last_heartbeat_date")
        if now_local.hour == hb_hour and last_hb != today:
            summary_parts = []
            for c in cfg["watch_class_numbers"]:
                c = str(c)
                summary_parts.append(
                    f"{c}: {parsed[c]['open']}/{parsed[c]['total']}"
                )
            summary = " | ".join(summary_parts)
            if not dry_run:
                notifier.heartbeat(summary)
            else:
                log.info("[dry-run] would send heartbeat: %s", summary)
            state["last_heartbeat_date"] = today
    except Exception:
        log.exception("Heartbeat block failed (non-fatal)")

    _atomic_write_json(STATE_PATH, state)
    log.info("Run complete. state.json updated.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="ASU seat watcher")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages")
    parser.add_argument("--force-alert", action="store_true", help="Send a test alert this run")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run, force_alert=args.force_alert))


if __name__ == "__main__":
    main()
