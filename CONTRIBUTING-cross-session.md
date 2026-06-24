# Cross-session change contract — v1.0

> Status: ACCEPTED 2026-06-24. Linux session accepted the macOS redline as-is (the trimmed always-5 + optional rendering-3, the grep-able `Needs-verify:` convention, the §3 release gate, and the .deb-manual-upload-as-lock-mode-gate framing). Promoted from `docs/proposals/cross-session-contract.md` (history preserved). Amend in place from here; major changes get a new version header.

Two Claude sessions (Linux / macOS-Windows) ship `xjmzx/pong` in lockstep off one `main`. This is a light contract, not process for its own sake — it exists to stop one session shipping a change through a code path it can't run.

## 1. The one rule that matters: `Needs-verify`
Every shared change names the platforms it was **not** run on, with the specific path. Make it grep-able:

```
Needs-verify: linux   (lock-mode tex.update(surf) SDL2 path)
Needs-verify: windows (Google Sans Flex fallback width at 40px)
```

`git log --grep "Needs-verify"` is then the open-verification queue. A change with **no** Needs-verify line asserts the author ran every path it touches.

## 2. Commit-message header
**Always** (one line each, in the commit body) for any change beyond a localized same-platform bug fix:

- **What** — one sentence
- **Surface** — clock / calendar / lock / dashboard-only / all
- **Platforms affected** — all (shared `pong_lock.py`) / bundle-only (`sys.frozen`) / lock-only (X11/PAM)
- **Tested** — OS(es) actually run
- **Needs-verify** — see §1 (omit only if truly none)

**Only when the change draws something** add a rendering block — skip it for network/config/packaging changes:

- **Region** — `dash_content_rects[...]` key or lattice coords, or shared region (header strip / paddles)
- **Data sources** — `_weather["cities"]`, `by_calendar`, …
- **Cache key** — the tuple gating surface rebuild (mirror `weather_cities_surf`)

For net-new layout/features that want design review *before* code, open a `docs/proposals/*.md` instead (e.g. this file, `weather-layout.md`). For everything else, the commit body is enough — no PR overhead between trusted sessions.

## 3. Release gate (protects users)
Push to `main` freely; smoke-test on your platform first. **But do not put a change with an open `Needs-verify` on a release-critical path into a tagged release until the named session confirms.** Release-critical paths = lock mode (PAM/X11/`tex.update`), bundled `certifi` HTTPS, surface-format/alpha. Cosmetic or self-contained changes don't gate.

Verification reply = a follow-up commit `verified: <os> — <path>` (or a fix commit if broken).

## 4. The `.deb` is a manual gate, on purpose
CI builds **mac `.dmg` + win `.exe` only** (`release.yml` has no deb job). The Linux session builds + uploads `pong_<v>_all.deb` to the release **by hand** — and that's a feature, not overhead: the `.deb` is the *only* artifact that exercises lock mode (PAM, X11 grab, xrandr mirror, SDL2 `tex.update`). A human uploading it = a human verified those paths on real hardware. So: **a tag from the macOS session produces an intentionally incomplete release until Linux verifies + adds the `.deb`.** Don't automate this away.

## 5. Build/verification matrix (who validates what)

| Platform | Bundle | Modes | Verifies |
|---|---|---|---|
| Linux `.deb` | system install | lock + dashboard | PAM, X11 keyboard grab, xrandr multi-monitor mirror, SDL2 `tex.update(surf)`, system trust store |
| macOS `.dmg` | PyInstaller | dashboard only | `sys.frozen` certifi CA, RESIZABLE window surface format (0.4.2 alpha fix), `~/Library/{Application Support,Caches}/pong` paths |
| Windows `.exe` | PyInstaller | dashboard only | per-monitor DPI awareness, Google Sans Flex font fallback, `%APPDATA%`/`%LOCALAPPDATA%\pong` paths |

## 6. Cross-cutting risks (smoke-test on the platform that exercises the path)
- Shared `surf` / `dash_static_surf` allocation → lock-mode SDL2 path on **Linux**.
- Network/HTTPS code → bundled `certifi` on **mac/win** (silent failure if the CA store regresses; `weather/fetch-fail` / `calendar/fetch-fail` in the crash log are the canaries).
- `os.path.expanduser("~/...")` for state → use `_user_config_dir()` / `_user_cache_dir()`; never hardcode `~/.config` or `~/.cache`.
- pygame surface-format assumptions generally → the 0.4.2 RESIZABLE-alpha class of bug; new full-frame surfaces must be opaque (`Surface((W,H), 0, 32)`).

## Acceptance log
- **v0** (Linux, 2026-06-24) — initial proposal.
- **v0.1** (macOS, 2026-06-24, commit db7a9dd) — redline: trimmed template, formalized `Needs-verify:`, added release gate, reframed `.deb` upload as manual lock-mode verification act.
- **v1.0** (Linux, 2026-06-24) — accepted v0.1 as-is. No further open questions.
