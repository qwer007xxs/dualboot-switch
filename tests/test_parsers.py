"""Parser tests using real-world output samples."""

import pytest

from dualboot_switch.backends import (
    parse_bcdedit_firmware,
    parse_bootctl_list,
    parse_efibootmgr,
    parse_grub_cfg,
)
from dualboot_switch.core import guess_os_family

# --------------------------------------------------------------------------- #

BCDEDIT_SAMPLE = """
Firmware Boot Manager
---------------------
identifier              {fwbootmgr}
displayorder            {bootmgr}
                        {6cf1a1a2-9f61-11ee-9a3b-806e6f6e6963}
                        {b2721d73-1db4-4c62-bf78-c548a880142d}
timeout                 2

Windows Boot Manager
--------------------
identifier              {bootmgr}
device                  partition=\\Device\\HarddiskVolume1
path                    \\EFI\\Microsoft\\Boot\\bootmgfw.efi
description             Windows Boot Manager
locale                  en-US
inherit                 {globalsettings}
default                 {current}
displayorder            {current}
timeout                 30

Firmware Application (101fffff)
-------------------------------
identifier              {6cf1a1a2-9f61-11ee-9a3b-806e6f6e6963}
device                  partition=\\Device\\HarddiskVolume1
path                    \\EFI\\ubuntu\\shimx64.efi
description             ubuntu

Firmware Application (101fffff)
-------------------------------
identifier              {b2721d73-1db4-4c62-bf78-c548a880142d}
description             UEFI: PXE IPv4 Realtek PCIe GBE
"""


def test_bcdedit_finds_all_bootable_entries():
    entries = parse_bcdedit_firmware(BCDEDIT_SAMPLE)
    labels = [e.label for e in entries]

    assert "Windows Boot Manager" in labels
    assert "ubuntu" in labels
    # {fwbootmgr} is a container, never a target
    assert all(e.id != "{fwbootmgr}" for e in entries)


def test_bcdedit_reads_description_not_locale():
    entries = parse_bcdedit_firmware(BCDEDIT_SAMPLE)
    win = next(e for e in entries if e.id == "{bootmgr}")
    assert win.label == "Windows Boot Manager"
    assert win.os_family == "windows"


def test_bcdedit_marks_firmware_default():
    entries = parse_bcdedit_firmware(BCDEDIT_SAMPLE)
    assert next(e for e in entries if e.id == "{bootmgr}").is_default


def test_bcdedit_handles_localised_key_names():
    """Non-English Windows translates the key names but not the shapes."""
    localised = """
Firmware Application (101fffff)
-------------------------------
Bezeichner              {6cf1a1a2-9f61-11ee-9a3b-806e6f6e6963}
Gerät                   partition=\\Device\\HarddiskVolume1
Pfad                    \\EFI\\ubuntu\\shimx64.efi
Beschreibung            ubuntu
"""
    entries = parse_bcdedit_firmware(localised)
    assert len(entries) == 1
    assert entries[0].label == "ubuntu"
    assert entries[0].os_family == "linux"


# --------------------------------------------------------------------------- #

EFIBOOTMGR_SAMPLE = """BootCurrent: 0001
Timeout: 1 seconds
BootOrder: 0001,0000,0003
Boot0000* Windows Boot Manager\tHD(1,GPT,8f9c,0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)
Boot0001* ubuntu\tHD(1,GPT,8f9c,0x800,0x32000)/File(\\EFI\\ubuntu\\shimx64.efi)
Boot0003  UEFI: Built-in EFI Shell\tVenMedia(5023b95c)
"""


def test_efibootmgr_parses_entries_and_flags():
    entries = parse_efibootmgr(EFIBOOTMGR_SAMPLE)
    assert [e.id for e in entries] == ["0000", "0001", "0003"]

    ubuntu = next(e for e in entries if e.id == "0001")
    assert ubuntu.label == "ubuntu"
    assert ubuntu.is_current
    assert ubuntu.is_default          # first in BootOrder

    win = next(e for e in entries if e.id == "0000")
    assert win.label == "Windows Boot Manager"
    assert win.os_family == "windows"
    assert not win.is_current


def test_efibootmgr_marks_inactive_entries():
    entries = parse_efibootmgr(EFIBOOTMGR_SAMPLE)
    shell = next(e for e in entries if e.id == "0003")
    assert shell.detail.startswith("(inactive)")


def test_efibootmgr_verbose_without_tabs():
    """Some builds separate the label and device path with spaces only."""
    sample = (
        "BootCurrent: 0000\n"
        "BootOrder: 0000\n"
        "Boot0000* Fedora HD(1,GPT,abc,0x800,0x32000)/File(\\EFI\\fedora\\shim.efi)\n"
    )
    entries = parse_efibootmgr(sample)
    assert entries[0].label == "Fedora"


# --------------------------------------------------------------------------- #

BOOTCTL_SAMPLE = """Boot Loader Entries:
        title: Arch Linux (default) (selected)
           id: arch.conf
       source: /boot/loader/entries/arch.conf
      version: 6.6.10
        linux: /vmlinuz-linux

        title: Windows Boot Manager
           id: auto-windows
       source: /sys/firmware/efi/efivars/LoaderEntries-4a67b082
"""


def test_bootctl_parses_entries():
    entries = parse_bootctl_list(BOOTCTL_SAMPLE)
    assert [e.id for e in entries] == ["arch.conf", "auto-windows"]

    arch = entries[0]
    assert arch.label == "Arch Linux"      # markers stripped
    assert arch.is_default and arch.is_current
    assert arch.os_family == "linux"

    assert entries[1].os_family == "windows"


# --------------------------------------------------------------------------- #

GRUB_SAMPLE = """
menuentry 'Ubuntu' --class ubuntu --class gnu-linux $menuentry_id_option 'gnulinux-simple-abc' {
    linux /vmlinuz root=UUID=abc
}
submenu 'Advanced options for Ubuntu' $menuentry_id_option 'gnulinux-advanced-abc' {
    menuentry 'Ubuntu, with Linux 6.5.0-14-generic' --class ubuntu {
        linux /vmlinuz-6.5.0-14-generic
    }
    menuentry 'Ubuntu, with Linux 6.5.0-14-generic (recovery mode)' {
        linux /vmlinuz-6.5.0-14-generic
    }
}
menuentry 'Windows Boot Manager (on /dev/nvme0n1p1)' --class windows {
    chainloader /EFI/Microsoft/Boot/bootmgfw.efi
}
"""


def test_grub_parses_top_level_entries():
    entries = parse_grub_cfg(GRUB_SAMPLE)
    labels = [e.label for e in entries]
    assert "Ubuntu" in labels
    assert "Windows Boot Manager (on /dev/nvme0n1p1)" in labels


def test_grub_builds_submenu_paths():
    entries = parse_grub_cfg(GRUB_SAMPLE)
    nested = next(e for e in entries if "6.5.0-14-generic" in e.label
                  and "recovery" not in e.label)
    assert nested.id == (
        "Advanced options for Ubuntu>Ubuntu, with Linux 6.5.0-14-generic"
    )


def test_grub_leaves_submenu_after_closing_brace():
    """The entry after a submenu block must be top-level again."""
    entries = parse_grub_cfg(GRUB_SAMPLE)
    win = next(e for e in entries if e.os_family == "windows")
    assert ">" not in win.id


# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "label,expected",
    [
        ("Windows Boot Manager", "windows"),
        ("ubuntu", "linux"),
        ("Pop!_OS", "linux"),
        ("Fedora Linux", "linux"),
        ("UEFI: PXE IPv4 Realtek", "network"),
        ("Mac OS X", "macos"),
        ("Some Vendor Thing", "unknown"),
    ],
)
def test_os_family_detection(label, expected):
    assert guess_os_family(label) == expected
