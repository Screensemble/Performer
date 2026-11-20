"""Plugin entry — the local playlist as a native Kodi media list.

Declared as xbmc.python.pluginsource so the playlist renders in Kodi's
standard media view (same screen type as a movie library list): items
show enable/schedule/playing state, and selecting one opens an options
dialog with the same per-item controls the web admin has. Control rows
at the top cover playlist-wide actions. State lives in
medialist/media_state.json — edits here and in the web admin stay in
sync, and every mutation pings the service to rescan + restart content.
"""

import os
import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

import medialist
from idle import resolve_path
from medialist import DEFAULT_MEDIA_DIR, IMAGE_EXTS, VIDEO_EXTS

ADDON = xbmcaddon.Addon()
# media.py imports this module's helpers outside plugin context, where
# argv isn't a directory handle — guard so the import is safe.
HANDLE = (
    int(sys.argv[1])
    if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit()
    else -1
)
BASE = sys.argv[0]
COMMAND_PROPERTY = "screensemble.command"
DEFAULT_SLIDE_SECONDS = 10
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday")


def log(msg):
    xbmc.log(f"[screensemble] {msg}", xbmc.LOGINFO)


def _url(**kw):
    return f"{BASE}?{urllib.parse.urlencode(kw)}"


def _command(cmd):
    """Signal the running service the same way settings buttons do."""
    xbmcgui.Window(10000).setProperty(COMMAND_PROPERTY, cmd)


def _managed():
    """Managed/paired mode — the server owns the playlist, so local
    editing surfaces collapse to status + needed local commands."""
    return ADDON.getSettingInt("player_mode") == 1


def _server_label():
    addr = ADDON.getSetting("server_address") or "server"
    link = (xbmcgui.Window(10000).getProperty("screensemble.link")
            or "offline")
    return f"Managed by {addr} — {link}"





def _apply(state):
    """Persist playlist state and ping the service to rescan + restart
    content. Callers re-render afterwards via list_root(update=True)."""
    medialist.save_state(state)
    _command("restart")


def _collect(state):
    """Merged playlist entries (files + URL items) in play order."""
    folder = resolve_path(
        ADDON.getSetting("local_media_folder") or DEFAULT_MEDIA_DIR
    )
    entries = {}
    if folder and "://" not in folder:
        try:
            for n in os.listdir(folder):
                full = os.path.join(folder, n)
                if not os.path.isfile(full):
                    continue
                ext = os.path.splitext(n)[1].lower()
                if ext in VIDEO_EXTS or ext in IMAGE_EXTS:
                    entries[n] = {
                        "name": n,
                        "kind": "video" if ext in VIDEO_EXTS else "image",
                        "src": full,
                        "remote": False,
                    }
        except OSError as e:
            log(f"media folder not listable ({folder}): {e}")
    for u in state["urls"]:
        ext = os.path.splitext(u["url"].split("?")[0])[1].lower()
        entries[u["name"]] = {
            "name": u["name"],
            "kind": "image" if ext in IMAGE_EXTS else "video",
            "src": u["url"],
            "remote": True,
        }
    return [entries[n] for n in medialist.sort_names(list(entries), state)]


def _schedule_summary(sched):
    if not sched:
        return ""
    bits = []
    if sched.get("days"):
        bits.append("/".join(DAYS[int(d)][:3] for d in sched["days"]))
    if sched.get("from") or sched.get("to"):
        bits.append(f"{sched.get('from', '…')}-{sched.get('to', '…')}")
    if sched.get("start") or sched.get("end"):
        bits.append(f"{sched.get('start', '…')}→{sched.get('end', '…')}")
    return " ".join(bits)


def _control_row(label, **params):
    li = xbmcgui.ListItem(label=f"[B]{label}[/B]")
    li.setArt({"icon": "DefaultAddonService.png"})
    return li, _url(**params)


def list_root(update=False):
    """Root menu — player-level actions. The playlist itself lives one
    level down under Media."""
    xbmcplugin.setContent(HANDLE, "files")
    xbmcplugin.setPluginCategory(HANDLE, "Screensemble")
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)

    rows = []
    if _managed():
        # Server owns the playlist — show a status row (click refreshes
        # the link state) and only the needed local commands.
        status = xbmcgui.ListItem(
            label=f"[COLOR FF6E8099]{_server_label()}[/COLOR]"
        )
        status.setArt({"icon": "DefaultAddonService.png"})
        rows.append((status, _url(action="refresh")))
    else:
        n = len(_collect(medialist.load_state()))
        media = xbmcgui.ListItem(
            label=f"[B]Media & playlist[/B]  ({n} item{'s' if n != 1 else ''})"
        )
        media.setArt({"icon": "DefaultFolder.png"})
        rows.append((media, _url(action="mediawin")))
    rows += [
        _control_row("Restart content", action="cmd", cmd="restart"),
        _control_row(
            "Resume idle presentation", action="cmd", cmd="resume",
            back="root",
        ),
        _control_row("Addon settings", action="settings"),
    ]
    for li, url in rows:
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE, updateListing=update)


def _input(heading, default="", hidden=False):
    dlg = xbmcgui.Dialog()
    t = (xbmcgui.INPUT_ALPHANUM if not hidden
         else xbmcgui.INPUT_ALPHANUM | xbmcgui.INPUT_PASSWORD)
    return dlg.input(heading, default, t)


def _move(state, names, name, delta):
    order = list(names)
    i = order.index(name)
    j = i + delta
    if not (0 <= j < len(order)):
        return
    order[i], order[j] = order[j], order[i]
    state["order"] = order


def _schedule_menu(name, sched):
    dlg = xbmcgui.Dialog()
    while True:
        opts = [
            f"Time window…  ({sched.get('from') or '…'} – "
            f"{sched.get('to') or '…'})",
            f"Days of week…  ({_day_summary(sched.get('days'))})",
            f"Date range…  ({sched.get('start') or '…'} → "
            f"{sched.get('end') or '…'})",
            "Clear all schedule rules",
            "Done",
        ]
        c = dlg.select(f"Schedule — {name}", opts)
        if c in (-1, 4):
            return sched
        if c == 0:
            v = _input(
                "Time window (HH:MM-HH:MM, overnight ok; empty = any time)",
                f"{sched.get('from', '')}-{sched.get('to', '')}"
                if sched.get("from") or sched.get("to") else "",
            )
            if v is None:
                continue
            v = v.strip()
            if not v:
                sched.pop("from", None)
                sched.pop("to", None)
            else:
                parts = v.split("-")
                if len(parts) == 2 and all(
                    _parse_hhmm(p) is not None for p in parts
                ):
                    sched["from"], sched["to"] = parts[0].strip(), parts[1].strip()
                else:
                    dlg.notification(
                        "Screensemble", "Use HH:MM-HH:MM (e.g. 08:00-18:00)",
                        xbmcgui.NOTIFICATION_WARNING,
                    )
        elif c == 1:
            sel = dlg.multiselect(
                "Days this item may play (empty = every day)",
                list(DAYS),
                preselect=[int(d) for d in sched.get("days", [])],
            )
            if sel is None:
                continue
            if sel:
                sched["days"] = sorted(sel)
            else:
                sched.pop("days", None)
        elif c == 2:
            v = _input(
                "Date range (YYYY-MM-DD..YYYY-MM-DD; empty = no limit)",
                f"{sched.get('start', '')}..{sched.get('end', '')}"
                if sched.get("start") or sched.get("end") else "",
            )
            if v is None:
                continue
            v = v.strip()
            if not v:
                sched.pop("start", None)
                sched.pop("end", None)
            else:
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
                    dlg.notification(
                        "Screensemble",
                        "Use YYYY-MM-DD..YYYY-MM-DD",
                        xbmcgui.NOTIFICATION_WARNING,
                    )
        elif c == 3:
            sched.clear()
            return sched


def _day_summary(days):
    if not days:
        return "every day"
    return ", ".join(DAYS[int(d)][:3] for d in days)


def _parse_hhmm(v):
    try:
        h, m = v.split(":")
        h, m = int(h), int(m)
        return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None
    except (ValueError, TypeError):
        return None


def item_menu(name):
    dlg = xbmcgui.Dialog()
    state = medialist.load_state()
    entries = _collect(state)
    it = next((e for e in entries if e["name"] == name), None)
    if not it:
        return
    names = [e["name"] for e in entries]
    enabled = name not in state["disabled"]
    sched = dict(state["schedules"].get(name) or {})
    dur = state["durations"].get(name)

    opts = [("Play now", "play")]
    opts.append(("Disable" if enabled else "Enable", "toggle"))
    if it["kind"] == "image":
        opts.append((
            f"Image duration…  ({dur:g}s)" if dur
            else f"Image duration…  (default {DEFAULT_SLIDE_SECONDS}s)",
            "duration",
        ))
    label = ("Schedule…  (" + _schedule_summary(sched) + ")") if sched \
        else "Schedule…"
    opts.append((label, "schedule"))
    opts += [("Move up", "up"), ("Move down", "down")]
    opts.append((
        "Remove URL entry" if it["remote"] else "Delete file", "remove",
    ))

    c = dlg.select(name, [o[0] for o in opts])
    if c < 0:
        return
    action = opts[c][1]

    if action == "play":
        _command(f"play:{name}")
        return
    if action == "toggle":
        if enabled:
            state["disabled"].append(name)
        else:
            state["disabled"] = [d for d in state["disabled"] if d != name]
    elif action == "duration":
        v = _input("Seconds to show this image (0 = default 20)",
                   str(dur or ""))
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
    elif action == "schedule":
        sched = _schedule_menu(name, sched)
        if sched:
            state["schedules"][name] = sched
        else:
            state["schedules"].pop(name, None)
    elif action == "up":
        _move(state, names, name, -1)
    elif action == "down":
        _move(state, names, name, +1)
    elif action == "remove":
        if it["remote"]:
            if not dlg.yesno("Screensemble", f"Remove URL entry '{name}'?"):
                return
            state["urls"] = [u for u in state["urls"] if u["name"] != name]
        else:
            if not dlg.yesno(
                "Screensemble", f"Delete '{name}' from the media folder?"
            ):
                return
            try:
                os.remove(it["src"])
            except OSError as e:
                dlg.notification(
                    "Screensemble", f"Delete failed: {e}",
                    xbmcgui.NOTIFICATION_ERROR,
                )
                return
        for key in ("order", "disabled"):
            state[key] = [n for n in state[key] if n != name]
        for key in ("durations", "schedules"):
            state[key].pop(name, None)
    _apply(state)


def _add_file():
    """Pick a media file anywhere Kodi can reach (local, USB, SMB…) and
    copy it into the built-in media folder so it joins the playlist."""
    dlg = xbmcgui.Dialog()
    mask = "|".join(sorted(VIDEO_EXTS | IMAGE_EXTS))
    path = dlg.browse(1, "Choose media to add", "files", mask)
    if not path:
        return
    base = os.path.basename(urllib.parse.unquote(path).rstrip("/\\"))
    if not base:
        return
    dest = os.path.join(DEFAULT_MEDIA_DIR, base)
    if resolve_path(path) == dest:
        dlg.notification("Screensemble", f"{base} is already in the folder",
                         xbmcgui.NOTIFICATION_INFO)
        return
    root, ext = os.path.splitext(base)
    i = 2
    while os.path.exists(dest):
        dest = os.path.join(DEFAULT_MEDIA_DIR, f"{root} ({i}){ext}")
        i += 1
    if not xbmcvfs.copy(path, dest):
        dlg.notification("Screensemble", "Copy failed",
                         xbmcgui.NOTIFICATION_ERROR)
        return
    dlg.notification("Screensemble",
                     f"Added {os.path.basename(dest)}",
                     xbmcgui.NOTIFICATION_INFO)
    _apply(medialist.load_state())


def _add_url():
    dlg = xbmcgui.Dialog()
    name = _input("Name for this URL item")
    if not name:
        return
    url = _input("Stream or image URL")
    if not url or "://" not in url:
        if url:
            dlg.notification(
                "Screensemble", "That doesn't look like a URL",
                xbmcgui.NOTIFICATION_WARNING,
            )
        return
    state = medialist.load_state()
    state["urls"] = [u for u in state["urls"] if u["name"] != name]
    state["urls"].append({"name": name, "url": url})
    _apply(state)


def main():
    query = sys.argv[2][1:] if len(sys.argv) > 2 else ""
    params = dict(urllib.parse.parse_qsl(query))
    action = params.get("action", "")

    if not action:
        list_root()
        return
    if action in ("media", "mediawin"):
        # The real playlist UI is the custom media window — this plugin
        # entry stays as the launch row + managed-mode status surface.
        xbmc.executebuiltin("RunScript(script.screensemble,media)")
        list_root(update=True)
        return
    # Deep-link safety — no local playlist editing while managed.
    if _managed() and action in {"item", "shuffle", "addfile", "addurl"}:
        list_root(update=True)
        return
    if action == "item":
        item_menu(params["name"])
    elif action == "cmd":
        _command(params["cmd"])
    elif action == "shuffle":
        state = medialist.load_state()
        state["shuffle"] = not state["shuffle"]
        _apply(state)
    elif action == "addfile":
        _add_file()
    elif action == "addurl":
        _add_url()
    elif action == "settings":
        ADDON.openSettings()
    # refresh and every action re-render the root in place — folder
    # items must satisfy the directory contract, and updateListing keeps
    # the user on the same screen instead of pushing a new level.
    list_root(update=True)


if __name__ == "__main__":
    main()
