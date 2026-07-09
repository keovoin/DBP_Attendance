"""Command handlers and conversation state machine.

The :class:`AttendanceBot` class turns raw Telegram updates into attendance
actions. It is decoupled from the network layer: it depends only on a
``TelegramClient``-like object and a :class:`Database`, so its logic can be
exercised in tests with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import geo, timeutil
from .config import Config
from .db import (
    ROLE_ADMIN,
    ROLE_REGULAR,
    TYPE_ON_SITE,
    TYPE_REMOTE,
    Database,
    Member,
    Site,
)
from .reports import build_csv, render_text
from .telegram_api import (
    inline_keyboard,
    location_request_keyboard,
    remove_keyboard,
)

# Conversation states
STATE_AWAITING_REALNAME = "awaiting_realname"
STATE_AWAITING_UNIT = "awaiting_unit"
STATE_AWAITING_BASE = "awaiting_base"
STATE_AWAITING_ONSITE_LOCATION = "awaiting_onsite_location"
STATE_AWAITING_LATE_REMARK = "awaiting_late_remark"

HELP_TEXT = (
    "\U0001F4CB *Attendance Tracker*\n\n"
    "/register - Register (set your real name, unit and base location)\n"
    "/clockin - Clock in (choose Remote or On_Site)\n"
    "/clockout - Clock out for today\n"
    "/status - See if you are currently clocked in\n"
    "/summary [week|month] - Your hours and attendance summary\n"
    "/setname <full name> - Update your real name\n"
    "/setunit <unit> - Set your unit/department\n"
    "/setbase - Choose your base location\n"
    "/remark <YYYY-MM-DD> <text> - Add a late remark for a date\n"
    "/view [member <name>] [from <date>] [to <date>] - View attendance\n"
    "/export [member <name>] [from <date>] [to <date>] - Download CSV report\n"
    "/whoami - Show your profile\n"
    "/help - Show this message\n\n"
    "*Admin only*\n"
    "/setlocation <lat> <lon> - Set the default on-site location\n"
    "/promote <name|telegram_id> - Grant Admin to a member\n"
)


@dataclass
class ConversationState:
    name: str
    data: dict = field(default_factory=dict)


class AttendanceBot:
    def __init__(self, client, db: Database, config: Config) -> None:
        self.client = client
        self.db = db
        self.config = config
        self.states: dict[int, ConversationState] = {}

    @property
    def tz(self) -> float:
        return self.config.tz_offset_hours

    # ------------------------------------------------------------------ #
    # Update dispatch
    # ------------------------------------------------------------------ #
    def handle_update(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                self._handle_callback_query(update["callback_query"])
            elif "message" in update:
                self._handle_message(update["message"])
        except Exception as exc:  # pragma: no cover - defensive guard
            chat_id = self._extract_chat_id(update)
            if chat_id is not None:
                try:
                    self._send(chat_id, f"Sorry, something went wrong: {exc}")
                except Exception:
                    pass

    @staticmethod
    def _extract_chat_id(update: dict) -> Optional[int]:
        if "message" in update:
            return update["message"].get("chat", {}).get("id")
        if "callback_query" in update:
            return update["callback_query"].get("message", {}).get("chat", {}).get("id")
        return None

    # ------------------------------------------------------------------ #
    # Message handling
    # ------------------------------------------------------------------ #
    def _handle_message(self, message: dict) -> None:
        chat_id = message.get("chat", {}).get("id")
        from_user = message.get("from", {})
        user_id = from_user.get("id")
        if chat_id is None or user_id is None:
            return

        if "location" in message:
            self._handle_location(chat_id, user_id, message["location"])
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        if not text.startswith("/"):
            self._handle_conversation_text(chat_id, user_id, from_user, text)
            return

        command, _, arg_str = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        arg_str = arg_str.strip()

        dispatch = {
            "/start": lambda: self._cmd_register(chat_id, user_id, from_user),
            "/register": lambda: self._cmd_register(chat_id, user_id, from_user),
            "/help": lambda: self._send(chat_id, HELP_TEXT, parse_mode="Markdown"),
            "/whoami": lambda: self._cmd_whoami(chat_id, user_id),
            "/setname": lambda: self._cmd_set_name(chat_id, user_id, arg_str),
            "/setunit": lambda: self._cmd_set_unit(chat_id, user_id, arg_str),
            "/setbase": lambda: self._cmd_set_base(chat_id, user_id),
            "/clockin": lambda: self._cmd_clock_in(chat_id, user_id, from_user),
            "/clockout": lambda: self._cmd_clock_out(chat_id, user_id),
            "/status": lambda: self._cmd_status(chat_id, user_id),
            "/summary": lambda: self._cmd_summary(chat_id, user_id, arg_str),
            "/remark": lambda: self._cmd_remark(chat_id, user_id, arg_str),
            "/view": lambda: self._cmd_view(chat_id, user_id, arg_str),
            "/export": lambda: self._cmd_export(chat_id, user_id, arg_str),
            "/setlocation": lambda: self._cmd_set_location(chat_id, user_id, arg_str),
            "/promote": lambda: self._cmd_promote(chat_id, user_id, arg_str),
        }
        handler = dispatch.get(command)
        if handler is None:
            self._send(chat_id, "Unknown command. Send /help to see what I can do.")
        else:
            handler()

    def _handle_conversation_text(
        self, chat_id: int, user_id: int, from_user: dict, text: str
    ) -> None:
        state = self.states.get(user_id)
        if state is None:
            self._send(
                chat_id,
                "I didn't understand that. Send /help to see available commands.",
            )
            return
        if state.name == STATE_AWAITING_REALNAME:
            self._complete_realname(chat_id, user_id, text, state)
        elif state.name == STATE_AWAITING_UNIT:
            self._complete_unit(chat_id, user_id, text, state)
        elif state.name == STATE_AWAITING_LATE_REMARK:
            self._complete_late_remark(chat_id, user_id, text, state)
        elif state.name == STATE_AWAITING_ONSITE_LOCATION:
            self._send(
                chat_id,
                "I'm waiting for your location. Tap the \U0001F4CD button to share "
                "it, or send /clockin again to restart.",
            )
        else:
            self.states.pop(user_id, None)

    # ------------------------------------------------------------------ #
    # Registration wizard
    # ------------------------------------------------------------------ #
    def _telegram_name(self, from_user: dict) -> str:
        first = (from_user.get("first_name") or "").strip()
        last = (from_user.get("last_name") or "").strip()
        full = (first + " " + last).strip()
        return full or (from_user.get("username") or f"User {from_user.get('id')}")

    def _cmd_register(self, chat_id: int, user_id: int, from_user: dict) -> None:
        existing = self.db.get_member(user_id)
        if existing is not None:
            self._send(
                chat_id,
                "You are already registered. Your existing record is unchanged.\n"
                "Use /whoami to review it, or /setname, /setunit, /setbase "
                "to update details.",
            )
            return

        role = (
            ROLE_ADMIN if user_id in self.config.admin_telegram_ids else ROLE_REGULAR
        )
        self.db.create_member(
            telegram_id=user_id,
            name=self._telegram_name(from_user),
            role=role,
            created_at=timeutil.now_iso(self.tz),
        )
        self.states[user_id] = ConversationState(
            STATE_AWAITING_REALNAME, {"flow": "register"}
        )
        role_note = " (You have Admin access.)" if role == ROLE_ADMIN else ""
        self._send(
            chat_id,
            f"Welcome!{role_note}\n\nLet's set up your profile. "
            "First, what is your *full real name*? "
            "(Your Telegram name may be a nickname; reports use this name.)",
            parse_mode="Markdown",
        )

    def _complete_realname(
        self, chat_id: int, user_id: int, text: str, state: ConversationState
    ) -> None:
        name = text.strip()
        if not name:
            self._send(chat_id, "Please send a non-empty name.")
            return
        self.db.update_name(user_id, name)
        self._send(chat_id, f"Thanks, {name}!")
        self._prompt_unit(chat_id, user_id, flow="register")

    # --- unit (select from admin list, or type) ------------------------
    def _prompt_unit(self, chat_id: int, user_id: int, flow) -> None:
        units = self.db.list_units()
        self.states[user_id] = ConversationState(STATE_AWAITING_UNIT, {"flow": flow})
        if units:
            rows = [[(u.name, f"unit:{u.id}")] for u in units]
            self._send(
                chat_id,
                "Select your *unit / department* (or type a name):",
                reply_markup=inline_keyboard(rows),
                parse_mode="Markdown",
            )
        else:
            self._send(
                chat_id,
                "Which *unit / department* are you from? Reply with its name.",
                parse_mode="Markdown",
            )

    def _complete_unit(
        self, chat_id: int, user_id: int, text: str, state: ConversationState
    ) -> None:
        unit = text.strip()
        if not unit:
            self._send(chat_id, "Please send a non-empty unit/department name.")
            return
        self._apply_unit(chat_id, user_id, unit, state)

    def _apply_unit(
        self, chat_id: int, user_id: int, unit: str, state: ConversationState
    ) -> None:
        self.db.set_unit(user_id, unit)
        if state.data.get("flow") == "register":
            self._prompt_base_selection(chat_id, user_id, flow="register")
        else:
            self.states.pop(user_id, None)
            self._send(chat_id, f"Unit set to: {unit}")

    def _prompt_base_selection(self, chat_id: int, user_id: int, flow: str) -> None:
        sites = self.db.list_sites()
        if not sites:
            self.states.pop(user_id, None)
            if flow == "register":
                self._finish_registration(chat_id, user_id, base_note=(
                    "No base locations are configured yet - an Admin can set "
                    "yours later."
                ))
            else:
                self._send(
                    chat_id,
                    "No base locations are configured yet. Ask an Admin to add "
                    "sites first.",
                )
            return
        rows = [[(s.name, f"base:{s.id}")] for s in sites]
        rows.append([("(None / not based at a site)", "base:0")])
        self.states[user_id] = ConversationState(STATE_AWAITING_BASE, {"flow": flow})
        self._send(
            chat_id,
            "Finally, choose your *base location*:" if flow == "register"
            else "Choose your *base location*:",
            reply_markup=inline_keyboard(rows),
            parse_mode="Markdown",
        )

    def _finish_registration(
        self, chat_id: int, user_id: int, base_note: str = ""
    ) -> None:
        member = self.db.get_member(user_id)
        self.states.pop(user_id, None)
        base_name = self._base_site_name(member)
        lines = [
            "\u2705 You're all set!",
            f"Name: {member.name}",
            f"Unit: {member.unit or '(not set)'}",
            f"Base location: {base_name}",
        ]
        if base_note:
            lines.append("")
            lines.append(base_note)
        lines.append("")
        lines.append("You can now /clockin. Send /help for all commands.")
        self._send(chat_id, "\n".join(lines))

    def _base_site_name(self, member: Optional[Member]) -> str:
        if member is None or member.base_site_id is None:
            return "(not set)"
        site = self.db.get_site(member.base_site_id)
        return site.name if site else "(not set)"

    # ------------------------------------------------------------------ #
    # Profile update commands
    # ------------------------------------------------------------------ #
    def _cmd_set_name(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        if not arg_str:
            self._send(chat_id, "Usage: /setname <your full real name>")
            return
        self.db.update_name(user_id, arg_str)
        self._send(chat_id, f"Your name is now: {arg_str}")

    def _cmd_set_unit(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        if not arg_str:
            self._prompt_unit(chat_id, user_id, flow=None)
            return
        self.db.set_unit(user_id, arg_str)
        self._send(chat_id, f"Unit set to: {arg_str}")

    def _cmd_set_base(self, chat_id: int, user_id: int) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        self._prompt_base_selection(chat_id, user_id, flow="change")

    def _cmd_whoami(self, chat_id: int, user_id: int) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        role_label = "Admin" if member.is_admin else "Regular User"
        self._send(
            chat_id,
            f"Name: {member.name}\n"
            f"Telegram ID: {member.telegram_id}\n"
            f"Role: {role_label}\n"
            f"Unit: {member.unit or '(not set)'}\n"
            f"Base location: {self._base_site_name(member)}",
        )

    # ------------------------------------------------------------------ #
    # Clock in
    # ------------------------------------------------------------------ #
    def _cmd_clock_in(self, chat_id: int, user_id: int, from_user: dict) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        today = timeutil.today_iso(self.tz)
        if self.db.get_open_entry(user_id, today) is not None:
            self._send(
                chat_id,
                "You already have an open clock-in for today. "
                "Use /clockout to close it first.",
            )
            return
        self._start_clock_in_selection(chat_id, user_id)

    def _start_clock_in_selection(self, chat_id: int, user_id: int) -> None:
        keyboard = inline_keyboard(
            [[("\U0001F3E0 Remote", "clockin:Remote"),
              ("\U0001F4CD On_Site", "clockin:On_Site")]]
        )
        self._send(chat_id, "How would you like to clock in?", reply_markup=keyboard)

    def _handle_callback_query(self, callback: dict) -> None:
        data = callback.get("data") or ""
        callback_id = callback.get("id")
        user_id = callback.get("from", {}).get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        if chat_id is None or user_id is None:
            return
        if callback_id:
            self.client.answer_callback_query(callback_id)

        if data.startswith("clockin:"):
            self._on_clockin_choice(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("base:"):
            self._on_base_choice(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("unit:"):
            self._on_unit_choice(chat_id, user_id, data.split(":", 1)[1])

    def _on_unit_choice(self, chat_id: int, user_id: int, raw: str) -> None:
        if self._require_member(chat_id, user_id) is None:
            return
        try:
            item = self.db.get_unit(int(raw))
        except ValueError:
            return
        if item is None:
            self._send(chat_id, "That unit is no longer available - type a name.")
            return
        state = self.states.get(user_id) or ConversationState(STATE_AWAITING_UNIT)
        self._apply_unit(chat_id, user_id, item.name, state)

    def _on_clockin_choice(self, chat_id: int, user_id: int, choice: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        today = timeutil.today_iso(self.tz)
        if self.db.get_open_entry(user_id, today) is not None:
            self._send(
                chat_id,
                "You already have an open clock-in for today. "
                "Use /clockout to close it first.",
            )
            return
        if choice == TYPE_REMOTE:
            self._do_remote_clock_in(chat_id, user_id, member)
        elif choice == TYPE_ON_SITE:
            self._begin_onsite_clock_in(chat_id, user_id, member)

    def _on_base_choice(self, chat_id: int, user_id: int, raw: str) -> None:
        state = self.states.get(user_id)
        try:
            site_id = int(raw)
        except ValueError:
            return
        base_id = site_id if site_id > 0 else None
        self.db.set_base_site(user_id, base_id)
        flow = state.data.get("flow") if state else "change"
        if flow == "register":
            self._finish_registration(chat_id, user_id)
        else:
            self.states.pop(user_id, None)
            self._send(chat_id, f"Base location set to: {self._base_site_name(self.db.get_member(user_id))}")

    def _radius(self) -> float:
        """Effective geofence radius: admin override in the DB, else config."""
        return self.db.get_geofence_radius(self.config.geofence_radius_meters)

    def _late_flag(self) -> bool:
        """True if a clock-in right now counts as late per the work schedule."""
        sched = self.db.get_work_schedule()
        today = timeutil.today_iso(self.tz)
        if not timeutil.is_workday(today, sched.days):
            return False
        return timeutil.is_after_with_grace(
            timeutil.now(self.tz), sched.start, sched.grace_minutes
        )

    def _do_remote_clock_in(self, chat_id: int, user_id: int, member: Member) -> None:
        now = timeutil.now_iso(self.tz)
        today = timeutil.today_iso(self.tz)
        late = self._late_flag()
        entry = self.db.create_clock_in(
            telegram_id=user_id, date=today, clock_in_time=now,
            clock_in_type=TYPE_REMOTE, coordinator=None,
            is_late=1 if late else 0,
        )
        msg = f"\u2705 Clocked in (Remote) at {now}."
        self._send(chat_id, msg + self._status_suffix(user_id, entry.id, late))

    def _member_sites(self, member: Member) -> list[Site]:
        """Sites this member's On_Site clock-in is validated against."""
        if member.base_site_id is not None:
            site = self.db.get_site(member.base_site_id)
            if site is not None:
                return [site]
        return self.db.get_effective_sites()

    def _begin_onsite_clock_in(self, chat_id: int, user_id: int, member: Member) -> None:
        if not self._member_sites(member):
            self._send(
                chat_id,
                "On-site clock-in is unavailable because no on-site location "
                "has been configured. Please ask an Admin to add a site.",
            )
            return
        self.states[user_id] = ConversationState(STATE_AWAITING_ONSITE_LOCATION)
        self._send(
            chat_id,
            "Please share your current location to verify you are on-site.",
            reply_markup=location_request_keyboard(),
        )

    def _handle_location(self, chat_id: int, user_id: int, location: dict) -> None:
        state = self.states.get(user_id)
        if state is None or state.name != STATE_AWAITING_ONSITE_LOCATION:
            self._send(
                chat_id,
                "Thanks, but I wasn't expecting a location. Use /clockin to start.",
                reply_markup=remove_keyboard(),
            )
            return
        member = self._require_member(chat_id, user_id)
        if member is None:
            self.states.pop(user_id, None)
            return

        sites = self._member_sites(member)
        if not sites:
            self.states.pop(user_id, None)
            self._send(
                chat_id,
                "No on-site location is configured. Ask an Admin to add a site.",
                reply_markup=remove_keyboard(),
            )
            return

        lat = float(location["latitude"])
        lon = float(location["longitude"])
        best_site = min(
            sites,
            key=lambda s: geo.haversine_distance_meters(lat, lon, s.latitude, s.longitude),
        )
        distance = geo.haversine_distance_meters(
            lat, lon, best_site.latitude, best_site.longitude
        )
        self.states.pop(user_id, None)

        radius = self._radius()
        if distance > radius:
            self._send(
                chat_id,
                f"\u274C You appear to be {distance:.0f} m from the nearest site "
                f"({best_site.name}), which is outside the permitted "
                f"{radius:.0f}-meter range. On-site clock-in denied.",
                reply_markup=remove_keyboard(),
            )
            return

        now = timeutil.now_iso(self.tz)
        today = timeutil.today_iso(self.tz)
        late = self._late_flag()
        entry = self.db.create_clock_in(
            telegram_id=user_id, date=today, clock_in_time=now,
            clock_in_type=TYPE_ON_SITE, coordinator=None,
            latitude=lat, longitude=lon, is_late=1 if late else 0,
        )
        msg = (
            f"\u2705 Clocked in (On_Site) at {now}.\n"
            f"Site: {best_site.name} ({distance:.0f} m away)\n"
            f"\U0001F4CD Location: {lat:.6f}, {lon:.6f}\n"
            f"Map: https://www.google.com/maps?q={lat},{lon}"
        )
        self.client.send_message(
            chat_id, msg + self._status_suffix(user_id, entry.id, late),
            reply_markup=remove_keyboard(),
        )

    def _status_suffix(self, user_id: int, entry_id: int, late: bool) -> str:
        """Append a status note; if late, also start the remark capture flow."""
        sched = self.db.get_work_schedule()
        if late:
            self.states[user_id] = ConversationState(
                STATE_AWAITING_LATE_REMARK, {"entry_id": entry_id}
            )
            return (
                f"\n\n\u26A0\uFE0F You clocked in after {sched.start} and are marked "
                "late. Please reply with a short reason for your late arrival."
            )
        # On time (only worth saying on a working day).
        today = timeutil.today_iso(self.tz)
        if timeutil.is_workday(today, sched.days):
            return "\n\U0001F7E2 On time - thank you!"
        return ""

    def _complete_late_remark(
        self, chat_id: int, user_id: int, text: str, state: ConversationState
    ) -> None:
        entry_id = state.data.get("entry_id")
        self.states.pop(user_id, None)
        if entry_id is None:
            return
        self.db.set_late_remark(entry_id, text.strip())
        self._send(chat_id, "Thanks - your late remark has been saved.")

    # ------------------------------------------------------------------ #
    # Clock out
    # ------------------------------------------------------------------ #
    def _cmd_clock_out(self, chat_id: int, user_id: int) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        today = timeutil.today_iso(self.tz)
        entry = self.db.get_open_entry(user_id, today)
        if entry is None:
            self._send(
                chat_id, "You have no active clock-in for today. Use /clockin first."
            )
            return
        now = timeutil.now_iso(self.tz)
        self.db.set_clock_out(entry.id, now)
        worked = timeutil.format_hours(
            timeutil.duration_hours(entry.clock_in_time, now)
        )
        self._send(chat_id, f"\u2705 Clocked out at {now}. Worked {worked} today.")

    # ------------------------------------------------------------------ #
    # Status & summary
    # ------------------------------------------------------------------ #
    def _cmd_status(self, chat_id: int, user_id: int) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        today = timeutil.today_iso(self.tz)
        open_entry = self.db.get_open_entry(user_id, today)
        if open_entry is not None:
            elapsed = timeutil.format_hours(
                timeutil.duration_hours(
                    open_entry.clock_in_time, timeutil.now_iso(self.tz)
                )
            )
            self._send(
                chat_id,
                f"\U0001F7E2 You are clocked in ({open_entry.clock_in_type}) since "
                f"{open_entry.clock_in_time}.\nElapsed: {elapsed}."
                + ("\n\u26A0\uFE0F Marked late today." if open_entry.is_late else ""),
            )
            return
        entry = self.db.get_entry_by_date(user_id, today)
        if entry is not None and entry.clock_out_time:
            worked = timeutil.format_hours(
                timeutil.duration_hours(entry.clock_in_time, entry.clock_out_time)
            )
            self._send(
                chat_id,
                f"\u26AA You are clocked out. Today you worked {worked} "
                f"({entry.clock_in_time} - {entry.clock_out_time}).",
            )
            return
        self._send(chat_id, "\u26AA You have not clocked in today. Use /clockin.")

    def _cmd_summary(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        period = arg_str.strip().lower()
        now = timeutil.now(self.tz)
        if period == "week":
            start, end = timeutil.week_range(now)
            label = "this week"
        else:
            start, end = timeutil.month_range(now)
            label = "this month"

        entries = self.db.query_attendance(
            telegram_id=user_id, start_date=start, end_date=end
        )
        total_hours = sum(
            timeutil.duration_hours(e.clock_in_time, e.clock_out_time)
            for e in entries
        )
        days_present = len({e.date for e in entries})
        late_count = sum(1 for e in entries if e.is_late)
        onsite = sum(1 for e in entries if e.clock_in_type == TYPE_ON_SITE)
        remote = sum(1 for e in entries if e.clock_in_type == TYPE_REMOTE)
        sched = self.db.get_work_schedule()
        workdays = timeutil.count_workdays(start, end, sched.days)

        self._send(
            chat_id,
            f"\U0001F4CA Summary for {member.name} ({label}: {start} to {end})\n"
            f"Days present: {days_present}" + (f" of {workdays} work days" if workdays else "") + "\n"
            f"Total hours: {timeutil.format_hours(total_hours)}\n"
            f"On-site: {onsite}   Remote: {remote}\n"
            f"Late arrivals: {late_count}",
        )

    # ------------------------------------------------------------------ #
    # Late remark command (Req 6)
    # ------------------------------------------------------------------ #
    def _cmd_remark(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        date_token, _, remark = arg_str.partition(" ")
        date_token = date_token.strip()
        remark = remark.strip()
        if not date_token or not remark:
            self._send(
                chat_id,
                "Usage: /remark <YYYY-MM-DD> <remark text>\n"
                "Example: /remark 2026-07-08 Bus was delayed by 30 minutes",
            )
            return
        if not timeutil.is_valid_date(date_token):
            self._send(chat_id, "Please provide a valid date in YYYY-MM-DD format.")
            return
        entry = self.db.get_entry_by_date(user_id, date_token)
        if entry is None:
            self._send(
                chat_id,
                f"No attendance record exists for {date_token}. "
                "Clock in on that date before adding a remark.",
            )
            return
        self.db.set_late_remark(entry.id, remark)
        self._send(chat_id, f"Late remark saved for {date_token}.")

    # ------------------------------------------------------------------ #
    # View / export (Req 7, 8, 9, 10)
    # ------------------------------------------------------------------ #
    def _cmd_view(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        parsed = self._parse_query_args(arg_str)
        if parsed.get("error"):
            self._send(chat_id, parsed["error"])
            return
        target_id, include_member, err = self._resolve_query_target(member, parsed)
        if err:
            self._send(chat_id, err)
            return
        entries = self.db.query_attendance(
            telegram_id=target_id, start_date=parsed.get("from"),
            end_date=parsed.get("to"),
        )
        self._send(chat_id, render_text(entries, include_member=include_member))

    def _cmd_export(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        parsed = self._parse_query_args(arg_str)
        if parsed.get("error"):
            self._send(chat_id, parsed["error"])
            return
        target_id, _include, err = self._resolve_query_target(member, parsed)
        if err:
            self._send(chat_id, err)
            return
        entries = self.db.query_attendance(
            telegram_id=target_id, start_date=parsed.get("from"),
            end_date=parsed.get("to"),
        )
        if not entries:
            self._send(chat_id, "No attendance records found for that selection.")
            return
        csv_bytes = build_csv(entries)
        stamp = timeutil.today_iso(self.tz)
        self.client.send_document(
            chat_id, f"attendance_{stamp}.csv", csv_bytes,
            caption=f"Attendance report ({len(entries)} record(s))",
        )

    def _resolve_query_target(self, member: Member, parsed: dict):
        member_name = parsed.get("member")
        if not member.is_admin:
            if member_name:
                return None, False, "You can only view your own attendance records."
            return member.telegram_id, False, None
        if member_name:
            matches = self.db.find_members_by_name(member_name)
            if not matches:
                return None, False, f"No member found with the name '{member_name}'."
            if len(matches) > 1:
                ids = ", ".join(str(m.telegram_id) for m in matches)
                return (None, False,
                        f"Multiple members named '{member_name}' (IDs: {ids}). "
                        "Please specify by Telegram ID.")
            return matches[0].telegram_id, True, None
        return None, True, None

    # ------------------------------------------------------------------ #
    # Admin: location & promote
    # ------------------------------------------------------------------ #
    def _cmd_set_location(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        if not member.is_admin:
            self._send(chat_id, "Setting the on-site location requires Admin access.")
            return
        parts = arg_str.replace(",", " ").split()
        if len(parts) != 2:
            self._send(
                chat_id,
                "Usage: /setlocation <latitude> <longitude>\n"
                "Example: /setlocation 11.5564 104.9282\n"
                "(For multiple named sites, use the web dashboard.)",
            )
            return
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            self._send(chat_id, "Latitude and longitude must be numbers.")
            return
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            self._send(
                chat_id,
                "Latitude must be between -90 and 90 and longitude between "
                "-180 and 180.",
            )
            return
        self.db.set_configured_location(lat, lon)
        self._send(
            chat_id,
            f"Default on-site location set to ({lat}, {lon}). On-site clock-ins "
            f"are validated within {self._radius():.0f} meters.",
        )

    def _cmd_promote(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        if not member.is_admin:
            self._send(chat_id, "Promoting members requires Admin access.")
            return
        target = arg_str.strip()
        if not target:
            self._send(chat_id, "Usage: /promote <member name or Telegram ID>")
            return
        target_member: Optional[Member] = None
        if target.isdigit():
            target_member = self.db.get_member(int(target))
        if target_member is None:
            matches = self.db.find_members_by_name(target)
            if len(matches) == 1:
                target_member = matches[0]
            elif len(matches) > 1:
                ids = ", ".join(str(m.telegram_id) for m in matches)
                self._send(
                    chat_id,
                    f"Multiple members named '{target}' (IDs: {ids}). "
                    "Please promote by Telegram ID.",
                )
                return
        if target_member is None:
            self._send(chat_id, f"No registered member matches '{target}'.")
            return
        if target_member.is_admin:
            self._send(chat_id, f"{target_member.name} is already an Admin.")
            return
        self.db.set_role(target_member.telegram_id, ROLE_ADMIN)
        self._send(
            chat_id,
            f"{target_member.name} (ID {target_member.telegram_id}) is now an Admin.",
        )

    # ------------------------------------------------------------------ #
    # Argument parsing & helpers
    # ------------------------------------------------------------------ #
    def _parse_query_args(self, arg_str: str) -> dict:
        tokens = arg_str.split()
        result: dict = {}
        i = 0
        while i < len(tokens):
            token = tokens[i].lower()
            if token == "member":
                name_parts: list[str] = []
                i += 1
                while i < len(tokens) and tokens[i].lower() not in ("from", "to"):
                    name_parts.append(tokens[i])
                    i += 1
                if not name_parts:
                    return {"error": "Please provide a name after 'member'."}
                result["member"] = " ".join(name_parts)
            elif token in ("from", "to"):
                if i + 1 >= len(tokens):
                    return {"error": f"Please provide a date after '{token}'."}
                date_value = tokens[i + 1]
                if not timeutil.is_valid_date(date_value):
                    return {"error": f"Invalid date '{date_value}'. Use YYYY-MM-DD."}
                result[token] = date_value
                i += 2
            else:
                return {
                    "error": (
                        "I couldn't parse that. Usage: [member <name>] "
                        "[from <YYYY-MM-DD>] [to <YYYY-MM-DD>]"
                    )
                }
        return result

    def _require_member(self, chat_id: int, user_id: int) -> Optional[Member]:
        member = self.db.get_member(user_id)
        if member is None:
            self._send(
                chat_id, "You are not registered yet. Send /register to get started."
            )
        return member

    def _send(self, chat_id: int, text: str, **kwargs) -> None:
        self.client.send_message(chat_id, text, **kwargs)
