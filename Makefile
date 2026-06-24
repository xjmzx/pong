DESTDIR =
# User install by default — no sudo, lands under ~/.local/{bin,share}
# which is already on PATH and on XDG_DATA_DIRS on a stock Ubuntu user
# session. Override (e.g. PREFIX=/usr/local) for a system-wide install,
# or use `make deb` which forces PREFIX=/usr internally.
PREFIX = $(HOME)/.local

# Debian packaging — `make deb` stages the install tree under build/
# and emits dist/pong_$(VERSION)_all.deb. Override MAINTAINER if you
# fork.
VERSION = 0.4.4
MAINTAINER = xjmzx <jabbanawanga@gmail.com>
PKG_NAME = pong_$(VERSION)_all
PKG_DIR = build/$(PKG_NAME)
DIST_DIR = dist

.PHONY: all install uninstall deps deb clean-deb help mac-venv mac-icon app dmg clean-app

# macOS dashboard .app build (PyInstaller). Dashboard mode only — lock
# mode is Linux/PAM-only and is never bundled.
MAC_VENV = .venv
MAC_SPEC = packaging/macos/pong-dashboard.spec
MAC_ICON = packaging/macos/pong.icns
MAC_ICONSET = packaging/macos/pong.iconset

all: help

install:
	install -D -m755 -- pong_lock.py "$(DESTDIR)$(PREFIX)/bin/pong"
	install -D -m644 -- pong.desktop "$(DESTDIR)$(PREFIX)/share/applications/pong.desktop"
	install -D -m644 -- pong-dash.desktop "$(DESTDIR)$(PREFIX)/share/applications/pong-dash.desktop"
	install -D -m644 -- icon.svg "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong.svg"
	install -D -m644 -- icon-dash.svg "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong-dash.svg"
	install -d "$(DESTDIR)$(PREFIX)/share/systemd/user"
	sed 's|@BINDIR@|$(PREFIX)/bin|g' pong-lock.service.in \
	  > "$(DESTDIR)$(PREFIX)/share/systemd/user/pong-lock.service"
	chmod 644 "$(DESTDIR)$(PREFIX)/share/systemd/user/pong-lock.service"

uninstall:
	rm -f -- "$(DESTDIR)$(PREFIX)/bin/pong"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/applications/pong.desktop"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/applications/pong-dash.desktop"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong.svg"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong-dash.svg"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/systemd/user/pong-lock.service"

deps:
	@echo "Installing pong runtime deps..."
	sudo apt install -y python3-pam python3-pygame python3-icalendar
	pip install --user --break-system-packages recurring-ical-events
	@echo "Done."

deb:
	rm -rf $(PKG_DIR)
	mkdir -p $(PKG_DIR)/DEBIAN $(DIST_DIR)
	$(MAKE) install DESTDIR=$(CURDIR)/$(PKG_DIR) PREFIX=/usr
	@printf '%s\n' \
	  "Package: pong" \
	  "Version: $(VERSION)" \
	  "Section: x11" \
	  "Priority: optional" \
	  "Architecture: all" \
	  "Depends: python3 (>= 3.8), python3-pam, python3-pygame, python3-icalendar" \
	  "Recommends: python3-recurring-ical-events" \
	  "Maintainer: $(MAINTAINER)" \
	  "Homepage: https://github.com/xjmzx/pong" \
	  "Description: Ambient Pong screen lock + dashboard for Ubuntu/X11" \
	  " Two AI paddles auto-play across a 4x4 ambient dashboard of clock," \
	  " day, date, weather, sunrise/sunset, and Google Calendar readouts." \
	  " Authenticates against the real login password via PAM. Mirrors" \
	  " across multiple monitors." \
	  " ." \
	  " Run \`pong --dashboard\` to view the same dashboard in a regular" \
	  " resizable window with no lock and no PAM dependency." \
	  " ." \
	  " Deterrent-level lock: VT-switch and SSH still work." \
	  > $(PKG_DIR)/DEBIAN/control
	dpkg-deb --root-owner-group --build $(PKG_DIR) $(DIST_DIR)/$(PKG_NAME).deb
	@echo ""
	@echo "Built: $(DIST_DIR)/$(PKG_NAME).deb"
	@echo "Install with: sudo apt install ./$(DIST_DIR)/$(PKG_NAME).deb"

clean-deb:
	rm -rf build $(DIST_DIR)

# --- macOS dashboard .app -------------------------------------------------
# Build flow:  make mac-venv   (once: venv + pygame/icalendar/pyinstaller)
#              make app        (icon + PyInstaller bundle -> dist/)
# Requires macOS with python3 and librsvg (`brew install librsvg`).

mac-venv:
	python3 -m venv $(MAC_VENV)
	$(MAC_VENV)/bin/pip install --quiet --upgrade pip
	$(MAC_VENV)/bin/pip install --quiet pygame icalendar recurring-ical-events certifi pyinstaller
	@echo "venv ready: $(MAC_VENV)"

mac-icon: $(MAC_ICON)

$(MAC_ICON): icon-dash.svg
	rm -rf $(MAC_ICONSET)
	mkdir -p $(MAC_ICONSET)
	for sz in 16 32 128 256 512; do \
	  rsvg-convert -w $$sz -h $$sz icon-dash.svg -o $(MAC_ICONSET)/icon_$${sz}x$${sz}.png; \
	  d=$$((sz*2)); \
	  rsvg-convert -w $$d -h $$d icon-dash.svg -o $(MAC_ICONSET)/icon_$${sz}x$${sz}@2x.png; \
	done
	iconutil -c icns $(MAC_ICONSET) -o $(MAC_ICON)
	rm -rf $(MAC_ICONSET)

app: $(MAC_ICON)
	$(MAC_VENV)/bin/pyinstaller --noconfirm --distpath dist --workpath build/pyi $(MAC_SPEC)
	@echo ""
	@echo "Built: dist/Pong Dashboard.app"
	@echo "Run:   open 'dist/Pong Dashboard.app'   (or drag it into /Applications)"

# Drag-to-Applications .dmg for a release. Builds the .app first.
# Prefers create-dmg (laid-out window); falls back to hdiutil.
dmg: app
	packaging/macos/make-dmg.sh "dist/Pong Dashboard.app" "$(VERSION)" dist

clean-app:
	rm -rf build/pyi dist/"Pong Dashboard.app" dist/PongDashboard-*.dmg $(MAC_ICONSET)

help:
	@echo "Available make targets:"
	@echo "  deps       - Install runtime dependencies (apt + pip)"
	@echo "  install    - Install pong binary + .desktop + icon"
	@echo "  uninstall  - Remove installed files"
	@echo "  deb        - Build a .deb package at dist/$(PKG_NAME).deb"
	@echo "  clean-deb  - Remove build/ and dist/ artifacts"
	@echo "  mac-venv   - (macOS) Create .venv with pygame + pyinstaller"
	@echo "  app        - (macOS) Build dist/Pong Dashboard.app (run mac-venv first)"
	@echo "  dmg        - (macOS) Build a drag-to-Applications .dmg from the .app"
	@echo "  clean-app  - (macOS) Remove the built .app + .dmg + PyInstaller workdir"
	@echo "  help       - Print this help"
	@echo ""
	@echo "Variables:"
	@echo "  PREFIX     - Install prefix (default: \$$HOME/.local)"
	@echo "             System install: sudo make install PREFIX=/usr/local"
	@echo "  VERSION    - Package version (default: $(VERSION))"
	@echo "  MAINTAINER - .deb maintainer string"
