"""Core types and helpers shared by every backend."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

# Keywords used to guess which OS family an entry belongs to. Order matters:
# the first match wins, so put the more specific strings first.
_OS_HINTS = [
    ("windows", ("windows", "microsoft", "bootmgfw")),
    ("linux", (
        "ubuntu", "debian", "fedora", "arch", "manjaro", "pop!", "pop_os",
        "linux", "grub", "opensuse", "suse", "mint", "endeavour", "nixos",
        "gentoo", "rhel", "centos", "rocky", "alma", "garuda", "zorin",
        "elementary", "kali", "systemd-boot", "shim", "vmlinuz",
    )),
    ("macos", ("mac os", "macos", "apple", "osx")),
    ("firmware", ("setup", "bios", "firmware settings", "uefi settings")),
    ("network", ("pxe", "ipv4", "ipv6", "network")),
    ("removable", ("usb", "cd/dvd", "cdrom", "removable")),
]


def guess_os_family(label: str) -> str:
    """Best-effort guess of the OS family from a boot entry's label."""
    low = (label or "").lower()
    for family, needles in _OS_HINTS:
        if any(n in low for n in needles):
            return family
    return "unknown"


@dataclass
class BootEntry:
    """One selectable boot target, normalised across all backends."""

    id: str                      # backend-specific id (GUID, 0001, index, .conf)
    label: str                   # human readable name
    os_family: str = "unknown"   # windows | linux | macos | firmware | ...
    is_current: bool = False     # the entry we booted from right now
    is_default: bool = False     # the entry the firmware/bootloader defaults to
    detail: str = ""             # extra info for tooltips / verbose CLI

    def __post_init__(self) -> None:
        if self.os_family == "unknown":
            self.os_family = guess_os_family(self.label)

    @property
    def icon(self) -> str:
        return {
            "windows": "\U0001FA9F",
            "linux": "\U0001F427",
            "macos": "\U0001F34E",
            "firmware": "⚙",
            "network": "\U0001F310",
            "removable": "\U0001F4BE",
        }.get(self.os_family, "\U0001F4BF")


@dataclass
class SystemInfo:
    """What we managed to work out about the machine."""

    os_name: str                       # Windows | Linux | Darwin
    firmware: str                      # uefi | bios | unknown
    backend_name: str = "none"
    entries: list = field(default_factory=list)
    needs_privileges: bool = True
    notes: list = field(default_factory=list)


class BootSwitchError(RuntimeError):
    """Raised when a boot operation cannot be completed."""


# --------------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------------- #

def run(cmd, check: bool = False, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command; never raises on non-zero unless ``check`` is set.

    stdout/stderr are decoded leniently because bcdedit and efibootmgr both
    emit locale-specific, occasionally non-UTF-8 bytes.
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        raise BootSwitchError(f"command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BootSwitchError(
            f"command timed out: {' '.join(map(str, cmd))}"
        ) from exc

    stdout = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        raise BootSwitchError(
            f"{' '.join(map(str, cmd))} failed ({proc.returncode}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    return result


def which(name: str) -> Optional[str]:
    return shutil.which(name)


# --------------------------------------------------------------------------- #
# Trusted binary resolution
# --------------------------------------------------------------------------- #
#
# This process may be running elevated. Resolving helper binaries through a
# bare PATH lookup would let anything writable and earlier on PATH -- or, on
# Windows, a stray file in the current working directory, which CreateProcess
# searches before the system directory -- run with our privileges. So we look
# only in directories that require root/Administrator to write.

#: Searched in order. Deliberately excludes /usr/local/* and anything under a
#: user's home, both of which are writable by non-root on many setups.
TRUSTED_UNIX_DIRS = ("/usr/sbin", "/sbin", "/usr/bin", "/bin")


def resolve_binary(name: str) -> Optional[str]:
    """Return an absolute path to a system binary, or None if not found.

    Never falls back to a bare PATH lookup: an unresolvable name is treated as
    missing rather than trusted.
    """
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        for sub in ("System32", "Sysnative"):
            candidate = os.path.join(system_root, sub, name + ".exe")
            if os.path.isfile(candidate):
                return candidate
        return None

    for directory in TRUSTED_UNIX_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def require_binary(name: str) -> str:
    path = resolve_binary(name)
    if path is None:
        raise BootSwitchError(
            f"{name} was not found in a trusted system directory."
        )
    return path


# --------------------------------------------------------------------------- #
# Privilege helpers
# --------------------------------------------------------------------------- #

def is_elevated() -> bool:
    """True if we can write firmware/bootloader state."""
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def relaunch_elevated(argv: Optional[list] = None) -> bool:
    """Try to restart this process with elevated rights.

    Returns True if a new elevated process was started (caller should exit).
    """
    argv = argv if argv is not None else sys.argv[1:]
    script = os.path.abspath(sys.argv[0])

    if os.name == "nt":
        # list2cmdline applies the exact quoting rules CommandLineToArgvW
        # expects, so an argument containing a quote cannot smuggle in extra
        # arguments to the process we are about to elevate.
        params = subprocess.list2cmdline([script, *argv])
        try:
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            return int(rc) > 32
        except Exception:
            return False

    # Linux / BSD: prefer a graphical prompt.
    for launcher in (["pkexec"], ["sudo", "-A"]):
        if which(launcher[0]) is None:
            continue
        if "-A" in launcher and not os.environ.get("SUDO_ASKPASS"):
            continue
        try:
            subprocess.Popen([*launcher, sys.executable, script, *argv])
            return True
        except Exception:
            continue
    return False


# --------------------------------------------------------------------------- #
# Firmware detection
# --------------------------------------------------------------------------- #

def detect_firmware() -> str:
    """Return 'uefi', 'bios', or 'unknown'."""
    system = platform.system()

    if system == "Linux":
        return "uefi" if os.path.isdir("/sys/firmware/efi") else "bios"

    if system == "Windows":
        # bcdedit only knows {fwbootmgr} on UEFI systems.
        try:
            bcdedit = resolve_binary("bcdedit")
            if bcdedit is None:
                raise BootSwitchError("bcdedit not found")
            res = run([bcdedit, "/enum", "{fwbootmgr}"])
            if res.returncode == 0 and "fwbootmgr" in res.stdout.lower():
                return "uefi"
        except BootSwitchError:
            pass
        # Secondary signal that does not need admin rights.
        try:
            import winreg  # noqa: PLC0415

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control"
            ) as key:
                value, _ = winreg.QueryValueEx(key, "PEFirmwareType")
                return {1: "bios", 2: "uefi"}.get(value, "unknown")
        except Exception:
            return "unknown"

    if system == "Darwin":
        return "uefi"

    return "unknown"


def reboot_now() -> None:
    """Reboot the machine immediately."""
    if os.name == "nt":
        run([require_binary("shutdown"), "/r", "/t", "0"], check=True)
        return
    systemctl = resolve_binary("systemctl")
    if systemctl:
        run([systemctl, "reboot"], check=True)
    else:
        run([require_binary("reboot")], check=True)
