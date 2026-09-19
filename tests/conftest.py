from __future__ import annotations

import pytest

from hotline_registry.registry import Person, Registry


@pytest.fixture(autouse=True)
def _no_real_discord(monkeypatch):
    """No test may reach Discord. This is not hypothetical.

    On 2026-09-20 the registration path grew a call that republishes the admin
    channel. `test_cog.py` exercises that path, `publish()` resolves its token
    and channel from ~/data/hotline/.env like the real thing does, and the suite
    duly posted its own fixtures -- Ana, and a Bogdan who "can do: everything" --
    over the real roster in Bogdan's private channel. He found it before we did.

    The lesson is not "mock harder in that one test": any future code on a tested
    path can reach the network the same way, and the credentials are always
    there to be found. So the network is closed by default for the whole suite
    and a test that wants it has to say so, rather than the other way round.
    """
    def refuse(*args, **kwargs):
        raise RuntimeError(
            "a test tried to talk to Discord. If that is deliberate, patch "
            "hotline_registry.admin._request (or contact._discord) in the test itself."
        )

    monkeypatch.setattr("hotline_registry.admin._request", refuse)


@pytest.fixture()
def store(tmp_path):
    return tmp_path / "registry.json"


@pytest.fixture()
def registry(store) -> Registry:
    reg = Registry(store)
    reg.upsert(Person(discord_id="111", discord_name="milos#0", name="Milos",
                      sip="sip:milos@sip.linphone.org", capabilities="backend, Go, code review"))
    reg.upsert(Person(discord_id="222", discord_name="ana#0", name="Ana",
                      sip="", capabilities="design and frontend"))
    return reg
