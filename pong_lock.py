#!/usr/bin/env python3
"""Ambient Pong screen lock for Ubuntu/X11. Mirrors across all connected displays."""

import datetime as _dt
import fcntl
import getpass
import json
import os
import re
import socket
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
PADDLE_W, PADDLE_H = 14, 120
PADDLE_MARGIN = 60
BALL_SIZE = 14
BALL_SPEED_X = 7.0
BALL_SPEED_Y = 4.2
PADDLE_SPEED = 5.5
MAX_PASSWORD_LEN = 256
CLOCK_FONT_SIZE = 260           # central clock height in logical px
DAY_FONT_SIZE = 120             # 3-letter day (sans) — paired with DATE
DATE_FONT_SIZE = 120            # date (mono) — paired with DAY
WEATHER_FONT_SIZE = 72          # temperature chip below the date
SUN_FONT_SIZE = 36              # sunrise/sunset row
DASH_FONT_SIZE = 26             # corner dashboard chips
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

# ndisc-suite fizx palette — channel triples mirrored from
# ~/code_gh/xjmzx/ndisc.smpl/src/index.css :root.
PAL_BG       = (9, 13, 18)      # near-black navy field
PAL_PANEL    = (13, 17, 23)     # subtle tile fill for perimeter cells
PAL_FG       = (240, 246, 252)  # cool white — kept for high-contrast fallbacks
PAL_MUTED    = (107, 122, 141)  # cool grey-blue (currently unused; kept for ref)
PAL_ACCENT   = (122, 240, 205)  # mint — focal clock
PAL_MAUVE    = (167, 139, 250)  # light purple — paddles/ball + secondary text
PAL_MAUVE_DIM = (67, 56, 100)   # dimmed mauve — SPACE wake-tile
PAL_AUBURN   = (178, 96, 58)    # warm rust — weather
PAL_OK       = (74, 222, 128)   # green — online indicator
PAL_ALERT    = (248, 113, 113)  # red — offline indicator, errors
PAL_NET      = (40, 35, 60)     # 4×4 main grid lines
PAL_GRID_SUB = (22, 20, 35)     # 16×16 sub-grid lines (graph-paper feel)
WEATHER_LOCATION = "Hanoi"      # wttr.in location string; "" for IP-based
WEATHER_REFRESH_SEC = 1800      # 30 min between fetches
WEATHER_TIMEOUT_SEC = 6         # fetch timeout
NET_REFRESH_SEC = 10            # network connectivity poll interval
EVENT_HEADER_FONT_SIZE = 20     # "NEXT" header on the event chip
EVENT_TIME_FONT_SIZE = 40       # next-event time line
EVENT_LABEL_FONT_SIZE = 26      # next-event label line
EVENT_LABEL_MAX = 14            # truncate event labels to this many chars
CALENDAR_CONFIG = os.path.expanduser("~/.config/pong/calendars.json")
CALENDAR_REFRESH_SEC = 600      # 10 min between ICS fetches
CALENDAR_LOOKAHEAD_DAYS = 7     # only surface events within this window

# 4×4 dashboard grid. Clock occupies the centre 2×2; perimeter cells
# carry the dashboard chips. Pong stays full-screen on top so paddles
# still mark the screen edges.
GRID_COLS = 4
GRID_ROWS = 4
CELL_W = LOGICAL_W // GRID_COLS
CELL_H = LOGICAL_H // GRID_ROWS
SUB_GRID_DIV = 4                # each 4×4 cell subdivided 4 ways → 16×16
HL_INSET = 10                   # gap between cell edge and highlight frame
HL_RADIUS = 14                  # highlight corner radius
HL_WIDTH = 1                    # highlight outline thickness

STATE_FILE = os.path.expanduser("~/.cache/pong_lock_state")
LOCK_FILE = os.path.expanduser("~/.cache/pong_lock.lock")


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
    y = row * CELL_H + (rowspan * CELL_H) // 2
    return x, y


def draw_highlight(surf, col, row, colspan=1, rowspan=1, color=None):
    """Inset, rounded outline that groups a cell or cell-span as one unit.
    Pass `color` to override the default dim mauve (e.g. calendar tint)."""
    x = col * CELL_W + HL_INSET
    y = row * CELL_H + HL_INSET
    w = colspan * CELL_W - 2 * HL_INSET
    h = rowspan * CELL_H - 2 * HL_INSET
    pygame.draw.rect(surf, color if color is not None else PAL_NET,
                     (x, y, w, h),
                     width=HL_WIDTH, border_radius=HL_RADIUS)


def draw_grid(surf):
    """Two-tier grid: faint 16×16 graph-paper sub-grid + 4×4 main grid.
    Perimeter cells take a panel-tone fill; centre 2×2 (clock) stays BG."""
    # Perimeter tile fills — every 4x4 cell except the centre 2x2.
    for col in range(GRID_COLS):
        for row in range(GRID_ROWS):
            if 1 <= col <= 2 and 1 <= row <= 2:
                continue
            pygame.draw.rect(surf, PAL_PANEL,
                             (col * CELL_W, row * CELL_H, CELL_W, CELL_H))
    # Sub-grid (16×16) — drawn first so main grid overlays cleanly.
    sub_cols = GRID_COLS * SUB_GRID_DIV
    sub_rows = GRID_ROWS * SUB_GRID_DIV
    for c in range(1, sub_cols):
        if c % SUB_GRID_DIV == 0:
            continue
        x = c * LOGICAL_W // sub_cols
        pygame.draw.line(surf, PAL_GRID_SUB, (x, 0), (x, LOGICAL_H), 1)
    for r in range(1, sub_rows):
        if r % SUB_GRID_DIV == 0:
            continue
        y = r * LOGICAL_H // sub_rows
        pygame.draw.line(surf, PAL_GRID_SUB, (0, y), (LOGICAL_W, y), 1)
    # Main grid (4×4) — brighter, drawn on top.
    for c in range(1, GRID_COLS):
        pygame.draw.line(surf, PAL_NET, (c * CELL_W, 0),
                         (c * CELL_W, LOGICAL_H), 1)
    for r in range(1, GRID_ROWS):
        pygame.draw.line(surf, PAL_NET, (0, r * CELL_H),
                         (LOGICAL_W, r * CELL_H), 1)


_weather = {"text": "", "sun": "", "moon": ""}
_net = {"online": False, "ssid": ""}


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
            _weather["sun"] = f"↑ {sunrise[:5]}  ↓ {sunset[:5]}"
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


def _check_online():
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2):
            return True
    except OSError:
        return False


def _read_ssid():
    for cmd in (["iwgetid", "-r"], ["nmcli", "-t", "-f", "active,ssid",
                                    "dev", "wifi"]):
        try:
            out = subprocess.check_output(cmd, text=True,
                                          stderr=subprocess.DEVNULL,
                                          timeout=2).strip()
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            continue
        if cmd[0] == "iwgetid":
            if out:
                return out
        else:
            for line in out.splitlines():
                if line.startswith("yes:"):
                    return line[4:]
    return ""


def start_net_thread():
    def loop():
        while True:
            _net["online"] = _check_online()
            _net["ssid"] = _read_ssid() if _net["online"] else ""
            time.sleep(NET_REFRESH_SEC)
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
    rects = get_displays() or [(0, 0, 1920, 1080)]

    pygame.init()
    pygame.font.init()
    pygame.mouse.set_visible(False)
    start_weather_thread()
    start_net_thread()
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
    clock_font = pygame.font.SysFont(MONO_STACK, CLOCK_FONT_SIZE, bold=True)
    day_font = pygame.font.SysFont(SANS_STACK, DAY_FONT_SIZE, bold=True)
    date_font = pygame.font.SysFont(MONO_STACK, DATE_FONT_SIZE, bold=True)
    weather_font = pygame.font.SysFont(MONO_STACK, WEATHER_FONT_SIZE, bold=True)
    sun_font = pygame.font.SysFont(MONO_STACK, SUN_FONT_SIZE)
    dash_font = pygame.font.SysFont(SANS_STACK, DASH_FONT_SIZE)
    dash_mono = pygame.font.SysFont(MONO_STACK, DASH_FONT_SIZE)
    event_header_font = pygame.font.SysFont(SANS_STACK,
                                            EVENT_HEADER_FONT_SIZE, bold=True)
    event_header_surf = event_header_font.render("NEXT", True, PAL_MAUVE)
    # Pre-render the keyboard hint pieces. Bottom-row only: Ctrl Alt SPACE Alt Ctrl.
    kb_label_font = pygame.font.SysFont(SANS_STACK, KB_LABEL_FONT_SIZE,
                                        bold=True)
    kb_ctrl_label = kb_label_font.render("Ctrl", True, PAL_MAUVE_DIM)
    kb_alt_label = kb_label_font.render("Alt", True, PAL_MAUVE_DIM)
    kb_total_w = 4 * KB_MOD_W + 4 * KB_GAP + KB_SPACE_W
    # Each key: (width, label_surface_or_none, border_color)
    kb_keys = [
        (KB_MOD_W, kb_ctrl_label, PAL_MAUVE_DIM),
        (KB_MOD_W, kb_alt_label,  PAL_MAUVE_DIM),
        (KB_SPACE_W, None,        PAL_MAUVE),
        (KB_MOD_W, kb_alt_label,  PAL_MAUVE_DIM),
        (KB_MOD_W, kb_ctrl_label, PAL_MAUVE_DIM),
    ]
    event_time_font = pygame.font.SysFont(MONO_STACK, EVENT_TIME_FONT_SIZE,
                                          bold=True)
    event_label_font = pygame.font.SysFont(SANS_STACK, EVENT_LABEL_FONT_SIZE)
    clock_str = ""
    clock_surf = None
    day_str = ""
    day_surf = None
    date_str = ""
    date_surf = None
    weather_str = ""
    weather_surf = None
    sun_str = ""
    sun_surf = None
    net_key = (None, None)
    net_surf = None
    host_surf = dash_mono.render(user_host, True, PAL_MAUVE)

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

        surf.fill(PAL_BG)
        draw_grid(surf)
        # Look up per-calendar tints before drawing highlights so the two
        # calendar cells can be framed in their respective colours.
        cals_for_render = _events.get("by_calendar", [])
        cal_color_0 = (cals_for_render[0].get("color")
                       if len(cals_for_render) > 0 else None)
        cal_color_1 = (cals_for_render[1].get("color")
                       if len(cals_for_render) > 1 else None)
        # Group highlights — inset rounded outlines that bind related cells.
        draw_highlight(surf, 0, 0)                              # user@host
        draw_highlight(surf, 1, 0, colspan=2)                   # DAY + DATE
        draw_highlight(surf, 3, 0)                              # network
        draw_highlight(surf, 1, 1, colspan=2, rowspan=2)        # CLOCK 2×2
        draw_highlight(surf, 0, 3, color=cal_color_0)           # cal[0]
        draw_highlight(surf, 1, 3)                              # temp
        draw_highlight(surf, 2, 3)                              # sun
        draw_highlight(surf, 3, 3, color=cal_color_1)           # cal[1]

        # Central clock — re-rendered only when the displayed time changes.
        cur_clock = time.strftime("%H:%M" if CLOCK_24H else "%-I:%M")
        if cur_clock != clock_str:
            clock_str = cur_clock
            clock_surf = clock_font.render(clock_str, True, PAL_ACCENT)
        cur_day = time.strftime("%a").upper()
        if cur_day != day_str:
            day_str = cur_day
            day_surf = day_font.render(day_str, True, PAL_MAUVE)
        cur_date = time.strftime("%-d %b").upper()
        if cur_date != date_str:
            date_str = cur_date
            date_surf = date_font.render(date_str, True, PAL_MAUVE)
        cur_weather = _weather["text"]
        if cur_weather != weather_str:
            weather_str = cur_weather
            weather_surf = (weather_font.render(weather_str, True, PAL_AUBURN)
                            if weather_str else None)
        cur_sun = _weather["sun"]
        if cur_sun != sun_str:
            sun_str = cur_sun
            sun_surf = (sun_font.render(sun_str, True, PAL_MAUVE)
                        if sun_str else None)
        cur_net = (_net["online"], _net["ssid"])
        if cur_net != net_key:
            net_key = cur_net
            label = "online" if cur_net[0] else "offline"
            if cur_net[1]:
                label = f"{label} · {cur_net[1]}"
            net_surf = dash_font.render(label, True, PAL_MAUVE)

        # Cell-centred blit helper.
        def blit_cell(s, col, row, colspan=1, rowspan=1):
            cx, cy = cell_center(col, row, colspan, rowspan)
            surf.blit(s, (cx - s.get_width() // 2, cy - s.get_height() // 2))

        # CLOCK — centre 2×2.
        blit_cell(clock_surf, 1, 1, colspan=2, rowspan=2)

        # Top row: user@host | DAY | DATE | ● net·SSID
        blit_cell(host_surf, 0, 0)
        blit_cell(day_surf, 1, 0)
        blit_cell(date_surf, 2, 0)
        # Net chip — dot + label rendered side-by-side, centred in cell (3,0).
        nc_x, nc_y = cell_center(3, 0)
        dot_r = 6
        gap = 10
        total_w = dot_r * 2 + gap + net_surf.get_width()
        dot_x = nc_x - total_w // 2 + dot_r
        pygame.draw.circle(surf, PAL_OK if net_key[0] else PAL_ALERT,
                           (dot_x, nc_y), dot_r)
        surf.blit(net_surf, (dot_x + dot_r + gap,
                             nc_y - net_surf.get_height() // 2))

        # Bottom row: JOG (cal[0]) | TEMP | SUN | TT (cal[1])
        if weather_surf is not None:
            blit_cell(weather_surf, 1, 3)
        if sun_surf is not None:
            blit_cell(sun_surf, 2, 3)

        # Per-calendar chips — each pinned to its assigned cell. Name is
        # rendered in the calendar's tint (matches the frame colour). If
        # an event exists, NEXT / time / summary stack underneath.
        cal_cells = [(0, 3), (3, 3)]
        for i, (col, row) in enumerate(cal_cells):
            if i >= len(cals_for_render):
                continue
            cal = cals_for_render[i]
            name_color = cal.get("color") or PAL_MAUVE
            name_surf = event_label_font.render(
                cal["name"].upper(), True, name_color)
            time_surf = None
            summary_surf = None
            if cal["next"]:
                local_start = cal["next"]["start"].astimezone()
                time_text = local_start.strftime("%a %H:%M")
                summary_text = cal["next"]["summary"].upper()[:EVENT_LABEL_MAX]
                time_surf = event_time_font.render(
                    time_text, True, PAL_MAUVE)
                summary_surf = event_label_font.render(
                    summary_text, True, PAL_MAUVE)
            parts = [name_surf]
            if time_surf is not None:
                parts.extend([event_header_surf, time_surf, summary_surf])
            stacked_h = (sum(p.get_height() for p in parts)
                         + 4 * (len(parts) - 1))
            cx, cy = cell_center(col, row)
            y = cy - stacked_h // 2
            surf.blit(name_surf, (cx - name_surf.get_width() // 2, y))
            y += name_surf.get_height() + 4
            if time_surf is not None:
                surf.blit(event_header_surf,
                          (cx - event_header_surf.get_width() // 2, y))
                y += event_header_surf.get_height() + 4
                surf.blit(time_surf,
                          (cx - time_surf.get_width() // 2, y))
                y += time_surf.get_height() + 4
                surf.blit(summary_surf,
                          (cx - summary_surf.get_width() // 2, y))

        pygame.draw.rect(surf, PAL_MAUVE,
                         (PADDLE_MARGIN, int(pl - PADDLE_H / 2), PADDLE_W, PADDLE_H))
        pygame.draw.rect(surf, PAL_MAUVE,
                         (LOGICAL_W - PADDLE_MARGIN - PADDLE_W, int(pr - PADDLE_H / 2),
                          PADDLE_W, PADDLE_H))
        pygame.draw.rect(surf, PAL_MAUVE,
                         (int(bx - BALL_SIZE / 2), int(by - BALL_SIZE / 2),
                          BALL_SIZE, BALL_SIZE))

        if typing:
            t = font.render("*" * len(typed) + "_", True, PAL_MAUVE)
            # Tuck the input just above the bottom of the centre 2×2.
            input_y = 3 * CELL_H - t.get_height() - 8
            surf.blit(t, (LOGICAL_W // 2 - t.get_width() // 2, input_y))
            # Progress underline — only appears in the last INPUT_WARN_SEC
            # seconds, shrinking to 0 as the timeout approaches. Auburn.
            remaining = max(0.0, typing_until - now)
            if remaining <= INPUT_WARN_SEC:
                bar_x = LOGICAL_W // 2 - INPUT_BAR_WIDTH // 2
                bar_y = input_y + t.get_height() + 6
                pygame.draw.rect(surf, PAL_AUBURN,
                                 (bar_x, bar_y,
                                  int(INPUT_BAR_WIDTH * (remaining / INPUT_WARN_SEC)),
                                  INPUT_BAR_HEIGHT))
        else:
            # Mini-keyboard hint — Ctrl Alt SPACE Alt Ctrl, SPACE highlighted.
            kx = LOGICAL_W // 2 - kb_total_w // 2
            ky = 3 * CELL_H - KB_KEY_H - 8
            for w, label_surf, color in kb_keys:
                pygame.draw.rect(surf, color, (kx, ky, w, KB_KEY_H),
                                 width=1, border_radius=KB_RADIUS)
                if label_surf is not None:
                    surf.blit(label_surf,
                              (kx + (w - label_surf.get_width()) // 2,
                               ky + (KB_KEY_H - label_surf.get_height()) // 2))
                kx += w + KB_GAP
        if feedback and now < feedback_until:
            t = small.render(feedback, True, (248, 113, 113))  # ndisc --c-alert
            # Anchor to the same band as input; stack above when both shown.
            fb_y = 3 * CELL_H - t.get_height() - 8
            if typing:
                fb_y -= font.get_height() + 6
            surf.blit(t, (LOGICAL_W // 2 - t.get_width() // 2, fb_y))

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
