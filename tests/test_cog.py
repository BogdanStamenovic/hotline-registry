"""The submit path, without a human pressing the button.

What a person's press actually does is: Discord renders the modal, they type,
Discord delivers an interaction, and `RegistryModal.callback` turns it into a
row and a role. The first three are Discord's; the last is ours and is the part
that can be wrong, so it is the part that is tested here.
"""

from __future__ import annotations

import asyncio

import pytest

discord = pytest.importorskip("discord")

from hotline_registry.cog import RegistryCog, RegistryModal


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content, *, ephemeral=False, **kw):
        self.messages.append((content, ephemeral))


class FakeRole:
    name = "registered"


class FakeMember:
    def __init__(self, uid: int, name: str) -> None:
        self.id = uid
        self._name = name
        self.roles: list[FakeRole] = []
        self.added: list[FakeRole] = []

    def __str__(self) -> str:
        return self._name

    async def add_roles(self, role, reason=""):
        self.added.append(role)
        self.roles.append(role)


class FakeGuild:
    def __init__(self, gid: int, with_role: bool = True) -> None:
        self.id = gid
        self.name = "Claude contacts"
        self.roles = [FakeRole()] if with_role else []
        self.text_channels = []


class FakeInteraction:
    def __init__(self, user, guild) -> None:
        self.user = user
        self.guild = guild
        self.guild_id = guild.id if guild else None
        self.response = FakeResponse()


def submit(registry, values, *, uid=555, with_role=True, monkeypatch=None):
    """Drive one modal submission.

    Everything happens inside `asyncio.run` because py-cord's `Modal` and `View`
    build their item store against the *running* loop and raise without one --
    which is a fact about the library worth keeping visible here, since it also
    means the cog may only construct them from inside a handler.
    """
    # isinstance(member, discord.Member) has to be true for the role branch.
    monkeypatch.setattr(discord, "Member", FakeMember)
    member = FakeMember(uid, "milos#0")
    interaction = FakeInteraction(member, FakeGuild(777, with_role))

    async def go():
        modal = RegistryModal(registry)
        # Keyed by identity, and patched ONCE. All three fields are the same
        # class, so a per-field patch of `type(item).value` makes the last one
        # win for all three -- which looks exactly like the callback reading the
        # wrong field, and is not.
        answers = {id(item): value for item, value in zip(modal.children, values)}
        monkeypatch.setattr(
            type(modal.children[0]), "value",
            property(lambda self: answers.get(id(self), "")), raising=False)
        await modal.callback(interaction)

    asyncio.run(go())
    return interaction, member


def test_a_submission_becomes_a_consent_row(registry, monkeypatch):
    submit(
        registry,
        ["Milos", "milos@sip.linphone.org", "backend, Go, code review"],
        monkeypatch=monkeypatch,
    )
    person = registry.people["555"]
    assert person.sip == "sip:milos@sip.linphone.org"
    assert person.capabilities == "backend, Go, code review"
    assert person.guild_id == "777"
    assert person.callable_ and person.messageable


def test_the_reply_is_ephemeral_and_says_what_may_now_happen(registry, monkeypatch):
    interaction, _ = submit(
        registry, ["Milos", "milos@sip.linphone.org", "Go"], monkeypatch=monkeypatch)
    content, ephemeral = interaction.response.messages[0]
    assert ephemeral is True, "somebody's SIP address must not be echoed into a public channel"
    assert "message you on Discord" in content and "ring your Linphone" in content


def test_a_blank_sip_is_confirmed_back_as_do_not_call(registry, monkeypatch):
    interaction, _ = submit(registry, ["Ana", "", "design"], monkeypatch=monkeypatch)
    content, _ = interaction.response.messages[0]
    assert "nobody will call you" in content
    assert not registry.people["555"].callable_
    assert registry.people["555"].messageable


def test_submitting_grants_the_role_that_opens_the_server(registry, monkeypatch):
    _, member = submit(registry, ["Milos", "", "Go"], monkeypatch=monkeypatch)
    assert [r.name for r in member.added] == ["registered"]


def test_a_missing_role_does_not_lose_the_registration(registry, monkeypatch):
    """The consent record is the thing that matters. A role that could not be
    granted is a visibility annoyance; a lost registration is somebody who
    filled in a form for nothing."""
    interaction, member = submit(
        registry, ["Milos", "", "Go"], with_role=False, monkeypatch=monkeypatch)
    assert registry.people["555"].messageable
    assert member.added == []
    assert "could not give you the member role" in interaction.response.messages[0][0]


def test_leaving_the_server_revokes_consent(registry, monkeypatch):
    """Belt to the per-contact check's braces: this only fires if the privileged
    Server Members intent is ever switched on."""
    cog = RegistryCog(bot=None, registry=registry)
    member = FakeMember(111, "milos#0")
    member.guild = FakeGuild(777)
    monkeypatch.setattr("hotline_registry.cog.GUILD_ID", 0)
    asyncio.run(cog.on_member_remove(member))
    assert not registry.resolve("Milos").messageable
