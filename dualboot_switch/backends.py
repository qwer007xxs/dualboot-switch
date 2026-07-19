"""Boot backends.

Every backend exposes the same two operations:

    list_entries()       -> list[BootEntry]
    set_next_boot(entry) -> None          # one-shot; does NOT change the default

Nothing here ever changes the persistent boot order. The whole point of the
tool is "boot into the other OS *once*, then go back to normal".
"""

from __future__ import annotations

import glob
import os
import platform
import re
from typing import List, Optional

from .core import (
    BootEntry,
    BootSwitchError,
    SystemInfo,
    detect_firmware,
    is_elevated,
    require_binary,
    resolve_binary,
    run,
)


def _reject(entry_id: str, why: str):
    raise BootSwitchError(
        f"refusing to use boot entry id {entry_id!r}: {why}. "
        "Entry ids must come from this tool's own listing."
    )


class Backend:
    name = "base"
    description = ""
    #: commands that must exist on PATH for this backend to be usable
    requires: tuple = ()

    @classmethod
    def available(cls) -> bool:
        return all(resolve_binary(c) for c in cls.requires)

    def list_entries(self) -> List[BootEntry]:
        raise NotImplementedError

    def set_next_boot(self, entry: BootEntry) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Windows: bcdedit against the UEFI firmware boot manager
# --------------------------------------------------------------------------- #

_GUID_RE = re.compile(r"^\{[0-9a-fA-F-]{8,}\}$|^\{[a-z]+\}$")
_KV_SPLIT = re.compile(r"\s{2,}")


def _looks_like_description(value: str) -> bool:
    """Filter out GUIDs, device paths and flags when hunting for a label.

    bcdedit localises its key names, so we cannot simply look for the literal
    key ``description`` on a Thai or German Windows. Instead we look at the
    shape of the *values*.
    """
    if not value:
        return False
    if _GUID_RE.match(value):
        return False
    if "=" in value or value.startswith("\\"):
        return False
    if value.strip().lower() in {"yes", "no", "true", "false"}:
        return False
    if value.replace("-", "").isdigit():
        return False
    return True


def parse_bcdedit_firmware(text: str) -> List[BootEntry]:
    """Parse the output of ``bcdedit /enum firmware``."""
    entries: List[BootEntry] = []
    default_id: Optional[str] = None

    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        # Drop the "-------" underline that follows every block header.
        if set(lines[1].strip()) <= {"-"}:
            header, body = lines[0].strip(), lines[2:]
        else:
            header, body = lines[0].strip(), lines[1:]

        items = []
        for line in body:
            if line[:1].isspace():          # continuation of a multi-value key
                continue
            parts = _KV_SPLIT.split(line.strip(), maxsplit=1)
            if len(parts) == 2:
                items.append((parts[0].strip(), parts[1].strip()))

        identifier = next(
            (v for _, v in items if _GUID_RE.match(v)), None
        )
        if not identifier:
            continue

        # {fwbootmgr} is the container, not a bootable target. Its first
        # displayorder entry is the firmware default.
        if identifier == "{fwbootmgr}":
            for line in body:
                m = re.search(r"(\{[0-9a-fA-F-]{8,}\}|\{bootmgr\})", line)
                if m:
                    default_id = m.group(1)
                    break
            continue

        label = next(
            (v for k, v in items
             if k.lower() == "description" and _looks_like_description(v)),
            None,
        )
        if label is None:
            label = next((v for _, v in items if _looks_like_description(v)), header)

        entries.append(
            BootEntry(id=identifier, label=label, detail=header)
        )

    for e in entries:
        if default_id and e.id.lower() == default_id.lower():
            e.is_default = True
        if e.id == "{bootmgr}" and platform.system() == "Windows":
            e.is_current = True
    return entries


class WindowsFirmwareBackend(Backend):
    name = "bcdedit"
    description = "Windows UEFI firmware boot manager (bcdedit /enum firmware)"
    requires = ("bcdedit",)

    @classmethod
    def available(cls) -> bool:
        return os.name == "nt" and resolve_binary("bcdedit") is not None

    def list_entries(self) -> List[BootEntry]:
        res = run([require_binary("bcdedit"), "/enum", "firmware"])
        if res.returncode != 0:
            hint = (
                " (run as Administrator)"
                if not is_elevated()
                else " (is this machine actually UEFI?)"
            )
            raise BootSwitchError(
                f"bcdedit /enum firmware failed{hint}: "
                f"{res.stderr.strip() or res.stdout.strip()}"
            )
        return parse_bcdedit_firmware(res.stdout)

    def set_next_boot(self, entry: BootEntry) -> None:
        if not is_elevated():
            raise BootSwitchError("Administrator rights are required.")
        if not _GUID_RE.match(entry.id):
            _reject(entry.id, "not a well-formed bcdedit identifier")
        run(
            [require_binary("bcdedit"), "/set", "{fwbootmgr}",
             "bootsequence", entry.id],
            check=True,
        )


# --------------------------------------------------------------------------- #
# Linux: efibootmgr (works no matter which bootloader is installed)
# --------------------------------------------------------------------------- #

_EFI_ENTRY_RE = re.compile(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.*)$")


def parse_efibootmgr(text: str) -> List[BootEntry]:
    """Parse the output of ``efibootmgr`` (optionally ``-v``)."""
    entries: List[BootEntry] = []
    current = None
    order: List[str] = []

    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("BootCurrent:"):
            current = line.split(":", 1)[1].strip()
            continue
        if line.startswith("BootOrder:"):
            order = [x.strip() for x in line.split(":", 1)[1].split(",")]
            continue
        m = _EFI_ENTRY_RE.match(line)
        if not m:
            continue
        num, active, rest = m.groups()
        # -v output appends the device path after a tab or after HD(/PciRoot(
        label = re.split(r"\t|(?=\sHD\()|(?=\sPciRoot\()|(?=\sVenHw\()", rest)[0]
        entries.append(
            BootEntry(
                id=num.upper(),
                label=label.strip() or f"Boot{num}",
                detail=rest.strip(),
                is_current=(current is not None and num.upper() == current.upper()),
                is_default=bool(order and num.upper() == order[0].upper()),
            )
        )
        if not active:
            entries[-1].detail = "(inactive) " + entries[-1].detail
    return entries


class EfibootmgrBackend(Backend):
    name = "efibootmgr"
    description = "UEFI firmware boot entries (efibootmgr --bootnext)"
    requires = ("efibootmgr",)

    @classmethod
    def available(cls) -> bool:
        return (
            platform.system() == "Linux"
            and os.path.isdir("/sys/firmware/efi")
            and resolve_binary("efibootmgr") is not None
        )

    def list_entries(self) -> List[BootEntry]:
        res = run([require_binary("efibootmgr")], check=True)
        return parse_efibootmgr(res.stdout)

    def set_next_boot(self, entry: BootEntry) -> None:
        if not is_elevated():
            raise BootSwitchError("root privileges are required (try sudo).")
        # A boot number is exactly four hex digits. Anything else would be an
        # efibootmgr flag, and efibootmgr can create and delete entries.
        if not re.fullmatch(r"[0-9A-Fa-f]{4}", entry.id):
            _reject(entry.id, "not a four-digit UEFI boot number")
        run(
            [require_binary("efibootmgr"), "--bootnext", entry.id],
            check=True,
        )


# --------------------------------------------------------------------------- #
# Linux: systemd-boot
# --------------------------------------------------------------------------- #

def parse_bootctl_list(text: str) -> List[BootEntry]:
    """Parse ``bootctl list`` output into entries."""
    entries: List[BootEntry] = []
    title = None
    ident = None
    source = ""

    def flush():
        nonlocal title, ident, source
        if ident:
            clean = re.sub(r"\s*\((default|selected)\)", "", title or ident).strip()
            entries.append(
                BootEntry(
                    id=ident,
                    label=clean,
                    detail=source,
                    is_default="(default)" in (title or ""),
                    is_current="(selected)" in (title or ""),
                )
            )
        title = ident = None
        source = ""

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("title:"):
            flush()
            title = line.split(":", 1)[1].strip()
        elif line.startswith("id:"):
            ident = line.split(":", 1)[1].strip()
        elif line.startswith("source:"):
            source = line.split(":", 1)[1].strip()
    flush()
    return entries


class SystemdBootBackend(Backend):
    name = "systemd-boot"
    description = "systemd-boot loader entries (bootctl set-oneshot)"
    requires = ("bootctl",)

    @classmethod
    def available(cls) -> bool:
        if platform.system() != "Linux" or resolve_binary("bootctl") is None:
            return False
        res = run([require_binary("bootctl"), "is-installed"])
        return res.returncode == 0 and "yes" in res.stdout.lower()

    def list_entries(self) -> List[BootEntry]:
        res = run([require_binary("bootctl"), "list"], check=True)
        return parse_bootctl_list(res.stdout)

    def set_next_boot(self, entry: BootEntry) -> None:
        if not is_elevated():
            raise BootSwitchError("root privileges are required (try sudo).")
        # An entry id is a bare filename under loader/entries, or an
        # auto-generated id. Never a path and never an option.
        if (not re.fullmatch(r"[\w.+-]+", entry.id)) or entry.id.startswith("-"):
            _reject(entry.id, "not a plain systemd-boot entry id")
        run(
            [require_binary("bootctl"), "set-oneshot", entry.id],
            check=True,
        )


# --------------------------------------------------------------------------- #
# Linux: GRUB
# --------------------------------------------------------------------------- #

_MENUENTRY_RE = re.compile(
    r"^\s*(menuentry|submenu)\s+(['\"])(?P<title>.*?)\2", re.MULTILINE
)

GRUB_CFG_CANDIDATES = (
    "/boot/grub/grub.cfg",
    "/boot/grub2/grub.cfg",
    "/boot/efi/EFI/*/grub.cfg",
)


def parse_grub_cfg(text: str) -> List[BootEntry]:
    """Extract top-level and one level of nested menu entries from grub.cfg.

    GRUB addresses nested entries as ``"Submenu title>Entry title"``, which is
    exactly what we build here.
    """
    entries: List[BootEntry] = []
    submenu_stack: List[str] = []
    depth = 0

    for line in text.splitlines():
        m = _MENUENTRY_RE.match(line)
        if m:
            kind, title = m.group(1), m.group("title")
            path = ">".join([*submenu_stack, title])
            if kind == "menuentry":
                entries.append(BootEntry(id=path, label=title, detail=path))
            else:
                submenu_stack.append(title)
            if "{" in line:
                depth += 1
            continue
        depth += line.count("{") - line.count("}")
        if submenu_stack and depth < len(submenu_stack):
            submenu_stack.pop()
    return entries


class GrubBackend(Backend):
    name = "grub"
    description = "GRUB menu entries (grub-reboot / grub2-reboot)"

    @classmethod
    def _tool(cls) -> Optional[str]:
        return resolve_binary("grub-reboot") or resolve_binary("grub2-reboot")

    @classmethod
    def _cfg(cls) -> Optional[str]:
        for pattern in GRUB_CFG_CANDIDATES:
            for path in sorted(glob.glob(pattern)):
                if os.path.isfile(path):
                    return path
        return None

    @classmethod
    def available(cls) -> bool:
        return (
            platform.system() == "Linux"
            and cls._tool() is not None
            and cls._cfg() is not None
        )

    def list_entries(self) -> List[BootEntry]:
        cfg = self._cfg()
        if not cfg:
            raise BootSwitchError("grub.cfg not found")
        try:
            with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
                return parse_grub_cfg(fh.read())
        except PermissionError as exc:
            raise BootSwitchError(f"cannot read {cfg} (try sudo)") from exc

    def set_next_boot(self, entry: BootEntry) -> None:
        if not is_elevated():
            raise BootSwitchError("root privileges are required (try sudo).")
        tool = self._tool()
        if not tool:
            raise BootSwitchError("grub-reboot not found")
        # grub-reboot takes a menu title, which can contain almost anything, so
        # instead of pattern-matching we require the id to be one we just read
        # out of grub.cfg ourselves.
        if entry.id not in {e.id for e in self.list_entries()}:
            _reject(entry.id, "no such entry in grub.cfg")
        # grub-reboot's argument parser rejects "--" as an unrecognised
        # option, so we cannot use it as a separator here.
        if entry.id.startswith("-"):
            _reject(entry.id, "would be parsed as an option")
        res = run([tool, entry.id])
        if res.returncode != 0:
            raise BootSwitchError(
                f"{os.path.basename(tool)} failed: "
                f"{res.stderr.strip() or res.stdout.strip()}\n"
                "GRUB_DEFAULT=saved must be set in /etc/default/grub for "
                "one-shot booting to work."
            )


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

#: Preference order. Firmware-level backends come first because they are the
#: only ones that reliably reach *the other OS* rather than another kernel.
ALL_BACKENDS = (
    WindowsFirmwareBackend,
    EfibootmgrBackend,
    SystemdBootBackend,
    GrubBackend,
)


def available_backends() -> List[Backend]:
    found = []
    for cls in ALL_BACKENDS:
        try:
            if cls.available():
                found.append(cls())
        except Exception:
            continue
    return found


def pick_backend(preferred: Optional[str] = None) -> Optional[Backend]:
    backends = available_backends()
    if preferred:
        for b in backends:
            if b.name == preferred:
                return b
        raise BootSwitchError(
            f"backend '{preferred}' is not available here "
            f"(available: {', '.join(b.name for b in backends) or 'none'})"
        )
    return backends[0] if backends else None


def inspect_system(preferred: Optional[str] = None) -> SystemInfo:
    """Detect firmware, choose a backend and enumerate boot entries."""
    info = SystemInfo(
        os_name=platform.system(),
        firmware=detect_firmware(),
        needs_privileges=not is_elevated(),
    )

    if info.firmware == "bios":
        info.notes.append(
            "Legacy BIOS/MBR detected. One-shot firmware boot is not available; "
            "only GRUB can offer a one-shot entry here."
        )

    try:
        backend = pick_backend(preferred)
    except BootSwitchError as exc:
        info.notes.append(str(exc))
        return info

    if backend is None:
        info.notes.append(
            "No supported boot backend found. Expected one of: bcdedit (Windows), "
            "efibootmgr, bootctl, or grub-reboot."
        )
        return info

    info.backend_name = backend.name
    try:
        info.entries = backend.list_entries()
    except BootSwitchError as exc:
        info.notes.append(str(exc))
    return info
