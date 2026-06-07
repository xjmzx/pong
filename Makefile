DESTDIR =
PREFIX = /usr/local

.PHONY: all install uninstall deps help

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
	@echo "Done. Optional: install iwgetid (wireless-tools) for SSID readout:"
	@echo "  sudo apt install wireless-tools"

help:
	@echo "Available make targets:"
	@echo "  deps       - Install runtime dependencies (apt + pip)"
	@echo "  install    - Install pong binary + .desktop + icon"
	@echo "  uninstall  - Remove installed files"
	@echo "  help       - Print this help"
	@echo ""
	@echo "Variables:"
	@echo "  PREFIX     - Install prefix (default: /usr/local)"
	@echo "             User install: make install PREFIX=\$$HOME/.local"
