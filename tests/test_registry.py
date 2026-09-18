from __future__ import annotations

import json
import os
import stat

import pytest

from hotline_registry.registry import Person, Registry, RegistryError, normalise_sip


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("b0g13a", "sip:b0g13a@sip.linphone.org"),
        ("b0g13a@sip.linphone.org", "sip:b0g13a@sip.linphone.org"),
        ("sip:b0g13a@sip.linphone.org", "sip:b0g13a@sip.linphone.org"),
        ("  sip:x@y.z  ", "sip:x@y.z"),
        ("", ""),
    ],
)
def test_normalise_sip(raw, want):
    assert normalise_sip(raw) == want


def test_blank_sip_means_do_not_call(registry):
    ana = registry.resolve("Ana")
    assert ana.messageable
    assert not ana.callable_
    assert ana.channels() == "message"


def test_sip_makes_someone_callable(registry):
    assert registry.resolve("Milos").callable_


def test_leaving_withdraws_both_permissions(registry):
    registry.revoke("111")
    milos = registry.resolve("Milos")
    assert not milos.messageable and not milos.callable_
    assert milos not in registry.all()
    assert milos in registry.all(include_revoked=True)


def test_resubmitting_keeps_when_they_consented(registry):
    before = registry.resolve("Milos").registered_at
    registry.upsert(Person(discord_id="111", discord_name="milos#0", name="Milos",
                           sip="sip:new@sip.linphone.org", capabilities="Go"))
    after = registry.resolve("Milos")
    assert after.registered_at == before
    assert after.sip == "sip:new@sip.linphone.org"


def test_resubmitting_restores_a_revoked_person(registry):
    registry.revoke("111")
    registry.upsert(Person(discord_id="111", discord_name="milos#0", name="Milos"))
    assert registry.resolve("Milos").messageable


def test_a_partial_matching_two_people_is_an_error_not_a_guess(registry):
    registry.upsert(Person(discord_id="333", discord_name="milos2#0", name="Milos Other"))
    with pytest.raises(RegistryError, match="matches 2 people"):
        registry.resolve("Mil")


def test_two_people_with_the_same_name_force_the_discord_id(registry):
    registry.upsert(Person(discord_id="333", discord_name="milos2#0", name="Milos"))
    with pytest.raises(RegistryError, match="use the Discord id"):
        registry.resolve("Milos")
    assert registry.resolve("333").discord_name == "milos2#0"


def test_exact_match_beats_a_partial_one(registry):
    registry.upsert(Person(discord_id="333", discord_name="milos2#0", name="Milos Other"))
    assert registry.resolve("Milos").discord_id == "111"


def test_unknown_person_says_why(registry):
    with pytest.raises(RegistryError, match="joined the server"):
        registry.resolve("Nobody")


def test_search_looks_at_what_people_said_they_do(registry):
    assert [p.name for p in registry.search("go")] == ["Milos"]
    assert [p.name for p in registry.search("design")] == ["Ana"]


def test_store_is_never_group_or_world_readable(registry, store):
    mode = stat.S_IMODE(os.stat(store).st_mode)
    assert mode & 0o077 == 0, oct(mode)
    assert stat.S_IMODE(os.stat(store.parent).st_mode) & 0o077 == 0


def test_store_round_trips(registry, store):
    raw = json.loads(store.read_text())
    assert raw["schema"] == 1
    assert set(raw["people"]) == {"111", "222"}
    assert Registry(store).resolve("Ana").capabilities == "design and frontend"


def test_a_missing_store_is_empty_not_an_error(tmp_path):
    assert Registry(tmp_path / "nope.json").all() == []
