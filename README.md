# ASU Class Openings Alert Bot

A small, reliable Python bot that watches one or more ASU class sections for
open seats and sends you a Telegram alert the moment a seat becomes
available — so you can sprint to the registration page before it's gone.

---

## 1. Purpose

ASU's high-demand classes routinely fill up minutes after registration
opens, and casual F5'ing the catalog page is a losing battle. This bot:

- Polls the ASU catalog **every 15 minutes**, around the clock.
- Tracks the open-seat count for any class numbers you specify.
- Pings you on **Telegram** the instant a seat opens, with a one-click link
  back to the registration page.
- Re-pings you every 15 minutes while seats remain open, so a missed first
  notification doesn't cost you the seat.
- Refuses to send wrong numbers — if anything about ASU's API changes
  unexpectedly, the bot loudly alerts that the scraper is broken instead of
  silently reporting "0 open seats."

It does **not** auto-register. Registration requires your ASU login + Duo
2FA, and automating that would violate ASU's terms. The bot's job is to get
you to the page fast; clicking *Enroll* is still your move.

## 2. How it works

The ASU catalog page is a single-page JavaScript app. A plain HTTP `GET` to
the page returns an empty HTML shell — the class data only appears after
the browser executes the SPA, which then calls a microservice with a
short-lived bearer token. Hitting the microservice directly returns
**401 Unauthorized**.

So the bot uses a **real browser** under the hood:

1. **Playwright launches headless Chromium** and loads the catalog URL with
   your search parameters (subject, catalog number, term, session, online
   vs in-person, etc.).
2. The SPA fetches its bearer token and fires its XHR to
   `eadvs-cscc-catalog-api.apps.asu.edu/.../search/classes`. The bot
   intercepts that XHR response and reads the JSON.
3. The bot walks the JSON for records matching your watched class numbers
   and computes open seats as `ENRLCAP − ENRLTOT` (ASU's PeopleSoft
   convention: capacity minus currently enrolled).
4. It diffs the result against `data/state.json` (the last observed
   counts). Any change → 🚨 Telegram alert. Any non-zero open count →
   🟢 reminder alert (every run, until seats are gone).
5. State is atomically written. On Windows, a single scheduled task runs
   the bot every 15 minutes via Task Scheduler.

A one-time **verification gate** (`scripts/verify.py`) opens a visible
Chromium window, prints the detected seat numbers, and asks you to confirm
they match the live catalog page before the watcher is allowed to run at
all. This is the bot's safety mechanism against ASU silently changing
their API shape.

## 3. What it does

| Trigger | Telegram alert |
|---|---|
| Open-seat count changed for a watched class | 🚨 *MAT 243 seat change — Class 41738: open seats 0 → 5 (of 80) [Open registration page]* |
| Open seats > 0 (sent every run, in addition to a change alert) | 🟢 *MAT 243 seats OPEN — Class 41738: 5 of 80 open right now [Open registration page]* |
| Scraper produced an uncertain reading (API shape changed, class not found, cap moved, network failure) | ⚠️ *ASU Seat Watcher: scraper broken — \<reason\>* |
| Daily heartbeat at a configurable hour | ✓ *ASU Seat Watcher heartbeat — 41738: 0/80 \| 46051: 0/80* |
| Manual test (`watcher.py --force-alert`) | 🧪 test message |

Everything is logged to `logs/watcher.log` (rotating, 5 MB × 3 backups).

## 4. Implementation / setup

### Prerequisites

- Windows 10 / 11 (the scheduling scripts are PowerShell; the Python code
  itself is cross-platform)
- Python 3.11+ (`python --version`)
- A Telegram bot token + your chat ID (instructions below)

### Step 1 — Clone and install

```powershell
git clone https://github.com/<your-username>/asu-class-openings-alert-bot.git
cd asu-class-openings-alert-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

### Step 2 — Create your own Telegram bot

> **Why your own?** The bot's Telegram token gives full control over the
> bot account. `.env` is in `.gitignore` and never committed, so the public
> repo never contains anyone's token. You need to create one for your own
> deployment.

1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot` and follow the prompts. BotFather will print a token
   like `1234567890:AAH...`. **Copy it.**
3. Open a chat with your new bot and send it any message (so the bot has
   a chat to reply in).
4. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a
   browser. Find `"chat":{"id":<NUMBER>,...}` in the JSON — that number
   is your `TELEGRAM_CHAT_ID`.
5. Copy `.env.example` to `.env` and fill in both values:

```powershell
Copy-Item .env.example .env
notepad .env
```

Your `.env` should look like:

```
TELEGRAM_BOT_TOKEN=1234567890:AAH...
TELEGRAM_CHAT_ID=987654321
```

Quick test that Telegram is wired up:

```powershell
python -c "from dotenv import load_dotenv; load_dotenv(); from src import notifier; print(notifier.send('test from ASU watcher'))"
```

You should see `True` printed and receive a Telegram message.

### Step 3 — Configure which classes to watch

Edit `config.json`:

```json
{
  "query": {
    "campusOrOnlineSelection": "O",
    "catalogNbr": "243",
    "honors": "F",
    "promod": "F",
    "searchType": "all",
    "session": "B",
    "subject": "MAT",
    "term": "2264"
  },
  "watch_class_numbers": ["41738", "46051"],
  "expected_total_seats": 80,
  "schedule_interval_minutes": 15,
  "schedule_start_time": "00:00",
  "heartbeat_hour_local": 9
}
```

- `query` — same parameters the ASU catalog URL takes. You can copy them
  straight out of the catalog page URL after running a search.
- `watch_class_numbers` — list of ASU class numbers as strings (the
  5-digit IDs shown on the catalog page next to each section).
- `expected_total_seats` — must match the cap shown on the catalog page.
  If ASU changes the cap, you'll get a `⚠️ scraper broken` alert until
  you update this value.
- `schedule_interval_minutes` — how often the bot polls. 15 is a sensible
  default; 5 is the lowest I'd recommend (you can be rate-limited at
  more aggressive intervals).

### Step 4 — Verify correctness (MANDATORY)

This is the safety gate. It opens a visible Chromium window, captures the
catalog API response, prints the detected open-seat / total numbers, and
asks you to confirm them against the live catalog page. **The watcher
refuses to run until you have done this once.**

```powershell
python scripts\verify.py
```

Compare the printed values against the catalog page that opens in your
browser. If they match, type `y`. A file `data\verified.flag` is written.

If they don't match, type `n`; the script lets you identify the correct
JSON field names by hand (subtract vs direct mode, plus the field names
to use). Only write a flag if you're sure.

If ASU later changes their API field names, re-run this script.

### Step 5 — Dry run

```powershell
python src\watcher.py --dry-run
```

You should see lines like `first observation: 41738 open=0 total=80` in
the console and in `logs\watcher.log`. No Telegram message is sent.
`data\state.json` is written.

### Step 6 — Test the alert path

Force-edit state to fake a previous reading and watch for the change alert:

```powershell
notepad data\state.json
# change "41738" -> "open" to 5, save

python src\watcher.py
# Telegram should fire: 🚨 41738: open seats 5 → 0
```

Or, send a one-off test:

```powershell
python src\watcher.py --force-alert
```

### Step 7 — Test the broken-scraper alert path

Edit `config.json` and replace one watched class number with a fake (e.g.
`"00000"`), run `python src\watcher.py`. You should get
`⚠️ scraper broken: class 00000 missing from response`. Restore the real
config.

### Step 8 — Schedule (Windows Task Scheduler, 15-minute repeat)

Open PowerShell **as Administrator**, navigate to the project folder, and:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\scripts\setup_schedule.ps1
```

That registers a single task named `ASUSeatWatcher` that fires every 15
minutes for the next 10 years. Verify the actual repetition config (the
`NextRunTime` field in `Get-ScheduledTaskInfo` displays imprecisely and is
not the source of truth):

```powershell
(Get-ScheduledTask -TaskName 'ASUSeatWatcher').Triggers[0].Repetition
```

You should see `Interval : PT15M` and `Duration : P3650D`.

To remove the schedule later:

```powershell
.\scripts\unregister_schedule.ps1
```

### Changing the watched classes later

Edit `config.json -> watch_class_numbers` and `expected_total_seats`, then
**re-run `python scripts\verify.py`** (the flag's `observed` block should
reflect your new classes) before the next scheduled run. The scheduled task
itself doesn't need to be re-registered.

### Changing the polling interval

Edit `config.json -> schedule_interval_minutes`, then in an Administrator
PowerShell:

```powershell
.\scripts\unregister_schedule.ps1
.\scripts\setup_schedule.ps1
```

## 5. Troubleshooting

**Logs** live in `logs\watcher.log` (rotating, 5 MB × 3 backups).

**The watcher refuses to start: "data/verified.flag is missing".**
Run `python scripts\verify.py`. The flag is intentional — it stops the bot
from sending wrong numbers if the API shape ever changes silently.

**`⚠️ scraper broken` alerts every run.**
ASU likely changed their JSON field names or the seat cap. Re-run
`python scripts\verify.py` to discover and confirm the new shape. The
flag is overwritten.

**No alerts arriving but the task is running.**
Check `logs\watcher.log` for errors. Make sure `.env` has the right values
and that you sent at least one message *to* your bot in Telegram (the bot
can't initiate a chat).

**`ModuleNotFoundError: No module named 'playwright'`.**
You're running outside the venv. Activate it (`.\.venv\Scripts\Activate.ps1`)
or call `.\.venv\Scripts\python.exe` directly.

**`Register-ScheduledTask` failed.**
Run PowerShell as Administrator. `Set-ExecutionPolicy -Scope Process Bypass -Force`
if you also hit the script-execution policy block.

**Laptop is asleep when the task should run.**
The task is registered with `StartWhenAvailable`, so a missed run fires on
next wake. That said, if your machine is closed for hours, you'll miss seat
changes during that window.

## 6. Non-goals / out of scope

- **No auto-registration.** Bot alerts; you register.
- **No web dashboard.** Telegram and log files are the entire UI.
- **No database.** A small JSON file is plenty for a handful of class
  records.
- **No multi-user support.** One user, one Telegram chat. Two users could
  trivially run two separate clones, each with their own `.env`.
- **No silent DOM fallback.** If JSON capture fails, the bot fires a loud
  ⚠️ alert rather than scraping the rendered page — DOM scraping degrades
  silently into wrong numbers, and "loud failure" is safer than
  "quiet wrong answers."

## 7. Stack

- Python 3.11+
- [Playwright](https://playwright.dev/python/) (headless Chromium) for
  catching the catalog SPA's XHR with its bearer token attached
- Telegram Bot HTTP API (no SDK; one `requests.post` call)
- Windows Task Scheduler for the 15-minute cron

No paid services. No database. No deployment — it runs from your machine.
