"""Hand-added rows: Bogdan's "add or unadd people", and what keeps them honest."""

from __future__ import annotations

import json

import pytest

from hotline_registry.registry import (
    Person,
    Registry,
    RegistryError,
    parse_discord_id,
)

SNOWFLAKE = "123456789012345678"


def add(registry, **kw):
    base = {"consent_note": "agreed on a call, 2026-11-06", "added_by": "1329",
            "added_via": "test"}
    base.update(kw)
    return registry.add_admin(base.pop("name", "Jovana"), **base)


@pytest.mark.parametrize(("raw", "want"), [
    (SNOWFLAKE, SNOWFLAKE), (f"<@{SNOWFLAKE}>", SNOWFLAKE),
    (f"<@!{SNOWFLAKE}>", SNOWFLAKE), ("  ", ""),
])
def test_parse_discord_id(raw, want):
    assert parse_discord_id(raw) == want


@pytest.mark.parametrize("raw", ["milos", "@milos", "123", "<#123456789012345678>"])
def test_a_username_is_refused_not_guessed(raw):
    with pytest.raises(RegistryError, match="not a Discord user id"):
        parse_discord_id(raw)


def test_rows_from_before_source_existed_read_as_form(store):
    store.write_text(json.dumps({"schema": 1, "people": {"9": {
        "discord_id": "9", "discord_name": "old#0", "name": "Old"}}}))
    person = Registry(store).resolve("Old")
    assert person.source == "form" and not person.admin_added


def test_unknown_keys_from_a_newer_version_do_not_break_reading(store):
    store.write_text(json.dumps({"people": {"9": {
        "discord_id": "9", "discord_name": "x", "name": "X", "whatsapp": "+381"}}}))
    assert Registry(store).resolve("X").name == "X"


def test_an_admin_row_records_who_added_it_and_how_they_agreed(registry, store):
    person = add(registry, discord_id=f"<@{SNOWFLAKE}>", sip="jovana")
    assert person.discord_id == SNOWFLAKE
    assert person.source == "admin" and person.added_by == "1329"
    assert person.sip == "sip:jovana@sip.linphone.org"
    again = Registry(store).resolve("Jovana")
    assert again.consent_note == "agreed on a call, 2026-11-06"
    assert again.introduced_at == ""


def test_a_consent_note_is_required(registry):
    with pytest.raises(RegistryError, match="consent note"):
        add(registry, consent_note="  ")


def test_no_discord_gets_a_key_that_can_never_be_dmed(registry):
    person = add(registry, sip="sip:j@sip.linphone.org")
    assert person.discord_id.startswith("admin-")
    assert not person.messageable and person.callable_
    assert person.channels() == "call"


def test_no_discord_and_no_sip_is_recorded_but_unreachable(registry):
    assert add(registry).channels() == "none (no Discord, no SIP)"


def test_an_admin_add_does_not_overwrite_a_self_registration(registry):
    registry.upsert(Person(discord_id=SNOWFLAKE, discord_name="j#0", name="J"))
    with pytest.raises(RegistryError, match="already in the registry"):
        add(registry, discord_id=SNOWFLAKE)
    assert registry.people[SNOWFLAKE].source == "form"


def test_a_revoked_person_can_be_added_back(registry):
    registry.upsert(Person(discord_id=SNOWFLAKE, discord_name="j#0", name="J"))
    registry.revoke(SNOWFLAKE, "removed by Bogdan")
    assert registry.people[SNOWFLAKE].revoked_reason == "removed by Bogdan"
    person = add(registry, discord_id=SNOWFLAKE)
    assert person.messageable and person.source == "admin"


def test_edit_changes_fields_and_says_who(registry):
    person = registry.edit("222", edited_by="1329", capabilities="design, CSS", sip="ana")
    assert person.capabilities == "design, CSS" and person.callable_
    assert person.edited_by == "1329" and person.source == "form"


def test_edit_leaves_none_fields_alone(registry):
    registry.edit("222", edited_by="x", name=None, capabilities="new")
    assert registry.people["222"].name == "Ana"


def test_edit_attaches_a_discord_id_by_rekeying(registry, store):
    person = add(registry, sip="j")
    old = person.discord_id
    registry.edit(old, edited_by="x", discord_id=SNOWFLAKE)
    reloaded = Registry(store)
    assert old not in reloaded.people and reloaded.people[SNOWFLAKE].messageable


def test_edit_refuses_to_take_someone_elses_id(registry):
    other = "876543210987654321"
    registry.upsert(Person(discord_id=other, discord_name="o#0", name="Other"))
    person = add(registry)
    with pytest.raises(RegistryError, match="somebody else"):
        registry.edit(person.discord_id, edited_by="x", discord_id=other)


def test_edit_refuses_to_strip_a_discord_identity(registry):
    with pytest.raises(RegistryError, match="identity"):
        registry.edit("111", edited_by="x", discord_id=" ")


def test_edit_refuses_unknown_fields(registry):
    with pytest.raises(RegistryError, match="cannot edit"):
        registry.edit("111", edited_by="x", revoked="no")


def test_a_self_registration_over_an_admin_row_makes_it_theirs(registry):
    """Filling in the form is the stronger consent; the row becomes a form row."""
    add(registry, discord_id=SNOWFLAKE)
    registry.upsert(Person(discord_id=SNOWFLAKE, discord_name="j#0", name="J"))
    assert registry.people[SNOWFLAKE].source == "form"


def test_mark_introduced_is_once(registry):
    person = add(registry, discord_id=SNOWFLAKE)
    registry.mark_introduced(person.discord_id)
    first = registry.people[SNOWFLAKE].introduced_at
    assert first
    registry.mark_introduced(person.discord_id)
    assert registry.people[SNOWFLAKE].introduced_at == first
