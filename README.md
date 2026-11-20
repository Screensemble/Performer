<p align="center">
  <img src="icon.png" alt="Screensemble logo" width="128">
</p>
<h1 align="center">Screensemble Player</h1>
<p align="center">
  A Kodi addon that turns any device into an always-intentional signage display.
  <br><em>An ensemble of screens.</em>
</p>
<p align="center">
  <a href="AI-DECLARATION.md"><img src="https://img.shields.io/badge/AI--DECLARATION-pair-blue" alt="AI-DECLARATION: pair"></a>
</p>
<p align="center">
  <a href="https://github.com/Screensemble/Screensemble-Addon/releases"><img src="https://img.shields.io/github/v/release/Screensemble/Screensemble-Addon" alt="Latest release"></a>
  <a href="https://github.com/Screensemble/Screensemble-Addon/actions/workflows/release.yml"><img src="https://github.com/Screensemble/Screensemble-Addon/actions/workflows/release.yml/badge.svg" alt="Release build"></a>
  <a href="https://github.com/Screensemble/Screensemble-Addon/actions/workflows/ci.yml"><img src="https://github.com/Screensemble/Screensemble-Addon/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

**Every display always shows an intentional state.** A display should never look unattended just because it's waiting — no Kodi home screen, no Android launcher, no playback controls, no blank. The idle presentation stays resident beneath playback, so transitions reveal it instantly instead of exposing whatever was underneath.

Runs fully standalone with its own playlist, or paired to a [Screensemble Server](../Screensemble-Server) for fleet-managed content — and it keeps playing its last known content when the server is unreachable.

## Features

- **Unified content sequence** - one playlist across images and videos: ordering, per-item enable/disable, schedules (dates, weekdays, time-of-day incl. overnight), shuffle, and remote URL items.
- **Seamless transitions** - consecutive videos chain inside a single Kodi playlist (~100ms internal hops, seamless all-video loops); the next image is preloaded beneath the last video of a run so video→image is an instant reveal; images hold through the video load gap. Same behavior across the loop wraparound.
- **Managed mode** - pair via QR or on-TV confirm, then everything is remote: media pushes (SHA-256 verified, `.part`-staged then activated), shared playlist sync, settings, commands.
- **Deferred updates** - content changes queue and apply at the next natural item boundary — a video finishes, an image timer expires. Pushed batches settle before flagging. Nothing cuts mid-piece.
- **Local mode** - completely standalone: own media folder, on-TV playlist manager, web admin on the LAN. No server needed, ever.
- **Managed cache** - server media lives in a cache folder with a configurable cap; unreferenced files evict least-recently-used first, playlist items are never evicted.
- **Branded standby** - empty managed displays hold a dim brand mark on black; empty local displays rotate feature cards.
- **Remote-first input** - everything works from a basic D-pad remote. No keyboard, no mouse.
- **Stall watchdog** - a hung video (player alive, clock frozen) is skipped automatically. Screensaver stays inhibited while the service runs.

## Quick start

**From the Screensemble repository (recommended):** install
`repository.screensemble-<version>.zip` from the latest release once, then
install **Screensemble Player** from that repository inside Kodi — new
releases arrive through Kodi's normal update check.

**Direct zip:** grab `script.screensemble-<version>.zip` from the latest
release, then Kodi → Settings → Add-ons → **Install from zip file** → restart.

Requirements: Kodi 19 (Matrix) or later. On first run the addon stays hidden — open it from **Programs → Screensemble** to launch the setup wizard.

## How it works

1. **Setup wizard** - pick Local or Server mode; media folder defaults to the built-in profile folder (a custom folder is an advanced setting).
2. **Local mode** - drop media in the folder (or use the web admin to upload); the display runs its own sequence with your order, durations, and schedules.
3. **Managed mode** - pair to a server with the QR code or on-TV confirm; the display pulls its content over the same outbound socket it reports to. Offline? It keeps playing what's cached.
4. **Playback** - the idle window stays modally resident beneath video, so every transition is a reveal, never a reload.

## Remote map

| Button | On the idle screen | During video |
|---|---|---|
| OK / Select / Menu / Play | Actions menu | Kodi player OSD |
| D-pad right / down | Next item | Kodi player OSD |
| D-pad left / up | Previous item | Kodi player OSD |
| Back | Once: toast; twice within 3 s: exit to Kodi UI | Stop → return to idle |

Resume after exiting: **Programs → Screensemble → Resume**.

## Web admin

In Local mode the addon serves an admin page on port **8282**
(configurable; enable/disable needs a Kodi restart):

- `http://<device-ip>:8282/` - status, playlist ordering, enable/disable,
  per-item durations and schedules, shuffle, URL entries, media
  upload/delete, transport commands, settings.

Media management is disabled when the media folder is a remote VFS path
(`smb://`, `nfs://`, …) since Python can't write those.

## Settings

Kodi → Add-ons → My add-ons → Services → **Screensemble Player** →
Configure. Setup (mode, media folder, web admin), Connection (server
address, device name), Idle Presentation (colors, scaling), Advanced
(custom folders, media cache limit, re-run wizard).

Path pickers are text fields plus a **Browse…** button that opens Kodi's
VFS browser — do not reintroduce native browse controls; they crash
Kodi's settings dialog. Changes apply live without a restart.

## Development

```sh
python tools/build_zip.py   # dist/script.screensemble-<ver>.zip
```

For faster iteration, copy this folder into Kodi's `addons/` directory
named `script.screensemble` and restart Kodi. Logs land in `kodi.log`
with the `[screensemble]` prefix — `seq N/M` marks each item advance,
`content update queued` marks a deferred change. See `AGENTS.md` for
architecture notes and the invariants that keep transitions clean.

## Releasing

Publish a GitHub Release tagged to match `addon.xml`'s `version` (e.g.
`v0.2.0`) and Actions builds the addon zip + repository addon zip,
attaches them to the release, and refreshes the `repo` branch that serves
as the Kodi repository index.

## License

[MIT](LICENSE)
