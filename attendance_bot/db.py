"""SQLite data store for the Telegram Attendance Tracker.

This module owns the schema and all persistence logic. It exposes a thin
repository-style :class:`Database` class whose methods map onto the domain
concepts (Members, Attendance_Entry records, Sites, work schedule, audit log).

Only the standard library ``sqlite3`` module is used.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

# Role constants
ROLE_REGULAR = "regular"
ROLE_ADMIN = "admin"

# Clock-in type constants
TYPE_REMOTE = "Remote"
TYPE_ON_SITE = "On_Site"

# Config keys
CFG_LOCATION_LAT = "location_lat"        # legacy single-location support
CFG_LOCATION_LON = "location_lon"
CFG_WORK_START = "work_start"            # HH:MM
CFG_WORK_END = "work_end"                # HH:MM
CFG_WORK_DAYS = "work_days"              # comma list of weekday indexes (Mon=0)
CFG_REMINDERS = "reminders_enabled"      # "1" / "0"
CFG_GRACE_MINUTES = "grace_minutes"      # minutes after start before "late"
CFG_AUTO_CLOCKOUT = "auto_clockout_enabled"   # "1" / "0"
CFG_AUTO_CLOCKOUT_TIME = "auto_clockout_time"  # HH:MM
CFG_GEOFENCE_RADIUS = "geofence_radius"  # meters (overrides env default)
CFG_LAST_MORNING = "last_morning_reminder"
CFG_LAST_EVENING = "last_evening_reminder"
CFG_LAST_AUTO_CLOCKOUT = "last_auto_clockout"
CFG_LAST_HOLIDAY_NOTICE = "last_holiday_notice"

DEFAULT_WORK_START = "09:00"
DEFAULT_WORK_END = "17:00"
DEFAULT_WORK_DAYS = "0,1,2,3,4"          # Mon-Fri
DEFAULT_AUTO_CLOCKOUT_TIME = "23:59"


@dataclass
class Member:
    telegram_id: int
    name: str
    role: str
    coordinator: Optional[str]
    created_at: str
    unit: Optional[str] = None
    base_site_id: Optional[int] = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


@dataclass
class AttendanceEntry:
    id: int
    telegram_id: int
    member_name: str
    date: str
    clock_in_time: Optional[str]
    clock_out_time: Optional[str]
    clock_in_type: Optional[str]
    coordinator: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    late_remark: Optional[str]
    is_late: int = 0


@dataclass
class Site:
    id: int
    name: str
    latitude: float
    longitude: float
    created_at: str = ""


@dataclass
class NamedItem:
    """A simple id/name lookup row (used for units and coordinators)."""
    id: int
    name: str


@dataclass
class Holiday:
    date: str   # YYYY-MM-DD
    name: str


@dataclass
class Announcement:
    id: int
    at: str
    text: str
    sent_count: int = 0


@dataclass
class WorkSchedule:
    start: str = DEFAULT_WORK_START
    end: str = DEFAULT_WORK_END
    days: set = field(default_factory=lambda: {0, 1, 2, 3, 4})
    reminders_enabled: bool = True
    grace_minutes: int = 0
    auto_clockout_enabled: bool = True
    auto_clockout_time: str = DEFAULT_AUTO_CLOCKOUT_TIME


class Database:
    """A small persistence layer backed by SQLite."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            # WAL lets the web portal read while the bot writes.
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Schema & migrations
    # ------------------------------------------------------------------ #
    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                telegram_id  INTEGER PRIMARY KEY,
                name         TEXT NOT NULL,
                role         TEXT NOT NULL DEFAULT 'regular',
                coordinator  TEXT,
                created_at   TEXT NOT NULL,
                unit         TEXT,
                base_site_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS attendance (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id    INTEGER NOT NULL,
                date           TEXT NOT NULL,
                clock_in_time  TEXT,
                clock_out_time TEXT,
                clock_in_type  TEXT,
                coordinator    TEXT,
                latitude       REAL,
                longitude      REAL,
                late_remark    TEXT,
                is_late        INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (telegram_id) REFERENCES members(telegram_id)
            );

            CREATE INDEX IF NOT EXISTS idx_attendance_member_date
                ON attendance(telegram_id, date);

            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sites (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                latitude   REAL NOT NULL,
                longitude  REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                at        TEXT NOT NULL,
                actor     TEXT NOT NULL,
                action    TEXT NOT NULL,
                detail    TEXT
            );

            CREATE TABLE IF NOT EXISTS units (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS coordinators (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS holidays (
                date TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS announcements (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                at         TEXT NOT NULL,
                text       TEXT NOT NULL,
                sent_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release to existing DBs."""
        member_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(members)")}
        if "unit" not in member_cols:
            self.conn.execute("ALTER TABLE members ADD COLUMN unit TEXT")
        if "base_site_id" not in member_cols:
            self.conn.execute("ALTER TABLE members ADD COLUMN base_site_id INTEGER")
        att_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(attendance)")}
        if "is_late" not in att_cols:
            self.conn.execute(
                "ALTER TABLE attendance ADD COLUMN is_late INTEGER NOT NULL DEFAULT 0"
            )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Members
    # ------------------------------------------------------------------ #
    def get_member(self, telegram_id: int) -> Optional[Member]:
        row = self.conn.execute(
            "SELECT * FROM members WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return self._row_to_member(row) if row else None

    def create_member(
        self,
        telegram_id: int,
        name: str,
        role: str = ROLE_REGULAR,
        coordinator: Optional[str] = None,
        created_at: str = "",
        unit: Optional[str] = None,
        base_site_id: Optional[int] = None,
    ) -> Member:
        self.conn.execute(
            """
            INSERT INTO members
                (telegram_id, name, role, coordinator, created_at, unit, base_site_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, name, role, coordinator, created_at, unit, base_site_id),
        )
        self.conn.commit()
        member = self.get_member(telegram_id)
        assert member is not None
        return member

    def set_coordinator(self, telegram_id: int, coordinator: str) -> None:
        self.conn.execute(
            "UPDATE members SET coordinator = ? WHERE telegram_id = ?",
            (coordinator, telegram_id),
        )
        self.conn.commit()

    def set_role(self, telegram_id: int, role: str) -> None:
        self.conn.execute(
            "UPDATE members SET role = ? WHERE telegram_id = ?", (role, telegram_id)
        )
        self.conn.commit()

    def set_unit(self, telegram_id: int, unit: Optional[str]) -> None:
        self.conn.execute(
            "UPDATE members SET unit = ? WHERE telegram_id = ?", (unit, telegram_id)
        )
        self.conn.commit()

    def set_base_site(self, telegram_id: int, base_site_id: Optional[int]) -> None:
        self.conn.execute(
            "UPDATE members SET base_site_id = ? WHERE telegram_id = ?",
            (base_site_id, telegram_id),
        )
        self.conn.commit()

    def update_member(
        self,
        telegram_id: int,
        name: str,
        role: str,
        coordinator: Optional[str],
        unit: Optional[str],
        base_site_id: Optional[int],
    ) -> None:
        self.conn.execute(
            """
            UPDATE members
            SET name = ?, role = ?, coordinator = ?, unit = ?, base_site_id = ?
            WHERE telegram_id = ?
            """,
            (name, role, coordinator, unit, base_site_id, telegram_id),
        )
        self.conn.commit()

    def update_name(self, telegram_id: int, name: str) -> None:
        self.conn.execute(
            "UPDATE members SET name = ? WHERE telegram_id = ?", (name, telegram_id)
        )
        self.conn.commit()

    def delete_member(self, telegram_id: int) -> None:
        """Delete a member and all of their attendance entries."""
        self.conn.execute("DELETE FROM attendance WHERE telegram_id = ?", (telegram_id,))
        self.conn.execute("DELETE FROM members WHERE telegram_id = ?", (telegram_id,))
        self.conn.commit()

    def find_members_by_name(self, name: str) -> list[Member]:
        rows = self.conn.execute(
            "SELECT * FROM members WHERE lower(name) = lower(?)", (name,)
        ).fetchall()
        return [self._row_to_member(r) for r in rows]

    def list_members(self) -> list[Member]:
        rows = self.conn.execute(
            "SELECT * FROM members ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
        return [self._row_to_member(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Attendance
    # ------------------------------------------------------------------ #
    def get_open_entry(self, telegram_id: int, date: str) -> Optional[AttendanceEntry]:
        row = self.conn.execute(
            """
            SELECT a.*, m.name AS member_name
            FROM attendance a JOIN members m ON m.telegram_id = a.telegram_id
            WHERE a.telegram_id = ? AND a.date = ?
              AND a.clock_in_time IS NOT NULL AND a.clock_out_time IS NULL
            ORDER BY a.id DESC LIMIT 1
            """,
            (telegram_id, date),
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_entry_by_date(self, telegram_id: int, date: str) -> Optional[AttendanceEntry]:
        row = self.conn.execute(
            """
            SELECT a.*, m.name AS member_name
            FROM attendance a JOIN members m ON m.telegram_id = a.telegram_id
            WHERE a.telegram_id = ? AND a.date = ?
            ORDER BY a.id DESC LIMIT 1
            """,
            (telegram_id, date),
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_entry(self, entry_id: int) -> Optional[AttendanceEntry]:
        return self._get_entry_by_id(entry_id)

    def create_clock_in(
        self,
        telegram_id: int,
        date: str,
        clock_in_time: str,
        clock_in_type: str,
        coordinator: Optional[str],
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        is_late: int = 0,
    ) -> AttendanceEntry:
        cur = self.conn.execute(
            """
            INSERT INTO attendance
                (telegram_id, date, clock_in_time, clock_in_type,
                 coordinator, latitude, longitude, is_late)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, date, clock_in_time, clock_in_type, coordinator,
             latitude, longitude, int(is_late)),
        )
        self.conn.commit()
        entry = self._get_entry_by_id(int(cur.lastrowid))
        assert entry is not None
        return entry

    def set_clock_out(self, entry_id: int, clock_out_time: str) -> None:
        self.conn.execute(
            "UPDATE attendance SET clock_out_time = ? WHERE id = ?",
            (clock_out_time, entry_id),
        )
        self.conn.commit()

    def list_open_entries(self, date: str) -> list[AttendanceEntry]:
        """All members' entries for a date that are clocked in but not out."""
        rows = self.conn.execute(
            """
            SELECT a.*, m.name AS member_name
            FROM attendance a JOIN members m ON m.telegram_id = a.telegram_id
            WHERE a.date = ? AND a.clock_in_time IS NOT NULL
              AND a.clock_out_time IS NULL
            ORDER BY a.id ASC
            """,
            (date,),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def set_is_late(self, entry_id: int, is_late: int) -> None:
        self.conn.execute(
            "UPDATE attendance SET is_late = ? WHERE id = ?",
            (int(is_late), entry_id),
        )
        self.conn.commit()

    def set_late_remark(self, entry_id: int, remark: str) -> None:
        self.conn.execute(
            "UPDATE attendance SET late_remark = ? WHERE id = ?", (remark, entry_id)
        )
        self.conn.commit()

    def update_entry(
        self,
        entry_id: int,
        date: str,
        clock_in_time: Optional[str],
        clock_out_time: Optional[str],
        clock_in_type: Optional[str],
        coordinator: Optional[str],
        late_remark: Optional[str],
        is_late: int = 0,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE attendance
            SET date = ?, clock_in_time = ?, clock_out_time = ?, clock_in_type = ?,
                coordinator = ?, late_remark = ?, is_late = ?,
                latitude = ?, longitude = ?
            WHERE id = ?
            """,
            (date, clock_in_time or None, clock_out_time or None,
             clock_in_type or None, coordinator or None, late_remark or None,
             int(is_late), latitude, longitude, entry_id),
        )
        self.conn.commit()

    def delete_entry(self, entry_id: int) -> None:
        self.conn.execute("DELETE FROM attendance WHERE id = ?", (entry_id,))
        self.conn.commit()

    def query_attendance(
        self,
        telegram_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[AttendanceEntry]:
        clauses: list[str] = []
        params: list[object] = []
        if telegram_id is not None:
            clauses.append("a.telegram_id = ?")
            params.append(telegram_id)
        if start_date is not None:
            clauses.append("a.date >= ?")
            params.append(start_date)
        if end_date is not None:
            clauses.append("a.date <= ?")
            params.append(end_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT a.*, m.name AS member_name
            FROM attendance a JOIN members m ON m.telegram_id = a.telegram_id
            {where}
            ORDER BY a.date ASC, m.name COLLATE NOCASE ASC, a.id ASC
            """,
            params,
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Config, legacy location & work schedule
    # ------------------------------------------------------------------ #
    def set_config(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_config(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_configured_location(self, latitude: float, longitude: float) -> None:
        self.set_config(CFG_LOCATION_LAT, repr(latitude))
        self.set_config(CFG_LOCATION_LON, repr(longitude))

    def get_configured_location(self) -> Optional[tuple[float, float]]:
        lat = self.get_config(CFG_LOCATION_LAT)
        lon = self.get_config(CFG_LOCATION_LON)
        if lat is None or lon is None:
            return None
        try:
            return float(lat), float(lon)
        except ValueError:
            return None

    def get_work_schedule(self) -> WorkSchedule:
        start = self.get_config(CFG_WORK_START) or DEFAULT_WORK_START
        end = self.get_config(CFG_WORK_END) or DEFAULT_WORK_END
        days_raw = self.get_config(CFG_WORK_DAYS)
        if days_raw is None:
            days_raw = DEFAULT_WORK_DAYS
        days = set()
        for part in days_raw.split(","):
            part = part.strip()
            if part.isdigit():
                days.add(int(part))
        reminders = (self.get_config(CFG_REMINDERS) or "1") == "1"
        grace_raw = self.get_config(CFG_GRACE_MINUTES)
        grace = int(grace_raw) if grace_raw and grace_raw.isdigit() else 0
        auto = (self.get_config(CFG_AUTO_CLOCKOUT) or "1") == "1"
        auto_time = self.get_config(CFG_AUTO_CLOCKOUT_TIME) or DEFAULT_AUTO_CLOCKOUT_TIME
        return WorkSchedule(
            start=start, end=end, days=days, reminders_enabled=reminders,
            grace_minutes=grace, auto_clockout_enabled=auto,
            auto_clockout_time=auto_time,
        )

    def set_work_schedule(
        self,
        start: str,
        end: str,
        days: set,
        reminders_enabled: bool,
        grace_minutes: int = 0,
        auto_clockout_enabled: bool = True,
        auto_clockout_time: str = DEFAULT_AUTO_CLOCKOUT_TIME,
    ) -> None:
        self.set_config(CFG_WORK_START, start)
        self.set_config(CFG_WORK_END, end)
        self.set_config(CFG_WORK_DAYS, ",".join(str(d) for d in sorted(days)))
        self.set_config(CFG_REMINDERS, "1" if reminders_enabled else "0")
        self.set_config(CFG_GRACE_MINUTES, str(int(grace_minutes)))
        self.set_config(CFG_AUTO_CLOCKOUT, "1" if auto_clockout_enabled else "0")
        self.set_config(CFG_AUTO_CLOCKOUT_TIME, auto_clockout_time)

    def get_geofence_radius(self, default: float) -> float:
        """Configured geofence radius (meters), falling back to ``default``."""
        raw = self.get_config(CFG_GEOFENCE_RADIUS)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def set_geofence_radius(self, meters: float) -> None:
        self.set_config(CFG_GEOFENCE_RADIUS, repr(float(meters)))

    # ------------------------------------------------------------------ #
    # Sites
    # ------------------------------------------------------------------ #
    def add_site(self, name: str, latitude: float, longitude: float,
                 created_at: str = "") -> Site:
        cur = self.conn.execute(
            "INSERT INTO sites (name, latitude, longitude, created_at) "
            "VALUES (?, ?, ?, ?)",
            (name, latitude, longitude, created_at),
        )
        self.conn.commit()
        site = self.get_site(int(cur.lastrowid))
        assert site is not None
        return site

    def get_site(self, site_id: int) -> Optional[Site]:
        row = self.conn.execute(
            "SELECT * FROM sites WHERE id = ?", (site_id,)
        ).fetchone()
        return self._row_to_site(row) if row else None

    def list_sites(self) -> list[Site]:
        rows = self.conn.execute(
            "SELECT * FROM sites ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
        return [self._row_to_site(r) for r in rows]

    def delete_site(self, site_id: int) -> None:
        self.conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        # Unassign this base site from any members.
        self.conn.execute(
            "UPDATE members SET base_site_id = NULL WHERE base_site_id = ?", (site_id,)
        )
        self.conn.commit()

    def get_effective_sites(self) -> list[Site]:
        """Sites to validate On_Site clock-ins against.

        Returns configured sites; if none exist, falls back to the legacy
        single configured location (so older setups keep working).
        """
        sites = self.list_sites()
        if sites:
            return sites
        legacy = self.get_configured_location()
        if legacy:
            return [Site(id=0, name="Main", latitude=legacy[0], longitude=legacy[1])]
        return []

    # ------------------------------------------------------------------ #
    # Units & coordinators (admin-managed master lists)
    # ------------------------------------------------------------------ #
    def add_unit(self, name: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO units (name) VALUES (?)", (name,))
        self.conn.commit()

    def list_units(self) -> list[NamedItem]:
        rows = self.conn.execute(
            "SELECT * FROM units ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
        return [NamedItem(r["id"], r["name"]) for r in rows]

    def get_unit(self, unit_id: int) -> Optional[NamedItem]:
        row = self.conn.execute(
            "SELECT * FROM units WHERE id = ?", (unit_id,)
        ).fetchone()
        return NamedItem(row["id"], row["name"]) if row else None

    def delete_unit(self, unit_id: int) -> None:
        self.conn.execute("DELETE FROM units WHERE id = ?", (unit_id,))
        self.conn.commit()

    def add_coordinator(self, name: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO coordinators (name) VALUES (?)", (name,)
        )
        self.conn.commit()

    def list_coordinators(self) -> list[NamedItem]:
        rows = self.conn.execute(
            "SELECT * FROM coordinators ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
        return [NamedItem(r["id"], r["name"]) for r in rows]

    def get_coordinator(self, coord_id: int) -> Optional[NamedItem]:
        row = self.conn.execute(
            "SELECT * FROM coordinators WHERE id = ?", (coord_id,)
        ).fetchone()
        return NamedItem(row["id"], row["name"]) if row else None

    def delete_coordinator(self, coord_id: int) -> None:
        self.conn.execute("DELETE FROM coordinators WHERE id = ?", (coord_id,))
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Public holidays
    # ------------------------------------------------------------------ #
    def add_holiday(self, date: str, name: str) -> None:
        self.conn.execute(
            "INSERT INTO holidays (date, name) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET name = excluded.name",
            (date, name),
        )
        self.conn.commit()

    def list_holidays(self) -> list[Holiday]:
        rows = self.conn.execute(
            "SELECT * FROM holidays ORDER BY date ASC"
        ).fetchall()
        return [Holiday(r["date"], r["name"]) for r in rows]

    def delete_holiday(self, date: str) -> None:
        self.conn.execute("DELETE FROM holidays WHERE date = ?", (date,))
        self.conn.commit()

    def get_holiday_name(self, date: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT name FROM holidays WHERE date = ?", (date,)
        ).fetchone()
        return row["name"] if row else None

    def is_holiday(self, date: str) -> bool:
        return self.get_holiday_name(date) is not None

    def holiday_dates(self) -> set:
        rows = self.conn.execute("SELECT date FROM holidays").fetchall()
        return {r["date"] for r in rows}

    # ------------------------------------------------------------------ #
    # Announcements
    # ------------------------------------------------------------------ #
    def add_announcement(self, at: str, text: str, sent_count: int) -> Announcement:
        cur = self.conn.execute(
            "INSERT INTO announcements (at, text, sent_count) VALUES (?, ?, ?)",
            (at, text, int(sent_count)),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM announcements WHERE id = ?", (int(cur.lastrowid),)
        ).fetchone()
        return Announcement(row["id"], row["at"], row["text"], row["sent_count"])

    def list_announcements(self, limit: int = 20) -> list[Announcement]:
        rows = self.conn.execute(
            "SELECT * FROM announcements ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Announcement(r["id"], r["at"], r["text"], r["sent_count"]) for r in rows]

    def latest_announcement(self) -> Optional[Announcement]:
        row = self.conn.execute(
            "SELECT * FROM announcements ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return Announcement(row["id"], row["at"], row["text"], row["sent_count"])

    # ------------------------------------------------------------------ #
    # Audit log
    # ------------------------------------------------------------------ #
    def add_audit(self, at: str, actor: str, action: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (at, actor, action, detail),
        )
        self.conn.commit()

    def list_audit(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Row mappers
    # ------------------------------------------------------------------ #
    def _get_entry_by_id(self, entry_id: int) -> Optional[AttendanceEntry]:
        row = self.conn.execute(
            """
            SELECT a.*, m.name AS member_name
            FROM attendance a JOIN members m ON m.telegram_id = a.telegram_id
            WHERE a.id = ?
            """,
            (entry_id,),
        ).fetchone()
        return self._row_to_entry(row) if row else None

    @staticmethod
    def _row_to_member(row: sqlite3.Row) -> Member:
        keys = row.keys()
        return Member(
            telegram_id=row["telegram_id"],
            name=row["name"],
            role=row["role"],
            coordinator=row["coordinator"],
            created_at=row["created_at"],
            unit=row["unit"] if "unit" in keys else None,
            base_site_id=row["base_site_id"] if "base_site_id" in keys else None,
        )

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> AttendanceEntry:
        keys = row.keys()
        return AttendanceEntry(
            id=row["id"],
            telegram_id=row["telegram_id"],
            member_name=row["member_name"],
            date=row["date"],
            clock_in_time=row["clock_in_time"],
            clock_out_time=row["clock_out_time"],
            clock_in_type=row["clock_in_type"],
            coordinator=row["coordinator"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            late_remark=row["late_remark"],
            is_late=row["is_late"] if "is_late" in keys else 0,
        )

    @staticmethod
    def _row_to_site(row: sqlite3.Row) -> Site:
        return Site(
            id=row["id"],
            name=row["name"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            created_at=row["created_at"] if "created_at" in row.keys() else "",
        )
