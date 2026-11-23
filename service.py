import json
import os
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from standby import (
    ADDON_PATH,
    StandbyWindow,
    ensure_runtime_assets,
    resolve_path,
)
import medialist
from medialist import IMAGE_EXTS, VIDEO_EXTS
from maintenance import MaintenanceWindow
from splash import SplashWindow
from web import AdminServer, post_json

ADDON = xbmcaddon.Addon()
HOME = xbmcgui.Window(10000)

COMMAND_PROPERTY = "screensemble.command"
COMMAND_RESUME = "resume"
COMMAND_RELOAD = "reload"
DEFAULT_SLIDE_SECONDS = 10.0  # per-item playlist durations override this
PROMO_SECONDS = 8.0           # feature promos when the playlist is empty
PROMO_DIR = os.path.join(ADDON_PATH, "resources", "media")
# Managed displays show a static brand standby instead of promos while
# waiting for the server — self-advertising makes no sense there.
STANDBY_IMG = os.path.join(PROMO_DIR, "standby.png")
PROP_PAIR_URL = "screensemble.pairurl"     # set by web /pair — offer pending
PROP_PAIR_INFO = "screensemble.pairinfo"   # server /api/info JSON
PROP_PAIR_ID = "screensemble.pairid"       # server's pairing_id

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
BACK_EXIT_SECONDS = 3.0

# Legacy: the opt-in Back=Stop keymap was replaced by fullscreen-exit
# detection in the poll loop. Remove a leftover file if an older version
# installed one — it lives in userdata and persists across upgrades.
LEGACY_KEYMAP = os.path.join(
    xbmcvfs.translatePath("special://userdata"), "keymaps", "screensemble.xml"
)


def remove_legacy_keymap():
    if os.path.exists(LEGACY_KEYMAP):
        os.remove(LEGACY_KEYMAP)
        log("removed leftover Screensemble keymap (no longer needed)")


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

        "server_media_dir": addon.getSetting("server_media_dir"),
        "cache_max_mb": addon.getSettingInt("cache_max_mb"),
        "idle_type": addon.getSettingInt("idle_type"),          # 0=color 1=image
        "idle_color": addon.getSetting("idle_color") or "#000000",
        "letterbox_color": addon.getSetting("letterbox_color") or "#000000",
        "image_path": addon.getSetting("idle_image"),
        "scaling": addon.getSettingInt("idle_scaling"),         # 0=contain 1=cover 2=stretch
        "ensemble_hold": addon.getSettingBool("ensemble_hold"), # idle until conductor performs
        "loop_count": addon.getSettingInt("loop_count"),        # 0=forever
        "schedule_on": addon.getSettingBool("schedule_on"),     # display hours gate
        "schedule_days": addon.getSetting("schedule_days"),
        "schedule_from": addon.getSetting("schedule_from"),
        "schedule_to": addon.getSetting("schedule_to"),
        "off_image": addon.getSetting("off_image"),             # off-hours surface (empty=black)

        "update_check": addon.getSettingBool("update_check"),
        "update_time": addon.getSetting("update_time") or "00:00",
        "web_admin": addon.getSettingBool("web_admin"),
        "maintenance_timeout": addon.getSettingInt("maintenance_timeout"),
        "maintenance_ui": addon.getSettingBool("maintenance_ui"),
        "web_port": addon.getSettingInt("web_port") or 8282,
        "splash_seconds": addon.getSettingInt("splash_seconds"),
        "splash_show_server": addon.getSettingBool("splash_show_server"),
        "splash_show_qr": addon.getSettingBool("splash_show_qr"),
        "server_auth_token": addon.getSetting("server_auth_token"),
        "local_lock": addon.getSettingBool("local_lock"),
        "local_password": addon.getSetting("local_password"),
        "tls_insecure": addon.getSettingBool("tls_insecure"),
    }


class SettingsMonitor(xbmc.Monitor):
    """Flags addon settings changes so the loop can apply them live."""

    def __init__(self):
        super().__init__()
        self.dirty = False

    def onSettingsChanged(self):
        self.dirty = True


class SignagePlayer(xbmc.Player):
    """Playback monitor. The return-to-standby path is local and immediate —
    spec 7.5: no server round-trip, no Kodi UI between last frame and standby."""

    def __init__(self, on_return_to_standby, on_av_started=None):
        super().__init__()
        self._on_return_to_standby = on_return_to_standby
        self._on_av_started = on_av_started
        self.playing = False

    def onAVStarted(self):
        self.playing = True
        if self._on_av_started:
            self._on_av_started()

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
            self._on_return_to_standby()


class StandbyController:
    """Owns the standby presentation and local-mode content."""

    def __init__(self, config, player):
        self.config = config
        self.player = player
        self.window = None
        self.window_open = False          # standby window resident via doModal()
        self.content_pending = False      # start content once the window is up
        self.content_due = 0.0
        self.maintenance = False
        self._maint_since = 0.0
        self._maint_ui_dismissed = False  # maintenance window backed out this session
        self._offline_since = None   # managed link down since (degraded admin)
        self._last_back = 0.0
        self.slideshow_files = []
        self.video_files = []
        self.sequence = []              # interleaved play order (dicts)
        self._seq_pos = -1              # index into sequence
        self._seq_playing = False       # a sequence video is in flight
        self._seq_ended_since = None    # sustained-silence detector
        self._last_play_time = None     # player clock, stall detection
        self._stall_since = None        # when the frozen clock began
        self._pl_base = 0               # playlist idx 0 maps to this pos
        self._seq_started = 0.0         # when the current item began
        self._previewing = False        # preview interrupted the sequence
        self._slide_next = 0.0
        self._promo_files = []          # feature cards for empty playlists
        self._update_last = None        # date key of the last update check
        self._update_deferred = None    # date key of a deferred check (log once)
        self._promo_index = 0
        self.prepared = False              # conductor: staged, awaiting "go"
        self._prepare_pending = False       # pause the moment a video opens
        self._perform_deadline = None        # mono ms deadline for a synced go
        self._run_active = False            # a performed run is playing out
        self._run_passes = 0                # full sequence passes this run
        self._run_loops = 0                 # pass limit (0 = loop forever)
        self._sched_off = False             # inside display-hours off window
        self._pair_dialog = False   # on-TV pair confirm currently showing
        self._pending_refresh = False  # content change waits for a boundary
        self._pulls_inflight = 0    # server pulls still downloading
        self._pull_settle = None    # batch settle deadline before flagging
        self.rescan_media()

    # -- standby presentation -------------------------------------------------

    def _media_folder(self):
        # Managed displays play the server cache folder; local displays
        # the configured local folder. Both fall back to the built-in dir.
        return (
            self.config.get("server_media_dir")
            if self.config.get("player_mode") == 1
            else self.config.get("local_media_folder")
        ) or medialist.DEFAULT_MEDIA_DIR

    def _media_sources(self, state):
        """Media folder scan plus URL entries -> (paths, kinds) name maps."""
        paths = {}   # display name -> playable path or URL
        kinds = {}   # display name -> "video" | "image"
        folder = self._media_folder()
        if folder:
            try:
                folder = resolve_path(folder)
                for n in os.listdir(folder):
                    if not os.path.isfile(os.path.join(folder, n)):
                        continue
                    ext = os.path.splitext(n)[1].lower()
                    if ext in VIDEO_EXTS:
                        kinds[n] = "video"
                    elif ext in IMAGE_EXTS:
                        kinds[n] = "image"
                    else:
                        continue
                    paths[n] = os.path.join(folder, n)
            except OSError as e:
                log(f"media folder not listable ({folder}): {e}")
        for u in state["urls"]:
            ext = os.path.splitext(u["url"].split("?")[0])[1].lower()
            kinds[u["name"]] = "image" if ext in IMAGE_EXTS else "video"
            paths[u["name"]] = u["url"]
        return paths, kinds

    def _build_catalog(self, paths, kinds, state):
        """Every known item in stored order (pre-active filter) — the
        proxied admin API lists disabled/scheduled-off items too."""
        catalog = []
        url_names = {u["name"] for u in state["urls"]}
        for n in medialist.sort_names(list(paths), state):
            size = None
            if n not in url_names:
                try:
                    size = os.path.getsize(paths[n])
                except OSError:
                    pass
            catalog.append(
                {"name": n, "kind": kinds[n],
                 "remote": n in url_names, "size": size}
            )
        return catalog

    def refresh_catalog(self):
        """Rebuild the admin-facing listing without touching the play
        sequence — mutations show immediately while playback changes
        still wait for the next item boundary."""
        state = medialist.load_state()
        self._media_state = state
        paths, kinds = self._media_sources(state)
        self.catalog = self._build_catalog(paths, kinds, state)

    def rescan_media(self):
        """Build the effective playlist: media-folder files plus remote URL
        entries, in playlist order, skipping disabled items and items
        whose schedule doesn't match right now."""
        self.slideshow_files = []
        self.video_files = []
        state = medialist.load_state()
        self._media_state = state
        disabled = set(state["disabled"])
        scheds = state["schedules"]

        def active(name):
            return name not in disabled and medialist.schedule_active(
                scheds.get(name)
            )

        paths, kinds = self._media_sources(state)
        self.catalog = self._build_catalog(paths, kinds, state)
        names_all = [s["name"] for s in self.catalog]
        names = [n for n in names_all if active(n)]
        # Manifest touch: playlist members stay hot in the cache —
        # evictions expire least-recently-touched orphans first. The cap
        # only applies to the managed cache, never the local folder.
        medialist.touch_manifest(names_all)
        if self.config.get("player_mode") == 1:
            medialist.enforce_cache_limit(
                resolve_path(self._media_folder()),
                (self.config.get("cache_max_mb") or 0) * 1048576,
            )
        # Unified interleaved sequence — the playlist order is honored
        # across types (img, vid, img plays exactly in that order). The
        # split lists are kept for status/UI counts.
        self.sequence = [
            {"name": n, "kind": kinds[n], "path": paths[n]} for n in names
        ]
        for n in names:
            (self.video_files if kinds[n] == "video"
             else self.slideshow_files).append(paths[n])
        self._name_of = {paths[n]: n for n in names}
        try:
            self._promo_files = sorted(
                os.path.join(PROMO_DIR, n)
                for n in os.listdir(PROMO_DIR)
                if n.lower().startswith("promo")
                and n.lower().endswith(".png")
            )
        except OSError:
            self._promo_files = []
        self._promo_index = min(
            self._promo_index, max(len(self._promo_files) - 1, 0)
        )
        log(
            f"media scan: {len(self.video_files)} video(s), "
            f"{len(self.slideshow_files)} image(s) in {self._media_folder()}"
        )

    def request_content(self, delay=4.0):
        """Queue local content; it starts once the standby window is resident so
        video always opens over it (never the standby window over video). Once
        the window is up, a short settle is enough — a long wait just shows
        the (often black) underlay between items."""
        self.content_pending = True
        if self.window_open:
            delay = min(delay, 0.75)
        self.content_due = time.perf_counter() + delay

    # -- deferred content updates ------------------------------------------
    # Edits, reorders and pushed media never cut playback mid-piece: the
    # change is queued and applies at the next natural item boundary —
    # the current video ends or the image timer expires. Server pushes
    # land on background threads and only queue the flag once the whole
    # batch has settled.

    def defer_content_refresh(self):
        """Queue a content update for the next item boundary so the
        current piece finishes seamlessly. The admin catalog rebuilds
        immediately — it is just a listing, so deletes/edits show up in
        the UIs right away while only playback waits."""
        self.refresh_catalog()
        self._pending_refresh = True
        log("content update queued for the next item boundary")

    def _apply_pending_refresh(self, just_ended=None):
        """Boundary reached — rescan, then continue the flow from the
        just-finished item's new position (restart if it was removed).
        For videos the next image is already preloaded beneath, so the
        cut stays seamless."""
        self._pending_refresh = False
        self.rescan_media()
        if not self.sequence:
            self._seq_playing = False
            self.refresh_view()
            return
        idx = -1
        if just_ended:
            idx = next(
                (i for i, s in enumerate(self.sequence)
                 if s["name"] == just_ended),
                -1,
            )
        self._seq_pos = idx
        self._advance_seq()

    def pull_started(self):
        self._pulls_inflight += 1
        self._pull_settle = None

    def pull_finished(self):
        self._pulls_inflight = max(0, self._pulls_inflight - 1)
        if not self._pulls_inflight:
            # Pushes arrive as a batch — the update queues only once the
            # last file has landed, after a short settle window.
            self._pull_settle = time.perf_counter() + 3.0

    def start_content(self):
        """Local-mode content entry point: start the unified playlist
        sequence, or just refresh the standby presentation if it's empty."""
        self.content_pending = False
        self.prepared = False
        if self.sequence:
            self._seq_pos = -1
            self._advance_seq()
            log(f"playlist sequence: {len(self.sequence)} item(s)")
        else:
            self.refresh_view()

    def _seq_duration(self, item):
        """How long an image item stays up — per-item duration wins,
        then the 20s default. Videos play to their natural end."""
        dur = (getattr(self, "_media_state", None) or {}).get(
            "durations", {}
        ).get(item["name"], DEFAULT_SLIDE_SECONDS)
        return max(float(dur or DEFAULT_SLIDE_SECONDS), 1.0)

    def _play_seq(self):
        """Start the sequence item at _seq_pos: videos play fullscreen
        once (end-of-playback advances the sequence); images render on
        the resident standby window for their duration."""
        if not (0 <= self._seq_pos < len(self.sequence)):
            return
        if (
            self.config.get("ensemble_hold")
            and not self._run_active
            and not self.prepared
        ):
            # Ensemble-held — the display idles until a conductor 'perform'
            # starts a run; the local sequence never starts on its own.
            return
        if self._sched_off and not self._run_active and not self.prepared:
            # Display hours closed — the ambient sequence stays off; an
            # armed stage or a performed run overrides signage hours.
            return
        item = self.sequence[self._seq_pos]
        self._seq_started = time.perf_counter()
        if item["kind"] == "video":
            self._seq_playing = True
            seq = self.sequence
            # Chain every consecutive video (wraparound-aware) into one
            # Kodi playlist — the player then transitions internally with
            # zero reveal. The chain stops at the first image; that image
            # is preloaded under the last video in on_av_started.
            pl = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
            pl.clear()
            pl.add(item["path"])
            n = (self._seq_pos + 1) % len(seq)
            while n != self._seq_pos and seq[n]["kind"] == "video":
                pl.add(seq[n]["path"])
                n = (n + 1) % len(seq)
            self._pl_base = self._seq_pos   # playlist index 0 == this pos
            # Underlay for the load gap only. A staged playlist image
            # holds on screen until the first frame — artwork is always a
            # fine interstitial, even when the previous item differs
            # (restarts jump positions). Anything else — standby card,
            # promo, cleared — gets a clean black cut.
            holding = (
                self.window_open
                and self.window._last_img in self._name_of
            )
            if self.window_open and not holding:
                self.window.clear_standby_image()
            self.player.play(pl)
            log(f"seq {self._seq_pos + 1}/{len(self.sequence)} video: {item['name']}")
        else:
            self._seq_playing = False
            if self.window_open:
                self.window.set_standby_image(item["path"])
            self._slide_next = self._seq_started + self._seq_duration(item)
            log(f"seq {self._seq_pos + 1}/{len(self.sequence)} image: {item['name']}")

    def _advance_seq(self, delta=1):
        if not self.sequence:
            self._seq_playing = False
            return
        new_pos = (self._seq_pos + delta) % len(self.sequence)
        if (
            self._run_active
            and delta > 0
            and self._seq_pos >= 0
            and new_pos <= self._seq_pos
        ):
            # Pass boundary — a performed run ends after loop_count passes
            # and the display re-arms instead of wrapping forever.
            self._run_passes += 1
            if self._run_loops and self._run_passes >= self._run_loops:
                self._end_run()
                return
        self._seq_pos = new_pos
        self._play_seq()

    def _end_run(self):
        """A counted run finished its last pass — return to 'Arm': stop
        playback, show the idle surface, and stage item 0 for the next
        'go'. Display never sits on a dead end."""
        self._run_active = False
        self._seq_playing = False
        self._seq_ended_since = None
        if self.player.isPlaying():
            self.player.stop()
        log("run complete — re-armed")
        self.prepare()

    def return_to_standby(self):
        """Called when playback finishes. The standby window is still resident
        beneath the video window — Kodi reveals it instantly, so there is
        nothing to re-show here."""

    # -- conductor (managed mode) ------------------------------------------

    def cancel(self):
        """Conductor disarm — drop staged/run state synchronously and
        resume the ambient sequence. Unlike 'restart' the session flags
        clear immediately, so a prepare landing right behind it can no
        longer be wiped by a deferred content start."""
        self.prepared = False
        self._perform_deadline = None
        self._run_active = False
        self.content_pending = False
        if self.player.isPlaying():
            self.player.stop()
        self.request_content(0.5)

    def prepare(self):
        """Stage the sequence at item 0 and hold: a video opens paused on
        its first frame (the monitor pauses it on AV start), an image is
        shown statically. tick() does not advance while prepared — 'go'
        releases every staged display at once."""
        # A queued ambient restart (deferred request_content) must never
        # wipe a stage that landed after it — last write wins.
        self.content_pending = False
        self.rescan_media()
        self.prepared = bool(self.sequence)
        if not self.prepared:
            self.refresh_view()     # empty playlist -> promo cards
            return
        self._previewing = False
        self._seq_pos = 0
        self._seq_started = time.perf_counter()
        item = self.sequence[0]
        if item["kind"] == "video":
            self._prepare_pending = True
            self._seq_playing = True
            # Windowed staging — the video decodes behind the standby window
            # and pauses on its first frame, but fullscreenvideo never
            # activates, so Kodi's pause seekbar can never satisfy its
            # visible-condition. Releasing goes fullscreen at 'go'.
            self.player.play(item["path"], windowed=True)
            xbmc.executebuiltin("PlayerControl(RepeatOff)")
        else:
            self._seq_playing = False
            if self.window_open:
                self.window.set_standby_image(item["path"])
        log(f"conductor prepare — staged {item['name']}")

    def perform(self, at_mono=None):
        """Release the prepared sequence: resume a staged video or start the
        staged image's timer. With nothing staged it just restarts
        content. `at_mono` (display monotonic ms) defers the release to a
        shared deadline so synced displays all start on the same instant."""
        if at_mono is not None:
            delay_ms = at_mono - time.monotonic() * 1000
            if delay_ms > 0:
                self._perform_deadline = time.monotonic() + delay_ms / 1000
                log(f"conductor perform scheduled in {delay_ms:.0f}ms")
                return
            # Deadline already passed — release immediately below.
        self._release()

    def _release(self):
        self._perform_deadline = None
        # A performed run counts passes — when loop_count hits, the run ends
        # and the display re-arms (idle surface + item 0 staged again).
        # _run_active is set even when nothing was prepared: under
        # ensemble_hold the _play_seq gate keys on it, and a bare 'perform'
        # must start the run from cold too.
        self._run_passes = 0
        self._run_loops = self.config.get("loop_count") or 0
        self._run_active = True
        if not self.prepared:
            self.request_content()
            return
        self.prepared = False
        if self._seq_playing:
            if self.player.playing and xbmc.getCondVisibility(
                "Player.Paused"
            ):
                self.player.pause()      # resume the staged video
                # The staged video played windowed — going fullscreen now
                # is the reveal (resume alone re-enters fullscreen, but ask
                # explicitly in case Kodi skips the transition).
                xbmc.executebuiltin("ActivateWindow(fullscreenvideo)")
            elif not self.player.playing:
                self._seq_playing = False
                self._play_seq()         # staged video never opened
        elif 0 <= self._seq_pos < len(self.sequence):
            self._slide_next = (
                time.perf_counter()
                + self._seq_duration(self.sequence[self._seq_pos])
            )
        log("conductor perform — sequence released")

    def on_av_started(self):
        """Fired by the player monitor on AV start: an prepared video pauses
        on its first frame so 'go' is the only thing that moves it."""
        if self._prepare_pending:
            self._prepare_pending = False
            self.player.pause()
            log("prepared video paused on first frame")
        if self._seq_playing and not self._previewing and self.sequence:
            pl = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
            plpos = pl.getposition()
            if plpos >= 0:
                # A chained playlist advanced internally — sync the
                # engine's position without re-playing anything.
                new_pos = (self._pl_base + plpos) % len(self.sequence)
                if self._run_active and new_pos < self._seq_pos:
                    self._run_passes += 1  # RepeatAll wrap inside the chain
                self._seq_pos = new_pos
                self._seq_started = time.perf_counter()
                if plpos == pl.size() - 1:
                    # Last video of the chain: preload the next item under
                    # it so the end-of-playback reveal is instant.
                    after = self.sequence[
                        (self._seq_pos + 1) % len(self.sequence)
                    ]
                    if after["kind"] == "image" and self.window_open:
                        self.window.set_standby_image(after["path"])
            # PlayerControl only takes effect on a live player — calling
            # it before play() is a no-op, so repeat mode is set here.
            # A chain covering the whole sequence (all-video) loops
            # internally forever: zero-gap, including wraparound — unless
            # a counted run is playing out, which must end at the boundary.
            if pl.size() >= len(self.sequence) and not self._run_loops:
                xbmc.executebuiltin("PlayerControl(RepeatAll)")
            else:
                xbmc.executebuiltin("PlayerControl(RepeatOff)")

    def run_standby(self, abort_check):
        """Blocking: open the standby window modally and keep it resident. The
        fullscreen video window opens over it, so on playback stop Kodi
        reveals the standby screen with zero gap — no skin re-parse.

        Refuses to open while video is on top: activating a modal window
        would cover the playing video. Waits until playback is done instead."""
        if self.maintenance:
            xbmc.sleep(500)
            return
        waited = 0.0
        while self.player.playing and waited < 5.0 and not abort_check():
            xbmc.sleep(100)
            waited += 0.1
        win = StandbyWindow("standby.xml", ADDON_PATH, "Default", "1080i")
        win.on_action = self._on_window_action
        win.config = self._view_config()
        self.window = win
        self._slide_next = time.perf_counter() + DEFAULT_SLIDE_SECONDS
        self.window_open = True
        threading.Thread(
            target=self._watch_abort,
            args=(abort_check, lambda: self.window_open),
            daemon=True,
        ).start()
        win.doModal()  # returns when exit_to_kodi()/shutdown closes it
        win._closed = True  # dead — Kodi may keep it in the window stack
        self.window_open = False
        self.window = None

    def _watch_abort(self, abort, alive):
        """Kodi stops a service by flagging abortRequested — but the main
        thread is parked inside a modal doModal and cannot notice until the
        window closes, so the whole interpreter (web admin, link, window
        callbacks) zombies on indefinitely, still eating input. Post backs
        so the modal's own handler tears it down on the GUI thread; direct
        close() here would wedge on the GUI bridge lock."""
        while alive() and not abort():
            time.sleep(0.4)
        for _ in range(6):
            if not alive():
                break
            xbmc.executebuiltin("Action(back)")
            time.sleep(0.5)

    def _view_config(self):
        cfg = dict(self.config)
        cur = (
            self.sequence[self._seq_pos]
            if 0 <= self._seq_pos < len(self.sequence) else None
        )
        if self._sched_off and not self._run_active:
            # Off-hours surface — the pushed off-hours image, else plain
            # black. Deliberate and static; a performed run still covers it.
            if self.config.get("off_image"):
                cfg["idle_type"] = 1
                cfg["image_path"] = self.config["off_image"]
            else:
                cfg["idle_type"] = 0
                cfg["idle_color"] = "#000000"
            return cfg
        if cur and cur["kind"] == "image":
            cfg["idle_type"] = 1
            cfg["image_path"] = cur["path"]
        elif self.prepared or (
            self.config.get("ensemble_hold") and not self._run_active
        ):
            # Armed / ensemble-held — the staged video hides windowed+paused
            # behind this surface: the pushed idle image, else the brand
            # standby, else a promo card. Never a bare color while waiting.
            if self.config.get("image_path"):
                cfg["idle_type"] = 1
            elif os.path.exists(STANDBY_IMG):
                cfg["idle_type"] = 1
                cfg["image_path"] = STANDBY_IMG
            elif self._promo_files:
                cfg["idle_type"] = 1
                cfg["image_path"] = self._promo_files[self._promo_index]
            else:
                cfg["idle_type"] = 0
        elif not self.sequence:
            if self.config.get("player_mode") == 1 \
                    and os.path.exists(STANDBY_IMG):
                # Managed + nothing queued — static dimmed brand standby.
                cfg["idle_type"] = 1
                cfg["image_path"] = STANDBY_IMG
            elif self._promo_files:
                # Empty playlist — a feature promo is always better than
                # a bare color field.
                cfg["idle_type"] = 1
                cfg["image_path"] = self._promo_files[self._promo_index]
        else:
            # Video item current — solid background only, so the open
            # latency between items shows a cut, not a logo flash.
            cfg["idle_type"] = 0
        return cfg

    def current_name(self):
        """Name of the playlist item on screen — the playing file for
        videos, the shown image for image items."""
        # getPlayingFile() logs an exception on Kodi's side whenever nothing
        # is playing even though the catch swallows it — gate on the cached
        # flag so image slides don't spam the log every tick.
        if self.player.playing:
            try:
                cur = self.player.getPlayingFile() or ""
                if cur:
                    return os.path.basename(cur)
            except Exception:
                pass
        if 0 <= self._seq_pos < len(self.sequence):
            item = self.sequence[self._seq_pos]
            if item["kind"] == "image" and self.window_open:
                return item["name"]
        return ""

    def standby_visible(self):
        """Is our standby window currently the active window?"""
        if self.window is None:
            return False
        try:
            return xbmcgui.getCurrentWindowId() == self.window.getId()
        except Exception:
            return True

    def close_standby(self):
        if self.window is not None:
            self.window.close()

    def _sched_now_off(self):
        """Is the display-hours schedule currently closed? Reuses the
        per-item schedule evaluator — same days/from/to semantics,
        overnight windows included."""
        if not self.config.get("schedule_on"):
            return False
        days = [
            int(d) for d in
            str(self.config.get("schedule_days") or "").split(",")
            if d.strip().isdigit()
        ]
        return not medialist.schedule_active({
            "days": days,
            "from": self.config.get("schedule_from"),
            "to": self.config.get("schedule_to"),
        })

    def tick(self):
        """Periodic work: advance the slideshow when due, and re-scan on a
        timer while any item carries a time-window schedule so entries
        appear/disappear on their boundaries."""
        # The media manager window reads this to mark the playing row —
        # covers images (no player file) as well as videos.
        HOME.setProperty("screensemble.now", self.current_name())
        # Synced 'go' — release exactly at the shared deadline. The tick
        # cadence (~50-100ms) bounds the overshoot; displays land within a
        # frame or two of each other on a normal LAN.
        if self._perform_deadline and time.monotonic() >= self._perform_deadline:
            if self.prepared:
                log("conductor perform — deadline reached")
                self._release()
            else:
                self._perform_deadline = None  # cancelled by stop/disarm
        # Server pushes download in the background — the content update
        # only queues once the batch has settled (nothing in flight, and
        # a short grace so back-to-back files count as one batch).
        if (
            self._pull_settle
            and not self._pulls_inflight
            and time.perf_counter() >= self._pull_settle
        ):
            self._pull_settle = None
            self.defer_content_refresh()

        # Daily addon-update check: UpdateAddonRepos makes Kodi re-fetch
        # every repository index; its installer then stops this service,
        # swaps the files and restarts — the addon only pulls the trigger.
        # A landed update kills playback and tears down the windows, so a
        # staged performance defers it to the next tick where nothing is
        # armed; fire_key still makes it once-per-day, not once-per-attempt.
        if self.config.get("update_check"):
            now = time.localtime()
            hh, _, mm = (self.config.get("update_time") or "0:0").partition(":")
            fire_key = (now.tm_year, now.tm_mon, now.tm_mday)
            # Fire only inside the hour starting at update_time — the old
            # "first tick at/after" catch-up meant a box booted mid-day
            # installed updates mid-presentation; a gallery box must keep
            # the swap inside the dead-hour window. Also means a service
            # restart outside the window can't re-fire the same day's check.
            span = (
                (now.tm_hour * 60 + now.tm_min)
                - (int(hh or 0) * 60 + int(mm or 0))
            ) % 1440
            due = self._update_last != fire_key and span < 60
            if due and (self.prepared or self._perform_deadline):
                if self._update_deferred != fire_key:
                    self._update_deferred = fire_key
                    log("daily update check deferred — performance staged")
            elif due:
                self._update_last = fire_key
                log("daily update check — refreshing addon repositories")
                xbmc.executebuiltin("UpdateAddonRepos")
                xbmc.executebuiltin("UpdateLocalAddons")

        state = getattr(self, "_media_state", None) or {}
        if (
            medialist.has_schedules(state)
            and time.perf_counter() >= getattr(self, "_sched_next", 0.0)
        ):
            self._sched_next = time.perf_counter() + 30.0
            before = list(self.sequence)
            cur_name = (
                self.sequence[self._seq_pos]["name"]
                if 0 <= self._seq_pos < len(self.sequence) else None
            )
            self.rescan_media()
            if self.sequence != before:
                log("schedule boundary — content set changed, queued")
                # Keep _seq_pos honest under the new order, then let the
                # current piece finish before the change takes effect.
                if cur_name:
                    self._seq_pos = next(
                        (i for i, s in enumerate(self.sequence)
                         if s["name"] == cur_name),
                        -1,
                    )
                self.defer_content_refresh()

        # A preview interrupted the sequence — resume it once the preview
        # ends (video stops, or the image's minute is up).
        if self._previewing:
            if (
                not self.player.playing
                and time.perf_counter() >= self._slide_next
            ):
                self._previewing = False
                self.request_content()
            return

        # Nothing playing — a queued update applies immediately.
        if self._pending_refresh and not self.sequence:
            self._apply_pending_refresh()

        # Display hours — outside the window the ambient signage is off:
        # playback stops and the off surface (custom image or black) shows
        # until the window reopens. Armed/prepared/run state overrides —
        # a conductor-triggered performance ignores signage hours.
        off_now = self._sched_now_off()
        if off_now != self._sched_off:
            self._sched_off = off_now
            if off_now:
                if (
                    self._seq_playing and self.player.playing
                    and not (self._run_active or self.prepared)
                ):
                    self.player.stop()
                self.refresh_view()
                log("display hours closed — off surface")
            else:
                self.refresh_view()
                if self._seq_playing and not self.player.playing:
                    self._play_seq()   # resume the interrupted ambient item
                log("display hours open — resuming")
        if self._sched_off and not (self._run_active or self.prepared):
            return  # off-hours: the sequence machinery stays asleep

        if not self.sequence:
            # Empty playlist — local mode rotates the feature promos;
            # managed mode holds the static standby card (_view_config).
            if (
                self.window_open
                and not self.maintenance
                and not self.player.playing
                and self._promo_files
                and self.config.get("player_mode") != 1
                and time.perf_counter() >= self._slide_next
            ):
                self._promo_index = (
                    (self._promo_index + 1) % len(self._promo_files)
                )
                self.window.set_standby_image(
                    self._promo_files[self._promo_index]
                )
                self._slide_next = time.perf_counter() + PROMO_SECONDS
            return
        if self.prepared:
            return                      # conductor hold until "go"
        if not self.window_open or self.maintenance:
            return
        if self._seq_playing:
            # Video item ended — or never started. Playlist-internal
            # transitions (chained videos, RepeatAll boundaries) report
            # not-playing for a fraction of a second, so only sustained
            # silence counts as a real end; the next image is already
            # preloaded beneath, so this hold never shows a gap.
            if self.player.playing:
                self._seq_ended_since = None
                # Stall watchdog: a live player whose clock hasn't moved
                # in ~8s is hung (network stall, decoder wedge) — stop it
                # and let the silence path advance. Paused playback is
                # deliberate, not a stall.
                paused = xbmc.getCondVisibility("Player.Paused")
                t = None
                if not paused:
                    try:
                        t = self.player.getTime()
                    except RuntimeError:
                        pass
                now = time.perf_counter()
                if t is not None:
                    if (
                        self._last_play_time is not None
                        and abs(t - self._last_play_time) < 0.05
                    ):
                        self._stall_since = self._stall_since or now
                        if now - self._stall_since > 8.0:
                            log("video stalled >8s — skipping item")
                            self._stall_since = None
                            self._last_play_time = None
                            try:
                                self.player.stop()
                            except Exception:
                                pass
                    else:
                        self._stall_since = None
                    self._last_play_time = t
            else:
                now = time.perf_counter()
                if self._seq_ended_since is None:
                    self._seq_ended_since = now
                elif (
                    now - self._seq_ended_since > 2.0
                    and now - self._seq_started > 3.0
                ):
                    self._seq_ended_since = None
                    if self._pending_refresh:
                        self._apply_pending_refresh(
                            self.sequence[self._seq_pos]["name"]
                            if 0 <= self._seq_pos < len(self.sequence)
                            else None
                        )
                    else:
                        self._advance_seq()
            return
        cur = (
            self.sequence[self._seq_pos]
            if 0 <= self._seq_pos < len(self.sequence) else None
        )
        if (
            cur
            and cur["kind"] == "image"
            and not self.player.playing
            and time.perf_counter() >= self._slide_next
        ):
            if self._pending_refresh:
                self._apply_pending_refresh(cur["name"])
            else:
                self._advance_seq()

    def slide_skip(self, direction):
        """D-pad manual skip: right/down = next, left/up = previous —
        moves the whole sequence, not just images."""
        if not self.sequence or not self.window_open or self.maintenance:
            return
        if self._seq_playing and self.player.playing:
            self.player.stop()
            self._seq_playing = False
        self._advance_seq(direction)
        log(f"skip -> seq {self._seq_pos + 1}/{len(self.sequence)} (manual)")

    # -- input -------------------------------------------------------------
    # Everything here must work with a plain D-pad remote (Android TV etc.):
    #
    #   OK / Select / Play / Menu   open the actions menu
    #   D-pad left/right / up/down  skip through the playlist
    #   Back x2                     exit to the Kodi UI

    def _on_window_action(self, action):
        aid = action.getId()
        if aid in BACK_ACTIONS:
            self._handle_back()
        elif aid in MENU_ACTIONS:
            self._open_actions_menu()
        elif aid in (xbmcgui.ACTION_MOVE_RIGHT, xbmcgui.ACTION_MOVE_DOWN):
            self.slide_skip(1)
        elif aid in (xbmcgui.ACTION_MOVE_LEFT, xbmcgui.ACTION_MOVE_UP):
            self.slide_skip(-1)
        elif aid in (xbmcgui.ACTION_SELECT_ITEM,) or aid in PLAY_ACTIONS:
            self._open_actions_menu()
        # every other action is swallowed — StandbyWindow.onAction never calls
        # the base implementation, so navigation cannot close the window.

    def _open_actions_menu(self):
        actions = [
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

    # -- preview ------------------------------------------------------------

    def play_item(self, name):
        """Preview a single playlist entry (web admin Play button): videos
        play fullscreen once; images show on the standby screen for a minute."""
        state = getattr(self, "_media_state", None) or medialist.load_state()
        url = next((u["url"] for u in state["urls"] if u["name"] == name), None)
        folder = resolve_path(
            self.config.get("local_media_folder") or medialist.DEFAULT_MEDIA_DIR
        )
        path = url or (os.path.join(folder, name) if folder and name else "")
        if not path:
            return
        ext = os.path.splitext(path.split("?")[0])[1].lower()
        self.prepared = False
        self._previewing = True       # tick() resumes the sequence after
        self._seq_playing = False
        if ext in IMAGE_EXTS:
            if self.window_open:
                self.window.set_standby_image(path)
                self._slide_next = time.perf_counter() + 60.0
                log(f"preview image: {name}")
        else:
            self._slide_next = 0.0
            self.player.play(path)
            xbmc.executebuiltin("PlayerControl(RepeatOff)")
            log(f"preview video: {name}")

    def request_exit(self):
        """Foreign-thread exit path (web admin, conductor command).

        A direct window.close() from a non-GUI thread wedges on Kodi's
        GUI bridge lock and takes every later xbmcgui call with it — so
        post back-presses instead; the standby window's own double-back
        exit runs on the GUI thread. Three covers a fullscreen video —
        the first back just leaves the player."""
        if not (self.window_open or self.player.playing):
            return

        def _post():
            for _ in range(3):
                if self.maintenance:  # exit landed — don't back on Kodi UI
                    break
                xbmc.executebuiltin("Action(back)")
                time.sleep(0.6)

        threading.Thread(target=_post, daemon=True).start()

    def exit_to_kodi(self):
        # Optional kiosk lock — every exit path (double-back, the actions
        # menu's "Exit to Kodi UI") funnels here, so one password gate
        # covers them all. Kodi itself can't intercept Android TV's Home
        # button; OS-level lockdown needs Android app pinning.
        if self.config.get("local_lock") and self.config.get("local_password"):
            pw = xbmcgui.Dialog().input(
                "Screensemble is locked — enter the password to exit",
                "",
                xbmcgui.INPUT_ALPHANUM,
                xbmcgui.ALPHANUM_HIDE_INPUT,
            )
            if pw != self.config["local_password"]:
                if pw:  # empty = cancelled keyboard, stay quiet
                    notify("Incorrect password")
                log("exit blocked by screen lock")
                return
        self.maintenance = True
        self._maint_since = time.perf_counter()
        self._maint_ui_dismissed = False
        self.close_standby()  # run_standby()'s doModal() returns, window drops
        log("exited to Kodi UI — resume via Programs > Screensemble")
        notify("Standby exited. Resume from Programs > Screensemble")

    def resume_from_menu(self, with_content=True):
        self.maintenance = False
        self._maint_since = 0.0
        self._maint_ui_dismissed = False
        self._offline_since = None   # managed link down since (degraded admin)
        self.content_pending = False
        self.rescan_media()
        if with_content:
            self.request_content()

    def refresh_view(self):
        """Re-apply current view config to the resident standby window (after a
        settings change). Does not interrupt playback."""
        if self.window_open and not self.player.playing:
            self.window.config = self._view_config()
            self.window.apply()


def pair_prompt(controller, monitor):
    """Runs on its own thread (dialog blocks until answered): the web /pair
    endpoint parked a server offer — the human confirms or cancels on the
    TV remote itself. OK applies server mode; Cancel drops the offer."""
    url = HOME.getProperty(PROP_PAIR_URL)
    try:
        info = json.loads(HOME.getProperty(PROP_PAIR_INFO) or "{}")
    except ValueError:
        info = {}
    # WHO is asking — server's /api/info if it answered, else just the URL.
    lines = ["Allow this server to manage the display?", ""]
    if info.get("name"):
        ver = info.get("version")
        lines.append(str(info["name"]) + (f"  (v{ver})" if ver else ""))
    if info.get("owner"):
        lines.append(f"Owner: {info['owner']}")
    if info.get("info"):
        lines.append(str(info["info"]))
    lines.append(url)
    try:
        ok = xbmcgui.Dialog().yesno(
            "Screensemble pairing",
            "\n".join(lines),
            nolabel="Cancel",
            yeslabel="Pair",
        )
    except Exception as e:
        log(f"pair prompt failed: {e}")
        ok = False
    pairing_id = HOME.getProperty(PROP_PAIR_ID)
    HOME.clearProperty(PROP_PAIR_URL)
    HOME.clearProperty(PROP_PAIR_INFO)
    HOME.clearProperty(PROP_PAIR_ID)
    resp, err = None, None
    if ok:
        # Tell the server the human confirmed — it registers us and issues
        # this display's device token. No token, no managed mode.
        resp, err = post_json(url, "/api/pair/accept", {"pairing_id": pairing_id})
        if err or not (resp or {}).get("ok") or not (resp or {}).get("token"):
            log(f"pair accept failed: {err or resp}")
            notify("Pairing failed — start again from the server's Pair page")
            ok = None
    if ok:
        addon = xbmcaddon.Addon()
        addon.setSetting("server_address", url)
        addon.setSetting("server_auth_token", str(resp["token"]))
        addon.setSetting("paired_id", str(resp.get("player_id") or ""))
        addon.setSettingInt("player_mode", 1)
        addon.setSettingBool("setup_complete", True)
        monitor.dirty = True
        log(f"paired to server {url}")
        notify(f"Paired — managed by {url}")
    elif ok is False:
        if pairing_id:
            post_json(url, "/api/pair/decline", {"pairing_id": pairing_id})
        log("pairing declined on TV")
        notify("Pairing declined")
    controller._pair_dialog = False


def poll_loop(monitor, controller):
    """Background thread: commands, live settings, setup completion, pending
    content, and the slideshow timer. The main thread is blocked inside the
    standby window's doModal() loop."""
    player = controller.player
    was_fullscreen = False
    while not monitor.abortRequested():
        # Leaving fullscreen video while it still plays in the background
        # (what Back does in stock Kodi) means "stop" for signage — this
        # gives Back=Stop behavior without a user keymap. Armed videos are
        # the exception: they stage windowed+paused behind the standby
        # window, so a stray fullscreen activation while armed gets pushed
        # back instead — a paused video in fullscreen would let the
        # seekbar OSD leak through.
        fs = xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)")
        paused = xbmc.getCondVisibility("Player.Paused")
        if controller.prepared and fs and paused:
            xbmc.executebuiltin("Action(back)")
        elif (
            was_fullscreen and not fs and player.isPlaying()
            and not controller.prepared
        ):
            # While armed the staged video is *meant* to sit non-fullscreen
            # and paused — pushing it back out of a stray fullscreen must
            # not then count as Back=Stop and destroy the stage.
            player.stop()
            log("left fullscreen video with playback running — stopped")
        was_fullscreen = fs

        # Command channel from the script entry point (Programs > Screensemble).
        cmd = HOME.getProperty(COMMAND_PROPERTY)
        if cmd:
            HOME.clearProperty(COMMAND_PROPERTY)
            if cmd == COMMAND_RESUME:
                controller.resume_from_menu()
            elif cmd == COMMAND_RELOAD:
                monitor.dirty = True
            elif cmd == "stop":
                if player.isPlaying():
                    player.stop()
            elif cmd == "update":
                controller.defer_content_refresh()
            elif cmd == "restart":
                controller.rescan_media()
                controller.request_content()
            elif cmd.startswith("play:"):
                controller.play_item(cmd[5:])

        # Left in the Kodi UI too long — auto-resume so the display never
        # sits unattended on a launcher screen (0 = stay until resumed).
        timeout = controller.config.get("maintenance_timeout", 0)
        if (controller.maintenance and timeout and controller._maint_since
                and time.perf_counter() - controller._maint_since > timeout * 60):
            log("maintenance timeout — resuming presentation")
            controller.resume_from_menu()

        # Pairing offer from the web /pair endpoint — confirmed on the TV.
        if HOME.getProperty(PROP_PAIR_URL) and not controller._pair_dialog:
            controller._pair_dialog = True
            threading.Thread(
                target=pair_prompt, args=(controller, monitor), daemon=True
            ).start()

        # Live settings changes (folder changed, slideshow interval, colors).
        if monitor.dirty:
            monitor.dirty = False
            was_setup = controller.config["setup_complete"]
            config = load_config()
            ensure_runtime_assets()
            remove_legacy_keymap()
            controller.config = config
            controller.defer_content_refresh()
            controller.refresh_view()
            # The wizard's setSetting arrives through this path too — detect
            # the False->True flip here or content never gets queued.
            if config["setup_complete"] and not was_setup:
                controller.request_content()
                log("setup complete — content queued")
            else:
                log("settings reloaded")
            # Pairing flips the mode at runtime — bring the server link up
            # without waiting for a restart.
            if config["player_mode"] == 1 and not getattr(
                controller, "_link", None
            ):
                from link import ServerLink

                controller._link = ServerLink(controller)
                controller._link.start()
                log("managed mode — server link started")

        if not controller.config["setup_complete"]:
            # Poll for wizard completion; then bring the standby presentation up.
            # Fresh Addon() — the cached instance misses script-instance writes.
            if xbmcaddon.Addon().getSettingBool("setup_complete"):
                config = load_config()
                ensure_runtime_assets()
                controller.config = config
                controller.rescan_media()
                controller.request_content()
                log("setup complete — content queued")
        elif controller.content_pending and not controller.prepared and (
            controller.window_open or time.perf_counter() >= controller.content_due
        ):
            # Content starts only once the standby window is resident (or the
            # deadline forces it) so video always opens over the standby screen.
            controller.start_content()

        controller.tick()

        if monitor.waitForAbort(0.5):
            break


def main():
    config = load_config()
    log(
        "service starting "
        f"(device={config['device_name']!r} mode={config['player_mode']} "
        f"server={config['server_address']!r} setup={config['setup_complete']})"
    )
    ensure_runtime_assets()
    remove_legacy_keymap()

    # Unattended signage: never let Kodi's own screensaver/dim take over
    # the display. Released again in the shutdown path.
    xbmc.executebuiltin("InhibitScreensaver(true)")

    monitor = SettingsMonitor()
    controller = StandbyController(
        config,
        SignagePlayer(
            lambda: controller.return_to_standby(),
            lambda: controller.on_av_started(),
        ),
    )

    if config["setup_complete"]:
        # Content waits until the splash has had its moment.
        controller.request_content(delay=config["splash_seconds"] + 2.0)
    else:
        log("setup not complete — staying hidden until the wizard runs")

    worker = threading.Thread(target=poll_loop, args=(monitor, controller), daemon=True)
    worker.start()

    # Web admin in every mode — settings, uploads, transport control for
    # whoever's on the LAN; managed mode proxies the same UI via the
    # conductor too.
    web = None
    if config["web_admin"]:
        try:
            web = AdminServer(
                controller,
                xbmcaddon.Addon(),
                on_settings_saved=lambda: setattr(monitor, "dirty", True),
                port=config["web_port"],
            )
            web.start()
        except OSError as e:
            web = None
            log(f"web admin failed to start: {e}")

    # Managed mode: the display dials the server and holds a persistent
    # WebSocket — status goes up, commands come down. Works across the
    # internet; no inbound listener needed on the display's network.
    link = None
    if config["player_mode"] == 1:
        from link import ServerLink

        link = controller._link = ServerLink(controller)
        link.start()
        log("managed mode — server link started")

    # Boot splash: brand + admin/server info before content begins.
    if config["setup_complete"] and config["splash_seconds"] > 0:
        try:
            splash = SplashWindow("splash.xml", ADDON_PATH, "Default", "1080i")
            splash.config = config
            splash.web_enabled = web is not None
            splash.show_for(config["splash_seconds"])
        except Exception as e:
            log(f"splash failed: {e}")

    # Main thread: keep the standby window modally resident. doModal() blocks
    # until the window is closed (exit_to_kodi or shutdown), then we either
    # wait hidden (maintenance) or reopen.
    while not monitor.abortRequested():
        if (
            controller.config["setup_complete"]
            and not controller.maintenance
            and not controller.window_open
        ):
            # Same settle as the maintenance branch — give the previous
            # modal's teardown a GUI pump before doModal-ing the next one.
            monitor.waitForAbort(0.5)
            try:
                controller.run_standby(monitor.abortRequested)
            except Exception as e:
                controller.window_open = False
                controller.window = None
                log(f"standby window failed: {e} — retrying")
                monitor.waitForAbort(1.0)
        elif (
            controller.maintenance
            and controller.config.get("maintenance_ui")
            and not controller._maint_ui_dismissed
        ):
            # Branded pause screen — Resume / Settings / Continue-to-Kodi.
            # doModal returns on any of those, on an external resume (the
            # window's ticker notices maintenance clear), or on abort.
            # Settle briefly: the standby modal's teardown is still in
            # flight when doModal returns — open the next modal too soon
            # and Kodi may render it without ever making it the active
            # modal (input falls through to the home screen).
            monitor.waitForAbort(0.5)
            try:
                maint = MaintenanceWindow(
                    "maintenance.xml", ADDON_PATH, "Default", "1080i"
                )
                maint.controller = controller
                maint._alive = True  # before the watcher reads it
                threading.Thread(
                    target=controller._watch_abort,
                    args=(monitor.abortRequested, lambda: maint._alive),
                    daemon=True,
                ).start()
                maint.open()
            except Exception as e:
                controller._maint_ui_dismissed = True
                log(f"maintenance window failed: {e}")
        elif monitor.waitForAbort(0.5):
            break

    log("service stopping")
    if web is not None:
        web.stop()
    if link is not None:
        link.stop()
    controller.close_standby()
    xbmc.executebuiltin("InhibitScreensaver(false)")


if __name__ == "__main__":
    main()
