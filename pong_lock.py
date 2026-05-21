#!/usr/bin/env python3
"""Ambient Pong screen lock for Ubuntu/X11. Mirrors across all connected displays."""

import getpass
import os
import re
import subprocess
import sys
import time

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
CLOCK_COLOR = (150, 150, 150)   # dim grey — sits behind the paddles/ball
CLOCK_24H = True                # False for 12-hour time

STATE_FILE = os.path.expanduser("~/.cache/pong_lock_state")


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
    rects = get_displays() or [(0, 0, 1920, 1080)]

    pygame.init()
    pygame.font.init()
    pygame.mouse.set_visible(False)

    win, ren, tex, dst_rects = make_window(rects)
    pygame.event.set_grab(True)

    surf = pygame.Surface((LOGICAL_W, LOGICAL_H))
    font = pygame.font.SysFont("monospace", 56)
    small = pygame.font.SysFont("monospace", 32)
    # Chunky typewriter face for the central clock; falls back if absent.
    clock_font = pygame.font.SysFont("courier 10 pitch,courier,dejavu sans mono",
                                     CLOCK_FONT_SIZE, bold=True)
    clock_str = ""
    clock_surf = None

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

        surf.fill((0, 0, 0))
        for y in range(0, LOGICAL_H, 24):
            pygame.draw.rect(surf, (40, 40, 40), (LOGICAL_W // 2 - 2, y, 4, 12))

        # Central clock — re-rendered only when the displayed time changes.
        cur_clock = time.strftime("%H:%M" if CLOCK_24H else "%I:%M").lstrip("0")
        if cur_clock != clock_str:
            clock_str = cur_clock
            clock_surf = clock_font.render(clock_str, True, CLOCK_COLOR)
        surf.blit(clock_surf, (LOGICAL_W // 2 - clock_surf.get_width() // 2,
                               LOGICAL_H // 2 - clock_surf.get_height() // 2))

        pygame.draw.rect(surf, (220, 220, 220),
                         (PADDLE_MARGIN, int(pl - PADDLE_H / 2), PADDLE_W, PADDLE_H))
        pygame.draw.rect(surf, (220, 220, 220),
                         (LOGICAL_W - PADDLE_MARGIN - PADDLE_W, int(pr - PADDLE_H / 2),
                          PADDLE_W, PADDLE_H))
        pygame.draw.rect(surf, (220, 220, 220),
                         (int(bx - BALL_SIZE / 2), int(by - BALL_SIZE / 2),
                          BALL_SIZE, BALL_SIZE))

        if typing:
            t = font.render("*" * len(typed) + "_", True, (220, 220, 220))
            surf.blit(t, (LOGICAL_W // 2 - t.get_width() // 2, LOGICAL_H - 220))
        if feedback and now < feedback_until:
            t = small.render(feedback, True, (220, 120, 120))
            surf.blit(t, (LOGICAL_W // 2 - t.get_width() // 2, LOGICAL_H - 140))

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
