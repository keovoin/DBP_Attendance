# Deploying the Attendance Bot to Fly.io

This bot runs as an always-on **long-polling worker** (no public URL, no
webhook). The SQLite database is kept on a **persistent Fly volume** so your
attendance history survives deploys and restarts.

Everything the bot needs is in this repo:

- `Dockerfile` - zero-dependency Python image
- `fly.toml` - app config with the volume mount and non-secret env vars
- `.dockerignore` - keeps secrets and local files out of the image

## Easiest path: deploy from GitHub (no terminal, no local machine)

This repo includes a GitHub Actions workflow (`.github/workflows/fly-deploy.yml`)
that deploys to Fly.io for you. You only paste three secrets into the GitHub
website; everything else is automatic.

**1. Create a Fly deploy token (in your browser)**
   - Go to <https://fly.io/dashboard> and open the `dbp-attendance` app.
   - Open its **Tokens** section and create a new token (a "deploy token" is
     enough). Copy the whole token.

**2. Add three secrets on GitHub (in your browser)**
   - Go to the repo -> **Settings** -> **Secrets and variables** -> **Actions**.
   - Click **New repository secret** and add each of these:
     | Name | Value |
     | --- | --- |
     | `FLY_API_TOKEN` | the token you copied from Fly |
     | `BOT_TOKEN` | your bot token from @BotFather |
     | `ADMIN_TELEGRAM_IDS` | your numeric Telegram ID from @userinfobot |

**3. Run the deploy (in your browser)**
   - Go to the repo **Actions** tab -> **Deploy to Fly.io** -> **Run workflow**.
   - (It also runs automatically on every push.)
   - Watch it go green. The workflow pushes your Telegram secrets to Fly and
     deploys. Then message your bot `/register` on Telegram.

> The very first run will fail if you haven't added the three secrets yet - that
> is expected. Add them and re-run.
>
> This requires the Fly app and its data volume to already exist. If you have
> run `fly launch` once before (which created `dbp-attendance`), you're set. If
> the deploy complains about a missing volume, create one named
> `attendance_data` in the Fly dashboard (Volumes) or via
> `fly volumes create attendance_data --region sin --size 1`.

---

## Prerequisites (CLI alternative)

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

> **Already ran `fly launch`?** Then the `dbp-attendance` app already exists.
> Skip step 1, make sure the volume exists (step 2) and secrets are set
> (step 3), then just run `fly deploy` with the corrected `fly.toml` from this
> repo.

```bash
# 1. Register the app WITHOUT deploying yet. This reuses the committed fly.toml.
#    If the name "dbp-attendance" is taken, edit `app = ...` in fly.toml
#    (or let this command assign a unique name).
fly launch --no-deploy --copy-config --name dbp-attendance --region sin

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
| Restart the bot | `fly apps restart dbp-attendance` |
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

## Troubleshooting

### `failed to connect to machine` / error `PM05`

This means Fly's proxy could not reach your machine. The two usual causes:

1. **Secrets not set (most common).** If `BOT_TOKEN` is missing the bot exits
   on startup and the machine crash-loops, so nothing ever listens.
   - Check: `fly secrets list` (you should see `BOT_TOKEN` and
     `ADMIN_TELEGRAM_IDS`).
   - Check: `fly logs` - a clear `BOT_TOKEN is not set` message confirms it.
   - Fix: `fly secrets set BOT_TOKEN="..." ADMIN_TELEGRAM_IDS="..."` then
     `fly deploy`.
2. **Port mismatch.** The app runs a health-check server on port `8080`; this
   must equal `internal_port` in `[http_service]` (and the `PORT` env). Both
   are `8080` in the committed config - only change them together.

After fixing, redeploy and confirm:

```bash
fly deploy
fly status          # machine should be "started" and checks "passing"
fly logs            # look for "Health-check server listening on 0.0.0.0:8080"
                    # and "Polling for updates."
```

### Machine keeps restarting

Run `fly logs` and read the traceback. A bad `BOT_TOKEN` shows as a Telegram
`401`/`Could not reach Telegram` message; fix the secret and redeploy.

## Important notes

- **Run only ONE instance.** A Telegram bot must have a single poller; two
  machines calling `getUpdates` will conflict and cause dropped/duplicated
  messages. `fly.toml` is set up for a single VM - do not scale the count up.
- **Cost.** A single `shared-cpu-1x` / 256 MB machine plus a 1 GB volume is
  Fly's smallest paid footprint (a few dollars a month); check current Fly
  pricing for exact figures.
- **Timezone.** `TZ_OFFSET_HOURS` in `fly.toml` controls timestamps and the
  "current date" logic. It's set to `7` = Cambodia / Indochina Time (ICT,
  UTC+7). Change it only if you need a different local UTC offset, then redeploy.
