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
- **CSV export** delivered as a downloadable Telegram document.
- **Admin web dashboard** — an optional browser portal with summary stats, a
  recent-activity chart, a filterable attendance table, and CSV export.

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
| `/register` (or `/start`) | Everyone | Register and set your coordinator. |
| `/setcoordinator <name>` | Member | Set or change your coordinator. |
| `/clockin` | Member | Clock in; choose **Remote** or **On_Site**. |
| `/clockout` | Member | Close today's open clock-in. |
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
   (100 m by default) → the entry is created and your coordinates are stored;
   otherwise it's rejected.

An Admin must run `/setlocation` first, or on-site clock-in is refused.

## Admin web dashboard (optional)

Set `ADMIN_PORTAL_PASSWORD` and the app also serves a browser dashboard on
`PORT` (8080). Open the app URL (on Fly.io: `https://dbp-attendance.fly.dev`),
log in with that password, and you get:

- **Dashboard** — summary cards (members, admins, clock-ins today, currently
  clocked in, on-site vs remote) and a 14-day activity chart.
- **Attendance** — a filterable table (by member and date range) with one-click
  CSV export.
- **Members** — everyone's role, coordinator, and registration date, plus a form
  to **add a member** (by Telegram ID, name, role, coordinator).
- **Settings** — **configure the on-site location** (latitude/longitude) used for
  On_Site geofence validation.

The dashboard is protected by a signed `HttpOnly` session cookie,
and shares the bot's database (SQLite in WAL mode for safe concurrent reads).
If `ADMIN_PORTAL_PASSWORD` is empty, only a health endpoint is served.

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
  timeutil.py       # timezone-offset time helpers
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
