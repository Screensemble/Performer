# AGENTS.md — Screensemble Player (Kodi Addon)

## Screen lock (optional kiosk mode)

`local_lock` + `local_password` (settings.xml Security group, offered in the
setup wizard): every exit path funnels through `exit_to_kodi()` in
service.py, which prompts for the password when the lock is on. The local
web admin (`web.py`) gets a password-gated login page under the same flag —
stateless `ss_lock` cookie, sessions die at Kodi restart. Kodi cannot
intercept Android TV's Home button; OS-level kiosk needs Android app
pinning (documented in the site docs).

## What this is

Kodi service addon that turns a device (e.g., Android TV box) into a Screensemble display. Works standalone (local playlists/schedules) or connected to a Screensemble server (Managed or Orchestrated mode). Responsible for the idle presentation, local asset caching, video playback, and seamless content transitions.

## Tech

- Kodi 19 (Matrix) or later — the Python 3 ABI (`xbmc.python` 3.0.0) is the hard floor: Kodi 18 runs Python 2 and can't install the zip at all. Every API the addon calls is v19 or older (`WindowXML`/`doModal`, `Monitor`, `PlayList`, `onAVStarted`, `setImage(useCache)` is v13-era, `getSettingBool` is v18-era), and the code is Python 3.8-clean for Matrix's runtime — so 19 is the minimum by capability, not preference.
- Service addon (`xbmc.service`) — starts with Kodi, no user-facing UI.
- Use only official addon APIs: `xbmc`, `xbmcvfs`, `xbmcaddon`, `xbmcgui`, `special://` paths.
- Media lives in the addon profile directory by default; a custom folder is an advanced settings option.

## Structure

- `addon.xml` — addon manifest (id `script.screensemble`).
- `service.py` — service entry point. Main thread keeps the idle window modally resident (`doModal`); a daemon thread handles commands, settings reloads, setup completion, and the sequence engine tick.
- `idle.py` — idle rendering: `IdleWindow` (WindowXML), runtime PNG generation, setting helpers, image underlay used for seamless video↔image transitions.
- `link.py` — server link: persistent outbound WebSocket (`/ws/player`), hello/status/commands, `media_op`, settings proxy, `pull_file` downloads (sha256-verified, `.part`-staged), file serving for thumbnails.
- `medialist.py` — playlist state (`media_state.json`): order, disabled, durations, schedules, url entries, shuffle — plus the managed-cache manifest and eviction (`media_manifest.json`).
- `media.py` — on-TV playlist manager (WindowXML): reorder, enable/disable, per-item duration/schedule, add file/URL, shuffle, preview.
- `menu.py`, `default.py` — Programs-menu entry point and action dialogs; `default.py` also handles settings Browse… pickers.
- `wizard.py` — custom WindowXML first-run setup wizard.
- `splash.py`, `qr.py` — startup splash and QR pairing tile.
- `web.py` — web admin (`ThreadingHTTPServer`, default port 8282): status, settings, media ops, upload, pairing endpoints; pages in `resources/www/`.
- `tools/build_zip.py` — build the Kodi-installable zip (`script.screensemble/` top folder); used by CI, releases, and local builds.
- `tools/build_repo.py` — generate the Kodi repository index (`addons.xml` + md5 + asset layout); run by the release workflow.
- `tools/repository.screensemble/` — the repository-addon skeleton users install once for release updates.
- `resources/settings.xml`, `resources/skins/Default/1080i/*.xml`, `resources/www/*.html`, `resources/language/…/strings.po`.

## Key invariants

- The idle window is opened once via `doModal()` and stays resident for the whole session; fullscreen video renders over it. Never re-`show()` it per transition — that's what caused the multi-second Kodi-UI gap.
- Consecutive videos are chained into a real `xbmc.PlayList` (wraparound-aware); the image after the last chained video is preloaded onto the idle window beneath playback. Video↔image transitions must never expose black, the logo, or the Kodi UI.
- Content edits apply at the next natural item boundary (`defer_content_refresh`), never mid-piece. Pushed files flag only after the batch settles. Explicit `restart` commands stay immediate.
- A custom keymap must not ship — the service removes `userdata/keymaps/screensemble.xml` on start; Back-during-video is handled by watching the fullscreen→GUI transition.
- Settings reads must go through a fresh `xbmcaddon.Addon()` — a long-lived instance caches and misses cross-instance writes.
- Settings path controls must be plain text + a Browse… action button calling `Dialog().browse` — Kodi's native browse controls crash the settings dialog.
- `ControlImage.setImage(..., useCache=False)` for image rotation — otherwise Kodi serves the stale texture.
- Player identity is hardware-anchored; the link token is per-device, issued by the server at QR pairing (never minted locally), and enforced on every hello — unpaired displays are refused. `getInfoLabel("Network.MacAddress")` returns the literal string `"Busy"` until the label resolves — only accept a real MAC shape and pin the first-resolved identity in `device.id`, or every device boots to the same `hw-` id and pairings overwrite each other.
- The local web admin refuses cross-site POST/DELETE (Origin / Sec-Fetch-Site check); in managed mode only read-only status endpoints stay open, and `pull_file` only fetches http(s) URLs on the paired server's host.

## Development workflow

- Build a distributable zip with `python tools/build_zip.py`; install on a device via Kodi's *Install from zip*, or copy the folder into Kodi's `addons/` as `script.screensemble` and restart.
- Kodi logs: `kodi.log` on the device; service logs are prefixed `[screensemble]`.

## Conventions

- The player core (idle renderer, cache, transitions, playback) must never depend on the server; server communication lives in `link.py`.
- Never expose the Kodi home screen, playback controls, or Android UI during normal operation. (Exception: Kodi renders Home before service addons start, so a cold boot shows ~1s of Estuary — unavoidable, documented in the site docs under kiosk lockdown.)
- Idle presentation shows at boot before any server connection; a managed display with no content holds a branded standby, never a blank.
- Asset updates are atomic: download → verify (SHA-256) → stage (`.part`) → activate; never destroy a working presentation.
- No video decoding, audio output, or UI animation while idle.
