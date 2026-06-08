DESTDIR =
PREFIX = /usr/local

# Debian packaging — `make deb` stages the install tree under build/
# and emits dist/pong_$(VERSION)_all.deb. Override MAINTAINER if you
# fork.
VERSION = 0.2.0
MAINTAINER = xjmzx <jabbanawanga@gmail.com>
PKG_NAME = pong_$(VERSION)_all
PKG_DIR = build/$(PKG_NAME)
DIST_DIR = dist

.PHONY: all install uninstall deps deb clean-deb help

all: help

install:
	install -D -m755 -- pong_lock.py "$(DESTDIR)$(PREFIX)/bin/pong"
	install -D -m644 -- pong.desktop "$(DESTDIR)$(PREFIX)/share/applications/pong.desktop"
	install -D -m644 -- icon.svg "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong.svg"

uninstall:
	rm -f -- "$(DESTDIR)$(PREFIX)/bin/pong"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/applications/pong.desktop"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong.svg"

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
	  "Description: Ambient Pong screen lock for Ubuntu/X11" \
	  " Two AI paddles auto-play across a 4x4 ambient dashboard of clock," \
	  " day, date, weather, network, and Google Calendar readouts." \
	  " Authenticates against the real login password via PAM. Mirrors" \
	  " across multiple monitors." \
	  " ." \
	  " Deterrent-level lock: VT-switch and SSH still work." \
	  > $(PKG_DIR)/DEBIAN/control
	dpkg-deb --root-owner-group --build $(PKG_DIR) $(DIST_DIR)/$(PKG_NAME).deb
	@echo ""
	@echo "Built: $(DIST_DIR)/$(PKG_NAME).deb"
	@echo "Install with: sudo apt install ./$(DIST_DIR)/$(PKG_NAME).deb"

clean-deb:
	rm -rf build $(DIST_DIR)

help:
	@echo "Available make targets:"
	@echo "  deps       - Install runtime dependencies (apt + pip)"
	@echo "  install    - Install pong binary + .desktop + icon"
	@echo "  uninstall  - Remove installed files"
	@echo "  deb        - Build a .deb package at dist/$(PKG_NAME).deb"
	@echo "  clean-deb  - Remove build/ and dist/ artifacts"
	@echo "  help       - Print this help"
	@echo ""
	@echo "Variables:"
	@echo "  PREFIX     - Install prefix (default: /usr/local)"
	@echo "             User install: make install PREFIX=\$$HOME/.local"
	@echo "  VERSION    - Package version (default: $(VERSION))"
	@echo "  MAINTAINER - .deb maintainer string"
