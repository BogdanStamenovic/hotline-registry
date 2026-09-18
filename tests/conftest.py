from __future__ import annotations

import pytest

from hotline_registry.registry import Person, Registry


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
