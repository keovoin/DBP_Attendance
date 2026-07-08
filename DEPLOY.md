# Deploying the Attendance Bot to Fly.io

This bot runs as an always-on **long-polling worker** (no public URL, no
webhook). The SQLite database is kept on a **persistent Fly volume** so your
attendance history survives deploys and restarts.

Everything the bot needs is in this repo:

- `Dockerfile` - zero-dependency Python image
- `fly.toml` - app config with the volume mount and non-secret env vars
- `.dockerignore` - keeps secrets and local files out of the image

## Prerequisites

1. A Telegram bot token from [@BotFather](https://t.me/BotFather).
2. Your numeric Telegram ID from [@userinfobot](https://t.me/userinfobot)
   (used to bootstrap the first Admin).
3. The Fly CLI (`flyctl`) installed and signed in:

   ```bash
   # macOS / Linux
   curl -L https://fly.io/install.sh | sh
   # Windows (PowerShell)
   #   pwsh -c "iwr https://fly.io/install.sh -useb | iex"

   fly auth signup   # or: fly auth login
   ```

## One-time setup

From the repository root (on the `feat/attendance-tracker-bot` branch):

```bash
# 1. Register the app WITHOUT deploying yet. This reuses the committed fly.toml.
#    If the name "dbp-attendance-bot" is taken, edit `app = ...` in fly.toml
#    (or let this command assign a unique name).
fly launch --no-deploy --copy-config --name dbp-attendance-bot --region sin

# 2. Create the persistent volume for the database (1 GB is plenty).
#    Use the SAME region as primary_region in fly.toml.
fly volumes create attendance_data --region sin --size 1

# 3. Set your secrets (these are NEVER stored in git).
fly secrets set \
  BOT_TOKEN="123456789:AAE-your-real-token" \
  ADMIN_TELEGRAM_IDS="<your-numeric-telegram-id>"

# 4. Deploy.
fly deploy
```

## Verify it's running

```bash
fly status          # should show one machine in the "started" state
fly logs            # look for "Starting bot @yourbot" and "Polling for updates."
```

Now open Telegram, find your bot, and send `/register`. Because your ID is in
`ADMIN_TELEGRAM_IDS`, you'll be created as an **Admin** and can run
`/setlocation` and `/promote`.

## Everyday operations

| Task | Command |
| --- | --- |
| Redeploy after code changes | `fly deploy` |
| Tail logs | `fly logs` |
| Restart the bot | `fly apps restart dbp-attendance-bot` |
| Change a setting (e.g. timezone) | edit `[env]` in `fly.toml`, then `fly deploy` |
| Rotate the bot token | `fly secrets set BOT_TOKEN="new-token"` (auto-redeploys) |
| Open a shell on the machine | `fly ssh console` |

## Backing up the database

The database file is `/data/attendance.db` on the volume. To download a copy:

```bash
fly ssh console -C "cat /data/attendance.db" > attendance-backup.db
```

Or export from within Telegram at any time using the bot's `/export` command,
which sends a CSV report.

## Important notes

- **Run only ONE instance.** A Telegram bot must have a single poller; two
  machines calling `getUpdates` will conflict and cause dropped/duplicated
  messages. `fly.toml` is set up for a single VM - do not scale the count up.
- **Cost.** A single `shared-cpu-1x` / 256 MB machine plus a 1 GB volume is
  Fly's smallest paid footprint (a few dollars a month); check current Fly
  pricing for exact figures.
- **Timezone.** `TZ_OFFSET_HOURS` in `fly.toml` controls timestamps and the
  "current date" logic. It's set to `7` (ICT) by default - change it to your
  local UTC offset and redeploy.
