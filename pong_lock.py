#!/usr/bin/env python3
"""Ambient Pong dashboard for Ubuntu/X11.

Two modes share one binary:
- `pong` (default): full-screen lock that mirrors across every connected
  monitor and authenticates against the real login password via PAM.
- `pong --dashboard`: same dashboard rendered in a resizable, regular
  window. No PAM, no keyboard grab, single monitor. Close the window or
  press Esc/Q to quit.
"""

import datetime as _dt
import fcntl
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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
# All perimeter-tile text (cal chips, temp, sun, host, net, city/source
# labels) renders through a single ui_font at one size + regular weight,
# Ubuntu-first stack. Centre cluster (CLOCK/DAY/DATE) keeps its own fonts.
UI_FONT_SIZE = 14
# --- Dashboard / lock divergences (track here) ---
# As the dashboard variant evolves it will intentionally drift from the
# lock screen. Every delta lives behind a `dashboard_mode` gate; keep
# this list in sync as new deltas land so the two modes stay legible.
#
#   * UI font     — DASH_UI_FONT_SIZE = 24  (lock: UI_FONT_SIZE = 14)
#   * Grid        — 5×4 (GRID_COLS_DASH) instead of lock's 4×4. CELL_W
#                   shrinks to LOGICAL_W//5 = 384 so the perimeter is
#                   14 uniform chip slots wrapping a 3×2 clock centred
#                   at col 2. Lock keeps its 2×2 clock + 12 perimeter
#                   slots, with col 3 intentionally empty
#   * DAY DATE    — large mono-bold focal line in lock
#                   (DAYLINE_FONT_SIZE = 120) at the merged (1-2, 0)
#                   span. In dashboard mode the text shrinks to
#                   DASH_UI_FONT_SIZE and is folded into the weather
#                   composite at (0, 2) as the first line of the stack.
#                   The original (2, 0) DATE slot is currently a free
#                   tile awaiting reassignment.
#   * Clock span  — 2×2 at (1, 1) in lock; 3×2 at (1, 1) in dashboard
#   * Layout      — lock keeps a 4×4 cell grid. Dashboard now drops the
#                   per-column cell concept entirely and tiles a
#                   uniform square mini-tile lattice (DASH_MINI_SIZE)
#                   across the active area. Content tiles (cal0, cal1,
#                   weather, identity, clock) are explicit lattice
#                   regions stored in `dash_content_rects`; every other
#                   lattice cell is a square mini-tile in
#                   `empty_tile_rects`, indexed 0..N for reference
#   * Pong ball   — invisible in dashboard mode. Physics still drives
#                   the tile-flash interaction; paddles stay visible
#                   as the only pong vestige
#   * Ball flash  — each empty mini-tile bumps its alpha briefly when
#                   the pong ball's centre passes through it; decays
#                   over TILE_FLASH_DUR. The flash registry lives in
#                   the `tile_flash` list parallel to empty_tile_rects
#   * Clock size  — lock uses CLOCK_FONT_SIZE = 260 as a fixed value. In
#                   dashboard the clock is uniformly scaled at startup
#                   to fill the 3×2 area with an inter-tile-gap margin
#                   (2 × HL_INSET) on every side; aspect is preserved
#   * Clock layer — in lock the clock sits inside a single 2×2 framed
#                   tile. In dashboard the 6 cells under the clock are
#                   themselves sub-divided into 3×2 mini-tiles (so the
#                   underlying lattice continues unbroken), and the
#                   clock surface renders later as its own visual
#                   layer on top — no framing tile of its own.
#   * Cell (1-2,3) — empty input strip with bg + outline in lock; in
#                   dashboard mode the bottom row is split into 5
#                   regular chip cells (the input slot is gone)
#   * No PAM, no keyboard grab, no mouse hide, no input handling
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
CALENDAR_LOOKAHEAD_DAYS = 7     # only surface events within this window

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
            _fetch_weather_once()
            time.sleep(WEATHER_REFRESH_SEC)
    t = threading.Thread(target=loop, daemon=True)
    t.start()


_events = {"by_calendar": []}  # [{name, color, next: {start, summary} or None}]


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


def _fetch_calendars_once():
    """Pull each ICS, expand recurrences, capture each calendar's soonest."""
    if not HAS_ICS:
        return  # parse deps not installed; silently skip
    calendars = _load_calendars()
    if not calendars:
        return

    now = _dt.datetime.now(_dt.timezone.utc)
    until = now + _dt.timedelta(days=CALENDAR_LOOKAHEAD_DAYS)
    results = []
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
                for ev in recurring_ical_events.of(ical).between(now, until):
                    start = ev.get("DTSTART").dt
                    if (isinstance(start, _dt.date)
                            and not isinstance(start, _dt.datetime)):
                        start = _dt.datetime.combine(
                            start, _dt.time.min, tzinfo=_dt.timezone.utc)
                    elif start.tzinfo is None:
                        start = start.replace(tzinfo=_dt.timezone.utc)
                    if start < now:
                        continue
                    summary = str(ev.get("SUMMARY", "")).strip()
                    location = str(ev.get("LOCATION", "")).strip()
                    if next_event is None or start < next_event["start"]:
                        next_event = {"start": start,
                                      "summary": summary,
                                      "location": location}
            except Exception:
                pass  # per-calendar failure; still surface the colour+name
        results.append({"name": name, "color": color, "next": next_event})
    _events["by_calendar"] = results


def start_calendar_thread():
    def loop():
        while True:
            _fetch_calendars_once()
            time.sleep(CALENDAR_REFRESH_SEC)
    t = threading.Thread(target=loop, daemon=True)
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

    if not dashboard_mode and PAM is None:
        sys.stderr.write(
            "Missing dep: install with `sudo apt install python3-pam`\n")
        return 1

    # Mode-specific grid: dashboard uses 5 cols so the clock can be a
    # centred 3×2 block. Module-level CELL_W is overwritten so every
    # helper (cell_center, draw_tile_bg, draw_highlight, draw_grid)
    # reads the right value via name lookup.
    global GRID_COLS, CELL_W
    if dashboard_mode:
        GRID_COLS = GRID_COLS_DASH
        CELL_W = LOGICAL_W // GRID_COLS_DASH

    # Dashboard lattice: uniform square mini-tiles tiled across the
    # active area (between paddle clearance + dock-style top/bottom
    # inset), centred. Content tiles (4 left chips + clock) overlay
    # specific regions of the lattice; their cells STILL appear in the
    # empty_tile_rects list so the ball-flash continues uninterrupted
    # under the clock layer. Only the left-column content cells are
    # excluded (those are fully opaque content tiles).
    empty_tile_rects = []
    dash_content_rects = {}  # "cal0" | "cal1" | "weather" | "identity" | "clock" -> (x,y,w,h)
    if dashboard_mode:
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

        # Content regions in lattice coords. Left column: 4 tiles
        # stacked, each 3 cols × 2 rows. Clock: 4 digits × 2 cols + a
        # 1-col colon = 9 lattice cols, 2 lattice rows tall (each digit
        # is drawn within a 2×2 tile boundary — stage-2 work will turn
        # the glyphs into tile-block characters). 15-col lattice keeps
        # the clock perfectly centred while leaving the right 3 cols
        # free for future content.
        for i, key in enumerate(("cal0", "cal1", "weather", "identity")):
            dash_content_rects[key] = _lat_rect(0, i * 2, 3, 2)
        clock_cols = 9
        clock_rows = 2
        clock_c = (lat_cols - clock_cols) // 2
        clock_r = (lat_rows - clock_rows) // 2
        dash_content_rects["clock"] = _lat_rect(
            clock_c, clock_r, clock_cols, clock_rows)

        for r in range(lat_rows):
            for c in range(lat_cols):
                empty_tile_rects.append(
                    (lat_x + c * pitch, lat_y + r * pitch,
                     DASH_MINI_SIZE, DASH_MINI_SIZE))
    tile_flash = [0.0] * len(empty_tile_rects)

    _lock_fp = acquire_single_instance(  # noqa: F841 (kept open for flock)
        DASH_LOCK_FILE if dashboard_mode else LOCK_FILE)
    _ensure_theme_config()
    theme_name = _resolve_theme()
    P.update(build_palette(theme_name))

    pygame.init()
    pygame.font.init()
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

    surf = pygame.Surface((LOGICAL_W, LOGICAL_H))
    # Pre-render the static dashboard layer once: bg + grid + all
    # 112 mini-tile bgs + mini-tile outlines + 4 content tile outlines.
    # Per-frame work then drops to ~one full-window blit plus dynamic
    # overlays (cal tints, flashes, clock, text, paddles). Big perf win
    # over re-allocating ~112 SRCALPHA surfaces every frame.
    dash_static_surf = None
    flash_buf = None
    if dashboard_mode:
        dash_static_surf = pygame.Surface((LOGICAL_W, LOGICAL_H))
        dash_static_surf.fill(P["BG"])
        _faint_static = P["MAUVE"]
        for (x, y, w, h) in empty_tile_rects:
            _tile_bg_rect(dash_static_surf, x, y, w, h,
                          _faint_static, TILE_BG_FAINT_ALPHA)
            _highlight_rect(dash_static_surf, x, y, w, h)
        # Slight extra mauve wash over the weather + identity content
        # regions so the 6-mini-tile group reads as a coherent panel
        # rather than disappearing into the lattice. Same alpha as
        # cal tiles' tint (TILE_BG_ALPHA) so the four content tiles
        # carry equal visual weight. Cal tiles get their per-calendar
        # tint applied dynamically per frame.
        for key in ("weather", "identity"):
            _tile_bg_rect(dash_static_surf, *dash_content_rects[key],
                          _faint_static, TILE_BG_ALPHA)
        for key in ("cal0", "cal1", "weather", "identity"):
            _highlight_rect(dash_static_surf,
                            *dash_content_rects[key])
        # Reusable buffer for flash overlays (one alloc, refilled per
        # active flash).
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

    if dashboard_mode:
        clock_font = _make_dash_clock_font(CLOCK_FONT_SIZE)
        # Grow the clock to fill its lattice region with a margin equal
        # to the inter-tile gap on every side. Aspect preserved — pick
        # the smaller of the width-fit and height-fit scales.
        margin = DASH_MINI_GAP
        _, _, cw, ch = dash_content_rects["clock"]
        avail_w = cw - 2 * margin
        avail_h = ch - 2 * margin
        probe_w, probe_h = clock_font.size("88:88")
        scale = min(avail_w / probe_w, avail_h / probe_h)
        if scale > 1.0:
            clock_font = _make_dash_clock_font(int(CLOCK_FONT_SIZE * scale))
    else:
        clock_font = pygame.font.SysFont(MONO_STACK, CLOCK_FONT_SIZE, bold=True)
    # Dashboard focal fonts: share the clock's Google Sans Flex Bold
    # face so the focal stack (clock, DAY DATE, temp) rhymes. Temp has
    # its own larger tier.
    dash_focal_font = (_make_dash_clock_font(DASH_FOCAL_FONT_SIZE)
                       if dashboard_mode else None)
    dash_temp_font = (_make_dash_clock_font(DASH_TEMP_FONT_SIZE)
                      if dashboard_mode else None)
    if dashboard_mode:
        dayline_font = dash_focal_font
    else:
        dayline_font = pygame.font.SysFont(
            MONO_STACK, DAYLINE_FONT_SIZE, bold=True)
    ui_font = pygame.font.SysFont(
        UBUNTU_STACK,
        DASH_UI_FONT_SIZE if dashboard_mode else UI_FONT_SIZE)
    # Static attribution labels — city for the temp tile, service for the
    # sun tile. Pre-rendered once since neither value changes at runtime.
    # City label sits inline with the now-larger temp — keep it small.
    city_font = pygame.font.SysFont(
        UBUNTU_STACK,
        DASH_LABEL_FONT_SIZE if dashboard_mode else UI_FONT_SIZE)
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

    clock = pygame.time.Clock()
    running = True

    while running:
        now = time.time()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
                continue
            if dashboard_mode:
                if ev.type == pygame.VIDEORESIZE:
                    screen = pygame.display.set_mode(
                        ev.size, pygame.RESIZABLE)
                elif ev.type == pygame.KEYDOWN and ev.key in (
                        pygame.K_ESCAPE, pygame.K_q):
                    running = False
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

        # Per-calendar tint lookup (shared by both modes — drawn as an
        # overlay in dashboard, baked into the cell bg in lock).
        cals_for_render = _events.get("by_calendar", [])
        cal_color_0 = (cals_for_render[0].get("color")
                       if len(cals_for_render) > 0 else None)
        cal_color_1 = (cals_for_render[1].get("color")
                       if len(cals_for_render) > 1 else None)
        faint = P["MAUVE"]
        if dashboard_mode:
            # Dashboard: blit the pre-rendered static layer (bg + grid +
            # all 112 mini-tile bgs/outlines + content tile outlines).
            # Then layer dynamic stuff: cal tints (translucent over
            # mini-tiles), flash overlays, and content text below.
            surf.blit(dash_static_surf, (0, 0))
            if cal_color_0 is not None:
                _tile_bg_rect(surf, *dash_content_rects["cal0"],
                              cal_color_0, TILE_BG_ALPHA)
            if cal_color_1 is not None:
                _tile_bg_rect(surf, *dash_content_rects["cal1"],
                              cal_color_1, TILE_BG_ALPHA)
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
        else:
            surf.fill(P["BG"])
            draw_grid(surf)
            # 4×4 lock layout — unchanged.
            draw_tile_bg(surf, 0, 0, color=cal_color_0, left=PADDLE_CLEAR)
            draw_tile_bg(surf, 0, 1, color=cal_color_1, left=PADDLE_CLEAR)
            draw_tile_bg(surf, 1, 0, colspan=2, color=faint,
                         alpha=TILE_BG_FAINT_ALPHA)              # DAY DATE
            draw_tile_bg(surf, 1, 1, colspan=2, rowspan=2,
                         color=faint, alpha=TILE_BG_FAINT_ALPHA)  # CLOCK
            draw_tile_bg(surf, 1, 3, colspan=2, color=faint,
                         alpha=TILE_BG_INPUT_ALPHA)              # input
            draw_tile_bg(surf, 0, 2, color=faint,
                         alpha=TILE_BG_FAINT_ALPHA,
                         left=PADDLE_CLEAR)
            draw_tile_bg(surf, 0, 3, color=faint,
                         alpha=TILE_BG_FAINT_ALPHA,
                         left=PADDLE_CLEAR)
            draw_highlight(surf, 0, 0, left=PADDLE_CLEAR)
            draw_highlight(surf, 0, 1, left=PADDLE_CLEAR)
            draw_highlight(surf, 0, 2, left=PADDLE_CLEAR)
            draw_highlight(surf, 0, 3, left=PADDLE_CLEAR)
            draw_highlight(surf, 1, 0, colspan=2)               # DAY DATE
            draw_highlight(surf, 1, 1, colspan=2, rowspan=2)    # CLOCK 2×2
            draw_highlight(surf, 1, 3, colspan=2)               # input strip

        # Central clock — re-rendered only when the displayed time changes.
        cur_clock = time.strftime("%H:%M" if CLOCK_24H else "%-I:%M")
        if cur_clock != clock_str:
            clock_str = cur_clock
            clock_surf = clock_font.render(clock_str, True, P["ACCENT"])
            if dashboard_mode:
                # Measure the colon's horizontal centre inside the
                # rendered surface so we can pin it to the lattice's
                # central column rather than the surface midpoint.
                colon_idx = clock_str.find(":")
                w_left = clock_font.size(clock_str[:colon_idx])[0]
                w_through = clock_font.size(clock_str[:colon_idx + 1])[0]
                clock_colon_offset = (w_left + w_through) // 2
        cur_dayline = time.strftime("%a %d %b").upper()
        if cur_dayline != dayline_str:
            dayline_str = cur_dayline
            dayline_surf = dayline_font.render(dayline_str, True,
                                               P["MAUVE_FADE"])
        # Dashboard mode folds the date line into the weather composite
        # (the standalone DATE tile at (2,0) is empty for now), so the
        # composite rebuilds whenever the date rolls over too.
        cur_weather_key = (_weather["text"],
                           dayline_str if dashboard_mode else None)
        if cur_weather_key != weather_key:
            weather_key = cur_weather_key
            temp_text, _date_key = cur_weather_key
            if temp_text:
                INLINE_GAP = 12  # horizontal gap between temp ↔ city
                temp_font = (dash_temp_font
                             if dashboard_mode else ui_font)
                temp_surf = temp_font.render(temp_text, True, P["AUBURN"])
                row_tc = [temp_surf]
                if city_surf is not None:
                    row_tc.append(city_surf)
                row_tc_w = (sum(s.get_width() for s in row_tc)
                            + INLINE_GAP * (len(row_tc) - 1))
                row_tc_h = max(s.get_height() for s in row_tc)
                if dashboard_mode and dayline_surf is not None:
                    # Pin the dayline to the centre of the top mini-row
                    # and the temp+city row to the centre of the bottom
                    # mini-row inside the content rect. Vertical padding
                    # then falls out of the lattice rhythm.
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
                    weather_surf = pygame.Surface(
                        (row_tc_w, row_tc_h), pygame.SRCALPHA)
                    x = 0
                    for s in row_tc:
                        weather_surf.blit(
                            s, (x, (row_tc_h - s.get_height()) // 2))
                        x += s.get_width() + INLINE_GAP
            else:
                weather_surf = None

        # Cell-centred blit helper.
        def blit_cell(s, col, row, colspan=1, rowspan=1):
            cx, cy = cell_center(col, row, colspan, rowspan)
            surf.blit(s, (cx - s.get_width() // 2, cy - s.get_height() // 2))

        # CLOCK — 2×2 in lock, 3×2 in dashboard (col 2 is the centre
        # column of the 5-col grid). DAY DATE sits as a single-cell
        # chip at col 2 in dashboard, or the merged (1,0)+(2,0) span
        # in lock.
        if dashboard_mode:
            # Clock floats over its lattice region; colon is pinned to
            # the central lattice column (= rect centre x), digits
            # spread proportionally around it.
            cx, cy, cw, ch = dash_content_rects["clock"]
            center_x = cx + cw // 2
            surf.blit(clock_surf,
                      (center_x - clock_colon_offset,
                       cy + (ch - clock_surf.get_height()) // 2))
        else:
            blit_cell(clock_surf, 1, 1, colspan=2, rowspan=2)
            blit_cell(dayline_surf, 1, 0, colspan=2)

        # Shared left-column text-left anchor + per-tile centre lookups.
        if dashboard_mode:
            col0_text_x = dash_content_rects["cal0"][0] + TILE_INSET
            _cy = lambda k: (dash_content_rects[k][1]
                             + dash_content_rects[k][3] // 2)
        else:
            col0_text_x = PADDLE_CLEAR + TILE_INSET
            _cm = {"cal0": (0, 0), "cal1": (0, 1),
                   "weather": (0, 2), "identity": (0, 3)}
            _cy = lambda k: cell_center(*_cm[k])[1]

        # Weather composite (with DATE in dashboard mode), flush-left.
        if weather_surf is not None:
            cy = _cy("weather")
            surf.blit(weather_surf,
                      (col0_text_x, cy - weather_surf.get_height() // 2))

        # Identity + design chip — two lines: user@host above the
        # design profile (theme + key hexes).
        nc_y = _cy("identity")
        row_gap = 16
        chip_h = host_surf.get_height() + row_gap + design_surf.get_height()
        host_y = nc_y - chip_h // 2
        design_y = host_y + host_surf.get_height() + row_gap
        left_x = col0_text_x
        surf.blit(host_surf, (left_x, host_y))
        surf.blit(design_surf, (left_x, design_y))

        # Per-calendar chips — each pinned to its assigned cell. Name is
        # rendered in the calendar's tint (matches the frame colour). If
        # an event exists, NEXT / time / summary stack underneath.
        cal_cells = [(0, 0), (0, 1)]
        for i, (col, row) in enumerate(cal_cells):
            if i >= len(cals_for_render):
                continue
            cal = cals_for_render[i]
            name_color = cal.get("color") or P["MAUVE"]
            name_surf = ui_font.render(
                cal["name"].upper(), True, name_color)
            time_surf = None
            location_surf = None
            summary_surf = None
            if cal["next"]:
                local_start = cal["next"]["start"].astimezone()
                time_text = local_start.strftime(
                    "%a %d %b | %H:%M").upper()
                summary_text = cal["next"]["summary"].upper()[:EVENT_LABEL_MAX]
                time_surf = ui_font.render(
                    time_text, True, WHITE_TEXT)
                summary_surf = ui_font.render(
                    summary_text, True, WHITE_TEXT)
                loc_text = (cal["next"].get("location", "")
                            .upper()[:EVENT_LABEL_MAX])
                if loc_text:
                    location_surf = ui_font.render(
                        loc_text, True, WHITE_TEXT)
            # Each row is a list of surfs laid left-to-right; row height
            # = max of the surfs in it. First row combines the cal name
            # with " / NEXT" so the header sits inline with the title.
            if time_surf is None:
                rows = [[name_surf]]
            else:
                rows = [[name_surf, event_header_surf], [time_surf]]
                if location_surf is not None:
                    rows.append([location_surf])
                rows.append([summary_surf])
            row_heights = [max(s.get_height() for s in r) for r in rows]
            stacked_h = sum(row_heights) + 4 * (len(rows) - 1)
            cy = _cy("cal0" if i == 0 else "cal1")
            left_x = col0_text_x
            y = cy - stacked_h // 2
            for row, rh in zip(rows, row_heights):
                x = left_x
                for s in row:
                    surf.blit(s, (x, y + (rh - s.get_height()) // 2))
                    x += s.get_width()
                y += rh + 4

        surf.blit(paddle_surf,
                  (PADDLE_MARGIN, int(pl - PADDLE_H / 2)))
        surf.blit(paddle_surf,
                  (LOGICAL_W - PADDLE_MARGIN - PADDLE_W, int(pr - PADDLE_H / 2)))
        # Ball runs the physics + tile-flash driver but is invisible in
        # dashboard mode — the lattice flashes carry the signal now.
        if not dashboard_mode:
            surf.blit(ball_surf,
                      (int(bx - BALL_SIZE / 2), int(by - BALL_SIZE / 2)))

        # Input strip lives in its own 2×1 tile below the clock; everything
        # related (asterisks, warning bar, kb hint, feedback) centres on it.
        # In dashboard mode the tile renders as a reserved empty slot — the
        # background fill + outline already drew above.
        if not dashboard_mode:
            input_cx, input_cy = cell_center(1, 3, colspan=2)
            if typing:
                t = font.render("*" * len(typed) + "_", True, P["MAUVE"])
                input_y = input_cy - t.get_height() // 2
                surf.blit(t, (input_cx - t.get_width() // 2, input_y))
                # Progress underline — only appears in the last INPUT_WARN_SEC
                # seconds, shrinking to 0 as the timeout approaches. Auburn.
                remaining = max(0.0, typing_until - now)
                if remaining <= INPUT_WARN_SEC:
                    bar_x = input_cx - INPUT_BAR_WIDTH // 2
                    bar_y = input_y + t.get_height() + 6
                    pygame.draw.rect(surf, P["AUBURN"],
                                     (bar_x, bar_y,
                                      int(INPUT_BAR_WIDTH * (remaining / INPUT_WARN_SEC)),
                                      INPUT_BAR_HEIGHT))
            else:
                # Mini-keyboard hint — Ctrl Alt SPACE Alt Ctrl, SPACE highlighted.
                kx = input_cx - kb_total_w // 2
                ky = input_cy - KB_KEY_H // 2
                for w, label_surf, color in kb_keys:
                    pygame.draw.rect(surf, color, (kx, ky, w, KB_KEY_H),
                                     width=1, border_radius=KB_RADIUS)
                    if label_surf is not None:
                        surf.blit(label_surf,
                                  (kx + (w - label_surf.get_width()) // 2,
                                   ky + (KB_KEY_H - label_surf.get_height()) // 2))
                    kx += w + KB_GAP
            if feedback and now < feedback_until:
                t = small.render(feedback, True, P["ALERT"])
                # Anchor to tile centre; stack above the input when both shown.
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

        clock.tick(60)

    if not dashboard_mode:
        pygame.event.set_grab(False)
    pygame.mouse.set_visible(True)
    pygame.quit()


if __name__ == "__main__":
    sys.exit(main())
