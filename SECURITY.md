# Security notes

This tool runs elevated and writes firmware boot variables, so it is worth
being explicit about what it does and does not do.

## Threat model

The tool assumes the local user is trusted to reboot their own machine. It is
designed so that *being installed* does not widen an attacker's options
compared to a machine without it. Specifically, a non-root local process should
not be able to use this tool to gain root or to gain persistence.

## Design decisions

**No shell, anywhere.** Every subprocess call passes an argument list, never a
command string, and `shell=True` appears nowhere in the codebase. Boot entry
labels routinely contain quotes, parentheses and slashes; none of it is ever
interpreted.

**Binaries resolve to trusted directories only.** `resolve_binary()` looks in
`/usr/sbin`, `/sbin`, `/usr/bin`, `/bin` on Unix, and `%SystemRoot%\System32`
on Windows -- never a bare `PATH` lookup. This matters because the process runs
elevated: on Windows, `CreateProcess` searches the current working directory
before the system directory, so a `bcdedit.exe` dropped into a user-writable
folder would otherwise run as Administrator. An unresolvable name is treated as
missing rather than trusted.

**Entry ids are validated before they reach a command.** The helper tools we
call can do considerably more than set a one-shot target -- `efibootmgr
--create` registers a permanent boot entry pointing at an arbitrary EFI binary
-- so ids are checked against the shape each backend expects before use:

| Backend | Accepted id |
|---|---|
| `bcdedit` | `{GUID}` or `{name}` |
| `efibootmgr` | exactly four hex digits |
| `systemd-boot` | word characters, dot, plus, hyphen; not leading `-` |
| `grub` | must match an entry re-read from `grub.cfg`; not leading `-` |

**Elevation passes arguments safely.** The Windows relaunch path builds its
command line with `subprocess.list2cmdline`, which applies the quoting rules
`CommandLineToArgvW` expects, so an argument containing a quote cannot append
extra arguments to the elevated process.

**Nothing persistent is written.** Every backend uses a one-shot mechanism
(`bootsequence`, `BootNext`, `set-oneshot`, `grub-reboot`). The tool never
changes the default boot order, and it writes no config files of its own.

**No network access and no credentials.** The tool makes no outbound
connections and reads no secrets. It has no update mechanism.

## The genuinely risky part is the setup you choose

The largest real-world risk is not the code, it is a passwordless-sudo rule
copied from a forum post. `NOPASSWD: /usr/bin/efibootmgr --bootnext *` grants
far more than it appears to, because a sudoers `*` matches across spaces. See
[docs/passwordless.md](docs/passwordless.md) for why, and for two configurations
that do not have that problem.

## Known limitations

- The tool trusts the output of `bcdedit`, `efibootmgr`, `bootctl` and
  `grub.cfg`. All of these are root-owned; if an attacker can write them, they
  already have root.
- Changing UEFI boot variables can trigger a BitLocker recovery prompt. That is
  the firmware doing its job, not a fault, but keep your recovery key available.
- Some vendor firmwares silently ignore `BootNext`. A failed switch is a
  firmware bug rather than a security issue, but it does mean you should not
  rely on this tool as the only way to reach a given OS.

## Reporting

Open an issue, or for anything you would rather not post publicly, use GitHub's
private vulnerability reporting on the Security tab.
