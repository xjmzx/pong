# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds "Pong Dashboard.app" for macOS.

Bundles pong_lock.py (dashboard mode) plus pygame, icalendar and
recurring_ical_events into a self-contained, double-clickable .app.

Build:  pyinstaller --noconfirm packaging/macos/pong-dashboard.spec
        (run from the repo root, with the venv active)
"""
import os
from PyInstaller.utils.hooks import collect_all

# SPECPATH is injected by PyInstaller = directory holding this spec file.
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# Bundle version: CI sets PONG_VERSION from the release tag; default for
# local builds.
PONG_VERSION = os.environ.get("PONG_VERSION", "0.4.2")

datas, binaries, hiddenimports = [], [], []
# certifi: bundle a CA trust store so HTTPS (weather + calendar ICS) works
# on machines without the bundled OpenSSL's baked-in cert path.
for pkg in ("icalendar", "recurring_ical_events", "pygame", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# pong_lock.py imports the SDL2 video bindings at module top level.
hiddenimports += ["pygame._sdl2", "pygame._sdl2.video"]

a = Analysis(
    [os.path.join(SPECPATH, "..", "pong_dash_entry.py")],
    pathex=[ROOT],                      # so `import pong_lock` resolves
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PAM"],        # PAM is Linux-only, never used here
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pong-dashboard",
    console=False,                      # windowed app, no terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,                   # build for the host arch
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="pong-dashboard",
)
app = BUNDLE(
    coll,
    name="Pong Dashboard.app",
    icon=os.path.join(SPECPATH, "pong.icns"),
    bundle_identifier="uk.upleb.pong.dashboard",
    info_plist={
        "CFBundleShortVersionString": PONG_VERSION,
        "CFBundleVersion": PONG_VERSION,
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "LSMinimumSystemVersion": "11.0",
    },
)
