# hotline-registry

A list of people who have agreed to be contacted by Claude, and the two ways of
contacting them. People put themselves on it by joining a Discord server and
filling in a form; nothing else puts anybody on it. An agent then runs
`hotline-registry list` to see who is there and what they can do, and
`hotline-registry message` or `hotline-registry call` to reach one of them.

The registry is an **authorization allowlist**, not an address book. Somebody
being in it is the permission — they joined, they filled in the form, and that
act is the consent. Nothing here can add a person by hand, on purpose.

## How it works

```
person joins "Claude contacts"  ->  presses the button in #registry
                                ->  fills in a modal: name, Linphone SIP, what they can do
                                ->  row written to the registry, "registered" role granted
                                ->  an agent may now DM them, and ring them if they gave a SIP
person leaves the server        ->  consent revoked, immediately
```

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
| `hotline_registry.cog` | inside `hotlined` | the `#registry` button, the modal, the join and leave handlers |
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
| `HOTLINE_IOS_SRC` | `~/data/hotline-ios/server/src` | where `hotline_ios.client` is imported from |
| `HOTLINE_REGISTRY_GUILD` | unset | restrict the cog's join/leave handling to one guild |

## Usage

| Command | What it does |
|---|---|
| `list` | everyone contactable: name, Discord, SIP, which channels, what they can do |
| `find QUERY` | people whose self-description mentions QUERY |
| `show WHO` | one person in full, including what may and may not be done to them |
| `message WHO TEXT` | send a Discord DM; prints the message id |
| `call WHO REASON` | ring their Linphone and wait; prints what they said |
| `status` | whether messaging and calling actually work right now |
| `revoke WHO` | mark somebody uncontactable |

| Flag | Meaning |
|---|---|
| `--dry-run` | on `message`, `call` and `revoke`: print exactly what would happen, do nothing |
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
- **One registry, one server.** `guild_id` is recorded but nothing reads it.
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
