#!/usr/bin/env python3
"""Generate the Kodi repository index.

Kodi browses a repo by fetching addons.xml (+ its md5) and then zips and
artwork from <datadir>/<addon.id>/. The release workflow calls this to
lay out the `repo` branch content:

    repo/
      addons.xml
      addons.xml.md5
      script.screensemble/
        script.screensemble-<ver>.zip
        icon.png
        fanart.jpg
      repository.screensemble/
        repository.screensemble-<ver>.zip
        icon.png
        fanart.jpg

Usage: python tools/build_repo.py <addon.zip> <outdir>
"""
import hashlib
import os
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_ID = "script.screensemble"
REPO_ID = "repository.screensemble"
REPO_SKEL = os.path.join(ROOT, "tools", REPO_ID)


def _version(xml_path):
    return ET.parse(xml_path).getroot().attrib["version"]


def _zip_addon(src_dir, addon_id, out_path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(src_dir):
            for name in files:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, src_dir)
                z.write(full, f"{addon_id}/{rel.replace(os.sep, '/')}")
    with zipfile.ZipFile(out_path) as z:
        assert z.testzip() is None


def build(addon_zip, outdir):
    addon_ver = _version(os.path.join(ROOT, "addon.xml"))
    repo_ver = _version(os.path.join(REPO_SKEL, "addon.xml"))

    # Addon payload
    dst = os.path.join(outdir, ADDON_ID)
    os.makedirs(dst, exist_ok=True)
    shutil.copy(addon_zip, os.path.join(dst, os.path.basename(addon_zip)))
    for art in ("icon.png", "fanart.jpg"):
        src = os.path.join(ROOT, art)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(dst, art))

    # Repository addon payload
    dst = os.path.join(outdir, REPO_ID)
    os.makedirs(dst, exist_ok=True)
    repo_zip = os.path.join(dst, f"{REPO_ID}-{repo_ver}.zip")
    _zip_addon(REPO_SKEL, REPO_ID, repo_zip)
    for art in ("icon.png", "fanart.jpg"):
        src = os.path.join(ROOT, art)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(dst, art))

    # addons.xml — each member's addon.xml verbatim inside <addons>
    bodies = []
    for aid in (ADDON_ID, REPO_ID):
        xml_path = (
            os.path.join(ROOT, "addon.xml")
            if aid == ADDON_ID
            else os.path.join(REPO_SKEL, "addon.xml")
        )
        with open(xml_path, encoding="utf-8") as f:
            body = f.read().strip()
        # An <?xml?> declaration can't appear inside <addons> — strip it.
        if body.startswith("<?xml"):
            body = body.split("?>", 1)[1].strip()
        bodies.append(body)
    index = "<addons>\n" + "\n".join(bodies) + "\n</addons>\n"
    index_path = os.path.join(outdir, "addons.xml")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index)
    md5 = hashlib.md5(index.encode("utf-8")).hexdigest()
    with open(index_path + ".md5", "w") as f:
        f.write(md5)

    print(f"repo index: {ADDON_ID} {addon_ver}, {REPO_ID} {repo_ver}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: build_repo.py <addon.zip> <outdir>")
    build(sys.argv[1], sys.argv[2])
