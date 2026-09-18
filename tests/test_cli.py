from __future__ import annotations

import json

import pytest

from hotline_registry import contact
from hotline_registry.cli import main


def run(argv, store, capsys):
    code = main(["--store", str(store), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "hotline-registry" in capsys.readouterr().out


def test_no_command_is_a_usage_error(capsys):
    assert main([]) == 2


def test_list_is_json_an_agent_can_parse(registry, store, capsys):
    code, out, _ = run(["list", "--json"], store, capsys)
    assert code == 0
    people = json.loads(out)
    assert {p["name"] for p in people} == {"Milos", "Ana"}
    assert next(p for p in people if p["name"] == "Ana")["callable"] is False


def test_list_callable_only(registry, store, capsys):
    _, out, _ = run(["list", "--callable", "--json"], store, capsys)
    assert [p["name"] for p in json.loads(out)] == ["Milos"]


def test_list_hides_revoked_until_asked(registry, store, capsys):
    registry.revoke("111")
    assert [p["name"] for p in json.loads(run(["list", "--json"], store, capsys)[1])] == ["Ana"]
    both = json.loads(run(["list", "--all", "--json"], store, capsys)[1])
    assert {p["name"] for p in both} == {"Milos", "Ana"}


def test_find_searches_capabilities(registry, store, capsys):
    _, out, _ = run(["find", "review", "--json"], store, capsys)
    assert [p["name"] for p in json.loads(out)] == ["Milos"]


def test_show_says_what_may_be_done(registry, store, capsys):
    code, out, _ = run(["show", "Ana"], store, capsys)
    assert code == 0
    assert "you may       message" in out
    assert "(none given)" in out


def test_show_unknown_person_fails(registry, store, capsys):
    code, _, err = run(["show", "Nobody"], store, capsys)
    assert code == 1 and "nobody in the registry" in err


def test_dry_run_message_sends_nothing(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "_discord", lambda *a, **k: pytest.fail("must not send"))
    code, out, _ = run(["message", "Milos", "--dry-run", "hello", "there"], store, capsys)
    assert code == 0
    assert "would DM Milos" in out and "hello there" in out


def test_dry_run_call_rings_nothing(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "_ios_client", lambda: pytest.fail("must not ring"))
    code, out, _ = run(["call", "Milos", "--dry-run", "are", "you", "free?"], store, capsys)
    assert code == 0
    assert "would ring Milos at sip:milos@sip.linphone.org" in out


def test_dry_run_call_says_when_it_would_refuse(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "_ios_client", lambda: pytest.fail("must not ring"))
    code, out, _ = run(["call", "Ana", "--dry-run", "hi"], store, capsys)
    assert code == 0 and "would NOT call Ana" in out


def test_message_prints_only_the_id_on_stdout(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "_discord",
                        lambda m, p, b=None: {"id": "chan"} if p.endswith("channels") else {"id": "m1"})
    code, out, err = run(["message", "Milos", "hello"], store, capsys)
    assert code == 0 and out.strip() == "m1"
    assert "sent to Milos" in err


def test_message_needs_something_to_send(registry, store, capsys):
    assert run(["message", "Milos"], store, capsys)[0] == 2


def test_call_needs_a_reason(registry, store, capsys):
    assert run(["call", "Milos"], store, capsys)[0] == 2


def _client(state, *, fake=False, reply=""):
    class Outcome:
        transcript, transport, detail, waited_seconds = None, "sip", "", 1.0

    Outcome.state, Outcome.fake, Outcome.reply = state, fake, reply

    class Client:
        @staticmethod
        def place_call(reason, **kw):
            return Outcome()

    return Client


def test_call_answered_prints_what_they_said(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "_ios_client", lambda: _client("answered", reply="sure, tomorrow"))
    code, out, _ = run(["call", "Milos", "free?"], store, capsys)
    assert code == 0 and out.strip() == "sure, tomorrow"


def test_call_declined_is_exit_4(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "_ios_client", lambda: _client("declined"))
    assert run(["call", "Milos", "free?"], store, capsys)[0] == 4


def test_call_unanswered_is_exit_3(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "_ios_client", lambda: _client("unanswered"))
    assert run(["call", "Milos", "free?"], store, capsys)[0] == 3


def test_a_fake_doorbell_is_a_failure_not_a_success(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "_ios_client", lambda: _client("answered", fake=True))
    code, _, err = run(["call", "Milos", "free?"], store, capsys)
    assert code == 1 and "nothing rang" in err


def test_revoke_dry_run_changes_nothing(registry, store, capsys):
    code, out, _ = run(["revoke", "Milos", "--dry-run"], store, capsys)
    assert code == 0 and "would revoke Milos" in out
    assert registry.resolve("Milos").messageable


def test_revoke(registry, store, capsys):
    assert run(["revoke", "Milos"], store, capsys)[0] == 0
    from hotline_registry.registry import Registry

    assert not Registry(store).resolve("Milos").messageable


def test_status_reports_both_channels(registry, store, capsys, monkeypatch):
    monkeypatch.setattr(contact, "call_health", lambda: {"ok": False, "error": "daemon down"})
    monkeypatch.setattr("hotline_registry.cli.call_health",
                        lambda: {"ok": False, "error": "daemon down"})
    code, out, _ = run(["status"], store, capsys)
    assert code == 0
    assert "2 contactable, 1 of them callable" in out
    assert "daemon down" in out


def test_a_refused_call_is_not_narrated_as_a_ring(registry, store, capsys, monkeypatch):
    """Ana gave no SIP. Saying "ringing Ana at " and then refusing describes a
    call that never happened."""
    monkeypatch.setattr(contact, "_ios_client", lambda: pytest.fail("must not ring"))
    code, _, err = run(["call", "Ana", "are you free?"], store, capsys)
    assert code == 1
    assert "ringing Ana" not in err
    assert "do not ring me" in err
