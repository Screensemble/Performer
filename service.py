import xbmc
import xbmcaddon

ADDON = xbmcaddon.Addon()


def get_setting(key, default=""):
    return ADDON.getSetting(key) or default


def main():
    monitor = xbmc.Monitor()
    server_address = get_setting("server_address")
    device_name = get_setting("device_name")
    xbmc.log(
        f"[screensemble] service starting (device={device_name!r}, server={server_address!r})",
        xbmc.LOGINFO,
    )

    # TODO: player core — idle renderer, local asset cache/manifest, and the
    # mode adapters (standalone scheduler / managed / controlled). Player core
    # must never depend on the server; see spec sections 1 and 7.
    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break

    xbmc.log("[screensemble] service stopping", xbmc.LOGINFO)


if __name__ == "__main__":
    main()
