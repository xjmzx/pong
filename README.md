# pong

Ambient Pong screen lock for Ubuntu/X11. Two AI paddles auto-play across a 4×4 dashboard of clock, day, date, weather, network, and Google Calendar readouts. Authenticates against your real login password via PAM. Multi-monitor mirror.

This is a deterrent-level lock — somewhere between leaving the desktop unlocked and a hardened session lock. VT-switch and SSH still work; that is intentional.

## Install

Two paths: a `.deb` package (cleanest for Ubuntu/Debian), or a source install via the Makefile (for dev or non-deb systems).

### As a .deb package

Build the package once, then `apt install` it like any other system package:

```
make deb
sudo apt install ./dist/pong_0.2.0_all.deb
```

The `.deb` lands files at standard system paths (`/usr/bin/pong`, `/usr/share/applications/pong.desktop`, `/usr/share/icons/...`) and declares its runtime dependencies, so apt pulls in `python3-pam`, `python3-pygame`, and `python3-icalendar` automatically. `wireless-tools` and `python3-recurring-ical-events` are `Recommends` — the lock degrades gracefully without them (no SSID readout / no calendar chips).

To uninstall:

```
sudo apt remove pong
```

To bump the version for a release: edit `VERSION` at the top of the Makefile, or override per-build:

```
make deb VERSION=0.3.0
```

### From source

User-space install (`~/.local`):

```
make deps
make install PREFIX=$HOME/.local
```

`make deps` installs `python3-pam`, `python3-pygame`, `python3-icalendar` via apt and `recurring-ical-events` via pip --user. Then `make install` places the launcher at `$PREFIX/bin/pong` plus the `.desktop` entry and icon. Use `sudo make install` (no `PREFIX`) for a system-wide install under `/usr/local`.

The PAM binding comes from the Debian `python3-pam` package (module name `PAM`, capital letters) — not the PyPI `python-pam` library, which is a different thing.

## Run

Run the installed command:

```
pong
```

Or run directly from the repo without installing:

```
python3 pong_lock.py
```

Bind to a GNOME custom shortcut (e.g. `Ctrl+Alt+P`) for one-key activation. Point the shortcut command at the installed `pong` command (use the full path, e.g. `~/.local/bin/pong`) rather than a repo path — that way the shortcut keeps working if the repo is moved or renamed.

## Unlock

The lock screen shows a dim `[ Ctrl ][ Alt ][ SPACE ][ Alt ][ Ctrl ]` mini-keyboard hint at the center-bottom. **Press SPACE** to open the password prompt; type your password and Enter. Other keys are ignored.

The input times out after 8 seconds of inactivity — an auburn progress underline appears in the last 2 seconds as a warning. Each keystroke resets the timer.

## Dashboard

The screen is divided into a 4×4 grid with the clock in the centre 2×2:

```
┌─user@host─┬──── DAY · DATE ────┬─● online·SSID─┐
│           │                    │               │
│           │                    │               │
│           │       CLOCK        │               │
│           │      (2×2 cell)    │               │
│           │                    │               │
│           │                    │               │
├─CAL[0]────┼────TEMP────┬────SUN────┼─CAL[1]────┤
│ JOG       │   37°C     │ ↑05:14    │   TT      │
│ NEXT      │            │ ↓18:36    │   NEXT    │
│ Wed 18:30 │            │           │   ...     │
└───────────┴────────────┴───────────┴───────────┘
```

Top row: `user@host`, weekday + date, network status (with WiFi SSID if available). Bottom row: two Google Calendar slots, current temperature, sunrise/sunset. Calendar cells render with their Google-side colour as the frame outline + name text.

## Configuration

### Calendars (`~/.config/pong/calendars.json`)

An empty template is written automatically on first launch (`chmod 600`). Edit it to add Google Calendar private-iCal URLs:

```json
{
  "calendars": [
    {"name": "Jog", "url": "https://calendar.google.com/calendar/ical/.../basic.ics", "color": "pistachio"},
    {"name": "Work", "url": "https://calendar.google.com/calendar/ical/.../basic.ics", "color": "mango"}
  ]
}
```

Up to two calendars are surfaced — first entry → bottom-left cell, second → bottom-right. Both are visible at all times (their name always shows); when there's an upcoming event within 7 days, a `NEXT / time / summary` stack appears below the name.

**Getting the URL:** In Google Calendar (web), click the calendar in the left sidebar → **Settings and sharing** → scroll to **Integrate calendar** → copy **Secret address in iCal format**. Treat it as a password — anyone with the URL can read all events.

**Why ICS not OAuth:** Google revoked `gcalcli`'s default OAuth client and now requires app verification. The private ICS URL is the lower-friction read-only path.

**Colors:** Use Google Calendar's named palette (`mango`, `pistachio`, `tomato`, `flamingo`, `tangerine`, `pumpkin`, `banana`, `citron`, `avocado`, `basil`, `sage`, `eucalyptus`, `peacock`, `cobalt`, `blueberry`, `lavender`, `wisteria`, `amethyst`, `grape`, `graphite`, `birch`, `cocoa`) or a hex code (`"#7DBC58"`). Unknown names fall back to the default mauve frame.

### Weather (top of `pong_lock.py`)

- `WEATHER_LOCATION` — wttr.in location string (city name, airport code, or `~Lat,Lon`); empty string uses IP geolocation
- `WEATHER_REFRESH_SEC` — fetch cadence (default 30 min)

No API key needed. Powered by [wttr.in](https://wttr.in).

## Tunables (top of `pong_lock.py`)

Auth + input:
- `MAX_ATTEMPTS` — wrong passwords before cooloff (default 3)
- `COOLOFF_SECONDS` — lockout duration (default 15 min)
- `PAM_SERVICE` — `"login"` by default; switch to `"passwd"` or a custom `/etc/pam.d/pong` if `login` denies on your system
- `INPUT_TIMEOUT` — cancel the password prompt after this many idle seconds (default 8)
- `INPUT_WARN_SEC` — auburn underline appears at this many seconds remaining (default 2)
- `MAX_PASSWORD_LEN` — input buffer cap

Layout + typography:
- `CLOCK_FONT_SIZE`, `DAY_FONT_SIZE`, `DATE_FONT_SIZE`, `WEATHER_FONT_SIZE`, `SUN_FONT_SIZE`, `DASH_FONT_SIZE` — per-element font sizes
- `CLOCK_24H` — `True` for 24-hour clock, `False` for 12-hour
- `SANS_STACK` / `MONO_STACK` (in `main()`) — typeface fallback chain
- `PAL_*` constants — ndisc-suite fizx colour palette
- `HL_INSET`, `HL_RADIUS`, `HL_WIDTH` — group-highlight frame geometry
- `SUB_GRID_DIV` — sub-grid density (default 4 → 16×16 graph paper)

Network + calendar polling:
- `NET_REFRESH_SEC` — connectivity check cadence (default 10s)
- `CALENDAR_REFRESH_SEC` — ICS fetch cadence (default 10 min)
- `CALENDAR_LOOKAHEAD_DAYS` — only surface events within this window

Lockout state persists at `~/.cache/pong_lock_state` across Ctrl+C and re-launches. Single-instance lock at `~/.cache/pong_lock.lock` prevents a second invocation while pong is already running.

## Failsafe

`Ctrl+Alt+F3` (or any other unused F-key) switches to a TTY — log in there and kill or reboot. This is documented intentional bypass.

## Portability across Linux devices

All state and config sits under `~/.cache/pong_lock_*` and `~/.config/pong/` (which holds `calendars.json` and `theme.json`). To set up on a fresh Linux box:

1. `git clone` the repo (or copy the source)
2. `make deb && sudo apt install ./dist/pong_0.2.0_all.deb` — or `make deps && make install PREFIX=$HOME/.local` for a user-space install
3. Bind `pong` to a keyboard shortcut in your DE's settings (use the full path `/usr/bin/pong` for the .deb install, `~/.local/bin/pong` for the source install)
4. Launch pong once to auto-create the empty `calendars.json` + `theme.json` templates
5. Edit `~/.config/pong/calendars.json` with your ICS URLs + colours

Per-machine state (`~/.cache/pong_lock_*`) is not portable; per-user config (`~/.config/pong/*`) is.

## Limitations

- X11 only. Wayland will not work — relies on `xrandr` and an X11 keyboard grab.
- Multi-monitor mirror uses a single borderless SDL window sized to the bounding box of all `xrandr` rects, with the frame streamed to one texture and drawn per-monitor. Creating one top-level window per monitor does not work reliably on Mutter/X11.
- Window transparency (`Window.opacity`) is not honoured by Mutter X11 on full-coverage borderless windows; the lock is opaque by design.
