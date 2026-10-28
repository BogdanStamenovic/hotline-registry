"""Which machine handles which part of the system, checked against the machines.

Asked for 2026-10-28: "detailing what parts of the system a certain device
handles. For example on archserver hotline cvoiced is running as well as track
tasks. On pigion wake. On arch laptop is a more up to date keyring." The full
spec is docs/DEVICES.md.

**Every duty is a claim with a probe.** A map written once and read forever is a
status field, and this project has paid for those repeatedly: the handoff that
said "do not touch these files" long after the reason was gone, the registry row
that said "working" after a power-off. So a duty records how to test it, `check`
runs the tests on the live machines, and the map shows the result and its date
rather than the claim alone.

**Unreachable is not false.** A laptop that is asleep says nothing about its
duties, so a probe that cannot run yields `unknown`, never `contradicted`. Only a
probe that ran and failed contradicts anything.

**Not a consent record.** This shares a CLI and an admin channel with the people
registry and nothing else. People put themselves on that list and nothing may
add them by hand; devices are added by whoever knows about them. Keeping the two
in separate modules and separate files is what stops one rule leaking into the
other.

The store is outside the repository (the repository is public; the store holds
addresses and MAC addresses), mode 600, next to the people registry.
"""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import socket
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .registry import RegistryError


def default_store() -> pathlib.Path:
    # Read per call, not at import: the test suite points this at a temp file,
    # and an import-time constant would quietly keep the real one.
    return pathlib.Path(
        os.environ.get("HOTLINE_REGISTRY_DEVICES", "")
        or pathlib.Path.home() / ".local/share/hotline-registry/devices.json"
    )

PROBE_TYPES = ("unit", "command", "freshest")
STATES = ("verified", "contradicted", "unknown", "unprobed")
MARK = {"verified": "✅", "contradicted": "❌", "unknown": "❔", "unprobed": "➖"}

SSH_TIMEOUT = 15.0
# ssh's own exit status when it could not connect or authenticate, as opposed to
# the remote command's exit status. Only this one means "could not ask".
SSH_FAILED = 255

# Units every Linux box runs, which are nobody's duty. `discover` hides them so
# what is left is the work somebody actually set up.
BASE_UNITS = (
    "systemd-", "dbus", "polkit", "getty@", "user@", "NetworkManager", "wpa_supplicant",
    "avahi", "accounts-daemon", "udisks2", "upower", "rtkit", "bluetooth", "cups", "colord",
    "ModemManager", "power-profiles", "switcheroo", "serial-getty", "at-spi", "gvfs", "xdg-",
    "gnome-", "evolution-", "dconf", "pipewire", "wireplumber", "gcr-", "obex", "filter-chain",
    "mpris-proxy", "dirmngr@", "iwd", "cron", "cronie", "sshd", "ssh.", "tailscaled", "nvidia",
    "bolt", "containerd", "docker", "profiler", "dbus-", "gdm", "fwupd", "thermald", "irqbalance",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Device:
    name: str
    kind: str = ""
    ssh: str = ""
    local: bool = False
    power: str = ""
    wol_mac: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Device:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class Duty:
    id: str
    device: str
    title: str
    kind: str = "service"
    probe: dict[str, Any] | None = None
    if_down: str = ""
    last: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> str:
        if not self.probe:
            return "unprobed"
        return str(self.last.get("state") or "unknown")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Duty:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def haystack(self) -> str:
        bits = [self.id, self.title, self.kind, self.if_down, json.dumps(self.probe or {})]
        return " ".join(bits).lower()


class DeviceMap:
    """The store: devices and their duties, one JSON file."""

    def __init__(self, path: pathlib.Path | str | None = None) -> None:
        self.path = pathlib.Path(path) if path else default_store()
        self.devices: dict[str, Device] = {}
        self.duties: dict[str, Duty] = {}
        self.load()

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            raise RegistryError(f"cannot read the device map at {self.path}: {exc}") from exc
        self.devices = {k: Device.from_dict(v) for k, v in dict(raw.get("devices", {})).items()}
        self.duties = {k: Duty.from_dict(v) for k, v in dict(raw.get("duties", {})).items()}

    def save(self) -> None:
        """Atomic, and mode 600 before the rename -- same reasoning as `Registry.save`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(".tmp")
        payload = {
            "schema": 1,
            "devices": {k: asdict(d) for k, d in sorted(self.devices.items())},
            "duties": {k: asdict(d) for k, d in sorted(self.duties.items())},
        }
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    # ---- queries ---------------------------------------------------------

    def device(self, name: str) -> Device:
        found = self.devices.get(name.strip().lower())
        if found is None:
            known = ", ".join(sorted(self.devices)) or "none"
            raise RegistryError(f"no device called {name!r}. Known: {known}")
        return found

    def duties_of(self, name: str) -> list[Duty]:
        return sorted((d for d in self.duties.values() if d.device == name), key=lambda d: d.id)

    def where(self, query: str) -> list[Duty]:
        """Which duties match QUERY -- the "which box runs X" question."""
        words = query.lower().split()
        if not words:
            return []
        return sorted(
            (d for d in self.duties.values()
             if all(w in d.haystack() or w in d.device for w in words)),
            key=lambda d: d.id,
        )

    # ---- writes ----------------------------------------------------------

    def put_device(self, device: Device) -> Device:
        device.name = device.name.strip().lower()
        if not device.name:
            raise RegistryError("a device needs a name")
        self.devices[device.name] = device
        return device

    def drop_device(self, name: str, force: bool = False) -> Device:
        device = self.device(name)
        owned = self.duties_of(device.name)
        if owned and not force:
            raise RegistryError(
                f"{device.name} still has {len(owned)} duties ({', '.join(d.id for d in owned)}); "
                "drop them first, or pass --force to drop them with it"
            )
        for duty in owned:
            del self.duties[duty.id]
        return self.devices.pop(device.name)

    def claim(self, duty: Duty) -> Duty:
        self.device(duty.device)
        validate_probe(duty.probe)
        if duty.probe and duty.probe.get("type") == "freshest":
            for other in duty.probe.get("among", []):
                self.device(other)
        previous = self.duties.get(duty.id)
        # A re-claim with the same probe keeps its last result; a new probe has
        # not been run yet, so its old result would describe a different test.
        if previous is not None and previous.probe == duty.probe:
            duty.last = previous.last
        self.duties[duty.id] = duty
        return duty

    def drop(self, duty_id: str) -> Duty:
        if duty_id not in self.duties:
            raise RegistryError(f"no duty {duty_id!r}")
        return self.duties.pop(duty_id)


def validate_probe(probe: dict[str, Any] | None) -> None:
    if probe is None:
        return
    kind = probe.get("type")
    if kind not in PROBE_TYPES:
        raise RegistryError(f"probe type must be one of {', '.join(PROBE_TYPES)}, not {kind!r}")
    if kind == "unit" and not probe.get("unit"):
        raise RegistryError("a unit probe needs a unit name")
    if kind == "unit" and probe.get("scope", "user") not in ("user", "system"):
        raise RegistryError("a unit probe's scope is 'user' or 'system'")
    if kind == "command" and not probe.get("command"):
        raise RegistryError("a command probe needs a command")
    if kind == "freshest" and (not probe.get("path") or len(probe.get("among", [])) < 2):
        raise RegistryError("a freshest probe needs a path and at least two devices in 'among'")


# ---- running things on a device --------------------------------------------


class Unreachable(Exception):
    """The probe could not be run on the device at all."""


Runner = Callable[[Device, str], "tuple[int, str]"]


def is_here(device: Device) -> bool:
    return device.local or device.name == socket.gethostname().lower()


def run_on(device: Device, command: str, timeout: float = SSH_TIMEOUT) -> tuple[int, str]:
    """Run a shell command on the device. Returns (exit status, output).

    Raises Unreachable when the command never ran: no ssh route, ssh refused,
    or a timeout. A command that ran and failed is a result, not an error.
    """
    if is_here(device):
        argv = ["sh", "-c", command]
    elif device.ssh:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", device.ssh, command]
    else:
        raise Unreachable(f"{device.name} has no ssh route")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise Unreachable(f"{device.name} did not answer within {timeout:.0f}s") from exc
    except OSError as exc:
        raise Unreachable(f"could not run a probe on {device.name}: {exc}") from exc
    output = (done.stdout + done.stderr).strip()
    if not is_here(device) and done.returncode == SSH_FAILED:
        raise Unreachable(f"ssh to {device.ssh} failed: {output[-200:] or 'no detail'}")
    return done.returncode, output


def _home_path(path: str) -> str:
    """A path for a remote shell: `~/x` becomes "$HOME/x", quoted so spaces survive."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def probe(duty: Duty, dmap: DeviceMap, runner: Runner = run_on) -> dict[str, Any]:
    """Test one duty now. Returns its new `last` record."""
    spec = duty.probe
    if not spec:
        return {"state": "unprobed", "at": _now(), "detail": "no probe defined"}
    device = dmap.device(duty.device)
    kind = spec["type"]
    try:
        if kind == "unit":
            user = spec.get("scope", "user") == "user"
            cmd = f"systemctl {'--user ' if user else ''}is-active {shlex.quote(spec['unit'])}"
            rc, out = runner(device, cmd)
            state = "verified" if rc == 0 else "contradicted"
            return {"state": state, "at": _now(), "detail": f"{spec['unit']}: {out or rc}"}
        if kind == "command":
            rc, out = runner(device, spec["command"])
            state = "verified" if rc == 0 else "contradicted"
            return {"state": state, "at": _now(), "detail": f"exit {rc}" + (f": {out[-160:]}" if out else "")}
        if kind == "freshest":
            return _freshest(duty, dmap, runner)
    except Unreachable as exc:
        return {"state": "unknown", "at": _now(), "detail": str(exc)}
    raise RegistryError(f"unknown probe type {kind!r}")


def _freshest(duty: Duty, dmap: DeviceMap, runner: Runner) -> dict[str, Any]:
    spec = duty.probe or {}
    cmd = f"stat -c %Y -- {_home_path(spec['path'])} 2>/dev/null || echo missing"
    stamps: dict[str, int | None] = {}
    unreachable: list[str] = []
    for name in spec["among"]:
        try:
            _status, out = runner(dmap.device(name), cmd)
        except Unreachable:
            unreachable.append(name)
            continue
        last = out.strip().splitlines()[-1] if out.strip() else ""
        stamps[name] = int(last) if last.isdigit() else None
    have = {n: t for n, t in stamps.items() if t is not None}
    def when(t: int) -> str:
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")
    summary = ", ".join(f"{n} {when(t)}" for n, t in sorted(have.items(), key=lambda x: -x[1]))
    mine = stamps.get(duty.device)
    if duty.device in unreachable:
        # Its own copy could not be read, which is not the same as it having none.
        return {"state": "unknown", "at": _now(),
                "detail": f"could not ask {duty.device} itself; seen elsewhere: {summary or 'none'}"}
    if mine is not None and have and mine == max(have.values()) and not unreachable:
        return {"state": "verified", "at": _now(), "detail": f"newest copy: {summary}"}
    if have and (mine is None or mine < max(have.values())):
        newest = max(have, key=lambda n: have[n])
        # A newer copy elsewhere is a contradiction even if some device could
        # not be asked: whatever that one holds, this one is not the newest.
        return {"state": "contradicted", "at": _now(),
                "detail": f"{newest} has a newer copy: {summary or 'no copies found'}"}
    return {"state": "unknown", "at": _now(),
            "detail": f"could not ask {', '.join(unreachable) or 'anyone'}; seen so far: {summary or 'none'}"}


def check(dmap: DeviceMap, devices: Iterable[str] = (), runner: Runner = run_on) -> list[tuple[Duty, str, str]]:
    """Probe every duty (or those of `devices`), store the results.

    Returns (duty, previous state, new state) for every duty that was probed.
    """
    wanted = {d.lower() for d in devices}
    for name in wanted:
        dmap.device(name)
    changes = []
    for duty in sorted(dmap.duties.values(), key=lambda d: d.id):
        if wanted and duty.device not in wanted:
            continue
        before = duty.state
        duty.last = probe(duty, dmap, runner)
        changes.append((duty, before, duty.state))
    return changes


def discover(dmap: DeviceMap, name: str, runner: Runner = run_on) -> list[str]:
    """Running units on the device that no duty on it claims."""
    device = dmap.device(name)
    found: list[str] = []
    for scope in ("--user ", ""):
        cmd = (f"systemctl {scope}list-units --type=service,timer --state=running,waiting "
               "--no-legend --plain 2>/dev/null")
        _status, out = runner(device, cmd)
        for line in out.splitlines():
            unit = line.split()[0] if line.split() else ""
            if unit and not unit.startswith(BASE_UNITS):
                found.append(("user " if scope else "system ") + unit)
    claimed = {
        str((d.probe or {}).get("unit")) for d in dmap.duties_of(device.name)
        if (d.probe or {}).get("type") == "unit"
    }
    return sorted({f for f in found if f.split(" ", 1)[1] not in claimed})


# ---- the admin channel -----------------------------------------------------


def render(dmap: DeviceMap) -> list[str]:
    """The map as Discord message blocks, one per device, for the admin mirror."""
    if not dmap.devices:
        return []
    counts = {s: 0 for s in STATES}
    for duty in dmap.duties.values():
        counts[duty.state] += 1
    header = (
        f"**Device map — {len(dmap.devices)} devices, {len(dmap.duties)} duties**\n"
        f"{MARK['verified']} {counts['verified']} verified · {MARK['contradicted']} "
        f"{counts['contradicted']} contradicted · {MARK['unknown']} {counts['unknown']} unknown · "
        f"{MARK['unprobed']} {counts['unprobed']} unprobed. Rewritten on every change and every check."
    )
    blocks = [header]
    for device in sorted(dmap.devices.values(), key=lambda d: d.name):
        meta = " · ".join(x for x in (device.kind, device.power, device.notes) if x)
        lines = [f"\n**{device.name}**" + (f" — {meta}" if meta else "")]
        for duty in dmap.duties_of(device.name):
            when = str(duty.last.get("at", ""))[:16].replace("T", " ")
            lines.append(f"{MARK[duty.state]} {duty.title}" + (f" · checked {when}Z" if when else ""))
            if duty.state == "contradicted":
                lines.append(f"  ↳ {duty.last.get('detail', '')}"[:300])
        if len(lines) == 1:
            lines.append("(no duties recorded)")
        blocks.append("\n".join(lines))
    return blocks
