DESTDIR =
PREFIX = /usr/local

.PHONY: all install uninstall help

all: help

install:
	install -D -m755 -- pong_lock.py "$(DESTDIR)$(PREFIX)/bin/pong"
	install -D -m644 -- pong.desktop "$(DESTDIR)$(PREFIX)/share/applications/pong.desktop"
	install -D -m644 -- icon.svg "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong.svg"

uninstall:
	rm -f -- "$(DESTDIR)$(PREFIX)/bin/pong"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/applications/pong.desktop"
	rm -f -- "$(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/pong.svg"

help:
	@echo "Available make targets:"
	@echo "  install    - Install pong binary + .desktop + icon"
	@echo "  uninstall  - Remove installed files"
	@echo "  help       - Print this help"
	@echo ""
	@echo "Variables:"
	@echo "  PREFIX     - Install prefix (default: /usr/local)"
	@echo "             User install: make install PREFIX=\$$HOME/.local"
