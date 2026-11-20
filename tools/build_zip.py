#!/usr/bin/env python3
"""Build the Kodi-installable addon zip.

Kodi requires the archive to contain a top-level folder named after the
addon id. Used by CI, the release workflow, and local builds:

    python tools/build_zip.py                      # dist/script.screensemble-<ver>.zip
    python tools/build_zip.py out/my.zip
"""
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_ID = "script.screensemble"

# Source trees and artifacts that must not ship inside the addon zip.
EXCLUDE_DIRS = {".git", ".github", "tools", "__pycache__", ".venv",
                ".devin", ".claude", "dist", ".idea", ".vscode"}
EXCLUDE_FILES = {".gitignore", ".env", ".dockerignore"}
EXCLUDE_EXT = {".zip", ".pyc", ".pyo", ".log"}


def addon_version():
    return ET.parse(
        os.path.join(ROOT, "addon.xml")
    ).getroot().attrib["version"]


def collect():
    entries = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        if set(os.path.relpath(base, ROOT).split(os.sep)) & EXCLUDE_DIRS:
            continue
        for name in files:
            if name in EXCLUDE_FILES:
                continue
            if os.path.splitext(name)[1].lower() in EXCLUDE_EXT:
                continue
            full = os.path.join(base, name)
            entries.append((full, os.path.relpath(full, ROOT)))
    return sorted(entries, key=lambda e: e[1].lower())


def build(out_path):
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    entries = collect()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for full, rel in entries:
            z.write(full, f"{ADDON_ID}/{rel.replace(os.sep, '/')}")
    with zipfile.ZipFile(out_path) as z:
        bad = z.testzip()
        assert bad is None, f"corrupt entry: {bad}"
        names = z.namelist()
        assert all(n.startswith(f"{ADDON_ID}/") for n in names), \
            "zip must contain the addon folder at its root"
        assert f"{ADDON_ID}/addon.xml" in names
    return len(entries)


if __name__ == "__main__":
    out = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(
            ROOT, "dist", f"{ADDON_ID}-{addon_version()}.zip"
        )
    )
    n = build(out)
    print(f"{out} — {n} files, crc ok")
