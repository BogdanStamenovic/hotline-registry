"""Mirror the registry into a private Discord channel Bogdan can read.

He asked for "an admin channel where i can see the full registry of all the
people who registered". The obvious implementation is to post the roster once
and be done -- and that is the failure this project keeps paying for. A snapshot
is a status field: it describes the registry at the moment it was written and
then quietly stops being true the first time somebody signs up. So the channel
is *republished* rather than posted: every write to the registry rewrites it.

Rewriting wholesale, rather than editing one tracked message, is deliberate. An
edit-in-place scheme has to remember which message it owns, and that memory is
one more thing that can drift out of step with the registry it claims to
describe. Deleting the bot's own messages and posting the current truth cannot
drift -- and in an admin channel that only he reads, the notification when
somebody registers is a feature rather than noise.

The channel holds other people's SIP addresses and Discord ids, which is why it
is created with @everyone denied VIEW_CHANNEL and an explicit allow for him
alone. Nothing here re-checks that; if the channel is ever recreated by hand,
the overwrites have to be set again.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from .contact import load_env_file
from .registry import Person, Registry

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
# Discord's hard cap is 2000; leave room for the chunk to end on a line break.
LIMIT = 1900

CHANNEL_ENV = "HOTLINE_REGISTRY_ADMIN_CHANNEL"
TOKEN_ENV = "HOTLINE_BOT_TOKEN"


def _request(method: str, path: str, token: str, body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/BogdanStamenovic/hotline-registry, 1.0)",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def render(people: list[Person]) -> list[str]:
    """The roster as Discord messages, split on person boundaries.

    Capabilities are reproduced in full and not truncated: the point of the
    channel is that he can read what somebody said they can do without going to
    a terminal, and a summary he cannot trust is worth less than no summary.
    """
    live = [p for p in people if not p.revoked]
    gone = [p for p in people if p.revoked]

    header = f"**Contact registry — {len(live)} registered**"
    if gone:
        header += f" ({len(gone)} revoked)"
    header += "\nRepublished whenever somebody registers, so this is current."

    blocks = [header]
    for person in sorted(live, key=lambda p: p.name.lower()):
        reach = person.channels() or "nothing — no Discord DM and no SIP"
        blocks.append(
            f"\n**{person.name}** — {person.discord_name}\n"
            f"· reachable by: {reach}\n"
            f"· SIP: {person.sip or '(none given — do not call)'}\n"
            f"· registered {person.registered_at[:10]}"
            + (f", updated {person.updated_at[:10]}" if person.updated_at[:10] != person.registered_at[:10] else "")
            + f"\n· can do: {person.capabilities or '(said nothing)'}"
        )
    for person in sorted(gone, key=lambda p: p.name.lower()):
        blocks.append(f"\n~~**{person.name}** — {person.discord_name}~~ · revoked, do not contact")

    chunks: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) + 1 > LIMIT:
            chunks.append(current)
            current = ""
        current = f"{current}\n{block}" if current else block
        while len(current) > LIMIT:  # one person with a very long answer
            # rfind returns -1 when there is no line break to split on, and
            # `-1 or LIMIT` is -1 -- which silently trimmed one character instead
            # of splitting and left the chunk over the limit. Discord would have
            # rejected it at the worst moment: when somebody had just registered.
            cut = current.rfind("\n", 0, LIMIT)
            if cut <= 0:
                cut = LIMIT
            chunks.append(current[:cut])
            current = current[cut:].lstrip("\n")
    if current.strip():
        chunks.append(current)
    return chunks


def publish(registry: Registry, channel_id: str = "", token: str = "") -> int:
    """Replace the admin channel's contents with the registry as it stands now.

    Returns the number of messages posted. Raises nothing on a missing channel or
    token -- a registry write must not fail because the mirror could not be
    updated, since the registry is the record and the channel is only a view of
    it. Every such case is logged loudly instead.
    """
    # The environment first, then hotline's .env -- the same order and the same
    # loader contact.py already uses to find the bot token, rather than a second
    # mechanism that could disagree with it. The CLI runs outside the daemon and
    # has no environment of its own, so without the file it would find nothing.
    env_file = load_env_file()
    channel_id = channel_id or os.environ.get(CHANNEL_ENV, "") or env_file.get(CHANNEL_ENV, "")
    token = token or os.environ.get(TOKEN_ENV, "") or env_file.get(TOKEN_ENV, "")
    if not channel_id or not token:
        log.warning(
            "not mirroring the registry: %s is unset",
            CHANNEL_ENV if not channel_id else TOKEN_ENV,
        )
        return 0

    try:
        existing = _request("GET", f"/channels/{channel_id}/messages?limit=100", token)
        me = _request("GET", "/users/@me", token)
        my_id = me["id"] if isinstance(me, dict) else None
        for message in existing if isinstance(existing, list) else []:
            if message.get("author", {}).get("id") == my_id:
                _request("DELETE", f"/channels/{channel_id}/messages/{message['id']}", token)

        posted = 0
        for chunk in render(registry.all(include_revoked=True)):
            _request("POST", f"/channels/{channel_id}/messages", token, {"content": chunk})
            posted += 1
        return posted
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, KeyError, TypeError) as exc:
        log.warning("could not mirror the registry to channel %s: %s", channel_id, exc)
        return 0
