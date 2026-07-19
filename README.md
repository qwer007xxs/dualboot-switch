# dualboot-switch

Reboot straight into your other OS — one click, no F12 mashing, no BIOS menu.

Pick the target from a small GUI, hit reboot, and the machine comes up in the
other OS **once**. Your normal default boot order is never modified.

Runs on **Windows** and **Linux** from the same codebase. Pure Python standard
library — no pip dependencies.

```
┌─────────────────────────────────────────┐
│  Dual Boot Switch                       │
│  Linux · firmware: UEFI · using:        │
│  efibootmgr                             │
├─────────────────────────────────────────┤
│  🪟  Windows Boot Manager               │
│      default                            │
│                                         │
│  🐧  ubuntu                             │
│      running now                        │
│                                         │
│  💾  UEFI: Built-in EFI Shell           │
├─────────────────────────────────────────┤
│  Refresh          Reboot into Windows…  │
└─────────────────────────────────────────┘
```

## How it works

The tool auto-detects your firmware type and available tooling, then picks a
backend. Preference order — firmware-level mechanisms first, because those are
the only ones that reliably reach *another operating system* rather than
another kernel on the same one.

| Backend | Platform | Lists via | One-shot via |
|---|---|---|---|
| `bcdedit` | Windows, UEFI | `bcdedit /enum firmware` | `bcdedit /set {fwbootmgr} bootsequence {GUID}` |
| `efibootmgr` | Linux, UEFI | `efibootmgr` | `efibootmgr --bootnext XXXX` |
| `systemd-boot` | Linux | `bootctl list` | `bootctl set-oneshot entry.conf` |
| `grub` | Linux | parses `grub.cfg` | `grub-reboot "Entry title"` |

Every one of these is a **one-shot** mechanism. Nothing here writes your
persistent boot order, so a normal restart later behaves exactly as before.

## Install

Requires Python 3.8+ with Tk (bundled on Windows and macOS; on Linux install
`python3-tk` / `python3-tkinter`).

```bash
git clone https://github.com/qwer007xxs/dualboot-switch.git
cd dualboot-switch
pip install -e .
```

Or run it straight from the source tree with no install at all:

```bash
python -m dualboot_switch
```

## Usage

### GUI

```bash
dualboot-switch            # opens the window
```

Launch it normally. If it is not elevated, the window still lists everything
and shows a **Restart elevated** button that re-launches via UAC on Windows or
`pkexec` on Linux.

### Command line

```bash
dualboot-switch list                    # what can this machine boot?
dualboot-switch list -v                 # include device paths

dualboot-switch boot windows            # arm the next boot, restart later
dualboot-switch boot windows --reboot   # arm it and restart now
dualboot-switch boot 0000 -r -y         # by id, no confirmation prompt

dualboot-switch --backend grub list     # force a specific backend
```

`boot` accepts an entry id, an exact label, any unambiguous substring of a
label, or an OS family (`windows`, `linux`, `macos`).

### Making it a real shortcut

**Windows** — create a shortcut to:

```
pythonw.exe -m dualboot_switch boot linux --reboot --yes
```

Then Properties → Advanced → **Run as administrator**.

**Linux** — drop a `.desktop` file in `~/.local/share/applications/`:

```ini
[Desktop Entry]
Type=Application
Name=Reboot to Windows
Exec=pkexec dualboot-switch boot windows --reboot --yes
Icon=system-reboot
Terminal=false
```

To skip the password prompt entirely, add a polkit rule or a narrowly scoped
sudoers line — see [`docs/passwordless.md`](docs/passwordless.md).

## Requirements per platform

**Windows**

- UEFI firmware (not legacy BIOS/CSM)
- Administrator rights to change the boot target
- `bcdedit` — ships with Windows

**Linux**

- `efibootmgr` (`apt install efibootmgr`, `dnf install efibootmgr`) — recommended
- or `bootctl` if you use systemd-boot
- or `grub-reboot` **plus** `GRUB_DEFAULT=saved` in `/etc/default/grub`,
  followed by `sudo update-grub`
- root rights to change the boot target

## Security

The tool runs elevated, so it resolves helper binaries only from root-owned
system directories, validates every boot entry id before it reaches a command,
and never invokes a shell. Details, plus the threat model, are in
[SECURITY.md](SECURITY.md).

One thing worth calling out here: if you set up passwordless sudo for this,
**do not use a wildcard**. `NOPASSWD: /usr/bin/efibootmgr --bootnext *` also
permits `efibootmgr --create --loader ...`, which is a root-level persistence
primitive. [docs/passwordless.md](docs/passwordless.md) explains why and gives
two safe configurations.

## Caveats

- **Legacy BIOS/MBR** has no firmware-level one-shot boot. Only the GRUB
  backend can help there.
- **BitLocker**: changing UEFI boot variables can trigger a recovery-key
  prompt on some machines. Have your key handy the first time.
- **Fast Startup** on Windows can leave the Windows partition in a hibernated
  state that Linux refuses to mount read-write. Unrelated to this tool, but
  worth disabling on a dual-boot machine.
- Some vendor firmwares ignore `BootNext`. If a reboot lands you back in the
  same OS, that is a firmware bug, not this tool. Try the GRUB backend instead.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The parsers for all four backends are tested against captured real-world
output, so you can extend them without a dual-boot machine to hand.

## License

MIT — see [LICENSE](LICENSE).
