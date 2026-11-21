"""Boot splash — brand + how-to-reach-me screen shown before content starts.

A modal WindowXML shown once at service start (after setup is complete):
logo, the local web-admin URL in Local mode, or the server address with a
live connection indicator in Server mode. Any button skips it; otherwise it
closes itself after the configured duration.
"""

import os
import threading
import time
import urllib.request

import xbmc
import xbmcgui

import qr
from standby import PROFILE
from web import lan_ip

CONTROL_ADMIN = 600     # "Local admin: http://…"
CONTROL_SERVER = 601    # "Server: … · connected/connecting/unreachable"
CONTROL_DOT = 602       # connection indicator dot (white tile tinted)
CONTROL_HINT = 603      # "press any button to continue"
CONTROL_QR = 604        # QR code -> web admin (local mode only)
CONTROL_QR_CAP = 605    # "Scan to manage this display"
CONTROL_QR_URL = 606    # admin URL printed under the caption
CONTROL_QR_CARD = 607   # navy card behind the QR block
CONTROL_QR_SUB = 608    # "Local web admin — settings, media, playback"
QR_GROUP = (CONTROL_QR, CONTROL_QR_CAP, CONTROL_QR_URL, CONTROL_QR_CARD, CONTROL_QR_SUB)

QR_PATH = os.path.join(PROFILE, "qr_admin.png")

DOT_COLORS = {
    "connecting": "FFFFB300",   # amber
    "connected": "FF4CAF50",    # green
    "unreachable": "FF8C2F39",  # red
    "hidden": "FF666666",       # grey
}


def log(msg):
    xbmc.log(f"[screensemble] {msg}", xbmc.LOGINFO)


def check_server(address, timeout=2.5):
    """HTTP health probe against the Screensemble server."""
    base = address if "://" in address else f"http://{address}"
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


class SplashWindow(xbmcgui.WindowXML):
    config = None        # service config dict
    web_enabled = False
    _timer = None
    _seconds = 0
    _opened_at = 0.0
    _closed = False      # doModal returned — kill zombie re-activations

    # Actions that should NOT dismiss the splash: no-ops and mouse moves that
    # Kodi delivers to a freshly opened window.
    IGNORED_ACTIONS = {
        xbmcgui.ACTION_NOOP,
        getattr(xbmcgui, "ACTION_MOUSE_MOVE", 107),
    }
    DISMISS_GRACE = 0.75  # seconds before a keypress can skip the splash

    def show_for(self, seconds):
        """Open modally; close on any input or when the timer fires. The
        countdown runs from window-open — the sequence engine starts
        content at splash_seconds + 2s, so the close must land first."""
        self._seconds = seconds
        self._opened_at = time.perf_counter()
        self.doModal()
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm(self):
        if self._timer is not None or self._seconds <= 0 or self._closed:
            return
        # Count down from doModal, not from now — the server probe can
        # resolve seconds late, and closing after the first video has
        # gone fullscreen knocks it back to the GUI and gets it stopped.
        # close() also stays on the Timer thread — a builtin action hits
        # whatever window is focused (Action(back) on fullscreen video
        # kills playback).
        remaining = self._seconds - (time.perf_counter() - self._opened_at)
        self._timer = threading.Timer(max(0.5, remaining), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self):
        # Mark closed before close() so a re-init fired mid-teardown hits
        # the zombie guard instead of probing and arming another timer.
        self._closed = True
        self.close()

    def onInit(self):
        if self._closed:
            # Zombie re-activation — Kodi re-showed the modal stack after
            # a fullscreen transition. Kill it on the main thread.
            self.close()
            return
        # No <visible> condition in the skin (it stomps setVisible each
        # frame) — hide until we decide to show.
        for cid in QR_GROUP:
            self.getControl(cid).setVisible(False)
        cfg = self.config
        if cfg["player_mode"] == 0:
            ip = lan_ip()
            if self.web_enabled:
                url = (
                    f"http://{ip}:{cfg['web_port']}"
                    if ip and not ip.startswith("127.")
                    else "no network detected"
                )
                self._set(CONTROL_ADMIN, f"Local admin: {url}")
                if url != "no network detected" and cfg.get("splash_show_qr"):
                    self._show_qr(url + "/")
            else:
                self._set(CONTROL_ADMIN, "Local mode — web admin disabled")
            self._set(CONTROL_SERVER, "")
            self._set_dot("hidden")
            self._arm()
        else:
            self._set(CONTROL_ADMIN, "")
            addr = cfg.get("server_address") or ""
            # Server policy may later forbid displaying the address
            # (splash_show_server models that locally for now).
            if addr and cfg.get("splash_show_server", True):
                self._set(CONTROL_SERVER, f"Server: {addr}")
            else:
                self._set(CONTROL_SERVER, "Managed by Screensemble server")
            if not addr:
                self._set_dot("unreachable")
                self._arm()
            else:
                self._set_dot("connecting")
                threading.Thread(target=self._probe, args=(addr,), daemon=True).start()

    def _probe(self, addr):
        state = "connected" if check_server(addr) else "unreachable"
        log(f"splash: server {addr} -> {state}")
        if self._closed:
            return
        try:
            self._set_dot(state)
        except Exception:
            pass  # window already closed
        self._arm()

    def _show_qr(self, url):
        """Generate the admin QR into the profile dir and reveal the manage
        card. Written via a temp file so a concurrent reader never sees a
        half PNG. The card carries the URL, so the plain label above goes
        away."""
        try:
            tmp = QR_PATH + ".tmp"
            qr.write_rounded_qr_png(url, tmp, 155, 18, bg=(14, 27, 44))
            os.replace(tmp, QR_PATH)
            img = self.getControl(CONTROL_QR)
            img.setImage(QR_PATH, False)
            self._set(CONTROL_QR_URL, url)
            for cid in QR_GROUP:
                self.getControl(cid).setVisible(True)
            self._set(CONTROL_ADMIN, "")
        except Exception as e:
            log(f"splash QR failed: {e}")

    def _set(self, cid, text):
        self.getControl(cid).setLabel(text)

    def _set_dot(self, state):
        dot = self.getControl(CONTROL_DOT)
        if state == "hidden":
            dot.setVisible(False)
        else:
            dot.setVisible(True)
            dot.setColorDiffuse(DOT_COLORS[state])

    def onAction(self, action):
        aid = action.getId()
        if aid in self.IGNORED_ACTIONS:
            return
        elapsed = time.perf_counter() - self._opened_at
        if elapsed < self.DISMISS_GRACE and elapsed < self._seconds:
            return
        log(f"splash dismissed by action {aid}")
        self._closed = True
        self.close()  # any real button press skips the splash
