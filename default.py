"""Script entry point — what runs when the user opens Screensemble from
Programs. First launch runs the setup wizard; afterwards a small menu
offers resume / settings / re-run wizard.
"""

import xbmc
import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()
HOME = xbmcgui.Window(10000)
COMMAND_PROPERTY = "screensemble.command"
COMMAND_RESUME = "resume"


def main():
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
