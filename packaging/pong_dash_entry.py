"""macOS / Windows bundle entry point.

A double-clicked .app/.exe bundle is launched with no CLI args, but pong's
dashboard is selected via `--dashboard`. Force it here, then hand off to
the unchanged pong_lock.main(). Lock mode is Linux/PAM-only and is never
reachable from the bundle.
"""
import os
import sys

# Frozen bundles (PyInstaller .app/.exe) ship their own OpenSSL but no CA
# trust store, and the baked-in default cert path (e.g. a Homebrew dir on
# macOS) does not exist on a clean user machine — so every HTTPS call
# (weather + Google Calendar ICS) fails certificate verification and is
# swallowed silently. Point OpenSSL at certifi's bundled CA file. Gated on
# sys.frozen so the Linux source/.deb path keeps using the system store.
if getattr(sys, "frozen", False):
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except Exception:
        pass

sys.argv = [sys.argv[0], "--dashboard"]

import pong_lock  # noqa: E402  (after argv is set; import triggers no work)

sys.exit(pong_lock.main())
