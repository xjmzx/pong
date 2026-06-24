#!/usr/bin/env bash
# Build a drag-to-Applications .dmg for the Pong Dashboard .app.
#
# Prefers `create-dmg` (brew install create-dmg) for a laid-out window with
# positioned icons + Applications drop link. Falls back to plain `hdiutil`
# (always present) which still ships an /Applications symlink so the user
# can drag the app across — just without the custom window background.
#
# Usage: make-dmg.sh <app-path> <version> <out-dir>
#   defaults: dist/Pong Dashboard.app  0.0.0  dist
set -euo pipefail

APP="${1:-dist/Pong Dashboard.app}"
VERSION="${2:-0.0.0}"
OUTDIR="${3:-dist}"

VOLNAME="Pong Dashboard"
APPNAME="$(basename "$APP")"          # "Pong Dashboard.app"
ARCH="$(uname -m)"                    # arm64 / x86_64
DMG="$OUTDIR/PongDashboard-${VERSION}-macos-${ARCH}.dmg"

if [ ! -d "$APP" ]; then
  echo "error: app not found at '$APP' — run 'make app' first" >&2
  exit 1
fi
mkdir -p "$OUTDIR"
rm -f "$DMG"

# Stage a folder holding only the .app — both DMG builders take a source
# directory and copy its whole contents into the image.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/"

if command -v create-dmg >/dev/null 2>&1; then
  echo "==> building DMG with create-dmg (laid-out window)"
  # create-dmg adds the Applications drop link itself, so the stage holds
  # only the .app. It exits non-zero on a benign AppleScript timeout even
  # when the .dmg was written, so tolerate that and verify the file after.
  create-dmg \
    --volname "$VOLNAME" \
    --window-pos 200 120 \
    --window-size 640 360 \
    --icon-size 128 \
    --icon "$APPNAME" 160 185 \
    --app-drop-link 480 185 \
    --no-internet-enable \
    "$DMG" "$STAGE" || true
else
  echo "==> create-dmg not found — falling back to hdiutil"
  echo "    (tip: 'brew install create-dmg' for a laid-out window)"
  # Drag-to-Applications affordance without create-dmg: a symlink to
  # /Applications sitting next to the app inside the image.
  ln -s /Applications "$STAGE/Applications"
  hdiutil create \
    -volname "$VOLNAME" \
    -srcfolder "$STAGE" \
    -fs HFS+ \
    -format UDZO \
    -ov \
    "$DMG" >/dev/null
fi

if [ ! -f "$DMG" ]; then
  echo "error: DMG was not produced at '$DMG'" >&2
  exit 1
fi
echo ""
echo "Built: $DMG ($(du -h "$DMG" | cut -f1))"
echo "Attach this to a GitHub Release. Unsigned: on first launch the user"
echo "right-clicks the app -> Open, or runs:"
echo "  xattr -dr com.apple.quarantine '/Applications/$APPNAME'"
