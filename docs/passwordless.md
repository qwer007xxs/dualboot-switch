# Running without a password prompt

By default the Linux GUI asks for your password through `pkexec` every time.
You can remove that prompt, but the obvious way to do it opens a root hole, so
read this section before copying anything.

## The trap: wildcards in sudoers

You will find advice like this in a lot of forum posts. **Do not use it:**

```
# DANGEROUS - do not copy
your_username ALL=(root) NOPASSWD: /usr/bin/efibootmgr --bootnext *
```

A `*` in a sudoers command matches spaces, so that single line also permits:

```bash
sudo efibootmgr --bootnext 0000 --create --loader '\evil.efi' --disk /dev/sda
```

`--create` registers a new UEFI boot entry pointing at an EFI binary of the
attacker's choosing. That is arbitrary code execution before the operating
system starts, surviving a reinstall of the OS, and it required no password.
The same trap applies to `bootctl` and `grub-reboot`, both of which can also
write persistent state.

## Option A - sudoers, one line per target

Enumerate the exact commands instead. Run `dualboot-switch list` to get your
boot numbers, then `sudo visudo -f /etc/sudoers.d/dualboot-switch`:

```
your_username ALL=(root) NOPASSWD: /usr/bin/efibootmgr --bootnext 0000
your_username ALL=(root) NOPASSWD: /usr/bin/efibootmgr --bootnext 0001
your_username ALL=(root) NOPASSWD: /usr/bin/systemctl reboot
```

No wildcards, so nothing beyond those three exact command lines is permitted.
Confirm the paths with `which efibootmgr systemctl` first -- sudoers matches on
the absolute path, and it differs across distributions.

Boot numbers are stable until you add or remove an OS. If they change, update
this file.

## Option B - polkit rule

```javascript
// /etc/polkit-1/rules.d/49-dualboot-switch.rules
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.policykit.exec" &&
        action.lookup("program") == "/usr/bin/dualboot-switch" &&
        subject.isInGroup("wheel")) {
        return polkit.Result.YES;
    }
});
```

Replace `wheel` with `sudo` on Debian and Ubuntu.

**Check who can write that path before you enable this.** The rule grants
passwordless root to whatever sits at that exact path, so if the path is
writable by a non-root user, any such user gets root:

```bash
namei -l "$(which dualboot-switch)"
```

Every component must be root-owned and not group- or world-writable. This is
why the path above is `/usr/bin` and not `/usr/local/bin`: `/usr/local` is
group-writable on some distributions. A `pip install --user` script lives under
your home directory and is therefore never a safe target for this rule -- if
that is where your copy landed, use Option A instead.

## The trade-off either way

Both options let any process running as your user reboot the machine into
another OS without a prompt. On a single-user desktop that is a reasonable
trade. On a shared or managed machine, leave the password prompt in place.
