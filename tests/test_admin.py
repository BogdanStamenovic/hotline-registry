"""The private admin channel that mirrors the registry.

The thing worth testing is that it is a *mirror* and not a snapshot: a roster
posted once is a status field, true when written and quietly wrong the moment
somebody registers.
"""

from __future__ import annotations

import pytest

from hotline_registry.admin import LIMIT, publish, render
from hotline_registry.registry import Person, Registry


def _person(name: str, sip: str = "sip:x@sip.linphone.org", **kw) -> Person:
    return Person(
        discord_id=kw.pop("discord_id", name.lower()),
        discord_name=f"{name.lower()} ({name})",
        name=name,
        sip=sip,
        capabilities=kw.pop("capabilities", "things"),
        **kw,
    )


def test_everyone_registered_appears():
    out = "\n".join(render([_person("Ana"), _person("Bob")]))
    assert "Ana" in out and "Bob" in out
    assert "2 registered" in out


def test_capabilities_are_not_truncated():
    """He reads this instead of going to a terminal; a summary he cannot trust
    is worth less than no summary."""
    said = "I can " + "do a very specific thing " * 10
    out = "\n".join(render([_person("Ana", capabilities=said)]))
    assert said.strip() in out


def test_somebody_with_no_sip_is_marked_uncallable():
    out = "\n".join(render([_person("Ana", sip="")]))
    assert "do not call" in out
    assert "message" in out


def test_revoked_people_are_shown_as_revoked_not_dropped():
    """Dropping them would make 'who used to be reachable' unanswerable."""
    gone = _person("Ana")
    gone.revoked = True
    out = "\n".join(render([gone, _person("Bob")]))
    assert "revoked" in out
    assert "Ana" in out
    assert "1 registered" in out


def test_long_rosters_are_split_under_discords_limit():
    people = [_person(f"P{i}", capabilities="x" * 900) for i in range(12)]
    chunks = render(people)
    assert len(chunks) > 1
    assert all(len(c) <= LIMIT for c in chunks)


def test_one_person_longer_than_a_message_still_splits():
    chunks = render([_person("Ana", capabilities="y" * 4000)])
    assert all(len(c) <= LIMIT for c in chunks)


def test_publish_without_a_channel_configured_is_a_warning_not_a_crash(monkeypatch, tmp_path):
    """A registry write must never fail because the mirror could not be updated:
    the registry is the record, the channel is only a view of it."""
    monkeypatch.setenv("HOTLINE_REGISTRY_ADMIN_CHANNEL", "")
    monkeypatch.setenv("HOTLINE_BOT_TOKEN", "")
    monkeypatch.setattr("hotline_registry.admin.load_env_file", lambda *a, **k: {})
    assert publish(Registry(tmp_path / "r.json")) == 0


def test_publish_survives_discord_refusing(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("discord is down")

    monkeypatch.setattr("hotline_registry.admin._request", boom)
    monkeypatch.setattr(
        "hotline_registry.admin.load_env_file",
        lambda *a, **k: {"HOTLINE_REGISTRY_ADMIN_CHANNEL": "1", "HOTLINE_BOT_TOKEN": "t"},
    )
    assert publish(Registry(tmp_path / "r.json")) == 0


def test_publish_replaces_its_own_messages_rather_than_appending(monkeypatch, tmp_path):
    """Run twice, and the channel must hold one roster -- not two."""
    calls: list[tuple[str, str]] = []

    def fake(method, path, token, body=None):
        calls.append((method, path))
        if path == "/users/@me":
            return {"id": "bot"}
        if method == "GET" and "/messages" in path:
            return [{"id": "old1", "author": {"id": "bot"}},
                    {"id": "human", "author": {"id": "someone-else"}}]
        return {}

    monkeypatch.setattr("hotline_registry.admin._request", fake)
    monkeypatch.setattr(
        "hotline_registry.admin.load_env_file",
        lambda *a, **k: {"HOTLINE_REGISTRY_ADMIN_CHANNEL": "42", "HOTLINE_BOT_TOKEN": "t"},
    )
    registry = Registry(tmp_path / "r.json")
    registry.upsert(_person("Ana"))

    assert publish(registry) == 1
    assert ("DELETE", "/channels/42/messages/old1") in calls
    # Somebody else's message is not ours to delete.
    assert ("DELETE", "/channels/42/messages/human") not in calls


def test_the_suite_cannot_reach_discord():
    """The guard that exists because the suite once posted its own fixtures into
    Bogdan's real private channel, over the real roster."""
    from hotline_registry import admin

    with pytest.raises(RuntimeError, match="tried to talk to Discord"):
        admin._request("GET", "/users/@me", "token")


def test_the_guard_is_what_stands_between_a_registration_and_discord(registry, monkeypatch):
    """The conftest guard is load-bearing, not decorative.

    Swap it for a recorder and a single modal submit reaches the REAL channel id
    out of ~/data/hotline/.env -- which is exactly what happened on 2026-09-20,
    when the suite posted its own fixtures over the roster in Bogdan's private
    channel. This test fails if the registration path stops republishing, which
    is the moment the guard could be reconsidered.
    """
    from tests.test_cog import submit

    reached: list[str] = []
    monkeypatch.setattr(
        "hotline_registry.admin._request",
        lambda method, path, *a, **k: reached.append(path),
    )
    submit(registry, ["Ana", "", "design"], monkeypatch=monkeypatch)

    assert reached, "a registration no longer republishes the admin channel"
    assert any("/channels/" in path for path in reached), reached
