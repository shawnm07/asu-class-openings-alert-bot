"""Main orchestration for the ASU seat watcher.

Iterates every entry in config["watches"], polls each catalog URL, diffs
the open-seat count for every class section against the last observed
state, and fires Telegram alerts on change. Also re-pings every run while
any class has open > 0. Refuses to run unless data/verified.flag exists
(written by scripts/verify.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _process_watch(
    watch: dict,
    schema: dict,
    cfg: dict,
    state: dict,
    log: logging.Logger,
    dry_run: bool,
    force_alert: bool,
) -> tuple[int, list[str]]:
    """Run one watch end-to-end. Returns (return_code, summary_lines).
    return_code != 0 means a scraper_broken_alert was already fired for this
    watch — caller should record it but keep processing other watches."""
    name = watch.get("name") or watch.get("url") or "<unnamed watch>"
    url = watch.get("url")
    if not url:
        msg = f"watch {name!r} has no 'url'"
        log.error(msg)
        if not dry_run:
            notifier.scraper_broken_alert(msg, watch_name=name)
        return 2, []
    class_numbers = [str(c) for c in (watch.get("class_numbers") or [])]

    try:
        api_json = scraper.fetch_url(url, cfg, watch_class_numbers=class_numbers)
        parsed = scraper.extract_seats(api_json, class_numbers, field_overrides=schema)
    except scraper.ScraperError as e:
        log.exception("ScraperError on watch %s", name)
        if not dry_run:
            notifier.scraper_broken_alert(str(e), watch_name=name)
        return 2, []
    except Exception as e:
        log.exception("Unexpected scraper failure on watch %s", name)
        if not dry_run:
            notifier.scraper_broken_alert(f"unexpected: {e}", watch_name=name)
        return 2, []

    if not parsed:
        msg = f"watch {name!r}: no class records returned"
        log.error(msg)
        if not dry_run:
            notifier.scraper_broken_alert(msg, watch_name=name)
        return 2, []

    # Sanity: open in [0, total]; total positive.
    for c, rec in parsed.items():
        if not (isinstance(rec["open"], int) and isinstance(rec["total"], int)):
            reason = f"class {c}: non-int open/total"
            log.error(reason)
            if not dry_run:
                notifier.scraper_broken_alert(reason, watch_name=name)
            return 2, []
        if rec["total"] <= 0 or not (0 <= rec["open"] <= rec["total"]):
            reason = f"class {c}: open={rec['open']} not in [0, {rec['total']}]"
            log.error(reason)
            if not dry_run:
                notifier.scraper_broken_alert(reason, watch_name=name)
            return 2, []

    watch_state = state["watches"].setdefault(name, {"classes": {}})
    summary: list[str] = []
    for c, rec in parsed.items():
        curr = rec["open"]
        total = rec["total"]
        prev_entry = watch_state["classes"].get(c, {})
        prev = prev_entry.get("open") if isinstance(prev_entry, dict) else None
        prev_total = prev_entry.get("total") if isinstance(prev_entry, dict) else None

        if prev_total is not None and prev_total != total:
            reason = (
                f"watch {name!r} class {c}: total changed {prev_total} -> {total} "
                "(ASU may have moved the cap)"
            )
            log.warning(reason)
            if not dry_run:
                notifier.scraper_broken_alert(reason, watch_name=name)

        sent_change = False
        if prev is None:
            log.info("[%s] first observation: %s open=%s total=%s", name, c, curr, total)
        elif prev != curr:
            log.info(
                "[%s] CHANGE: class %s open %s -> %s (total=%s)",
                name, c, prev, curr, total,
            )
            if not dry_run:
                notifier.seat_change_alert(name, c, prev, curr, total, url)
            else:
                log.info("[dry-run] would send seat_change_alert")
            sent_change = True
        else:
            log.info("[%s] no change: class %s open=%s total=%s", name, c, curr, total)

        if curr > 0 and not sent_change:
            if not dry_run:
                notifier.seats_open_alert(name, c, curr, total, url)
            else:
                log.info("[dry-run] would send seats_open_alert(%s, %s/%s)", c, curr, total)

        watch_state["classes"][c] = {
            "open": curr,
            "total": total,
            "last_seen": _now_iso(),
            "fields": rec["fields"],
        }
        summary.append(f"{c}: {curr}/{total}")

    if force_alert:
        first_c, first_rec = next(iter(parsed.items()))
        if not dry_run:
            notifier.send(
                f"🧪 <b>Test alert ({name})</b> — watcher is alive.\n"
                f"Current: class {first_c} open={first_rec['open']} of {first_rec['total']}"
            )

    return 0, summary


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

    schema = flag.get("schema")
    if not schema or not schema.get("mode"):
        msg = (
            "data/verified.flag has no 'schema' block. Re-run "
            "`python scripts/verify.py` to regenerate it."
        )
        log.error(msg)
        print(msg, file=sys.stderr)
        return 1

    watches = cfg.get("watches") or []
    if not watches:
        msg = "config.json has no 'watches' entries. Add at least one watch (url + name)."
        log.error(msg)
        return 1

    state = _load_json(STATE_PATH, default={}) or {}
    if "watches" not in state:
        state = {"watches": {}, "last_heartbeat_date": state.get("last_heartbeat_date")}

    overall_rc = 0
    all_summary: list[str] = []
    for watch in watches:
        rc, summary = _process_watch(
            watch, schema, cfg, state, log,
            dry_run=dry_run, force_alert=force_alert,
        )
        if rc != 0:
            overall_rc = rc
        if summary:
            all_summary.append(f"{watch.get('name', '<?>')}: " + " | ".join(summary))

    # Heartbeat once per day at heartbeat_hour_local.
    try:
        now_local = datetime.now().astimezone()
        today = now_local.date().isoformat()
        hb_hour = int(cfg.get("heartbeat_hour_local", 9))
        last_hb = state.get("last_heartbeat_date")
        if now_local.hour == hb_hour and last_hb != today and all_summary:
            text = "\n".join(all_summary)
            if not dry_run:
                notifier.heartbeat(text)
            else:
                log.info("[dry-run] would send heartbeat:\n%s", text)
            state["last_heartbeat_date"] = today
    except Exception:
        log.exception("Heartbeat block failed (non-fatal)")

    _atomic_write_json(STATE_PATH, state)
    log.info("Run complete. state.json updated.")
    return overall_rc


def main():
    parser = argparse.ArgumentParser(description="ASU seat watcher")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages")
    parser.add_argument("--force-alert", action="store_true", help="Send a test alert this run")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run, force_alert=args.force_alert))


if __name__ == "__main__":
    main()
