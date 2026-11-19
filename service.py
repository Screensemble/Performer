import os
import time

import xbmc
import xbmcaddon
import xbmcgui

from idle import (
    ADDON_PATH,
    IdleWindow,
    ensure_runtime_assets,
    letter_key,
    resolve_path,
    SOLID_PNG,
)

ADDON = xbmcaddon.Addon()
HOME = xbmcgui.Window(10000)

COMMAND_PROPERTY = "screensemble.command"
COMMAND_RESUME = "resume"
COMMAND_RELOAD = "reload"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts"}
BACK_ACTIONS = {
    xbmcgui.ACTION_PARENT_DIR,
    xbmcgui.ACTION_PREVIOUS_MENU,
    xbmcgui.ACTION_NAV_BACK,
}
# Remote "menu"-style buttons across platforms (Android TV menu, info, context).
MENU_ACTIONS = {
    getattr(xbmcgui, "ACTION_CONTEXT_MENU", 117),
    getattr(xbmcgui, "ACTION_MENU", 163),
    getattr(xbmcgui, "ACTION_SHOW_INFO", 11),
}
# Remote media buttons.
PLAY_ACTIONS = {getattr(xbmcgui, "ACTION_PLAYER_PLAY", 79)}
PAUSE_ACTIONS = {
    getattr(xbmcgui, "ACTION_PAUSE", 12),
    getattr(xbmcgui, "ACTION_PLAYER_PLAYPAUSE", 80),
}
BACK_EXIT_SECONDS = 3.0


def log(msg):
    xbmc.log(f"[screensemble] {msg}", xbmc.LOGINFO)


def notify(text, icon=xbmcgui.NOTIFICATION_INFO):
    xbmcgui.Dialog().notification("Screensemble", text, icon)


def load_config():
    # Fresh Addon() every call: the long-lived instance caches settings and
    # does NOT see writes made by the script entry point (e.g. pick()).
    addon = xbmcaddon.Addon()
    return {
        "setup_complete": addon.getSettingBool("setup_complete"),
        "player_mode": addon.getSettingInt("player_mode"),      # 0=local 1=server
        "server_address": addon.getSetting("server_address"),
        "device_name": addon.getSetting("device_name"),
        "local_media_folder": addon.getSetting("local_media_folder"),
        "slideshow_seconds": addon.getSettingInt("slideshow_seconds") or 10,
        "server_media_dir": addon.getSetting("server_media_dir"),
        "idle_engine": addon.getSettingInt("idle_engine"),      # 0=window 1=picture
        "idle_type": addon.getSettingInt("idle_type"),          # 0=color 1=image
        "idle_color": addon.getSetting("idle_color") or "#000000",
        "letterbox_color": addon.getSetting("letterbox_color") or "#000000",
        "image_path": addon.getSetting("idle_image"),
        "scaling": addon.getSettingInt("idle_scaling"),         # 0=contain 1=cover 2=stretch
        "test_video": addon.getSetting("test_video"),
        "debug_overlay": addon.getSettingBool("poc_debug"),
    }


class SettingsMonitor(xbmc.Monitor):
    """Flags addon settings changes so the loop can apply them live."""

    def __init__(self):
        super().__init__()
        self.dirty = False

    def onSettingsChanged(self):
        self.dirty = True


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
    """Owns the idle presentation, local-mode slideshow, and PoC test actions."""

    def __init__(self, config, player):
        self.config = config
        self.player = player
        self.window = None
        self.maintenance = False
        self.armed = False
        self._last_back = 0.0
        self._suppress_idle_until = 0.0  # grace window after play() calls
        self.slideshow_files = []
        self.video_files = []
        self._slide_index = 0
        self._slide_next = 0.0
        self.rescan_media()

    # -- idle presentation -------------------------------------------------

    def rescan_media(self):
        """Collect image and video files from the local media folder (Local mode)."""
        self.slideshow_files = []
        self.video_files = []
        folder = self.config.get("local_media_folder") or ""
        if self.config.get("player_mode") != 0 or not folder:
            return
        try:
            names = sorted(os.listdir(resolve_path(folder)))
            folder = resolve_path(folder)
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                if ext in IMAGE_EXTS:
                    self.slideshow_files.append(os.path.join(folder, n))
                elif ext in VIDEO_EXTS:
                    self.video_files.append(os.path.join(folder, n))
        except OSError as e:
            log(f"media folder not listable ({folder}): {e}")
        log(
            f"media scan: {len(self.video_files)} video(s), "
            f"{len(self.slideshow_files)} image(s) in {folder}"
        )

    def start_content(self):
        """Local-mode content entry point: loop the folder's videos if any,
        otherwise fall back to the idle presentation (slideshow/color)."""
        if self.video_files:
            playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
            playlist.clear()
            for path in self.video_files:
                playlist.add(path)
            self._suppress_idle_until = time.perf_counter() + 3.0
            self.player.play(playlist)
            xbmc.executebuiltin("PlayerControl(RepeatAll)")
            log(f"local playlist: {len(self.video_files)} video(s), RepeatAll")
        else:
            self.show_idle()

    def return_to_idle(self):
        """Called when playback finishes. For a video playlist, the next item
        starting counts as continued playback, not an end — give it a brief
        grace window before falling back to idle."""
        if self.video_files and not self.armed:
            deadline = time.perf_counter() + 1.5
            while time.perf_counter() < deadline:
                if self.player.playing:
                    return
                xbmc.sleep(50)
        self.show_idle()

    def show_idle(self):
        if self.maintenance:
            return
        if self.config["idle_engine"] == 1:
            self._show_picture()
            return
        if self.window is None:
            self.window = IdleWindow("idle.xml", ADDON_PATH, "Default", "1080i")
            self.window.on_action = self._on_window_action
        self.window.config = self._view_config()
        self.window.show()
        self.window.apply()  # onInit is unreliable for non-modal windows
        self._slide_next = time.perf_counter() + self.config["slideshow_seconds"]
        log(f"idle shown (window) t={time.perf_counter():.3f}")

    def _view_config(self):
        cfg = dict(self.config)
        if self.slideshow_files:
            cfg["idle_type"] = 1
            cfg["image_path"] = self.slideshow_files[self._slide_index]
        return cfg

    def idle_visible(self):
        """Is our idle window currently the active window?"""
        if self.window is None:
            return False
        try:
            return xbmcgui.getCurrentWindowId() == self.window.getId()
        except Exception:
            return True

    def _show_picture(self):
        # Comparison engine: Kodi's built-in picture viewer. Keys cannot be
        # intercepted here — see README for what this proves/disproves.
        if self.config["idle_type"] == 1 and self.config["image_path"]:
            source = resolve_path(self.config["image_path"])
        else:
            source = SOLID_PNG
        xbmc.executebuiltin(f"ShowPicture({source})")
        log(f"idle shown (picture) t={time.perf_counter():.3f}")

    def tick(self):
        """Periodic work: advance the slideshow when due."""
        if (
            self.window is None
            or self.player.playing
            or len(self.slideshow_files) < 2
            or time.perf_counter() < self._slide_next
        ):
            return
        self._show_slide((self._slide_index + 1) % len(self.slideshow_files))

    def _show_slide(self, index):
        self._slide_index = index
        self.window.set_idle_image(self.slideshow_files[index])
        self._slide_next = time.perf_counter() + self.config["slideshow_seconds"]

    def slide_skip(self, direction):
        """D-pad manual skip: right/down = next, left/up = previous."""
        if len(self.slideshow_files) < 2 or self.window is None:
            return
        self._show_slide((self._slide_index + direction) % len(self.slideshow_files))
        log(f"slide {self._slide_index + 1}/{len(self.slideshow_files)} (manual)")

    # -- input -------------------------------------------------------------
    # Everything here must work with a plain D-pad remote (Android TV etc.):
    #
    #   OK / Select / Play   play the configured test video fullscreen
    #   Menu / Info          open the actions menu (all PoC functions)
    #   Pause                "go" — resume armed/paused playback
    #   Back x2              exit to the Kodi UI (resume via addon menu)
    #
    # Keyboard letters (P/A/G/M) kept as equivalents for desktop testing.

    def _on_window_action(self, action):
        aid = action.getId()
        btn = action.getButtonCode()
        if aid in BACK_ACTIONS:
            self._handle_back()
        elif aid in MENU_ACTIONS:
            self._open_actions_menu()
        elif aid in PAUSE_ACTIONS:
            self.resume()
        elif aid in (xbmcgui.ACTION_MOVE_RIGHT, xbmcgui.ACTION_MOVE_DOWN):
            self.slide_skip(1)
        elif aid in (xbmcgui.ACTION_MOVE_LEFT, xbmcgui.ACTION_MOVE_UP):
            self.slide_skip(-1)
        elif (
            aid in (xbmcgui.ACTION_SELECT_ITEM,)
            or aid in PLAY_ACTIONS
            or aid == letter_key("p")
            or btn == ord("p")
        ):
            self.play_test_video()
        elif aid == letter_key("a") or btn == ord("a"):
            self.arm_test_video()
        elif aid == letter_key("g") or btn == ord("g"):
            self.resume()
        elif aid == letter_key("m") or btn == ord("m"):
            self.exit_to_kodi()
        # every other action is swallowed — IdleWindow.onAction never calls
        # the base implementation, so navigation cannot close the window.

    def _open_actions_menu(self):
        actions = [
            ("Play test video", self.play_test_video),
            ("Arm (play + pause)", self.arm_test_video),
            ("Go (resume playback)", self.resume),
            ("Restart local content", self.start_content),
            ("Exit to Kodi UI", self.exit_to_kodi),
        ]
        choice = xbmcgui.Dialog().select("Screensemble", [a[0] for a in actions])
        if choice >= 0:
            actions[choice][1]()

    def _handle_back(self):
        now = time.perf_counter()
        if self._last_back and now - self._last_back < BACK_EXIT_SECONDS:
            self.exit_to_kodi()
        else:
            self._last_back = now
            notify("Press back again to exit Screensemble")

    # -- PoC test actions ---------------------------------------------------

    def play_test_video(self):
        path = resolve_path(self.config["test_video"])
        if not path:
            notify("No test video configured (addon settings)", xbmcgui.NOTIFICATION_WARNING)
            return
        log(f"play test video: {path}")
        self.armed = False
        self._suppress_idle_until = time.perf_counter() + 3.0
        self.player.play(path)

    def arm_test_video(self):
        path = resolve_path(self.config["test_video"])
        if not path:
            notify("No test video configured (addon settings)", xbmcgui.NOTIFICATION_WARNING)
            return
        log(f"ARM: play+pause {path} — watch for a first-frame flash over idle")
        self._suppress_idle_until = time.perf_counter() + 3.0
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

    def exit_to_kodi(self):
        self.maintenance = True
        if self.window is not None:
            self.window.close()
            self.window = None
        log("exited to Kodi UI — resume via Programs > Screensemble")
        notify("Idle exited. Resume from Programs > Screensemble")

    def resume_from_menu(self):
        self.maintenance = False
        self.rescan_media()
        self.start_content()

    def refresh_view(self):
        """Re-apply current view config to the visible idle window (after a
        settings change). Does not interrupt playback."""
        if self.window is not None and not self.player.playing:
            self.window.config = self._view_config()
            self.window.apply()


def main():
    config = load_config()
    log(
        "service starting "
        f"(device={config['device_name']!r} mode={config['player_mode']} "
        f"server={config['server_address']!r} setup={config['setup_complete']})"
    )
    ensure_runtime_assets(config["idle_color"])

    # Unattended signage: never let Kodi's own screensaver/dim take over
    # the display. Released again in the shutdown path.
    xbmc.executebuiltin("InhibitScreensaver(true)")

    monitor = SettingsMonitor()
    controller = IdleController(config, SignagePlayer(lambda: controller.return_to_idle()))
    player = controller.player

    if config["setup_complete"]:
        controller.start_content()
    else:
        log("setup not complete — staying hidden until the wizard runs")

    while not monitor.abortRequested():
        # Command channel from the script entry point (Programs > Screensemble).
        cmd = HOME.getProperty(COMMAND_PROPERTY)
        if cmd:
            HOME.clearProperty(COMMAND_PROPERTY)
            if cmd == COMMAND_RESUME:
                controller.resume_from_menu()
            elif cmd == COMMAND_RELOAD:
                monitor.dirty = True

        # Live settings changes (folder changed, slideshow interval, colors).
        if monitor.dirty:
            monitor.dirty = False
            config = load_config()
            ensure_runtime_assets(config["idle_color"])
            controller.config = config
            controller.rescan_media()
            controller.refresh_view()
            log("settings reloaded")

        if not config["setup_complete"]:
            # Poll for wizard completion; then bring the idle presentation up.
            # Fresh Addon() — the cached instance misses script-instance writes.
            if xbmcaddon.Addon().getSettingBool("setup_complete"):
                config = load_config()
                ensure_runtime_assets(config["idle_color"])
                controller.config = config
                controller.rescan_media()
                controller.start_content()
                log("setup complete — content started")
        elif (
            config["idle_engine"] == 0
            and not player.playing
            and not controller.maintenance
            and time.perf_counter() >= controller._suppress_idle_until
        ):
            # Only re-show if our window actually lost the foreground —
            # repeated show() re-triggers onInit and resets the slide.
            if not controller.idle_visible():
                controller.show_idle()
            controller.tick()

        if monitor.waitForAbort(0.5):
            break

    log("service stopping")
    if controller.window is not None:
        controller.window.close()
    xbmc.executebuiltin("InhibitScreensaver(false)")


if __name__ == "__main__":
    main()
