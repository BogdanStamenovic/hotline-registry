from __future__ import annotations

import pytest

from hotline_registry import contact
from hotline_registry.contact import ContactError, call_person, send_message


def test_will_not_message_someone_who_withdrew(registry, monkeypatch):
    monkeypatch.setattr(contact, "_discord", lambda *a, **k: pytest.fail("must not reach Discord"))
    registry.revoke("111")
    with pytest.raises(ContactError, match="consent is withdrawn"):
        send_message(registry.resolve("Milos"), "hello")


def test_will_not_send_an_empty_message(registry, monkeypatch):
    monkeypatch.setattr(contact, "_discord", lambda *a, **k: pytest.fail("must not reach Discord"))
    with pytest.raises(ContactError, match="empty message"):
        send_message(registry.resolve("Milos"), "   ")


def test_message_opens_a_dm_then_posts(registry, monkeypatch):
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        return {"id": "chan1"} if path.endswith("channels") else {"id": "msg1"}

    monkeypatch.setattr(contact, "_discord", fake)
    assert send_message(registry.resolve("Milos"), "hello") == "msg1"
    assert calls[0][1] == "/users/@me/channels"
    assert calls[0][2] == {"recipient_id": "111"}
    assert calls[1][1] == "/channels/chan1/messages"


def test_blank_sip_is_refused_with_the_reason(registry, monkeypatch):
    monkeypatch.setattr(contact, "_ios_client", lambda: pytest.fail("must not reach the daemon"))
    with pytest.raises(ContactError, match="message me, do not"):
        call_person(registry.resolve("Ana"), "are you free?")


def test_revoked_is_refused_even_with_a_sip(registry, monkeypatch):
    monkeypatch.setattr(contact, "_ios_client", lambda: pytest.fail("must not reach the daemon"))
    registry.revoke("111")
    with pytest.raises(ContactError, match="withdrawn consent"):
        call_person(registry.resolve("Milos"), "are you free?")


def test_call_sends_their_address_not_his(registry, monkeypatch):
    seen = {}

    class FakeOutcome:
        state, reply, transcript = "answered", "yes", None
        transport, fake, detail, waited_seconds = "sip", False, "", 3.0

    class FakeClient:
        @staticmethod
        def place_call(reason, **kw):
            seen.update(kw, reason=reason)
            return FakeOutcome()

    monkeypatch.setattr(contact, "_ios_client", lambda: FakeClient)
    result = call_person(registry.resolve("Milos"), "can you review the PR?")
    assert seen["to"] == "sip:milos@sip.linphone.org"
    assert seen["callee"] == "Milos"
    # Never the chain: a fall-through would ring Bogdan with Milos's question.
    assert seen["transport"] == "sip"
    assert result.state == "answered" and result.reply == "yes"


def test_an_old_hotline_ios_is_reported_not_silently_ringing_bogdan(registry, monkeypatch):
    class OldClient:
        @staticmethod
        def place_call(reason, **kw):
            raise TypeError("place_call() got an unexpected keyword argument 'to'")

    monkeypatch.setattr(contact, "_ios_client", lambda: OldClient)
    with pytest.raises(ContactError, match="does not accept a `to` address"):
        call_person(registry.resolve("Milos"), "hi")


def test_env_file_parsing_ignores_comments_and_quotes(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# a comment\nA=1\nB="two"\nnot a pair\nC=three\n')
    assert contact.load_env_file(path) == {"A": "1", "B": "two", "C": "three"}


def test_someone_who_left_is_not_messaged_and_is_revoked(registry, monkeypatch):
    def fake(method, path, body=None):
        if method == "GET" and "/members/" in path:
            raise ContactError("Discord refused (404): unknown member")
        pytest.fail(f"must not go further than the membership check: {method} {path}")

    monkeypatch.setattr(contact, "_discord", fake)
    monkeypatch.setattr(contact, "GUILD_ID", "999")
    with pytest.raises(ContactError, match="left the server"):
        send_message(registry.resolve("Milos"), "hello", registry)
    assert not registry.resolve("Milos").messageable


def test_someone_who_left_is_not_called(registry, monkeypatch):
    def fake(method, path, body=None):
        raise ContactError("Discord refused (404): unknown member")

    monkeypatch.setattr(contact, "_discord", fake)
    monkeypatch.setattr(contact, "GUILD_ID", "999")
    monkeypatch.setattr(contact, "_ios_client", lambda: pytest.fail("must not ring"))
    with pytest.raises(ContactError, match="left the server"):
        call_person(registry.resolve("Milos"), "hi", registry=registry)


def test_discord_being_unreachable_is_not_treated_as_a_withdrawal(registry, monkeypatch):
    """The one thing this must never do: turn a network blip into a revocation."""
    sent = []

    def fake(method, path, body=None):
        if method == "GET" and "/members/" in path:
            raise ContactError("cannot reach Discord: timed out")
        sent.append(path)
        return {"id": "chan"} if path.endswith("channels") else {"id": "m1"}

    monkeypatch.setattr(contact, "_discord", fake)
    monkeypatch.setattr(contact, "GUILD_ID", "999")
    assert send_message(registry.resolve("Milos"), "hello", registry) == "m1"
    assert registry.resolve("Milos").messageable


def test_no_guild_on_record_means_unknown_not_refused(registry, monkeypatch):
    monkeypatch.setattr(contact, "GUILD_ID", "")
    assert contact.still_a_member(registry.resolve("Milos")) is None


def test_reconcile_revokes_only_the_ones_that_404(registry, monkeypatch):
    monkeypatch.setattr(contact, "GUILD_ID", "999")

    def fake(method, path, body=None):
        if path.endswith("/members/222"):
            raise ContactError("Discord refused (404): unknown member")
        return {}

    monkeypatch.setattr(contact, "_discord", fake)
    present, left, unknown = contact.reconcile(registry)
    assert [p.name for p in present] == ["Milos"]
    assert [p.name for p in left] == ["Ana"]
    assert unknown == []
    assert not registry.resolve("Ana").messageable
