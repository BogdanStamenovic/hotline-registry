"""`add` / `edit` / `revoke --reason`, and the first-contact line, from the CLI."""

from __future__ import annotations

import json

import pytest

from hotline_registry import cli, contact
from hotline_registry.cli import main
from hotline_registry.contact import call_context, compose_message, introduction

SNOWFLAKE = "123456789012345678"


def run(argv, store, capsys):
    code = main(["--store", str(store), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def add(store, capsys, *extra):
    return run(["add", "Jovana", "--consent", "said yes on the phone",
                "--discord", SNOWFLAKE, "--can-do", "Serbian law", *extra], store, capsys)


def test_add_writes_an_admin_row_and_prints_its_key(registry, store, capsys):
    code, out, _ = add(store, capsys)
    assert code == 0 and out.strip() == SNOWFLAKE
    row = json.loads(run(["show", "Jovana", "--json"], store, capsys)[1])
    assert row["source"] == "admin" and row["added_via"] == "cli"
    assert row["added_by"].startswith("cli")
    assert row["consent_note"] == "said yes on the phone"


def test_add_needs_a_consent_note(registry, store, capsys):
    code, _, err = run(["add", "Jovana"], store, capsys)
    assert code == 2 and "--consent" in err


def test_add_dry_run_writes_nothing(registry, store, capsys):
    code, out, _ = add(store, capsys, "--dry-run")
    assert code == 0 and "would add Jovana" in out
    assert SNOWFLAKE not in json.loads(store.read_text())["people"]


def test_add_rejects_a_username_for_discord(registry, store, capsys):
    code, _, err = run(["add", "J", "--consent", "x", "--discord", "jovana"], store, capsys)
    assert code == 1 and "not a Discord user id" in err


def test_list_and_show_say_where_a_row_came_from(registry, store, capsys):
    add(store, capsys)
    _, out, _ = run(["list"], store, capsys)
    lines = {line.split()[0]: line for line in out.splitlines()[2:]}
    assert " admin " in lines["Jovana"] and " form " in lines["Milos"]
    _, shown, _ = run(["show", "Jovana"], store, capsys)
    assert "added by hand" in shown and "said yes on the phone" in shown
    assert "not yet" in shown
    assert "self-registered" in run(["show", "Milos"], store, capsys)[1]


def test_edit_changes_only_what_was_given(registry, store, capsys):
    code, _, _ = run(["edit", "Ana", "--sip", "ana"], store, capsys)
    assert code == 0
    row = json.loads(run(["show", "Ana", "--json"], store, capsys)[1])
    assert row["sip"] == "sip:ana@sip.linphone.org" and row["callable"]
    assert row["capabilities"] == "design and frontend"
    assert row["edited_by"].startswith("cli") and row["source"] == "form"


def test_edit_with_nothing_is_a_usage_error(registry, store, capsys):
    assert run(["edit", "Ana"], store, capsys)[0] == 2


def test_edit_dry_run_changes_nothing(registry, store, capsys):
    code, out, _ = run(["edit", "Ana", "--name", "Anja", "--dry-run"], store, capsys)
    assert code == 0 and "would change Ana" in out
    assert run(["show", "Ana"], store, capsys)[0] == 0


def test_revoke_keeps_the_reason(registry, store, capsys):
    run(["revoke", "Milos", "--reason", "removed by Bogdan"], store, capsys)
    _, out, _ = run(["show", "Milos"], store, capsys)
    assert "consent withdrawn (removed by Bogdan)" in out


def test_a_scratch_store_never_republishes_the_real_channel(registry, store, capsys, monkeypatch):
    """The suite once posted its fixtures over his real roster (2026-09-20).
    A `--store` copy must not be able to do the same through `add`."""
    monkeypatch.setattr(cli, "publish", lambda *a, **k: pytest.fail("published a scratch store"))
    code, _, err = add(store, capsys)
    assert code == 0 and "not the live store" in err


def test_the_live_store_is_republished_after_a_change(registry, store, capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "DEFAULT_STORE", store)
    monkeypatch.setattr(cli, "publish", lambda reg, *a, **k: seen.append(len(reg.people)) or 1)
    add(store, capsys)
    run(["edit", "Jovana", "--can-do", "law"], store, capsys)
    run(["revoke", "Jovana", "--no-publish"], store, capsys)
    assert seen == [3, 3]


# -- the first-contact line ---------------------------------------------------

def _admin(registry, **kw):
    return registry.add_admin("Jovana", consent_note="yes", added_by="1", added_via="t",
                              discord_id=SNOWFLAKE, **kw)


def test_a_form_row_is_never_introduced(registry):
    assert introduction(registry.resolve("Milos")) == ""
    assert compose_message(registry.resolve("Milos"), "hi") == "hi"


def test_an_admin_row_is_told_how_it_got_here_and_how_to_leave(registry):
    text = compose_message(_admin(registry), "hi")
    assert text.startswith("hi\n\n")
    assert "Bogdan added you to Claude's contact list" in text and "reply STOP" in text
    assert "leave the" not in text, "not a server member: leaving cannot be the way out"


def test_a_member_is_also_offered_leaving_the_server(registry):
    assert "leave the Claude contacts" in introduction(_admin(registry, guild_id="777"))


def test_a_long_message_keeps_the_introduction(registry):
    text = compose_message(_admin(registry), "x" * 5000)
    assert len(text) <= 1900 and text.endswith("contacted again.)")


def test_the_introduction_is_sent_once(registry, store, capsys, monkeypatch):
    sent = []

    def fake(method, path, body=None):
        if body and "content" in body:
            sent.append(body["content"])
        return {"id": "chan"} if path.endswith("channels") else {"id": "m"}

    monkeypatch.setattr(contact, "_discord", fake)
    _admin(registry)
    code, _, err = run(["message", "Jovana", "hello"], store, capsys)
    assert code == 0 and "replied STOP" in err
    run(["message", "Jovana", "again"], store, capsys)
    assert "Bogdan added you" in sent[0] and sent[1] == "again"


def test_dry_run_shows_the_introduction(registry, store, capsys):
    _admin(registry)
    _, out, _ = run(["message", "Jovana", "--dry-run", "hello"], store, capsys)
    assert "Bogdan added you" in out


def test_a_call_carries_the_introduction_and_marks_it_only_when_answered(registry, monkeypatch):
    person = registry.add_admin("J", consent_note="yes", added_by="1", added_via="t", sip="j")
    seen = {}

    def client(state):
        class Outcome:
            transcript, transport, detail, waited_seconds, fake, reply = None, "sip", "", 1, False, ""

        Outcome.state = state

        class Client:
            @staticmethod
            def place_call(reason, **kw):
                seen.update(kw)
                return Outcome()
        return Client

    monkeypatch.setattr(contact, "_ios_client", lambda: client("unanswered"))
    contact.call_person(person, "hi", context="ctx", registry=registry)
    assert "say so on this call" in seen["context"] and seen["context"].endswith("ctx")
    assert not registry.people[person.discord_id].introduced_at
    monkeypatch.setattr(contact, "_ios_client", lambda: client("answered"))
    contact.call_person(person, "hi", registry=registry)
    assert registry.people[person.discord_id].introduced_at
    assert call_context(registry.people[person.discord_id], "ctx") == "ctx"


def test_no_discord_cannot_be_messaged_and_says_why(registry, monkeypatch):
    monkeypatch.setattr(contact, "_discord", lambda *a, **k: pytest.fail("must not reach Discord"))
    person = registry.add_admin("J", consent_note="yes", added_by="1", added_via="t", sip="j")
    with pytest.raises(contact.ContactError, match="without a Discord account"):
        contact.send_message(person, "hi")


def test_an_admin_row_outside_the_server_is_not_revoked_for_not_being_in_it(registry, monkeypatch):
    """He may add somebody precisely because they are not a member. The default
    guild must not then 404 them out of the registry on first contact."""
    monkeypatch.setattr(contact, "GUILD_ID", "999")
    def fake(method, path, body=None):
        assert SNOWFLAKE not in path, "checked a hand-added non-member's membership"
        return {}

    monkeypatch.setattr(contact, "_discord", fake)
    person = _admin(registry)
    assert contact.still_a_member(person) is None
    _, left, unknown = contact.reconcile(registry)
    assert person not in left + unknown and registry.people[SNOWFLAKE].messageable


def test_an_admin_row_seen_in_the_server_is_revoked_when_they_leave(registry, monkeypatch):
    def fake(method, path, body=None):
        raise contact.ContactError("Discord refused (404): unknown member")

    monkeypatch.setattr(contact, "_discord", fake)
    _admin(registry, guild_id="777")
    _, left, _ = contact.reconcile(registry)
    assert [p.name for p in left] == ["Jovana"]
