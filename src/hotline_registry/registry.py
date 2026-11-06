"""The registry itself: who consented to be contacted, and on which channels.

**The store is not a contact list, it is a consent record.** An entry gets
here one of two ways, and every row says which (`source`):

- `form` -- they joined the "Claude contacts" server and filled in the form.
  That act, theirs, is the standing permission for an agent to reach them.
  `upsert` takes a Discord id it can only have got from an interaction the
  person performed. Rows written before `source` existed have no such key and
  read back as `form`, which is what they all were.
- `admin` -- Bogdan added them by hand (2026-11-06: *"the ability for me to
  edit the admin registry so i can add or unadd people"*). The consent is then
  his claim about them, so the row carries who added it (`added_by`), where
  (`added_via`) and his note on how they agreed (`consent_note`), and the first
  agent to reach them says so (`introduced_at`). A row that hides that it was
  typed in by somebody else is an address book with a consent-shaped label on
  it; one that says so is an honest record of a second kind of consent.

Two channels, two separate permissions, because they are not the same promise:

| Channel | Granted by | Withdrawn by |
|---|---|---|
| Discord DM | joining the server and submitting the form | leaving the server, or `revoked` |
| Phone (SIP) | typing a Linphone address into the form | clearing that field, or leaving |
| WhatsApp (message and call) | typing a WhatsApp number into the form | clearing that field, or leaving |

Leaving the SIP field empty is a complete answer -- it means "message me, do not
ring me" -- so an empty `sip` is never an error, only a `callable` of False.

The file lives outside the repository on purpose (`~/.local/share`, mode 600).
Bogdan's instruction, 2026-09-18: *"make the code itself public. But make the db
with the contacts private"*.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


class RegistryError(Exception):
    """Raised when hotline-registry cannot complete an operation."""


DEFAULT_STORE = pathlib.Path(
    os.environ.get("HOTLINE_REGISTRY_STORE", "")
    or pathlib.Path.home() / ".local/share/hotline-registry/registry.json"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_sip(raw: str) -> str:
    """Make a SIP address out of whatever someone typed into the form.

    People write their Linphone address every way there is: `b0g13a`,
    `b0g13a@sip.linphone.org`, `sip:b0g13a@sip.linphone.org`. The bare-username
    case is the interesting one -- it is what the Linphone UI shows them, and
    it is useless as a URI, so a default domain is filled in rather than
    rejecting the entry and losing the consent that came with it.
    """
    raw = raw.strip()
    if not raw:
        return ""
    raw = raw.removeprefix("sip:")
    if "@" not in raw:
        raw = f"{raw}@{os.environ.get('SIP_DOMAIN', 'sip.linphone.org')}"
    return f"sip:{raw}"


def normalise_whatsapp(raw: str) -> str:
    """E.164 (`+381600000000`) out of what someone typed, or "" if it is not a number.

    Blank means "not on WhatsApp", the same complete answer a blank SIP field
    is. Spaces, dashes, brackets and a leading 00 are what people type; a bare
    national number with no country code cannot be told apart from a foreign
    one, so it is not guessed at.
    """
    digits = re.sub(r"[\s().\-]", "", (raw or "").strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    return digits if re.fullmatch(r"\+[1-9]\d{6,14}", digits) else ""


MESSAGE_CHANNELS = ("discord", "whatsapp")
CALL_CHANNELS = ("sip", "whatsapp")
_ALIASES = {"linphone": "sip", "phone": "sip", "wa": "whatsapp", "dm": "discord"}


def parse_via(raw: str, allowed: tuple[str, ...]) -> str:
    """An ordered channel preference, as a comma list of known names ("" = default).

    `"WhatsApp, linphone"` -> `"whatsapp"` for messages, `"whatsapp,sip"` for
    calls: each list keeps only the channels that kind of contact can use, so
    one answer in the form fills both.
    """
    seen: list[str] = []
    for word in re.split(r"[\s,;/>]+", (raw or "").lower()):
        word = _ALIASES.get(word, word)
        if word in allowed and word not in seen:
            seen.append(word)
    return ",".join(seen)


SOURCE_FORM = "form"
SOURCE_ADMIN = "admin"
NO_DISCORD_PREFIX = "admin-"
"""Key prefix for a hand-added person with no Discord account. `discord_id` is
the registry's key, and somebody reachable only on Linphone still needs one;
the prefix makes it impossible to mistake for a snowflake and DM it."""


def parse_discord_id(raw: str) -> str:
    """A Discord user id out of an id or a mention (`<@123>`, `<@!123>`).

    Returns "" for blank input and raises on anything else -- a username is not
    accepted because resolving one needs the privileged members intent, and a
    guess at which "milos" was meant is exactly the mistake `resolve` refuses
    to make.
    """
    raw = raw.strip()
    if not raw:
        return ""
    match = re.fullmatch(r"<@!?(\d{15,22})>|(\d{15,22})", raw)
    if not match:
        raise RegistryError(
            f"{raw!r} is not a Discord user id or mention. In Discord: right-click "
            "the person, Copy User ID (needs Developer Mode)."
        )
    return match.group(1) or match.group(2)


def _routes(preferred: str, default: tuple[str, ...], have: dict[str, bool]) -> list[str]:
    order = [v for v in preferred.split(",") if v in default]
    order += [v for v in default if v not in order]
    return [v for v in order if have.get(v)]


@dataclass
class Person:
    """One consenting person. `discord_id` is the identity (or an `admin-...`
    key for a hand-added person with no Discord); for a form row everything
    else is what they said about themselves.

    Every field after `discord_name` has a default, and `from_dict` ignores
    keys it does not know, so a new optional field is one line here and no
    migration: old rows simply read back with the default.
    """

    discord_id: str
    discord_name: str
    name: str
    sip: str = ""
    capabilities: str = ""
    guild_id: str = ""
    registered_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    revoked: bool = False
    """Set when they leave the server or ask to be removed. The row is kept
    rather than deleted so that `hotline-registry show` can still explain why an
    agent may not contact somebody it contacted last week -- a missing row and a
    withdrawn consent look identical otherwise."""
    revoked_reason: str = ""
    source: str = SOURCE_FORM
    added_by: str = ""
    """Discord id of whoever added an `admin` row (the CLI writes `cli:<user>`)."""
    added_via: str = ""
    """Where it was added: a Discord channel link and interaction id, or `cli`."""
    consent_note: str = ""
    """In the adder's words: how this person agreed to be contacted."""
    edited_by: str = ""
    """Set when somebody other than the person changed the row, so a form row
    whose answers were corrected by hand no longer claims to be in their words."""
    introduced_at: str = ""
    """When an agent first reached an `admin` row and said how they got here.
    Empty means the next contact must carry that line."""
    whatsapp: str = ""
    """E.164, or "" for "not on WhatsApp" -- a complete answer, like a blank SIP."""
    message_via: str = ""
    """Ordered message channels ("whatsapp,discord"); "" is the default, Discord first."""
    call_via: str = ""
    """Ordered call channels ("whatsapp,sip"); "" is the default, SIP first."""

    @property
    def admin_added(self) -> bool:
        return self.source == SOURCE_ADMIN

    @property
    def has_discord(self) -> bool:
        return bool(self.discord_id) and not self.discord_id.startswith(NO_DISCORD_PREFIX)

    @property
    def messageable(self) -> bool:
        return bool(self.message_routes()) and not self.revoked

    @property
    def callable_(self) -> bool:
        return bool(self.call_routes()) and not self.revoked

    def message_routes(self) -> list[str]:
        """Message channels they can be reached on, in their order of preference."""
        have = {"discord": self.has_discord, "whatsapp": bool(self.whatsapp)}
        return _routes(self.message_via, MESSAGE_CHANNELS, have)

    def call_routes(self) -> list[str]:
        """Call channels they can be rung on, in their order of preference."""
        have = {"sip": bool(self.sip), "whatsapp": bool(self.whatsapp)}
        return _routes(self.call_via, CALL_CHANNELS, have)

    def channels(self) -> str:
        if self.revoked:
            return "none (revoked)"
        if not self.whatsapp:
            ways = [w for w, ok in (("message", self.has_discord), ("call", bool(self.sip))) if ok]
            return ", ".join(ways) or "none (no Discord, no SIP)"
        out = [f"{kind} ({' > '.join(routes)})" for kind, routes in
               (("message", self.message_routes()), ("call", self.call_routes())) if routes]
        return ", ".join(out)

    @classmethod
    def from_dict(cls, raw: dict) -> Person:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


class Registry:
    """The JSON file, read and written whole.

    Whole-file rather than a database because the working set is the number of
    people who have joined one Discord server -- tens, not thousands -- and a
    file a human can read and a human can delete a line from is worth more here
    than indexed access to data that fits in a screenful.
    """

    def __init__(self, path: pathlib.Path | str | None = None) -> None:
        self.path = pathlib.Path(path) if path else DEFAULT_STORE
        self.people: dict[str, Person] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            raise RegistryError(f"cannot read the registry at {self.path}: {exc}") from exc
        self.people = {
            str(k): Person.from_dict(v) for k, v in dict(raw.get("people", {})).items()
        }

    # Every mutator reloads first. `hotlined` holds one Registry for its whole
    # uptime while the CLI writes the same file, so mutating the in-memory copy
    # would write back a stale roster -- un-revoking somebody the CLI revoked
    # an hour ago, or dropping a person it added. Reloading shrinks that to the
    # milliseconds between read and rename; there is no lock, see Limitations.

    def save(self) -> None:
        """Write atomically, and never group- or world-readable.

        The mode is set on the temporary file BEFORE the rename, not on the
        final path afterwards: a chmod after the fact leaves a window in which
        other people's phone numbers are world-readable, and the window is
        exactly as long as the scheduler makes it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(".tmp")
        payload = {"schema": 1, "people": {k: asdict(p) for k, p in self.people.items()}}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    def upsert(self, person: Person) -> Person:
        """Record a form submission. Re-submitting updates in place.

        `registered_at` is preserved across an update because it is when they
        consented, and a field that silently moves forward every time somebody
        fixes a typo cannot answer "since when".
        """
        self.load()
        existing = self.people.get(person.discord_id)
        if existing is not None:
            person.registered_at = existing.registered_at
        person.updated_at = _now()
        person.revoked = False
        person.revoked_reason = ""
        self.people[person.discord_id] = person
        self.save()
        return person

    def add_admin(
        self,
        name: str,
        *,
        consent_note: str,
        added_by: str,
        added_via: str,
        discord_id: str = "",
        discord_name: str = "",
        sip: str = "",
        capabilities: str = "",
        guild_id: str = "",
        whatsapp: str = "",
        message_via: str = "",
        call_via: str = "",
    ) -> Person:
        """Record somebody Bogdan added by hand.

        Refuses to overwrite a live self-registration: that row holds their own
        words and their own consent, and an admin add on top of it would
        replace both with his. `edit` is the way to change one. A revoked row
        may be re-added -- that is the "unadd, then add back" case.
        """
        self.load()
        name = name.strip()
        consent_note = consent_note.strip()
        if not name:
            raise RegistryError("a hand-added person needs a name")
        if not consent_note:
            raise RegistryError(
                "say how they agreed to be contacted (the consent note); "
                "it is the only consent record a hand-added row has"
            )
        discord_id = parse_discord_id(discord_id)
        sip = normalise_sip(sip)
        number = normalise_whatsapp(whatsapp)
        if whatsapp.strip() and not number:
            raise RegistryError(f"{whatsapp!r} is not a WhatsApp number; use +countrycode...")
        if discord_id:
            existing = self.people.get(discord_id)
            if existing is not None and not existing.revoked:
                raise RegistryError(
                    f"{existing.name} ({discord_id}) is already in the registry "
                    f"({existing.source}); edit them instead"
                )
            key = discord_id
        else:
            key = NO_DISCORD_PREFIX + secrets.token_hex(4)
        person = Person(
            discord_id=key,
            discord_name=discord_name.strip(),
            name=name,
            sip=sip,
            capabilities=capabilities.strip(),
            guild_id=guild_id,
            source=SOURCE_ADMIN,
            added_by=added_by,
            added_via=added_via,
            consent_note=consent_note,
            whatsapp=number,
            message_via=parse_via(message_via, MESSAGE_CHANNELS),
            call_via=parse_via(call_via, CALL_CHANNELS),
        )
        self.people[key] = person
        self.save()
        return person

    EDITABLE = ("name", "discord_name", "sip", "capabilities", "consent_note", "guild_id",
                "whatsapp", "message_via", "call_via")

    def edit(self, key: str, *, edited_by: str, discord_id: str | None = None,
             **fields: str | None) -> Person:
        """Change an existing row's fields in place. `None` means "leave it".

        Changing the Discord id re-keys the row (it is the key), which is how a
        Linphone-only hand-added person gets a Discord account attached later.
        `registered_at` and `source` never move: an edit is not a new consent.
        """
        self.load()
        person = self.people.get(str(key))
        if person is None:
            raise RegistryError(f"nobody in the registry has the key {key!r}")
        unknown = set(fields) - set(self.EDITABLE)
        if unknown:
            raise RegistryError(f"cannot edit {', '.join(sorted(unknown))}")
        if discord_id is not None:
            new_id = parse_discord_id(discord_id)
            if not new_id and person.has_discord:
                raise RegistryError(
                    "cannot remove a Discord id: it is the row's identity. "
                    "Revoke the row and add them again instead."
                )
            if new_id and new_id != person.discord_id:
                if new_id in self.people:
                    raise RegistryError(f"{new_id} is already somebody else's row")
                del self.people[person.discord_id]
                person.discord_id = new_id
                self.people[new_id] = person
        for name, value in fields.items():
            if value is None:
                continue
            value = value.strip()
            if name == "sip":
                value = normalise_sip(value)
            if name == "whatsapp":
                number = normalise_whatsapp(value)
                if value and not number:
                    raise RegistryError(f"{value!r} is not a WhatsApp number; use +countrycode...")
                value = number
            if name == "message_via":
                value = parse_via(value, MESSAGE_CHANNELS)
            if name == "call_via":
                value = parse_via(value, CALL_CHANNELS)
            if name == "name" and not value:
                raise RegistryError("the name cannot be blank")
            setattr(person, name, value)
        person.edited_by = edited_by
        person.updated_at = _now()
        self.save()
        return person

    def revoke(self, discord_id: str, why: str = "") -> Person | None:
        self.load()
        person = self.people.get(str(discord_id))
        if person is None:
            return None
        person.revoked = True
        person.revoked_reason = why
        person.updated_at = _now()
        self.save()
        return person

    def mark_introduced(self, discord_id: str) -> None:
        self.load()
        person = self.people.get(str(discord_id))
        if person is not None and not person.introduced_at:
            person.introduced_at = _now()
            self.save()

    def all(self, *, include_revoked: bool = False) -> list[Person]:
        people = sorted(self.people.values(), key=lambda p: p.name.lower())
        return people if include_revoked else [p for p in people if not p.revoked]

    def resolve(self, query: str) -> Person:
        """Find one person by anything a human or an agent would type.

        Ambiguity is an error rather than a best guess. The operations behind
        this resolve to ringing a real phone, and picking the likelier of two
        Milos's is not a mistake anyone gets to take back.
        """
        query = query.strip()
        if not query:
            raise RegistryError("say who: a name, a Discord id, or a Discord username")
        pool = self.all(include_revoked=True)
        exact = [p for p in pool
                 if query == p.discord_id
                 or query.lower() in (p.name.lower(), p.discord_name.lower())]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise RegistryError(
                f"{query!r} matches {len(exact)} people exactly "
                f"({', '.join(p.discord_id for p in exact)}); use the Discord id"
            )
        partial = [p for p in pool
                   if query.lower() in p.name.lower() or query.lower() in p.discord_name.lower()]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise RegistryError(
                f"nobody in the registry matches {query!r}. "
                "Only people who joined the server and filled the form, or whom Bogdan "
                "added by hand, are in it."
            )
        names = ", ".join(f"{p.name} ({p.discord_name})" for p in partial)
        raise RegistryError(f"{query!r} matches {len(partial)} people: {names}")

    def search(self, query: str) -> list[Person]:
        """People whose self-description mentions `query`. Substring, not fuzzy:
        an agent asking "who can do Rust" wants the people who said Rust."""
        q = query.strip().lower()
        if not q:
            return self.all()
        return [p for p in self.all()
                if q in p.capabilities.lower() or q in p.name.lower()]
