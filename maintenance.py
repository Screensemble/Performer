"""Maintenance surface — branded 'presentation paused' screen shown while
the display sits in the Kodi UI.

The main thread opens this modally whenever the controller is in
maintenance (exit_to_kodi) and the remote hasn't dismissed it. Rows:
Resume returns to the presentation, Addon settings opens the settings
dialog over this window, Continue to Kodi dismisses the surface and
leaves the user on the home screen (Back does the same). Any other
resume path — web admin, server command, maintenance timeout — flips
controller.maintenance off; the ticker notices and closes the window so
doModal returns and the loop can reopen standby.
"""

import threading
import time

import xbmc
import xbmcgui

from web import lan_ip

CONTROL_STATUS = 600    # "Device · Managed — server:8090"
CONTROL_HINT = 601      # web admin URL
CONTROL_COUNT = 602     # "Auto-resuming in M:SS" / back hint
CONTROL_LIST = 9000

BACK_ACTIONS = {
    xbmcgui.ACTION_NAV_BACK,
    xbmcgui.ACTION_PREVIOUS_MENU,
}


def log(msg):
    xbmc.log(f"[screensemble] {msg}", xbmc.LOGINFO)


class MaintenanceWindow(xbmcgui.WindowXML):
    controller = None    # StandbyController — injected before open()
    _alive = False
    _closed = False      # doModal returned — guard zombie re-activations

    def open(self):
        self._alive = True
        self.doModal()
        self._alive = False
        self._closed = True

    def onInit(self):
        if self._closed:
            self.close()
            return
        cfg = self.controller.config
        device = cfg.get("device_name") or "This display"
        if cfg.get("player_mode") == 1:
            mode = f"Managed — {cfg.get('server_address') or 'no server set'}"
        else:
            mode = "Local mode"
        self._set(CONTROL_STATUS, f"{device} · {mode}")
        ip = lan_ip()
        self._set(
            CONTROL_HINT,
            f"web admin http://{ip}:{cfg['web_port']}"
            if ip and not ip.startswith("127.")
            else "no network detected",
        )
        self._rows = ["resume", "settings", "kodi"]
        lst = self.getControl(CONTROL_LIST)
        lst.reset()
        for label in ("Resume presentation", "Addon settings", "Continue to Kodi"):
            lst.addItem(xbmcgui.ListItem(label=label))
        self.setFocus(lst)
        threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self):
        # 1s heartbeat — refresh the auto-resume countdown, and notice when
        # another path resumed the presentation (controller.maintenance
        # cleared). Post a back so the GUI thread closes this window —
        # close() from here would wedge on the GUI bridge lock.
        while self._alive:
            c = self.controller
            if c is None:
                return
            if not c.maintenance:
                xbmc.executebuiltin("Action(back)")
            try:
                timeout = c.config.get("maintenance_timeout", 0)
                if timeout and c._maint_since:
                    left = max(
                        0,
                        int(timeout * 60 - (time.perf_counter() - c._maint_since)),
                    )
                    self._set(CONTROL_COUNT, f"Auto-resuming in {left // 60}:{left % 60:02d}")
                else:
                    self._set(CONTROL_COUNT, "Back — stay in Kodi")
            except Exception:
                pass
            time.sleep(1.0)

    def _set(self, cid, text):
        self.getControl(cid).setLabel(text)

    def onClick(self, control_id):
        if control_id == CONTROL_LIST:
            self._activate_selected()

    def _activate_selected(self):
        try:
            pos = self.getControl(CONTROL_LIST).getSelectedPosition()
        except Exception:
            pos = self.getCurrentListPosition()
        if not (0 <= pos < len(self._rows)):
            return
        what = self._rows[pos]
        if what == "resume":
            self.controller.resume_from_menu()
            self.close()
        elif what == "settings":
            xbmc.executebuiltin("Addon.OpenSettings(script.screensemble)")
        else:  # "kodi" — leave the surface, sit on the home screen
            self._dismiss()

    def onAction(self, action):
        if self._closed:
            return  # zombie — don't resurrect dismissed state
        if action.getId() in BACK_ACTIONS:
            if not self.controller.maintenance:
                # A posted back arriving after an external resume — drop
                # the window plainly; no "dismiss to Kodi" bookkeeping.
                self.close()
            else:
                self._dismiss()

    def _dismiss(self):
        self.controller._maint_ui_dismissed = True
        self.close()
