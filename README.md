# pong-lock

Ambient Pong screen lock for Ubuntu/X11. Two AI paddles auto-play on a black background while a password prompt sits in the middle. Authenticates against your real login password via PAM. Multi-monitor mirror.

This is a deterrent-level lock — somewhere between leaving the desktop unlocked and a hardened session lock. VT-switch and SSH still work; that is intentional.

## Install

```
sudo apt install python3-pygame python3-pam
```

The PAM binding comes from the Debian `python3-pam` package (module name `PAM`, capital letters) — not the PyPI `python-pam` library, which is a different thing.

## Run

```
python3 pong_lock.py
```

Bind to a GNOME custom shortcut (e.g. `Ctrl+Alt+P`) for one-key activation.

## Tunables

Top of `pong_lock.py`:

- `MAX_ATTEMPTS` — wrong passwords before cooloff (default 3)
- `COOLOFF_SECONDS` — lockout duration (default 15 min)
- `PAM_SERVICE` — `"login"` by default; switch to `"passwd"` or a custom `/etc/pam.d/pong-lock` if `login` denies on your system
- `INPUT_TIMEOUT` — cancel the password prompt after this many idle seconds
- `MAX_PASSWORD_LEN` — input buffer cap
- Ball / paddle physics constants

Lockout state persists at `~/.cache/pong_lock_state` across Ctrl+C and re-launches.

## Failsafe

`Ctrl+Alt+F3` (or any other unused F-key) switches to a TTY — log in there and kill or reboot. This is documented intentional bypass.

## Limitations

- X11 only. Wayland will not work — relies on `xrandr` and an X11 keyboard grab.
- Multi-monitor mirror uses a single borderless SDL window sized to the bounding box of all `xrandr` rects, with the frame streamed to one texture and drawn per-monitor. Creating one top-level window per monitor does not work reliably on Mutter/X11.
