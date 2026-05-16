from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, parse_mode: str = "HTML") -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("Telegram credentials missing (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False

    try:
        r = requests.post(
            API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        log.error("Telegram request failed: %s", e)
        return False

    if not r.ok:
        log.error("Telegram send failed: %s %s", r.status_code, r.text)
    return r.ok


def seat_change_alert(watch_name: str, class_nbr: str, old, new, total, url: str) -> bool:
    return send(
        f"🚨 <b>{watch_name} — seat change</b>\n"
        f"Class <b>{class_nbr}</b>: open seats <b>{old} → {new}</b> (of {total})\n"
        f'<a href="{url}">Open registration page</a>'
    )


def low_seats_alert(watch_name: str, class_nbr: str, open_count, total, url: str) -> bool:
    """Persistent reminder used when open count is non-zero but at or below
    the low-seat threshold. Repeats every run while the section stays in
    that range, so a missed first ping doesn't cost the seat."""
    return send(
        f"🟡 <b>{watch_name} — only {open_count} seat{'s' if open_count != 1 else ''} left</b>\n"
        f"Class <b>{class_nbr}</b>: <b>{open_count} of {total}</b> open\n"
        f'<a href="{url}">Open registration page</a>'
    )


def scraper_broken_alert(reason: str, watch_name: str | None = None) -> bool:
    prefix = f"⚠️ <b>ASU Seat Watcher: scraper broken</b>"
    if watch_name:
        prefix += f" — {watch_name}"
    return send(f"{prefix}\n{reason}")


def heartbeat(state_summary: str) -> bool:
    return send(f"✓ ASU Seat Watcher heartbeat\n{state_summary}")
