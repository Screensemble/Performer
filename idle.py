"""Idle presentation rendering for the Screensemble PoC.

Two engines are implemented so they can be compared on hardware (spec open
question #2):

- "window":  a dedicated fullscreen xbmcgui.WindowXML window (spec 7.8.1 —
             the preferred candidate). Fully controllable: letterbox color,
             scaling mode, key interception, debug overlay.
- "picture": Kodi's built-in picture viewer via ShowPicture(). One line, but
             we cannot intercept keys or control the window — included so the
             limitation is testable, not as a real candidate.
"""

import os
import struct
import zlib

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_PATH = ADDON.getAddonInfo("path")
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))

WHITE_PNG = os.path.join(PROFILE, "screensemble_white.png")
SOLID_PNG = os.path.join(PROFILE, "screensemble_solid.png")

# Control ids inside idle.xml
CONTROL_BG = 200          # fullscreen white texture, tinted via colordiffuse
CONTROL_IMG_CONTAIN = 201
CONTROL_IMG_STRETCH = 202
CONTROL_IMG_COVER = 203
CONTROL_DEBUG_LABEL = 300

# idle_scaling setting index -> control id
SCALING_CONTROLS = {0: CONTROL_IMG_CONTAIN, 1: CONTROL_IMG_COVER, 2: CONTROL_IMG_STRETCH}


def letter_key(ch):
    """Kodi action id for a keyboard letter key."""
    return 0xF000 + ord(ch)


def hex_to_colordiffuse(value):
    """'#rrggbb' -> 'FFRRGGBB' for ControlImage.setColorDiffuse."""
    h = (value or "").strip().lstrip("#")
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        h = "000000"
    return "FF" + h.upper()


def hex_to_rgb(value):
    h = (value or "").strip().lstrip("#")
    if len(h) != 6:
        h = "000000"
    return bytes(int(h[i:i + 2], 16) for i in (0, 2, 4))


def write_solid_png(path, rgb, size=8):
    """Minimal dependency-free solid-color PNG writer."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    row = b"\x00" + bytes(rgb) * size
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as f:
        f.write(payload)


def ensure_runtime_assets(color_hex):
    """Generate the white tile and solid-color PNG in the profile dir."""
    xbmcvfs.mkdirs(PROFILE)
    if not os.path.exists(WHITE_PNG):
        write_solid_png(WHITE_PNG, b"\xff\xff\xff")
    write_solid_png(SOLID_PNG, hex_to_rgb(color_hex))


def resolve_path(path):
    """Translate special:// paths; pass through normal filesystem paths."""
    if not path:
        return ""
    return xbmcvfs.translatePath(path)


class IdleWindow(xbmcgui.WindowXML):
    """Dedicated fullscreen idle window (spec 7.8.1).

    Constructed with the standard WindowXML signature — Kodi's C-level
    constructor validates its args, so custom constructor parameters are not
    allowed. Assign .config (and .on_action) after construction, before show().
    """

    config = None
    on_action = None  # callable(action), set by service
    _img_cid = None   # id of the currently visible image control

    def set_idle_image(self, path):
        """Swap the visible image control's texture (slideshow rotation).
        useCache=False is required — otherwise Kodi re-serves the previously
        displayed texture and the image never changes."""
        if self._img_cid is not None:
            self.getControl(self._img_cid).setImage(path, False)

    def onInit(self):
        self.apply()

    def apply(self):
        """Apply the current config to the controls. Safe to call any time —
        controls only exist once the window has initialized, so failures are
        logged and ignored (onInit will retry when the window initializes)."""
        try:
            self._apply()
        except Exception as e:
            xbmc.log(f"[screensemble] idle apply failed: {e}", xbmc.LOGERROR)

    def onAction(self, action):
        # Deliberately do NOT call the base implementation: every action we
        # don't explicitly handle is swallowed, so ESC/back cannot close the
        # window and expose the Kodi UI.
        if self.on_action:
            self.on_action(action)

    def _apply(self):
        cfg = self.config

        bg = self.getControl(CONTROL_BG)
        bg.setImage(WHITE_PNG)
        bg.setColorDiffuse(hex_to_colordiffuse(cfg["letterbox_color"]))

        selected = SCALING_CONTROLS.get(cfg["scaling"], CONTROL_IMG_CONTAIN)
        self._img_cid = selected if cfg["idle_type"] == 1 else None
        for cid in SCALING_CONTROLS.values():
            img = self.getControl(cid)
            img.setVisible(cid == self._img_cid)
        if cfg["idle_type"] == 1 and cfg["image_path"]:
            resolved = resolve_path(cfg["image_path"])
            self.getControl(selected).setImage(resolved)
            xbmc.log(f"[screensemble] idle image -> {resolved}", xbmc.LOGINFO)
            self.getControl(CONTROL_BG).setColorDiffuse(
                hex_to_colordiffuse(cfg["letterbox_color"])
            )
        else:
            self.getControl(CONTROL_BG).setColorDiffuse(
                hex_to_colordiffuse(cfg["idle_color"])
            )

        label = self.getControl(CONTROL_DEBUG_LABEL)
        label.setVisible(bool(cfg["debug_overlay"]))
        if cfg["debug_overlay"]:
            label.setLabel(
                "SCREENSEMBLE IDLE PoC | mode={} | {} | "
                "OK=play Menu=actions Back x2=exit".format(
                    "local" if cfg.get("player_mode", 0) == 0 else "server",
                    "image" if cfg["idle_type"] == 1 else "color " + cfg["idle_color"],
                )
            )
