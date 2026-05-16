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


def seat_change_alert(class_nbr: str, old, new, total, url: str) -> bool:
    return send(
        f"🚨 <b>MAT 243 seat change</b>\n"
        f"Class <b>{class_nbr}</b>: open seats <b>{old} → {new}</b> (of {total})\n"
        f'<a href="{url}">Open registration page</a>'
    )


def seats_open_alert(class_nbr: str, open_count, total, url: str) -> bool:
    """Sent every run while open > 0 (and no change happened this run) so the
    user can't miss it just because they slept through the first ping."""
    return send(
        f"🟢 <b>MAT 243 seats OPEN</b>\n"
        f"Class <b>{class_nbr}</b>: <b>{open_count} of {total}</b> open right now\n"
        f'<a href="{url}">Open registration page</a>'
    )


def scraper_broken_alert(reason: str) -> bool:
    return send(f"⚠️ <b>ASU Seat Watcher: scraper broken</b>\n{reason}")


def heartbeat(state_summary: str) -> bool:
    return send(f"✓ ASU Seat Watcher heartbeat\n{state_summary}")
