"""Script entry point — what runs when the user opens Screensemble from
Programs. First launch runs the setup wizard; afterwards a small menu
offers resume / settings / re-run wizard.
"""

import sys

import xbmc
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
    "idle_image": "Choose the idle image",
    "test_video": "Choose the test video",
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

    if not ADDON.getSettingBool("setup_complete"):
        run_wizard()
        return

    choice = xbmcgui.Dialog().select(
        "Screensemble",
        [
            "Resume idle presentation",
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


if __name__ == "__main__":
    main()
