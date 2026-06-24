# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds a single-file "Pong Dashboard.exe" for Windows.

Bundles pong_lock.py (dashboard mode) plus pygame, icalendar and
recurring_ical_events into one self-contained, windowed .exe.

Build (on Windows, in a venv with pyinstaller):
    pyinstaller --noconfirm packaging\\windows\\pong-dashboard-win.spec
"""
import os
from PyInstaller.utils.hooks import collect_all

# SPECPATH is injected by PyInstaller = directory holding this spec file.
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# Bundle version: CI sets PONG_VERSION from the release tag.
PONG_VERSION = os.environ.get("PONG_VERSION", "0.4.5")

datas, binaries, hiddenimports = [], [], []
# certifi: bundle a CA trust store so HTTPS (weather + calendar ICS) works
# on machines without the bundled OpenSSL's baked-in cert path.
for pkg in ("icalendar", "recurring_ical_events", "pygame", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
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

# Single-file build: packing a.binaries + a.datas into EXE (with no
# COLLECT) is what makes PyInstaller emit one self-contained .exe.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Pong Dashboard",
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,                      # windowed app, no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=os.path.join(SPECPATH, "icon.ico"),
)
