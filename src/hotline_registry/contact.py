"""Reaching a person: a message (Discord DM or WhatsApp), or a call (Linphone or WhatsApp).

Each person's channels are tried in their own order (`Person.message_routes`,
`Person.call_routes`), falling through when one fails. WhatsApp messages go
through `wa send text` (upstream wacli); WhatsApp calls go through
hotline-calld, which drives wa-call.

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

import importlib
import json
import os
import pathlib
import shutil
import subprocess
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
CALL_SRCS = [pathlib.Path(p) for p in (
    os.environ.get("HOTLINE_CALL_SRC", ""),
    str(pathlib.Path.home() / ".local/share/ownbox/tools/hotline-call/src"),
    str(pathlib.Path.home() / "data/hotline-call/src"),
) if p]
"""Where `hotline_call.client` is looked for. Phone calls moved out of hotline-ios
into hotline-call on 2026-10-31; the ownbox checkout is what runs, the dev
checkout is the fallback."""
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
    if not person.has_discord:
        return None
    # A hand-added row is only tied to the server if the adder saw them in it
    # (`guild_id` recorded). Falling back to the default guild for one would
    # revoke somebody Bogdan added precisely because they are NOT a member.
    guild = person.guild_id or ("" if person.admin_added else GUILD_ID)
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
        if person.admin_added and not person.guild_id:
            continue  # not membership-based consent; nothing to reconcile
        state = still_a_member(person)
        if state is True:
            present.append(person)
        elif state is False:
            registry.revoke(person.discord_id)
            left.append(person)
        else:
            unknown.append(person)
    return present, left, unknown


def introduction(person: Person) -> str:
    """The one line a hand-added person hears the first time an agent reaches them.

    Empty for a form row (they know how they got here: they filled the form in)
    and for an admin row already introduced. "Reply STOP" is a request to a
    human: nothing reads DM replies automatically, so whoever sent the message
    has to act on it with `hotline-registry revoke`.
    """
    if not person.admin_added or person.introduced_at:
        return ""
    ways = []
    if person.guild_id:
        ways.append("leave the Claude contacts Discord server")
    ways.append("reply STOP")
    return (
        "(Bogdan added you to Claude's contact list by hand, which is why an agent "
        f"can reach you. To opt out, {' or '.join(ways)} and you will not be "
        "contacted again.)"
    )


def compose_message(person: Person, text: str) -> str:
    """The DM as it will actually be sent: their text, then the introduction if due."""
    intro = introduction(person)
    if not intro:
        return text[:1900]
    return f"{text[:1900 - len(intro) - 2]}\n\n{intro}"


def call_context(person: Person, context: str) -> str:
    """`context` for the voice on the line, with the introduction as an instruction."""
    intro = introduction(person)
    if not intro:
        return context
    spoken = intro.strip("()").replace("reply STOP", "say so on this call")
    line = (f"Early in the call, tell them this in one sentence: {spoken} "
            "If they opt out, say that you will pass it on and include it in your reply.")
    return f"{line}\n\n{context}" if context else line


def send_message(person: Person, text: str, registry: Any = None, *,
                 via: str | None = None) -> str:
    """Message them on their preferred channel. Returns the message id.

    See `deliver_message`; this is it without saying which channel carried it.
    """
    return deliver_message(person, text, registry, via=via)[1]


def deliver_message(person: Person, text: str, registry: Any = None, *,
                    via: str | None = None) -> tuple[str, str]:
    """Send on the first channel that works, in their order. Returns (channel, id).

    Bogdan, 2026-11-06: "if message backend is discord use discord ... if its
    whatsapp use whatsapp". `person.message_routes()` is their order (Discord
    first unless they said otherwise), restricted to channels they gave; `via`
    forces one. A channel that fails falls through to the next, with one
    exception: having left the server withdraws consent for every channel, so
    the membership check runs once, first, and stops everything.
    """
    if person.revoked:
        raise ContactError(
            f"{person.name} is in the registry but consent is withdrawn; not messaging them"
        )
    routes = person.message_routes()
    if via:
        if via not in routes:
            raise ContactError(f"{person.name} cannot be messaged on {via} "
                               f"(they can be on: {', '.join(routes) or 'nothing'})")
        routes = [via]
    if not routes:
        raise ContactError(
            f"{person.name} was added by hand without a Discord account or a WhatsApp "
            "number; there is nobody to DM. Use `hotline-registry call` if they gave a SIP address."
        )
    if not text.strip():
        raise ContactError("refusing to send an empty message")
    check_membership(person, registry)
    body = compose_message(person, text)
    failures: list[str] = []
    for route in routes:
        try:
            sent = _send_discord(person, body) if route == "discord" else _send_whatsapp(person, body)
        except ContactError as exc:
            failures.append(f"{route}: {exc}")
            continue
        if registry is not None and introduction(person):
            registry.mark_introduced(person.discord_id)
        return route, sent
    raise ContactError(f"could not message {person.name} -- " + "; ".join(failures))


def _send_discord(person: Person, body: str) -> str:
    """Opening the DM channel is idempotent on Discord's side -- `POST
    /users/@me/channels` with a recipient already DM'd returns the existing
    channel -- so there is nothing to cache and nothing to go stale."""
    channel = _discord("POST", "/users/@me/channels", {"recipient_id": person.discord_id})
    channel_id = str(channel.get("id", ""))
    if not channel_id:
        raise ContactError(f"Discord did not open a DM channel with {person.name}")
    sent = _discord("POST", f"/channels/{channel_id}/messages", {"content": body})
    return str(sent.get("id", ""))


def wa_command() -> list[str]:
    """`wa` (ownbox's WhatsApp tool), wherever it is: HOTLINE_REGISTRY_WA, PATH, ~/.local/bin."""
    chosen = os.environ.get("HOTLINE_REGISTRY_WA", "")
    if chosen:
        return [chosen]
    found = shutil.which("wa") or str(pathlib.Path.home() / ".local/bin/wa")
    return [found]


def _send_whatsapp(person: Person, body: str) -> str:
    """`wa send text`, which is upstream wacli on the linked account.

    The recipient is given as a JID, not the bare number: wacli's `--to` also
    matches contact and chat *names*, and a message meant for one person must
    not be resolved by a fuzzy match.
    """
    jid = person.whatsapp.lstrip("+") + "@s.whatsapp.net"
    command = [*wa_command(), "send", "text", "--to", jid, "--message", body, "--json"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except FileNotFoundError as exc:
        raise ContactError("the `wa` command is not installed (ownbox install wa)") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContactError("wa send timed out after 120 s") from exc
    try:
        envelope = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
    except ValueError:
        envelope = {}
    if envelope.get("success") is True:
        data = envelope.get("data") or {}
        return str(data.get("id", ""))
    why = envelope.get("error") or done.stderr.strip()[-300:] or f"exit {done.returncode}"
    raise ContactError(f"WhatsApp refused: {why}")


def whatsapp_status() -> dict[str, Any]:
    """Whether `wa` is installed and linked, for `status`. Never raises."""
    try:
        done = subprocess.run([*wa_command(), "auth", "status", "--json"],
                              capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    try:
        envelope = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (done.stderr or done.stdout).strip()[-200:] or "no answer"}
    data = envelope.get("data") or {}
    authed = bool(data.get("authenticated", data.get("logged_in", False)))
    return {"ok": envelope.get("success") is True and authed,
            "error": "" if authed else (envelope.get("error") or "not linked (`wa link`)")}


def _ios_client() -> Any:
    """The call client: `hotline_call.client`, else the older `hotline_ios.client`.

    A soft dependency rather than a hard one, because this repository is public
    and the call client is a sibling checkout rather than something pip can
    fetch. The module is standard-library-only by its own design, so putting its
    source directory on the path costs nothing and pulls in no wheels.

    hotline-call is tried first: since 2026-10-31 it owns calls and hotline-ios
    no longer has a client. hotline-ios is still tried so a box that has not cut
    over keeps ringing people.
    """
    for module, roots in (("hotline_call", CALL_SRCS), ("hotline_ios", [IOS_SRC])):
        try:
            return importlib.import_module(f"{module}.client")
        except ImportError:
            pass
        for root in roots:
            if root.is_dir() and str(root) not in sys.path:
                sys.path.insert(0, str(root))
                try:
                    return importlib.import_module(f"{module}.client")
                except ImportError:
                    continue
    looked = ", ".join(str(p) for p in [*CALL_SRCS, IOS_SRC])
    raise ContactError(
        f"cannot import hotline_call.client or hotline_ios.client (looked in {looked}). "
        "Calling needs hotline-call; messaging does not."
    )


@dataclass
class CallResult:
    state: str
    reply: str = ""
    transcript: list[dict[str, str]] | None = None
    transport: str = ""
    fake: bool = False
    detail: str = ""
    waited_seconds: float = 0.0
    via: str = ""

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
    via: str | None = None,
) -> CallResult:
    """Ring them and, unless `wait` is false, come back with what they said.

    Over their Linphone SIP address or their WhatsApp number, in their order
    (`person.call_routes()`, SIP first unless they said otherwise); `via`
    forces one. Both are free: SIP-to-SIP from our Linphone account, or a
    WhatsApp call from the account wa-call is linked to. hotline-calld does the
    ringing and the falling through -- WhatsApp unlinked or down rings SIP --
    so this sends it both addresses and the order. Ringing an actual phone
    *number* over the phone network would need a paid trunk: his decision, not
    built.
    """
    if not person.callable_:
        if person.revoked:
            raise ContactError(f"{person.name} has withdrawn consent; not calling them")
        raise ContactError(
            f"{person.name} left the SIP and WhatsApp fields blank, which means 'message "
            "me, do not ring me'. Use `hotline-registry message` instead."
        )
    routes = person.call_routes()
    if via:
        if via not in routes:
            raise ContactError(f"{person.name} cannot be called on {via} "
                               f"(they can be on: {', '.join(routes)})")
        routes = [via]
    check_membership(person, registry)
    client = _ios_client()
    addressing: dict[str, Any] = {"to": person.sip if "sip" in routes else ""}
    if "whatsapp" in routes:
        # Only when WhatsApp is in play, so a SIP-only call is the exact request
        # an older hotline-call client accepts.
        addressing.update(whatsapp=person.whatsapp, via="auto" if len(routes) > 1 else routes[0],
                          via_order=routes)
    try:
        outcome = client.place_call(
            reason,
            context=call_context(person, context),
            source=source,
            timeout=timeout,
            ring_timeout=ring_timeout,
            wait=wait,
            transport="sip" if routes == ["sip"] else "auto",
            callee=person.name,
            **addressing,
        )
    except TypeError as exc:
        raise ContactError(
            "this hotline-call client does not accept "
            + ("a WhatsApp number yet; update hotline-call" if "whatsapp" in routes
               else "a `to` address yet -- it predates the registry's SIP generalisation")
            + f" ({exc})"
        ) from exc
    except Exception as exc:  # the client raises DaemonError/CallTimeout
        raise ContactError(f"hotline-calld could not place the call: {exc}") from exc
    # Only a call somebody picked up counts as having told them.
    if registry is not None and outcome.state == "answered" and introduction(person):
        registry.mark_introduced(person.discord_id)
    return CallResult(
        state=outcome.state, reply=outcome.reply, transcript=outcome.transcript,
        transport=outcome.transport, fake=outcome.fake, detail=outcome.detail,
        waited_seconds=outcome.waited_seconds, via=str(getattr(outcome, "via", "") or ""),
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
