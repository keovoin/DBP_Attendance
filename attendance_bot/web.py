"""Admin web portal / dashboard for the Telegram Attendance Tracker.

A self-contained, dependency-free web app built on the standard library
``http.server``. It reads and writes the same SQLite database the bot uses and
provides: dashboard stats, a filterable attendance table (view/edit/delete),
member management (add/edit/delete), multiple site management, work-schedule
settings, a map view, an analytics page, CSV + Excel export, and an audit log.

Access is protected by a single admin password (``ADMIN_PORTAL_PASSWORD``).
Sessions are kept in a signed, HttpOnly cookie. The portal only starts if a
password is configured; otherwise the app serves a minimal health endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import http.cookies
import json
import logging
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import timeutil
from .config import Config
from .db import (
    ROLE_ADMIN,
    ROLE_REGULAR,
    TYPE_ON_SITE,
    TYPE_REMOTE,
    Database,
)
from .reports import build_csv
from .xlsx import build_xlsx, read_xlsx

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
    if value is None or value == "":
        return "&mdash;"
    return html.escape(str(value))


def _attr(value: object) -> str:
    """Escape a value for use inside an HTML attribute (no em dash)."""
    return html.escape("" if value is None else str(value), quote=True)


PAGE_CSS = """
:root, :root[data-theme="dark"] {
  --bg:#0f172a; --card:#1e293b; --muted:#94a3b8; --text:#e2e8f0;
  --accent:#38bdf8; --accent2:#34d399; --border:#334155; --danger:#f87171;
  --th-bg:#172033; --input-bg:#0b1220; --btn2-bg:#334155; --shadow:rgba(0,0,0,.3);
  --on-accent:#04283a; --on-danger:#3a0404;
}
:root[data-theme="light"] {
  --bg:#f1f5f9; --card:#ffffff; --muted:#64748b; --text:#0f172a;
  --accent:#0284c7; --accent2:#059669; --border:#cbd5e1; --danger:#dc2626;
  --th-bg:#f1f5f9; --input-bg:#ffffff; --btn2-bg:#e2e8f0; --shadow:rgba(2,8,23,.08);
  --on-accent:#ffffff; --on-danger:#ffffff;
}
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       background:var(--bg); color:var(--text);
       transition: background .2s ease, color .2s ease; }
a { color:var(--accent); text-decoration:none; }
header { position:sticky; top:0; z-index:50; display:flex; align-items:center;
         justify-content:space-between; padding:14px 24px; background:var(--card);
         border-bottom:1px solid var(--border); flex-wrap:wrap; gap:8px;
         box-shadow:0 1px 3px var(--shadow); }
header .brand { font-weight:800; font-size:18px; letter-spacing:-.02em; }
header nav a { margin-left:4px; color:var(--muted); padding:6px 11px; border-radius:9px;
         font-size:14px; transition:background .15s ease, color .15s ease; }
header nav a:hover { color:var(--text); background:var(--btn2-bg); }
header nav a.active { color:var(--text); background:var(--btn2-bg); }
main { max-width:1150px; margin:0 auto; padding:28px 24px; }
h1 { font-size:24px; margin:0 0 6px; font-weight:800; letter-spacing:-.02em; }
h2 { font-size:15px; color:var(--muted); font-weight:700; margin:28px 0 12px;
     text-transform:uppercase; letter-spacing:.05em; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:16px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:16px; padding:18px;
        box-shadow:0 1px 3px var(--shadow); transition:transform .15s ease, box-shadow .15s ease; }
.card:hover { transform:translateY(-2px); box-shadow:0 8px 24px var(--shadow); }
.card .num { font-size:30px; font-weight:800; letter-spacing:-.02em; }
.card .lbl { color:var(--muted); font-size:13px; margin-top:4px; }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--border); border-radius:16px; overflow:hidden;
        box-shadow:0 1px 3px var(--shadow); }
th,td { text-align:left; padding:12px 14px; border-bottom:1px solid var(--border); font-size:14px; }
th { color:var(--muted); font-weight:700; background:var(--th-bg);
     text-transform:uppercase; font-size:11px; letter-spacing:.05em; }
tr:last-child td { border-bottom:none; }
tr:hover td { background:color-mix(in srgb, var(--accent) 7%, transparent); }
.badge { padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600; }
.badge.onsite { background:rgba(52,211,153,.15); color:var(--accent2); }
.badge.ontime { background:rgba(52,211,153,.15); color:var(--accent2); }
.badge.remote { background:rgba(56,189,248,.15); color:var(--accent); }
.badge.admin { background:rgba(250,204,21,.15); color:#facc15; }
.badge.user { background:rgba(148,163,184,.15); color:var(--muted); }
.badge.late { background:rgba(248,113,113,.15); color:var(--danger); }
form.filters { display:flex; flex-wrap:wrap; gap:10px; align-items:end; margin-bottom:16px; }
label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
input,select,button,textarea { font:inherit; padding:9px 11px; border-radius:10px;
        border:1px solid var(--border); background:var(--input-bg); color:var(--text);
        transition:border-color .15s ease, box-shadow .15s ease; }
input:focus,select:focus,textarea:focus { outline:none; border-color:var(--accent);
        box-shadow:0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
button, .btn { background:var(--accent); color:var(--on-accent); border:none; font-weight:700;
        cursor:pointer; box-shadow:0 1px 2px var(--shadow);
        transition:filter .15s ease, transform .05s ease; }
button:hover, .btn:hover { filter:brightness(1.08); }
button:active, .btn:active { transform:translateY(1px); }
.btn { display:inline-block; padding:10px 15px; }
.btn.secondary { background:var(--btn2-bg); color:var(--text); }
.btn.danger { background:var(--danger); color:var(--on-danger); }
.chart { display:flex; align-items:flex-end; gap:6px; height:140px; padding:12px;
         background:var(--card); border:1px solid var(--border); border-radius:12px; }
.bar { flex:1; background:linear-gradient(var(--accent),#0ea5e9); border-radius:4px 4px 0 0; min-height:2px; position:relative; }
.bar span { position:absolute; bottom:-20px; left:0; right:0; text-align:center; font-size:10px; color:var(--muted); }
.bar b { position:absolute; top:-18px; left:0; right:0; text-align:center; font-size:11px; }
.muted { color:var(--muted); }
.login-wrap { max-width:360px; margin:10vh auto; }
.login-wrap .card { padding:24px; }
.login-wrap input { width:100%; margin-bottom:12px; }
.login-wrap button { width:100%; }
.err { color:var(--danger); font-size:14px; margin-bottom:10px; }
.ok { background:rgba(52,211,153,.15); color:var(--accent2); padding:10px 12px; border-radius:8px; margin-bottom:16px; font-size:14px; }
.banner-err { background:rgba(248,113,113,.15); color:var(--danger); padding:10px 12px; border-radius:8px; margin-bottom:16px; font-size:14px; }
.panel { background:var(--card); border:1px solid var(--border); border-radius:16px; padding:20px;
        margin-bottom:22px; box-shadow:0 1px 3px var(--shadow); }
.panel h2 { margin-top:0; }
form.stack { display:flex; flex-wrap:wrap; gap:12px; align-items:end; }
form.stack > div { flex:1; min-width:150px; }
form.stack input, form.stack select, form.stack textarea { width:100%; }
.actions a { margin-right:10px; font-size:13px; }
.row2 { display:flex; gap:20px; flex-wrap:wrap; }
.row2 > .panel { flex:1; min-width:300px; }
.checks label { display:inline-flex; align-items:center; gap:6px; margin-right:14px;
        color:var(--text); font-size:14px; }
.checks input { width:auto; }
#map { height:460px; border-radius:12px; border:1px solid var(--border); }
.theme-toggle { margin-left:16px; background:transparent; border:1px solid var(--border);
        color:var(--text); cursor:pointer; padding:6px 10px; border-radius:8px;
        font-size:15px; line-height:1; }
.theme-toggle:hover { border-color:var(--accent); }
@media (max-width: 680px) {
  header { flex-direction:column; align-items:stretch; padding:12px 16px; }
  header nav { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; }
  header nav a { margin-left:0; }
  .theme-toggle { margin-left:auto; }
  main { padding:16px; }
  h1 { font-size:20px; }
  /* Let wide tables scroll horizontally instead of squashing. */
  table { display:block; overflow-x:auto; white-space:nowrap; }
  form.stack > div { min-width:100%; }
  .row2 > .panel { min-width:100%; }
  .cards { grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); }
  #map { height:360px; }
}
"""

# Applies the saved theme before first paint (avoids a flash of the wrong theme)
# and wires the toggle button.
THEME_SCRIPT = (
    "<script>(function(){try{var t=localStorage.getItem('theme')||'dark';"
    "document.documentElement.setAttribute('data-theme',t);}catch(e){}})();"
    "function toggleTheme(){var d=document.documentElement;"
    "var t=d.getAttribute('data-theme')==='light'?'dark':'light';"
    "d.setAttribute('data-theme',t);try{localStorage.setItem('theme',t);}catch(e){}"
    "var b=document.getElementById('themeBtn');if(b)b.textContent=t==='light'?'\\u2600\\ufe0f':'\\ud83c\\udf19';}"
    "</script>"
)


def layout(title: str, body: str, active: str = "", head_extra: str = "") -> bytes:
    def nav(label: str, href: str, key: str) -> str:
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} - Attendance Admin</title>
<style>{PAGE_CSS}</style>{THEME_SCRIPT}{head_extra}</head>
<body>
<header>
  <div class="brand">📋 Attendance Admin</div>
  <nav>
    {nav("Dashboard", "/", "dash")}
    {nav("Attendance", "/attendance", "att")}
    {nav("Members", "/members", "mem")}
    {nav("Analytics", "/analytics", "analytics")}
    {nav("Map", "/map", "map")}
    {nav("Announce", "/announce", "announce")}
    {nav("Settings", "/settings", "set")}
    <a href="/logout">Log out</a>
    <button id="themeBtn" class="theme-toggle" onclick="toggleTheme()"
            aria-label="Toggle light or dark theme" title="Toggle light/dark">🌙</button>
  </nav>
</header>
<main>{body}</main>
<script>(function(){{var t=document.documentElement.getAttribute('data-theme');
var b=document.getElementById('themeBtn');if(b)b.textContent=t==='light'?'☀️':'🌙';}})();</script>
</body></html>"""
    return page.encode("utf-8")


def login_page(error: str = "") -> bytes:
    err = f'<div class="err">{_e(error)}</div>' if error else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login - Attendance Admin</title><style>{PAGE_CSS}</style>{THEME_SCRIPT}</head>
<body><div class="login-wrap"><div class="card">
<h1>📋 Attendance Admin</h1>
<p class="muted">Enter the admin password to continue.</p>
{err}
<form method="post" action="/login">
  <input type="password" name="password" placeholder="Admin password" autofocus>
  <button type="submit">Log in</button>
</form>
<p style="text-align:center;margin:14px 0 0">
  <button id="themeBtn" class="theme-toggle" style="margin:0" onclick="toggleTheme()"
          aria-label="Toggle light or dark theme">🌙</button>
</p>
</div></div>
<script>(function(){{var t=document.documentElement.getAttribute('data-theme');
var b=document.getElementById('themeBtn');if(b)b.textContent=t==='light'?'☀️':'🌙';}})();</script>
</body></html>"""
    return page.encode("utf-8")


# --------------------------------------------------------------------- #
# Request handler
# --------------------------------------------------------------------- #
class _PortalHandler(BaseHTTPRequestHandler):
    server_version = "AttendancePortal/1.0"

    @property
    def config(self) -> Config:
        return self.server.ctx_config  # type: ignore[attr-defined]

    @property
    def db(self) -> Database:
        return self.server.ctx_db  # type: ignore[attr-defined]

    @property
    def client(self):
        return getattr(self.server, "ctx_client", None)

    def log_message(self, *args, **kwargs) -> None:
        return

    # -- low-level ------------------------------------------------------
    def _send(self, status, body, content_type="text/html; charset=utf-8",
              extra_headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location, extra_headers=None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()

    def _flash_redirect(self, path, *, ok="", err="") -> None:
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

    def _audit(self, action: str, detail: str = "") -> None:
        self.db.add_audit(
            timeutil.now_iso(self.config.tz_offset_hours), "web-admin", action, detail
        )

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
        m = jar.get(COOKIE_NAME)
        return m is not None and verify_session_token(self.config.portal_secret, m.value)

    def _require_auth(self) -> bool:
        if self._is_authed():
            return True
        self._redirect("/login")
        return False

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            boundary = ""
            for part in ctype.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[len("boundary="):].strip('"')
            return self._parse_multipart(body, boundary) if boundary else {}
        text = body.decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}

    def _parse_multipart(self, body: bytes, boundary: str) -> dict:
        """Minimal multipart/form-data parser (stdlib-only).

        Text fields become ``str`` values; a file field's value is the raw
        ``bytes`` plus a companion ``"<name>_filename"`` entry.
        """
        result: dict = {}
        for segment in body.split(b"--" + boundary.encode()):
            if segment in (b"", b"--", b"--\r\n"):
                continue
            if segment.startswith(b"\r\n"):
                segment = segment[2:]
            if segment.endswith(b"\r\n"):
                segment = segment[:-2]
            if b"\r\n\r\n" not in segment:
                continue
            raw_headers, _, content = segment.partition(b"\r\n\r\n")
            headers = raw_headers.decode("utf-8", "replace")
            name = filename = None
            for line in headers.split("\r\n"):
                if line.lower().startswith("content-disposition"):
                    for token in line.split(";"):
                        token = token.strip()
                        if token.startswith("name="):
                            name = token[len("name="):].strip('"')
                        elif token.startswith("filename="):
                            filename = token[len("filename="):].strip('"')
            if name is None:
                continue
            if filename is not None:
                result[name] = content
                result[f"{name}_filename"] = filename
            else:
                result[name] = content.decode("utf-8", "replace")
        return result

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
            exp = http.cookies.SimpleCookie()
            exp[COOKIE_NAME] = ""
            exp[COOKIE_NAME]["path"] = "/"
            exp[COOKIE_NAME]["max-age"] = 0
            self._redirect("/login", [("Set-Cookie", exp[COOKIE_NAME].OutputString())])
            return

        if not self._require_auth():
            return

        routes = {
            "/": self._dashboard,
            "/attendance": lambda: self._attendance_page(params),
            "/members": lambda: self._members_page(params),
            "/member": lambda: self._member_edit_page(params),
            "/entry": lambda: self._entry_edit_page(params),
            "/analytics": lambda: self._analytics_page(params),
            "/map": lambda: self._map_page(params),
            "/announce": lambda: self._announce_page(params),
            "/settings": lambda: self._settings_page(params),
        }
        if path == "/export.csv":
            self._export_csv(params)
        elif path == "/report.xlsx":
            self._export_xlsx(params)
        elif path == "/members/template.xlsx":
            self._member_template()
        elif path in routes:
            self._send(200, routes[path]())
        else:
            self._send(404, layout("Not found", "<h1>404</h1><p>Page not found.</p>"))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        form = self._read_form()

        if path == "/login":
            self._handle_login(form)
            return
        if not self._is_authed():
            self._redirect("/login")
            return

        actions = {
            "/members/add": self._add_member,
            "/members/update": self._update_member,
            "/members/delete": self._delete_member,
            "/entry/update": self._update_entry,
            "/entry/delete": self._delete_entry,
            "/sites/add": self._add_site,
            "/sites/delete": self._delete_site,
            "/settings/location": self._set_location,
            "/settings/schedule": self._set_schedule,
            "/settings/geofence": self._set_geofence,
            "/settings/recalc-late": self._recalc_late,
            "/members/bulk": self._bulk_add_members,
            "/units/add": self._add_unit,
            "/units/delete": self._delete_unit,
            "/holidays/add": self._add_holiday,
            "/holidays/delete": self._delete_holiday,
            "/announce/send": self._send_announcement,
        }
        action = actions.get(path)
        if action is None:
            self._send(404, b"not found", "text/plain; charset=utf-8")
        else:
            action(form)

    def _handle_login(self, form) -> None:
        password = form.get("password", "")
        expected = self.config.admin_portal_password
        if expected and hmac.compare_digest(password, expected):
            cookie = http.cookies.SimpleCookie()
            cookie[COOKIE_NAME] = make_session_token(self.config.portal_secret)
            m = cookie[COOKIE_NAME]
            m["path"] = "/"
            m["httponly"] = True
            m["samesite"] = "Lax"
            m["secure"] = True
            m["max-age"] = SESSION_TTL_SECONDS
            self._redirect("/", [("Set-Cookie", m.OutputString())])
        else:
            self._send(200, login_page("Incorrect password."))

    # ================================================================== #
    # Shared render helpers
    # ================================================================== #
    def _site_options(self, selected_id, include_none=True) -> str:
        opts = []
        if include_none:
            sel = " selected" if not selected_id else ""
            opts.append(f'<option value="0"{sel}>(none)</option>')
        for s in self.db.list_sites():
            sel = " selected" if selected_id and int(selected_id) == s.id else ""
            opts.append(f'<option value="{s.id}"{sel}>{_e(s.name)}</option>')
        return "".join(opts)

    def _named_datalists(self) -> str:
        units = "".join(f'<option value="{_attr(u.name)}">'
                        for u in self.db.list_units())
        return f'<datalist id="unitlist">{units}</datalist>'

    def _site_name(self, site_id) -> str:
        if not site_id:
            return "&mdash;"
        s = self.db.get_site(int(site_id))
        return _e(s.name) if s else "&mdash;"

    def _type_badge(self, t) -> str:
        if t == TYPE_ON_SITE:
            return '<span class="badge onsite">On_Site</span>'
        if t == TYPE_REMOTE:
            return '<span class="badge remote">Remote</span>'
        return _e(None)

    # ================================================================== #
    # Dashboard
    # ================================================================== #
    def _dashboard(self) -> bytes:
        tz = self.config.tz_offset_hours
        today = timeutil.today_iso(tz)
        entries = self.db.query_attendance()
        members = self.db.list_members()

        today_entries = [e for e in entries if e.date == today]
        cards = [
            (len(members), "Members"),
            (sum(1 for m in members if m.role == ROLE_ADMIN), "Admins"),
            (len(entries), "Total entries"),
            (len(today_entries), "Clock-ins today"),
            (sum(1 for e in today_entries if e.clock_in_time and not e.clock_out_time),
             "Currently in"),
            (sum(1 for e in entries if e.is_late), "Late (all time)"),
        ]
        cards_html = "".join(
            f'<div class="card"><div class="num">{n}</div>'
            f'<div class="lbl">{_e(lbl)}</div></div>' for n, lbl in cards
        )
        recent = sorted(entries, key=lambda e: (e.date, e.id), reverse=True)[:10]
        rows = "".join(self._entry_row(e) for e in recent) or \
            '<tr><td colspan="7" class="muted">No attendance yet.</td></tr>'
        body = f"""
        <h1>Dashboard</h1>
        <p class="muted">As of {_e(today)} (UTC{tz:+g}).</p>
        <div class="cards">{cards_html}</div>
        <h2>Clock-ins over the last 14 days</h2>
        {self._recent_chart(entries, tz)}
        <h2>Recent activity</h2>
        <table><tr><th>Date</th><th>Member</th><th>In</th><th>Out</th>
        <th>Type</th><th>Location</th><th>Late</th></tr>{rows}</table>
        """
        return layout("Dashboard", body, active="dash")

    def _recent_chart(self, entries, tz) -> str:
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
            pct = max(int((c / peak) * 100), 2)
            top = f"<b>{c}</b>" if c else ""
            bars += f'<div class="bar" style="height:{pct}%">{top}<span>{d[5:]}</span></div>'
        return f'<div class="chart">{bars}</div>'

    def _entry_row(self, e, editable=False) -> str:
        late = ('<span class="badge late">Late</span>' if e.is_late
                else '<span class="badge ontime">On time</span>')
        edit = (f'<td class="actions"><a href="/entry?id={e.id}">Edit</a></td>'
                if editable else "")
        return (
            f"<tr><td>{_e(e.date)}</td><td>{_e(e.member_name)}</td>"
            f"<td>{_e(e.clock_in_time)}</td><td>{_e(e.clock_out_time)}</td>"
            f"<td>{self._type_badge(e.clock_in_type)}</td>"
            f"{self._loc_cell(e)}"
            f"<td>{late}</td>{edit}</tr>"
        )

    def _loc_cell(self, e) -> str:
        if e.latitude is not None and e.longitude is not None:
            url = f"https://www.google.com/maps?q={e.latitude},{e.longitude}"
            return (f'<td><a href="{url}" target="_blank" rel="noopener">'
                    f'\U0001F4CD {e.latitude:.5f}, {e.longitude:.5f}</a></td>')
        return "<td>&mdash;</td>"

    # ================================================================== #
    # Attendance (with edit links)
    # ================================================================== #
    def _parse_filter(self, params):
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
        return member_param, start, end, telegram_id, start_v, end_v

    def _preset_links(self, path: str, keep: dict | None = None) -> str:
        """Quick date-range buttons (Today / Last 7 days / This week / month / All)."""
        from datetime import timedelta
        tz = self.config.tz_offset_hours
        now = timeutil.now(tz)
        today = now.strftime("%Y-%m-%d")
        last7 = (now - timedelta(days=6)).strftime("%Y-%m-%d")
        wk_s, wk_e = timeutil.week_range(now)
        mo_s, mo_e = timeutil.month_range(now)
        keep = keep or {}

        def link(label, frm, to):
            qp = dict(keep, **{"from": frm, "to": to})
            return (f'<a class="btn secondary" href="{path}?'
                    f'{urllib.parse.urlencode(qp)}">{label}</a>')

        return (
            '<div style="margin-bottom:12px; display:flex; gap:8px; flex-wrap:wrap">'
            + link("Today", today, today)
            + link("Last 7 days", last7, today)
            + link("This week", wk_s, wk_e)
            + link("This month", mo_s, mo_e)
            + f'<a class="btn secondary" href="{path}?'
            + urllib.parse.urlencode(dict(keep, **{"from": "", "to": ""}))
            + '">All</a></div>'
        )

    def _attendance_page(self, params) -> bytes:
        member_param, start, end, tid, start_v, end_v = self._parse_filter(params)
        entries = self.db.query_attendance(tid, start_v, end_v)
        options = ['<option value="all">All members</option>']
        for m in self.db.list_members():
            sel = " selected" if str(m.telegram_id) == member_param else ""
            options.append(f'<option value="{m.telegram_id}"{sel}>{_e(m.name)}</option>')
        rows = "".join(self._entry_row(e, editable=True) for e in entries) or \
            '<tr><td colspan="8" class="muted">No records.</td></tr>'
        qs = urllib.parse.urlencode({"member": member_param, "from": start, "to": end})
        body = f"""
        <h1>Attendance</h1>
        {self._flash(params)}
        {self._preset_links("/attendance", {"member": member_param})}
        <form class="filters" method="get" action="/attendance">
          <div><label>Member</label><select name="member">{''.join(options)}</select></div>
          <div><label>From</label><input type="date" name="from" value="{_attr(start)}"></div>
          <div><label>To</label><input type="date" name="to" value="{_attr(end)}"></div>
          <button type="submit">Filter</button>
          <a class="btn secondary" href="/export.csv?{qs}">CSV</a>
          <a class="btn secondary" href="/report.xlsx?{qs}">Excel</a>
        </form>
        <p class="muted">{len(entries)} record(s).</p>
        <table><tr><th>Date</th><th>Member</th><th>In</th><th>Out</th><th>Type</th>
        <th>Location</th><th>Late</th><th></th></tr>{rows}</table>
        """
        return layout("Attendance", body, active="att")

    def _entry_edit_page(self, params) -> bytes:
        try:
            entry_id = int(params.get("id", ["0"])[0])
        except ValueError:
            entry_id = 0
        e = self.db.get_entry(entry_id)
        if e is None:
            return layout("Attendance", "<h1>Entry not found</h1>"
                          "<p><a href='/attendance'>Back</a></p>", active="att")

        def opt(v):
            return " selected" if e.clock_in_type == v else ""
        on_sel = " selected" if not e.is_late else ""
        late_sel = " selected" if e.is_late else ""
        maplink = ""
        if e.latitude is not None and e.longitude is not None:
            maplink = (
                f' &nbsp;<a href="https://www.google.com/maps?q={e.latitude},'
                f'{e.longitude}" target="_blank" rel="noopener">view on map</a>'
            )
        body = f"""
        <h1>Edit attendance entry</h1>
        <p class="muted">{_e(e.member_name)} (ID {e.telegram_id})</p>
        <div class="panel">
          <form class="stack" method="post" action="/entry/update">
            <input type="hidden" name="id" value="{e.id}">
            <div><label>Date</label><input type="date" name="date" value="{_attr(e.date)}"></div>
            <div><label>Arrival / clock-in (YYYY-MM-DD HH:MM:SS)</label>
              <input name="clock_in_time" value="{_attr(e.clock_in_time)}"></div>
            <div><label>Clock-out / out time</label>
              <input name="clock_out_time" value="{_attr(e.clock_out_time)}"></div>
            <div><label>Status</label><select name="is_late">
              <option value="0"{on_sel}>On time</option>
              <option value="1"{late_sel}>Late</option>
            </select></div>
            <div><label>Type</label><select name="clock_in_type">
              <option value=""{opt(None)}>(none)</option>
              <option value="Remote"{opt(TYPE_REMOTE)}>Remote</option>
              <option value="On_Site"{opt(TYPE_ON_SITE)}>On_Site</option>
            </select></div>
            <div><label>Latitude{maplink}</label>
              <input name="latitude" value="{_attr(e.latitude)}"></div>
            <div><label>Longitude</label>
              <input name="longitude" value="{_attr(e.longitude)}"></div>
            <div style="flex-basis:100%"><label>Late remark</label>
              <input name="late_remark" value="{_attr(e.late_remark)}"></div>
            <div><button type="submit">Save</button>
              <a class="btn secondary" href="/attendance">Cancel</a></div>
          </form>
        </div>
        <form method="post" action="/entry/delete"
              onsubmit="return confirm('Delete this entry permanently?')">
          <input type="hidden" name="id" value="{e.id}">
          <button class="btn danger" type="submit">Delete entry</button>
        </form>
        """
        return layout("Edit entry", body, active="att")

    # ================================================================== #
    # Announcements (broadcast to all members)
    # ================================================================== #
    def _announce_page(self, params) -> bytes:
        member_count = len(self.db.list_members())
        history = ""
        for a in self.db.list_announcements(limit=15):
            history += (
                f"<tr><td class='muted'>{_e(a.at)}</td>"
                f"<td>{_e(a.text)}</td>"
                f"<td class='muted'>{a.sent_count}</td></tr>"
            )
        history = history or \
            '<tr><td colspan="3" class="muted">No announcements sent yet.</td></tr>'
        body = f"""
        <h1>Announcements</h1>
        {self._flash(params)}
        <div class="panel">
          <h2>Send an announcement</h2>
          <p class="muted">This is delivered to all {member_count} registered
          member(s) in Telegram right now.</p>
          <form method="post" action="/announce/send"
                onsubmit="return confirm('Send this announcement to all members?')">
            <textarea name="text" rows="4" style="width:100%"
              placeholder="e.g. Reminder: month-end closing is this Friday. Please clock in on time."></textarea>
            <div style="margin-top:12px"><button type="submit">Send to all members</button></div>
          </form>
        </div>
        <div class="panel">
          <h2>Recent announcements</h2>
          <table class="small"><tr><th>When</th><th>Message</th><th>Sent to</th></tr>
          {history}</table>
        </div>
        """
        return layout("Announce", body, active="announce")

    def _send_announcement(self, form) -> None:
        text = form.get("text", "").strip()
        if not text:
            self._flash_redirect("/announce", err="Please write an announcement.")
            return
        client = self.client
        if client is None:
            self._flash_redirect(
                "/announce", err="Messaging is unavailable (bot client not running)."
            )
            return
        message = f"\U0001F4E2 Announcement\n\n{text}"
        sent = 0
        for m in self.db.list_members():
            try:
                client.send_message(m.telegram_id, message)
                sent += 1
            except Exception:
                pass  # skip members who have blocked the bot, etc.
        self.db.add_announcement(
            timeutil.now_iso(self.config.tz_offset_hours), text, sent
        )
        self._audit("announcement.send", f"{sent} recipient(s)")
        self._flash_redirect("/announce", ok=f"Announcement sent to {sent} member(s).")

    # ================================================================== #
    # Members (list / add / edit / delete)
    # ================================================================== #
    def _members_page(self, params) -> bytes:
        members = self.db.list_members()
        rows = ""
        for m in members:
            badge = ('<span class="badge admin">Admin</span>' if m.role == ROLE_ADMIN
                     else '<span class="badge user">Regular</span>')
            rows += (
                f"<tr><td>{_e(m.name)}</td><td>{badge}</td><td>{_e(m.unit)}</td>"
                f"<td>{self._site_name(m.base_site_id)}</td>"
                f"<td class='muted'>{_e(m.telegram_id)}</td>"
                f"<td class='actions'><a href='/member?id={m.telegram_id}'>Edit</a></td></tr>"
            )
        rows = rows or '<tr><td colspan="6" class="muted">No members yet.</td></tr>'
        body = f"""
        <h1>Members</h1>
        {self._flash(params)}
        <div class="panel">
          <h2>Add a member</h2>
          <form class="stack" method="post" action="/members/add">
            <div><label>Telegram ID</label><input name="telegram_id" placeholder="123456789"></div>
            <div><label>Name</label><input name="name" placeholder="Full name"></div>
            <div><label>Unit</label><input name="unit" list="unitlist" placeholder="Department"></div>
            <div><label>Base site</label><select name="base_site_id">{self._site_options(None)}</select></div>
            <div><label>Role</label><select name="role">
              <option value="regular">Regular</option><option value="admin">Admin</option>
            </select></div>
            <div><button type="submit">Add</button></div>
          </form>
          <p class="muted" style="margin-bottom:0">Telegram ID must be the person's
          real numeric ID (from @userinfobot); or they can /register themselves.</p>
        </div>
        <div class="panel">
          <h2>Bulk add members</h2>
          <p class="muted">Columns: <code>telegram_id, name, unit, role</code>.
          Only ID and name are required; role is <code>regular</code> or
          <code>admin</code> (default regular). A header row is ignored.</p>
          <form method="post" action="/members/bulk" enctype="multipart/form-data">
            <p class="muted" style="margin:0 0 4px">Upload an Excel (.xlsx) or CSV file
            &mdash; <a href="/members/template.xlsx">download a template</a>:</p>
            <input type="file" name="file" accept=".xlsx,.csv">
            <p class="muted" style="margin:14px 0 4px">...and / or paste rows here:</p>
            <textarea name="bulk" rows="5" style="width:100%"
              placeholder="123456789, Sok Dara, Engineering, regular
987654321, Chan Nary, Operations, admin"></textarea>
            <div style="margin-top:12px"><button type="submit">Import members</button></div>
          </form>
        </div>
        <table><tr><th>Name</th><th>Role</th><th>Unit</th><th>Base site</th>
        <th>Telegram ID</th><th></th></tr>{rows}</table>
        {self._named_datalists()}
        """
        return layout("Members", body, active="mem")

    def _member_edit_page(self, params) -> bytes:
        try:
            tid = int(params.get("id", ["0"])[0])
        except ValueError:
            tid = 0
        m = self.db.get_member(tid)
        if m is None:
            return layout("Members", "<h1>Member not found</h1>"
                          "<p><a href='/members'>Back</a></p>", active="mem")
        role_admin = " selected" if m.role == ROLE_ADMIN else ""
        role_reg = " selected" if m.role == ROLE_REGULAR else ""
        body = f"""
        <h1>Edit member</h1>
        <p class="muted">Telegram ID {m.telegram_id}</p>
        <div class="panel">
          <form class="stack" method="post" action="/members/update">
            <input type="hidden" name="telegram_id" value="{m.telegram_id}">
            <div><label>Name</label><input name="name" value="{_attr(m.name)}"></div>
            <div><label>Unit</label><input name="unit" list="unitlist" value="{_attr(m.unit)}"></div>
            <div><label>Base site</label>
              <select name="base_site_id">{self._site_options(m.base_site_id)}</select></div>
            <div><label>Role</label><select name="role">
              <option value="regular"{role_reg}>Regular</option>
              <option value="admin"{role_admin}>Admin</option>
            </select></div>
            <div><button type="submit">Save</button>
              <a class="btn secondary" href="/members">Cancel</a></div>
          </form>
        </div>
        {self._named_datalists()}
        <form method="post" action="/members/delete"
              onsubmit="return confirm('Delete this member and ALL their attendance?')">
          <input type="hidden" name="telegram_id" value="{m.telegram_id}">
          <button class="btn danger" type="submit">Delete member</button>
        </form>
        """
        return layout("Edit member", body, active="mem")

    # ================================================================== #
    # Analytics
    # ================================================================== #
    def _analytics_page(self, params) -> bytes:
        tz = self.config.tz_offset_hours
        default_start, default_end = timeutil.month_range(timeutil.now(tz))
        start = params.get("from", [default_start])[0].strip()
        end = params.get("to", [default_end])[0].strip()
        start_v = start if timeutil.is_valid_date(start) else default_start
        end_v = end if timeutil.is_valid_date(end) else default_end

        sched = self.db.get_work_schedule()
        workdays = timeutil.effective_workdays(
            start_v, end_v, sched.days, self.db.holiday_dates()
        )
        entries = self.db.query_attendance(None, start_v, end_v)
        members = {m.telegram_id: m for m in self.db.list_members()}

        agg = {}
        for e in entries:
            a = agg.setdefault(e.telegram_id, {
                "hours": 0.0, "days": set(), "late": 0, "onsite": 0, "remote": 0})
            a["hours"] += timeutil.duration_hours(e.clock_in_time, e.clock_out_time)
            a["days"].add(e.date)
            a["late"] += 1 if e.is_late else 0
            if e.clock_in_type == TYPE_ON_SITE:
                a["onsite"] += 1
            elif e.clock_in_type == TYPE_REMOTE:
                a["remote"] += 1

        rows = ""
        total_hours = 0.0
        total_late = 0
        for tid, m in sorted(members.items(), key=lambda kv: kv[1].name.lower()):
            a = agg.get(tid)
            if not a:
                continue
            days = len(a["days"])
            rate = f"{(days / workdays * 100):.0f}%" if workdays else "&mdash;"
            total_hours += a["hours"]
            total_late += a["late"]
            rows += (
                f"<tr><td>{_e(m.name)}</td><td>{_e(m.unit)}</td>"
                f"<td>{self._site_name(m.base_site_id)}</td><td>{days}</td>"
                f"<td>{timeutil.format_hours(a['hours'])}</td><td>{a['late']}</td>"
                f"<td>{a['onsite']}</td><td>{a['remote']}</td><td>{rate}</td></tr>"
            )
        rows = rows or '<tr><td colspan="9" class="muted">No data in range.</td></tr>'
        cards = [
            (len([1 for a in agg.values()]), "Members active"),
            (timeutil.format_hours(total_hours), "Total hours"),
            (total_late, "Late arrivals"),
            (workdays, "Work days in range"),
        ]
        cards_html = "".join(
            f'<div class="card"><div class="num">{n}</div>'
            f'<div class="lbl">{_e(lbl)}</div></div>' for n, lbl in cards)
        body = f"""
        <h1>Analytics</h1>
        {self._preset_links("/analytics")}
        <form class="filters" method="get" action="/analytics">
          <div><label>From</label><input type="date" name="from" value="{_attr(start_v)}"></div>
          <div><label>To</label><input type="date" name="to" value="{_attr(end_v)}"></div>
          <button type="submit">Apply</button>
          <a class="btn secondary" href="/report.xlsx?from={_attr(start_v)}&to={_attr(end_v)}">Excel report</a>
        </form>
        <div class="cards">{cards_html}</div>
        <h2>Per-member ({_e(start_v)} to {_e(end_v)})</h2>
        <table><tr><th>Member</th><th>Unit</th><th>Base site</th><th>Days</th>
        <th>Hours</th><th>Late</th><th>On-site</th><th>Remote</th><th>Attendance</th></tr>
        {rows}</table>
        """
        return layout("Analytics", body, active="analytics")

    # ================================================================== #
    # Map
    # ================================================================== #
    def _map_page(self, params) -> bytes:
        sites = [{"name": s.name, "lat": s.latitude, "lon": s.longitude}
                 for s in self.db.list_sites()]
        points = []
        for e in self.db.query_attendance():
            if e.latitude is not None and e.longitude is not None:
                points.append({"name": e.member_name, "date": e.date,
                               "lat": e.latitude, "lon": e.longitude})
        points = points[-300:]
        center = sites[0] if sites else (points[-1] if points else {"lat": 11.5564, "lon": 104.9282})
        data = json.dumps({"sites": sites, "points": points, "center": center})
        head = (
            '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>'
            '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'
        )
        body = f"""
        <h1>Map</h1>
        {self._flash(params)}
        <p class="muted">Green = configured sites, blue = on-site clock-in points.
        Click the map to fill the coordinates below, then add a site.</p>
        <div id="map"></div>
        <div class="panel" style="margin-top:16px">
          <h2>Add a site</h2>
          <form class="stack" method="post" action="/sites/add">
            <div><label>Name</label><input name="name" placeholder="e.g. HQ" required></div>
            <div><label>Latitude</label><input name="latitude" id="lat" required></div>
            <div><label>Longitude</label><input name="longitude" id="lon" required></div>
            <div><button type="submit">Add site</button></div>
          </form>
        </div>
        <script>
        var D = {data};
        var c = D.center;
        var map = L.map('map').setView([c.lat, c.lon], 15);
        L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
          {{maxZoom: 19, attribution: '&copy; OpenStreetMap'}}).addTo(map);
        D.sites.forEach(function(s) {{
          L.circleMarker([s.lat, s.lon], {{color:'#34d399', radius:9}})
            .addTo(map).bindPopup('Site: ' + s.name);
        }});
        D.points.forEach(function(p) {{
          L.circleMarker([p.lat, p.lon], {{color:'#38bdf8', radius:5}})
            .addTo(map).bindPopup(p.name + '<br>' + p.date);
        }});
        map.on('click', function(ev) {{
          document.getElementById('lat').value = ev.latlng.lat.toFixed(6);
          document.getElementById('lon').value = ev.latlng.lng.toFixed(6);
        }});
        </script>
        """
        return layout("Map", body, active="map", head_extra=head)

    # ================================================================== #
    # Settings (location, sites, schedule, audit)
    # ================================================================== #
    def _settings_page(self, params) -> bytes:
        loc = self.db.get_configured_location()
        latv, lonv = (str(loc[0]), str(loc[1])) if loc else ("", "")
        radius = self.db.get_geofence_radius(self.config.geofence_radius_meters)

        sites = self.db.list_sites()
        site_rows = ""
        for s in sites:
            site_rows += (
                f"<tr><td>{_e(s.name)}</td><td>{_e(s.latitude)}</td>"
                f"<td>{_e(s.longitude)}</td><td class='actions'>"
                f"<form method='post' action='/sites/delete' style='display:inline'"
                f" onsubmit=\"return confirm('Delete site {_attr(s.name)}?')\">"
                f"<input type='hidden' name='id' value='{s.id}'>"
                f"<button class='btn danger' type='submit'>Delete</button></form></td></tr>"
            )
        site_rows = site_rows or '<tr><td colspan="4" class="muted">No sites yet.</td></tr>'

        def _named_rows(items, kind):
            out = ""
            for it in items:
                out += (
                    f"<tr><td>{_e(it.name)}</td><td class='actions'>"
                    f"<form method='post' action='/{kind}/delete' style='display:inline'"
                    f" onsubmit=\"return confirm('Delete {_attr(it.name)}?')\">"
                    f"<input type='hidden' name='id' value='{it.id}'>"
                    f"<button class='btn danger' type='submit'>Delete</button></form></td></tr>"
                )
            return out or '<tr><td colspan="2" class="muted">None yet.</td></tr>'

        unit_rows = _named_rows(self.db.list_units(), "units")

        holiday_rows = ""
        for h in self.db.list_holidays():
            holiday_rows += (
                f"<tr><td>{_e(h.date)}</td><td>{_e(h.name)}</td>"
                f"<td class='actions'>"
                f"<form method='post' action='/holidays/delete' style='display:inline'"
                f" onsubmit=\"return confirm('Delete holiday {_attr(h.date)}?')\">"
                f"<input type='hidden' name='date' value='{_attr(h.date)}'>"
                f"<button class='btn danger' type='submit'>Delete</button></form></td></tr>"
            )
        holiday_rows = holiday_rows or \
            '<tr><td colspan="3" class="muted">No holidays configured.</td></tr>'

        sched = self.db.get_work_schedule()
        day_checks = ""
        for i, name in enumerate(timeutil.WEEKDAY_NAMES):
            checked = "checked" if i in sched.days else ""
            day_checks += (f'<label><input type="checkbox" name="day_{i}" value="1" '
                           f'{checked}> {name}</label>')
        rem_checked = "checked" if sched.reminders_enabled else ""
        auto_checked = "checked" if sched.auto_clockout_enabled else ""

        audit = self.db.list_audit(limit=15)
        audit_rows = "".join(
            f"<tr><td class='muted'>{_e(a['at'])}</td><td>{_e(a['action'])}</td>"
            f"<td>{_e(a['detail'])}</td></tr>" for a in audit
        ) or '<tr><td colspan="3" class="muted">No changes recorded yet.</td></tr>'

        body = f"""
        <h1>Settings</h1>
        {self._flash(params)}
        <div class="row2">
          <div class="panel">
            <h2>Default on-site location</h2>
            <p class="muted">Used when no named sites exist. Manage multiple sites
            on the <a href="/map">Map</a> page.</p>
            <form class="stack" method="post" action="/settings/location">
              <div><label>Latitude</label><input name="latitude" value="{_attr(latv)}"></div>
              <div><label>Longitude</label><input name="longitude" value="{_attr(lonv)}"></div>
              <div><button type="submit">Save</button></div>
            </form>
            <h2 style="margin-top:18px">Geofence radius</h2>
            <form class="stack" method="post" action="/settings/geofence">
              <div><label>Radius (meters)</label>
                <input name="radius" value="{radius:.0f}"></div>
              <div><button type="submit">Save radius</button></div>
            </form>
            <p class="muted" style="margin-bottom:0">On-site clock-ins must be
            within this distance of a site.</p>
          </div>
          <div class="panel">
            <h2>Work schedule</h2>
            <form method="post" action="/settings/schedule">
              <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:end">
                <div><label>Start</label><input type="time" name="start" value="{_attr(sched.start)}"></div>
                <div><label>End</label><input type="time" name="end" value="{_attr(sched.end)}"></div>
                <div><label>Late grace (min)</label>
                  <input type="number" name="grace" min="0" value="{sched.grace_minutes}"></div>
              </div>
              <p class="muted" style="margin:12px 0 4px">Working days</p>
              <div class="checks">{day_checks}</div>
              <p class="muted" style="margin:12px 0 4px">Reminders &amp; auto clock-out</p>
              <div class="checks">
                <label><input type="checkbox" name="reminders" value="1"
                  {rem_checked}> Daily reminders</label>
                <label><input type="checkbox" name="auto_clockout" value="1"
                  {auto_checked}> Auto clock-out at</label>
                <input type="time" name="auto_clockout_time" value="{_attr(sched.auto_clockout_time)}"
                  style="width:auto">
              </div>
              <div style="margin-top:14px"><button type="submit">Save schedule</button></div>
            </form>
            <p class="muted" style="margin-bottom:0">"Late grace" = minutes after
            the start time before a clock-in is marked late.</p>
          </div>
        </div>
        <div class="panel">
          <h2>Recalculate late flags</h2>
          <p class="muted">Re-evaluate every attendance record against the current
          work schedule and grace period (fixes records wrongly marked late/on-time).</p>
          <form method="post" action="/settings/recalc-late"
                onsubmit="return confirm('Recalculate late flags for all records?')">
            <button type="submit">Recalculate now</button>
          </form>
        </div>
        <div class="panel">
          <h2>Sites</h2>
          <table class="small"><tr><th>Name</th><th>Latitude</th><th>Longitude</th><th></th></tr>
          {site_rows}</table>
          <p class="muted" style="margin-bottom:0">Add sites on the
          <a href="/map">Map</a> page (click the map to grab coordinates).</p>
        </div>
        <div class="panel">
          <h2>Units / departments</h2>
          <p class="muted">Members select from this list when registering.</p>
          <form class="stack" method="post" action="/units/add">
            <div><label>Unit name</label><input name="name" placeholder="e.g. Operations"></div>
            <div><button type="submit">Add unit</button></div>
          </form>
          <table class="small" style="margin-top:12px"><tr><th>Name</th><th></th></tr>
          {unit_rows}</table>
        </div>
        <div class="panel">
          <h2>Public holidays</h2>
          <p class="muted">On these dates nobody is marked late/absent, clock-in
          reminders are paused, and members get a holiday notice instead.</p>
          <form class="stack" method="post" action="/holidays/add">
            <div><label>From</label><input type="date" name="date"></div>
            <div><label>To (optional, for multi-day)</label>
              <input type="date" name="date_to"></div>
            <div><label>Name</label><input name="name" placeholder="e.g. Khmer New Year"></div>
            <div><button type="submit">Add holiday</button></div>
          </form>
          <table class="small" style="margin-top:12px">
          <tr><th>Date</th><th>Name</th><th></th></tr>{holiday_rows}</table>
        </div>
        <div class="panel">
          <h2>Recent changes (audit log)</h2>
          <table class="small"><tr><th>When</th><th>Action</th><th>Detail</th></tr>
          {audit_rows}</table>
        </div>
        """
        return layout("Settings", body, active="set")

    # ================================================================== #
    # Write actions
    # ================================================================== #
    def _add_member(self, form) -> None:
        tid = form.get("telegram_id", "").strip()
        name = form.get("name", "").strip()
        role = form.get("role", ROLE_REGULAR).strip()
        unit = form.get("unit", "").strip() or None
        base = self._parse_site_id(form.get("base_site_id"))
        if not tid.lstrip("-").isdigit() or not name:
            self._flash_redirect("/members", err="Provide a numeric Telegram ID and a name.")
            return
        if role not in (ROLE_ADMIN, ROLE_REGULAR):
            role = ROLE_REGULAR
        tid_int = int(tid)
        if self.db.get_member(tid_int) is not None:
            self._flash_redirect("/members", err="A member with that Telegram ID exists.")
            return
        self.db.create_member(tid_int, name, role, None,
                              timeutil.now_iso(self.config.tz_offset_hours), unit, base)
        self._audit("member.add", f"{name} ({tid_int})")
        self._flash_redirect("/members", ok=f"Added member: {name}.")

    def _bulk_add_members(self, form) -> None:
        """Bulk-create members from pasted text and/or an uploaded Excel/CSV file.

        Row format: ``telegram_id, name, unit, role`` (only the ID and name are
        required). A header row, blank lines and existing IDs are ignored.
        """
        rows: list[list[str]] = []

        # 1) Pasted text (comma or tab separated).
        text = form.get("bulk", "")
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append([p.strip() for p in line.replace("\t", ",").split(",")])

        # 2) Uploaded file (.xlsx or .csv/text).
        file_bytes = form.get("file")
        filename = (form.get("file_filename") or "").lower()
        if isinstance(file_bytes, bytes) and file_bytes:
            if filename.endswith(".xlsx"):
                rows.extend([[str(c).strip() for c in r] for r in read_xlsx(file_bytes)])
            else:
                content = file_bytes.decode("utf-8", "replace")
                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        rows.append([p.strip() for p in line.replace("\t", ",").split(",")])

        added, skipped, errors = self._process_member_rows(rows)
        self._audit("member.bulk", f"added={added} skipped={skipped} errors={errors}")
        self._flash_redirect(
            "/members",
            ok=f"Bulk import complete: {added} added, {skipped} already existed, "
            f"{errors} invalid row(s).",
        )

    def _process_member_rows(self, rows) -> tuple[int, int, int]:
        now = timeutil.now_iso(self.config.tz_offset_hours)
        added = skipped = errors = 0
        for parts in rows:
            tid = parts[0].strip() if parts else ""
            if not tid.lstrip("-").isdigit():
                # Ignore an obvious header row; otherwise count as an error.
                if tid.lower() not in ("telegram_id", "id", "telegram id", "telegramid"):
                    if tid or any(p for p in parts[1:]):
                        errors += 1
                continue
            name = parts[1].strip() if len(parts) > 1 else ""
            if not name:
                errors += 1
                continue
            unit = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
            role = parts[3].strip().lower() if len(parts) > 3 and parts[3].strip() else ROLE_REGULAR
            if role not in (ROLE_ADMIN, ROLE_REGULAR):
                role = ROLE_REGULAR
            tid_int = int(tid)
            if self.db.get_member(tid_int) is not None:
                skipped += 1
                continue
            self.db.create_member(tid_int, name, role, None, now, unit, None)
            added += 1
        return added, skipped, errors

    def _update_member(self, form) -> None:
        tid = form.get("telegram_id", "").strip()
        if not tid.lstrip("-").isdigit() or self.db.get_member(int(tid)) is None:
            self._flash_redirect("/members", err="Member not found.")
            return
        name = form.get("name", "").strip()
        if not name:
            self._flash_redirect(f"/member?id={tid}", err="Name cannot be empty.")
            return
        role = form.get("role", ROLE_REGULAR).strip()
        if role not in (ROLE_ADMIN, ROLE_REGULAR):
            role = ROLE_REGULAR
        self.db.update_member(
            int(tid), name, role, None,
            form.get("unit", "").strip() or None,
            self._parse_site_id(form.get("base_site_id")),
        )
        self._audit("member.update", f"{name} ({tid})")
        self._flash_redirect("/members", ok=f"Updated {name}.")

    def _delete_member(self, form) -> None:
        tid = form.get("telegram_id", "").strip()
        if tid.lstrip("-").isdigit() and self.db.get_member(int(tid)) is not None:
            m = self.db.get_member(int(tid))
            self.db.delete_member(int(tid))
            self._audit("member.delete", f"{m.name} ({tid})")
            self._flash_redirect("/members", ok="Member deleted.")
        else:
            self._flash_redirect("/members", err="Member not found.")

    def _update_entry(self, form) -> None:
        try:
            entry_id = int(form.get("id", "0"))
        except ValueError:
            entry_id = 0
        if self.db.get_entry(entry_id) is None:
            self._flash_redirect("/attendance", err="Entry not found.")
            return
        date = form.get("date", "").strip()
        if not timeutil.is_valid_date(date):
            self._flash_redirect(f"/entry?id={entry_id}", err="Invalid date.")
            return

        def _coord(name):
            raw = form.get(name, "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        self.db.update_entry(
            entry_id, date,
            form.get("clock_in_time", "").strip() or None,
            form.get("clock_out_time", "").strip() or None,
            form.get("clock_in_type", "").strip() or None,
            None,
            form.get("late_remark", "").strip() or None,
            1 if form.get("is_late") == "1" else 0,
            _coord("latitude"),
            _coord("longitude"),
        )
        self._audit("entry.update", f"entry {entry_id} on {date}")
        self._flash_redirect("/attendance", ok="Entry updated.")

    def _delete_entry(self, form) -> None:
        try:
            entry_id = int(form.get("id", "0"))
        except ValueError:
            entry_id = 0
        if self.db.get_entry(entry_id) is None:
            self._flash_redirect("/attendance", err="Entry not found.")
            return
        self.db.delete_entry(entry_id)
        self._audit("entry.delete", f"entry {entry_id}")
        self._flash_redirect("/attendance", ok="Entry deleted.")

    def _add_site(self, form) -> None:
        name = form.get("name", "").strip()
        try:
            lat = float(form.get("latitude", "").strip())
            lon = float(form.get("longitude", "").strip())
        except ValueError:
            self._flash_redirect("/map", err="Latitude and longitude must be numbers.")
            return
        if not name:
            self._flash_redirect("/map", err="Please give the site a name.")
            return
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            self._flash_redirect("/map", err="Coordinates out of range.")
            return
        self.db.add_site(name, lat, lon, timeutil.now_iso(self.config.tz_offset_hours))
        self._audit("site.add", f"{name} ({lat}, {lon})")
        self._flash_redirect("/map", ok=f"Site '{name}' added.")

    def _delete_site(self, form) -> None:
        try:
            site_id = int(form.get("id", "0"))
        except ValueError:
            site_id = 0
        site = self.db.get_site(site_id)
        if site is None:
            self._flash_redirect("/settings", err="Site not found.")
            return
        self.db.delete_site(site_id)
        self._audit("site.delete", site.name)
        self._flash_redirect("/settings", ok=f"Site '{site.name}' deleted.")

    def _add_unit(self, form) -> None:
        name = form.get("name", "").strip()
        if not name:
            self._flash_redirect("/settings", err="Please enter a unit name.")
            return
        self.db.add_unit(name)
        self._audit("unit.add", name)
        self._flash_redirect("/settings", ok=f"Unit '{name}' added.")

    def _delete_unit(self, form) -> None:
        try:
            uid = int(form.get("id", "0"))
        except ValueError:
            uid = 0
        item = self.db.get_unit(uid)
        if item is None:
            self._flash_redirect("/settings", err="Unit not found.")
            return
        self.db.delete_unit(uid)
        self._audit("unit.delete", item.name)
        self._flash_redirect("/settings", ok=f"Unit '{item.name}' deleted.")

    def _add_holiday(self, form) -> None:
        start = form.get("date", "").strip()
        end = form.get("date_to", "").strip() or start
        name = form.get("name", "").strip()
        if not timeutil.is_valid_date(start) or not timeutil.is_valid_date(end):
            self._flash_redirect("/settings", err="Please provide valid date(s).")
            return
        if not name:
            self._flash_redirect("/settings", err="Please name the holiday.")
            return
        dates = timeutil.dates_in_range(start, end)
        if not dates:
            self._flash_redirect(
                "/settings", err="The 'To' date must be on or after the 'From' date."
            )
            return
        if len(dates) > 366:
            self._flash_redirect("/settings", err="Holiday range is too long.")
            return
        for d in dates:
            self.db.add_holiday(d, name)
        span = start if len(dates) == 1 else f"{start} to {end}"
        self._audit("holiday.add", f"{span} {name} ({len(dates)} day(s))")
        self._flash_redirect(
            "/settings", ok=f"Added {len(dates)} holiday day(s): {name} ({span})."
        )

    def _delete_holiday(self, form) -> None:
        date = form.get("date", "").strip()
        if not self.db.is_holiday(date):
            self._flash_redirect("/settings", err="Holiday not found.")
            return
        self.db.delete_holiday(date)
        self._audit("holiday.delete", date)
        self._flash_redirect("/settings", ok=f"Holiday {date} deleted.")

    def _set_location(self, form) -> None:
        try:
            lat = float(form.get("latitude", "").strip())
            lon = float(form.get("longitude", "").strip())
        except ValueError:
            self._flash_redirect("/settings", err="Latitude and longitude must be numbers.")
            return
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            self._flash_redirect("/settings", err="Coordinates out of range.")
            return
        self.db.set_configured_location(lat, lon)
        self._audit("location.set", f"{lat}, {lon}")
        self._flash_redirect("/settings", ok="Default location updated.")

    def _set_schedule(self, form) -> None:
        start = form.get("start", "").strip()
        end = form.get("end", "").strip()
        if not timeutil.is_valid_hhmm(start) or not timeutil.is_valid_hhmm(end):
            self._flash_redirect("/settings", err="Start/end must be valid HH:MM times.")
            return
        days = {i for i in range(7) if form.get(f"day_{i}") == "1"}
        reminders = form.get("reminders") == "1"
        auto = form.get("auto_clockout") == "1"
        auto_time = form.get("auto_clockout_time", "").strip()
        if not timeutil.is_valid_hhmm(auto_time):
            auto_time = "23:59"
        try:
            grace = max(0, int(form.get("grace", "0") or "0"))
        except ValueError:
            grace = 0
        self.db.set_work_schedule(start, end, days, reminders, grace, auto, auto_time)
        self._audit(
            "schedule.set",
            f"{start}-{end} days={sorted(days)} grace={grace} rem={reminders} "
            f"auto={auto}@{auto_time}",
        )
        self._flash_redirect("/settings", ok="Work schedule saved.")

    def _set_geofence(self, form) -> None:
        try:
            radius = float(form.get("radius", "").strip())
        except ValueError:
            self._flash_redirect("/settings", err="Radius must be a number.")
            return
        if radius <= 0 or radius > 100000:
            self._flash_redirect("/settings", err="Radius must be between 1 and 100000 m.")
            return
        self.db.set_geofence_radius(radius)
        self._audit("geofence.set", f"{radius:.0f} m")
        self._flash_redirect("/settings", ok=f"Geofence radius set to {radius:.0f} m.")

    def _recalc_late(self, form) -> None:
        sched = self.db.get_work_schedule()
        changed = 0
        for e in self.db.query_attendance():
            dt = timeutil.parse_dt(e.clock_in_time)
            if dt is None:
                continue
            wd = timeutil.weekday_of(e.date)
            late = 1 if (
                wd in sched.days
                and timeutil.is_after_with_grace(dt, sched.start, sched.grace_minutes)
            ) else 0
            if late != (e.is_late or 0):
                self.db.set_is_late(e.id, late)
                changed += 1
        self._audit("late.recalc", f"{changed} record(s) updated")
        self._flash_redirect("/settings", ok=f"Recalculated late flags: {changed} updated.")

    # ================================================================== #
    # Exports
    # ================================================================== #
    def _report_rows(self, entries):
        for e in entries:
            yield [
                e.member_name, e.date, e.clock_in_time or "", e.clock_out_time or "",
                round(timeutil.duration_hours(e.clock_in_time, e.clock_out_time), 2),
                e.clock_in_type or "",
                "Yes" if e.is_late else "No", e.late_remark or "",
            ]

    def _export_csv(self, params) -> None:
        _mp, _s, _en, tid, sv, ev = self._parse_filter(params)
        entries = self.db.query_attendance(tid, sv, ev)
        stamp = timeutil.today_iso(self.config.tz_offset_hours)
        self._send(
            200, build_csv(entries), "text/csv; charset=utf-8",
            [("Content-Disposition", f'attachment; filename="attendance_{stamp}.csv"')],
        )

    def _member_template(self) -> None:
        header = ["telegram_id", "name", "unit", "role"]
        example = [
            [123456789, "Sok Dara", "Engineering", "regular"],
            [987654321, "Chan Nary", "Operations", "admin"],
        ]
        data = build_xlsx(header, example, sheet_name="Members")
        self._send(
            200, data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            [("Content-Disposition", 'attachment; filename="members_template.xlsx"')],
        )

    def _export_xlsx(self, params) -> None:
        _mp, _s, _en, tid, sv, ev = self._parse_filter(params)
        entries = self.db.query_attendance(tid, sv, ev)
        header = ["Member", "Date", "Clock In", "Clock Out", "Hours", "Type",
                  "Late", "Late Remark"]
        data = build_xlsx(header, self._report_rows(entries), sheet_name="Attendance")
        stamp = sv or timeutil.today_iso(self.config.tz_offset_hours)
        self._send(
            200, data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            [("Content-Disposition", f'attachment; filename="attendance_{stamp}.xlsx"')],
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_site_id(raw):
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None


# --------------------------------------------------------------------- #
# Server bootstrap
# --------------------------------------------------------------------- #
def start_web_portal(config: Config, client=None) -> ThreadingHTTPServer:
    """Start the admin portal in a daemon thread; returns the server.

    The portal uses its own database connection (a separate Database instance
    on the same file) so it never contends with the bot thread's connection.
    ``client`` is the Telegram client used to broadcast announcements.
    """
    server = ThreadingHTTPServer(("0.0.0.0", config.health_port), _PortalHandler)
    server.ctx_config = config  # type: ignore[attr-defined]
    server.ctx_db = Database(config.db_path)  # type: ignore[attr-defined]
    server.ctx_client = client  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, name="web-portal", daemon=True)
    thread.start()
    logger.info("Admin web portal listening on 0.0.0.0:%d", config.health_port)
    return server
