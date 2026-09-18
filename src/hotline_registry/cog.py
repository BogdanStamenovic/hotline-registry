"""The `#registry` form, as a real Discord modal.

**Why this is a cog and not a program.** A bot gets one gateway identity, and
`hotlined` already holds it -- it is the thing carrying every agent's contact
with Bogdan. Standing up a second client on the same token to serve a button
would, at best, duplicate every event both processes see; at worst it disturbs
the one connection nobody can afford to lose. So the registry's Discord half
ships as a cog in this repository and `hotlined` loads it. One connection, and
the code still lives with the tool it belongs to.

**Why a modal rather than "post your details in the channel".** Bogdan asked for
a form. A modal is the only Discord surface where the fields are actually fields:
the three answers arrive separately and already labelled, so nothing has to guess
which line of a free-text post was the SIP address. It also means the button can
be pressed again to correct a typo, which a posted message cannot.

**The button is persistent.** `custom_id` is fixed and the view has `timeout=None`,
so it keeps working across restarts of `hotlined` without anybody re-posting the
message. A view with a timeout would look identical and quietly stop responding
the first time the daemon was restarted, which is the failure mode this comment
exists to prevent somebody reintroducing.
"""

from __future__ import annotations

import logging
import os

import discord

from .registry import Person, Registry, normalise_sip

log = logging.getLogger("hotline-registry.cog")

GUILD_ID = int(os.environ.get("HOTLINE_REGISTRY_GUILD", "0") or 0)
REGISTERED_ROLE = "registered"
BUTTON_ID = "hotline-registry:open-form"

WELCOME = (
    "**This is the registry. Filling it in is the whole membership.**\n\n"
    "Bogdan runs agents that do real work, and sometimes one of them needs a "
    "person -- a question answered, a review, a heads-up. This form is how an "
    "agent is *allowed* to reach you. Nobody is contacted who is not in here.\n\n"
    "**Three fields:**\n"
    "- **Name** -- what you want to be called.\n"
    "- **Linphone SIP address** -- optional. Fill it in and an agent may ring "
    "your phone through Linphone; it is a free SIP-to-SIP call. **Leave it blank "
    "and you will never be called**, only messaged. That is a complete answer.\n"
    "- **What you can do** -- in your own words. This is what an agent searches "
    "when it is looking for somebody who knows about a thing.\n\n"
    "You can press the button again any time to change your answers. "
    "Leave the server and you stop being contactable, immediately and without asking."
)


class RegistryModal(discord.ui.Modal):
    """The form itself. Three fields, exactly as he specified them."""

    def __init__(self, registry: Registry, existing: Person | None = None) -> None:
        super().__init__(title="Claude contacts — registry")
        self.registry = registry
        self.add_item(discord.ui.InputText(
            label="Name",
            placeholder="What should we call you?",
            value=existing.name if existing else None,
            max_length=80,
            required=True,
        ))
        self.add_item(discord.ui.InputText(
            label="Linphone SIP address (optional)",
            placeholder="sip:you@sip.linphone.org  — blank means: do not call me",
            value=existing.sip if existing else None,
            max_length=120,
            required=False,
        ))
        self.add_item(discord.ui.InputText(
            label="What can you do?",
            style=discord.InputTextStyle.long,
            placeholder="In your own words. An agent searches this to find the right person.",
            value=existing.capabilities if existing else None,
            max_length=1000,
            required=True,
        ))

    async def callback(self, interaction: discord.Interaction) -> None:
        user = interaction.user
        if user is None:
            return
        name = (self.children[0].value or "").strip()
        sip = normalise_sip(self.children[1].value or "")
        capabilities = (self.children[2].value or "").strip()

        person = Person(
            discord_id=str(user.id),
            discord_name=str(user),
            name=name or str(user),
            sip=sip,
            capabilities=capabilities,
            guild_id=str(interaction.guild_id or ""),
        )
        try:
            self.registry.upsert(person)
        except Exception:
            log.exception("could not write the registry")
            await interaction.response.send_message(
                "Something went wrong writing that down, and I would rather say so than "
                "pretend. Nothing was saved — try again in a moment.",
                ephemeral=True,
            )
            return

        granted = await _grant_registered(interaction)
        how = "message you on Discord" + (" and ring your Linphone" if sip else "")
        await interaction.response.send_message(
            f"Thanks {person.name} — you're in the registry.\n"
            f"An agent may now **{how}**."
            + ("" if sip else "\n\nYou left the SIP field blank, so nobody will call you.")
            + ("" if granted else "\n\n(I could not give you the member role — Bogdan will sort it.)"),
            ephemeral=True,
        )
        log.info("registry: %s (%s) registered, callable=%s", person.name, user.id, bool(sip))


async def _grant_registered(interaction: discord.Interaction) -> bool:
    """Open the rest of the server. Failure here is reported, never silent.

    Deliberately not fatal to the registration: the consent record is the thing
    that matters and it is already written. A missing role is a visibility
    annoyance Bogdan can fix in the UI; a lost registration is somebody who
    filled in a form for nothing.
    """
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return False
    role = discord.utils.get(guild.roles, name=REGISTERED_ROLE)
    if role is None:
        log.warning("no %r role in guild %s", REGISTERED_ROLE, guild.id)
        return False
    if role in member.roles:
        return True
    try:
        await member.add_roles(role, reason="completed the registry form")
        return True
    except discord.HTTPException:
        log.exception("could not add %r to %s", REGISTERED_ROLE, member.id)
        return False


class RegistryView(discord.ui.View):
    """The button that lives in `#registry` forever."""

    def __init__(self, registry: Registry) -> None:
        super().__init__(timeout=None)
        self.registry = registry

    @discord.ui.button(label="Fill in the registry", style=discord.ButtonStyle.primary,
                       custom_id=BUTTON_ID, emoji="📇")
    async def open_form(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        existing = self.registry.people.get(str(interaction.user.id)) if interaction.user else None
        await interaction.response.send_modal(RegistryModal(self.registry, existing))


class RegistryCog(discord.Cog):
    """Everything the registry needs from the gateway, and nothing else.

    It handles exactly two events and one button. In particular it does not
    touch `on_message`, because `hotlined`'s own handler owns that and a cog
    that answered messages would answer his.
    """

    def __init__(self, bot: discord.Bot, registry: Registry | None = None) -> None:
        self.bot = bot
        self.registry = registry or Registry()

    @discord.Cog.listener()
    async def on_ready(self) -> None:
        # Re-attaching the view is what makes the button survive a restart; the
        # message in #registry is not re-posted and does not need to be.
        self.bot.add_view(RegistryView(self.registry))
        log.info("registry cog ready; %d people on file", len(self.registry.people))

    @discord.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Point them at the form. Needs the Server Members intent; without it
        this simply never fires and the channel message still explains itself."""
        if GUILD_ID and member.guild.id != GUILD_ID:
            return
        channel = discord.utils.get(member.guild.text_channels, name="registry")
        where = channel.mention if channel else "#registry"
        try:
            await member.send(
                f"Welcome to {member.guild.name}. Before anything else, please fill in "
                f"the form in {where} — it is the only thing that lets an agent contact "
                "you, and it takes about twenty seconds."
            )
        except discord.HTTPException:
            log.info("could not DM %s on join; the channel message covers it", member.id)

    @discord.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Leaving the server withdraws consent. Immediately, and without asking.

        This is the half of the consent model that is easy to forget to build.
        A registry that only ever grows is an address list: the whole claim that
        membership *is* the permission depends on the permission ending when the
        membership does.
        """
        if GUILD_ID and member.guild.id != GUILD_ID:
            return
        person = self.registry.revoke(str(member.id))
        if person is not None:
            log.info("registry: %s left the server; consent revoked", person.name)


def setup(bot: discord.Bot) -> None:
    """Entry point for `bot.load_extension("hotline_registry.cog")`."""
    bot.add_cog(RegistryCog(bot))
