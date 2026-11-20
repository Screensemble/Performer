"""First-run setup wizard — custom WindowXML UI, remote-first.

A step machine: each step sets the step label/description and rebuilds a
button list (control 9000). Rows either run an action (Kodi's VFS browse
dialog or on-screen keyboard input) or move between steps. Back on the
first step cancels; finish writes setup_complete so the service starts
the idle presentation.
"""

import os
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

import qr
from idle import ADDON_PATH, PROFILE, ensure_runtime_assets

ADDON = xbmcaddon.Addon()
HOME = xbmcgui.Window(10000)

CONTROL_WORDMARK = 400
CONTROL_STEP = 401
CONTROL_DESC = 402
CONTROL_BIG = 403
CONTROL_QR = 404
CONTROL_QR_CAP = 405
CONTROL_MARK = 412
CONTROL_LIST = 9000

QR_PATH = os.path.join(PROFILE, "qr_setup.png")

# Card geometry — rows anchor to the card's bottom edge and the description
# textbox grows to fill the space between the title and the first row.
CARD_LEFT, CARD_TOP, CARD_W, CARD_H = 510, 220, 900, 640
CARD_BOTTOM = CARD_TOP + CARD_H
QR_CARD = (460, 170, 1000, 720)   # bigger card for the QR pairing screen
QR_CARD_BOTTOM = QR_CARD[1] + QR_CARD[3]
LIST_LEFT = 560
LIST_ITEM_H = 66
LIST_BOTTOM_MARGIN = 24
DESC_TOP = 412
DESC_LIST_GAP = 16
CONTROL_CARD = 410
CONTROL_BAR = 411

BACK_ACTIONS = {
    xbmcgui.ACTION_PARENT_DIR,
    xbmcgui.ACTION_PREVIOUS_MENU,
    xbmcgui.ACTION_NAV_BACK,
}

def log(msg):
    xbmc.log(f"[screensemble] {msg}", xbmc.LOGINFO)


def _setting(sid, default=""):
    return ADDON.getSetting(sid) or default


class WizardWindow(xbmcgui.WindowXML):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = 0          # 0=local 1=server — Local is the default row
        self.step = 0
        self.finished = False
        self.remote = False    # a phone is doing setup via /setup
        self._rows = []
        self._done = False     # watchdog stop flag
        # setup_complete state at open — a false→true flip while the
        # wizard is up (phone finish, or a pairing accept) closes it.
        # Re-running after setup stays open on purpose.
        self._was_incomplete = not xbmcaddon.Addon().getSettingBool(
            "setup_complete"
        )

    # -- steps ------------------------------------------------------------

    def _steps(self):
        steps = [
            {
                "title": "Set up this display",
                "qr": True,
                "big": "Scan to set up",
                "desc": (
                    "Scan the QR code with your phone — or open the setup "
                    "address in any browser on this network — to configure "
                    "this display, including server pairing.\n\n"
                    "Or choose 'Set up manually' to continue here with the "
                    "remote."
                ),
                "rows": [("Set up manually", "next")],
            },
            {
                "title": "How should this display run?",
                "desc": (
                    "Local plays images/videos from this display's built-in "
                    "media folder and never needs a network. Server connects "
                    "this display to a Screensemble server for managed "
                    "content and synchronized playback. You can switch later "
                    "in settings."
                ),
                "rows": [
                    ("Local (standalone)", "mode_local"),
                    ("Server (managed / controlled)", "mode_server"),
                ],
            }
        ]
        if self.mode != 0:
            steps += [
                {
                    "title": "Server connection",
                    "desc": (
                        "Address of the Screensemble server (host or host:port) "
                        "and a name for this display. The display keeps its "
                        "local idle presentation until it can reach the server."
                    ),
                    "rows": [
                        (lambda: f"Server address   ·   {_setting('server_address') or 'not set'}", "input_server"),
                        (lambda: f"Device name   ·   {_setting('device_name') or 'not set'}", "input_name"),
                        ("Continue", "next"),
                        ("Back", "back"),
                    ],
                },
                {
                    "title": "Cache folder",
                    "desc": (
                        "Where server-pushed videos and images are stored on "
                        "this display. Downloaded content survives reboots and "
                        "server outages."
                    ),
                    "rows": [
                        (lambda: f"Browse cache folder…   ·   {_setting('server_media_dir') or 'not set'}", "browse_cache"),
                        ("Continue", "next"),
                        ("Back", "back"),
                    ],
                },
            ]
        steps.append(
            {
                "title": "Ready",
                "desc": (
                    "Setup is complete. The display will start its idle "
                    "presentation immediately — Back twice from the idle "
                    "screen anytime to reach Kodi's settings."
                ),
                "rows": [
                    ("Finish — start Screensemble", "finish"),
                    ("Back", "back"),
                ],
            }
        )
        return steps

    # -- rendering ---------------------------------------------------------

    def onInit(self):
        # QR for phone setup — the service publishes the token'd URL.
        self._setup_url = HOME.getProperty("screensemble.setup_url")
        if self._setup_url:
            try:
                tmp = QR_PATH + ".tmp"
                qr.write_rounded_qr_png(self._setup_url, tmp, 280, 26, bg=(16, 32, 60))
                os.replace(tmp, QR_PATH)
                self.getControl(CONTROL_QR).setImage(QR_PATH, False)
            except Exception as e:
                log(f"wizard QR failed: {e}")
                self._setup_url = ""
        threading.Thread(target=self._watchdog, daemon=True).start()
        self._render()

    def _watchdog(self):
        """Track the web setup heartbeat + pairing state from the service
        process and mirror it onto the card."""
        while not self._done:
            remote = HOME.getProperty("screensemble.websetup") == "1"
            if remote != self.remote:
                self.remote = remote
                try:
                    self._render()
                except Exception:
                    return
            # Setup finished elsewhere — the phone's Finish, or a pairing
            # accept — hand off to the service. Gated on the wizard having
            # opened while setup was incomplete so a re-run doesn't
            # instantly close itself.
            if (
                self._was_incomplete
                and xbmcaddon.Addon().getSettingBool("setup_complete")
            ):
                self.finished = True
                self._done = True
                self.close()
                return
            xbmc.sleep(500)

    def _render(self):
        if self.remote:
            self.getControl(CONTROL_STEP).setLabel("REMOTE SETUP")
            self.getControl(CONTROL_BIG).setLabel("Setup in progress…")
            self.getControl(CONTROL_DESC).setText(
                "Setup is being finished from the web page on your phone. "
                "This screen will continue automatically."
            )
            self.getControl(CONTROL_LIST).setVisible(False)
            self._show_qr(False)
            return
        self.getControl(CONTROL_LIST).setVisible(True)
        steps = self._steps()
        total = len(steps)
        step = steps[self.step]
        if step.get("qr"):
            # Pairing screen is the entry point, not a numbered step.
            self.getControl(CONTROL_STEP).setLabel("")
        else:
            self.getControl(CONTROL_STEP).setLabel(
                f"STEP {self.step} OF {total - 1} — {step['title'].upper()}"
            )
        self.getControl(CONTROL_BIG).setLabel(step.get("big", ""))
        self.getControl(CONTROL_DESC).setText(step["desc"])
        lst = self.getControl(CONTROL_LIST)
        lst.reset()
        self._rows = []
        for label, action in step["rows"]:
            text = label() if callable(label) else label
            lst.addItem(xbmcgui.ListItem(label=text))
            self._rows.append(action)
        # QR step gets a larger card and its own spacious two-column layout;
        # every other step uses the compact standard card.
        on_qr = bool(step.get("qr") and self._setup_url)
        card_bottom = QR_CARD_BOTTOM if on_qr else CARD_BOTTOM

        # Anchor rows to the card's bottom edge; stretch the description to
        # fill the space above them instead of scrolling.
        try:
            n = max(len(self._rows), 1)
            list_h = n * LIST_ITEM_H
            list_top = card_bottom - LIST_BOTTOM_MARGIN - list_h
            lst.setHeight(list_h)
            lst.setPosition(LIST_LEFT, list_top)
        except Exception as e:
            log(f"wizard layout resize failed: {e}")

        self._show_qr(on_qr)
        try:
            card = self.getControl(CONTROL_CARD)
            bar = self.getControl(CONTROL_BAR)
            desc = self.getControl(CONTROL_DESC)
            wordmark = self.getControl(CONTROL_WORDMARK)
            mark = self.getControl(CONTROL_MARK)
            big = self.getControl(CONTROL_BIG)
            if on_qr:
                # Larger card gets its own header geometry — 50px padding so
                # the wordmark/logo stay aligned with the card, not the
                # standard card's coordinates.
                x, y, w, h = QR_CARD
                card.setPosition(x, y)
                card.setWidth(w)
                card.setHeight(h)
                bar.setPosition(x, y)
                bar.setWidth(w)
                wordmark.setPosition(510, 219)
                mark.setPosition(1300, 200)
                big.setPosition(900, 330)
                big.setWidth(470)
                qimg = self.getControl(CONTROL_QR)
                qimg.setPosition(550, 360)
                qimg.setWidth(280)
                qimg.setHeight(280)
                cap = self.getControl(CONTROL_QR_CAP)
                cap.setPosition(500, 652)
                cap.setWidth(390)
                cap.setHeight(140)
                cap.setText(
                    "scan with your phone\nor open this address:\n"
                    f"{self._setup_url}"
                )
                desc.setPosition(900, 400)
                desc.setWidth(470)
                desc.setHeight(330)
            else:
                card.setPosition(CARD_LEFT, CARD_TOP)
                card.setWidth(CARD_W)
                card.setHeight(CARD_H)
                bar.setPosition(CARD_LEFT, CARD_TOP)
                bar.setWidth(CARD_W)
                wordmark.setPosition(560, 271)
                mark.setPosition(1250, 252)
                big.setPosition(560, 352)
                big.setWidth(800)
                desc.setPosition(560, DESC_TOP)
                desc.setWidth(800)
                desc.setHeight(list_top - DESC_LIST_GAP - DESC_TOP)
        except Exception as e:
            log(f"wizard card resize failed: {e}")
        self.setFocus(lst)

    def _show_qr(self, visible):
        show = bool(visible and self._setup_url)
        self.getControl(CONTROL_QR).setVisible(show)
        self.getControl(CONTROL_QR_CAP).setVisible(show)

    # -- input -------------------------------------------------------------

    def onAction(self, action):
        aid = action.getId()
        if aid in BACK_ACTIONS:
            self._back()
        # while a phone is running setup, the on-screen rows are hidden —
        # nothing else is actionable (Back above still cancels the wizard)
        # select is handled by onClick — handling it here too would fire twice

    def onClick(self, control_id):
        if control_id == CONTROL_LIST:
            self._activate_selected()

    def _activate_selected(self):
        try:
            pos = self.getControl(CONTROL_LIST).getSelectedPosition()
        except Exception:
            pos = self.getCurrentListPosition()
        if 0 <= pos < len(self._rows):
            self._activate(self._rows[pos])

    def _activate(self, action):
        dlg = xbmcgui.Dialog()
        if action in ("mode_local", "mode_server"):
            self.mode = 0 if action == "mode_local" else 1
            self.step += 1
            self._render()
        elif action == "browse_cache":
            path = dlg.browse(0, "Choose the cache folder for server media", "files")
            if path:
                ADDON.setSetting("server_media_dir", path)
            self._render()
        elif action == "input_server":
            value = dlg.input(
                "Server address (host or host:port)",
                _setting("server_address"),
                xbmcgui.INPUT_ALPHANUM,
            )
            if value:
                ADDON.setSetting("server_address", value)
            self._render()
        elif action == "input_name":
            value = dlg.input(
                "Device name", _setting("device_name"), xbmcgui.INPUT_ALPHANUM
            )
            if value:
                ADDON.setSetting("device_name", value)
            self._render()
        elif action == "next":
            self.step += 1
            self._render()
        elif action == "back":
            self._back()
        elif action == "finish":
            ADDON.setSettingInt("player_mode", self.mode)
            ADDON.setSettingBool("setup_complete", True)
            self.finished = True
            log(f"wizard finished (mode={'local' if self.mode == 0 else 'server'})")
            self._done = True
            self.close()

    def _back(self):
        if self.step == 0 or self.remote:
            self._done = True
            self.close()
        else:
            self.step -= 1
            self._render()


def run_wizard():
    ensure_runtime_assets()
    win = WizardWindow("wizard.xml", ADDON_PATH, "Default", "1080i")
    win.doModal()
    if win.finished:
        xbmcgui.Dialog().notification(
            "Screensemble", "Setup complete — idle presentation starting"
        )
    elif not xbmcaddon.Addon().getSettingBool("setup_complete"):
        xbmcgui.Dialog().notification(
            "Screensemble",
            "Setup cancelled — open Screensemble again to finish setup",
            xbmcgui.NOTIFICATION_WARNING,
        )
