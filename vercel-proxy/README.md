# Vercel proxy for the Attendance dashboard

Some networks block `*.fly.dev`. This tiny Vercel project puts an allowed
`*.vercel.app` URL in front of the admin dashboard.

- Your **browser** only talks to `your-app.vercel.app` (not blocked).
- **Vercel's servers** forward every request to `https://dbp-attendance.fly.dev`
  (Vercel is outside your company network, so the block doesn't apply).
- The whole app (Telegram bot + dashboard + database) still runs on Fly.io.
  This only re-routes the dashboard URL.

> The Telegram bot itself is **not** affected by the `fly.dev` block — it
> communicates through Telegram's servers, so clocking in keeps working.
> You only need this proxy to open the **admin dashboard** in a browser.

## Deploy (browser only, ~2 minutes)

1. Go to <https://vercel.com> and sign in with GitHub.
2. **Add New... -> Project** and **Import** the `keovoin/DBP_Attendance` repo.
3. In the import screen:
   - **Root Directory**: click *Edit* and choose **`vercel-proxy`**.
   - **Framework Preset**: *Other* (no build command / output needed).
4. Click **Deploy**.
5. Open the URL Vercel gives you (e.g. `https://dbp-attendance-xxxx.vercel.app`)
   and log in with your `ADMIN_PORTAL_PASSWORD`.

Share that `*.vercel.app` link with any admins who are behind the block.

## Notes

- If you change the Fly app name, update the `destination` URL in `vercel.json`.
- The Map page also loads map tiles from OpenStreetMap; if your network blocks
  those too, the map won't render but every other page works normally.
