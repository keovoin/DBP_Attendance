"""Command handlers and conversation state machine.

The :class:`AttendanceBot` class turns raw Telegram updates into attendance
actions. It is deliberately decoupled from the network layer: it depends only
on a :class:`TelegramClient`-like object and a :class:`Database`, so its logic
can be exercised in tests with fakes.

Each acceptance criterion from the specification maps onto behaviour here; see
the inline references (e.g. "Req 2.3") for traceability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import geo
from .config import Config
from .db import (
    ROLE_ADMIN,
    ROLE_REGULAR,
    TYPE_ON_SITE,
    TYPE_REMOTE,
    Database,
    Member,
)
from .reports import build_csv, render_text
from .telegram_api import (
    inline_keyboard,
    location_request_keyboard,
    remove_keyboard,
)
from . import timeutil

# Conversation states
STATE_AWAITING_COORDINATOR = "awaiting_coordinator"
STATE_AWAITING_ONSITE_LOCATION = "awaiting_onsite_location"

HELP_TEXT = (
    "\U0001F4CB *Attendance Tracker*\n\n"
    "/register - Register and set your coordinator\n"
    "/clockin - Clock in (choose Remote or On_Site)\n"
    "/clockout - Clock out for today\n"
    "/setcoordinator <name> - Set or change your coordinator\n"
    "/remark <YYYY-MM-DD> <text> - Add a late remark for a date\n"
    "/view [member <name>] [from <date>] [to <date>] - View attendance\n"
    "/export [member <name>] [from <date>] [to <date>] - Download CSV report\n"
    "/whoami - Show your registration details\n"
    "/help - Show this message\n\n"
    "*Admin only*\n"
    "/setlocation <lat> <lon> - Set the on-site location\n"
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
            # Never let one bad update kill the polling loop.
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
            msg = update["callback_query"].get("message", {})
            return msg.get("chat", {}).get("id")
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

        # A shared location takes priority when we're expecting one.
        if "location" in message:
            self._handle_location(chat_id, user_id, message["location"])
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        # Non-command text may be part of an active conversation.
        if not text.startswith("/"):
            self._handle_conversation_text(chat_id, user_id, from_user, text)
            return

        command, _, arg_str = text.partition(" ")
        command = command.split("@", 1)[0].lower()  # strip @botname suffix
        arg_str = arg_str.strip()

        if command in ("/start", "/register"):
            self._cmd_register(chat_id, user_id, from_user)
        elif command == "/help":
            self._send(chat_id, HELP_TEXT, parse_mode="Markdown")
        elif command == "/whoami":
            self._cmd_whoami(chat_id, user_id)
        elif command == "/setcoordinator":
            self._cmd_set_coordinator(chat_id, user_id, from_user, arg_str)
        elif command == "/clockin":
            self._cmd_clock_in(chat_id, user_id, from_user)
        elif command == "/clockout":
            self._cmd_clock_out(chat_id, user_id)
        elif command == "/remark":
            self._cmd_remark(chat_id, user_id, arg_str)
        elif command == "/view":
            self._cmd_view(chat_id, user_id, arg_str)
        elif command == "/export":
            self._cmd_export(chat_id, user_id, arg_str)
        elif command == "/setlocation":
            self._cmd_set_location(chat_id, user_id, arg_str)
        elif command == "/promote":
            self._cmd_promote(chat_id, user_id, arg_str)
        else:
            self._send(
                chat_id,
                "Unknown command. Send /help to see what I can do.",
            )

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
        if state.name == STATE_AWAITING_COORDINATOR:
            self._complete_coordinator(chat_id, user_id, from_user, text, state)
        elif state.name == STATE_AWAITING_ONSITE_LOCATION:
            self._send(
                chat_id,
                "I'm waiting for your location. Please tap the \U0001F4CD button to "
                "share it, or send /clockin again to restart.",
            )
        else:
            self.states.pop(user_id, None)

    # ------------------------------------------------------------------ #
    # Registration (Req 1)
    # ------------------------------------------------------------------ #
    def _display_name(self, from_user: dict) -> str:
        first = (from_user.get("first_name") or "").strip()
        last = (from_user.get("last_name") or "").strip()
        full = (first + " " + last).strip()
        if full:
            return full
        username = from_user.get("username")
        if username:
            return username
        return f"User {from_user.get('id')}"

    def _cmd_register(self, chat_id: int, user_id: int, from_user: dict) -> None:
        existing = self.db.get_member(user_id)
        if existing is not None:
            # Req 1.3: already registered -> inform and retain record.
            self._send(
                chat_id,
                "You are already registered. Your existing record is unchanged.\n"
                "Use /setcoordinator to update your coordinator or /help for commands.",
            )
            return

        # Req 1.1: create a Member; default role Regular_User. Bootstrap admins
        # are promoted immediately based on configuration.
        role = (
            ROLE_ADMIN if user_id in self.config.admin_telegram_ids else ROLE_REGULAR
        )
        name = self._display_name(from_user)
        self.db.create_member(
            telegram_id=user_id,
            name=name,
            role=role,
            coordinator=None,
            created_at=timeutil.now_iso(self.config.tz_offset_hours),
        )
        role_note = " You have been granted Admin access." if role == ROLE_ADMIN else ""
        # Req 1.2: prompt for coordinator name after registration.
        self.states[user_id] = ConversationState(STATE_AWAITING_COORDINATOR)
        self._send(
            chat_id,
            f"Welcome, {name}! You are now registered.{role_note}\n\n"
            "Who is your coordinator? Please reply with their name.",
        )

    def _complete_coordinator(
        self,
        chat_id: int,
        user_id: int,
        from_user: dict,
        text: str,
        state: ConversationState,
    ) -> None:
        coordinator = text.strip()
        if not coordinator:
            self._send(chat_id, "Please send a non-empty coordinator name.")
            return
        self.db.set_coordinator(user_id, coordinator)
        self.states.pop(user_id, None)
        self._send(chat_id, f"Coordinator set to: {coordinator}")

        # If the coordinator was requested as a prerequisite for clocking in
        # (Req 1.4), continue straight into the clock-in flow.
        if state.data.get("after") == "clockin":
            self._start_clock_in_selection(chat_id, user_id)

    def _cmd_set_coordinator(
        self, chat_id: int, user_id: int, from_user: dict, arg_str: str
    ) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        if not arg_str:
            # Fall back to the interactive prompt.
            self.states[user_id] = ConversationState(STATE_AWAITING_COORDINATOR)
            self._send(chat_id, "Please reply with your coordinator's name.")
            return
        self.db.set_coordinator(user_id, arg_str)
        self._send(chat_id, f"Coordinator set to: {arg_str}")

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
            f"Coordinator: {member.coordinator or '(not set)'}",
        )

    # ------------------------------------------------------------------ #
    # Clock in (Req 2 & 3)
    # ------------------------------------------------------------------ #
    def _cmd_clock_in(self, chat_id: int, user_id: int, from_user: dict) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return

        today = timeutil.today_iso(self.config.tz_offset_hours)
        # Req 2.3: reject if an open (not clocked-out) entry exists today.
        if self.db.get_open_entry(user_id, today) is not None:
            self._send(
                chat_id,
                "You already have an open clock-in for today. "
                "Use /clockout to close it first.",
            )
            return

        # Req 1.4: coordinator must be set before the first attendance entry.
        if not member.coordinator:
            self.states[user_id] = ConversationState(
                STATE_AWAITING_COORDINATOR, {"after": "clockin"}
            )
            self._send(
                chat_id,
                "Before your first clock-in, I need your coordinator's name.\n"
                "Please reply with their name.",
            )
            return

        self._start_clock_in_selection(chat_id, user_id)

    def _start_clock_in_selection(self, chat_id: int, user_id: int) -> None:
        # Req 2.1: prompt to select Remote or On_Site.
        keyboard = inline_keyboard(
            [[("\U0001F3E0 Remote", "clockin:Remote"), ("\U0001F4CD On_Site", "clockin:On_Site")]]
        )
        self._send(
            chat_id,
            "How would you like to clock in?",
            reply_markup=keyboard,
        )

    def _handle_callback_query(self, callback: dict) -> None:
        data = callback.get("data") or ""
        callback_id = callback.get("id")
        from_user = callback.get("from", {})
        user_id = from_user.get("id")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        if chat_id is None or user_id is None:
            return

        if callback_id:
            self.client.answer_callback_query(callback_id)

        if data.startswith("clockin:"):
            choice = data.split(":", 1)[1]
            member = self._require_member(chat_id, user_id)
            if member is None:
                return
            today = timeutil.today_iso(self.config.tz_offset_hours)
            # Guard again in case an entry opened between prompt and selection.
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

    def _do_remote_clock_in(
        self, chat_id: int, user_id: int, member: Member
    ) -> None:
        # Req 2.2 & 2.4
        now = timeutil.now_iso(self.config.tz_offset_hours)
        today = timeutil.today_iso(self.config.tz_offset_hours)
        self.db.create_clock_in(
            telegram_id=user_id,
            date=today,
            clock_in_time=now,
            clock_in_type=TYPE_REMOTE,
            coordinator=member.coordinator,
        )
        self._send(
            chat_id,
            f"\u2705 Clocked in (Remote) at {now}.\nCoordinator: {member.coordinator}",
        )

    def _begin_onsite_clock_in(
        self, chat_id: int, user_id: int, member: Member
    ) -> None:
        # Req 3.5: reject early if no Configured_Location is set.
        if self.db.get_configured_location() is None:
            self._send(
                chat_id,
                "On-site clock-in is unavailable because no on-site location "
                "has been configured. Please ask an Admin to run /setlocation.",
            )
            return
        # Req 3.1: request the member's current geolocation.
        self.states[user_id] = ConversationState(STATE_AWAITING_ONSITE_LOCATION)
        self._send(
            chat_id,
            "Please share your current location to verify you are on-site.",
            reply_markup=location_request_keyboard(),
        )

    def _handle_location(self, chat_id: int, user_id: int, location: dict) -> None:
        state = self.states.get(user_id)
        if state is None or state.name != STATE_AWAITING_ONSITE_LOCATION:
            # A stray location share with no pending on-site clock-in.
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

        configured = self.db.get_configured_location()
        if configured is None:
            # Req 3.5 (location got cleared between prompt and share).
            self.states.pop(user_id, None)
            self._send(
                chat_id,
                "No on-site location is configured. Ask an Admin to run /setlocation.",
                reply_markup=remove_keyboard(),
            )
            return

        lat = float(location["latitude"])
        lon = float(location["longitude"])
        conf_lat, conf_lon = configured
        # Req 3.2: compute the distance to the Configured_Location.
        distance = geo.haversine_distance_meters(lat, lon, conf_lat, conf_lon)
        self.states.pop(user_id, None)

        radius = self.config.geofence_radius_meters
        if distance > radius:
            # Req 3.4: outside the geofence -> reject.
            self._send(
                chat_id,
                f"\u274C You appear to be {distance:.0f} m away, which is outside the "
                f"permitted {radius:.0f}-meter range. On-site clock-in denied.",
                reply_markup=remove_keyboard(),
            )
            return

        # Req 3.3: within the geofence -> create On_Site entry, store location.
        now = timeutil.now_iso(self.config.tz_offset_hours)
        today = timeutil.today_iso(self.config.tz_offset_hours)
        self.db.create_clock_in(
            telegram_id=user_id,
            date=today,
            clock_in_time=now,
            clock_in_type=TYPE_ON_SITE,
            coordinator=member.coordinator,
            latitude=lat,
            longitude=lon,
        )
        self._send(
            chat_id,
            f"\u2705 Clocked in (On_Site) at {now}.\n"
            f"Distance from site: {distance:.0f} m\n"
            f"Coordinator: {member.coordinator}",
            reply_markup=remove_keyboard(),
        )

    # ------------------------------------------------------------------ #
    # Clock out (Req 5)
    # ------------------------------------------------------------------ #
    def _cmd_clock_out(self, chat_id: int, user_id: int) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        today = timeutil.today_iso(self.config.tz_offset_hours)
        entry = self.db.get_open_entry(user_id, today)
        if entry is None:
            # Req 5.2
            self._send(
                chat_id,
                "You have no active clock-in for today. Use /clockin first.",
            )
            return
        # Req 5.1 & 5.3
        now = timeutil.now_iso(self.config.tz_offset_hours)
        self.db.set_clock_out(entry.id, now)
        self._send(chat_id, f"\u2705 Clocked out at {now}.")

    # ------------------------------------------------------------------ #
    # Late remark (Req 6)
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
            # Req 6.2
            self._send(
                chat_id,
                f"No attendance record exists for {date_token}. "
                "Clock in on that date before adding a remark.",
            )
            return
        # Req 6.1 & 6.3 (replace any existing remark).
        self.db.set_late_remark(entry.id, remark)
        self._send(chat_id, f"Late remark saved for {date_token}.")

    # ------------------------------------------------------------------ #
    # View attendance (Req 7, 8, 10)
    # ------------------------------------------------------------------ #
    def _cmd_view(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        parsed = self._parse_query_args(arg_str)
        if parsed.get("error"):
            self._send(chat_id, parsed["error"])
            return

        target_id, include_member, err = self._resolve_query_target(
            chat_id, member, parsed
        )
        if err:
            self._send(chat_id, err)
            return

        entries = self.db.query_attendance(
            telegram_id=target_id,
            start_date=parsed.get("from"),
            end_date=parsed.get("to"),
        )
        self._send(
            chat_id,
            render_text(entries, include_member=include_member),
        )

    # ------------------------------------------------------------------ #
    # Export (Req 9)
    # ------------------------------------------------------------------ #
    def _cmd_export(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        parsed = self._parse_query_args(arg_str)
        if parsed.get("error"):
            self._send(chat_id, parsed["error"])
            return

        target_id, _include, err = self._resolve_query_target(chat_id, member, parsed)
        if err:
            self._send(chat_id, err)
            return

        entries = self.db.query_attendance(
            telegram_id=target_id,
            start_date=parsed.get("from"),
            end_date=parsed.get("to"),
        )
        if not entries:
            self._send(chat_id, "No attendance records found for that selection.")
            return

        csv_bytes = build_csv(entries)
        stamp = timeutil.today_iso(self.config.tz_offset_hours)
        filename = f"attendance_{stamp}.csv"
        self.client.send_document(
            chat_id,
            filename,
            csv_bytes,
            caption=f"Attendance report ({len(entries)} record(s))",
        )

    def _resolve_query_target(
        self, chat_id: int, member: Member, parsed: dict
    ):
        """Determine which member's records to return, enforcing RBAC.

        Returns ``(target_telegram_id_or_None, include_member_name, error)``.
        A target of None means "all members" (Admin only).
        """
        member_name = parsed.get("member")

        if not member.is_admin:
            # Req 10.1 / Req 7.1: Regular_User only ever sees their own records.
            if member_name:
                return (
                    None,
                    False,
                    "You can only view your own attendance records.",
                )
            return member.telegram_id, False, None

        # Admin path (Req 8).
        if member_name:
            matches = self.db.find_members_by_name(member_name)
            if not matches:
                return None, False, f"No member found with the name '{member_name}'."
            if len(matches) > 1:
                ids = ", ".join(str(m.telegram_id) for m in matches)
                return (
                    None,
                    False,
                    f"Multiple members named '{member_name}' (IDs: {ids}). "
                    "Please specify by Telegram ID.",
                )
            return matches[0].telegram_id, True, None

        # Admin, no specific member -> all members (Req 8.1).
        return None, True, None

    # ------------------------------------------------------------------ #
    # Configure location (Req 4)
    # ------------------------------------------------------------------ #
    def _cmd_set_location(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        if not member.is_admin:
            # Req 4.2
            self._send(
                chat_id,
                "Setting the on-site location requires Admin access.",
            )
            return
        parts = arg_str.replace(",", " ").split()
        if len(parts) != 2:
            self._send(
                chat_id,
                "Usage: /setlocation <latitude> <longitude>\n"
                "Example: /setlocation 11.5564 104.9282",
            )
            return
        try:
            lat = float(parts[0])
            lon = float(parts[1])
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
        # Req 4.1 & 4.3 (subsequent validations use the stored value).
        self.db.set_configured_location(lat, lon)
        self._send(
            chat_id,
            f"On-site location set to ({lat}, {lon}). On-site clock-ins will be "
            f"validated within {self.config.geofence_radius_meters:.0f} meters.",
        )

    # ------------------------------------------------------------------ #
    # Promote (Req 10.2 / 10.3)
    # ------------------------------------------------------------------ #
    def _cmd_promote(self, chat_id: int, user_id: int, arg_str: str) -> None:
        member = self._require_member(chat_id, user_id)
        if member is None:
            return
        if not member.is_admin:
            # Req 10.3
            self._send(chat_id, "Promoting members requires Admin access.")
            return
        target = arg_str.strip()
        if not target:
            self._send(
                chat_id,
                "Usage: /promote <member name or Telegram ID>",
            )
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

        # Req 10.2
        self.db.set_role(target_member.telegram_id, ROLE_ADMIN)
        self._send(
            chat_id,
            f"{target_member.name} (ID {target_member.telegram_id}) is now an Admin.",
        )

    # ------------------------------------------------------------------ #
    # Argument parsing
    # ------------------------------------------------------------------ #
    def _parse_query_args(self, arg_str: str) -> dict:
        """Parse ``[member <name>] [from <date>] [to <date>]`` in any order.

        The ``member`` name may contain spaces; it extends until the next
        recognised keyword (``from`` / ``to``) or the end of input.
        """
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
                    return {
                        "error": f"Invalid date '{date_value}'. Use YYYY-MM-DD format."
                    }
                result[token] = date_value
                i += 2
            else:
                return {
                    "error": (
                        "I couldn't parse that. Usage: "
                        "[member <name>] [from <YYYY-MM-DD>] [to <YYYY-MM-DD>]"
                    )
                }
        return result

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    def _require_member(self, chat_id: int, user_id: int) -> Optional[Member]:
        member = self.db.get_member(user_id)
        if member is None:
            self._send(
                chat_id,
                "You are not registered yet. Send /register to get started.",
            )
        return member

    def _send(self, chat_id: int, text: str, **kwargs) -> None:
        self.client.send_message(chat_id, text, **kwargs)
