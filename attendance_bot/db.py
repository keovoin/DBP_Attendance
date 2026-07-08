"""SQLite data store for the Telegram Attendance Tracker.

This module owns the schema and all persistence logic. It exposes a thin
repository-style :class:`Database` class whose methods map onto the domain
concepts (Members, Attendance_Entry records, Configured_Location).

Only the standard library ``sqlite3`` module is used.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

# Role constants
ROLE_REGULAR = "regular"
ROLE_ADMIN = "admin"

# Clock-in type constants
TYPE_REMOTE = "Remote"
TYPE_ON_SITE = "On_Site"

# Config keys
CFG_LOCATION_LAT = "location_lat"
CFG_LOCATION_LON = "location_lon"


@dataclass
class Member:
    telegram_id: int
    name: str
    role: str
    coordinator: Optional[str]
    created_at: str

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


class Database:
    """A small persistence layer backed by SQLite."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        # check_same_thread=False so the single polling thread and any helper
        # code can share the connection safely (all access is serialized here).
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets the web portal read the database concurrently while the bot
        # thread writes to it, without "database is locked" errors.
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                telegram_id  INTEGER PRIMARY KEY,
                name         TEXT NOT NULL,
                role         TEXT NOT NULL DEFAULT 'regular',
                coordinator  TEXT,
                created_at   TEXT NOT NULL
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
                FOREIGN KEY (telegram_id) REFERENCES members(telegram_id)
            );

            CREATE INDEX IF NOT EXISTS idx_attendance_member_date
                ON attendance(telegram_id, date);

            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
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
    ) -> Member:
        self.conn.execute(
            """
            INSERT INTO members (telegram_id, name, role, coordinator, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, name, role, coordinator, created_at),
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
            "UPDATE members SET role = ? WHERE telegram_id = ?",
            (role, telegram_id),
        )
        self.conn.commit()

    def update_name(self, telegram_id: int, name: str) -> None:
        self.conn.execute(
            "UPDATE members SET name = ? WHERE telegram_id = ?",
            (name, telegram_id),
        )
        self.conn.commit()

    def find_members_by_name(self, name: str) -> list[Member]:
        """Case-insensitive lookup used to resolve a Member by display name."""
        rows = self.conn.execute(
            "SELECT * FROM members WHERE lower(name) = lower(?)", (name,)
        ).fetchall()
        return [self._row_to_member(r) for r in rows]

    def list_members(self) -> list[Member]:
        """Return all members, ordered by name (used by the web portal)."""
        rows = self.conn.execute(
            "SELECT * FROM members ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
        return [self._row_to_member(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Attendance
    # ------------------------------------------------------------------ #
    def get_open_entry(self, telegram_id: int, date: str) -> Optional[AttendanceEntry]:
        """Return an entry for the date that has a clock-in but no clock-out."""
        row = self.conn.execute(
            """
            SELECT a.*, m.name AS member_name
            FROM attendance a
            JOIN members m ON m.telegram_id = a.telegram_id
            WHERE a.telegram_id = ?
              AND a.date = ?
              AND a.clock_in_time IS NOT NULL
              AND a.clock_out_time IS NULL
            ORDER BY a.id DESC
            LIMIT 1
            """,
            (telegram_id, date),
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_entry_by_date(
        self, telegram_id: int, date: str
    ) -> Optional[AttendanceEntry]:
        """Return the most recent entry for a member on a date, if any."""
        row = self.conn.execute(
            """
            SELECT a.*, m.name AS member_name
            FROM attendance a
            JOIN members m ON m.telegram_id = a.telegram_id
            WHERE a.telegram_id = ? AND a.date = ?
            ORDER BY a.id DESC
            LIMIT 1
            """,
            (telegram_id, date),
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def create_clock_in(
        self,
        telegram_id: int,
        date: str,
        clock_in_time: str,
        clock_in_type: str,
        coordinator: Optional[str],
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> AttendanceEntry:
        cur = self.conn.execute(
            """
            INSERT INTO attendance
                (telegram_id, date, clock_in_time, clock_in_type,
                 coordinator, latitude, longitude)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                date,
                clock_in_time,
                clock_in_type,
                coordinator,
                latitude,
                longitude,
            ),
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

    def set_late_remark(self, entry_id: int, remark: str) -> None:
        self.conn.execute(
            "UPDATE attendance SET late_remark = ? WHERE id = ?",
            (remark, entry_id),
        )
        self.conn.commit()

    def query_attendance(
        self,
        telegram_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[AttendanceEntry]:
        """Return attendance entries filtered by member and/or date range.

        ``telegram_id`` of None means "all members" (used by Admin views).
        Dates are inclusive ISO ``YYYY-MM-DD`` strings.
        """
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
            FROM attendance a
            JOIN members m ON m.telegram_id = a.telegram_id
            {where}
            ORDER BY m.name COLLATE NOCASE ASC, a.date ASC, a.id ASC
            """,
            params,
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Config / Configured_Location
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

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _get_entry_by_id(self, entry_id: int) -> Optional[AttendanceEntry]:
        row = self.conn.execute(
            """
            SELECT a.*, m.name AS member_name
            FROM attendance a
            JOIN members m ON m.telegram_id = a.telegram_id
            WHERE a.id = ?
            """,
            (entry_id,),
        ).fetchone()
        return self._row_to_entry(row) if row else None

    @staticmethod
    def _row_to_member(row: sqlite3.Row) -> Member:
        return Member(
            telegram_id=row["telegram_id"],
            name=row["name"],
            role=row["role"],
            coordinator=row["coordinator"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> AttendanceEntry:
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
        )
