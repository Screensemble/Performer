# AGENTS.md — Screensemble Player (Kodi Addon)

## What this is

Kodi service addon that turns a device (e.g., TiVo Stream 4K / Android TV) into a Screensemble display. Works standalone (local playlists/schedules) or connected to a Screensemble server (Managed or Controlled mode). Responsible for the idle presentation, local asset caching, video playback, and local video-to-idle transitions.

The governing design document is `../screensemble-design-spec.md` (in the parent folder). Key sections: 1 (operating modes), 5 (playback lifecycle / Arm-Go), 7 (idle presentation), 8 (addon design).

## Tech

- Kodi 19 (Matrix) or later, Python 3 API (`xbmc.python` 3.0.0). Whether to support Kodi 18 is an open question (spec section 16).
- Service addon (`xbmc.service`) — starts with Kodi, no user-facing UI.
- Use only official addon APIs: `xbmc`, `xbmcvfs`, `xbmcaddon`, `xbmcgui`, `special://` paths.
- Store media in the addon profile directory or a server-configurable path.

## Structure

- `addon.xml` — addon manifest (id `script.screensemble`).
- `service.py` — service entry point, monitor loop, playback callbacks, PoC test actions.
- `idle.py` — idle rendering: `IdleWindow` (WindowXML), runtime PNG generation, setting helpers.
- `resources/settings.xml` — addon settings (connection, idle presentation, PoC testing).
- `resources/skins/Default/1080i/idle.xml` — fullscreen idle window layout.

## Development workflow

- Install on a device by zipping the addon folder (Kodi → Install from zip). Do not commit the zip.
- PoC install/test steps and pass/fail criteria: see `README.md`.
- Kodi logs: `kodi.log` on the device; service logs are prefixed `[screensemble]` and timestamped with `time.perf_counter()` for transition-gap measurement.
- Target hardware for validation: TiVo Stream 4K (spec sections 11–12); desktop Kodi is the current dev loop.

## Conventions

- The player core (idle renderer, cache, transitions, playback) must never depend on the server; server communication lives behind an adapter.
- Never expose the Kodi home screen, playback controls, or Android UI during normal operation.
- Idle presentation shows at boot before any server connection; fallback chain: active asset → previous known-good → configured solid color → black.
- Asset updates are atomic: download → verify (SHA-256) → stage → activate; never destroy a working presentation.
- No video decoding, audio output, or UI animation while idle.
