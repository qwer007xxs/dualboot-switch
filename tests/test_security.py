"""Regression tests for the hardening described in SECURITY.md.

These all assert on *refusal*: each one constructs the malicious input that
would otherwise reach a privileged command, and checks we stop first.
"""

import pathlib
import re
import subprocess

import pytest

from dualboot_switch import backends
from dualboot_switch.backends import (
    EfibootmgrBackend,
    GrubBackend,
    SystemdBootBackend,
    WindowsFirmwareBackend,
)
from dualboot_switch.core import BootEntry, BootSwitchError, resolve_binary

SRC = pathlib.Path(__file__).resolve().parent.parent / "dualboot_switch"


@pytest.fixture
def elevated(monkeypatch):
    """Pretend we are root so validation, not the privilege check, is hit."""
    monkeypatch.setattr(backends, "is_elevated", lambda: True)


@pytest.fixture
def no_exec(monkeypatch):
    """Fail loudly if anything actually tries to run a command."""
    def boom(*args, **kwargs):
        raise AssertionError(f"a command was executed: {args!r}")

    monkeypatch.setattr(backends, "run", boom)
    monkeypatch.setattr(backends, "require_binary", lambda name: "/bin/true")


# --------------------------------------------------------------------------- #
# Entry id validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "bad_id",
    [
        "--create",                       # would register a new boot entry
        "0000 --create --loader \\x.efi",  # smuggled extra arguments
        "-n",
        "00000",                          # five digits
        "zzzz",
        "",
    ],
)
def test_efibootmgr_refuses_ids_that_are_not_boot_numbers(bad_id, elevated, no_exec):
    """efibootmgr --create can point a boot entry at an arbitrary EFI binary."""
    with pytest.raises(BootSwitchError, match="refusing to use boot entry id"):
        EfibootmgrBackend().set_next_boot(BootEntry(id=bad_id, label="x"))


def test_efibootmgr_accepts_a_real_boot_number(elevated, monkeypatch):
    seen = {}
    monkeypatch.setattr(backends, "require_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(backends, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd))
    EfibootmgrBackend().set_next_boot(BootEntry(id="0A1F", label="ubuntu"))
    assert seen["cmd"] == ["/usr/bin/efibootmgr", "--bootnext", "0A1F"]


@pytest.mark.parametrize("bad_id", ["notaguid", "/set", "{bad", ""])
def test_bcdedit_refuses_malformed_identifiers(bad_id, elevated, no_exec):
    with pytest.raises(BootSwitchError, match="refusing to use boot entry id"):
        WindowsFirmwareBackend().set_next_boot(BootEntry(id=bad_id, label="x"))


@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "/boot/loader/entries/x.conf", "-h", "a b", "x;y"],
)
def test_systemd_boot_refuses_paths_and_options(bad_id, elevated, no_exec):
    with pytest.raises(BootSwitchError, match="refusing to use boot entry id"):
        SystemdBootBackend().set_next_boot(BootEntry(id=bad_id, label="x"))


def test_grub_refuses_an_entry_it_did_not_read_from_grub_cfg(elevated, monkeypatch):
    monkeypatch.setattr(GrubBackend, "_tool", classmethod(lambda cls: "/usr/sbin/grub-reboot"))
    monkeypatch.setattr(
        GrubBackend, "list_entries",
        lambda self: [BootEntry(id="Ubuntu", label="Ubuntu")],
    )
    monkeypatch.setattr(backends, "run", lambda *a, **k: pytest.fail("executed"))

    with pytest.raises(BootSwitchError, match="no such entry in grub.cfg"):
        GrubBackend().set_next_boot(BootEntry(id="Windows", label="Windows"))


# --------------------------------------------------------------------------- #
# Privilege checks come before anything is executed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "backend_cls", [EfibootmgrBackend, SystemdBootBackend, GrubBackend, WindowsFirmwareBackend]
)
def test_unprivileged_callers_are_refused_before_execution(backend_cls, monkeypatch):
    monkeypatch.setattr(backends, "is_elevated", lambda: False)
    monkeypatch.setattr(backends, "run", lambda *a, **k: pytest.fail("executed"))
    with pytest.raises(BootSwitchError, match="(?i)require"):
        backend_cls().set_next_boot(BootEntry(id="0000", label="x"))


# --------------------------------------------------------------------------- #
# Binary resolution
# --------------------------------------------------------------------------- #

def test_resolve_binary_never_returns_a_relative_path():
    for name in ("sh", "efibootmgr", "bcdedit", "bootctl", "definitely-not-real"):
        found = resolve_binary(name)
        assert found is None or pathlib.Path(found).is_absolute()


def test_resolve_binary_ignores_path(monkeypatch, tmp_path):
    """A binary planted on PATH must not be picked up."""
    fake = tmp_path / "efibootmgr"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_binary("efibootmgr") != str(fake)


def test_source_never_falls_back_to_bare_path_lookup():
    """which() is unsafe for elevated execution; backends must not use it."""
    text = (SRC / "backends.py").read_text()
    assert "which(" not in text


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #

def test_no_shell_execution_anywhere():
    for path in SRC.glob("*.py"):
        assert "shell=True" not in path.read_text(), path.name


def test_no_string_commands_passed_to_run():
    """Every run()/Popen() call site must pass a list, not a string."""
    for path in SRC.glob("*.py"):
        for call in re.findall(r"(?:run|Popen)\(\s*(.)", path.read_text()):
            assert call in "[*c", f"{path.name}: non-list command argument"


def _command_line_to_argv(cmdline: str):
    """A reference implementation of the CommandLineToArgvW rules."""
    args, cur, i, in_quotes = [], "", 0, False
    n = len(cmdline)
    while i < n:
        ch = cmdline[i]
        if ch == "\\":
            j = i
            while j < n and cmdline[j] == "\\":
                j += 1
            slashes = j - i
            if j < n and cmdline[j] == '"':
                cur += "\\" * (slashes // 2)
                if slashes % 2:
                    cur += '"'          # escaped quote: literal, stays in arg
                else:
                    in_quotes = not in_quotes
                i = j + 1
            else:
                cur += "\\" * slashes
                i = j
        elif ch == '"':
            in_quotes = not in_quotes
            i += 1
        elif ch == " " and not in_quotes:
            args.append(cur)
            cur = ""
            while i < n and cmdline[i] == " ":
                i += 1
        else:
            cur += ch
            i += 1
    args.append(cur)
    return args


def test_elevation_quoting_cannot_smuggle_extra_arguments():
    """A quote inside an argument must not split it into more arguments.

    The elevated process re-parses our command line with CommandLineToArgvW,
    so anything that survives as a separate argv entry is an argument we did
    not intend to pass to a process running as Administrator.
    """
    hostile = 'x" --backend grub boot windows -r -y "'
    argv = ["app.py", hostile]

    parsed = _command_line_to_argv(subprocess.list2cmdline(argv))
    assert parsed == argv

    # The hand-rolled quoting this replaced does not survive the round trip:
    # the argument splits apart and the smuggled flags become real arguments.
    naive = " ".join(f'"{a}"' for a in argv)
    naive_parsed = _command_line_to_argv(naive)
    assert naive_parsed != argv
    assert "--backend" in naive_parsed


# --------------------------------------------------------------------------- #
# Elevation relaunch target (regression: installed entry point must re-run)
# --------------------------------------------------------------------------- #

from dualboot_switch.core import elevation_target  # noqa: E402


def test_elevation_target_for_python_source(monkeypatch, tmp_path):
    script = tmp_path / "run.py"
    script.write_text("")
    monkeypatch.setattr("sys.argv", [str(script), "boot", "windows"])
    monkeypatch.setattr("sys.executable", "/usr/bin/python3")
    monkeypatch.delattr("sys.frozen", raising=False)
    target = elevation_target(["boot", "windows"])
    assert target == ["/usr/bin/python3", str(script), "boot", "windows"]


def test_elevation_target_for_installed_entry_point(monkeypatch, tmp_path):
    """argv[0] is a wrapper exe, NOT a .py file — must re-run it directly.

    The old code produced [python, wrapper.exe, ...], which fails because the
    interpreter cannot execute a wrapper binary as a script.
    """
    wrapper = tmp_path / "dualboot-switch.exe"
    wrapper.write_bytes(b"MZ")
    monkeypatch.setattr("sys.argv", [str(wrapper), "boot", "linux"])
    monkeypatch.setattr("sys.executable", "C:\\Python\\python.exe")
    monkeypatch.delattr("sys.frozen", raising=False)
    target = elevation_target(["boot", "linux"])
    assert target == [str(wrapper), "boot", "linux"]
    assert "python" not in target[0].lower()


def test_elevation_target_for_frozen_app(monkeypatch, tmp_path):
    exe = tmp_path / "app.bin"
    exe.write_bytes(b"")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(exe))
    monkeypatch.setattr("sys.argv", [str(exe), "gui"])
    target = elevation_target(["gui"])
    assert target == [str(exe), "gui"]


def test_elevation_target_falls_back_to_dash_m(monkeypatch):
    """If argv[0] points nowhere real, re-run the package via -m."""
    monkeypatch.setattr("sys.argv", ["/nonexistent/ghost", "list"])
    monkeypatch.setattr("sys.executable", "/usr/bin/python3")
    monkeypatch.delattr("sys.frozen", raising=False)
    target = elevation_target(["list"])
    assert target == ["/usr/bin/python3", "-m", "dualboot_switch", "list"]
