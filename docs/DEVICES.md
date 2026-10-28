# Device map — spec

The device map records which machine handles which part of Bogdan's system, and
checks each of those claims against the live machine. It answers "where does X
run", "what breaks if this box is off", and "which device has the up-to-date
copy of Y".

Asked for 2026-10-28: *"Detailing what parts of the system a certain device
handles. For example on archserver hotline cvoiced is running as well as track
tasks. On pigion wake. On arch laptop is a more up to date keyring."*

## Why it is in hotline-registry, and why it is separate inside it

hotline-registry is the place an agent looks up *where to go* for something:
today "who knows about X", now also "which machine does X". Same users (agents),
same CLI, same admin-channel mirror.

It shares nothing else. The people registry is a **consent record**: someone is
in it because they put themselves there, and nothing can add a person by hand.
Devices consent to nothing; an agent or Bogdan adds them. So the device map is
its own module (`hotline_registry.devices`), its own store (`devices.json`), and
its own command group (`hotline-registry devices …`). Nothing in the consent code
learns about devices.

## The model

**Device** — a machine.

| Field | Example | Notes |
|---|---|---|
| `name` | `pigion` | its tailscale name; the key |
| `kind` | `server`, `desktop`, `laptop`, `pi`, `phone`, `vps` | free text, for reading |
| `ssh` | `pigion` | how probes reach it; empty means "this machine" or "not reachable by ssh" |
| `local` | `true` on archserver | probes run in-process instead of over ssh |
| `power` | `always-on`, `wakes-on-schedule`, `when-he-uses-it` | so "unreachable" can be read correctly |
| `wol_mac` | `a8:a1:59:…` | optional |
| `notes` | `Pi Zero 2 W, 237 MiB free` | |

**Duty** — one thing a device is responsible for. A claim, with the probe that
tests it.

| Field | Example |
|---|---|
| `id` | `pigion/wake-server` (device/slug, unique) |
| `title` | `wake: the scheduler server every box syncs with` |
| `kind` | `service`, `schedule`, `data`, `capability`, `endpoint` |
| `probe` | see below; may be absent |
| `if_down` | `no box wakes on schedule; tasks queue until it is back` |
| `last` | result of the last check: state, when, detail |

### Probes

| `type` | Holds when | Example |
|---|---|---|
| `unit` | the systemd unit is active (`scope: user` or `system`) | `{"type":"unit","unit":"cvoiced.service","scope":"user"}` |
| `command` | a shell command exits 0 on the device | track tasks: `sqlite3 ~/.local/state/wake/wake.db "select 1 from tasks where status='pending' and owner='archserver' limit 1" \| grep -q 1` |
| `freshest` | `path` has the newest mtime on this device among `among` | keyring: `{"type":"freshest","path":"~/.local/share/keyrings/login.keyring","among":["arch","archserver"]}` |

A duty with no probe is allowed (a phone app, a physical thing) and is reported
as `unprobed`, never as verified.

### Check results

| State | Meaning |
|---|---|
| `verified` | the probe ran and the claim holds |
| `contradicted` | the probe ran and the claim is false: unit inactive, command failed, another device has a newer copy |
| `unknown` | the probe could not run: device unreachable, ssh refused, timeout. **Not** the same as contradicted; a laptop that is asleep says nothing about its duties |
| `unprobed` | no probe defined |

`freshest` is `unknown`, not verified, if *any* device in `among` could not be
reached, because the newest copy may be on the one nobody could ask.

## Commands

All under `hotline-registry devices`. `--json` on every read; `--dry-run` on every write.

| Command | What it does |
|---|---|
| `list` | devices and their duties, with the last check result of each |
| `show DEVICE` | one device in full |
| `where QUERY` | which device(s) handle QUERY: matched against duty ids, titles, units and notes |
| `check [DEVICE…]` | run every probe now, store the results, print what changed; exit 1 if anything is contradicted |
| `discover DEVICE` | running units on the device that no duty claims: candidates, never added automatically |
| `add-device NAME --kind --ssh --power …` | add or update a device |
| `claim DEVICE ID TITLE --kind … [--unit U \| --command C \| --freshest PATH --among A,B] [--if-down TEXT]` | add or update a duty |
| `drop ID` | remove a duty (or `drop-device NAME`) |

Exit codes: 0 success, 1 failure or something contradicted (`check`), 2 usage
error — the same contract as the rest of the CLI.

## Storage and mirror

- `~/.local/share/hotline-registry/devices.json`, mode 600, beside
  `registry.json`, **not in the repo** (it holds addresses and MACs, and the repo
  is public). Written atomically with the mode set before the rename, the same
  as the people registry (no lock: writers are one person or one daily task).
- `HOTLINE_REGISTRY_DEVICES` overrides the path; `--map PATH` points one command
  at a scratch map, and a scratch map never touches the admin channel.
- The admin channel is published as one document, the people roster then the
  map, because `publish` replaces every message the bot owns there. Every edit
  republishes it; `check` republishes only when a duty changed state, so a
  quiet day sends him no notification.

## Keeping it true

A map that is only written by hand decays the way handoffs do. Two things keep it
honest:

1. **`check` runs daily** at 10:00 from the wake shell task `device-map-daily`
   on archserver (`scripts/device-check.sh`). A state change republishes the
   admin channel, and a contradiction is also sent to the operator (hotline-80),
   when it appears rather than when someone next needs the map.
2. **`discover`** shows the other direction: work that is running but that
   nobody has written down.

## Seed

Seeded from the 2026-10-28 survey: 4 devices (archserver, pigion, arch, phone)
and 37 duties. First live check: 35 verified, 0 contradicted, 2 unprobed (the
phone has no ssh). `discover` then found nothing unclaimed on archserver or
pigion once distro timers and desktop-session units were filtered out; on the
laptop it found `subjectdesk`, which was added.

All three result states were exercised on real machines: docker (stopped on
purpose) came back contradicted, the offline `djole` came back unknown after the
6 s ssh connect timeout, everything else verified.

## Limitations

- Probes read state; they do not prove a service *works* (a unit can be active
  and broken). For hotline's own daemons that is what their health checks are for.
- Remote probes need key-based ssh from archserver. A device without it (the
  phone, the Windows boxes) can carry only unprobed duties.
- `check` runs from one machine. If archserver is off, nobody checks, and the
  admin channel keeps the last result, with its date.
- `freshest` compares mtimes, so a copy touched without being changed reads as
  "newer".
