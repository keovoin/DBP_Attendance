"""Tests for the SQLite data store."""

from attendance_bot.db import ROLE_ADMIN, ROLE_REGULAR, TYPE_REMOTE, Database


def test_create_and_get_member(db: Database):
    db.create_member(1, "Alice", ROLE_REGULAR, None, "2026-07-08 09:00:00")
    m = db.get_member(1)
    assert m is not None
    assert m.name == "Alice"
    assert m.role == ROLE_REGULAR
    assert not m.is_admin


def test_set_coordinator_and_role(db: Database):
    db.create_member(1, "Alice", ROLE_REGULAR, None, "t")
    db.set_coordinator(1, "Bob")
    db.set_role(1, ROLE_ADMIN)
    m = db.get_member(1)
    assert m.coordinator == "Bob"
    assert m.is_admin


def test_open_entry_lifecycle(db: Database):
    db.create_member(1, "Alice", ROLE_REGULAR, "Bob", "t")
    assert db.get_open_entry(1, "2026-07-08") is None
    entry = db.create_clock_in(1, "2026-07-08", "09:00", TYPE_REMOTE, "Bob")
    assert db.get_open_entry(1, "2026-07-08") is not None
    db.set_clock_out(entry.id, "17:00")
    assert db.get_open_entry(1, "2026-07-08") is None


def test_query_filters_by_member_and_date(db: Database):
    db.create_member(1, "Alice", ROLE_REGULAR, "Bob", "t")
    db.create_member(2, "Carol", ROLE_REGULAR, "Dan", "t")
    db.create_clock_in(1, "2026-07-01", "09:00", TYPE_REMOTE, "Bob")
    db.create_clock_in(1, "2026-07-10", "09:00", TYPE_REMOTE, "Bob")
    db.create_clock_in(2, "2026-07-05", "09:00", TYPE_REMOTE, "Dan")

    assert len(db.query_attendance()) == 3
    assert len(db.query_attendance(telegram_id=1)) == 2
    ranged = db.query_attendance(start_date="2026-07-02", end_date="2026-07-09")
    assert len(ranged) == 1
    assert ranged[0].telegram_id == 2


def test_configured_location_roundtrip(db: Database):
    assert db.get_configured_location() is None
    db.set_configured_location(11.5564, 104.9282)
    assert db.get_configured_location() == (11.5564, 104.9282)
    db.set_configured_location(1.0, 2.0)
    assert db.get_configured_location() == (1.0, 2.0)


def test_find_members_by_name_case_insensitive(db: Database):
    db.create_member(1, "Alice", ROLE_REGULAR, None, "t")
    assert len(db.find_members_by_name("alice")) == 1
    assert len(db.find_members_by_name("ALICE")) == 1
    assert db.find_members_by_name("nobody") == []
