#!/usr/bin/env python3
"""Ambient Pong screen lock for Ubuntu/X11. Mirrors across all connected displays."""

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
    sys.stderr.write("Missing dep: install with `sudo apt install python3-pam`\n")
    sys.exit(1)

# --- Tunables ---
MAX_ATTEMPTS = 3
COOLOFF_SECONDS = 15 * 60
PAM_SERVICE = "login"          # change to "passwd" or a custom service if PAM denies
INPUT_TIMEOUT = 8              # cancel password prompt if idle this long
LOGICAL_W, LOGICAL_H = 1920, 1080
PADDLE_W, PADDLE_H = 40, 120    # paddle width bumped to ~clock-font stroke weight
PADDLE_MARGIN = 60
BALL_SIZE = 40                  # ball matched to paddle weight (square, mint, ~50% alpha)
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
              "accent": (122, 240, 205), "mauve":  (167, 139, 250),
              "auburn": (178,  96,  58), "ok":     ( 74, 222, 128),
              "alert":  (248, 113, 113)},
    "upleb": {"bg":     ( 10,  15,  21), "panel":  ( 13,  17,  23),
              "fg":     (201, 209, 217), "muted":  (107, 122, 141),
              "accent": (255, 182, 158), "mauve":  (255, 179,  71),
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
TILE_INSET = 10                 # tile-edge → text-edge inset for L-aligned chips
TILE_BG_ALPHA = 38              # ~15% of 255 — calendar-tint tile-bg wash
TILE_BG_FAINT_ALPHA = 25        # ~10% of 255 — theme-mauve wash on plain tiles
TILE_BG_INPUT_ALPHA = 12        # ~5% of 255 — input strip, lighter still
PADDLE_GAP = 20                 # clear gap between paddle face and tile edge
# Col-0 tile edges sit at PADDLE_CLEAR so the rounded outline doesn't
# kiss the moving paddle. (Right column tiles, when added back, would
# mirror this via LOGICAL_W - PADDLE_CLEAR.)
PADDLE_CLEAR = PADDLE_MARGIN + PADDLE_W + PADDLE_GAP

STATE_FILE = os.path.expanduser("~/.cache/pong_lock_state")
LOCK_FILE = os.path.expanduser("~/.cache/pong_lock.lock")


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


def acquire_single_instance():
    """Refuse to launch a second pong over the first. Lock released on exit."""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    fp = open(LOCK_FILE, "w")
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
    calendars = _load_calendars()
    if not calendars:
        return
    try:
        import icalendar
        import recurring_ical_events
    except ImportError:
        return  # parse deps not installed yet; silently skip

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
                    if next_event is None or start < next_event["start"]:
                        next_event = {"start": start, "summary": summary}
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
    _lock_fp = acquire_single_instance()  # noqa: F841 (kept open for flock)
    _ensure_theme_config()
    theme_name = _resolve_theme()
    P.update(build_palette(theme_name))
    rects = get_displays() or [(0, 0, 1920, 1080)]

    pygame.init()
    pygame.font.init()
    pygame.mouse.set_visible(False)
    start_weather_thread()
    _ensure_calendar_config()
    start_calendar_thread()
    user_host = f"{getpass.getuser()}@{os.uname().nodename}"

    win, ren, tex, dst_rects = make_window(rects)
    pygame.event.set_grab(True)

    surf = pygame.Surface((LOGICAL_W, LOGICAL_H))
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
    clock_font = pygame.font.SysFont(MONO_STACK, CLOCK_FONT_SIZE, bold=True)
    dayline_font = pygame.font.SysFont(MONO_STACK, DAYLINE_FONT_SIZE, bold=True)
    ui_font = pygame.font.SysFont(UBUNTU_STACK, UI_FONT_SIZE)
    # Static attribution labels — city for the temp tile, service for the
    # sun tile. Pre-rendered once since neither value changes at runtime.
    city_surf = (ui_font.render(WEATHER_LOCATION.upper(), True,
                                P["MAUVE"])
                 if WEATHER_LOCATION else None)
    source_surf = ui_font.render("wttr.in", True, P["MAUVE"])
    event_header_surf = ui_font.render("NEXT", True, P["MAUVE"])
    # Design profile readout — theme + three signature hexes (accent /
    # mauve / auburn) so the active palette is legible on-screen.
    hex_rgb = lambda rgb: "#{:02X}{:02X}{:02X}".format(*rgb)
    design_str = " · ".join((theme_name.upper(),
                             hex_rgb(P["ACCENT"]),
                             hex_rgb(P["MAUVE"]),
                             hex_rgb(P["AUBURN"])))
    design_surf = ui_font.render(design_str, True, P["MAUVE"])
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
    dayline_str = ""
    dayline_surf = None
    weather_key = ("", ())
    weather_surf = None
    host_surf = ui_font.render(user_host, True, P["MAUVE"])
    # Paddles + ball: clock-tone at ~50% alpha so they sit one visual
    # step back from the focal clock. SRCALPHA surfaces because the main
    # `surf` is opaque — fill alpha is ignored on draw_rect against it.
    paddle_surf = pygame.Surface((PADDLE_W, PADDLE_H), pygame.SRCALPHA)
    paddle_surf.fill((*P["ACCENT"], PONG_ALPHA))
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

        surf.fill(P["BG"])
        draw_grid(surf)
        # Look up per-calendar tints before drawing highlights so the two
        # calendar cells can be framed in their respective colours.
        cals_for_render = _events.get("by_calendar", [])
        cal_color_0 = (cals_for_render[0].get("color")
                       if len(cals_for_render) > 0 else None)
        cal_color_1 = (cals_for_render[1].get("color")
                       if len(cals_for_render) > 1 else None)
        # Tile backdrops: cal tiles get their per-calendar tint at 15%;
        # everything else (DAY/DATE, CLOCK, input strip, weather, identity)
        # gets a faint theme-mauve wash at 10%. Drawn before highlights so
        # the neutral PAL_NET outline sits flush on top. fizx → cool violet,
        # upleb → warm orange — both fall out of P["MAUVE"] automatically.
        draw_tile_bg(surf, 0, 0, color=cal_color_0, left=PADDLE_CLEAR)
        draw_tile_bg(surf, 0, 1, color=cal_color_1, left=PADDLE_CLEAR)
        faint = P["MAUVE"]
        draw_tile_bg(surf, 1, 0, colspan=2, color=faint,
                     alpha=TILE_BG_FAINT_ALPHA)
        draw_tile_bg(surf, 1, 1, colspan=2, rowspan=2, color=faint,
                     alpha=TILE_BG_FAINT_ALPHA)
        draw_tile_bg(surf, 1, 3, colspan=2, color=faint,
                     alpha=TILE_BG_INPUT_ALPHA)
        draw_tile_bg(surf, 0, 2, color=faint, alpha=TILE_BG_FAINT_ALPHA,
                     left=PADDLE_CLEAR)
        draw_tile_bg(surf, 0, 3, color=faint, alpha=TILE_BG_FAINT_ALPHA,
                     left=PADDLE_CLEAR)
        # Group highlights — left column stacks the data chips
        # (cal[0]→cal[1]→temp→sun) top-to-bottom; bottom + right rows
        # carry the clock identity strip only.
        draw_highlight(surf, 0, 0, left=PADDLE_CLEAR)           # cal[0] (Jog)
        draw_highlight(surf, 1, 0, colspan=2)                   # DAY + DATE
        # (3,0) intentionally empty — right column reserved for future.
        draw_highlight(surf, 0, 1, left=PADDLE_CLEAR)           # cal[1] (TT)
        draw_highlight(surf, 1, 1, colspan=2, rowspan=2)        # CLOCK 2×2
        draw_highlight(surf, 1, 3, colspan=2)                   # input strip
        draw_highlight(surf, 0, 2, left=PADDLE_CLEAR)           # weather + sun
        draw_highlight(surf, 0, 3, left=PADDLE_CLEAR)           # identity

        # Central clock — re-rendered only when the displayed time changes.
        cur_clock = time.strftime("%H:%M" if CLOCK_24H else "%-I:%M")
        if cur_clock != clock_str:
            clock_str = cur_clock
            clock_surf = clock_font.render(clock_str, True, P["ACCENT"])
        cur_dayline = time.strftime("%a %d %b").upper()
        if cur_dayline != dayline_str:
            dayline_str = cur_dayline
            dayline_surf = dayline_font.render(dayline_str, True,
                                               P["MAUVE_FADE"])
        cur_weather_key = (_weather["text"], _weather["sun"])
        if cur_weather_key != weather_key:
            weather_key = cur_weather_key
            temp_text, sun_pair = cur_weather_key
            if temp_text:
                gap = 6
                parts = [ui_font.render(temp_text, True, P["AUBURN"])]
                if city_surf is not None:
                    parts.append(city_surf)
                if sun_pair:
                    parts.append(ui_font.render(sun_pair[0], True,
                                                P["MAUVE"]))
                    parts.append(ui_font.render(sun_pair[1], True,
                                                P["MAUVE"]))
                parts.append(source_surf)
                w = max(p.get_width() for p in parts)
                h = (sum(p.get_height() for p in parts)
                     + gap * (len(parts) - 1))
                weather_surf = pygame.Surface((w, h), pygame.SRCALPHA)
                y_off = 0
                for p in parts:
                    weather_surf.blit(p, (0, y_off))
                    y_off += p.get_height() + gap
            else:
                weather_surf = None

        # Cell-centred blit helper.
        def blit_cell(s, col, row, colspan=1, rowspan=1):
            cx, cy = cell_center(col, row, colspan, rowspan)
            surf.blit(s, (cx - s.get_width() // 2, cy - s.get_height() // 2))

        # CLOCK — centre 2×2.
        blit_cell(clock_surf, 1, 1, colspan=2, rowspan=2)

        # Top row: (0,0) empty | "MON 08 JUN" spanning (1,0)+(2,0) | (3,0) empty.
        blit_cell(dayline_surf, 1, 0, colspan=2)

        # Shared left-column text-left anchor (cal chips + weather +
        # identity all flush to this x).
        col0_text_x = PADDLE_CLEAR + TILE_INSET

        # Left column row 2: weather + sun + source composite, flush-left.
        if weather_surf is not None:
            cy = cell_center(0, 2)[1]
            surf.blit(weather_surf,
                      (col0_text_x, cy - weather_surf.get_height() // 2))

        # Identity + design chip at (0,3) — two lines: user@host above
        # the design profile (theme + key hexes).
        nc_y = cell_center(0, 3)[1]
        row_gap = 6
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
            summary_surf = None
            if cal["next"]:
                local_start = cal["next"]["start"].astimezone()
                time_text = local_start.strftime("%a %H:%M")
                summary_text = cal["next"]["summary"].upper()[:EVENT_LABEL_MAX]
                time_surf = ui_font.render(
                    time_text, True, P["MAUVE"])
                summary_surf = ui_font.render(
                    summary_text, True, P["MAUVE"])
            parts = [name_surf]
            if time_surf is not None:
                parts.extend([event_header_surf, time_surf, summary_surf])
            stacked_h = (sum(p.get_height() for p in parts)
                         + 4 * (len(parts) - 1))
            cy = cell_center(col, row)[1]
            left_x = PADDLE_CLEAR + TILE_INSET
            y = cy - stacked_h // 2
            surf.blit(name_surf, (left_x, y))
            y += name_surf.get_height() + 4
            if time_surf is not None:
                surf.blit(event_header_surf, (left_x, y))
                y += event_header_surf.get_height() + 4
                surf.blit(time_surf, (left_x, y))
                y += time_surf.get_height() + 4
                surf.blit(summary_surf, (left_x, y))

        surf.blit(paddle_surf,
                  (PADDLE_MARGIN, int(pl - PADDLE_H / 2)))
        surf.blit(paddle_surf,
                  (LOGICAL_W - PADDLE_MARGIN - PADDLE_W, int(pr - PADDLE_H / 2)))
        surf.blit(ball_surf,
                  (int(bx - BALL_SIZE / 2), int(by - BALL_SIZE / 2)))

        # Input strip lives in its own 2×1 tile below the clock; everything
        # related (asterisks, warning bar, kb hint, feedback) centres on it.
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

        tex.update(surf)
        ren.clear()
        for dst in dst_rects:
            tex.draw(dstrect=dst)
        ren.present()

        clock.tick(60)

    pygame.event.set_grab(False)
    pygame.mouse.set_visible(True)
    pygame.quit()


if __name__ == "__main__":
    sys.exit(main())
