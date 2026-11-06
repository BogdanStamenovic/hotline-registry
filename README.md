# hotline-registry

A list of people who have agreed to be contacted by Claude, and the two ways of
contacting them. People get on it in one of two ways: they join a Discord server
and fill in a form, or Bogdan adds them by hand. An agent then runs
`hotline-registry list` to see who is there and what they can do, and
`hotline-registry message` or `hotline-registry call` to reach one of them.

The registry is an **authorization allowlist**, not an address book. Somebody
being in it is the permission, and every row says whose word that permission
rests on:

| `source` | How the row got there | Whose consent |
|---|---|---|
| `form` | they joined the server and submitted the form | theirs, in the act of filling it in |
| `admin` | Bogdan added them (`/registry add` in the admin channel, or the CLI) | his claim that they agreed, written down in `consent_note` |

Rows from before `source` existed have no such key and read as `form`, which is
what they all were. A hand-added row also records who added it and where
(`added_by`, `added_via`), and the first agent to reach that person tells them
how they got on the list and how to get off it (see below).

## How it works

```
person joins "Claude contacts"  ->  presses the button in #registry
                                ->  fills in a modal: name, Linphone SIP, what they can do
                                ->  row written to the registry, "registered" role granted
                                ->  an agent may now DM them, and ring them if they gave a SIP
person leaves the server        ->  consent revoked, immediately

Bogdan, in #registry-admin      ->  /registry add | edit | remove
                                ->  add/edit: a modal (name, Discord id, SIP, can do, consent note)
                                ->  remove: soft revoke, reason "removed by Bogdan"
                                ->  the admin channel is republished
```

### Adding and removing people by hand

Bogdan asked for this on 2026-11-06: *"the ability for me to edit the admin
registry so i can add or unadd people"*.

- **`/registry add`** opens a modal: name, Discord user id or mention
  (optional), Linphone SIP (optional), what they can help with, and how they
  agreed to be contacted (required: it is the only consent record the row has).
  If the Discord id is a member of the server, that is recorded, and leaving
  the server then revokes them like anybody else.
- **`/registry edit PERSON`** opens the same modal prefilled. Attaching a
  Discord id to a Linphone-only person re-keys the row; removing one is refused.
- **`/registry remove PERSON [reason]`** is a soft revoke, the same as
  `hotline-registry revoke`. The row stays so that `show` can explain why an
  agent may no longer contact somebody. There is no hard delete; see
  Limitations.

**Only his Discord account can use them.** The commands are hidden from
non-administrators, but that is a server setting anyone with the right role can
change. The real check is his user id (`HOTLINE_REGISTRY_ADMIN_USER`, else
hotline's `DISCORD_USER_ID`), compared on every command, every autocomplete and
every modal submit. Anyone else gets an ephemeral refusal. Unset fails closed.
The commands also refuse outside the admin channel, and every reply is
ephemeral.

**`hotline-registry add` and `edit` do the same from a terminal, gated on
nothing.** An agent's shell is the same uid as his, so the code cannot tell
them apart. The Discord path is the one with a real identity behind it. An
agent using the CLI to add somebody is subject to the outward-action rules in
Bogdan's CLAUDE.md: putting a person on this list authorizes every agent to
contact them, so it needs his yes.

**First contact.** The first DM to a hand-added person ends with one line:
Bogdan added them to Claude's contact list by hand, and to opt out they can
reply STOP (and leave the server, if they are in it). The first answered call
gets the same, spoken. It is sent once, and `introduced_at` records when.
Nothing reads DM replies automatically, so `message` reminds the sending agent
to check the DM and run `revoke --reason 'replied STOP'` itself.

Two channels, two separate permissions, because they are not the same promise:

| Channel | How it works | Granted by | Withdrawn by |
|---|---|---|---|
| Discord DM | hotline's bot shares a guild with them, so it can DM them — REST only, no gateway | submitting the form | leaving the server, or `revoke` |
| Phone call | `hotline-iosd` registers one Linphone account and INVITEs theirs — SIP to SIP, free | typing a Linphone address into the form | clearing that field, or leaving |

**Leaving the SIP field blank is a complete answer.** It means "message me, do
not ring me", and `call` refuses with that reason rather than treating it as a
missing value.

Nothing in this repository implements Discord, SIP, speech recognition or
text-to-speech. Messaging is two REST calls with
[hotline](https://github.com/BogdanStamenovic/hotline)'s bot token; calling is
one HTTP call to `hotline-iosd`, which owns the SIP stack and the voice agent.
The parts this repository does own are the consent record and the checks in
front of the wire.

### The three pieces

| Piece | Where it runs | What it does |
|---|---|---|
| `hotline-registry` | a terminal, or an agent's shell | the CLI below |
| `hotline_registry.cog` | inside `hotlined` | the `#registry` button, the modal, the join and leave handlers, `/registry add\|edit\|remove` |
| `registry.json` | `~/.local/share/hotline-registry/` | the consent record, mode 600, **not** in this repo |

The Discord half is a **cog loaded by `hotlined`** rather than a program of its
own. A bot has one gateway identity and `hotlined` already holds it — it is what
carries every agent's contact with Bogdan — so a second client on the same token
would at best duplicate every event and at worst disturb the one connection
nobody can afford to lose.

## Install

```
ownbox install hotline-registry
```

Manual:

```
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Then, in `hotlined`, load the cog (`hotline` does this already if
`hotline_registry` is importable from its environment).

### Configuration

| Variable | Default | What it is |
|---|---|---|
| `HOTLINE_REGISTRY_STORE` | `~/.local/share/hotline-registry/registry.json` | the consent record |
| `HOTLINE_ENV` | `~/data/hotline/.env` | where the bot token is read from |
| `HOTLINE_CALL_SRC` | ownbox's hotline-call checkout, then `~/data/hotline-call/src` | where `hotline_call.client` is imported from |
| `HOTLINE_IOS_SRC` | `~/data/hotline-ios/server/src` | the pre-split fallback, `hotline_ios.client` |
| `HOTLINE_REGISTRY_GUILD` | unset | restrict the cog's join/leave handling to one guild |
| `HOTLINE_REGISTRY_ADMIN_CHANNEL` | read from `HOTLINE_ENV` | the private admin channel: the roster mirror, and where `/registry` works |
| `HOTLINE_REGISTRY_ADMIN_USER` | hotline's `DISCORD_USER_ID` | the only Discord user who may use `/registry` |

## Usage

| Command | What it does |
|---|---|
| `list` | everyone contactable: name, Discord, SIP, which channels, source, what they can do |
| `find QUERY` | people whose self-description mentions QUERY |
| `show WHO` | one person in full: what may be done to them, where the row came from, the consent note |
| `message WHO TEXT` | send a Discord DM; prints the message id |
| `call WHO REASON` | ring their Linphone and wait; prints what they said |
| `status` | whether messaging and calling actually work right now |
| `revoke WHO [--reason R]` | mark somebody uncontactable; the reason is kept on the row |
| `add NAME --consent TEXT [--discord ID] [--sip S] [--can-do T] [--guild ID]` | add somebody by hand (`source: admin`); prints the row's key |
| `edit WHO [--name] [--discord] [--sip] [--can-do] [--consent] [--guild]` | change only the fields given |
| `publish` | rewrite the admin channel from the registry as it stands |

| Flag | Meaning |
|---|---|
| `--dry-run` | on `message`, `call`, `revoke`, `add` and `edit`: print exactly what would happen, do nothing |
| `--no-publish` | on `revoke`, `add` and `edit`: do not refresh the admin channel afterwards |
| `--json` | on `list`, `find` and `show`: machine-readable, for an agent |
| `--all` | on `list`: include people who have withdrawn consent |
| `--callable` | on `list`: only people who gave a SIP address |
| `--context` | on `call`: everything the voice on the line should know — it knows nothing else |
| `--no-wait` | on `call`: ring and exit rather than waiting for an answer |

`WHO` accepts a name, a Discord username, or a Discord id. Ambiguity is an error
rather than a best guess, because the operation behind it rings a real phone.

Only the answer goes to stdout — a roster, a message id, what the person said —
so `$(hotline-registry ...)` gets that and nothing else. Progress and errors go
to stderr.

`add`, `edit` and `revoke` refresh the admin channel only when they wrote the
live store. With `--store` pointing at a scratch copy they say so and skip it.
Republishing a scratch copy would post test people over the real roster in his
private channel, which the test suite did once (2026-09-20).

Exit codes: `0` success, `1` the operation failed, `2` usage error. `call` adds
`3` it rang and nobody answered, and `4` they declined — the same contract
`hotline-call` uses, because "nobody answered" and "the daemon is dead" call for
completely different behaviour from whatever is driving.

### Examples

```
hotline-registry list
hotline-registry find rust
hotline-registry message Milos "the PR is green, whenever you have a minute"
hotline-registry call Milos --dry-run "can you review the deploy change today?"
answer=$(hotline-registry call Milos --context "$(cat brief.md)" "can you review it today?")
hotline-registry status
```

## Device map

The second half of the registry: which machine handles which part of the
system, and whether that is still true. Separate from the people registry in
everything but the command name and the admin channel (spec and reasoning:
[docs/DEVICES.md](docs/DEVICES.md)).

Each device has **duties**. Each duty carries a **probe** (a systemd unit is
active, a shell command exits 0, or this device has the newest copy of a file),
and `check` runs the probes on the live machines over ssh:

| Result | Meaning |
|---|---|
| ✅ verified | the probe ran and the claim holds |
| ❌ contradicted | the probe ran and the claim is false |
| ❔ unknown | the device could not be asked (asleep, offline): says nothing either way |
| ➖ unprobed | no probe defined, e.g. the phone |

| Command | What it does |
|---|---|
| `devices list` / `show DEVICE` | the map, with the last result of every duty |
| `devices where QUERY` | which device handles QUERY (`where wake`, `where keyring`) |
| `devices check [DEVICE…]` | run the probes now; exit 1 if anything is contradicted |
| `devices discover DEVICE` | running units on a device that no duty claims |
| `devices add-device NAME --ssh HOST --kind K --power P` | add or update a device |
| `devices claim DEVICE SLUG TITLE --unit U \| --command C \| --freshest PATH --among A,B` | add or update a duty |
| `devices drop ID` / `drop-device NAME` | remove one |

`--json` on every read, `--dry-run` on every write. The store is
`~/.local/share/hotline-registry/devices.json` (mode 600, not in this repo,
which is public). A daily wake task (`device-map-daily`, 10:00) runs
`scripts/device-check.sh`, which re-checks everything, republishes the admin
channel when anything changed, and tells hotline-80 about contradictions.

```
hotline-registry devices where keyring
hotline-registry devices check pigion
hotline-registry devices claim pigion wake-server "wake server" --unit wake-server.service \
    --if-down "nothing wakes on schedule"
```

## Limitations

**What works today**

- Messaging anybody in the registry, as long as hotline's bot still shares the
  server with them.
- Ringing anybody who gave a Linphone SIP address, with a two-way spoken
  conversation handled by `hotline-iosd`.
- Bogdan adding, editing and removing people from the admin channel, and
  anyone with a shell doing the same from the CLI.
- `--dry-run` on everything outward.

**What does not exist yet**

- **Calling a real phone number.** SIP-to-SIP is free; reaching the PSTN needs a
  paid trunk. That is money, so it is Bogdan's decision and is not built.
- **Gating the server is done with a role, not enforced by the bot.** An
  unregistered member sees only `#registry`, because `@everyone` is denied View
  Channel everywhere else and the `registered` role restores it. If Discord's
  permission model is changed by hand, the gate changes with it.
- **`on_member_join` needs the Server Members intent.** Without it the welcome
  DM never fires. Nothing else depends on it: the message in `#registry`
  explains itself, and registration works whether or not the DM arrived.
- **Consent is withdrawn on leaving, and that event needs the same intent.** If
  the intent is off, somebody who left stays in the registry until `revoke` is
  run by hand. `status` does not currently check this.
- **No rate limiting of its own.** It relies on Discord's, and a 429 surfaces as
  a failed command rather than being retried.
- **One registry, one server.** `guild_id` is what the membership check asks
  about; a hand-added row has one only if the person was a member when added.
- **Nothing acts on a STOP reply.** hotlined ignores DMs from anyone but
  Bogdan, and no DM-reply path exists to hook into. A hand-added person who
  replies STOP stays contactable until whoever reads the DM runs `revoke`.
  `message` says so on stderr every time it sends a first-contact line.
- **No hard delete.** Removing someone is a soft revoke. A row can only be
  deleted by editing `registry.json` by hand (it is plain JSON on purpose).
- **No lock on the store.** Every write re-reads the file first, so the cog and
  the CLI no longer overwrite each other with stale copies, but two writes in
  the same few milliseconds can still lose one.
- **The CLI's `add` and `edit` cannot verify who is typing.** See above: the
  rule for agents is in CLAUDE.md, not in code.
- **A Discord username is not accepted** in the add/edit form or `--discord`,
  only an id or a mention. Resolving a name needs the privileged members intent,
  and guessing which "milos" was meant is not a mistake worth making.
- **The `/registry` commands are global** (guild-only context, admin-only
  default permission) rather than registered to one guild, so they also appear
  in other servers the bot is in, where they refuse.
- **The device map's probes read state, they do not prove a service works.** An
  active unit can be broken; health checks are a different job.
- **Device probes need key-based ssh from the machine running `check`**
  (archserver). The phone and the Windows boxes can only carry unprobed duties,
  and if archserver is off nobody checks: the admin channel keeps the last
  result, with its date.
- **`freshest` compares mtimes**, so a file touched without being changed reads
  as newer.

**Things worth knowing before trusting a call**

- A `hotline-iosd` running a loopback doorbell accepts a call, reports success
  and rings nothing. `call` treats that as a failure (exit 1) rather than a
  success, and `status` prints it, but an older daemon may not report `fake` at
  all.
- The voice that talks to a registry person is a separate Sonnet session with
  **no file tools pointed anywhere useful** — its working directory is an empty
  one, and its instructions say it can look nothing up. It knows what `--context`
  told it. That is deliberate: it is talking to somebody who is not Bogdan.
