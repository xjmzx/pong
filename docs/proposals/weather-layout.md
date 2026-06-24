# Proposal: weather layout — current cities (shipped) + lookahead slot (handoff)

> Author: Claude (macOS session) · Status: input for Linux layout lead · First use of the cross-session template.

## What
Live current-temperature readout for up to three cities in the left weather tile (shipped 0.4.1–0.4.4); hand off the reserved right-flank `lookahead` slot to the Linux layout lead, with a proposed direction below.

## Surface
dashboard, clock view only (`if dashboard_mode and view_mode == "clock"`). Not in calendar view (that region is the week-3 date row), not in lock mode.

## Tile / region
- **Shipped:** `dash_content_rects["weather"]` — left column cell (0,2), `_lat_rect(0, 4, 3, 2)`.
- **Handoff:** `dash_content_rects["lookahead"]` — right flank `_lat_rect(lat_cols - 3, clock_r, 3, 2)`, reserved by Linux in `fbade97`, no renderer yet.

## Data sources consumed
- Shipped: `_weather["cities"]` — `list[(LABEL, temp_str)]`, one per `WEATHER_LOCATIONS` (default `["Hanoi","Edinburgh","Beijing"]`; `[0]` is primary and also feeds the header temp + `_weather["sun"]`/`["moon"]`).
- Fetch: primary city pulled in full (`%t|%S|%s|%m`), temp reused; others temp-only (`%t`). A city that fails a cycle keeps its last reading. Errors log `weather/fetch-fail`.
- For the lookahead slot, **not yet consumed:** wttr.in's JSON endpoint (`wttr.in/<loc>?format=j1`) exposes a 3-day hourly forecast — that's the natural source if the slot shows a forecast.

## Cache key
Shipped: `weather_cities_surf` rebuilt only when `tuple(_weather["cities"])` changes. The lookahead renderer should mirror this — a tuple of whatever forecast fields it consumes, gating one cached surface.

## Platforms affected
all (shared `pong_lock.py`); render path is dashboard-only so not lock-gated.

## Platforms tested
macOS (source + bundled .dmg, 0.4.4). Cities resolve live against wttr.in (Hanoi 39° / Edinburgh 17° / Beijing 29° at time of writing); tile renders correctly after the 0.4.2 opaque-surface fix.

## Platforms NOT tested
- **Needs-verify: Linux** — the left weather tile in lock mode is not reachable (dashboard-gated), so no lock-mode exposure; but confirm the dashboard tile renders identically under X11.
- **Needs-verify: Windows** — font metrics for the right-aligned temp (`weather_temp_font`, Google Sans Flex fallback) at the 40px tier; label/temp could collide if the fallback face is wider.

## Shipped layout (left tile)
```
┌────────┐
│HANOI 39│   city label left  (UBUNTU 28px, white)
│EDINB 17│   temp right        (clock face 40px, P["AUBURN"])
│BEIJI 29│   3 rows, evenly distributed in the 3×2 tile
└────────┘
```

## Handoff: proposed direction for the `lookahead` slot (Linux owns the call)
The left tile is **now** (current temps, 3 cities). The natural complement on the right flank is **next** — keeping the same now/next rhythm the calendar already uses (today's wash vs the week-ahead grid). Options, lightest first:

1. **Primary-city short forecast** — next 2–3 days hi/lo (+ icon/condition) for `WEATHER_LOCATIONS[0]`, from the `j1` JSON endpoint. Simplest "lookahead", one extra fetch.
2. **Sunrise/sunset/moon** — already fetched (`_weather["sun"]`, `_weather["moon"]`) but only shown in the header strip; the slot could surface them larger. Zero new data.
3. **Per-city sparkline** — a tiny temp trend per tracked city. Most work; probably over-scoped for a 3×2.

My weak preference: **(1)** for the forward-looking read, with **(2)** as the zero-cost fallback if a forecast fetch feels heavy. Either way the renderer should follow the shipped pattern: build a cached surface keyed on its data tuple, blit into `dash_content_rects["lookahead"]`, gated `if dashboard_mode and view_mode == "clock"`.

## macOS verification I'll do once the renderer lands
Captured-frame check (the temporary `PONG_SHOT` hook pattern) confirming the new slot composites opaque — the logical surfaces are 32-bit-pinned since 0.4.2 so the RESIZABLE alpha-format bug shouldn't recur, but worth a look.
