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

**The admin half is `/registry add|edit|remove`, and only Bogdan can use it.**
Slash commands rather than buttons in the admin channel because `publish`
deletes every bot message there on each refresh -- a button would have to be
re-posted with every roster -- and because picking a person out of tens is
what autocomplete is for. The commands are hidden from non-administrators by
`default_member_permissions`, but that is a convenience a server admin can
change; the real gate is `_is_admin`, checked against his user id on every
command, every autocomplete and every modal submit. Anyone else gets an
ephemeral refusal and nothing is read or written.
"""

from __future__ import annotations

import asyncio
import logging
import os

import discord

from .admin import CHANNEL_ENV, publish
from .contact import load_env_file
from .registry import Person, Registry, RegistryError, normalise_sip, parse_discord_id

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

        # The admin channel is a view of the registry, so it is rewritten on every
        # write rather than posted once. Never allowed to fail a registration:
        # the person IS registered at this point, and telling them otherwise
        # because Discord refused a mirror post would be a lie.
        try:
            publish(self.registry)
        except Exception:
            log.exception("could not republish the admin channel")

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
    if getattr(guild, "owner_id", None) == member.id:
        # Nobody can give the owner a role, including an administrator bot --
        # the owner sits above every role in the hierarchy by definition. They
        # also already see every channel, so there is nothing to grant and
        # nothing to apologise for. Without this the very first person to fill
        # in the form -- Bogdan, in his own server -- is told something went
        # wrong when nothing did.
        return True
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
        existing = self.registry.people.get(str(getattr(interaction.user, "id", ""))) if interaction.user else None
        await interaction.response.send_modal(RegistryModal(self.registry, existing))


ADMIN_USER_ENV = "HOTLINE_REGISTRY_ADMIN_USER"
REMOVED_BY_BOGDAN = "removed by Bogdan"


def _setting(*names: str) -> str:
    env_file = load_env_file()
    for name in names:
        value = os.environ.get(name, "") or env_file.get(name, "")
        if value:
            return value
    return ""


def admin_user_id() -> int:
    """His Discord id: the registry's own setting, else hotline's gate id.

    Read at use rather than at import so a changed `.env` needs no restart.
    Unset means 0, and 0 matches nobody: the admin commands fail closed.
    """
    raw = _setting(ADMIN_USER_ENV, "DISCORD_USER_ID")
    return int(raw) if raw.isdigit() else 0


def _is_admin(user: object) -> bool:
    uid = admin_user_id()
    return bool(uid) and getattr(user, "id", None) == uid


def _admin_channel_id() -> int:
    raw = _setting(CHANNEL_ENV)
    return int(raw) if raw.isdigit() else 0


async def _refuse_unless_admin(interaction: discord.Interaction) -> bool:
    """Send the refusal and return True if this interaction may not administer."""
    if not _is_admin(interaction.user):
        log.warning("registry admin refused for %s", getattr(interaction.user, "id", "?"))
        await interaction.response.send_message(
            "Only Bogdan can change the registry by hand.", ephemeral=True)
        return True
    channel = _admin_channel_id()
    if channel and interaction.channel_id != channel:
        await interaction.response.send_message(
            f"Use this in <#{channel}>, where the roster is.", ephemeral=True)
        return True
    return False


def _link(interaction: discord.Interaction) -> str:
    return (f"https://discord.com/channels/{interaction.guild_id or '@me'}/"
            f"{interaction.channel_id} (interaction {interaction.id})")


async def _refresh(registry: Registry) -> None:
    # Off the event loop: publish is a dozen blocking HTTP calls, and the loop
    # is the gateway every agent's Discord traffic rides on.
    try:
        await asyncio.to_thread(publish, registry)
    except Exception:
        log.exception("could not republish the admin channel")


async def _whois(interaction: discord.Interaction, discord_id: str) -> tuple[str, str]:
    """(their username, the guild id if they are a member of this server).

    Membership is recorded only when seen, because it is what makes "leave the
    server" an opt-out: a hand-added member who leaves is revoked like anyone
    else, and a non-member is never revoked for not being one.
    """
    name, guild_id = "", ""
    client = getattr(interaction, "client", None)
    try:
        if client is not None:
            name = str(await client.fetch_user(int(discord_id)))
    except Exception:  # noqa: BLE001 - a name is a nicety; the id is the identity
        log.info("could not look up Discord user %s", discord_id)
    guild = interaction.guild
    if guild is not None:
        try:
            member = await guild.fetch_member(int(discord_id))
            guild_id = str(guild.id)
            name = name or str(member)
        except Exception:  # noqa: BLE001 - 404 means not a member, which is allowed
            log.info("Discord user %s is not a member of %s", discord_id, guild.id)
    return name, guild_id


class AdminPersonModal(discord.ui.Modal):
    """Add a person, or edit one, prefilled. Five fields: Discord's modal limit."""

    def __init__(self, registry: Registry, existing: Person | None = None) -> None:
        verb = "Edit" if existing else "Add"
        super().__init__(title=f"{verb} a person — registry")
        self.registry = registry
        self.key = existing.discord_id if existing else None
        # A form row has no consent note -- its consent is the form -- so the
        # field is optional when editing one and required everywhere else.
        note_required = existing is None or existing.admin_added
        self.add_item(discord.ui.InputText(
            label="Name", max_length=80, required=True,
            value=existing.name if existing else None))
        self.add_item(discord.ui.InputText(
            label="Discord user id or mention (optional)", max_length=40, required=False,
            placeholder="Right-click them → Copy User ID. Blank: no Discord.",
            value=existing.discord_id if existing and existing.has_discord else None))
        self.add_item(discord.ui.InputText(
            label="Linphone SIP address (optional)", max_length=120, required=False,
            placeholder="Blank means: never call them",
            value=existing.sip if existing else None))
        self.add_item(discord.ui.InputText(
            label="What they can help with", style=discord.InputTextStyle.long,
            max_length=1000, required=False,
            value=existing.capabilities if existing else None))
        self.add_item(discord.ui.InputText(
            label="How they agreed to be contacted" + ("" if note_required else " (optional)"),
            style=discord.InputTextStyle.long, max_length=500, required=note_required,
            placeholder="e.g. asked them on the phone on 6 Nov, they said yes",
            value=existing.consent_note if existing else None))

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _refuse_unless_admin(interaction):
            return
        name, raw_discord, sip, can_do, note = (
            (child.value or "").strip() for child in self.children)
        # Deferred because a new Discord id costs two lookups before the reply,
        # and Discord drops an interaction not answered within three seconds.
        await interaction.response.defer(ephemeral=True)
        try:
            if self.key is not None and raw_discord == self.key:
                discord_id = self.key  # unchanged prefill; nothing to parse or look up
                discord_name, guild_id = "", ""
            else:
                discord_id = parse_discord_id(raw_discord)
                discord_name, guild_id = (
                    await _whois(interaction, discord_id) if discord_id else ("", ""))
            if self.key is None:
                person = self.registry.add_admin(
                    name, consent_note=note, added_by=str(getattr(interaction.user, "id", "")),
                    added_via=_link(interaction), discord_id=discord_id,
                    discord_name=discord_name, sip=sip, capabilities=can_do,
                    guild_id=guild_id)
                verb = "Added"
            else:
                current = self.registry.people.get(self.key)
                if current is None:
                    raise RegistryError("that row no longer exists; nothing changed")
                changes = {"name": name, "sip": sip, "capabilities": can_do,
                           "consent_note": note}
                new_id = None
                if discord_id and discord_id != current.discord_id:
                    new_id = discord_id
                    changes["discord_name"] = discord_name
                    changes["guild_id"] = guild_id
                elif not discord_id and current.has_discord:
                    new_id = ""  # registry.edit refuses this, with the reason
                person = self.registry.edit(
                    self.key, edited_by=str(getattr(interaction.user, "id", "")), discord_id=new_id, **changes)
                verb = "Updated"
        except RegistryError as exc:
            await interaction.followup.send(f"Nothing saved: {exc}", ephemeral=True)
            return
        except Exception:
            log.exception("could not write the registry")
            await interaction.followup.send(
                "Something went wrong writing that down. Nothing was saved.", ephemeral=True)
            return
        await interaction.followup.send(
            f"{verb} **{person.name}** ({person.source}) — may {person.channels()}."
            + ("\nNot a member of this server, so leaving it is not their opt-out; "
               "replying STOP is." if person.has_discord and not person.guild_id
               and person.admin_added else "")
            + (" Still revoked." if person.revoked else ""),
            ephemeral=True)
        log.info("registry admin: %s %s (%s)", verb.lower(), person.name, person.discord_id)
        await _refresh(self.registry)


async def _people_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """Names for Bogdan only. Autocomplete is its own interaction, and answering
    it for anyone else would leak the roster one keystroke at a time."""
    if not _is_admin(ctx.interaction.user):
        return []
    cog = getattr(ctx, "cog", None)
    registry: Registry = cog.registry if cog is not None else Registry()
    typed = (ctx.value or "").lower()
    out = []
    for person in registry.all(include_revoked=True):
        label = f"{person.name} — {person.discord_name or 'no Discord'}"
        if person.revoked:
            label += " (revoked)"
        if typed and typed not in label.lower():
            continue
        out.append(discord.OptionChoice(name=label[:100], value=person.discord_id))
    return out[:25]


class RegistryCog(discord.Cog):
    """Everything the registry needs from the gateway, and nothing else.

    It handles exactly two events and one button. In particular it does not
    touch `on_message`, because `hotlined`'s own handler owns that and a cog
    that answered messages would answer his.
    """

    def __init__(self, bot: discord.Bot, registry: Registry | None = None) -> None:
        self.bot = bot
        self.registry = registry or Registry()

    admin = discord.SlashCommandGroup(
        "registry", "Add, edit or remove people in the contact registry (Bogdan only)",
        contexts={discord.InteractionContextType.guild},
        default_member_permissions=discord.Permissions(administrator=True),
    )

    @admin.command(name="add", description="Add a person by hand")
    async def admin_add(self, ctx: discord.ApplicationContext) -> None:
        await self.handle_add(ctx.interaction)

    # Options by decorator, not by `discord.Option(...)` annotations: this module
    # has `from __future__ import annotations`, so annotations are strings and
    # py-cord silently falls back to a required, non-autocompleted option.
    @admin.command(name="edit", description="Change somebody's entry")
    @discord.option("person", str, description="Who", autocomplete=_people_autocomplete)
    async def admin_edit(self, ctx: discord.ApplicationContext, person: str) -> None:
        await self.handle_edit(ctx.interaction, person)

    @admin.command(name="remove", description="Make somebody uncontactable (soft: the row is kept)")
    @discord.option("person", str, description="Who", autocomplete=_people_autocomplete)
    @discord.option("reason", str, description="Kept on the row", required=False,
                    default=REMOVED_BY_BOGDAN)
    async def admin_remove(self, ctx: discord.ApplicationContext, person: str,
                           reason: str = REMOVED_BY_BOGDAN) -> None:
        await self.handle_remove(ctx.interaction, person, reason)

    # The handlers take a bare interaction so tests can drive them without
    # py-cord's command machinery; the decorated methods above only unwrap.
    async def handle_add(self, interaction: discord.Interaction) -> None:
        if await _refuse_unless_admin(interaction):
            return
        self.registry.load()
        await interaction.response.send_modal(AdminPersonModal(self.registry))

    async def handle_edit(self, interaction: discord.Interaction, key: str) -> None:
        if await _refuse_unless_admin(interaction):
            return
        self.registry.load()
        person = self._pick(key)
        if person is None:
            await interaction.response.send_message(
                f"Nobody in the registry matches {key!r}.", ephemeral=True)
            return
        await interaction.response.send_modal(AdminPersonModal(self.registry, person))

    async def handle_remove(self, interaction: discord.Interaction, key: str,
                            reason: str = REMOVED_BY_BOGDAN) -> None:
        if await _refuse_unless_admin(interaction):
            return
        self.registry.load()
        person = self._pick(key)
        if person is None:
            await interaction.response.send_message(
                f"Nobody in the registry matches {key!r}.", ephemeral=True)
            return
        if person.revoked:
            await interaction.response.send_message(
                f"{person.name} is already removed ({person.revoked_reason or 'no reason kept'}).",
                ephemeral=True)
            return
        self.registry.revoke(person.discord_id, reason or REMOVED_BY_BOGDAN)
        await interaction.response.send_message(
            f"Removed **{person.name}**: no agent may contact them now. The row is kept, "
            "so `show` can say why; `/registry add` with their Discord id brings them back.",
            ephemeral=True)
        log.info("registry admin: removed %s (%s)", person.name, person.discord_id)
        await _refresh(self.registry)

    def _pick(self, key: str) -> Person | None:
        """Autocomplete sends the key; a hand-typed name is resolved as the CLI would."""
        person = self.registry.people.get(key)
        if person is not None:
            return person
        try:
            return self.registry.resolve(key)
        except RegistryError:
            return None

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
