"""`/registry add|edit|remove`: only Bogdan, only in the admin channel.

Driven through the handlers with fake interactions. What Discord itself does --
rendering the modal, delivering the submit -- needs a human press and is not
covered here.
"""

from __future__ import annotations

import asyncio

import pytest

discord = pytest.importorskip("discord")

from hotline_registry import cog as cogmod
from hotline_registry.cog import AdminPersonModal, RegistryCog, _people_autocomplete

BOGDAN = 1329
CHANNEL = 4242
SNOWFLAKE = "123456789012345678"


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.modals: list[object] = []
        self.deferred: bool | None = None

    async def send_message(self, content, *, ephemeral=False, **kw):
        assert self.deferred is None, "answered an interaction that was already deferred"
        self.messages.append((content, ephemeral))

    async def defer(self, *, ephemeral=False, **kw):
        self.deferred = ephemeral

    async def send_modal(self, modal):
        self.modals.append(modal)


class FakeFollowup:
    """Followups land in the same list, but only after an ephemeral defer --
    a followup to a public defer would post SIP addresses into the channel."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def send(self, content, *, ephemeral=False, **kw):
        assert self.response.deferred is True and ephemeral
        self.response.messages.append((content, ephemeral))

    async def send_modal(self, modal):
        self.modals.append(modal)


class FakeUser:
    def __init__(self, uid: int, name: str = "someone#0") -> None:
        self.id = uid
        self._name = name

    def __str__(self) -> str:
        return self._name


class FakeGuild:
    def __init__(self, members: dict[int, FakeUser]) -> None:
        self.id = 777
        self.members = members

    async def fetch_member(self, uid: int):
        if uid not in self.members:
            raise RuntimeError("404 Unknown Member")
        return self.members[uid]


class FakeClient:
    async def fetch_user(self, uid: int):
        return FakeUser(uid, "jovana#0")


class FakeInteraction:
    def __init__(self, uid: int = BOGDAN, channel: int = CHANNEL, members=None) -> None:
        self.user = FakeUser(uid)
        self.id = 99
        self.guild = FakeGuild(members or {})
        self.guild_id = 777
        self.channel_id = channel
        self.client = FakeClient()
        self.response = FakeResponse()
        self.followup = FakeFollowup(self.response)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("HOTLINE_REGISTRY_ADMIN_USER", str(BOGDAN))
    monkeypatch.setenv("HOTLINE_REGISTRY_ADMIN_CHANNEL", str(CHANNEL))
    refreshed: list[int] = []

    async def refresh(registry):
        refreshed.append(len(registry.people))

    monkeypatch.setattr(cogmod, "_refresh", refresh)
    return refreshed


def drive(coro_factory):
    """py-cord's Modal needs a running loop to be built, hence inside asyncio.run."""
    return asyncio.run(coro_factory())


def submit_modal(registry, interaction, values, existing=None, monkeypatch=None):
    async def go():
        modal = AdminPersonModal(registry, existing)
        answers = {id(item): value for item, value in zip(modal.children, values)}
        monkeypatch.setattr(type(modal.children[0]), "value",
                            property(lambda self: answers.get(id(self), "")), raising=False)
        await modal.callback(interaction)
    drive(go)


@pytest.mark.parametrize("handler", ["add", "edit", "remove"])
def test_anyone_else_is_refused_ephemerally_and_nothing_changes(registry, handler):
    cog = RegistryCog(bot=None, registry=registry)
    interaction = FakeInteraction(uid=555)
    before = {k: (p.revoked, p.name) for k, p in registry.people.items()}

    async def go():
        if handler == "add":
            await cog.handle_add(interaction)
        elif handler == "edit":
            await cog.handle_edit(interaction, "111")
        else:
            await cog.handle_remove(interaction, "111")
    drive(go)

    assert interaction.response.messages == [("Only Bogdan can change the registry by hand.", True)]
    assert interaction.response.modals == []
    assert {k: (p.revoked, p.name) for k, p in registry.people.items()} == before


def test_an_unset_admin_id_fails_closed(registry, monkeypatch):
    monkeypatch.delenv("HOTLINE_REGISTRY_ADMIN_USER")
    monkeypatch.setattr(cogmod, "load_env_file", lambda *a, **k: {})
    monkeypatch.delenv("DISCORD_USER_ID", raising=False)
    interaction = FakeInteraction()
    drive(lambda: RegistryCog(None, registry).handle_add(interaction))
    assert "Only Bogdan" in interaction.response.messages[0][0]


def test_he_is_sent_to_the_admin_channel_from_anywhere_else(registry):
    interaction = FakeInteraction(channel=1)
    drive(lambda: RegistryCog(None, registry).handle_add(interaction))
    assert f"<#{CHANNEL}>" in interaction.response.messages[0][0]
    assert interaction.response.modals == []


def test_add_opens_an_empty_modal(registry):
    interaction = FakeInteraction()
    drive(lambda: RegistryCog(None, registry).handle_add(interaction))
    (modal,) = interaction.response.modals
    assert modal.key is None and len(modal.children) == 5


def test_edit_opens_the_modal_prefilled(registry):
    interaction = FakeInteraction()
    drive(lambda: RegistryCog(None, registry).handle_edit(interaction, "111"))
    (modal,) = interaction.response.modals
    assert modal.key == "111"
    assert [c.value for c in modal.children][:4] == [
        "Milos", "111", "sip:milos@sip.linphone.org", "backend, Go, code review"]
    assert modal.children[4].required is False, "a form row has no consent note to require"


def test_submitting_an_add_records_him_as_the_adder(registry, monkeypatch, _settings):
    interaction = FakeInteraction(members={int(SNOWFLAKE): FakeUser(int(SNOWFLAKE))})
    submit_modal(registry, interaction,
                 ["Jovana", f"<@{SNOWFLAKE}>", "jovana", "Serbian law", "said yes on 6 Nov"],
                 monkeypatch=monkeypatch)
    person = registry.people[SNOWFLAKE]
    assert person.source == "admin" and person.added_by == str(BOGDAN)
    assert person.added_via.startswith(f"https://discord.com/channels/777/{CHANNEL}")
    assert person.discord_name == "jovana#0" and person.guild_id == "777"
    assert person.sip == "sip:jovana@sip.linphone.org"
    content, ephemeral = interaction.response.messages[0]
    assert ephemeral and content.startswith("Added **Jovana** (admin)")
    assert _settings == [3], "the admin channel is refreshed after an add"


def test_a_non_member_is_added_without_a_guild_and_told_so(registry, monkeypatch):
    interaction = FakeInteraction(members={})
    submit_modal(registry, interaction, ["Jovana", SNOWFLAKE, "", "", "yes"],
                 monkeypatch=monkeypatch)
    assert registry.people[SNOWFLAKE].guild_id == ""
    assert "replying STOP is" in interaction.response.messages[0][0]


def test_a_bad_discord_field_saves_nothing_and_says_why(registry, monkeypatch, _settings):
    interaction = FakeInteraction()
    submit_modal(registry, interaction, ["Jovana", "jovana", "", "", "yes"],
                 monkeypatch=monkeypatch)
    assert interaction.response.messages[0][0].startswith("Nothing saved:")
    assert len(registry.people) == 2 and _settings == []


def test_the_modal_submit_rechecks_who_is_submitting(registry, monkeypatch):
    interaction = FakeInteraction(uid=555)
    submit_modal(registry, interaction, ["X", "", "", "", "yes"], monkeypatch=monkeypatch)
    assert "Only Bogdan" in interaction.response.messages[0][0]
    assert len(registry.people) == 2


def test_submitting_an_edit_changes_the_row_in_place(registry, monkeypatch):
    interaction = FakeInteraction()
    existing = registry.people["222"]
    submit_modal(registry, interaction, ["Ana", "222", "ana", "design, CSS", ""],
                 existing=existing, monkeypatch=monkeypatch)
    ana = registry.people["222"]
    assert ana.capabilities == "design, CSS" and ana.callable_
    assert ana.edited_by == str(BOGDAN) and ana.source == "form"
    assert interaction.response.messages[0][0].startswith("Updated **Ana**")


def test_clearing_the_discord_field_of_a_discord_row_is_refused(registry, monkeypatch):
    interaction = FakeInteraction()
    submit_modal(registry, interaction, ["Ana", "", "", "design", ""],
                 existing=registry.people["222"], monkeypatch=monkeypatch)
    assert "identity" in interaction.response.messages[0][0]


def test_remove_is_a_soft_revoke_with_his_reason(registry, _settings):
    interaction = FakeInteraction()
    drive(lambda: RegistryCog(None, registry).handle_remove(interaction, "111"))
    milos = registry.people["111"]
    assert milos.revoked and milos.revoked_reason == "removed by Bogdan"
    assert interaction.response.messages[0][0].startswith("Removed **Milos**")
    assert _settings == [2]


def test_remove_by_typed_name_resolves_like_the_cli(registry):
    interaction = FakeInteraction()
    drive(lambda: RegistryCog(None, registry).handle_remove(interaction, "Ana", "asked to leave"))
    assert registry.people["222"].revoked_reason == "asked to leave"


def test_remove_twice_says_so(registry):
    registry.revoke("111", "removed by Bogdan")
    interaction = FakeInteraction()
    drive(lambda: RegistryCog(None, registry).handle_remove(interaction, "111"))
    assert "already removed" in interaction.response.messages[0][0]


def test_the_cog_does_not_write_back_a_stale_roster(registry, store):
    """hotlined's Registry lives for the whole uptime while the CLI writes the
    file. A revoke done from the CLI must survive the cog's next write."""
    from hotline_registry.registry import Registry

    cog = RegistryCog(None, registry)
    Registry(store).revoke("111", "from the CLI")
    drive(lambda: cog.handle_remove(FakeInteraction(), "222"))
    on_disk = Registry(store)
    assert on_disk.people["111"].revoked and on_disk.people["222"].revoked


class FakeAutocomplete:
    def __init__(self, cog, uid=BOGDAN, value=""):
        self.cog = cog
        self.value = value
        self.interaction = FakeInteraction(uid=uid)


def test_autocomplete_lists_people_for_him(registry):
    cog = RegistryCog(None, registry)
    choices = asyncio.run(_people_autocomplete(FakeAutocomplete(cog, value="mil")))
    assert [(c.name, c.value) for c in choices] == [("Milos — milos#0", "111")]


def test_autocomplete_tells_nobody_else_who_is_in_it(registry):
    cog = RegistryCog(None, registry)
    assert asyncio.run(_people_autocomplete(FakeAutocomplete(cog, uid=555))) == []


def test_the_command_is_registered_as_one_guild_only_admin_group(registry):
    async def go():
        bot = discord.Bot()
        bot.add_cog(RegistryCog(bot, registry))
        return [c.to_dict() for c in bot.pending_application_commands]

    (command,) = asyncio.run(go())
    assert command["name"] == "registry" and command["contexts"] == [0]
    assert command["default_member_permissions"] == 8
    subs = {s["name"]: s for s in command["options"]}
    assert set(subs) == {"add", "edit", "remove"}
    assert subs["edit"]["options"][0]["autocomplete"] is True
    assert subs["remove"]["options"][1]["required"] is False
