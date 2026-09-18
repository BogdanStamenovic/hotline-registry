"""Command-line interface for hotline-registry.

The interface an agent uses to see who it may contact, and to contact them.

stdout carries the answer and nothing else -- a roster, a message id, what the
person said on the phone -- so `$(hotline-registry ...)` is usable. Progress,
warnings and errors go to stderr.

Exit codes: 0 success, 1 the operation failed, 2 usage error or aborted.
`call` narrows 1 the way `hotline-call` does, because "they declined" and "the
daemon is dead" call for completely different behaviour from whatever is driving:

    3  it rang and nobody answered
    4  they declined

**Every outward subcommand takes `--dry-run`**, which prints exactly what would
be sent to exactly whom and sends nothing. A tool that rings other people's
phones should be inspectable without ringing one.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import NoReturn

from . import __version__
from .contact import (
    CallResult,
    ContactError,
    call_health,
    call_person,
    reconcile,
    send_message,
)
from .registry import Person, Registry, RegistryError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_UNANSWERED = 3
EXIT_DECLINED = 4


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="hotline-registry",
        description="See who has consented to be contacted, and contact them.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print detailed progress")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress non-error output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--store", default=None, metavar="PATH",
                        help="registry file (default ~/.local/share/hotline-registry/registry.json)")
    sub = parser.add_subparsers(dest="command")

    ls = sub.add_parser("list", help="everyone who has filled in the registry form")
    ls.add_argument("--json", action="store_true", help="machine-readable, for an agent")
    ls.add_argument("--all", action="store_true", help="include people who have withdrawn consent")
    ls.add_argument("--callable", action="store_true", dest="only_callable",
                    help="only people who gave a SIP address")

    show = sub.add_parser("show", help="one person in full, including what may be done to them")
    show.add_argument("who")
    show.add_argument("--json", action="store_true")

    find = sub.add_parser("find", help="people whose self-description mentions something")
    find.add_argument("query")
    find.add_argument("--json", action="store_true")

    msg = sub.add_parser("message", help="send somebody a Discord DM")
    msg.add_argument("who")
    msg.add_argument("text", nargs="*", help="the message; '-' reads stdin")
    msg.add_argument("--dry-run", action="store_true", help="print what would be sent, send nothing")

    call = sub.add_parser("call", help="ring somebody's Linphone and wait for their answer")
    call.add_argument("who")
    call.add_argument("reason", nargs="*", help="what you need from them, in a sentence")
    call.add_argument("--context", default="",
                      help="everything the voice on the line needs to know. It knows "
                           "NOTHING else -- not your work, not his files, by design")
    call.add_argument("--source", default="an agent", help="who is calling; they hear this")
    call.add_argument("--timeout", type=float, default=600.0, metavar="SEC")
    call.add_argument("--ring-timeout", type=float, default=45.0, metavar="SEC")
    call.add_argument("--no-wait", action="store_true", help="ring and exit without waiting")
    call.add_argument("--dry-run", action="store_true",
                      help="print who would be rung at which address, ring nothing")

    sub.add_parser("status", help="whether messaging and calling actually work right now")

    rec = sub.add_parser(
        "reconcile",
        help="check everyone is still in the server, and revoke those who left")
    rec.add_argument("--dry-run", action="store_true",
                     help="report who has left, change nothing")

    revoke = sub.add_parser(
        "revoke", help="mark somebody uncontactable (they left, or asked not to be contacted)")
    revoke.add_argument("who")
    revoke.add_argument("--dry-run", action="store_true")
    return parser


def _row(person: Person) -> str:
    return (f"{person.name:<20} {person.discord_name:<22} "
            f"{(person.sip or '-'):<34} {person.channels():<16} {person.capabilities}")


def _as_dict(person: Person) -> dict:
    return {
        "discord_id": person.discord_id, "discord_name": person.discord_name,
        "name": person.name, "sip": person.sip, "capabilities": person.capabilities,
        "registered_at": person.registered_at, "updated_at": person.updated_at,
        "revoked": person.revoked, "messageable": person.messageable,
        "callable": person.callable_,
    }


def _print_roster(people: list[Person], as_json: bool) -> None:
    if as_json:
        print(json.dumps([_as_dict(p) for p in people], indent=2))
        return
    if not people:
        print("(nobody has filled in the registry form yet)")
        return
    header = f"{'NAME':<20} {'DISCORD':<22} {'SIP':<34} {'CHANNELS':<16} CAN DO"
    print(header)
    print("-" * len(header))
    for person in people:
        print(_row(person))


def _text_argument(parts: Sequence[str]) -> str:
    joined = " ".join(parts).strip()
    if joined == "-":
        return sys.stdin.read().strip()
    return joined


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"hotline-registry: error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if not args.command:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    def vlog(message: str) -> None:
        if args.verbose and not args.quiet:
            print(message, file=sys.stderr)

    try:
        registry = Registry(args.store)

        if args.command == "list":
            people = registry.all(include_revoked=args.all)
            if args.only_callable:
                people = [p for p in people if p.callable_]
            _print_roster(people, args.json)
            return EXIT_OK

        if args.command == "find":
            _print_roster(registry.search(args.query), args.json)
            return EXIT_OK

        if args.command == "show":
            person = registry.resolve(args.who)
            if args.json:
                print(json.dumps(_as_dict(person), indent=2))
                return EXIT_OK
            print(f"name          {person.name}")
            print(f"discord       {person.discord_name} ({person.discord_id})")
            print(f"sip           {person.sip or '(none given)'}")
            print(f"can do        {person.capabilities or '(not said)'}")
            print(f"registered    {person.registered_at}")
            print(f"updated       {person.updated_at}")
            print(f"you may       {person.channels()}")
            if person.revoked:
                print("NOTE          consent withdrawn; do not contact")
            return EXIT_OK

        if args.command == "status":
            people = registry.all()
            print(f"store         {registry.path}")
            print(f"people        {len(people)} contactable, "
                  f"{len([p for p in people if p.callable_])} of them callable")
            health = call_health()
            if health.get("ok"):
                fake = bool(health.get("fake"))
                print(f"calling       {'NO -- the doorbell is fake, it rings nothing' if fake else 'yes'}"
                      f" (transport {health.get('transport', '?')})")
            else:
                print(f"calling       no -- {health.get('error') or health}")
            try:
                from .contact import bot_token

                bot_token()
                print("messaging     yes (hotline's bot token found)")
            except ContactError as exc:
                print(f"messaging     no -- {exc}")
            return EXIT_OK

        if args.command == "reconcile":
            if args.dry_run:
                from .contact import still_a_member

                for person in registry.all():
                    state = still_a_member(person)
                    verdict = {True: "present", False: "HAS LEFT", None: "unknown"}[state]
                    print(f"{person.name:<20} {verdict}")
                return EXIT_OK
            present, left, unknown = reconcile(registry)
            for person in left:
                print(f"revoked {person.name} ({person.discord_name}) -- left the server")
            for person in unknown:
                log(f"could not check {person.name}; leaving them alone")
            log(f"{len(present)} still present, {len(left)} left, {len(unknown)} unknown")
            return EXIT_OK

        if args.command == "revoke":
            person = registry.resolve(args.who)
            if args.dry_run:
                print(f"would revoke {person.name} ({person.discord_id})")
                return EXIT_OK
            registry.revoke(person.discord_id)
            log(f"{person.name} is no longer contactable")
            return EXIT_OK

        if args.command == "message":
            person = registry.resolve(args.who)
            text = _text_argument(args.text)
            if not text:
                print("hotline-registry: error: nothing to send", file=sys.stderr)
                return EXIT_USAGE
            if args.dry_run:
                print(f"would DM {person.name} ({person.discord_name}, {person.discord_id}):")
                print(text)
                return EXIT_OK
            vlog(f"opening a DM channel with {person.discord_id}")
            message_id = send_message(person, text, registry)
            log(f"sent to {person.name}")
            print(message_id)
            return EXIT_OK

        if args.command == "call":
            person = registry.resolve(args.who)
            reason = _text_argument(args.reason)
            if not reason:
                print("hotline-registry: error: say why you are calling", file=sys.stderr)
                return EXIT_USAGE
            if args.dry_run:
                if not person.callable_:
                    print(f"would NOT call {person.name}: {person.channels()}")
                    return EXIT_OK
                print(f"would ring {person.name} at {person.sip}")
                print(f"  as       {args.source}")
                print(f"  reason   {reason}")
                print(f"  context  {args.context or '(none)'}")
                return EXIT_OK
            # Narrated only once it is actually going to happen. The refusals
            # live in `call_person`, where they cannot be skipped -- but printing
            # "ringing Bogdan at " and then refusing describes a call that never
            # was, which is the one thing narration must never do.
            if person.callable_:
                log(f"ringing {person.name} at {person.sip}")
            result: CallResult = call_person(
                person, reason, context=args.context, source=args.source,
                timeout=args.timeout, ring_timeout=args.ring_timeout, wait=not args.no_wait,
                registry=registry,
            )
            if result.fake:
                print("hotline-registry: error: the doorbell is fake -- nothing rang",
                      file=sys.stderr)
                return EXIT_FAILED
            log(f"{result.state} via {result.transport} after {result.waited_seconds:.0f}s")
            if result.state == "declined":
                log(f"{person.name} declined the call")
                return EXIT_DECLINED
            if result.state == "unanswered":
                log(f"{person.name} did not answer")
                return EXIT_UNANSWERED
            if result.state == "ringing":
                return EXIT_OK
            if result.state == "answered":
                print(result.reply)
                if args.verbose and result.transcript:
                    for turn in result.transcript:
                        vlog(f"  {turn.get('who', '?')}: {turn.get('text', '')}")
                return EXIT_OK
            print(f"hotline-registry: error: {result.state}: {result.detail}", file=sys.stderr)
            return EXIT_FAILED

        print(f"hotline-registry: error: unknown command {args.command!r}", file=sys.stderr)
        return EXIT_USAGE

    except RegistryError as exc:
        print(f"hotline-registry: error: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":  # `python -m hotline_registry.cli`, which the shim falls back to
    raise SystemExit(main())
