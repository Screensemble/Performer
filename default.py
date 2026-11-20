"""Script entry point — what runs when the user opens Screensemble from
Programs. First launch runs the setup wizard; afterwards a small menu
offers resume / settings / re-run wizard.
"""

import sys

import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()
HOME = xbmcgui.Window(10000)
COMMAND_PROPERTY = "screensemble.command"
COMMAND_RESUME = "resume"
COMMAND_RELOAD = "reload"

# Browse types for Dialog().browse: 0=directory 1=file 2=image
PICK_HEADINGS = {
    "local_media_folder": "Choose the media folder",
    "server_media_dir": "Choose the cache folder for server media",
    "idle_image": "Choose the standby image",
}


def pick(setting_id, browse_type):
    """Called from settings Browse… buttons (RunScript). Opens Kodi's VFS
    file browser and writes the chosen path back to the setting."""
    path = xbmcgui.Dialog().browse(
        browse_type, PICK_HEADINGS.get(setting_id, "Choose"), "files"
    )
    if path:
        ADDON.setSetting(setting_id, path)
        # Tell the running service directly — onSettingsChanged does not
        # reliably fire in the service instance for cross-instance writes.
        HOME.setProperty(COMMAND_PROPERTY, COMMAND_RELOAD)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "pick":
        pick(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0)
        return

    from wizard import run_wizard

    if len(sys.argv) > 1 and sys.argv[1] == "wizard":
        run_wizard()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "media":
        from media import run_media_ui
        run_media_ui()
        return

    if not ADDON.getSettingBool("setup_complete"):
        run_wizard()
        return

    if ADDON.getSettingInt("player_mode") == 1:
        addr = ADDON.getSetting("server_address") or "server"
        choice = xbmcgui.Dialog().select(
            f"Screensemble — managed by {addr}",
            [
                "Resume standby",
                "Settings",
                "Re-run setup wizard",
            ],
        )
        if choice == 0:
            HOME.setProperty(COMMAND_PROPERTY, COMMAND_RESUME)
        elif choice == 1:
            ADDON.openSettings()
        elif choice == 2:
            run_wizard()
        return

    choice = xbmcgui.Dialog().select(
        "Screensemble",
        [
            "Media list & playlist controls",
            "Resume standby",
            "Settings",
            "Re-run setup wizard",
        ],
    )
    if choice == 0:
        from media import run_media_ui
        run_media_ui()
    elif choice == 1:
        HOME.setProperty(COMMAND_PROPERTY, COMMAND_RESUME)
    elif choice == 2:
        ADDON.openSettings()
    elif choice == 3:
        run_wizard()


if __name__ == "__main__":
    main()
