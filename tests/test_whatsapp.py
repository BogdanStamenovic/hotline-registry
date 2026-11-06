"""WhatsApp in the registry: the number, the order, and the routing in front of the wire.

Nothing here reaches WhatsApp. `wa` is a fake script that records its argv and
answers with wacli's real JSON envelope; numbers are fake (+381600000000).
"""

from __future__ import annotations

import json

import pytest

from hotline_registry import contact
from hotline_registry.cli import main
from hotline_registry.registry import (
    CALL_CHANNELS,
    MESSAGE_CHANNELS,
    Person,
    normalise_whatsapp,
    parse_via,
)

NUMBER = "+381600000000"


@pytest.fixture
def fake_wa(tmp_path, monkeypatch):
    """A `wa` that records how it was called and reports a sent message."""
    log = tmp_path / "wa-calls.jsonl"
    script = tmp_path / "wa"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['auth', 'status']:\n"
        "    print(json.dumps({'success': True, 'data': {'authenticated': False}, 'error': None}))\n"
        "else:\n"
        "    print(json.dumps({'success': True, 'data': {'sent': True, 'to': 'x', 'id': 'WAID1'},"
        " 'error': None}))\n")
    script.chmod(0o755)
    monkeypatch.setenv("HOTLINE_REGISTRY_WA", str(script))

    def calls() -> list[list[str]]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


def person(**kw) -> Person:
    base = {"discord_id": "333", "discord_name": "vesna#0", "name": "Vesna", "whatsapp": NUMBER}
    base.update(kw)
    return Person(**base)


# ---- the record --------------------------------------------------------------


def test_numbers_are_normalised_and_garbage_is_blank():
    assert normalise_whatsapp("+381 60 000-0000") == NUMBER
    assert normalise_whatsapp("00381600000000") == NUMBER
    for raw in ("", "060 000 0000", "vesna", "+1"):
        assert normalise_whatsapp(raw) == ""


def test_one_preference_answer_fills_both_orders():
    assert parse_via("WhatsApp, Linphone", MESSAGE_CHANNELS) == "whatsapp"
    assert parse_via("WhatsApp, Linphone", CALL_CHANNELS) == "whatsapp,sip"
    assert parse_via("discord > whatsapp", MESSAGE_CHANNELS) == "discord,whatsapp"
    assert parse_via("pigeon", CALL_CHANNELS) == ""


def test_default_order_is_the_old_behaviour():
    p = person(sip="sip:v@sip.linphone.org")
    assert p.message_routes() == ["discord", "whatsapp"]
    assert p.call_routes() == ["sip", "whatsapp"]


def test_preference_reorders_but_only_over_channels_they_gave():
    p = person(message_via="whatsapp", call_via="whatsapp,sip")
    assert p.message_routes() == ["whatsapp", "discord"]
    assert p.call_routes() == ["whatsapp"], "no SIP given, so SIP is not a route"


def test_whatsapp_alone_makes_someone_messageable_and_callable():
    p = person(discord_id="admin-1234", source="admin")
    assert p.messageable and p.callable_
    assert "whatsapp" in p.channels()


def test_a_blank_whatsapp_changes_nothing():
    p = person(whatsapp="", sip="")
    assert p.channels() == "message"
    assert not p.callable_


def test_old_rows_read_back_without_whatsapp(registry):
    assert registry.resolve("Milos").whatsapp == ""
    assert registry.resolve("Milos").call_routes() == ["sip"]


def test_edit_validates_and_normalises_the_number(registry):
    p = registry.edit("111", edited_by="t", whatsapp="+381 60 000 0000", call_via="whatsapp")
    assert p.whatsapp == NUMBER and p.call_routes() == ["whatsapp", "sip"]
    with pytest.raises(Exception, match="not a WhatsApp number"):
        registry.edit("111", edited_by="t", whatsapp="0600000000")


# ---- messages ----------------------------------------------------------------


def test_whatsapp_first_goes_out_through_wa_send_to_a_jid(fake_wa, monkeypatch):
    monkeypatch.setattr(contact, "_discord", lambda *a, **k: pytest.fail("must not use Discord"))
    monkeypatch.setattr(contact, "still_a_member", lambda p: None)
    via, mid = contact.deliver_message(person(message_via="whatsapp"), "zdravo")
    assert (via, mid) == ("whatsapp", "WAID1")
    argv = fake_wa()[-1]
    assert argv[:2] == ["send", "text"]
    # A JID, so wacli cannot fuzzy-match a contact name.
    assert argv[argv.index("--to") + 1] == "381600000000@s.whatsapp.net"
    assert argv[argv.index("--message") + 1] == "zdravo"


def test_a_failing_discord_falls_through_to_whatsapp(fake_wa, monkeypatch):
    def refuse(*a, **k):
        raise contact.ContactError("Discord refused (403): blocked DMs")
    monkeypatch.setattr(contact, "_discord", refuse)
    monkeypatch.setattr(contact, "still_a_member", lambda p: None)
    assert contact.deliver_message(person(), "hi")[0] == "whatsapp"


def test_leaving_the_server_stops_every_channel(fake_wa, registry, monkeypatch):
    registry.edit("111", edited_by="t", whatsapp=NUMBER)
    monkeypatch.setattr(contact, "still_a_member", lambda p: False)
    with pytest.raises(contact.ContactError, match="left the server"):
        contact.deliver_message(registry.resolve("Milos"), "hi", registry)
    assert fake_wa() == [], "consent was withdrawn; WhatsApp must not be tried either"


def test_via_forces_one_channel_and_refuses_one_they_lack(fake_wa, monkeypatch):
    monkeypatch.setattr(contact, "still_a_member", lambda p: None)
    with pytest.raises(contact.ContactError, match="cannot be messaged on whatsapp"):
        contact.deliver_message(person(whatsapp=""), "hi", via="whatsapp")


def test_a_wa_refusal_is_reported_not_swallowed(tmp_path, monkeypatch):
    script = tmp_path / "wa"
    script.write_text("#!/bin/sh\necho '{\"success\":false,\"data\":null,\"error\":\"not authenticated\"}'\n")
    script.chmod(0o755)
    monkeypatch.setenv("HOTLINE_REGISTRY_WA", str(script))
    monkeypatch.setattr(contact, "still_a_member", lambda p: None)
    with pytest.raises(contact.ContactError, match="not authenticated"):
        contact.deliver_message(person(discord_id="admin-1", source="admin"), "hi")


def test_the_suite_cannot_reach_the_real_wa(monkeypatch):
    monkeypatch.setattr(contact, "still_a_member", lambda p: None)
    with pytest.raises(contact.ContactError, match="real wa"):
        contact.deliver_message(person(discord_id="admin-1", source="admin"), "hi")


# ---- calls -------------------------------------------------------------------


def recording_client(seen: dict):
    class Outcome:
        state, reply, transcript, transport, fake, detail = "ringing", "", None, "x", False, ""
        waited_seconds, via = 0.0, "whatsapp"

    class Client:
        @staticmethod
        def place_call(reason, **kw):
            seen.update(kw)
            return Outcome()
    return Client


def test_a_whatsapp_first_call_sends_both_addresses_and_the_order(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(contact, "_ios_client", lambda: recording_client(seen))
    monkeypatch.setattr(contact, "still_a_member", lambda p: None)
    result = contact.call_person(person(sip="sip:v@sip.linphone.org", call_via="whatsapp"), "r")
    assert seen["whatsapp"] == NUMBER and seen["to"] == "sip:v@sip.linphone.org"
    assert seen["via"] == "auto" and seen["via_order"] == ["whatsapp", "sip"]
    assert result.via == "whatsapp"


def test_a_sip_only_call_is_the_request_an_old_client_accepts(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(contact, "_ios_client", lambda: recording_client(seen))
    monkeypatch.setattr(contact, "still_a_member", lambda p: None)
    contact.call_person(person(whatsapp="", sip="sip:v@sip.linphone.org"), "r")
    assert "whatsapp" not in seen and "via" not in seen and seen["transport"] == "sip"


def test_a_whatsapp_only_person_is_rung_on_whatsapp_only(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(contact, "_ios_client", lambda: recording_client(seen))
    monkeypatch.setattr(contact, "still_a_member", lambda p: None)
    contact.call_person(person(), "r")
    assert seen["to"] == "" and seen["via"] == "whatsapp"


# ---- the CLI and the form --------------------------------------------------------


def run(argv, store, capsys):
    code = main(["--store", str(store), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_show_and_list_display_whatsapp(registry, store, capsys):
    registry.edit("111", edited_by="t", whatsapp=NUMBER, message_via="whatsapp")
    code, out, _ = run(["show", "Milos"], store, capsys)
    assert code == 0 and NUMBER in out and "messages via  whatsapp > discord" in out
    code, out, _ = run(["list", "--json"], store, capsys)
    row = next(r for r in json.loads(out) if r["name"] == "Milos")
    assert row["whatsapp"] == NUMBER and row["call_via"] == ["sip", "whatsapp"]


def test_dry_run_message_names_the_channel(registry, store, capsys, fake_wa):
    registry.edit("111", edited_by="t", whatsapp=NUMBER, message_via="whatsapp")
    code, out, _ = run(["message", "Milos", "--dry-run", "hi"], store, capsys)
    assert code == 0 and f"on WhatsApp ({NUMBER})" in out and "then discord" in out
    assert fake_wa() == []


def test_dry_run_call_lists_the_order(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "_ios_client", lambda: pytest.fail("must not ring"))
    registry.edit("111", edited_by="t", whatsapp=NUMBER, call_via="whatsapp")
    _, out, _ = run(["call", "Milos", "--dry-run", "free?"], store, capsys)
    assert f"would ring Milos at WhatsApp {NUMBER}, then sip:milos@sip.linphone.org" in out


def test_add_takes_whatsapp(store, capsys):
    code, _, _ = run(["add", "Vesna", "--consent", "said yes on the phone", "--whatsapp",
                        "+381 60 000 0000", "--call-via", "whatsapp", "--no-publish"],
                       store, capsys)
    assert code == 0
    from hotline_registry.registry import Registry

    p = Registry(store).resolve("Vesna")
    assert p.whatsapp == NUMBER and p.callable_ and p.call_routes() == ["whatsapp"]


def test_the_form_takes_whatsapp_and_a_preference(registry, monkeypatch):
    from test_cog import submit

    interaction, _ = submit(
        registry, ["Vesna", "", "translation", "+381 60 000 0000", "WhatsApp, Discord"],
        monkeypatch=monkeypatch)
    p = registry.people["555"]
    assert p.whatsapp == NUMBER and p.capabilities == "translation"
    assert p.message_routes() == ["whatsapp", "discord"] and p.call_routes() == ["whatsapp"]
    content = interaction.response.messages[0][0]
    assert "call you on WhatsApp" in content and "nobody will call you" not in content


def test_an_unreadable_number_is_said_back_not_saved(registry, monkeypatch):
    from test_cog import submit

    interaction, _ = submit(registry, ["Vesna", "", "x", "060 123", ""], monkeypatch=monkeypatch)
    assert registry.people["555"].whatsapp == ""
    assert "could not read '060 123'" in interaction.response.messages[0][0]
