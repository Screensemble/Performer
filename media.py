"""Media & playlist — custom management window.

A branded WindowXML surface (same visual language as the wizard) that
replaces the Kodi-native plugin listing for day-to-day playlist work.
Three views share one list control:

  main      — control rows (add file/URL, shuffle, restart) + playlist
              items with enable/schedule/playing state
  options   — per-item actions (play, enable, duration, schedule,
              move, delete) shown as rows on the same list
  schedule  — schedule rule rows for the selected item

Managed (server-paired) mode collapses everything to a status line plus
the local recovery commands — playlist editing lives on the server then.
"""

import os

import xbmc
import xbmcaddon
import xbmcgui

import medialist
from standby import ADDON_PATH, ensure_runtime_assets
from menu import (
    DAYS,
    DEFAULT_SLIDE_SECONDS,
    _add_file,
    _add_url,
    _collect,
    _command,
    _day_summary,
    _managed,
    _move,
    _parse_hhmm,
    _schedule_summary,
    _server_label,
)

ADDON = xbmcaddon.Addon()
HOME = xbmcgui.Window(10000)

CONTROL_CARD = 410
CONTROL_KICKER = 401
CONTROL_STATUS = 402
CONTROL_TITLE = 403
CONTROL_LIST = 9000

BACK_ACTIONS = {
    xbmcgui.ACTION_PARENT_DIR,
    xbmcgui.ACTION_PREVIOUS_MENU,
    xbmcgui.ACTION_NAV_BACK,
}


def log(msg):
    xbmc.log(f"[screensemble] media: {msg}", xbmc.LOGINFO)


def _now_playing():
    """Service-published name of the item currently on screen."""
    return HOME.getProperty("screensemble.now") or ""


def _meta(it, state):
    bits = [f"{'remote' if it['remote'] else 'local'} {it['kind']}"]
    if it["kind"] == "image":
        dur = state["durations"].get(it["name"])
        bits.append(f"{dur:g}s" if dur else f"{DEFAULT_SLIDE_SECONDS}s")
    sched = state["schedules"].get(it["name"])
    if sched:
        bits.append(f"schedule: {_schedule_summary(sched)}")
    bits.append(it["src"])
    return " · ".join(bits)


class MediaWindow(xbmcgui.WindowXML):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._view = "main"      # main | options | schedule
        self._rows = []          # (action, payload) per list position
        self._item = None        # selected entry in options/schedule
        self._sched = {}         # working copy while editing schedule

    # -- views -------------------------------------------------------------

    def onInit(self):
        self._render()

    def _render(self):
        lst = self.getControl(CONTROL_LIST)
        lst.reset()
        self._rows = []
        if _managed():
            self._render_managed(lst)
        elif self._view == "options" and self._item:
            self._render_options(lst)
        elif self._view == "schedule" and self._item:
            self._render_schedule(lst)
        else:
            self._render_main(lst)
        self.setFocus(lst)

    def _row(self, lst, label, action, payload=None, label2="", icon=""):
        li = xbmcgui.ListItem(label=label)
        if label2:
            li.setLabel2(label2)
        if icon:
            li.setArt({"icon": icon, "thumb": icon})
        lst.addItem(li)
        self._rows.append((action, payload))

    def _render_managed(self, lst):
        self.getControl(CONTROL_KICKER).setLabel("MANAGED MODE")
        self.getControl(CONTROL_TITLE).setLabel(_server_label())
        self.getControl(CONTROL_STATUS).setText(
            "Playlist editing is done on the Screensemble server — this "
            "display only offers local recovery commands while managed."
        )
        self._row(lst, "Restart content", "cmd", "restart",
                  "rescan the media folder and restart playback")
        self._row(lst, "Resume standby", "cmd", "resume",
                  "return to the standby surface")
        self._row(lst, "Close", "close")

    def _render_main(self, lst):
        state = medialist.load_state()
        entries = _collect(state)
        playing = _now_playing()
        self.getControl(CONTROL_KICKER).setLabel("LOCAL MODE")
        self.getControl(CONTROL_TITLE).setLabel("Media & playlist")
        status = f"{len(entries)} item{'s' if len(entries) != 1 else ''}"
        status += f" · playing {playing}" if playing else " · standby"
        self.getControl(CONTROL_STATUS).setText(status)

        self._row(lst, "Add media file…", "addfile",
                  label2="browse this device or the network, copy into the "
                         "media folder")
        self._row(lst, "Add URL…", "addurl",
                  label2="stream or remote image")
        self._row(lst, f"Shuffle: {'on' if state['shuffle'] else 'off'}",
                  "shuffle",
                  label2="randomize playlist order")
        self._row(lst, "Restart content", "cmd", "restart",
                  label2="rescan and restart playback")

        if not entries:
            li = xbmcgui.ListItem(label="(media folder is empty)")
            lst.addItem(li)
            self._rows.append(("none", None))
            return
        for it in entries:
            name = it["name"]
            disabled = name in state["disabled"]
            sched = state["schedules"].get(name)
            off = bool(sched) and not medialist.schedule_active(sched)
            label = f"▶ {name}" if playing == name else name
            if disabled:
                label = f"[COLOR FF6E8099]{label}[/COLOR]  (disabled)"
            elif off:
                label = f"[COLOR FF6E8099]{label}[/COLOR]  (scheduled off)"
            icon = ""
            if it["kind"] == "image":
                icon = it["src"]
            elif it["remote"]:
                icon = "DefaultAddonWebDav.png"
            else:
                icon = "DefaultVideo.png"
            self._row(lst, label, "item", it, label2=_meta(it, state),
                      icon=icon)

    def _render_options(self, lst):
        it = self._item
        name = it["name"]
        state = medialist.load_state()
        enabled = name not in state["disabled"]
        dur = state["durations"].get(name)
        sched = state["schedules"].get(name) or {}
        self.getControl(CONTROL_KICKER).setLabel("PLAYLIST ITEM")
        self.getControl(CONTROL_TITLE).setLabel(name)
        self.getControl(CONTROL_STATUS).setText(_meta(it, state))

        self._row(lst, "Play now", "play", name,
                  label2="preview this item fullscreen")
        self._row(lst, "Disable" if enabled else "Enable", "toggle", name,
                  label2="keep in list but skip during playback"
                  if enabled else "return this item to the playlist")
        if it["kind"] == "image":
            self._row(
                lst,
                f"Image duration…  ({dur:g}s)" if dur
                else f"Image duration…  (default {DEFAULT_SLIDE_SECONDS}s)",
                "duration", name,
                label2="seconds this image stays on screen")
        self._row(
            lst,
            ("Schedule…  (" + _schedule_summary(sched) + ")") if sched
            else "Schedule…",
            "schedule", name,
            label2="limit when this item may play")
        self._row(lst, "Move up", "move", -1)
        self._row(lst, "Move down", "move", +1)
        self._row(
            lst,
            "Remove URL entry" if it["remote"] else "Delete file",
            "remove", name,
            label2="this cannot be undone")
        self._row(lst, "Back to playlist", "back")

    def _render_schedule(self, lst):
        sched = self._sched
        self.getControl(CONTROL_KICKER).setLabel("SCHEDULE")
        self.getControl(CONTROL_TITLE).setLabel(self._item["name"])
        self.getControl(CONTROL_STATUS).setText(
            "Limits when this item may appear — an item with no rules "
            "plays any time."
        )
        self._row(
            lst,
            f"Time window…  ({sched.get('from') or '…'} – "
            f"{sched.get('to') or '…'})",
            "sched_time",
            label2="HH:MM-HH:MM, overnight ok; empty = any time")
        self._row(
            lst,
            f"Days of week…  ({_day_summary(sched.get('days'))})",
            "sched_days",
            label2="empty = every day")
        self._row(
            lst,
            f"Date range…  ({sched.get('start') or '…'} → "
            f"{sched.get('end') or '…'})",
            "sched_dates",
            label2="YYYY-MM-DD..YYYY-MM-DD; empty = no limit")
        self._row(lst, "Clear all schedule rules", "sched_clear")
        self._row(lst, "Done", "sched_done",
                  label2="save and return to the item")

    # -- input ---------------------------------------------------------------

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self._back()

    def onClick(self, control_id):
        if control_id != CONTROL_LIST:
            return
        try:
            pos = self.getControl(CONTROL_LIST).getSelectedPosition()
        except Exception:
            pos = self.getCurrentListPosition()
        if 0 <= pos < len(self._rows):
            self._activate(*self._rows[pos])

    def _back(self):
        if self._view == "schedule":
            self._save_sched()
            self._view = "options"
            self._render()
        elif self._view == "options":
            self._view = "main"
            self._item = None
            self._render()
        else:
            self.close()

    def _save_sched(self):
        state = medialist.load_state()
        if self._sched:
            state["schedules"][self._item["name"]] = self._sched
        else:
            state["schedules"].pop(self._item["name"], None)
        medialist.save_state(state)
        _command("update")

    def _input(self, heading, default=""):
        return xbmcgui.Dialog().input(
            heading, default, xbmcgui.INPUT_ALPHANUM
        )

    def _activate(self, action, payload):
        if action == "none":
            return
        if action in ("close", "back"):
            self._back() if action == "back" else self.close()
            return
        if action == "cmd":
            _command(payload)
            if payload == "resume":
                self.close()
                return
        elif action == "addfile":
            _add_file()
        elif action == "addurl":
            _add_url()
        elif action == "shuffle":
            state = medialist.load_state()
            state["shuffle"] = not state["shuffle"]
            medialist.save_state(state)
            _command("update")
        elif action == "item":
            self._item = payload
            self._view = "options"
        elif action == "play":
            _command(f"play:{payload}")
            self.close()
            return
        elif action == "toggle":
            self._toggle(payload)
        elif action == "duration":
            self._set_duration(payload)
        elif action == "schedule":
            state = medialist.load_state()
            self._sched = dict(
                state["schedules"].get(self._item["name"]) or {}
            )
            self._view = "schedule"
        elif action == "move":
            self._move_item(payload)
        elif action == "remove":
            if self._remove_item(payload):
                self._view = "main"
                self._item = None
        elif action == "sched_time":
            self._sched_time()
        elif action == "sched_days":
            self._sched_days()
        elif action == "sched_dates":
            self._sched_dates()
        elif action == "sched_clear":
            self._sched.clear()
        elif action == "sched_done":
            self._save_sched()
            self._view = "options"
        self._render()

    # -- item actions ---------------------------------------------------------

    def _toggle(self, name):
        state = medialist.load_state()
        if name in state["disabled"]:
            state["disabled"] = [d for d in state["disabled"] if d != name]
        else:
            state["disabled"].append(name)
        medialist.save_state(state)
        _command("update")

    def _set_duration(self, name):
        state = medialist.load_state()
        dur = state["durations"].get(name)
        v = self._input(
            f"Seconds to show this image (0 = default "
            f"{DEFAULT_SLIDE_SECONDS})",
            str(dur or ""),
        )
        if v is None:
            return
        try:
            secs = float(v)
        except ValueError:
            return
        if secs > 0:
            state["durations"][name] = secs
        else:
            state["durations"].pop(name, None)
        medialist.save_state(state)
        _command("update")

    def _move_item(self, delta):
        state = medialist.load_state()
        names = [e["name"] for e in _collect(state)]
        if self._item["name"] in names:
            _move(state, names, self._item["name"], delta)
            medialist.save_state(state)
            _command("update")

    def _remove_item(self, name):
        dlg = xbmcgui.Dialog()
        state = medialist.load_state()
        it = self._item
        if it["remote"]:
            if not dlg.yesno(
                "Screensemble", f"Remove URL entry '{name}'?"
            ):
                return False
            state["urls"] = [
                u for u in state["urls"] if u["name"] != name
            ]
        else:
            if not dlg.yesno(
                "Screensemble", f"Delete '{name}' from the media folder?"
            ):
                return False
            try:
                os.remove(it["src"])
            except OSError as e:
                dlg.notification(
                    "Screensemble", f"Delete failed: {e}",
                    xbmcgui.NOTIFICATION_ERROR,
                )
                return False
        for key in ("order", "disabled"):
            state[key] = [n for n in state[key] if n != name]
        for key in ("durations", "schedules"):
            state[key].pop(name, None)
        medialist.save_state(state)
        _command("update")
        return True

    # -- schedule editors -------------------------------------------------------

    def _sched_time(self):
        sched = self._sched
        v = self._input(
            "Time window (HH:MM-HH:MM, overnight ok; empty = any time)",
            f"{sched.get('from', '')}-{sched.get('to', '')}"
            if sched.get("from") or sched.get("to") else "",
        )
        if v is None:
            return
        v = v.strip()
        if not v:
            sched.pop("from", None)
            sched.pop("to", None)
            return
        parts = v.split("-")
        if len(parts) == 2 and all(
            _parse_hhmm(p) is not None for p in parts
        ):
            sched["from"], sched["to"] = parts[0].strip(), parts[1].strip()
        else:
            xbmcgui.Dialog().notification(
                "Screensemble", "Use HH:MM-HH:MM (e.g. 08:00-18:00)",
                xbmcgui.NOTIFICATION_WARNING,
            )

    def _sched_days(self):
        sched = self._sched
        sel = xbmcgui.Dialog().multiselect(
            "Days this item may play (empty = every day)",
            list(DAYS),
            preselect=[int(d) for d in sched.get("days", [])],
        )
        if sel is None:
            return
        if sel:
            sched["days"] = sorted(sel)
        else:
            sched.pop("days", None)

    def _sched_dates(self):
        sched = self._sched
        v = self._input(
            "Date range (YYYY-MM-DD..YYYY-MM-DD; empty = no limit)",
            f"{sched.get('start', '')}..{sched.get('end', '')}"
            if sched.get("start") or sched.get("end") else "",
        )
        if v is None:
            return
        v = v.strip()
        if not v:
            sched.pop("start", None)
            sched.pop("end", None)
            return
        parts = v.split("..")
        ok = len(parts) == 2
        if ok:
            for i, p in enumerate(parts):
                p = p.strip()
                try:
                    if p:
                        import datetime
                        datetime.date.fromisoformat(p)
                except ValueError:
                    ok = False
                parts[i] = p
        if ok:
            sched["start"], sched["end"] = parts[0], parts[1]
        else:
            xbmcgui.Dialog().notification(
                "Screensemble", "Use YYYY-MM-DD..YYYY-MM-DD",
                xbmcgui.NOTIFICATION_WARNING,
            )


def run_media_ui():
    ensure_runtime_assets()
    win = MediaWindow("media.xml", ADDON_PATH, "Default", "1080i")
    win.doModal()
    del win
