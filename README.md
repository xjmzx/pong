# pong

Ambient Pong screen lock for Ubuntu/X11. Two AI paddles auto-play across a 4×4 dashboard of clock, day, date, weather, sunrise/sunset, and Google Calendar readouts. Authenticates against your real login password via PAM. Dual fizx/upleb theme that alternates on each launch. Multi-monitor mirror.

This is a deterrent-level lock — somewhere between leaving the desktop unlocked and a hardened session lock. VT-switch and SSH still work; that is intentional.

The same binary doubles as a standalone dashboard: `pong --dashboard` renders the dashboard in a regular resizable window with no lock, no keyboard grab, and no PAM dependency. See [Dashboard mode](#dashboard-mode).

## Install

Two paths: a `.deb` package (cleanest for Ubuntu/Debian), or a source install via the Makefile (for dev or non-deb systems).

### As a .deb package

Build the package once, then `apt install` it like any other system package:

```
make deb
sudo apt install ./dist/pong_0.3.0_all.deb
```

The `.deb` lands files at standard system paths (`/usr/bin/pong`, `/usr/share/applications/pong.desktop`, `/usr/share/icons/...`) and declares its runtime dependencies, so apt pulls in `python3-pam`, `python3-pygame`, and `python3-icalendar` automatically. `python3-recurring-ical-events` is a `Recommends` — the lock degrades gracefully without it (no calendar chips, everything else still works).

To uninstall:

```
sudo apt remove pong
```

To bump the version for a release: edit `VERSION` at the top of the Makefile, or override per-build:

```
make deb VERSION=0.3.1
```

### From source

User-space install (`~/.local`):

```
make deps
make install PREFIX=$HOME/.local
```

`make deps` installs `python3-pam`, `python3-pygame`, `python3-icalendar` via apt and `recurring-ical-events` via pip --user. Then `make install` places the launcher at `$PREFIX/bin/pong` plus the `.desktop` entry and icon. Use `sudo make install` (no `PREFIX`) for a system-wide install under `/usr/local`.

The PAM binding comes from the Debian `python3-pam` package (module name `PAM`, capital letters) — not the PyPI `python-pam` library, which is a different thing.

### macOS (dashboard app)

The lock is Linux/X11/PAM-only, but **dashboard mode is cross-platform** — it's plain pygame plus network calls (wttr.in, ICS), with none of the lock's `xrandr`/PAM/keyboard-grab machinery. macOS can run it two ways:

**Run from source** (quickest):

```
python3 -m venv .venv
.venv/bin/pip install pygame icalendar recurring-ical-events
.venv/bin/python pong_lock.py --dashboard
```

**Build a double-clickable `Pong Dashboard.app`** (the macOS equivalent of the Windows app), via [PyInstaller](https://pyinstaller.org):

```
brew install librsvg     # one-time: needed only for the icon step
make mac-venv            # once: .venv with pygame + pyinstaller
make app                 # -> dist/Pong Dashboard.app
open "dist/Pong Dashboard.app"   # or drag it into /Applications
```

The bundle is self-contained (~60 MB) — Python, pygame, and the calendar libs are all inside it, so it runs on a Mac with no Python or deps installed. It launches straight into dashboard mode (lock mode is never bundled).

The bundle is **ad-hoc signed, not notarized.** Running it locally is fine, but if you copy/zip/AirDrop it to another Mac, Gatekeeper will block the first launch — right-click → **Open** (then **Open** again in the dialog), or strip quarantine with `xattr -dr com.apple.quarantine "Pong Dashboard.app"`. The packaging spec + entry point live in [`packaging/macos/`](packaging/macos/); `PONG_DASH_SIZE=960x540` overrides the initial window size. Built on Apple Silicon the `.app` is arm64-only; run `make app` on an Intel Mac (or under Rosetta) for an x86_64 build.

### Windows (dashboard .exe)

The same dashboard runs on Windows. From source: `pip install pygame icalendar recurring-ical-events` then `python pong_lock.py --dashboard`. To build a single-file **Pong Dashboard.exe** (windowed, self-contained), on a Windows machine with PyInstaller:

```
pip install pygame icalendar recurring-ical-events pyinstaller
pyinstaller --noconfirm packaging\windows\pong-dashboard-win.spec
```

The spec lives in [`packaging/windows/`](packaging/windows/) and shares the `--dashboard` entry point with the macOS build. Unsigned, so Windows SmartScreen may warn on first run — click **More info → Run anyway**.

### Releases (CI)

[`.github/workflows/release.yml`](.github/workflows/release.yml) builds all three assets (macOS arm64 + x86_64 `.dmg`, Windows `.exe`) and attaches them to a GitHub Release when a `v*` tag is pushed:

```
git tag v0.4.0 && git push origin v0.4.0
```

The tag drives the version baked into the artifact filenames and the macOS bundle's `Info.plist`. Run the workflow manually (Actions → *Release dashboard apps* → *Run workflow*) to dry-run the builds without cutting a release.

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

## Dashboard mode

Pass `--dashboard` to run the same dashboard as an ordinary windowed app — useful when you just want to glance at the clock, calendar chips, and weather without locking the screen:

```
pong --dashboard
```

The `.deb` install also ships a separate "Pong Dashboard" entry in *Show Applications*, so the dashboard can be launched from the app grid like any other app. The window is resizable (640×360 minimum), has a normal title bar, and quits on **Esc**, **Q**, or close. PAM is not used and `python3-pam` is not required for this mode. A separate single-instance lock at `~/.cache/pong_dash.lock` means a dashboard window can coexist with the lock variant if you ever want both running.

The bottom-row input strip is rendered as an empty reserved tile in dashboard mode (no SPACE wake hint, no password prompt). Everything else — clock, day/date, calendars, weather, identity chip — renders exactly as in lock mode, including the dual fizx/upleb theme alternation.

## Unlock

The lock screen shows a dim `[ Ctrl ][ Alt ][ SPACE ][ Alt ][ Ctrl ]` mini-keyboard hint at the center-bottom. **Press SPACE** to open the password prompt; type your password and Enter. Other keys are ignored.

The input times out after 8 seconds of inactivity — an auburn progress underline appears in the last 2 seconds as a warning. Each keystroke resets the timer.

## Dashboard

The screen is divided into a 4×4 grid with the clock in the centre 2×2 and the data chips stacked in the left column:

```
┌─JOG────┬──── MON 08 JUN ────┬─empty──┐
│ NEXT   │                    │        │
│ Wed... │                    │        │
├─TT─────┤                    ├────────┤
│ NEXT   │       CLOCK        │        │
│ Tue... │      (2×2 cell)    │ empty  │
├─37°C───┤                    ├────────┤
│ HANOI  │                    │        │
│ 05:14  │                    │        │
│ 18:36  │                    │        │
│ wttr.in│                    │        │
├─host───┼──── input strip ───┼────────┤
│ design │   (SPACE / pwd)    │        │
└────────┴────────────────────┴────────┘
```

**Left column (top → bottom):**
- (0,0) Jog (cal[0]) — calendar name + NEXT event, pistachio tint backdrop
- (0,1) TT (cal[1]) — calendar name + NEXT event, mango tint backdrop
- (0,2) Weather composite — temp, city, sunrise, sunset, source (wttr.in)
- (0,3) Identity — `user@host` plus a design profile line (`FIZX · #ACCENT · #MAUVE · #AUBURN`)

**Centre column:**
- (1–2, 0) DAY DATE — unified mono-bold line, e.g. `MON 08 JUN`
- (1–2, 1–2) CLOCK 2×2 — focal time digit
- (1–2, 3) Input strip — SPACE wake-hint, password prompt, warning bar, feedback

**Right column:** intentionally empty — reserved for future ambient data.

Calendar cells render with their Google-side colour as both a 15%-alpha backdrop and the name text. The design profile line shows which theme is active and the three signature hexes (accent, mauve, auburn) that vary across themes.

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

Up to two calendars are surfaced — first entry → cell (0,0), second → cell (0,1) (top of the left column). Both are visible at all times (their name always shows); when there's an upcoming event within 7 days, a `NEXT / time / summary` stack appears below the name.

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
- `CLOCK_FONT_SIZE` — centre clock (default 260, mono bold)
- `DAYLINE_FONT_SIZE` — "MON 08 JUN" line above the clock (default 120, mono bold)
- `UI_FONT_SIZE` — unified Ubuntu size for every perimeter chip (default 14, regular)
- `CLOCK_24H` — `True` for 24-hour clock, `False` for 12-hour
- `SANS_STACK` / `MONO_STACK` / `UBUNTU_STACK` (in `main()`) — typeface fallback chains
- `PAL_NET`, `PAL_GRID_MAIN`, `PAL_GRID_SUB`, `PAL_KB_DIM` — theme-invariant structural tones
- `THEMES["fizx"|"upleb"]` — the two palette dicts mirroring `ndisc.smpl/src/index.css`
- `HL_INSET`, `HL_RADIUS`, `HL_WIDTH` — group-highlight frame geometry
- `SUB_GRID_DIV` — sub-grid density (default 4 → 16×16 graph paper)

Polling cadence:
- `WEATHER_REFRESH_SEC` — wttr.in fetch cadence (default 30 min)
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
