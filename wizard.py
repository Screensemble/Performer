"""First-run setup wizard. Launched by the script entry point (default.py)
when the addon is opened from Programs, or on demand via 'Re-run setup
wizard'. Writes addon settings; the service picks them up and starts the
idle presentation once setup_complete is set.
"""

import xbmc
import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()


def _cancel():
    xbmcgui.Dialog().notification(
        "Screensemble",
        "Setup cancelled — open Screensemble again to finish setup",
        xbmcgui.NOTIFICATION_WARNING,
    )


def _finish(mode):
    ADDON.setSettingBool("setup_complete", True)
    xbmcgui.Dialog().ok(
        "Screensemble",
        "Setup complete. The idle presentation will start automatically.",
    )


def run_wizard():
    dlg = xbmcgui.Dialog()
    mode = dlg.select(
        "Screensemble setup — how should this display run?",
        [
            "Local (standalone) — play media from a local folder",
            "Server — managed/controlled by a Screensemble server",
        ],
    )
    if mode == 0:
        _wizard_local(dlg)
    elif mode == 1:
        _wizard_server(dlg)
    else:
        _cancel()


def _wizard_local(dlg):
    folder = dlg.browse(0, "Choose the media folder", "files")
    if not folder:
        return _cancel()
    ADDON.setSetting("local_media_folder", folder)

    options = ["5 s", "10 s", "15 s", "30 s", "60 s"]
    choice = dlg.select("Slideshow — seconds per image", options)
    seconds = int(options[choice if choice >= 0 else 1].split()[0])
    ADDON.setSettingInt("slideshow_seconds", seconds)

    ADDON.setSettingInt("player_mode", 0)
    _finish(mode=0)


def _wizard_server(dlg):
    address = dlg.input(
        "Server address (host or host:port)",
        ADDON.getSetting("server_address"),
        xbmcgui.INPUT_ALPHANUM,
    )
    if not address:
        return _cancel()
    ADDON.setSetting("server_address", address)

    name = dlg.input(
        "Device name (optional)",
        ADDON.getSetting("device_name"),
        xbmcgui.INPUT_ALPHANUM,
    )
    if name is not None:
        ADDON.setSetting("device_name", name)

    cache_dir = dlg.browse(0, "Choose the cache folder for server media", "files")
    if cache_dir:
        ADDON.setSetting("server_media_dir", cache_dir)

    ADDON.setSettingInt("player_mode", 1)
    dlg.ok(
        "Screensemble",
        "Enrollment happens automatically when the player reaches the server. "
        "Content, schedules, and idle profiles then come from the server; "
        "until the first successful connection the player shows its local "
        "idle presentation.",
    )
    _finish(mode=1)
