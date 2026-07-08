"""Behavioural tests mapping onto each acceptance criterion.

Each test references the requirement it verifies. A regular user has id 1;
the bootstrap admin (configured in the ``config`` fixture) has id 999.
"""

from attendance_bot.db import ROLE_ADMIN, TYPE_ON_SITE, TYPE_REMOTE

from .conftest import callback_update, location_update, message_update


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #
def register(bot, client, user_id, coordinator="Bob", first_name="Test"):
    bot.handle_update(message_update(user_id, "/register", first_name=first_name))
    if coordinator is not None:
        bot.handle_update(message_update(user_id, coordinator, first_name=first_name))
    client.reset()


def clock_in_remote(bot, client, user_id):
    bot.handle_update(message_update(user_id, "/clockin"))
    bot.handle_update(callback_update(user_id, "clockin:Remote"))


# --------------------------------------------------------------------- #
# Requirement 1: Registration
# --------------------------------------------------------------------- #
def test_register_creates_regular_member_and_prompts_coordinator(bot, client, db):
    bot.handle_update(message_update(1, "/register"))
    member = db.get_member(1)
    assert member is not None
    assert member.role == "regular"  # Req 1.1 default role
    assert "coordinator" in client.last_text.lower()  # Req 1.2 prompt


def test_coordinator_prompt_completes(bot, client, db):
    bot.handle_update(message_update(1, "/register"))
    bot.handle_update(message_update(1, "Bob"))
    assert db.get_member(1).coordinator == "Bob"


def test_duplicate_registration_is_rejected_and_record_retained(bot, client, db):
    register(bot, client, 1, coordinator="Bob")
    bot.handle_update(message_update(1, "/register"))
    assert "already registered" in client.last_text.lower()  # Req 1.3
    assert db.get_member(1).coordinator == "Bob"  # retained


def test_bootstrap_admin_gets_admin_role(bot, client, db):
    bot.handle_update(message_update(999, "/register"))
    assert db.get_member(999).role == ROLE_ADMIN


def test_coordinator_required_before_first_clock_in(bot, client, db):
    # Register without providing a coordinator (skip the follow-up reply).
    bot.handle_update(message_update(1, "/register"))
    # Simulate leaving the coordinator prompt without answering by clearing state.
    bot.states.pop(1, None)
    client.reset()
    bot.handle_update(message_update(1, "/clockin"))
    assert "coordinator" in client.last_text.lower()  # Req 1.4
    # Providing it now should continue into clock-in type selection.
    bot.handle_update(message_update(1, "Bob"))
    assert bot.client.last_markup is not None  # inline keyboard shown


# --------------------------------------------------------------------- #
# Requirement 2: Clock in with type selection
# --------------------------------------------------------------------- #
def test_clock_in_prompts_for_type(bot, client, db):
    register(bot, client, 1)
    bot.handle_update(message_update(1, "/clockin"))
    markup = client.last_markup
    assert markup is not None and "inline_keyboard" in markup  # Req 2.1
    labels = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert "clockin:Remote" in labels and "clockin:On_Site" in labels


def test_remote_clock_in_creates_entry_and_confirms(bot, client, db):
    register(bot, client, 1, coordinator="Bob")
    clock_in_remote(bot, client, 1)
    entries = db.query_attendance(telegram_id=1)
    assert len(entries) == 1  # Req 2.2
    assert entries[0].clock_in_type == TYPE_REMOTE
    assert entries[0].coordinator == "Bob"
    assert "clocked in" in client.last_text.lower()  # Req 2.4


def test_duplicate_open_clock_in_rejected(bot, client, db):
    register(bot, client, 1)
    clock_in_remote(bot, client, 1)
    client.reset()
    bot.handle_update(message_update(1, "/clockin"))
    assert "already have an open clock-in" in client.last_text.lower()  # Req 2.3


# --------------------------------------------------------------------- #
# Requirement 3: On-site clock in with location validation
# --------------------------------------------------------------------- #
def test_onsite_without_configured_location_rejected(bot, client, db):
    register(bot, client, 1)
    bot.handle_update(message_update(1, "/clockin"))
    bot.handle_update(callback_update(1, "clockin:On_Site"))
    assert "not been configured" in client.last_text.lower() or (
        "no on-site location" in client.last_text.lower()
    )  # Req 3.5


def test_onsite_within_radius_succeeds_and_stores_location(bot, client, db):
    db.set_configured_location(11.5564, 104.9282)
    register(bot, client, 1, coordinator="Bob")
    bot.handle_update(message_update(1, "/clockin"))
    bot.handle_update(callback_update(1, "clockin:On_Site"))  # Req 3.1 request loc
    # ~5 m away.
    bot.handle_update(location_update(1, 11.556445, 104.9282))
    entries = db.query_attendance(telegram_id=1)
    assert len(entries) == 1  # Req 3.3
    assert entries[0].clock_in_type == TYPE_ON_SITE
    assert entries[0].latitude is not None and entries[0].longitude is not None


def test_onsite_outside_radius_rejected(bot, client, db):
    db.set_configured_location(11.5564, 104.9282)
    register(bot, client, 1)
    bot.handle_update(message_update(1, "/clockin"))
    bot.handle_update(callback_update(1, "clockin:On_Site"))
    # ~1 km away.
    bot.handle_update(location_update(1, 11.5654, 104.9282))
    assert db.query_attendance(telegram_id=1) == []  # Req 3.4 rejected
    assert "outside" in client.last_text.lower()


# --------------------------------------------------------------------- #
# Requirement 4: Configure on-site location
# --------------------------------------------------------------------- #
def test_admin_sets_location(bot, client, db):
    register(bot, client, 999)  # admin
    bot.handle_update(message_update(999, "/setlocation 11.5564 104.9282"))
    assert db.get_configured_location() == (11.5564, 104.9282)  # Req 4.1


def test_regular_user_cannot_set_location(bot, client, db):
    register(bot, client, 1)
    bot.handle_update(message_update(1, "/setlocation 1 2"))
    assert "admin" in client.last_text.lower()  # Req 4.2
    assert db.get_configured_location() is None


def test_admin_updates_location_applies_to_validation(bot, client, db):
    register(bot, client, 999)
    bot.handle_update(message_update(999, "/setlocation 0 0"))
    bot.handle_update(message_update(999, "/setlocation 11.5564 104.9282"))  # Req 4.3
    assert db.get_configured_location() == (11.5564, 104.9282)


# --------------------------------------------------------------------- #
# Requirement 5: Clock out
# --------------------------------------------------------------------- #
def test_clock_out_records_timestamp(bot, client, db):
    register(bot, client, 1)
    clock_in_remote(bot, client, 1)
    client.reset()
    bot.handle_update(message_update(1, "/clockout"))
    entries = db.query_attendance(telegram_id=1)
    assert entries[0].clock_out_time is not None  # Req 5.1
    assert "clocked out" in client.last_text.lower()  # Req 5.3


def test_clock_out_without_open_entry_rejected(bot, client, db):
    register(bot, client, 1)
    bot.handle_update(message_update(1, "/clockout"))
    assert "no active clock-in" in client.last_text.lower()  # Req 5.2


# --------------------------------------------------------------------- #
# Requirement 6: Late remark
# --------------------------------------------------------------------- #
def test_remark_attaches_to_entry(bot, client, db):
    register(bot, client, 1)
    clock_in_remote(bot, client, 1)
    date = db.query_attendance(telegram_id=1)[0].date
    bot.handle_update(message_update(1, f"/remark {date} Bus was late"))
    assert db.query_attendance(telegram_id=1)[0].late_remark == "Bus was late"  # 6.1


def test_remark_without_entry_rejected(bot, client, db):
    register(bot, client, 1)
    bot.handle_update(message_update(1, "/remark 2020-01-01 whatever"))
    assert "no attendance record" in client.last_text.lower()  # Req 6.2


def test_remark_replaces_previous(bot, client, db):
    register(bot, client, 1)
    clock_in_remote(bot, client, 1)
    date = db.query_attendance(telegram_id=1)[0].date
    bot.handle_update(message_update(1, f"/remark {date} first"))
    bot.handle_update(message_update(1, f"/remark {date} second"))
    assert db.query_attendance(telegram_id=1)[0].late_remark == "second"  # Req 6.3


# --------------------------------------------------------------------- #
# Requirement 7: View own attendance
# --------------------------------------------------------------------- #
def test_regular_user_views_own_records(bot, client, db):
    register(bot, client, 1, coordinator="Bob")
    clock_in_remote(bot, client, 1)
    client.reset()
    bot.handle_update(message_update(1, "/view"))
    text = client.last_text
    assert "Type" in text and "Coordinator" in text  # Req 7.2 fields


def test_regular_user_date_range(bot, client, db):
    register(bot, client, 1)
    db.create_clock_in(1, "2026-07-01", "09:00", TYPE_REMOTE, "Bob")
    db.create_clock_in(1, "2026-07-20", "09:00", TYPE_REMOTE, "Bob")
    client.reset()
    bot.handle_update(message_update(1, "/view from 2026-07-15 to 2026-07-31"))
    assert "2026-07-20" in client.last_text  # Req 7.3
    assert "2026-07-01" not in client.last_text


# --------------------------------------------------------------------- #
# Requirement 8 & 10: Admin views and RBAC
# --------------------------------------------------------------------- #
def test_admin_views_all_members(bot, client, db):
    register(bot, client, 999)  # admin
    register(bot, client, 1, coordinator="Bob", first_name="Alice")
    clock_in_remote(bot, client, 1)
    client.reset()
    bot.handle_update(message_update(999, "/view"))
    assert "Alice" in client.last_text  # Req 8.1


def test_admin_views_specific_member(bot, client, db):
    register(bot, client, 999)
    register(bot, client, 1, coordinator="Bob", first_name="Alice")
    clock_in_remote(bot, client, 1)
    client.reset()
    bot.handle_update(message_update(999, "/view member Alice User"))
    assert "Alice" in client.last_text  # Req 8.2


def test_regular_user_cannot_view_others(bot, client, db):
    register(bot, client, 1, first_name="Alice")
    client.reset()
    bot.handle_update(message_update(1, "/view member Someone"))
    assert "only" in client.last_text.lower()  # Req 10.1


# --------------------------------------------------------------------- #
# Requirement 9: Export
# --------------------------------------------------------------------- #
def test_regular_user_export_own(bot, client, db):
    register(bot, client, 1, coordinator="Bob", first_name="Alice")
    clock_in_remote(bot, client, 1)
    client.reset()
    bot.handle_update(message_update(1, "/export"))
    assert len(client.documents) == 1  # Req 9.1 downloadable file
    content = client.documents[0]["content"].decode("utf-8-sig")
    assert "Member" in content and "Coordinator" in content  # Req 9.4 fields
    assert "Alice" in content


def test_admin_export_all(bot, client, db):
    register(bot, client, 999)
    register(bot, client, 1, coordinator="Bob", first_name="Alice")
    clock_in_remote(bot, client, 1)
    client.reset()
    bot.handle_update(message_update(999, "/export"))
    content = client.documents[0]["content"].decode("utf-8-sig")
    assert "Alice" in content  # Req 9.2


# --------------------------------------------------------------------- #
# Requirement 10: Promote
# --------------------------------------------------------------------- #
def test_admin_promotes_member(bot, client, db):
    register(bot, client, 999)
    register(bot, client, 1, first_name="Alice")
    bot.handle_update(message_update(999, "/promote 1"))
    assert db.get_member(1).role == ROLE_ADMIN  # Req 10.2


def test_regular_user_cannot_promote(bot, client, db):
    register(bot, client, 1)
    register(bot, client, 2, first_name="Carol")
    bot.handle_update(message_update(1, "/promote Carol"))
    assert "admin" in client.last_text.lower()  # Req 10.3
    assert db.get_member(2).role == "regular"
