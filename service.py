import time

import xbmc
import xbmcaddon
import xbmcgui

from idle import (
    IdleWindow,
    ensure_runtime_assets,
    letter_key,
    resolve_path,
    SOLID_PNG,
)

ADDON = xbmcaddon.Addon()


def log(msg):
    xbmc.log(f"[screensemble] {msg}", xbmc.LOGINFO)


def load_config():
    return {
        "server_address": ADDON.getSetting("server_address"),
        "device_name": ADDON.getSetting("device_name"),
        "idle_engine": ADDON.getSettingInt("idle_engine"),      # 0=window 1=picture
        "idle_type": ADDON.getSettingInt("idle_type"),          # 0=color 1=image
        "idle_color": ADDON.getSetting("idle_color") or "#000000",
        "letterbox_color": ADDON.getSetting("letterbox_color") or "#000000",
        "image_path": ADDON.getSetting("idle_image"),
        "scaling": ADDON.getSettingInt("idle_scaling"),         # 0=contain 1=cover 2=stretch
        "test_video": ADDON.getSetting("test_video"),
        "debug_overlay": ADDON.getSettingBool("poc_debug"),
    }


class SignagePlayer(xbmc.Player):
    """Playback monitor. The return-to-idle path is local and immediate —
    spec 7.5: no server round-trip, no Kodi UI between last frame and idle."""

    def __init__(self, on_return_to_idle):
        super().__init__()
        self._on_return_to_idle = on_return_to_idle
        self.playing = False

    def onAVStarted(self):
        self.playing = True
        log(f"playback started  t={time.perf_counter():.3f}")

    def onPlayBackEnded(self):
        self._finish("ended")

    def onPlayBackStopped(self):
        self._finish("stopped")

    def onPlayBackError(self):
        self._finish("error")

    def _finish(self, why):
        was_playing = self.playing
        self.playing = False
        if was_playing:
            log(f"playback {why}  t={time.perf_counter():.3f} -> showing idle")
            self._on_return_to_idle()


class IdleController:
    """Owns the idle presentation and the PoC test actions."""

    def __init__(self, config, player):
        self.config = config
        self.player = player
        self.window = None
        self.maintenance = False
        self.armed = False

    # -- idle presentation -------------------------------------------------

    def show_idle(self):
        if self.maintenance:
            return
        if self.config["idle_engine"] == 1:
            self._show_picture()
            return
        if self.window is None:
            self.window = IdleWindow(self.config)
            self.window.on_action = self._on_window_action
        self.window.show()
        log(f"idle shown (window) t={time.perf_counter():.3f}")

    def _show_picture(self):
        # Comparison engine: Kodi's built-in picture viewer. Keys cannot be
        # intercepted here — see README for what this proves/disproves.
        if self.config["idle_type"] == 1 and self.config["image_path"]:
            source = resolve_path(self.config["image_path"])
        else:
            source = SOLID_PNG
        xbmc.executebuiltin(f"ShowPicture({source})")
        log(f"idle shown (picture) t={time.perf_counter():.3f}")

    # -- PoC test actions (keyboard while idle window is focused) -----------
    #
    #   P or ENTER  play the configured test video fullscreen
    #   A           "arm" — start + immediately pause (does the first frame
    #               flash before the pause lands? spec open question #7)
    #   G           "go" — resume the armed/paused playback
    #   M           maintenance — deliberately reveal the Kodi UI, with an
    #               OK dialog to return to idle

    def _on_window_action(self, action):
        aid = action.getId()
        btn = action.getButtonCode()
        if aid in (xbmcgui.ACTION_SELECT_ITEM, xbmcgui.ACTION_PLAYER_PLAY) or (
            aid == letter_key("p") or btn == ord("p")
        ):
            self.play_test_video()
        elif aid == letter_key("a") or btn == ord("a"):
            self.arm_test_video()
        elif aid == letter_key("g") or btn == ord("g"):
            self.resume()
        elif aid == letter_key("m") or btn == ord("m"):
            self.enter_maintenance()
        # every other action (ESC, back, navigation) is swallowed by
        # IdleWindow.onAction not delegating to the base class.

    def play_test_video(self):
        path = resolve_path(self.config["test_video"])
        if not path:
            xbmcgui.Dialog().notification(
                "Screensemble", "No test video configured (addon settings)", xbmcgui.NOTIFICATION_WARNING
            )
            return
        log(f"play test video: {path}")
        self.armed = False
        self.player.play(path)

    def arm_test_video(self):
        path = resolve_path(self.config["test_video"])
        if not path:
            xbmcgui.Dialog().notification(
                "Screensemble", "No test video configured (addon settings)", xbmcgui.NOTIFICATION_WARNING
            )
            return
        log(f"ARM: play+pause {path} — watch for a first-frame flash over idle")
        self.player.play(path)
        for _ in range(200):
            if self.player.isPlaying():
                break
            xbmc.sleep(5)
        self.player.pause()
        self.armed = True
        log("ARMED (paused). Press G to resume.")

    def resume(self):
        if self.player.isPlaying():
            self.player.pause()
            self.armed = False
            log("GO: resumed playback")

    def enter_maintenance(self):
        self.maintenance = True
        if self.window is not None:
            self.window.close()
            self.window = None
        log("maintenance mode entered (Kodi UI revealed)")
        xbmcgui.Dialog().ok(
            "Screensemble maintenance",
            "Kodi UI is accessible. Press OK to return to the idle presentation.",
        )
        self.maintenance = False
        self.show_idle()


def main():
    config = load_config()
    log(
        "service starting "
        f"(device={config['device_name']!r} server={config['server_address']!r} "
        f"engine={config['idle_engine']} type={config['idle_type']})"
    )
    ensure_runtime_assets(config["idle_color"])

    monitor = xbmc.Monitor()
    player = SignagePlayer(on_return_to_idle=lambda: controller.show_idle())
    controller = IdleController(config, player)

    # Idle is shown before anything else; on PC Kodi the home window may
    # already exist by the time the service runs — on the target kiosk setup
    # the goal is for the idle window to be the first thing visible (spec 7.4).
    controller.show_idle()

    while not monitor.abortRequested():
        # Watchdog-lite: if the window engine is active, nothing is playing,
        # and we are not in maintenance, keep the idle window on top. This
        # covers Kodi navigating away unexpectedly.
        if (
            config["idle_engine"] == 0
            and not player.playing
            and not controller.maintenance
            and controller.window is not None
        ):
            controller.window.show()
        if monitor.waitForAbort(0.5):
            break

    log("service stopping")
    if controller.window is not None:
        controller.window.close()


if __name__ == "__main__":
    main()
