# Telegram Attendance Tracker

A Telegram bot that lets a team record daily attendance with **clock-in** and
**clock-out** actions directly in Telegram. Clock-in supports two modes —
**Remote** and **On_Site** — where On_Site is only allowed within a configurable
radius (**100 meters** by default) of an admin-configured location. Access is
role-based: **Regular Users** see only their own records, while **Admins** see
everyone's. Members and Admins can export attendance reports as CSV files, and
Admins get an optional **web dashboard** in the browser.

## Highlights

- **Zero third-party dependencies.** Runs on the Python 3.10+ standard library
  only (`urllib`, `sqlite3`, `csv`, `json`, `math`, `datetime`, `http.server`).
  No `pip install` step is required.
- **SQLite data store** — a single file, created automatically.
- **Geofenced on-site clock-in** using the haversine distance formula.
- **Role-based access control** with an easy first-admin bootstrap.
- **Guided registration** — new members set their real name, then **select**
  their coordinator, unit/department and base location from admin-managed lists
  (with a typed fallback), so no free-typing is needed.
- **Auto late-detection** — clock-ins after the work start time (plus a
  configurable grace period) are flagged and the member is prompted for a reason.
- **Daily reminders + auto clock-out** — morning "clock in" and evening "clock
  out" nudges, and anyone still clocked in is auto-closed at a configurable time
  (default 23:59).
- **Multiple sites** — configure several on-site locations; members are
  validated against their assigned base site.
- **CSV + Excel export** and a modern, **mobile-responsive admin web dashboard**
  with **light/dark themes**, analytics and a map.

## Getting started

### 1. Create a bot and get a token

Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`, and
copy the token it gives you.

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and set at least:

| Variable | Purpose |
| --- | --- |
| `BOT_TOKEN` | **Required.** Token from @BotFather. |
| `DB_PATH` | SQLite file path (default `data/attendance.db`). |
| `ADMIN_TELEGRAM_IDS` | Comma-separated Telegram user IDs auto-granted Admin on registration. Use this to create the first Admin. Find your ID via [@userinfobot](https://t.me/userinfobot). |
| `TZ_OFFSET_HOURS` | UTC offset for timestamps and "today" logic. Default `7` = Cambodia / Indochina Time (ICT). Other examples: `0` UTC, `-5` US Eastern. |
| `GEOFENCE_RADIUS_METERS` | On-site radius in meters. Default `100`. |
| `POLL_TIMEOUT_SECONDS` | Long-polling timeout for `getUpdates`. |
| `ADMIN_PORTAL_PASSWORD` | Optional. Set a password to enable the admin web dashboard. Empty = dashboard off. |
| `PORTAL_SECRET` | Optional. Signs dashboard login cookies (defaults to `BOT_TOKEN`). |
| `PORT` | Web/health server port (default `8080`). |

> **Bootstrapping the first Admin:** the `/promote` command requires Admin
> access, so at least one Admin must be seeded via `ADMIN_TELEGRAM_IDS`. Put
> your own Telegram ID there, then `/register` — you will be created as an
> Admin and can promote others.

### 3. Run

```bash
python -m attendance_bot
# or
python main.py
```

The bot uses long polling, so no public URL or webhook is needed.

## Commands

| Command | Who | Description |
| --- | --- | --- |
| `/register` (or `/start`) | Everyone | Guided setup: real name, coordinator, unit, base location. |
| `/clockin` | Member | Clock in; choose **Remote** or **On_Site**. |
| `/clockout` | Member | Close today's open clock-in. |
| `/status` | Member | See if you're currently clocked in and for how long. |
| `/summary [week\|month]` | Member | Your hours, days present, and late count. |
| `/setname <name>` | Member | Update your real full name. |
| `/setcoordinator <name>` | Member | Set or change your coordinator. |
| `/setunit <unit>` | Member | Set your unit/department. |
| `/setbase` | Member | Choose your base location (from configured sites). |
| `/remark <YYYY-MM-DD> <text>` | Member | Add/replace a late remark for a date. |
| `/view [member <name>] [from <date>] [to <date>]` | Member | View attendance (own for Regular Users; anyone/all for Admins). |
| `/export [member <name>] [from <date>] [to <date>]` | Member | Download a CSV report. |
| `/whoami` | Member | Show your registration details. |
| `/setlocation <lat> <lon>` | Admin | Set the on-site reference location. |
| `/promote <name\|telegram_id>` | Admin | Grant Admin to another member. |
| `/help` | Everyone | Show the command list. |

### On-site clock-in flow

1. `/clockin` → tap **On_Site**.
2. The bot asks you to share your location (tap the location button).
3. It computes the distance to the configured location. Within the radius
   (100 m by default) → the entry is created, your **coordinates are stored and
   shown back to you with a map link**; otherwise it's rejected.

An Admin must run `/setlocation` first, or on-site clock-in is refused.

## Admin web dashboard (optional)

Set `ADMIN_PORTAL_PASSWORD` and the app also serves a browser dashboard on
`PORT` (8080). Open the app URL (on Fly.io: `https://dbp-attendance.fly.dev`),
log in with that password, and you get:

- **Dashboard** — summary cards (members, admins, clock-ins today, currently
  clocked in, on-site vs remote) and a 14-day activity chart.
- **Attendance** — a filterable table (by member and date range) with one-click
  CSV export.
- **Attendance** — quick filters (Today / Last 7 days / This week / This month)
  plus custom member & date-range; a **Location** column with a map link for
  On_Site clock-ins; **edit or delete** entries — admins can adjust the
  **arrival/clock-in time, out time, on-time/late status, and GPS coordinates**;
  export to **CSV or Excel (.xlsx)**.
- **Members** — add, **edit, and delete** members (unit/department, base site,
  role, coordinator) and **bulk-import** many members by **uploading an Excel
  (.xlsx) or CSV file** (or pasting rows); a template is downloadable.
- **Analytics** — per-member hours worked, days present, late count, on-site vs
  remote, and attendance rate over a chosen date range.
- **Map** — plots configured sites and on-site clock-in points (Leaflet); click
  the map to grab coordinates and add a new site.
- **Settings** — the default on-site location, the **geofence radius**,
  **multiple named sites**, master lists of **units/departments** and
  **coordinators** (which members select during registration), the **work
  schedule** (start/end times, working days, **late grace period**, reminders
  on/off, **auto clock-out time**), a **"recalculate late flags"** action, and
  an **audit log** of admin changes.

The dashboard is protected by a signed `HttpOnly` session cookie, and shares the
bot's database (SQLite in WAL mode for safe concurrent reads). All admin write
actions are recorded in the audit log. If `ADMIN_PORTAL_PASSWORD` is empty, only
a health endpoint is served.

> The **Map** page loads Leaflet and OpenStreetMap tiles from a CDN, so it needs
> internet access in your browser (the rest of the dashboard works offline).

## Project layout

```
attendance_bot/
  __init__.py
  __main__.py       # `python -m attendance_bot`
  app.py            # config wiring + long-polling loop
  config.py         # .env / environment configuration
  db.py             # SQLite data store (members, attendance, config)
  geo.py            # haversine distance / geofence check
  handlers.py       # command + conversation state machine (the bot logic)
  reports.py        # CSV export and text rendering
  telegram_api.py   # minimal stdlib Telegram Bot API client
  timeutil.py       # timezone-offset time helpers, work-day math
  scheduler.py      # daily clock-in / clock-out reminder thread
  xlsx.py           # minimal stdlib .xlsx (Excel) writer
  web.py            # admin web dashboard (stdlib http.server)
  health.py         # minimal health endpoint (when dashboard is off)
main.py             # convenience launcher
tests/              # pytest suite covering every acceptance criterion
```

## Data model

- **members** — `telegram_id` (PK), `name`, `role` (`regular`/`admin`),
  `coordinator`, `created_at`.
- **attendance** — `id`, `telegram_id`, `date`, `clock_in_time`,
  `clock_out_time`, `clock_in_type` (`Remote`/`On_Site`), `coordinator`,
  `latitude`, `longitude`, `late_remark`.
- **config** — key/value store holding the configured on-site location.

## Running the tests

The suite uses `pytest` and needs no network (a fake Telegram client records
outbound calls). Every one of the 10 requirements' acceptance criteria is
covered.

```bash
pip install pytest      # only needed to run tests; the bot itself has no deps
python -m pytest -q
```

## Notes

- Timestamps are stored as `YYYY-MM-DD HH:MM:SS` in the configured local
  offset; dates as `YYYY-MM-DD`.
- The database file and any exports are git-ignored.
