"""Idle presentation rendering.

The idle presentation is a dedicated fullscreen xbmcgui.WindowXML window
(spec 7.8.1) held open modally for the lifetime of the service: fullscreen
video opens over it, so stopping playback reveals it instantly with no
Kodi UI in between. Fully controllable: letterbox color, scaling mode,
key interception, debug overlay.
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
ROUND_PNG = os.path.join(PROFILE, "screensemble_card.png")

# Control ids inside idle.xml
CONTROL_BG = 200          # fullscreen white texture, tinted via colordiffuse
CONTROL_IMG_CONTAIN = 201
CONTROL_IMG_STRETCH = 202
CONTROL_IMG_COVER = 203
CONTROL_DEBUG_LABEL = 300

# idle_scaling setting index -> control id
SCALING_CONTROLS = {0: CONTROL_IMG_CONTAIN, 1: CONTROL_IMG_COVER, 2: CONTROL_IMG_STRETCH}


def hex_to_colordiffuse(value):
    """'#rrggbb' -> 'FFRRGGBB' for ControlImage.setColorDiffuse."""
    h = (value or "").strip().lstrip("#")
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        h = "000000"
    return "FF" + h.upper()


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


def write_rounded_png(path, w, h, radius, fg, bg, pad=4):
    """Rounded rect (fg rgb) on a solid bg — opaque RGB PNG. No alpha
    channel, so Kodi's texture compression can't chew up the corners; a
    ~2px color blend keeps the edge smooth. `pad` insets the rect so the
    curve never touches the texture boundary (avoids edge clipping)."""
    import math

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    hw, hh = w / 2.0, h / 2.0
    inner_x, inner_y = hw - pad - radius, hh - pad - radius
    rows = []
    for y in range(h):
        row = bytearray(b"\x00")
        dy = abs(y + 0.5 - hh) - inner_y
        for x in range(w):
            dx = abs(x + 0.5 - hw) - inner_x
            ox, oy = max(dx, 0.0), max(dy, 0.0)
            d = math.sqrt(ox * ox + oy * oy) + min(max(dx, dy), 0.0) - radius
            t = 0.5 - d / 2.0
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            row += bytes(
                int(bg[i] + (fg[i] - bg[i]) * t) for i in range(3)
            )
        rows.append(bytes(row))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as f:
        f.write(png)


def ensure_runtime_assets():
    """Generate the white tile + rounded card the skins tint via
    colordiffuse."""
    xbmcvfs.mkdirs(PROFILE)
    if not os.path.exists(WHITE_PNG):
        write_solid_png(WHITE_PNG, b"\xff\xff\xff")
    if not os.path.exists(ROUND_PNG):
        # Navy card on black — the splash dimmer is solid black behind it.
        write_rounded_png(ROUND_PNG, 800, 185, 32, (14, 27, 44), (0, 0, 0))


def resolve_path(path):
    """Translate special:// paths; pass through normal filesystem paths."""
    if not path:
        return ""
    return xbmcvfs.translatePath(path)


def restart_app():
    """Relaunch Kodi. The RestartApp builtin is unreliable on Windows —
    it can terminate instead of restarting. Spawn a detached helper that
    waits for shutdown to finish, then relaunches; quit cleanly."""
    import subprocess
    import sys

    exe = sys.executable  # embedded Python runs inside kodi.exe
    try:
        if os.name == "nt":
            bat = os.path.join(PROFILE, "screensemble-restart.bat")
            with open(bat, "w") as f:
                f.write(
                    "@echo off\r\n"
                    # ping delay — timeout.exe needs a console stdin and
                    # fails outright in this detached context.
                    "ping -n 6 127.0.0.1 >nul\r\n"
                    f'start "" "{exe}"\r\n'
                )
            subprocess.Popen(
                ["cmd.exe", "/c", bat],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
        else:
            subprocess.Popen(
                ["sh", "-c", f'sleep 5 && "{exe}"'],
                start_new_session=True,
                close_fds=True,
            )
    except Exception:
        xbmc.executebuiltin("RestartApp")
        return
    xbmc.executebuiltin("Quit")


class IdleWindow(xbmcgui.WindowXML):
    """Dedicated fullscreen idle window (spec 7.8.1).

    Constructed with the standard WindowXML signature — Kodi's C-level
    constructor validates its args, so custom constructor parameters are not
    allowed. Assign .config (and .on_action) after construction, before show().
    """

    config = None
    on_action = None  # callable(action), set by service
    _img_cid = None   # id of the currently visible image control
    _last_img = None  # texture currently loaded — dedupes re-sets

    def _image_control(self):
        """The scaling-selected image control. apply() hides the image
        layer when the config has no image (idle_type=0 — e.g. while a
        video item is current), leaving _img_cid None; derive it lazily
        and un-hide so a later slide can always paint."""
        if self._img_cid is None:
            self._img_cid = SCALING_CONTROLS.get(
                (self.config or {}).get("scaling"), CONTROL_IMG_CONTAIN
            )
            self.getControl(self._img_cid).setVisible(True)
        return self.getControl(self._img_cid)

    def set_idle_image(self, path):
        """Swap the visible image control's texture (slideshow rotation).
        useCache=False is required — otherwise Kodi re-serves the previously
        displayed texture and the image never changes."""
        if path == self._last_img:
            return   # already showing — a re-set would flicker the texture
        self._last_img = path
        img = self._image_control()
        img.setColorDiffuse("FFFFFFFF")   # undo clear_idle_image tint
        img.setImage(path, False)

    def clear_idle_image(self):
        """Blank the image layer to black — the load gap between video
        items then shows an intentional cut instead of stale artwork or
        the standby card."""
        self._last_img = None
        img = self._image_control()
        img.setImage(WHITE_PNG, False)
        img.setColorDiffuse("FF000000")

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
        # onInit re-fires every time the modal window is re-revealed after
        # playback — while the current item is a video the config reports
        # "no image", but the engine may have staged an underlay (the next
        # image preloaded). Restore it or the reveal is a black gap.
        want_img = cfg["idle_type"] == 1 or bool(self._last_img)
        self._img_cid = selected if want_img else None
        for cid in SCALING_CONTROLS.values():
            img = self.getControl(cid)
            img.setVisible(cid == self._img_cid)
        if cfg["idle_type"] == 1 and cfg["image_path"]:
            resolved = resolve_path(cfg["image_path"])
            self._last_img = resolved
            self.getControl(selected).setColorDiffuse("FFFFFFFF")
            self.getControl(selected).setImage(resolved)
            self.getControl(CONTROL_BG).setColorDiffuse(
                hex_to_colordiffuse(cfg["letterbox_color"])
            )
        elif self._last_img:
            img = self.getControl(selected)
            img.setColorDiffuse("FFFFFFFF")
            img.setImage(self._last_img)
            self.getControl(CONTROL_BG).setColorDiffuse(
                hex_to_colordiffuse(cfg["letterbox_color"])
            )
        else:
            self.getControl(CONTROL_BG).setColorDiffuse(
                hex_to_colordiffuse(cfg["idle_color"])
            )

        self.getControl(CONTROL_DEBUG_LABEL).setVisible(False)
