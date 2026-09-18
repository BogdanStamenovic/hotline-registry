"""The registry itself: who consented to be contacted, and on which channels.

**The store is not a contact list, it is a consent record.** Someone is in here
because they joined the "Claude contacts" server and filled in the form, and
that act -- theirs, not ours -- is the standing permission for an agent to reach
them. Nothing else creates an entry. There is deliberately no `add` command, and
`upsert` takes a Discord id it can only have got from an interaction the person
performed, because a registry an agent can write into by hand is an address book
with a consent-shaped label on it.

Two channels, two separate permissions, because they are not the same promise:

| Channel | Granted by | Withdrawn by |
|---|---|---|
| Discord DM | joining the server and submitting the form | leaving the server, or `revoked` |
| Phone (SIP) | typing a Linphone address into the form | clearing that field, or leaving |

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


@dataclass
class Person:
    """One consenting person. `discord_id` is the identity; everything else is
    what they said about themselves."""

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

    @property
    def messageable(self) -> bool:
        return not self.revoked

    @property
    def callable_(self) -> bool:
        return bool(self.sip) and not self.revoked

    def channels(self) -> str:
        if self.revoked:
            return "none (revoked)"
        return "message, call" if self.sip else "message"

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
        existing = self.people.get(person.discord_id)
        if existing is not None:
            person.registered_at = existing.registered_at
        person.updated_at = _now()
        person.revoked = False
        self.people[person.discord_id] = person
        self.save()
        return person

    def revoke(self, discord_id: str, why: str = "") -> Person | None:
        person = self.people.get(str(discord_id))
        if person is None:
            return None
        person.revoked = True
        person.updated_at = _now()
        self.save()
        return person

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
                "Only people who joined the server and filled the form are in it."
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
