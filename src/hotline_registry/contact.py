"""Reaching a person: a Discord DM, or a real phone call to their Linphone.

Neither mechanism is implemented here. Both already exist and both are hard-won:

- **Messaging** is Discord's REST API using *hotline's own bot token*. The whole
  reason the "Claude contacts" server exists is that a bot can only DM someone it
  shares a guild with -- so once they have joined, a DM is two HTTP calls and no
  gateway connection at all. That matters: `hotlined` holds the gateway, and
  nothing here goes anywhere near it.
- **Calling** is `hotline-iosd`, over its local HTTP API. It owns the SIP stack,
  the ASR, the TTS and the call agent; this sends it an address and a reason.
  Re-implementing any of that here would be a second, worse SIP client.

The one thing this module does own is **the consent check**, and it owns it
because it is the last place before the wire. `person.messageable` and
`person.callable_` are asserted here rather than trusted from the caller, so a
bug in the CLI cannot ring somebody who never agreed to be rung.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .registry import Person, RegistryError

DISCORD_API = "https://discord.com/api/v10"
HOTLINE_ENV = pathlib.Path(
    os.environ.get("HOTLINE_ENV", "") or pathlib.Path.home() / "data/hotline/.env"
)
IOS_SRC = pathlib.Path(
    os.environ.get("HOTLINE_IOS_SRC", "") or pathlib.Path.home() / "data/hotline-ios/server/src"
)
GUILD_ID = os.environ.get("HOTLINE_REGISTRY_GUILD", "")


class ContactError(RegistryError):
    """A message or a call could not be delivered."""


def load_env_file(path: pathlib.Path = HOTLINE_ENV) -> dict[str, str]:
    """Parse a KEY=VALUE file. Values are never logged, printed or returned to
    anything but the caller that needs them."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def bot_token() -> str:
    """hotline's bot token, from the environment or from its `.env`.

    Same token, same bot, same identity the person actually joined the server
    with -- a second application would be a second invite and a second thing for
    him to approve.
    """
    token = os.environ.get("HOTLINE_BOT_TOKEN", "") or load_env_file().get("HOTLINE_BOT_TOKEN", "")
    if not token:
        raise ContactError(
            f"no HOTLINE_BOT_TOKEN in the environment or in {HOTLINE_ENV}; "
            "messaging needs hotline's bot token"
        )
    return token


def _discord(method: str, path: str, body: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{DISCORD_API}{path}", data=data, method=method,
        headers={
            "Authorization": f"Bot {bot_token()}",
            "Content-Type": "application/json",
            "User-Agent": "hotline-registry (https://github.com/BogdanStamenovic/hotline-registry, 0.1)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 403:
            raise ContactError(
                f"Discord refused ({exc.code}): {detail}. The usual cause is that "
                "they have left the server or blocked DMs from it."
            ) from exc
        raise ContactError(f"Discord refused ({exc.code}): {detail}") from exc
    except OSError as exc:
        raise ContactError(f"cannot reach Discord: {exc}") from exc


def still_a_member(person: Person) -> bool | None:
    """Is this person still in the server they consented in?

    **This is the consent model's feedback loop, and it exists because the
    obvious mechanism is unavailable.** Leaving the server is meant to withdraw
    consent immediately, which would normally be `on_member_remove` -- but that
    is a privileged Server Members intent, it is switched off for this
    application, and switching it on is a change only Bogdan can make in the
    developer portal. Worse, turning it on wrong takes `hotlined`'s gateway down
    with it.

    So membership is checked where it actually matters: one request, immediately
    before contacting somebody, against the endpoint that still answers without
    the intent. `GET /guilds/{g}/members/{u}` returns 200 or 404 for a bot with
    no privileged intents at all; `GET /guilds/{g}/members` returns 403. Verified
    against Discord on 2026-09-18, not inferred from the documentation.

    Returns None -- not False -- when the answer cannot be got at all (no guild
    recorded, Discord unreachable). None means "unknown", and an unknown must not
    silently become a refusal: a network blip is not a withdrawal of consent.
    """
    guild = person.guild_id or GUILD_ID
    if not guild:
        return None
    try:
        _discord("GET", f"/guilds/{guild}/members/{person.discord_id}")
        return True
    except ContactError as exc:
        if "(404)" in str(exc):
            return False
        return None


def check_membership(person: Person, registry: Any = None) -> None:
    """Refuse to contact somebody who has left, and write that down if asked.

    Called by both `send_message` and `call_person`, so the check cannot be
    skipped by using one rather than the other.
    """
    present = still_a_member(person)
    if present is False:
        if registry is not None:
            registry.revoke(person.discord_id)
        raise ContactError(
            f"{person.name} has left the server, which withdraws consent. "
            "Not contacting them."
        )


def reconcile(registry: Any) -> tuple[list[Person], list[Person], list[Person]]:
    """Sweep the whole registry against the server. Returns (present, left, unknown).

    The sweep is what catches somebody who left while nothing was trying to
    contact them. It is a command rather than a timer because it costs one
    request per person and the per-contact check above already covers the case
    that matters.
    """
    present: list[Person] = []
    left: list[Person] = []
    unknown: list[Person] = []
    for person in registry.all(include_revoked=True):
        if person.revoked:
            continue
        state = still_a_member(person)
        if state is True:
            present.append(person)
        elif state is False:
            registry.revoke(person.discord_id)
            left.append(person)
        else:
            unknown.append(person)
    return present, left, unknown


def send_message(person: Person, text: str, registry: Any = None) -> str:
    """DM them. Returns the message id.

    Opening the DM channel is a separate call that Discord makes idempotent --
    `POST /users/@me/channels` with a recipient already DM'd returns the existing
    channel rather than a second one -- so there is nothing to cache and nothing
    to go stale.
    """
    if not person.messageable:
        raise ContactError(
            f"{person.name} is in the registry but consent is withdrawn; not messaging them"
        )
    if not text.strip():
        raise ContactError("refusing to send an empty message")
    check_membership(person, registry)
    channel = _discord("POST", "/users/@me/channels", {"recipient_id": person.discord_id})
    channel_id = str(channel.get("id", ""))
    if not channel_id:
        raise ContactError(f"Discord did not open a DM channel with {person.name}")
    sent = _discord("POST", f"/channels/{channel_id}/messages", {"content": text[:1900]})
    return str(sent.get("id", ""))


def _ios_client() -> Any:
    """`hotline_ios.client`, imported from wherever hotline-ios actually is.

    A soft dependency rather than a hard one, because this repository is public
    and hotline-ios is a sibling checkout rather than something pip can fetch.
    The module itself is standard-library-only by its own design, so putting its
    source directory on the path costs nothing and pulls in no wheels.
    """
    try:
        from hotline_ios import client  # type: ignore[import-not-found,unused-ignore]

        return client
    except ImportError:
        pass
    if IOS_SRC.is_dir() and str(IOS_SRC) not in sys.path:
        sys.path.insert(0, str(IOS_SRC))
    try:
        from hotline_ios import client  # type: ignore[import-not-found,unused-ignore]

        return client
    except ImportError as exc:
        raise ContactError(
            f"cannot import hotline_ios.client (looked in {IOS_SRC}). "
            "Calling needs hotline-ios; messaging does not."
        ) from exc


@dataclass
class CallResult:
    state: str
    reply: str = ""
    transcript: list[dict[str, str]] | None = None
    transport: str = ""
    fake: bool = False
    detail: str = ""
    waited_seconds: float = 0.0

    @property
    def rang(self) -> bool:
        return self.state in ("answered", "unanswered", "declined", "ringing")


def call_person(
    person: Person,
    reason: str,
    *,
    context: str = "",
    source: str = "an agent",
    timeout: float = 600.0,
    ring_timeout: float = 45.0,
    wait: bool = True,
    registry: Any = None,
) -> CallResult:
    """Ring their Linphone and, unless `wait` is false, come back with what they said.

    SIP-to-SIP only, and therefore free: `hotline-iosd` registers one Linphone
    account and INVITEs another. Ringing an actual phone *number* would need a
    paid PSTN trunk, which is money and therefore his decision, not this tool's.
    """
    if not person.callable_:
        if person.revoked:
            raise ContactError(f"{person.name} has withdrawn consent; not calling them")
        raise ContactError(
            f"{person.name} left the SIP field blank, which means 'message me, do not "
            "ring me'. Use `hotline-registry message` instead."
        )
    check_membership(person, registry)
    client = _ios_client()
    try:
        outcome = client.place_call(
            reason,
            context=context,
            source=source,
            timeout=timeout,
            ring_timeout=ring_timeout,
            wait=wait,
            transport="sip",
            to=person.sip,
            callee=person.name,
        )
    except TypeError as exc:
        raise ContactError(
            "this hotline-ios does not accept a `to` address yet -- it predates the "
            f"registry's SIP generalisation ({exc})"
        ) from exc
    except Exception as exc:  # the client raises DaemonError/CallTimeout
        raise ContactError(f"hotline-iosd could not place the call: {exc}") from exc
    return CallResult(
        state=outcome.state, reply=outcome.reply, transcript=outcome.transcript,
        transport=outcome.transport, fake=outcome.fake, detail=outcome.detail,
        waited_seconds=outcome.waited_seconds,
    )


def call_health() -> dict[str, Any]:
    """What `hotline-iosd` says about itself, or why it cannot be asked.

    `fake` is the field that matters and the reason this is exposed as its own
    command: a loopback doorbell accepts a call, reports success, and rings
    nothing at all.
    """
    try:
        client = _ios_client()
        return dict(client.status())
    except Exception as exc:  # noqa: BLE001 -- reported, not raised: status must not fail
        return {"ok": False, "error": str(exc)}
