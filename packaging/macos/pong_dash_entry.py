"""macOS .app entry point.

A double-clicked .app bundle is launched with no CLI args, but pong's
dashboard is selected via `--dashboard`. Force it here, then hand off to
the unchanged pong_lock.main(). Lock mode is Linux/PAM-only and is never
reachable from the bundle.
"""
import sys

sys.argv = [sys.argv[0], "--dashboard"]

import pong_lock  # noqa: E402  (after argv is set; import triggers no work)

sys.exit(pong_lock.main())
