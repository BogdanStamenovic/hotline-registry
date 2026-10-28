"""`hotline-registry devices ...`: the device map's commands (spec: docs/DEVICES.md).

Kept out of cli.py because it is a separate concern sharing only the entry
point; see the module docstring of `devices`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from .devices import (
    MARK,
    Device,
    DeviceMap,
    Duty,
    Unreachable,
    check,
    discover,
)
from .registry import RegistryError

EXIT_OK, EXIT_FAILED = 0, 1


def add_parser(sub: Any) -> None:
    dev = sub.add_parser("devices", help="which device handles which part of the system")
    dev.add_argument("--map", default=None, metavar="PATH",
                     help="device map file (default ~/.local/share/hotline-registry/devices.json)")
    ds = dev.add_subparsers(dest="devices_command")

    ls = ds.add_parser("list", help="devices, their duties, and the last check of each")
    ls.add_argument("--json", action="store_true")
    show = ds.add_parser("show", help="one device in full")
    show.add_argument("device")
    show.add_argument("--json", action="store_true")
    where = ds.add_parser("where", help="which device handles something")
    where.add_argument("query", nargs="+")
    where.add_argument("--json", action="store_true")

    chk = ds.add_parser("check", help="run every probe now; exit 1 if anything is contradicted")
    chk.add_argument("device", nargs="*")
    chk.add_argument("--json", action="store_true")
    chk.add_argument("--no-publish", action="store_true", help="do not rewrite the admin channel")
    chk.add_argument("--changes-only", action="store_true", help="print only duties whose state changed")

    disc = ds.add_parser("discover", help="running units on a device that no duty claims")
    disc.add_argument("device")
    disc.add_argument("--json", action="store_true")

    add = ds.add_parser("add-device", help="add or update a device")
    add.add_argument("name")
    add.add_argument("--kind", default="")
    add.add_argument("--ssh", default="", help="ssh host alias probes use to reach it")
    add.add_argument("--local", action="store_true", help="probes run here rather than over ssh")
    add.add_argument("--power", default="", help="always-on, wakes-on-schedule, when-he-uses-it ...")
    add.add_argument("--wol-mac", default="")
    add.add_argument("--notes", default="")
    add.add_argument("--dry-run", action="store_true")

    claim = ds.add_parser("claim", help="add or update a duty: DEVICE SLUG TITLE")
    claim.add_argument("device")
    claim.add_argument("slug")
    claim.add_argument("title")
    claim.add_argument("--kind", default="service",
                       help="service, schedule, data, capability, endpoint")
    g = claim.add_mutually_exclusive_group()
    g.add_argument("--unit", help="probe: this systemd unit is active")
    g.add_argument("--command", dest="probe_command", help="probe: this shell command exits 0 on the device")
    g.add_argument("--freshest", metavar="PATH", help="probe: the device has the newest copy of PATH")
    claim.add_argument("--system", action="store_true", help="with --unit: a system unit, not a user one")
    claim.add_argument("--among", default="", help="with --freshest: comma-separated devices to compare")
    claim.add_argument("--if-down", default="", help="what stops working when this duty does")
    claim.add_argument("--dry-run", action="store_true")

    drop = ds.add_parser("drop", help="remove a duty by id (device/slug)")
    drop.add_argument("id")
    drop.add_argument("--dry-run", action="store_true")
    dropd = ds.add_parser("drop-device", help="remove a device")
    dropd.add_argument("name")
    dropd.add_argument("--force", action="store_true", help="also drop its duties")
    dropd.add_argument("--dry-run", action="store_true")


def _duty_dict(d: Duty) -> dict[str, Any]:
    out = asdict(d)
    out["state"] = d.state
    return out


def _line(d: Duty) -> str:
    when = str(d.last.get("at", ""))[:16].replace("T", " ")
    tail = f"  ({when}Z)" if when else ""
    return f"  {MARK[d.state]} {d.id:<34} {d.title}{tail}"


def _print_map(dmap: DeviceMap, names: list[str], as_json: bool) -> None:
    if as_json:
        print(json.dumps({
            "devices": [asdict(dmap.devices[n]) for n in names],
            "duties": [_duty_dict(d) for n in names for d in dmap.duties_of(n)],
        }, indent=2))
        return
    for name in names:
        dev = dmap.devices[name]
        meta = " · ".join(x for x in (dev.kind, dev.power, dev.ssh and f"ssh {dev.ssh}", dev.notes) if x)
        print(f"{dev.name}" + (f"  — {meta}" if meta else ""))
        for duty in dmap.duties_of(name):
            print(_line(duty))
        if not dmap.duties_of(name):
            print("  (no duties)")


def run(args: argparse.Namespace, log: Callable[[str], None]) -> int:
    from .admin import publish
    from .registry import Registry

    dmap = DeviceMap(args.map)
    cmd = args.devices_command
    if not cmd:
        print("hotline-registry devices: say which: list, show, where, check, discover, "
              "add-device, claim, drop, drop-device", file=sys.stderr)
        return 2

    def republish() -> None:
        # The admin channel shows the people roster and this map together.
        if not publish(Registry(getattr(args, "store", None))):
            log("note: the admin channel was not republished (see the warning above)")

    if cmd == "list":
        _print_map(dmap, sorted(dmap.devices), args.json)
        return EXIT_OK
    if cmd == "show":
        _print_map(dmap, [dmap.device(args.device).name], args.json)
        return EXIT_OK
    if cmd == "where":
        hits = dmap.where(" ".join(args.query))
        if args.json:
            print(json.dumps([_duty_dict(d) for d in hits], indent=2))
        else:
            for d in hits:
                print(f"{d.device:<12} {MARK[d.state]} {d.title}")
                if d.if_down:
                    print(f"{'':<14}if down: {d.if_down}")
        if not hits:
            log(f"no device has a duty matching {' '.join(args.query)!r}")
        return EXIT_OK if hits else EXIT_FAILED

    if cmd == "check":
        changes = check(dmap, args.device)
        dmap.save()
        if not args.no_publish:
            republish()
        shown = [c for c in changes if not args.changes_only or c[1] != c[2]]
        if args.json:
            print(json.dumps([{**_duty_dict(d), "before": b} for d, b, _a in shown], indent=2))
        else:
            for duty, before, after in shown:
                moved = f"  (was {before})" if before != after else ""
                print(f"{MARK[after]} {duty.id:<34} {after}{moved}  {duty.last.get('detail', '')}"[:220])
        bad = [d for d, _b, a in changes if a == "contradicted"]
        log(f"{len(changes)} duties checked, {len(bad)} contradicted")
        return EXIT_FAILED if bad else EXIT_OK

    if cmd == "discover":
        try:
            found = discover(dmap, args.device)
        except Unreachable as exc:
            print(f"hotline-registry: error: {exc}", file=sys.stderr)
            return EXIT_FAILED
        if args.json:
            print(json.dumps(found, indent=2))
        else:
            for unit in found:
                print(unit)
        log(f"{len(found)} running unit(s) on {args.device} that no duty claims")
        return EXIT_OK

    if cmd == "add-device":
        device = Device(name=args.name, kind=args.kind, ssh=args.ssh, local=args.local,
                        power=args.power, wol_mac=args.wol_mac, notes=args.notes)
        if args.dry_run:
            print(json.dumps(asdict(device), indent=2))
            log("dry run: nothing written")
            return EXIT_OK
        dmap.put_device(device)
        dmap.save()
        republish()
        log(f"device {device.name} saved")
        return EXIT_OK

    if cmd == "claim":
        probe: dict[str, Any] | None = None
        if args.unit:
            probe = {"type": "unit", "unit": args.unit, "scope": "system" if args.system else "user"}
        elif args.probe_command:
            probe = {"type": "command", "command": args.probe_command}
        elif args.freshest:
            among = [a.strip().lower() for a in args.among.split(",") if a.strip()]
            probe = {"type": "freshest", "path": args.freshest, "among": among}
        owner = dmap.device(args.device).name
        duty = Duty(id=f"{owner}/{args.slug.strip().lower()}", device=owner, title=args.title,
                    kind=args.kind, probe=probe, if_down=args.if_down)
        if args.dry_run:
            from .devices import validate_probe

            validate_probe(probe)
            print(json.dumps(_duty_dict(duty), indent=2))
            log("dry run: nothing written")
            return EXIT_OK
        dmap.claim(duty)
        dmap.save()
        republish()
        log(f"duty {duty.id} saved" + ("" if probe else " (no probe: it will show as unprobed)"))
        return EXIT_OK

    if cmd == "drop":
        if args.dry_run:
            if args.id not in dmap.duties:
                raise RegistryError(f"no duty {args.id!r}")
            log(f"dry run: would drop {args.id}")
            return EXIT_OK
        dmap.drop(args.id)
        dmap.save()
        republish()
        log(f"dropped {args.id}")
        return EXIT_OK

    if cmd == "drop-device":
        if args.dry_run:
            owned = dmap.duties_of(dmap.device(args.name).name)
            log(f"dry run: would drop {args.name} and {len(owned)} duties" if args.force or not owned
                else f"dry run: refused, {args.name} still has {len(owned)} duties")
            return EXIT_OK
        dmap.drop_device(args.name, force=args.force)
        dmap.save()
        republish()
        log(f"dropped device {args.name}")
        return EXIT_OK

    print(f"hotline-registry devices: unknown command {cmd!r}", file=sys.stderr)
    return 2
