"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__
from .backends import available_backends, inspect_system, pick_backend
from .core import BootEntry, BootSwitchError, is_elevated, reboot_now, relaunch_elevated


def _match(entries: List[BootEntry], needle: str) -> Optional[BootEntry]:
    """Resolve a user-supplied string to exactly one entry."""
    low = needle.lower()
    for e in entries:
        if e.id.lower() == low:
            return e
    exact = [e for e in entries if e.label.lower() == low]
    if len(exact) == 1:
        return exact[0]
    partial = [e for e in entries if low in e.label.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(repr(e.label) for e in partial)
        raise BootSwitchError(f"{needle!r} is ambiguous — matches: {names}")
    family = [e for e in entries if e.os_family == low]
    if len(family) == 1:
        return family[0]
    return None


def cmd_list(args) -> int:
    info = inspect_system(args.backend)
    print(f"OS        : {info.os_name}")
    print(f"Firmware  : {info.firmware.upper()}")
    print(f"Backend   : {info.backend_name}")
    print(f"Available : {', '.join(b.name for b in available_backends()) or 'none'}")
    print(f"Elevated  : {'yes' if is_elevated() else 'no'}")
    for note in info.notes:
        print(f"Note      : {note}")

    if not info.entries:
        return 1

    print()
    width = max(len(e.id) for e in info.entries)
    for e in info.entries:
        flags = []
        if e.is_current:
            flags.append("current")
        if e.is_default:
            flags.append("default")
        tag = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {e.id:<{width}}  {e.icon} {e.label}{tag}")
        if args.verbose and e.detail:
            print(f"  {'':<{width}}     {e.detail}")
    return 0


def cmd_boot(args) -> int:
    if not is_elevated():
        if args.no_elevate:
            print(
                "Elevated rights are required to change the boot target.",
                file=sys.stderr,
            )
            return 2
        if relaunch_elevated(sys.argv[1:]):
            return 0
        print(
            "Could not elevate automatically. Re-run as administrator/root.",
            file=sys.stderr,
        )
        return 2

    backend = pick_backend(args.backend)
    if backend is None:
        print("No supported boot backend found on this system.", file=sys.stderr)
        return 1

    entries = backend.list_entries()
    entry = _match(entries, args.target)
    if entry is None:
        print(f"No boot entry matches {args.target!r}.", file=sys.stderr)
        print("Known entries:", file=sys.stderr)
        for e in entries:
            print(f"  {e.id}  {e.label}", file=sys.stderr)
        return 1

    backend.set_next_boot(entry)
    print(f"Next boot set to: {entry.label}  ({backend.name} / {entry.id})")

    if args.reboot:
        if not args.yes:
            answer = input("Reboot now? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Not rebooting. The one-shot target is still armed.")
                return 0
        reboot_now()
    else:
        print("Restart when ready — the target applies to the next boot only.")
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main  # imported lazily so CLI works headless

    return gui_main(args.backend)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dualboot-switch",
        description="Set a one-shot boot target and reboot into your other OS.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--backend",
        choices=["bcdedit", "efibootmgr", "systemd-boot", "grub"],
        help="force a specific backend instead of auto-detecting",
    )

    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="show detected boot entries")
    p_list.add_argument("-v", "--verbose", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_boot = sub.add_parser("boot", help="arm a one-shot boot target")
    p_boot.add_argument(
        "target",
        help="entry id, exact label, substring of a label, or 'windows'/'linux'",
    )
    p_boot.add_argument(
        "-r", "--reboot", action="store_true", help="restart immediately"
    )
    p_boot.add_argument(
        "-y", "--yes", action="store_true", help="skip the reboot confirmation"
    )
    p_boot.add_argument(
        "--no-elevate",
        action="store_true",
        help="fail instead of prompting for elevation",
    )
    p_boot.set_defaults(func=cmd_boot)

    p_gui = sub.add_parser("gui", help="open the graphical interface (default)")
    p_gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args([*(argv or []), "gui"])
    try:
        return args.func(args)
    except BootSwitchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
