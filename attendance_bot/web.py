"""Admin web portal / dashboard for the Telegram Attendance Tracker.

A self-contained, dependency-free web app built on the standard library
``http.server``. It reads the same SQLite database the bot writes to and shows
an admin dashboard: summary stats, a recent-activity chart, a filterable
attendance table, a members list, and CSV export.

Access is protected by a single admin password (``ADMIN_PORTAL_PASSWORD``).
Sessions are kept in a signed, HttpOnly cookie. The portal only starts if a
password is configured; otherwise the app serves a minimal health endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import http.cookies
import logging
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .db import ROLE_ADMIN, ROLE_REGULAR, TYPE_ON_SITE, TYPE_REMOTE, Database
from .reports import build_csv
from . import timeutil

logger = logging.getLogger("attendance_bot.web")

SESSION_TTL_SECONDS = 12 * 3600
COOKIE_NAME = "att_session"


# --------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------- #
def _sign(secret: str, message: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def make_session_token(secret: str) -> str:
    expiry = str(int(time.time()) + SESSION_TTL_SECONDS)
    return f"{expiry}.{_sign(secret, expiry)}"


def verify_session_token(secret: str, token: str) -> bool:
    try:
        expiry_str, sig = token.split(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(sig, _sign(secret, expiry_str)):
        return False
    try:
        return int(expiry_str) > int(time.time())
    except ValueError:
        return False


# --------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------- #
def _e(value: object) -> str:
    """HTML-escape a value, rendering None as an em dash."""
    if value is None or value == "":
        return "&mdash;"
    return html.escape(str(value))


PAGE_CSS = """
:root { --bg:#0f172a; --card:#1e293b; --muted:#94a3b8; --text:#e2e8f0;
        --accent:#38bdf8; --accent2:#34d399; --border:#334155; }
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       background:var(--bg); color:var(--text); }
a { color:var(--accent); text-decoration:none; }
header { display:flex; align-items:center; justify-content:space-between;
         padding:16px 24px; background:var(--card); border-bottom:1px solid var(--border); }
header .brand { font-weight:700; font-size:18px; }
header nav a { margin-left:18px; color:var(--muted); }
header nav a:hover { color:var(--text); }
main { max-width:1100px; margin:0 auto; padding:24px; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:16px; color:var(--muted); font-weight:600; margin:28px 0 12px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:14px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; }
.card .num { font-size:28px; font-weight:700; }
.card .lbl { color:var(--muted); font-size:13px; margin-top:4px; }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--border); border-radius:12px; overflow:hidden; }
th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); font-size:14px; }
th { color:var(--muted); font-weight:600; background:#172033; }
tr:last-child td { border-bottom:none; }
.badge { padding:2px 8px; border-radius:999px; font-size:12px; font-weight:600; }
.badge.onsite { background:rgba(52,211,153,.15); color:var(--accent2); }
.badge.remote { background:rgba(56,189,248,.15); color:var(--accent); }
.badge.admin { background:rgba(250,204,21,.15); color:#facc15; }
.badge.user { background:rgba(148,163,184,.15); color:var(--muted); }
form.filters { display:flex; flex-wrap:wrap; gap:10px; align-items:end; margin-bottom:16px; }
label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
input,select,button { font:inherit; padding:8px 10px; border-radius:8px;
        border:1px solid var(--border); background:#0b1220; color:var(--text); }
button, .btn { background:var(--accent); color:#04283a; border:none; font-weight:700; cursor:pointer; }
.btn { display:inline-block; padding:9px 14px; }
.chart { display:flex; align-items:flex-end; gap:6px; height:140px; padding:12px;
         background:var(--card); border:1px solid var(--border); border-radius:12px; }
.bar { flex:1; background:linear-gradient(var(--accent),#0ea5e9); border-radius:4px 4px 0 0; min-height:2px; position:relative; }
.bar span { position:absolute; bottom:-20px; left:0; right:0; text-align:center;
            font-size:10px; color:var(--muted); }
.bar b { position:absolute; top:-18px; left:0; right:0; text-align:center; font-size:11px; }
.muted { color:var(--muted); }
.login-wrap { max-width:360px; margin:10vh auto; }
.login-wrap .card { padding:24px; }
.login-wrap input { width:100%; margin-bottom:12px; }
.login-wrap button { width:100%; }
.err { color:#f87171; font-size:14px; margin-bottom:10px; }
.ok { background:rgba(52,211,153,.15); color:var(--accent2); padding:10px 12px;
      border-radius:8px; margin-bottom:16px; font-size:14px; }
.banner-err { background:rgba(248,113,113,.15); color:#f87171; padding:10px 12px;
      border-radius:8px; margin-bottom:16px; font-size:14px; }
.panel { background:var(--card); border:1px solid var(--border); border-radius:12px;
      padding:18px; margin-bottom:22px; }
.panel h2 { margin-top:0; }
form.stack { display:flex; flex-wrap:wrap; gap:12px; align-items:end; }
form.stack > div { flex:1; min-width:150px; }
form.stack input, form.stack select { width:100%; }
"""


def layout(title: str, body: str, active: str = "") -> bytes:
    def nav(label: str, href: str, key: str) -> str:
        style = ' style="color:var(--text)"' if key == active else ""
        return f'<a href="{href}"{style}>{label}</a>'

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} - Attendance Admin</title>
<style>{PAGE_CSS}</style></head>
<body>
<header>
  <div class="brand">📋 Attendance Admin</div>
  <nav>
    {nav("Dashboard", "/", "dash")}
    {nav("Attendance", "/attendance", "att")}
    {nav("Members", "/members", "mem")}
    {nav("Settings", "/settings", "set")}
    <a href="/logout">Log out</a>
  </nav>
</header>
<main>{body}</main>
</body></html>"""
    return page.encode("utf-8")


def login_page(error: str = "") -> bytes:
    err = f'<div class="err">{_e(error)}</div>' if error else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login - Attendance Admin</title><style>{PAGE_CSS}</style></head>
<body><div class="login-wrap"><div class="card">
<h1>📋 Attendance Admin</h1>
<p class="muted">Enter the admin password to continue.</p>
{err}
<form method="post" action="/login">
  <input type="password" name="password" placeholder="Admin password" autofocus>
  <button type="submit">Log in</button>
</form>
</div></div></body></html>"""
    return page.encode("utf-8")


# --------------------------------------------------------------------- #
# Request handler
# --------------------------------------------------------------------- #
class _PortalHandler(BaseHTTPRequestHandler):
    server_version = "AttendancePortal/1.0"

    # -- shared context -------------------------------------------------
    @property
    def config(self) -> Config:
        return self.server.ctx_config  # type: ignore[attr-defined]

    @property
    def db(self) -> Database:
        return self.server.ctx_db  # type: ignore[attr-defined]

    def log_message(self, *args, **kwargs) -> None:
        return  # keep logs quiet

    # -- low-level responses -------------------------------------------
    def _send(self, status: int, body: bytes, content_type="text/html; charset=utf-8",
              extra_headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location: str, extra_headers=None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()

    # -- auth -----------------------------------------------------------
    def _is_authed(self) -> bool:
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return False
        morsel = jar.get(COOKIE_NAME)
        if morsel is None:
            return False
        return verify_session_token(self.config.portal_secret, morsel.value)

    def _require_auth(self) -> bool:
        if self._is_authed():
            return True
        self._redirect("/login")
        return False

    # -- routing --------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if path == "/login":
            self._send(200, login_page())
            return
        if path == "/logout":
            expired = http.cookies.SimpleCookie()
            expired[COOKIE_NAME] = ""
            expired[COOKIE_NAME]["path"] = "/"
            expired[COOKIE_NAME]["max-age"] = 0
            self._redirect("/login", [("Set-Cookie", expired[COOKIE_NAME].OutputString())])
            return

        if not self._require_auth():
            return

        if path == "/":
            self._send(200, self._dashboard())
        elif path == "/attendance":
            self._send(200, self._attendance_page(params))
        elif path == "/members":
            self._send(200, self._members_page(params))
        elif path == "/settings":
            self._send(200, self._settings_page(params))
        elif path == "/export.csv":
            self._export(params)
        else:
            self._send(404, layout("Not found", "<h1>404</h1><p>Page not found.</p>"))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        form = self._read_form()

        if path == "/login":
            self._handle_login(form)
            return

        # Every other POST is a state change and requires authentication.
        if not self._is_authed():
            self._redirect("/login")
            return

        if path == "/members/add":
            self._add_member(form)
        elif path == "/settings/location":
            self._set_location(form)
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}

    def _handle_login(self, form: dict) -> None:
        password = form.get("password", "")
        expected = self.config.admin_portal_password
        if expected and hmac.compare_digest(password, expected):
            token = make_session_token(self.config.portal_secret)
            cookie = http.cookies.SimpleCookie()
            cookie[COOKIE_NAME] = token
            m = cookie[COOKIE_NAME]
            m["path"] = "/"
            m["httponly"] = True
            m["samesite"] = "Lax"
            m["secure"] = True
            m["max-age"] = SESSION_TTL_SECONDS
            self._redirect("/", [("Set-Cookie", m.OutputString())])
        else:
            self._send(200, login_page("Incorrect password."))

    # -- write actions --------------------------------------------------
    def _add_member(self, form: dict) -> None:
        tid = form.get("telegram_id", "").strip()
        name = form.get("name", "").strip()
        role = form.get("role", ROLE_REGULAR).strip()
        coordinator = form.get("coordinator", "").strip() or None

        if not tid.lstrip("-").isdigit() or not name:
            self._flash_redirect(
                "/members", err="Provide a numeric Telegram ID and a name."
            )
            return
        if role not in (ROLE_ADMIN, ROLE_REGULAR):
            role = ROLE_REGULAR
        tid_int = int(tid)
        if self.db.get_member(tid_int) is not None:
            self._flash_redirect(
                "/members", err="A member with that Telegram ID already exists."
            )
            return
        self.db.create_member(
            telegram_id=tid_int,
            name=name,
            role=role,
            coordinator=coordinator,
            created_at=timeutil.now_iso(self.config.tz_offset_hours),
        )
        self._flash_redirect("/members", ok=f"Added member: {name}.")

    def _set_location(self, form: dict) -> None:
        try:
            lat = float(form.get("latitude", "").strip())
            lon = float(form.get("longitude", "").strip())
        except ValueError:
            self._flash_redirect(
                "/settings", err="Latitude and longitude must be numbers."
            )
            return
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            self._flash_redirect(
                "/settings",
                err="Latitude must be -90..90 and longitude -180..180.",
            )
            return
        self.db.set_configured_location(lat, lon)
        self._flash_redirect("/settings", ok=f"On-site location set to {lat}, {lon}.")

    def _flash_redirect(self, path: str, *, ok: str = "", err: str = "") -> None:
        key, msg = ("ok", ok) if ok else ("err", err)
        self._redirect(f"{path}?{key}={urllib.parse.quote(msg)}")

    @staticmethod
    def _flash(params) -> str:
        ok = params.get("ok", [""])[0]
        err = params.get("err", [""])[0]
        if ok:
            return f'<div class="ok">{_e(ok)}</div>'
        if err:
            return f'<div class="banner-err">{_e(err)}</div>'
        return ""

    # -- pages ----------------------------------------------------------
    def _dashboard(self) -> bytes:
        tz = self.config.tz_offset_hours
        today = timeutil.today_iso(tz)
        entries = self.db.query_attendance()
        members = self.db.list_members()

        total_members = len(members)
        total_admins = sum(1 for m in members if m.role == ROLE_ADMIN)
        total_entries = len(entries)
        today_entries = [e for e in entries if e.date == today]
        onsite = sum(1 for e in entries if e.clock_in_type == TYPE_ON_SITE)
        remote = sum(1 for e in entries if e.clock_in_type == TYPE_REMOTE)
        clocked_in_now = sum(
            1 for e in today_entries if e.clock_in_time and not e.clock_out_time
        )

        cards = [
            (total_members, "Members"),
            (total_admins, "Admins"),
            (total_entries, "Total entries"),
            (len(today_entries), "Clock-ins today"),
            (clocked_in_now, "Currently clocked in"),
            (onsite, "On-site (all time)"),
            (remote, "Remote (all time)"),
        ]
        cards_html = "".join(
            f'<div class="card"><div class="num">{num}</div>'
            f'<div class="lbl">{_e(label)}</div></div>'
            for num, label in cards
        )

        chart_html = self._recent_chart(entries, tz)

        recent = sorted(entries, key=lambda e: (e.date, e.id), reverse=True)[:10]
        rows = "".join(self._entry_row(e, include_member=True) for e in recent)
        if not rows:
            rows = '<tr><td colspan="6" class="muted">No attendance yet.</td></tr>'

        body = f"""
        <h1>Dashboard</h1>
        <p class="muted">Overview as of {_e(today)} (UTC{tz:+g}).</p>
        <div class="cards">{cards_html}</div>
        <h2>Clock-ins over the last 14 days</h2>
        {chart_html}
        <h2>Recent activity</h2>
        <table>
          <tr><th>Date</th><th>Member</th><th>In</th><th>Out</th>
              <th>Type</th><th>Coordinator</th></tr>
          {rows}
        </table>
        """
        return layout("Dashboard", body, active="dash")

    def _recent_chart(self, entries, tz) -> str:
        # Count entries per day for the last 14 days.
        from datetime import timedelta

        now = timeutil.now(tz)
        days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
        counts = {d: 0 for d in days}
        for e in entries:
            if e.date in counts:
                counts[e.date] += 1
        peak = max(counts.values()) or 1
        bars = ""
        for d in days:
            c = counts[d]
            pct = int((c / peak) * 100)
            label = d[5:]  # MM-DD
            top = f"<b>{c}</b>" if c else ""
            bars += (
                f'<div class="bar" style="height:{max(pct,2)}%">'
                f"{top}<span>{label}</span></div>"
            )
        return f'<div class="chart">{bars}</div>'

    def _attendance_page(self, params) -> bytes:
        members = self.db.list_members()
        member_param = params.get("member", ["all"])[0]
        start = params.get("from", [""])[0].strip()
        end = params.get("to", [""])[0].strip()

        telegram_id = None
        if member_param not in ("", "all"):
            try:
                telegram_id = int(member_param)
            except ValueError:
                telegram_id = None
        start_v = start if timeutil.is_valid_date(start) else None
        end_v = end if timeutil.is_valid_date(end) else None

        entries = self.db.query_attendance(
            telegram_id=telegram_id, start_date=start_v, end_date=end_v
        )

        options = ['<option value="all">All members</option>']
        for m in members:
            sel = " selected" if str(m.telegram_id) == member_param else ""
            options.append(
                f'<option value="{m.telegram_id}"{sel}>{_e(m.name)}</option>'
            )

        rows = "".join(self._entry_row(e, include_member=True) for e in entries)
        if not rows:
            rows = '<tr><td colspan="6" class="muted">No records for this filter.</td></tr>'

        qs = urllib.parse.urlencode(
            {"member": member_param, "from": start, "to": end}
        )
        body = f"""
        <h1>Attendance</h1>
        <form class="filters" method="get" action="/attendance">
          <div><label>Member</label>
            <select name="member">{''.join(options)}</select></div>
          <div><label>From (YYYY-MM-DD)</label>
            <input type="date" name="from" value="{_e(start)}"></div>
          <div><label>To (YYYY-MM-DD)</label>
            <input type="date" name="to" value="{_e(end)}"></div>
          <button type="submit">Filter</button>
          <a class="btn" href="/export.csv?{qs}">Export CSV</a>
        </form>
        <p class="muted">{len(entries)} record(s).</p>
        <table>
          <tr><th>Date</th><th>Member</th><th>In</th><th>Out</th>
              <th>Type</th><th>Coordinator</th></tr>
          {rows}
        </table>
        """
        return layout("Attendance", body, active="att")

    def _members_page(self, params) -> bytes:
        members = self.db.list_members()
        rows = ""
        for m in members:
            badge = (
                '<span class="badge admin">Admin</span>'
                if m.role == ROLE_ADMIN
                else '<span class="badge user">Regular</span>'
            )
            rows += (
                f"<tr><td>{_e(m.name)}</td><td>{badge}</td>"
                f"<td>{_e(m.coordinator)}</td><td class='muted'>{_e(m.telegram_id)}</td>"
                f"<td class='muted'>{_e(m.created_at)}</td></tr>"
            )
        if not rows:
            rows = '<tr><td colspan="5" class="muted">No members yet.</td></tr>'

        body = f"""
        <h1>Members</h1>
        {self._flash(params)}
        <div class="panel">
          <h2>Add a member</h2>
          <form class="stack" method="post" action="/members/add">
            <div><label>Telegram ID</label>
              <input name="telegram_id" placeholder="e.g. 123456789"></div>
            <div><label>Name</label>
              <input name="name" placeholder="Full name"></div>
            <div><label>Role</label>
              <select name="role">
                <option value="regular">Regular</option>
                <option value="admin">Admin</option>
              </select></div>
            <div><label>Coordinator</label>
              <input name="coordinator" placeholder="Optional"></div>
            <div><button type="submit">Add member</button></div>
          </form>
          <p class="muted" style="margin-bottom:0">The Telegram ID must be the
          person's real numeric ID (they can get it from
          <b>@userinfobot</b>) so the bot links their clock-ins. They can also
          just message the bot <b>/register</b> themselves.</p>
        </div>
        <table>
          <tr><th>Name</th><th>Role</th><th>Coordinator</th>
              <th>Telegram ID</th><th>Registered</th></tr>
          {rows}
        </table>
        """
        return layout("Members", body, active="mem")

    def _settings_page(self, params) -> bytes:
        loc = self.db.get_configured_location()
        if loc:
            lat, lon = loc
            latv, lonv = str(lat), str(lon)
            current = (
                f"Current on-site location: <b>{_e(lat)}, {_e(lon)}</b> &nbsp;"
                f"(<a href='https://www.google.com/maps?q={lat},{lon}' "
                f"target='_blank' rel='noopener'>view on map</a>)"
            )
        else:
            latv = lonv = ""
            current = "No on-site location configured yet."
        radius = self.config.geofence_radius_meters

        body = f"""
        <h1>Settings</h1>
        {self._flash(params)}
        <div class="panel">
          <h2>On-site location</h2>
          <p class="muted">{current}</p>
          <form class="stack" method="post" action="/settings/location">
            <div><label>Latitude</label>
              <input name="latitude" value="{_e(latv)}" placeholder="11.5564"></div>
            <div><label>Longitude</label>
              <input name="longitude" value="{_e(lonv)}" placeholder="104.9282"></div>
            <div><button type="submit">Save location</button></div>
          </form>
          <p class="muted" style="margin-bottom:0">On-site clock-ins must be
          within <b>{radius:.0f} meters</b> of this point. Tip: open Google Maps,
          right-click your office, and copy the latitude, longitude.</p>
        </div>
        """
        return layout("Settings", body, active="set")

    def _entry_row(self, e, include_member: bool) -> str:
        if e.clock_in_type == TYPE_ON_SITE:
            type_badge = '<span class="badge onsite">On_Site</span>'
        elif e.clock_in_type == TYPE_REMOTE:
            type_badge = '<span class="badge remote">Remote</span>'
        else:
            type_badge = _e(None)
        member_cell = f"<td>{_e(e.member_name)}</td>" if include_member else ""
        return (
            f"<tr><td>{_e(e.date)}</td>{member_cell}"
            f"<td>{_e(e.clock_in_time)}</td><td>{_e(e.clock_out_time)}</td>"
            f"<td>{type_badge}</td><td>{_e(e.coordinator)}</td></tr>"
        )

    def _export(self, params) -> None:
        member_param = params.get("member", ["all"])[0]
        start = params.get("from", [""])[0].strip()
        end = params.get("to", [""])[0].strip()
        telegram_id = None
        if member_param not in ("", "all"):
            try:
                telegram_id = int(member_param)
            except ValueError:
                telegram_id = None
        start_v = start if timeutil.is_valid_date(start) else None
        end_v = end if timeutil.is_valid_date(end) else None
        entries = self.db.query_attendance(
            telegram_id=telegram_id, start_date=start_v, end_date=end_v
        )
        csv_bytes = build_csv(entries)
        stamp = timeutil.today_iso(self.config.tz_offset_hours)
        filename = f"attendance_{stamp}.csv"
        self._send(
            200,
            csv_bytes,
            content_type="text/csv; charset=utf-8",
            extra_headers=[
                ("Content-Disposition", f'attachment; filename="{filename}"')
            ],
        )


# --------------------------------------------------------------------- #
# Server bootstrap
# --------------------------------------------------------------------- #
def start_web_portal(config: Config) -> ThreadingHTTPServer:
    """Start the admin portal in a daemon thread; returns the server.

    The portal uses its OWN database connection (a separate Database instance
    on the same file) so it never contends with the bot thread's connection.
    """
    server = ThreadingHTTPServer(("0.0.0.0", config.health_port), _PortalHandler)
    server.ctx_config = config  # type: ignore[attr-defined]
    server.ctx_db = Database(config.db_path)  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=server.serve_forever, name="web-portal", daemon=True
    )
    thread.start()
    logger.info(
        "Admin web portal listening on 0.0.0.0:%d", config.health_port
    )
    return server
