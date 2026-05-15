DESTDIR =
PREFIX = /usr/local

.PHONY: all install uninstall help

all: help

install:
	install -D -m755 -- pong_lock.py "$(DESTDIR)$(PREFIX)/bin/pong-lock"
	install -D -m644 -- pong-lock.desktop "$(DESTDIR)$(PREFIX)/share/applications/pong-lock.desktop"
	install -D -m644 -- icon.svg "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong-lock.svg"

uninstall:
	rm -f -- "$(DESTDIR)$(PREFIX)/bin/pong-lock"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/applications/pong-lock.desktop"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong-lock.svg"

help:
	@echo "Available make targets:"
	@echo "  install    - Install pong-lock binary + .desktop + icon"
	@echo "  uninstall  - Remove installed files"
	@echo "  help       - Print this help"
	@echo ""
	@echo "Variables:"
	@echo "  PREFIX     - Install prefix (default: /usr/local)"
	@echo "             User install: make install PREFIX=\$$HOME/.local"
