#!/usr/bin/env python3
"""Ambient Pong dashboard for Ubuntu/X11.

Two modes share one binary:
- `pong` (default): full-screen lock that mirrors across every connected
  monitor and authenticates against the real login password via PAM.
- `pong --dashboard`: same dashboard rendered in a resizable, regular
  window. No PAM, no keyboard grab, single monitor. Close the window or
  press Esc/Q to quit.
"""

import atexit
import datetime as _dt
import fcntl
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

# Switch stdout/stderr to line buffering so journalctl shows lifecycle
# prints + the pygame banner in real time. Default is block buffering
# when stdout isn't a TTY, which is why systemd scopes only flush at
# process exit and we can't see what the process was doing.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, OSError):
    pass

# Pong has no sound — keep SDL2 from spinning up Pulse mainloop +
# audio threads. Must be set BEFORE pygame import so SDL picks it up
# during its own init. Drops two threads + a few MB.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame
from pygame._sdl2.video import Renderer, Texture, Window

try:
    import PAM
except ImportError:
    PAM = None  # only required in lock mode; checked once mode is resolved

# Calendar parse deps are optional (`python3-icalendar` is a hard dep on
# the .deb; `recurring-ical-events` is `Recommends`). Import at module
# load so the background calendar thread never races with the main
# thread's SDL event pump — lazy imports from a thread while pygame is
# pumping events can segfault SDL on some Mutter/X11 builds.
try:
    import icalendar
    import recurring_ical_events
    HAS_ICS = True
except ImportError:
    HAS_ICS = False

# --- Tunables ---
MAX_ATTEMPTS = 3
COOLOFF_SECONDS = 15 * 60
PAM_SERVICE = "login"          # change to "passwd" or a custom service if PAM denies
INPUT_TIMEOUT = 8              # cancel password prompt if idle this long
LOGICAL_W, LOGICAL_H = 1920, 1080
PADDLE_W, PADDLE_H = 48, 144    # paddle bumped slightly (was 40×120)
PADDLE_MARGIN = 60
BALL_SIZE = 48                  # ball matched to paddle weight (was 40)
PONG_ALPHA = 128                # ~50% alpha on paddles + ball (clock-tone, recessive)
BALL_SPEED_X = 7.0
BALL_SPEED_Y = 4.2
PADDLE_SPEED = 5.5
MAX_PASSWORD_LEN = 256
CLOCK_FONT_SIZE = 260           # central clock height in logical px
DAYLINE_FONT_SIZE = 120         # "MON 08 JUN" — one mono-bold line under clock
UI_FONT_SIZE = 14               # lock-mode legacy; unused after the port
# --- Lock vs dashboard: what remains mode-specific ---
# After the 2026-06-12 port the *visual* code path is shared: same
# 15×8 lattice, same Google Sans Flex Bold focal stack, same content
# tile layout, same ball-flash interaction. The remaining `dashboard_mode`
# gates cover only functional differences:
#
#   * Window setup — lock uses a single borderless SDL Window +
#                    Renderer + Texture spanning the bounding box of
#                    every connected monitor (so the same frame
#                    mirrors across displays). Dashboard uses
#                    pygame.display.set_mode with RESIZABLE.
#   * Auth         — lock acquires LOCK_FILE, requires PAM, grabs the
#                    keyboard, hides the mouse, runs the password
#                    input loop with INPUT_TIMEOUT + lockout state.
#                    Dashboard acquires DASH_LOCK_FILE, no PAM, no
#                    grab, mouse visible, Esc/Q to quit.
#   * Ball         — invisible in both modes. Physics still drives the
#                    paddles + tile-flash; the lattice flashes carry
#                    the signal of its position.
#   * Input strip  — only rendered in lock mode. Sits at the central
#                    lattice column (col 7) row 6, just below the
#                    clock. Mini-keyboard wake-hint when idle;
#                    password asterisks + warning bar when typing.
#   * Output path  — lock streams `surf` into a Texture and draws it
#                    per-monitor sub-rect. Dashboard `smoothscale`s
#                    `surf` to the resizable window and flips.
DASH_UI_FONT_SIZE = 24
# HANOI (city label under the temp) is pinned at the smaller size so
# it sits visually quiet beside the now-larger temp.
DASH_LABEL_FONT_SIZE = 22
# Mid-tier focal font (DAY DATE) — bigger than chip text but smaller
# than the clock. Temp gets its own larger tier so it reads as primary
# weather data. Both render through Google Sans Flex Bold like the
# clock so the focal elements share a typeface.
DASH_FOCAL_FONT_SIZE = 40
DASH_TEMP_FONT_SIZE = 56
# SPACE wake-hint, rendered as a mini-keyboard bottom row.
KB_KEY_H = 32                   # height of all hint keys
KB_MOD_W = 38                   # width of Ctrl/Alt keys
KB_SPACE_W = 180                # width of the highlighted spacebar
KB_GAP = 4                      # gap between keys
KB_RADIUS = 4                   # corner radius
KB_LABEL_FONT_SIZE = 14         # tiny labels on Ctrl/Alt
INPUT_BAR_WIDTH = 360           # progress underline width
INPUT_BAR_HEIGHT = 3            # progress underline thickness
INPUT_WARN_SEC = 2              # remaining time at which the bar turns auburn
CLOCK_24H = True                # False for 12-hour time

# Theme-invariant tones — identical under both fizx and upleb.
PAL_NET       = (30, 34, 40)    # tile-highlight outlines (the rounded frames)
PAL_GRID_MAIN = (18, 19, 22)    # 4×4 main grid lines — near-black, faint
PAL_GRID_SUB  = (12, 13, 15)    # 16×16 sub-grid — barely above bg
PAL_KB_DIM    = (55, 60, 67)    # SPACE wake-tile Ctrl/Alt outline + label

# Dual-theme palette — channel triples mirrored verbatim from
# ~/code_gh/xjmzx/ndisc.smpl/src/index.css :root (fizx) and .theme-upleb.
# Pong alternates between the two on each launch; override via
# ~/.config/pong/theme.json {"mode": "fizx" | "upleb" | "alternate"}.
THEMES = {
    "fizx":  {"bg":     (  9,  13,  18), "panel":  ( 13,  17,  23),
              "fg":     (240, 246, 252), "muted":  (107, 122, 141),
              "accent": (122, 240, 205), "mauve":  (189, 168, 251),
              "auburn": (178,  96, 138), "ok":     ( 74, 222, 128),
              "alert":  (248, 113, 113)},
    "upleb": {"bg":     ( 10,  15,  21), "panel":  ( 13,  17,  23),
              "fg":     (201, 209, 217), "muted":  (107, 122, 141),
              "accent": (255, 182, 158), "mauve":  (255, 198, 117),
              "auburn": (178,  96,  58), "ok":     ( 74, 222, 128),
              "alert":  (248, 113, 113)},
}
THEME_CACHE  = os.path.expanduser("~/.cache/pong_lock_theme")
THEME_CONFIG = os.path.expanduser("~/.config/pong/theme.json")
P = {}  # populated by main() via build_palette(_resolve_theme())
WEATHER_LOCATION = "Hanoi"      # wttr.in location string; "" for IP-based
WEATHER_REFRESH_SEC = 1800      # 30 min between fetches
WEATHER_TIMEOUT_SEC = 6         # fetch timeout
EVENT_LABEL_MAX = 14            # truncate event labels to this many chars
CALENDAR_CONFIG = os.path.expanduser("~/.config/pong/calendars.json")
CALENDAR_REFRESH_SEC = 600      # 10 min between ICS fetches
# 35-day window covers the full rolling 4-week calendar block (4 × 7
# days = 28) plus a buffer for events near the window edges.
CALENDAR_LOOKAHEAD_DAYS = 35
CALENDAR_LOOKBACK_DAYS = 7      # safety: events earlier in current week

# 4×4 dashboard grid. Clock occupies the centre 2×2; perimeter cells
# carry the dashboard chips. Pong stays full-screen on top so paddles
# still mark the screen edges.
GRID_COLS = 4
# Dashboard mode steps the grid up to 5 columns so the clock can be a
# centered 3×2 block flanked by uniform 1-cell chip slots. GRID_COLS and
# CELL_W are overwritten in main() once the mode is resolved; the helper
# functions read them as module-level names so layout math stays a
# single point of truth.
GRID_COLS_DASH = 5
GRID_ROWS = 4
# Reserve a vertical band top + bottom that mimics dock/taskbar space —
# stops the perimeter tiles bleeding into where a panel would sit on a
# real desktop. Pong paddles + ball still play across the full screen.
DASH_INSET_Y = 60
CELL_W = LOGICAL_W // GRID_COLS
CELL_H = (LOGICAL_H - 2 * DASH_INSET_Y) // GRID_ROWS
SUB_GRID_DIV = 4                # each 4×4 cell subdivided 4 ways → 16×16
HL_INSET = 10                   # gap between cell edge and highlight frame
HL_RADIUS = 14                  # highlight corner radius
HL_WIDTH = 1                    # highlight outline thickness
TILE_INSET = 20                 # tile-edge → text-edge inset for L-aligned chips
TILE_BG_ALPHA = 38              # ~15% of 255 — calendar-tint tile-bg wash
TILE_BG_FAINT_ALPHA = 25        # ~10% of 255 — theme-mauve wash on plain tiles
TILE_BG_INPUT_ALPHA = 12        # ~5% of 255 — input strip, lighter still
# Day-focus boosts: today's group wash + ~20%, tomorrow's + ~10%, so
# the eye lands on "now" first and "next" second without the rest of
# the week fading too much.
TILE_BG_TODAY_BOOST = 51        # +20% of 255 added to today's wash
TILE_BG_TOMORROW_BOOST = 26     # +10% of 255 added to tomorrow's wash
# Past-day fade: yesterday recedes a step, anything ≥2 days ago
# recedes further. Same rule applies in both clock view (only the
# current M-F strip is shown) and full calendar view.
TILE_BG_PAST1_DROP = 13         # ~5% of 255 subtracted from yesterday
TILE_BG_PAST2_DROP = 26         # ~10% of 255 subtracted from ≥2 days ago
# Glyph fade for past days — date numeral + event labels dim with
# days elapsed, on top of the wash drop. Goes monotonically deeper
# day-by-day so the recent past reads as context, older past as
# background. Future + today stay at full opacity (255).
TILE_FG_PAST1 = 230             # ~90% — yesterday
TILE_FG_PAST2 = 204             # ~80% — 2 days past
TILE_FG_PAST3 = 179             # ~70% — 3 days past
TILE_FG_PAST4 = 153             # ~60% — 4+ days past
# Dashboard-only interaction: when the pong ball's centre enters one of
# the sub-divided empty tiles, that tile's alpha is bumped briefly and
# decays back to TILE_BG_FAINT_ALPHA. Pure visual cue for now —
# foundation for richer ball/tile interactions later.
TILE_FLASH_DUR = 0.45           # seconds the flash takes to fade out
TILE_FLASH_ALPHA_BOOST = 90     # peak alpha added on top of FAINT
# Universal "main text" colour — used regardless of theme so chrome
# text reads identically in fizx and upleb. Borrowed from fizx's FG.
WHITE_TEXT = (240, 246, 252)

# Dashboard-only uniform square-mini-tile lattice. Drops the grid-cell
# concept entirely for dashboard: every empty mini-tile is the same
# square size, separated by the same gap, tiled across the entire
# dashboard area. Content tiles (4 left chips + clock) overlay
# specific lattice regions; their cells still appear in the registry
# so the ball-flash effect remains continuous across the surface.
DASH_MINI_SIZE = 92             # square mini-tile (15 cols fit at this size)
DASH_MINI_GAP = 2 * HL_INSET    # gap between mini-tiles == inter-cell gap
PADDLE_GAP = 20                 # clear gap between paddle face and tile edge
# Col-0 tile edges sit at PADDLE_CLEAR so the rounded outline doesn't
# kiss the moving paddle. (Right column tiles, when added back, would
# mirror this via LOGICAL_W - PADDLE_CLEAR.)
PADDLE_CLEAR = PADDLE_MARGIN + PADDLE_W + PADDLE_GAP

STATE_FILE = os.path.expanduser("~/.cache/pong_lock_state")
LOCK_FILE = os.path.expanduser("~/.cache/pong_lock.lock")
DASH_LOCK_FILE = os.path.expanduser("~/.cache/pong_dash.lock")
CRASH_LOG = os.path.expanduser("~/.cache/pong/crash.log")

# Dashboard-mode window defaults.
DASH_WIN_W, DASH_WIN_H = 1280, 720
DASH_WIN_MIN = (640, 360)


def _fade(rgb, f):
    return tuple(int(c * f) for c in rgb)


def _ensure_theme_config():
    """Write the default theme.json template on first run."""
    if os.path.isfile(THEME_CONFIG):
        return
    try:
        os.makedirs(os.path.dirname(THEME_CONFIG), exist_ok=True)
        with open(THEME_CONFIG, "w") as f:
            json.dump({
                "_comment": ("\"mode\": \"alternate\" flips fizx/upleb on "
                             "each launch (default). Use \"fizx\" or "
                             "\"upleb\" to pin one scheme."),
                "mode": "alternate",
            }, f, indent=2)
    except OSError:
        pass


def _resolve_theme():
    """Pick fizx vs upleb. Config pin wins; otherwise flip last cached theme.
    First-ever launch lands on fizx (cache starts sentinel 'upleb')."""
    try:
        with open(THEME_CONFIG) as f:
            mode = json.load(f).get("mode", "alternate")
    except (OSError, json.JSONDecodeError):
        mode = "alternate"
    if mode in THEMES:
        return mode
    try:
        with open(THEME_CACHE) as f:
            last = f.read().strip()
    except OSError:
        last = "upleb"
    current = "fizx" if last == "upleb" else "upleb"
    try:
        os.makedirs(os.path.dirname(THEME_CACHE), exist_ok=True)
        with open(THEME_CACHE, "w") as f:
            f.write(current)
    except OSError:
        pass
    return current


def build_palette(name):
    """Resolve theme tokens + pong-local fades into one dict."""
    t = THEMES[name]
    return {
        "BG":          t["bg"],
        "PANEL":       t["panel"],
        "FG":          t["fg"],
        "MUTED":       t["muted"],
        "ACCENT":      t["accent"],
        "MAUVE":       t["mauve"],
        "MAUVE_FADE":  _fade(t["mauve"], 0.70),
        "AUBURN":      t["auburn"],
        "OK":          t["ok"],
        "ALERT":       t["alert"],
    }


def _log_crash(label, exc_type, exc_value, exc_tb):
    """Append a timestamped traceback to CRASH_LOG. Best-effort: a
    log-write failure must never raise out of an exception handler."""
    try:
        os.makedirs(os.path.dirname(CRASH_LOG), exist_ok=True)
        with open(CRASH_LOG, "a") as f:
            ts = _dt.datetime.now().isoformat(timespec="seconds")
            f.write(f"\n=== {ts} [{label}] ===\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
    except OSError:
        pass


def _log_lifecycle(label, detail=""):
    """Append a one-line lifecycle event to CRASH_LOG. Used to leave
    a trail when the process exits cleanly but unexpectedly (e.g.
    pygame.QUIT delivered by Mutter during a multi-monitor drag) —
    sys.excepthook only catches Python exceptions, so non-exception
    exits would otherwise vanish from our diagnostics."""
    try:
        os.makedirs(os.path.dirname(CRASH_LOG), exist_ok=True)
        with open(CRASH_LOG, "a") as f:
            ts = _dt.datetime.now().isoformat(timespec="seconds")
            line = f"{ts} [lifecycle:{label}]"
            if detail:
                line += f" {detail}"
            f.write(line + "\n")
    except OSError:
        pass


def install_emergency_cleanup(dashboard_mode):
    """Release the X keyboard grab + restore the mouse cursor on ANY
    exit path — clean return, unhandled exception, or SIGTERM. Without
    this, a crash in lock mode can leave the keyboard grabbed by a dead
    process; the user then can't type into GDM and has to switch VT or
    reboot. atexit doesn't fire on SIGKILL — nothing can save us there.

    Python's default SIGTERM behaviour is to terminate without running
    atexit; the handler below converts SIGTERM into SystemExit so the
    registered cleanup still gets to run."""
    def cleanup():
        _log_lifecycle("exit",
                       f"mode={'dash' if dashboard_mode else 'lock'}")
        try:
            if not dashboard_mode:
                pygame.event.set_grab(False)
            pygame.mouse.set_visible(True)
            pygame.quit()
        except Exception:
            pass

    def on_sigterm(*_):
        _log_lifecycle("sigterm",
                       f"mode={'dash' if dashboard_mode else 'lock'}")
        sys.exit(0)

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, on_sigterm)


def install_crash_logging(mode_label):
    """Route unhandled exceptions from the main thread + any daemon
    thread into CRASH_LOG before the default handler runs. Lets us see
    what killed a long-running session instead of `journalctl` only
    showing the systemd scope exit."""
    def hook(exc_type, exc_value, exc_tb):
        _log_crash(mode_label, exc_type, exc_value, exc_tb)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def thread_hook(args):
        _log_crash(f"{mode_label}/thread:{args.thread.name}",
                   args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = hook
    threading.excepthook = thread_hook


def acquire_single_instance(lock_path=LOCK_FILE):
    """Refuse to launch a second instance over the first. Lock-mode and
    dashboard-mode each take a distinct lock path so they can coexist."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fp = open(lock_path, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    return fp  # kept alive so the lock survives for this process's lifetime


def get_displays():
    """Connected-monitor rects via xrandr: [(x, y, w, h), ...]."""
    try:
        out = subprocess.check_output(["xrandr", "--query"], text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    pat = re.compile(r"(\d+)x(\d+)\+(\d+)\+(\d+)")
    rects = []
    for line in out.splitlines():
        if " connected" in line:
            m = pat.search(line)
            if m:
                w, h, x, y = map(int, m.groups())
                rects.append((x, y, w, h))
    return rects


def read_state():
    try:
        with open(STATE_FILE) as f:
            data = {}
            for line in f:
                if ":" in line:
                    k, v = line.strip().split(":", 1)
                    data[k] = v
        return int(data.get("attempts", 0)), float(data.get("until", 0))
    except (FileNotFoundError, ValueError):
        return 0, 0.0


def write_state(attempts, until):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(f"attempts:{attempts}\nuntil:{until}\n")


def is_locked_out():
    return time.time() < read_state()[1]


def lockout_remaining():
    return max(0, int(read_state()[1] - time.time()))


def record_failure():
    attempts, _ = read_state()
    attempts += 1
    if attempts >= MAX_ATTEMPTS:
        write_state(0, time.time() + COOLOFF_SECONDS)
    else:
        write_state(attempts, 0)


def record_success():
    write_state(0, 0)


def fmt_time(sec):
    return f"{sec // 60}:{sec % 60:02d}"


def cell_center(col, row, colspan=1, rowspan=1):
    """Logical pixel centre of a grid cell or merged cell-group."""
    x = col * CELL_W + (colspan * CELL_W) // 2
    y = DASH_INSET_Y + row * CELL_H + (rowspan * CELL_H) // 2
    return x, y


def draw_tile_bg(surf, col, row, colspan=1, rowspan=1, color=None,
                 alpha=TILE_BG_ALPHA, left=None, right=None):
    """Translucent rounded fill on a tile — same bounds as draw_highlight.
    No-op if color is None."""
    if color is None:
        return
    x = left if left is not None else col * CELL_W + HL_INSET
    right_x = (right if right is not None
               else (col + colspan) * CELL_W - HL_INSET)
    y = DASH_INSET_Y + row * CELL_H + HL_INSET
    w = right_x - x
    h = rowspan * CELL_H - 2 * HL_INSET
    bg = pygame.Surface((w, h), pygame.SRCALPHA)
    pygame.draw.rect(bg, (*color, alpha), (0, 0, w, h),
                     border_radius=HL_RADIUS)
    surf.blit(bg, (x, y))


def _tile_bg_rect(surf, x, y, w, h, color, alpha):
    """Translucent rounded fill at an explicit pixel rect — used for
    sub-divided tiles that don't sit on the main grid."""
    bg = pygame.Surface((w, h), pygame.SRCALPHA)
    pygame.draw.rect(bg, (*color, alpha), (0, 0, w, h),
                     border_radius=HL_RADIUS)
    surf.blit(bg, (x, y))


def _highlight_rect(surf, x, y, w, h, color=None):
    """Rounded outline at an explicit pixel rect (sub-divided tiles)."""
    pygame.draw.rect(surf, color if color is not None else PAL_NET,
                     (x, y, w, h),
                     width=HL_WIDTH, border_radius=HL_RADIUS)


def _split_3x2(x, y, w, h):
    """Return 6 (sx, sy, sw, sh) rects subdividing a parent tile into
    3 columns × 2 rows, with the same gap between sub-tiles as between
    full cells (2 × HL_INSET)."""
    gap = 2 * HL_INSET
    sw = (w - 2 * gap) // 3
    sh = (h - gap) // 2
    rects = []
    for sr in range(2):
        for sc in range(3):
            rects.append((x + sc * (sw + gap),
                          y + sr * (sh + gap),
                          sw, sh))
    return rects


def draw_highlight(surf, col, row, colspan=1, rowspan=1, color=None,
                   left=None, right=None):
    """Inset, rounded outline that groups a cell or cell-span as one unit.
    `left` / `right` override HL_INSET on that side (used to push col-0 +
    (3,0) tile edges in past the pong paddles)."""
    x = left if left is not None else col * CELL_W + HL_INSET
    right_x = (right if right is not None
               else (col + colspan) * CELL_W - HL_INSET)
    y = DASH_INSET_Y + row * CELL_H + HL_INSET
    w = right_x - x
    h = rowspan * CELL_H - 2 * HL_INSET
    pygame.draw.rect(surf, color if color is not None else PAL_NET,
                     (x, y, w, h),
                     width=HL_WIDTH, border_radius=HL_RADIUS)


def draw_grid(surf):
    """Two-tier grid: faint 16×16 graph-paper sub-grid + 4×4 main grid.
    All tiles render as flat PAL_BG — only the cal cells get a tinted
    backdrop, painted later by draw_tile_bg. Clipped vertically to the
    dashboard band so the dock-inset strips stay bare PAL_BG."""
    dash_top = DASH_INSET_Y
    dash_bot = LOGICAL_H - DASH_INSET_Y
    dash_h = dash_bot - dash_top
    # Sub-grid (16×16) — drawn first so main grid overlays cleanly.
    sub_cols = GRID_COLS * SUB_GRID_DIV
    sub_rows = GRID_ROWS * SUB_GRID_DIV
    for c in range(1, sub_cols):
        if c % SUB_GRID_DIV == 0:
            continue
        x = c * LOGICAL_W // sub_cols
        pygame.draw.line(surf, PAL_GRID_SUB, (x, dash_top), (x, dash_bot), 1)
    for r in range(1, sub_rows):
        if r % SUB_GRID_DIV == 0:
            continue
        y = dash_top + r * dash_h // sub_rows
        pygame.draw.line(surf, PAL_GRID_SUB, (0, y), (LOGICAL_W, y), 1)
    # Main grid (4×4) — near-black, sits one notch above the sub-grid.
    for c in range(1, GRID_COLS):
        pygame.draw.line(surf, PAL_GRID_MAIN, (c * CELL_W, dash_top),
                         (c * CELL_W, dash_bot), 1)
    for r in range(1, GRID_ROWS):
        pygame.draw.line(surf, PAL_GRID_MAIN, (0, dash_top + r * CELL_H),
                         (LOGICAL_W, dash_top + r * CELL_H), 1)


_weather = {"text": "", "sun": (), "moon": ""}


def _fetch_weather_once():
    fmt = urllib.parse.quote("%t|%S|%s|%m", safe="")
    url = f"https://wttr.in/{WEATHER_LOCATION}?format={fmt}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pong-lock"})
        with urllib.request.urlopen(req, timeout=WEATHER_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
        if not body or body.lower().startswith("unknown"):
            return
        parts = body.split("|")
        if len(parts) >= 4:
            temp, sunrise, sunset, moon = parts[:4]
            _weather["text"] = temp.replace("+", "")
            _weather["sun"] = (sunrise[:5], sunset[:5])
            _weather["moon"] = moon
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def start_weather_thread():
    def loop():
        while True:
            try:
                _fetch_weather_once()
            except Exception:
                _log_crash("weather/thread", *sys.exc_info())
            time.sleep(WEATHER_REFRESH_SEC)
    t = threading.Thread(target=loop, daemon=True, name="weather")
    t.start()


# `by_calendar` keeps the "next event per calendar" used by clock-view
# cal0/cal1 chips. `by_date` is a dict {date_obj: [(color, name, summary), ...]}
# used by calendar-view date tiles to render per-event labels.
# `version` is bumped on every successful fetch so the main thread
# knows when to invalidate caches.
_events = {"by_calendar": [], "by_date": {}, "version": 0}


# Google Calendar's named palette (approximate RGB). Lets the config name
# colours by their Google-side label rather than hex codes. Add new entries
# here if the user adopts more Google colour names later.
GOOGLE_CAL_COLORS = {
    "tomato":     (213,   0,   0),
    "flamingo":   (230, 124, 115),
    "tangerine":  (244,  81,  30),
    "pumpkin":    (239, 108,   0),
    "mango":      (255, 173,  70),
    "banana":     (246, 191,  38),
    "citron":     (235, 224,  72),
    "avocado":    (158, 199,  44),
    "pistachio":  (125, 188,  88),
    "basil":      ( 11, 128,  67),
    "sage":       ( 51, 182, 121),
    "eucalyptus": ( 15, 152, 113),
    "peacock":    (  3, 155, 229),
    "cobalt":     ( 41, 121, 255),
    "blueberry":  ( 63,  81, 181),
    "lavender":   (121, 134, 203),
    "wisteria":   (197, 202, 233),
    "amethyst":   (149, 117, 205),
    "grape":      (142,  36, 170),
    "graphite":   ( 97,  97,  97),
    "birch":      (167, 155, 142),
    "cocoa":      (121,  85,  72),
}


def _cal_color(name):
    """Resolve a colour name or hex to RGB; None if unset/unknown."""
    if not name:
        return None
    if name.startswith("#") and len(name) == 7:
        try:
            return tuple(int(name[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return None
    return GOOGLE_CAL_COLORS.get(name.lower())


def _ensure_calendar_config():
    """Write an empty 0600 template on first run so the user can edit it."""
    if os.path.isfile(CALENDAR_CONFIG):
        return
    try:
        os.makedirs(os.path.dirname(CALENDAR_CONFIG), exist_ok=True)
        with open(CALENDAR_CONFIG, "w") as f:
            json.dump({
                "_comment": ("Drop one entry per Google Calendar to surface. "
                             "Get URL from Calendar > Settings and sharing > "
                             "Integrate calendar > Secret address in iCal "
                             "format. Keep this file private (mode 0600)."),
                "calendars": []
                # Example entry:
                # {"name": "Jog",
                #  "url": "https://calendar.google.com/calendar/ical/.../basic.ics"}
            }, f, indent=2)
        os.chmod(CALENDAR_CONFIG, 0o600)
    except OSError:
        pass


def _load_calendars():
    try:
        with open(CALENDAR_CONFIG) as f:
            return json.load(f).get("calendars", [])
    except (OSError, json.JSONDecodeError):
        return []


def _start_local_date(start):
    """Return the local calendar date of a DTSTART value, handling
    both all-day events (start is a `date`) and timed events (start
    is a `datetime`, possibly naive)."""
    if isinstance(start, _dt.date) and not isinstance(start, _dt.datetime):
        return start
    if start.tzinfo is None:
        start = start.replace(tzinfo=_dt.timezone.utc)
    return start.astimezone().date()


def _fetch_calendars_once():
    """Pull each ICS, expand recurrences, capture each calendar's
    soonest event AND index every event in the window by local date so
    calendar-view date tiles can flag event presence per day."""
    if not HAS_ICS:
        return  # parse deps not installed; silently skip
    calendars = _load_calendars()
    if not calendars:
        return

    now = _dt.datetime.now(_dt.timezone.utc)
    window_start = now - _dt.timedelta(days=CALENDAR_LOOKBACK_DAYS)
    window_end = now + _dt.timedelta(days=CALENDAR_LOOKAHEAD_DAYS)
    results = []
    by_date = {}
    for cal in calendars:
        url = cal.get("url")
        name = cal.get("name", "")
        color = _cal_color(cal.get("color"))
        next_event = None
        if url:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "pong-lock"})
                with urllib.request.urlopen(req,
                                            timeout=10) as resp:
                    ics_data = resp.read()
                ical = icalendar.Calendar.from_ical(ics_data)
                for ev in recurring_ical_events.of(ical).between(
                        window_start, window_end):
                    start_raw = ev.get("DTSTART").dt
                    local_date = _start_local_date(start_raw)
                    # Normalize start into a tz-aware datetime for the
                    # "next event" comparator below.
                    if (isinstance(start_raw, _dt.date)
                            and not isinstance(start_raw, _dt.datetime)):
                        start_dt = _dt.datetime.combine(
                            start_raw, _dt.time.min,
                            tzinfo=_dt.timezone.utc)
                    elif start_raw.tzinfo is None:
                        start_dt = start_raw.replace(
                            tzinfo=_dt.timezone.utc)
                    else:
                        start_dt = start_raw
                    summary = str(ev.get("SUMMARY", "")).strip()
                    location = str(ev.get("LOCATION", "")).strip()
                    # Local HH:MM (or None for all-day events) so the
                    # 4-day outlook can sort chronologically and prefix
                    # event labels with the start time.
                    if (isinstance(start_raw, _dt.date)
                            and not isinstance(start_raw, _dt.datetime)):
                        time_str = None
                    else:
                        time_str = start_dt.astimezone().strftime("%H:%M")
                    # Multi-day expansion: an OFF block on a TT timetable
                    # or a multi-day holiday on any calendar lands on
                    # every spanned date, not just the start. iCal DTEND
                    # is exclusive for all-day events and inclusive
                    # (datetime-wise) for timed events.
                    dtend_obj = ev.get("DTEND")
                    if dtend_obj is not None:
                        dtend_raw = dtend_obj.dt
                        if (isinstance(dtend_raw, _dt.date)
                                and not isinstance(dtend_raw, _dt.datetime)):
                            end_local_date = (dtend_raw
                                              - _dt.timedelta(days=1))
                        else:
                            if dtend_raw.tzinfo is None:
                                dtend_raw = dtend_raw.replace(
                                    tzinfo=_dt.timezone.utc)
                            end_local = dtend_raw.astimezone()
                            end_local_date = end_local.date()
                            # Events that end exactly at midnight don't
                            # extend into that day.
                            if (end_local.time() == _dt.time(0, 0)
                                    and end_local_date > local_date):
                                end_local_date -= _dt.timedelta(days=1)
                    else:
                        end_local_date = local_date
                    if end_local_date < local_date:
                        end_local_date = local_date
                    # Per-date index: include all events in the window
                    # (past and future) so the calendar view shows the
                    # full picture of the visible 4 weeks. Continuation
                    # days clear time_str so a fictional "09:00" doesn't
                    # appear on day 2+ of a multi-day timed event.
                    d_cursor = local_date
                    while d_cursor <= end_local_date:
                        entry_time = (time_str
                                      if d_cursor == local_date else None)
                        by_date.setdefault(d_cursor, []).append(
                            (color, name, summary, entry_time))
                        d_cursor += _dt.timedelta(days=1)
                    # Next event: future only, for clock-view chip.
                    if start_dt >= now:
                        if (next_event is None
                                or start_dt < next_event["start"]):
                            next_event = {"start": start_dt,
                                          "summary": summary,
                                          "location": location}
            except Exception:
                pass  # per-calendar failure; still surface the colour+name
        results.append({"name": name, "color": color, "next": next_event})
    # Order each day's events: all-day context first (OFF days, holidays
    # — "this day is X"), then timed events chronologically. Sorted once
    # here so every consumer (calendar view date tiles, 4-day outlook)
    # sees the same order without re-sorting per frame.
    for d_key in by_date:
        by_date[d_key].sort(
            key=lambda e: (e[3] is not None, e[3] or ""))
    _events["by_calendar"] = results
    _events["by_date"] = by_date
    _events["version"] = _events.get("version", 0) + 1


def start_calendar_thread():
    def loop():
        while True:
            try:
                _fetch_calendars_once()
            except Exception:
                _log_crash("calendar/thread", *sys.exc_info())
            time.sleep(CALENDAR_REFRESH_SEC)
    t = threading.Thread(target=loop, daemon=True, name="calendar")
    t.start()


def authenticate(password):
    user = getpass.getuser()

    def conv(_auth, queries, _data):
        out = []
        for _query, qtype in queries:
            if qtype == PAM.PAM_PROMPT_ECHO_OFF:
                out.append((password, 0))
            elif qtype == PAM.PAM_PROMPT_ECHO_ON:
                out.append((user, 0))
            else:
                out.append(("", 0))
        return out

    auth = PAM.pam()
    auth.start(PAM_SERVICE)
    auth.set_item(PAM.PAM_USER, user)
    auth.set_item(PAM.PAM_CONV, conv)
    try:
        auth.authenticate()
        auth.acct_mgmt()
    except PAM.error:
        return False
    return True


def make_window(rects):
    """One borderless window spanning the bounding box of all monitors.

    Multiple top-level borderless windows on Mutter/X11 don't reliably map
    onto separate monitors — only one ends up visible. A single window
    covering the union of all rects works around this; we then draw the
    same Pong texture into each monitor's sub-rect.
    """
    min_x = min(r[0] for r in rects)
    min_y = min(r[1] for r in rects)
    total_w = max(r[0] + r[2] for r in rects) - min_x
    total_h = max(r[1] + r[3] for r in rects) - min_y
    win = Window("Pong Lock", size=(total_w, total_h),
                 position=(min_x, min_y), borderless=True)
    ren = Renderer(win)
    tex = Texture(ren, (LOGICAL_W, LOGICAL_H), streaming=True)
    dst_rects = [pygame.Rect(x - min_x, y - min_y, w, h) for (x, y, w, h) in rects]
    win.focus()
    return win, ren, tex, dst_rects


def main():
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print("Usage: pong [--dashboard]")
        print("  (no args)     full-screen lock + ambient pong (PAM auth)")
        print("  --dashboard   view the dashboard in a resizable window")
        return 0
    dashboard_mode = "--dashboard" in args or "--dash" in args
    install_crash_logging("dash" if dashboard_mode else "lock")
    install_emergency_cleanup(dashboard_mode)
    _log_lifecycle("startup",
                   f"mode={'dash' if dashboard_mode else 'lock'} "
                   f"pid={os.getpid()}")

    if not dashboard_mode and PAM is None:
        sys.stderr.write(
            "Missing dep: install with `sudo apt install python3-pam`\n")
        return 1

    # Uniform 15×8 lattice of square mini-tiles tiled across the
    # active area (between paddle clearance + dock-style top/bottom
    # inset), centred. Same in both modes — lock mode now wears the
    # same visual treatment as dashboard, differing only in the
    # functional bits (PAM auth, keyboard grab, multi-monitor mirror,
    # password input overlay). Content tiles (4 left chips + clock)
    # overlay specific regions; their cells STILL appear in the
    # empty_tile_rects list so the ball-flash continues uninterrupted
    # underneath.
    active_l = PADDLE_CLEAR
    active_t = DASH_INSET_Y
    active_w = LOGICAL_W - 2 * PADDLE_CLEAR
    active_h = LOGICAL_H - 2 * DASH_INSET_Y
    pitch = DASH_MINI_SIZE + DASH_MINI_GAP
    lat_cols = (active_w + DASH_MINI_GAP) // pitch
    lat_rows = (active_h + DASH_MINI_GAP) // pitch
    lat_w = lat_cols * DASH_MINI_SIZE + (lat_cols - 1) * DASH_MINI_GAP
    lat_h = lat_rows * DASH_MINI_SIZE + (lat_rows - 1) * DASH_MINI_GAP
    lat_x = active_l + (active_w - lat_w) // 2
    lat_y = active_t + (active_h - lat_h) // 2

    def _lat_rect(c, r, cspan=1, rspan=1):
        return (lat_x + c * pitch,
                lat_y + r * pitch,
                cspan * DASH_MINI_SIZE + (cspan - 1) * DASH_MINI_GAP,
                rspan * DASH_MINI_SIZE + (rspan - 1) * DASH_MINI_GAP)

    dash_content_rects = {}
    for i, key in enumerate(("cal0", "cal1", "weather", "identity")):
        dash_content_rects[key] = _lat_rect(0, i * 2, 3, 2)
    clock_cols = 9
    clock_rows = 2
    clock_c = (lat_cols - clock_cols) // 2
    clock_r = (lat_rows - clock_rows) // 2
    dash_content_rects["clock"] = _lat_rect(
        clock_c, clock_r, clock_cols, clock_rows)

    empty_tile_rects = []
    for r in range(lat_rows):
        for c in range(lat_cols):
            empty_tile_rects.append(
                (lat_x + c * pitch, lat_y + r * pitch,
                 DASH_MINI_SIZE, DASH_MINI_SIZE))
    tile_flash = [0.0] * len(empty_tile_rects)

    # Lock-mode input overlay anchor: central lattice column (col 7),
    # row 6 — sits cleanly below the clock (which lives at rows 3-4).
    input_cx = lat_x + 7 * pitch + DASH_MINI_SIZE // 2
    input_cy = lat_y + 6 * pitch + DASH_MINI_SIZE // 2

    _lock_fp = acquire_single_instance(  # noqa: F841 (kept open for flock)
        DASH_LOCK_FILE if dashboard_mode else LOCK_FILE)
    _ensure_theme_config()
    theme_name = _resolve_theme()
    P.update(build_palette(theme_name))

    pygame.init()
    pygame.font.init()
    _log_lifecycle("init", "stage=pygame_ready")
    start_weather_thread()
    _ensure_calendar_config()
    start_calendar_thread()
    user_host = f"{getpass.getuser()}@{os.uname().nodename}"

    win = ren = tex = None
    dst_rects = None
    screen = None
    if dashboard_mode:
        pygame.mouse.set_visible(True)
        screen = pygame.display.set_mode(
            (DASH_WIN_W, DASH_WIN_H), pygame.RESIZABLE)
        pygame.display.set_caption("Pong Dashboard")
        try:
            Window.from_display_module().minimum_size = DASH_WIN_MIN
        except Exception:
            pass
    else:
        pygame.mouse.set_visible(False)
        rects = get_displays() or [(0, 0, 1920, 1080)]
        win, ren, tex, dst_rects = make_window(rects)
        pygame.event.set_grab(True)
    _log_lifecycle("init", "stage=window_ready")

    surf = pygame.Surface((LOGICAL_W, LOGICAL_H))
    # Pre-render the static visual layer once: bg + 120 mini-tile
    # bgs + outlines + 4 content tile outlines + weather/identity
    # wash. Per-frame work drops to ~one full-window blit plus
    # dynamic overlays (cal tints, flashes, clock, text, paddles).
    dash_static_surf = pygame.Surface((LOGICAL_W, LOGICAL_H))
    dash_static_surf.fill(P["BG"])
    _faint_static = P["MAUVE"]
    # Mini-tile fills first, then any mode-specific washes, then the
    # outlines on top — so the outlines stay crisp regardless of what
    # tinting happens underneath.
    for (x, y, w, h) in empty_tile_rects:
        _tile_bg_rect(dash_static_surf, x, y, w, h,
                      _faint_static, TILE_BG_FAINT_ALPHA)
    # Weekend-day side tiles: one mini-tile-wide column of 4 recessive
    # fills on each side of the lattice, each spanning the height of a
    # 3×2 calendar-week group so the weekend bracket reads as the
    # full-week strip. Sit behind the paddles, purely aesthetic, no
    # numerals, no outline. Horizontal alpha gradient: each side fades
    # away from the lattice — the edge touching the grid is most
    # opaque, the outer edge fades to transparent. Built as a rounded
    # tile with a per-column alpha gradient multiplied in, so the
    # corner curvature is preserved.
    WEEKEND_H = 2 * DASH_MINI_SIZE + DASH_MINI_GAP
    def _weekend_tile(fade_in):
        tile = pygame.Surface((DASH_MINI_SIZE, WEEKEND_H), pygame.SRCALPHA)
        pygame.draw.rect(tile, (*_faint_static, TILE_BG_FAINT_ALPHA),
                         (0, 0, DASH_MINI_SIZE, WEEKEND_H),
                         border_radius=HL_RADIUS)
        grad = pygame.Surface((DASH_MINI_SIZE, WEEKEND_H), pygame.SRCALPHA)
        for px in range(DASH_MINI_SIZE):
            ratio = (px + 1) / DASH_MINI_SIZE
            a = int(255 * (ratio if fade_in else 1 - ratio))
            pygame.draw.line(grad, (255, 255, 255, a),
                             (px, 0), (px, WEEKEND_H - 1))
        tile.blit(grad, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        return tile
    left_weekend = _weekend_tile(fade_in=True)
    right_weekend = _weekend_tile(fade_in=False)
    for wr in (0, 2, 4, 6):
        wy = lat_y + wr * pitch
        dash_static_surf.blit(left_weekend, (lat_x - pitch, wy))
        dash_static_surf.blit(right_weekend,
                              (lat_x + lat_w + DASH_MINI_GAP, wy))
    # Weather + identity group washes used to be baked in here, but
    # they fight the calendar-view per-group event/empty wash. They're
    # applied per-frame in clock view only further down.
    # Lock mode: dark wash over the 5 mini-tiles that hold the password
    # input strip (cols 5-9, row 6) so the login zone reads as a
    # dedicated area against the lattice.
    if not dashboard_mode:
        for c in range(5, 10):
            tile_x = lat_x + c * pitch
            tile_y = lat_y + 6 * pitch
            _tile_bg_rect(dash_static_surf, tile_x, tile_y,
                          DASH_MINI_SIZE, DASH_MINI_SIZE,
                          P["BG"], 140)
    for (x, y, w, h) in empty_tile_rects:
        _highlight_rect(dash_static_surf, x, y, w, h)
    for key in ("cal0", "cal1", "weather", "identity"):
        _highlight_rect(dash_static_surf,
                        *dash_content_rects[key])
    _log_lifecycle("init", "stage=static_built")
    flash_buf = pygame.Surface(
        (DASH_MINI_SIZE, DASH_MINI_SIZE), pygame.SRCALPHA)
    font = pygame.font.SysFont("monospace", 56)
    small = pygame.font.SysFont("monospace", 32)
    # Typography mirrors the ndisc suite (tailwind.config.ts): Helvetica
    # stack for chrome/labels (DAY), mono stack for numeric data (CLOCK +
    # WEATHER). pygame.font.SysFont walks the comma-separated list and
    # picks the first installed family — same fallback shape as the CSS
    # stack used in ndisc / ndisc.smpl / ndisc.blobtree.
    SANS_STACK = "helvetica,arial,nimbus sans,liberation sans,dejavu sans"
    MONO_STACK = ("liberation mono,dejavu sans mono,nimbus mono ps,"
                  "ubuntu mono,courier new,courier")
    # Ubuntu-first stack for everything outside the centre cluster — one
    # uniform size + weight so the focal CLOCK/DAY/DATE stand alone.
    UBUNTU_STACK = "ubuntu,helvetica,arial,nimbus sans,liberation sans"
    # Dashboard clock face: load the genuine Google Sans Flex Bold
    # static instance straight from its file (no SDL_ttf faux-bold).
    # Falls back to a sans-bold SysFont stack if the static file isn't
    # present on this machine. Lock mode keeps its mono-bold focal
    # treatment.
    DASH_CLOCK_FILE = os.path.expanduser(
        "~/.local/share/fonts/GoogleSansFlex-Bold-Static.ttf")
    DASH_CLOCK_STACK = ("google sans flex,roboto,inter,ibm plex sans,"
                        "ubuntu,helvetica,arial,nimbus sans,liberation sans")

    def _make_dash_clock_font(size):
        if os.path.isfile(DASH_CLOCK_FILE):
            return pygame.font.Font(DASH_CLOCK_FILE, size)
        return pygame.font.SysFont(DASH_CLOCK_STACK, size, bold=True)

    # Clock: Google Sans Flex Bold static, auto-scaled to fill its
    # lattice region with a margin equal to the inter-tile gap on
    # every side. Aspect preserved — pick the smaller of the width-fit
    # and height-fit scales.
    clock_font = _make_dash_clock_font(CLOCK_FONT_SIZE)
    margin = DASH_MINI_GAP
    _, _, cw, ch = dash_content_rects["clock"]
    avail_w = cw - 2 * margin
    avail_h = ch - 2 * margin
    probe_w, probe_h = clock_font.size("88:88")
    scale = min(avail_w / probe_w, avail_h / probe_h)
    if scale > 1.0:
        clock_font = _make_dash_clock_font(int(CLOCK_FONT_SIZE * scale))
    # Focal stack shares the clock face so DAY DATE + temp rhyme with
    # the clock. Temp has its own larger tier.
    dash_focal_font = _make_dash_clock_font(DASH_FOCAL_FONT_SIZE)
    dash_temp_font = _make_dash_clock_font(DASH_TEMP_FONT_SIZE)
    dayline_font = dash_focal_font
    # Calendar date numerals. Dates land at the top-right mini-tile of
    # each conceptual 3×2 tile group, matching the rhythm of the four
    # existing left content tiles (cal0/cal1/weather/identity). The grid
    # tiles the whole lattice — 5 groups across × 4 down = 20 dates,
    # Mon-Fri across, weeks down. Anchor (col 2, row 0) = top-right of
    # the Jog content tile = Mon of the first rolling week.
    # Same Google Sans Flex Bold face + P["ACCENT"] as the clock so the
    # dates rhyme typographically with the focal typography.
    # Calendar-view date numerals. Pre-render once at startup, rebuild
    # on day rollover (handled in the main loop). Each entry is a
    # 6-tuple: (date_obj, surface, blit_x, blit_y, tile_x, tile_y).
    # tile_x/tile_y are kept around for per-frame event pip placement.
    date_blits = []
    date_font = None
    DATE_COLS = (2, 5, 8, 11, 14)   # top-right of each 3-col group
    DATE_ROWS = (0, 2, 4, 6)        # top row of each 2-row group
    cal_anchor_date = None

    def _calendar_anchor(today):
        """Mon-Fri today → this week's Monday. Sat-Sun today → next
        Monday. So the calendar block always opens on a workday-week
        and the weekend doesn't display a fading current row."""
        wd = today.weekday()
        if wd <= 4:
            return today - _dt.timedelta(days=wd)
        return today + _dt.timedelta(days=7 - wd)

    def _build_date_blits(anchor, today):
        blits = []
        for ri, rr in enumerate(DATE_ROWS):
            for ci, cc in enumerate(DATE_COLS):
                d = anchor + _dt.timedelta(days=ri * 7 + ci)
                # Today's numeral renders white in both header dayline
                # and calendar tile, so the eye pairs them.
                num_color = WHITE_TEXT if d == today else P["ACCENT"]
                ds = date_font.render(f"{d.day:02d}", True, num_color)
                tx = lat_x + cc * pitch
                ty = lat_y + rr * pitch
                # Date numeral: horizontally centred, top-aligned with a
                # small inset. Previously centred vertically, which left
                # only a sliver below for event labels and silently
                # dropped a 2nd event on stacked days.
                blits.append((
                    d, ds,
                    tx + (DASH_MINI_SIZE - ds.get_width()) // 2,
                    ty + 4,
                    tx, ty,
                ))
        return blits

    # Both modes need the date font + initial date_blits — lock renders
    # the same M-F first-week strip the dashboard does. Fixed size: we
    # want the date small and top-anchored so the bulk of the tile is
    # available for stacked event labels. No auto-rescale to fill the
    # tile — that would defeat the layout.
    DATE_FONT_SIZE = 30
    date_font = _make_dash_clock_font(DATE_FONT_SIZE)
    last_today = _dt.date.today()
    cal_anchor_date = _calendar_anchor(last_today)
    date_blits = _build_date_blits(cal_anchor_date, last_today)
    ui_font = pygame.font.SysFont(UBUNTU_STACK, DASH_UI_FONT_SIZE)
    # City label sits inline with the now-larger temp — keep it small.
    city_font = pygame.font.SysFont(UBUNTU_STACK, DASH_LABEL_FONT_SIZE)
    city_surf = (city_font.render(WEATHER_LOCATION.upper(), True,
                                  WHITE_TEXT)
                 if WEATHER_LOCATION else None)
    event_header_surf = ui_font.render(" / NEXT", True, WHITE_TEXT)
    # Design profile readout — two side-by-side palette columns
    # (fizx | upleb) so the chip shows both theme identities at once.
    # Each row is one theme slot (accent / mauve / auburn). Theme name
    # labels are omitted; the active theme is signalled by the rest of
    # the dashboard's accent palette.
    hex_rgb = lambda rgb: "#{:02X}{:02X}{:02X}".format(*rgb)
    CHIP_SIZE = 8
    CHIP_TEXT_GAP = 4
    ROW_GAP = 2
    COL_GAP = 20
    THEME_KEYS = ("fizx", "upleb")
    SLOTS = ("accent", "mauve", "auburn")
    theme_rows = {}     # list of (swatch_color, hex_surf) per theme
    for tk in THEME_KEYS:
        pal = THEMES[tk]
        theme_rows[tk] = [
            (pal[slot],
             ui_font.render(hex_rgb(pal[slot]), True, WHITE_TEXT))
            for slot in SLOTS
        ]
    col_widths = [
        max(CHIP_SIZE + CHIP_TEXT_GAP + hex_surf.get_width()
            for _, hex_surf in theme_rows[tk])
        for tk in THEME_KEYS
    ]
    design_w = sum(col_widths) + COL_GAP * (len(THEME_KEYS) - 1)
    row_h = max(CHIP_SIZE,
                theme_rows[THEME_KEYS[0]][0][1].get_height())
    n_rows = len(SLOTS)
    design_h = row_h * n_rows + ROW_GAP * (n_rows - 1)
    design_surf = pygame.Surface((design_w, design_h), pygame.SRCALPHA)
    col_x = 0
    for i, tk in enumerate(THEME_KEYS):
        y = 0
        for chip_color, hex_surf in theme_rows[tk]:
            chip_cy = y + row_h // 2
            pygame.draw.circle(design_surf, chip_color,
                               (col_x + CHIP_SIZE // 2, chip_cy),
                               CHIP_SIZE // 2)
            design_surf.blit(hex_surf,
                             (col_x + CHIP_SIZE + CHIP_TEXT_GAP,
                              y + (row_h - hex_surf.get_height()) // 2))
            y += row_h + ROW_GAP
        col_x += col_widths[i] + COL_GAP

    # Compact palette strip for the dashboard header — single row of
    # theme labels + coloured slot dots, replaces the tall two-column
    # design_surf for header-strip usage. (design_surf is still built
    # above because lock mode renders it inside the identity tile.)
    PAL_DOT_R = 5
    PAL_DOT_GAP = 5
    PAL_LABEL_GAP = 8
    PAL_THEME_GAP = 18
    theme_label_surfs = {tk: ui_font.render(tk, True, P["MUTED"])
                         for tk in THEME_KEYS}
    pal_strip_h = max(theme_label_surfs[THEME_KEYS[0]].get_height(),
                      PAL_DOT_R * 2)
    # Build a per-theme chunk so each (label + dots) can blit at its
    # own x — the header strip anchors fizx and upleb to specific
    # lattice-group columns rather than packing them side-by-side.
    def _build_theme_chunk(tk):
        label = theme_label_surfs[tk]
        chunk_w = (label.get_width() + PAL_LABEL_GAP
                   + len(SLOTS) * (PAL_DOT_R * 2)
                   + (len(SLOTS) - 1) * PAL_DOT_GAP)
        chunk = pygame.Surface((chunk_w, pal_strip_h), pygame.SRCALPHA)
        chunk.blit(label, (0, (pal_strip_h - label.get_height()) // 2))
        cx = label.get_width() + PAL_LABEL_GAP
        for si, slot in enumerate(SLOTS):
            pygame.draw.circle(
                chunk, THEMES[tk][slot],
                (cx + PAL_DOT_R, pal_strip_h // 2), PAL_DOT_R)
            cx += PAL_DOT_R * 2
            if si < len(SLOTS) - 1:
                cx += PAL_DOT_GAP
        return chunk
    palette_chunk_surfs = {tk: _build_theme_chunk(tk) for tk in THEME_KEYS}

    # Small bold sans for event-summary labels rendered inside date
    # tiles in calendar view. Coloured per-event by the calendar's tint.
    event_font = pygame.font.SysFont(SANS_STACK, 14, bold=True)
    # Cache rendered event-label surfaces keyed by (text, color) so
    # the per-frame render in calendar view doesn't pay font.render
    # cost on every tile every frame. Reset whenever the calendar
    # thread bumps the events_version counter.
    event_label_cache = {}
    events_seen_version = -1

    # Pre-render the keyboard hint pieces. Bottom-row only: Ctrl Alt SPACE Alt Ctrl.
    kb_label_font = pygame.font.SysFont(SANS_STACK, KB_LABEL_FONT_SIZE,
                                        bold=True)
    kb_ctrl_label = kb_label_font.render("Ctrl", True, PAL_KB_DIM)
    kb_alt_label = kb_label_font.render("Alt", True, PAL_KB_DIM)
    kb_total_w = 4 * KB_MOD_W + 4 * KB_GAP + KB_SPACE_W
    # Each key: (width, label_surface_or_none, border_color). Spacebar
    # picks up the active theme's mauve so the focal hint warms under
    # upleb without recolouring the dim Ctrl/Alt outlines.
    kb_keys = [
        (KB_MOD_W, kb_ctrl_label, PAL_KB_DIM),
        (KB_MOD_W, kb_alt_label,  PAL_KB_DIM),
        (KB_SPACE_W, None,        P["MAUVE"]),
        (KB_MOD_W, kb_alt_label,  PAL_KB_DIM),
        (KB_MOD_W, kb_ctrl_label, PAL_KB_DIM),
    ]
    clock_str = ""
    clock_surf = None
    # Horizontal offset (px) inside clock_surf where the colon's centre
    # sits. Lets the dashboard blit position the surface so the colon
    # lands on the central lattice column instead of the surface centre.
    clock_colon_offset = 0
    dayline_str = ""
    dayline_surf = None
    weather_key = ("", ())
    weather_surf = None
    # Header-strip versions of dayline + temp, rendered at ui_font size
    # rather than the focal 40/56 tiers — they live in the slim header
    # row above the lattice and need to stay compact.
    hdr_dayline_surf = None
    hdr_temp_surf = None
    host_surf = ui_font.render(user_host, True, WHITE_TEXT)
    # Paddles + ball: clock-tone at ~50% alpha so they sit one visual
    # step back from the focal clock. SRCALPHA surfaces because the main
    # `surf` is opaque — fill alpha is ignored on draw_rect against it.
    paddle_surf = pygame.Surface((PADDLE_W, PADDLE_H), pygame.SRCALPHA)
    # Paddle colour matches a mini-tile at peak flash intensity — same
    # mauve, same alpha (FAINT + FLASH_BOOST) — so the paddles read as
    # the dashboard's reach into the play area.
    pygame.draw.rect(paddle_surf,
                     (*P["MAUVE"],
                      TILE_BG_FAINT_ALPHA + TILE_FLASH_ALPHA_BOOST),
                     (0, 0, PADDLE_W, PADDLE_H),
                     border_radius=PADDLE_W // 2)
    ball_surf = pygame.Surface((BALL_SIZE, BALL_SIZE), pygame.SRCALPHA)
    ball_surf.fill((*P["ACCENT"], PONG_ALPHA))

    bx, by = LOGICAL_W / 2, LOGICAL_H / 2
    bvx, bvy = BALL_SPEED_X, BALL_SPEED_Y
    pl = pr = LOGICAL_H / 2

    typing = False
    typed = ""
    typing_until = 0.0
    feedback = ""
    feedback_until = 0.0

    # Dashboard view mode + tiny toggle button on the lattice outskirts.
    # "clock" (default) = current dashboard view (clock + cal/weather/identity
    # content). "calendar" = clock + cal0/cal1 text hidden, date numerals
    # rendered at the top-right of each conceptual 3×2 group. Click the
    # button to flip. Lock mode ignores view_mode entirely.
    view_mode = "clock"
    view_btn_r = 14
    view_btn_cx = lat_x + lat_w - view_btn_r
    view_btn_cy = lat_y - view_btn_r - 8

    clock = pygame.time.Clock()
    running = True
    _log_lifecycle("init", "stage=loop_entered")

    while running:
        now = time.time()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                # QUIT often arrives from Mutter via WM_DELETE_WINDOW
                # (e.g. multi-monitor drag corruption, alt-F4). Log
                # mouse position + screen size so we can correlate
                # next time the dashboard "crashes" out of view.
                try:
                    mx, my = pygame.mouse.get_pos()
                except Exception:
                    mx, my = -1, -1
                sw_log = sh_log = -1
                try:
                    if screen is not None:
                        sw_log, sh_log = screen.get_size()
                except Exception:
                    pass
                _log_lifecycle(
                    "quit",
                    f"mode={'dash' if dashboard_mode else 'lock'} "
                    f"view={view_mode if dashboard_mode else 'n/a'} "
                    f"mouse=({mx},{my}) "
                    f"window=({sw_log},{sh_log})")
                running = False
                continue
            if dashboard_mode:
                if ev.type == pygame.VIDEORESIZE:
                    # Mutter/X11 fires VIDEORESIZE per pixel of mouse
                    # motion during an edge-drag. set_mode rebuilds
                    # the SDL surface and stutters the drag if called
                    # on each one. Drain the queue and act on the last
                    # size only.
                    queued = pygame.event.get(pygame.VIDEORESIZE)
                    new_size = queued[-1].size if queued else ev.size
                    screen = pygame.display.set_mode(
                        new_size, pygame.RESIZABLE)
                elif ev.type == pygame.KEYDOWN and ev.key in (
                        pygame.K_ESCAPE, pygame.K_q):
                    _log_lifecycle(
                        "esc_or_q",
                        f"key={pygame.key.name(ev.key)}")
                    running = False
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    # Translate screen-space click to logical (1920×1080)
                    # coords, then hit-test the view-toggle circle.
                    sw, sh = screen.get_size()
                    sc = min(sw / LOGICAL_W, sh / LOGICAL_H)
                    if sc > 0:
                        ox = (sw - int(LOGICAL_W * sc)) // 2
                        oy = (sh - int(LOGICAL_H * sc)) // 2
                        lx = (ev.pos[0] - ox) / sc
                        ly = (ev.pos[1] - oy) / sc
                        dx = lx - view_btn_cx
                        dy = ly - view_btn_cy
                        # Slight slop on the hit area so the tiny button
                        # is still easy to click.
                        if dx * dx + dy * dy <= (view_btn_r + 6) ** 2:
                            view_mode = ("calendar" if view_mode == "clock"
                                         else "clock")
                continue
            if ev.type != pygame.KEYDOWN:
                continue
            if is_locked_out():
                feedback = f"Locked out — {fmt_time(lockout_remaining())} remaining"
                feedback_until = now + 4
                typing = False
                typed = ""
                continue
            if not typing:
                if ev.key == pygame.K_SPACE:
                    typing = True
                    typed = ""
                    typing_until = now + INPUT_TIMEOUT
                continue
            if ev.key == pygame.K_RETURN:
                if authenticate(typed):
                    record_success()
                    running = False
                else:
                    record_failure()
                    typed = ""
                    typing = False
                    if is_locked_out():
                        feedback = f"Locked for {fmt_time(lockout_remaining())}"
                    else:
                        attempts, _ = read_state()
                        left = MAX_ATTEMPTS - attempts
                        feedback = f"Wrong — {left} {'try' if left == 1 else 'tries'} left"
                    feedback_until = now + 4
            elif ev.key == pygame.K_ESCAPE:
                typing = False
                typed = ""
            elif ev.key == pygame.K_BACKSPACE:
                typed = typed[:-1]
                typing_until = now + INPUT_TIMEOUT
            elif ev.unicode and ev.unicode.isprintable() and len(typed) < MAX_PASSWORD_LEN:
                typed += ev.unicode
                typing_until = now + INPUT_TIMEOUT

        if typing and now > typing_until:
            typing = False
            typed = ""

        bx += bvx
        by += bvy
        if by - BALL_SIZE / 2 <= 0:
            by = BALL_SIZE / 2
            bvy = abs(bvy)
        elif by + BALL_SIZE / 2 >= LOGICAL_H:
            by = LOGICAL_H - BALL_SIZE / 2
            bvy = -abs(bvy)

        target_l = by if bvx < 0 else LOGICAL_H / 2
        target_r = by if bvx > 0 else LOGICAL_H / 2
        pl += max(-PADDLE_SPEED, min(PADDLE_SPEED, target_l - pl))
        pr += max(-PADDLE_SPEED, min(PADDLE_SPEED, target_r - pr))

        left_face = PADDLE_MARGIN + PADDLE_W
        right_face = LOGICAL_W - PADDLE_MARGIN - PADDLE_W
        if bx - BALL_SIZE / 2 <= left_face and abs(by - pl) < PADDLE_H / 2 and bvx < 0:
            bvx = abs(bvx)
            bvy += (by - pl) * 0.04
        if bx + BALL_SIZE / 2 >= right_face and abs(by - pr) < PADDLE_H / 2 and bvx > 0:
            bvx = -abs(bvx)
            bvy += (by - pr) * 0.04

        if bx < -60 or bx > LOGICAL_W + 60:
            direction = -1 if bx > LOGICAL_W else 1
            bx, by = LOGICAL_W / 2, LOGICAL_H / 2
            bvx = BALL_SPEED_X * direction
            bvy = BALL_SPEED_Y

        # Per-calendar tint lookup.
        cals_for_render = _events.get("by_calendar", [])
        cal_color_0 = (cals_for_render[0].get("color")
                       if len(cals_for_render) > 0 else None)
        cal_color_1 = (cals_for_render[1].get("color")
                       if len(cals_for_render) > 1 else None)
        faint = P["MAUVE"]
        # Blit the pre-rendered static layer, then layer the dynamic
        # bits. The cal0/cal1 calendar tints and the weather/identity
        # group washes are CLOCK-VIEW ONLY — in calendar view they'd
        # fight the per-group event/empty wash that runs further down.
        surf.blit(dash_static_surf, (0, 0))
        clock_view = (not dashboard_mode) or view_mode == "clock"
        # The cal0/cal1/weather/identity dynamic tile washes are gone
        # in both modes — the M-F first-week strip (rendered further
        # down) carries the calendar signal via per-day group washes.
        for i, (x, y, w, h) in enumerate(empty_tile_rects):
            if x <= bx <= x + w and y <= by <= y + h:
                tile_flash[i] = now
        for i, (x, y, w, h) in enumerate(empty_tile_rects):
            elapsed = now - tile_flash[i]
            if elapsed >= TILE_FLASH_DUR:
                continue
            boost = int(TILE_FLASH_ALPHA_BOOST
                        * (1 - elapsed / TILE_FLASH_DUR))
            flash_buf.fill((0, 0, 0, 0))
            pygame.draw.rect(flash_buf, (*faint, boost),
                             (0, 0, w, h),
                             border_radius=HL_RADIUS)
            surf.blit(flash_buf, (x, y))

        # Central clock — re-rendered only when the displayed time
        # changes. Measure colon centre offset so the blit pins the
        # colon to the lattice's central column.
        cur_clock = time.strftime("%H:%M" if CLOCK_24H else "%-I:%M")
        if cur_clock != clock_str:
            clock_str = cur_clock
            clock_surf = clock_font.render(clock_str, True, P["ACCENT"])
            colon_idx = clock_str.find(":")
            w_left = clock_font.size(clock_str[:colon_idx])[0]
            w_through = clock_font.size(clock_str[:colon_idx + 1])[0]
            clock_colon_offset = (w_left + w_through) // 2
        cur_dayline = time.strftime("%a %d %b").upper()
        if cur_dayline != dayline_str:
            dayline_str = cur_dayline
            # Render "MON 15 JUN" as three glyph runs so the date
            # numeral can be white (echoing today's white tile in the
            # calendar) while the day-of-week and month name stay in
            # the recessive MAUVE_FADE.
            def _split_dayline_render(font_):
                parts = dayline_str.split(" ", 2)
                if len(parts) != 3:
                    return font_.render(
                        dayline_str, True, P["MAUVE_FADE"])
                dow = font_.render(parts[0] + " ", True, P["MAUVE_FADE"])
                num = font_.render(parts[1], True, WHITE_TEXT)
                mon = font_.render(" " + parts[2], True, P["MAUVE_FADE"])
                w = dow.get_width() + num.get_width() + mon.get_width()
                h = max(dow.get_height(), num.get_height(),
                        mon.get_height())
                comp = pygame.Surface((w, h), pygame.SRCALPHA)
                x = 0
                for s in (dow, num, mon):
                    comp.blit(s, (x, (h - s.get_height()) // 2))
                    x += s.get_width()
                return comp
            dayline_surf = _split_dayline_render(dayline_font)
            # Header-strip version (compact, ui_font sized).
            hdr_dayline_surf = _split_dayline_render(ui_font)
        # Date line folds into the weather composite, so the composite
        # rebuilds whenever the date rolls over too.
        cur_weather_key = (_weather["text"], dayline_str)
        if cur_weather_key != weather_key:
            weather_key = cur_weather_key
            temp_text, _date_key = cur_weather_key
            if temp_text:
                INLINE_GAP = 12  # horizontal gap between temp ↔ city
                temp_surf = dash_temp_font.render(
                    temp_text, True, P["AUBURN"])
                # Header-strip temp (compact, ui_font sized).
                hdr_temp_surf = ui_font.render(
                    temp_text, True, P["AUBURN"])
                row_tc = [temp_surf]
                if city_surf is not None:
                    row_tc.append(city_surf)
                row_tc_w = (sum(s.get_width() for s in row_tc)
                            + INLINE_GAP * (len(row_tc) - 1))
                row_tc_h = max(s.get_height() for s in row_tc)
                # Pin the dayline to the centre of the top mini-row
                # and temp+city to the centre of the bottom mini-row
                # inside the weather content rect. Vertical padding
                # falls out of the lattice rhythm.
                _, _, _, content_h = dash_content_rects["weather"]
                top_cy = DASH_MINI_SIZE // 2
                bot_cy = (DASH_MINI_SIZE + DASH_MINI_GAP
                          + DASH_MINI_SIZE // 2)
                w = max(dayline_surf.get_width(), row_tc_w)
                weather_surf = pygame.Surface(
                    (w, content_h), pygame.SRCALPHA)
                weather_surf.blit(
                    dayline_surf,
                    (0, top_cy - dayline_surf.get_height() // 2))
                x = 0
                y = bot_cy - row_tc_h // 2
                for s in row_tc:
                    weather_surf.blit(
                        s, (x, y + (row_tc_h - s.get_height()) // 2))
                    x += s.get_width() + INLINE_GAP
            else:
                weather_surf = None
                hdr_temp_surf = None

        # Clock floats over its lattice region; colon pinned to the
        # central lattice column (= rect centre x), digits spread
        # proportionally around it. Hidden in calendar view so the
        # date numerals own the lattice without focal competition.
        if not dashboard_mode or view_mode == "clock":
            cx, cy, cw, ch = dash_content_rects["clock"]
            center_x = cx + cw // 2
            surf.blit(clock_surf,
                      (center_x - clock_colon_offset,
                       cy + (ch - clock_surf.get_height()) // 2))

        col0_text_x = dash_content_rects["cal0"][0] + TILE_INSET
        _cy = lambda k: (dash_content_rects[k][1]
                         + dash_content_rects[k][3] // 2)

        # (Lock mode previously rendered the focal weather composite
        # into the weather tile and the user@host + 2-column design
        # chip into the identity tile here. Both moved out: the header
        # strip above the lattice carries the weather + identity now,
        # and the 4-day outlook fills the tile interiors.)

        # Calendar-style date strip — same rendering in both modes:
        # full 5×4 rolling grid in dashboard calendar view; just the
        # first M-F row in dashboard clock view AND lock mode, so the
        # week-ahead reads the same wherever pong runs.
        if True:
            # Rebuild date_blits on day rollover so the window stays
            # anchored on the current/upcoming Monday.
            cur_today = _dt.date.today()
            cur_anchor = _calendar_anchor(cur_today)
            # Rebuild on either anchor flip (week rollover) or just the
            # date rolling forward inside the same workweek — the latter
            # is what moves the white "today" highlight Tue→Wed→Thu→Fri.
            if cur_anchor != cal_anchor_date or cur_today != last_today:
                cal_anchor_date = cur_anchor
                last_today = cur_today
                date_blits = _build_date_blits(cal_anchor_date, last_today)
            # Invalidate the event-label cache when the calendar fetcher
            # publishes a new version.
            cur_ver = _events.get("version", 0)
            if cur_ver != events_seen_version:
                events_seen_version = cur_ver
                event_label_cache.clear()
            by_date = _events.get("by_date", {})
            # Clock view shows just the first M-F row of the rolling
            # grid; calendar view shows all 4 weeks. Same rendering
            # path either way — only the slice differs.
            blits_to_render = (date_blits[:len(DATE_COLS)]
                               if view_mode == "clock" else date_blits)
            # 3×2 group wash behind each date tile: mauve for event
            # days (matches the existing baked-in wash on the left
            # column's weather/identity tiles), bg-tone for empty days
            # so they recede. Pass runs first so the date numerals +
            # event labels render on top.
            GROUP_W_PX = 3 * DASH_MINI_SIZE + 2 * DASH_MINI_GAP
            GROUP_H_PX = 2 * DASH_MINI_SIZE + 1 * DASH_MINI_GAP
            # Per-calendar wash priority: Off (peacock), ant (flamingo)
            # and Jog (pistachio) tint the group wash with their own
            # colour so those days stand out individually; other event
            # days use generic mauve; empty days recede to BG. Off goes
            # first so an OFF day overrides anything overlapping.
            WASH_PRIORITY = ("Off", "ant", "Jog")
            for d, _ds, _dxp, _dyp, tx, ty in blits_to_render:
                evs = by_date.get(d, [])
                wash = None
                for cal_name in WASH_PRIORITY:
                    hit = next((c for c, n, _s, _t in evs
                                if n == cal_name), None)
                    if hit is not None:
                        wash = hit
                        break
                if wash is None:
                    wash = P["MAUVE"] if evs else P["BG"]
                # Day-focus ladder: ≥2 past < yesterday < baseline <
                # tomorrow < today. The eye lands on now → next first;
                # past days recede into context. Date numerals and event
                # labels stay full colour either way.
                delta_days = (d - cur_today).days
                if delta_days == 0:
                    a = min(255, TILE_BG_ALPHA + TILE_BG_TODAY_BOOST)
                elif delta_days == 1:
                    a = min(255, TILE_BG_ALPHA + TILE_BG_TOMORROW_BOOST)
                elif delta_days == -1:
                    a = max(0, TILE_BG_ALPHA - TILE_BG_PAST1_DROP)
                elif delta_days < -1:
                    a = max(0, TILE_BG_ALPHA - TILE_BG_PAST2_DROP)
                else:
                    a = TILE_BG_ALPHA
                _tile_bg_rect(surf, tx - 2 * pitch, ty,
                              GROUP_W_PX, GROUP_H_PX,
                              wash, a)

            MAX_LABELS_PER_TILE = 4
            MAX_CHARS = 11        # roughly what fits at 14px bold sans
            LABEL_W_MAX = DASH_MINI_SIZE - 6
            def _past_fg_alpha(dd):
                if dd >= 0:
                    return 255
                if dd == -1:
                    return TILE_FG_PAST1
                if dd == -2:
                    return TILE_FG_PAST2
                if dd == -3:
                    return TILE_FG_PAST3
                return TILE_FG_PAST4
            for d, ds, dxp, dyp, tx, ty in blits_to_render:
                # Glyph alpha applied per-surface via set_alpha — the
                # cached surfaces are shared across tiles, so we
                # explicitly reset to fg_alpha before every blit (set
                # state, then blit immediately). Future + today get 255.
                fg_alpha = _past_fg_alpha((d - cur_today).days)
                ds.set_alpha(fg_alpha)
                surf.blit(ds, (dxp, dyp))
                events_today = by_date.get(d, [])
                if not events_today:
                    continue
                # Reserve the last visible slot for "+N" when total
                # events exceed the visible cap, so the overflow count
                # is always visible.
                total = len(events_today)
                if total > MAX_LABELS_PER_TILE:
                    show_count = MAX_LABELS_PER_TILE - 1
                else:
                    show_count = total
                text_y = dyp + ds.get_height() + 2
                drawn = 0
                for color, nm, summary, _ts in events_today[:show_count]:
                    raw = (summary or nm).upper()[:MAX_CHARS]
                    label_color = color or P["MUTED"]
                    cache_key = (raw, label_color)
                    label = event_label_cache.get(cache_key)
                    if label is None:
                        label = event_font.render(raw, True, label_color)
                        event_label_cache[cache_key] = label
                    if label.get_width() > LABEL_W_MAX:
                        # Fallback for unusually wide glyphs at small char
                        # budgets — shouldn't normally fire.
                        label = pygame.transform.scale(
                            label,
                            (LABEL_W_MAX, label.get_height()))
                    if (drawn > 0
                            and text_y + label.get_height()
                            > ty + DASH_MINI_SIZE):
                        break  # no room for this label
                    label.set_alpha(fg_alpha)
                    surf.blit(
                        label,
                        (tx + (DASH_MINI_SIZE - label.get_width()) // 2,
                         text_y))
                    text_y += label.get_height() + 1
                    drawn += 1
                leftover = total - drawn
                if leftover > 0:
                    more_raw = f"+{leftover}"
                    more_key = (more_raw, P["MUTED"])
                    more_label = event_label_cache.get(more_key)
                    if more_label is None:
                        more_label = event_font.render(
                            more_raw, True, P["MUTED"])
                        event_label_cache[more_key] = more_label
                    if text_y + more_label.get_height() <= ty + DASH_MINI_SIZE:
                        more_label.set_alpha(fg_alpha)
                        surf.blit(
                            more_label,
                            (tx + (DASH_MINI_SIZE
                                   - more_label.get_width()) // 2,
                             text_y))

        # Header strip above the lattice — same in both modes. Fixed
        # lattice-aligned anchors:
        #   * oobn@oobn identity flush left at lat_x
        #   * fizx palette chunk left-aligned to the 2nd 3×2 group (col 3)
        #   * upleb palette chunk left-aligned to the 4th 3×2 group (col 9)
        #   * current date (dayline) centred on the lattice
        #   * temp + city left-aligned to the 5th 3×2 group (col 12, Friday)
        HDR_GAP = 16
        hdr_y = view_btn_cy
        surf.blit(host_surf,
                  (lat_x, hdr_y - host_surf.get_height() // 2))
        fizx_chunk = palette_chunk_surfs["fizx"]
        upleb_chunk = palette_chunk_surfs["upleb"]
        surf.blit(fizx_chunk,
                  (lat_x + 3 * pitch,
                   hdr_y - fizx_chunk.get_height() // 2))
        surf.blit(upleb_chunk,
                  (lat_x + 9 * pitch,
                   hdr_y - upleb_chunk.get_height() // 2))
        if hdr_dayline_surf is not None:
            surf.blit(
                hdr_dayline_surf,
                (lat_x + lat_w // 2 - hdr_dayline_surf.get_width() // 2,
                 hdr_y - hdr_dayline_surf.get_height() // 2))
        weather_chunks = [s for s in (hdr_temp_surf, city_surf)
                          if s is not None]
        cur_x = lat_x + 12 * pitch
        for s in weather_chunks:
            surf.blit(s, (cur_x, hdr_y - s.get_height() // 2))
            cur_x += s.get_width() + HDR_GAP

        # Tiny view-toggle button on the lattice outskirts (top-right).
        # Each state filled with its own logical hue: ACCENT for clock
        # view (matches the focal clock tone), MAUVE for calendar view
        # (matches the calendar-day group-wash tone). Click area is
        # slightly larger than the visible circle (see event handler).
        if dashboard_mode:
            btn_color = P["ACCENT"] if view_mode == "clock" else P["MAUVE"]
            cx, cy = view_btn_cx, view_btn_cy
            if view_mode == "clock":
                # Dashboard icon: 2×2 grid of small filled rounded
                # squares — reads as "tiled layout / dashboard". Drawn
                # in ACCENT to echo the focal clock tone.
                cell = 9
                # 2-pixel gap centred on (cx, cy) — top-left starts at
                # -(cell+1), top-right at +1, same for y.
                for x in (cx - cell - 1, cx + 1):
                    for y in (cy - cell - 1, cy + 1):
                        pygame.draw.rect(
                            surf, btn_color, (x, y, cell, cell),
                            border_radius=2)
            else:
                # Calendar icon: rounded body + header divider + two
                # binder pegs above. Drawn in MAUVE to echo the
                # calendar-day group-wash tone.
                body = pygame.Rect(cx - 11, cy - 7, 22, 20)
                pygame.draw.rect(
                    surf, btn_color, body, width=2, border_radius=3)
                pygame.draw.line(
                    surf, btn_color,
                    (body.left + 2, body.top + 7),
                    (body.right - 2, body.top + 7), 2)
                pygame.draw.rect(
                    surf, btn_color,
                    (cx - 7, body.top - 4, 3, 5))
                pygame.draw.rect(
                    surf, btn_color,
                    (cx + 4, body.top - 4, 3, 5))

        surf.blit(paddle_surf,
                  (PADDLE_MARGIN, int(pl - PADDLE_H / 2)))
        surf.blit(paddle_surf,
                  (LOGICAL_W - PADDLE_MARGIN - PADDLE_W, int(pr - PADDLE_H / 2)))
        # Ball is invisible in both modes — the lattice flashes carry
        # the signal of its position. Physics + flashes still drive
        # the paddles and the tile interaction.

        # Password input strip — lock mode only. The dark wash baked
        # into the 5-tile region (cols 5-9, row 6) is the visual cue
        # for where the field lives; no keyboard graphic is needed.
        # SPACE still wakes the prompt; asterisks + warning bar
        # appear when typing; feedback text stacks above.
        if not dashboard_mode:
            if typing:
                t = font.render("*" * len(typed) + "_", True, P["MAUVE"])
                input_y = input_cy - t.get_height() // 2
                surf.blit(t, (input_cx - t.get_width() // 2, input_y))
                remaining = max(0.0, typing_until - now)
                if remaining <= INPUT_WARN_SEC:
                    bar_x = input_cx - INPUT_BAR_WIDTH // 2
                    bar_y = input_y + t.get_height() + 6
                    pygame.draw.rect(surf, P["AUBURN"],
                                     (bar_x, bar_y,
                                      int(INPUT_BAR_WIDTH * (remaining / INPUT_WARN_SEC)),
                                      INPUT_BAR_HEIGHT))
            if feedback and now < feedback_until:
                t = small.render(feedback, True, P["ALERT"])
                fb_y = input_cy - t.get_height() // 2
                if typing:
                    fb_y -= font.get_height() + 6
                surf.blit(t, (input_cx - t.get_width() // 2, fb_y))

        if dashboard_mode:
            sw, sh = screen.get_size()
            scale = min(sw / LOGICAL_W, sh / LOGICAL_H)
            scaled_w = max(1, int(LOGICAL_W * scale))
            scaled_h = max(1, int(LOGICAL_H * scale))
            scaled = pygame.transform.smoothscale(surf, (scaled_w, scaled_h))
            screen.fill(P["BG"])
            screen.blit(scaled,
                        ((sw - scaled_w) // 2, (sh - scaled_h) // 2))
            pygame.display.flip()
        else:
            tex.update(surf)
            ren.clear()
            for dst in dst_rects:
                tex.draw(dstrect=dst)
            ren.present()

        # Dashboard is ambient — 30fps halves CPU + heat without any
        # perceptible motion loss. Lock mode stays at 60fps because the
        # user is actively looking at it (and at the input strip).
        clock.tick(30 if dashboard_mode else 60)

    # Cleanup (grab release, mouse restore, pygame.quit) is registered
    # via atexit by install_emergency_cleanup, so it runs on this clean
    # exit path as well as on crashes / SIGTERM.


if __name__ == "__main__":
    sys.exit(main())
