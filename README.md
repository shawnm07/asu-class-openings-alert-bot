# ASU Class Openings Alert Bot

A small, reliable Python bot that watches one or more ASU class sections for
open seats and sends you a Telegram alert the moment a seat becomes
available — so you can sprint to the registration page before it's gone.

---

## 1. Purpose

ASU's high-demand classes routinely fill up minutes after registration
opens, and casual F5'ing the catalog page is a losing battle. This bot:

- Watches **any ASU catalog URL you paste in** — any subject, any course,
  any term, any campus or online. Track as many URLs simultaneously as
  you want.
- Polls the ASU catalog **every 15 minutes**, around the clock.
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

1. **You paste a catalog URL** into `config.json` — the exact URL from
   your browser's address bar after running an ASU catalog search. You
   can add as many URLs as you want; each is one "watch."
2. Every 15 minutes, **Playwright launches headless Chromium** and loads
   each watched URL in turn.
3. The SPA fetches its bearer token and fires its XHR to
   `eadvs-cscc-catalog-api.apps.asu.edu/.../search/classes`. The bot
   intercepts that XHR response and reads the JSON.
4. The bot walks the JSON for every class section in the search result
   (or only the specific class numbers you specified, minus anything in
   `exclude_class_numbers`) and computes open seats as
   `seatInfo.ENRL_CAP − seatInfo.ENRL_TOT` — the live counts the catalog
   page itself renders. If a section doesn't carry a `seatInfo` block,
   the bot falls back to `CLAS.ENRLCAP − CLAS.ENRLTOT` (the slightly
   stale PeopleSoft snapshot).
5. It diffs the result against `data/state.json` (the last observed
   counts, kept separately per watch). Any change since the last scan →
   🚨 Telegram alert. If the count didn't change but seats are low
   (0 < open ≤ `low_seat_threshold`, default 5) → 🟡 reminder alert.
   Otherwise silent. Each alert includes the watch's name so you know
   which course pinged. See [§3 Alert logic](#3-what-it-does) for the
   full rules.
6. State is atomically written. On Windows, a single scheduled task runs
   the bot every 15 minutes via Task Scheduler.

A one-time **verification gate** (`scripts/verify.py`) opens a visible
Chromium window, prints the detected seat numbers, and asks you to confirm
they match the live catalog page before the watcher is allowed to run at
all. This is the bot's safety mechanism against ASU silently changing
their API shape.

## 3. What it does

| Trigger | Telegram alert |
|---|---|
| Open-seat count **changed** since the previous scan (any direction — open, close, increased, decreased) | 🚨 *MAT 243 ASU Online — **5 seats opened**. Class 41738: 5 of 80 open (was 0) [Open registration page]*  / 🚨 *MAT 243 ASU Online — **1 seat taken**. Class 41738: 4 of 80 open (was 5) [Open registration page]* |
| No change this scan, but **0 < open ≤ `low_seat_threshold`** (default 5) | 🟡 *MAT 243 ASU Online — only 3 seats left. Class 41738: 3 of 80 open [Open registration page]* |
| No change this scan AND **open > threshold** | (silent — you already got a 🚨 when it changed) |
| No change this scan AND **open == 0** | (silent — you already got a 🚨 when it closed) |
| Scraper produced an uncertain reading (API shape changed, section not found, cap moved, network failure) | ⚠️ *ASU Seat Watcher: scraper broken — \<watch name\> — \<reason\>* |
| Daily heartbeat at a configurable hour | ✓ *ASU Seat Watcher heartbeat — MAT 243 ASU Online: 41738: 0/80 \| 46051: 0/80* |
| Manual test (`watcher.py --force-alert`) | 🧪 test message |

### Alert logic in plain English

The bot tries to ping you exactly when you need to know, and *not* ping
you when nothing has changed:

- **Anything changes:** you always get one 🚨. This covers the moment a
  seat opens (0 → N) and the moment it closes (N → 0), plus every
  in-between movement (e.g., 73 → 74).
- **Seats are low and unchanged:** if open count is at or below
  `low_seat_threshold` (default 5) and didn't change this scan, you get
  a 🟡 reminder every run, so a missed first ping doesn't cost the seat.
- **Seats are plentiful and unchanged:** silent. If 50 seats sat at 50
  for four hours, you don't need 16 identical alerts.
- **Section is stably full at 0:** silent. You already got a 🚨 when it
  closed.

Every Telegram alert includes the watch's `name` so you can tell at a
glance which course pinged you — important once you're watching more
than one URL.

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

The bot accepts a **list of catalog URLs** to monitor. Each entry is one
"watch." Just paste the URL from your browser's address bar after running
the search you want — that's it.

Edit `config.json`:

```json
{
  "api_host_substring": "eadvs-cscc-catalog-api.apps.asu.edu",
  "api_path_substring": "search/classes",
  "schedule_interval_minutes": 15,
  "schedule_start_time": "00:00",
  "heartbeat_hour_local": 9,
  "watches": [
    {
      "name": "MAT 243 ASU Online Session B",
      "url": "https://catalog.apps.asu.edu/catalog/classes/classlist?campusOrOnlineSelection=O&catalogNbr=243&honors=F&promod=F&searchType=all&session=B&subject=MAT&term=2264",
      "class_numbers": [],
      "exclude_class_numbers": []
    },
    {
      "name": "CSE 110 Tempe Fall",
      "url": "https://catalog.apps.asu.edu/catalog/classes/classlist?campus=TEMPE&catalogNbr=110&searchType=all&subject=CSE&term=2267",
      "class_numbers": [],
      "exclude_class_numbers": ["12345", "67890"]
    }
  ]
}
```

**Per-watch fields:**

- `name` — friendly label shown in Telegram alerts. Pick anything readable.
- `url` — the **exact URL** from the catalog page after you've run the
  search you want. Open
  [catalog.apps.asu.edu](https://catalog.apps.asu.edu/catalog/classes/classsearch),
  search for the course, copy the URL from the address bar, paste it here.
- `class_numbers` — optional. Leave `[]` to alert on **any section** the
  search returns. Add specific 5-digit class numbers (as strings) to
  narrow to just those sections (useful if a search returns sections you
  don't actually want — wrong instructor, bad meeting time, etc.).
- `exclude_class_numbers` — optional blocklist. 5-digit class numbers
  listed here are **ignored** by the watcher — no alerts, no state diff,
  not even shown during verification. Use this when a search returns a
  section you specifically don't want (e.g., the one with the bad
  professor). Takes effect immediately after re-running verify.

**Global fields:**

- `schedule_interval_minutes` — how often the bot polls. 15 is sensible;
  5 is the lowest I'd recommend (catalog API may rate-limit at more
  aggressive intervals).
- `schedule_start_time` — clock-time the repeating trigger anchors to.
- `heartbeat_hour_local` — hour (0-23) at which the daily ✓ heartbeat
  fires.
- `low_seat_threshold` — *optional, defaults to 5*. When a section's open
  count is at or below this number (but greater than 0), the bot sends a
  🟡 reminder every run. Above this number, no reminder — only change
  alerts. Set to 0 to disable the reminder entirely. Set higher (e.g.,
  10) if you want louder coverage of low-but-not-imminent openings.

After editing `config.json`, **always re-run `python scripts\verify.py`**
so the bot can confirm it knows how to read each watch's response.

> **Adding a watch later** is the same flow: edit `config.json`, append
> a new entry to `watches`, re-run `verify.py`. No need to restart the
> scheduled task — the watcher script reads `config.json` fresh each run.

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
minutes for the next 10 years. **It runs silently** — the task launches
`pythonw.exe` (Python's no-console build), so no terminal window pops up
while you're working. Output goes to `logs\watcher.log` instead.

Verify the actual repetition config (the `NextRunTime` field in
`Get-ScheduledTaskInfo` displays imprecisely and is not the source of
truth):

```powershell
(Get-ScheduledTask -TaskName 'ASUSeatWatcher').Triggers[0].Repetition
```

You should see `Interval : PT15M` and `Duration : P3650D`.

To remove the schedule later:

```powershell
.\scripts\unregister_schedule.ps1
```

### Changing or adding watches later

Edit `config.json -> watches` (append a new entry, edit a URL, narrow a
section list, whatever you need), then **re-run `python scripts\verify.py`**
so the bot confirms each watch's response is still readable. The scheduled
task itself doesn't need to be re-registered — it re-reads `config.json`
every run.

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
