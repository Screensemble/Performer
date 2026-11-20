# Screensemble Player (Kodi addon)

Service addon that turns a Kodi device into a Screensemble signage display.
See `AGENTS.md` for architecture notes.

The display always shows an intentional state: the idle presentation stays
resident beneath playback, so video↔image transitions reveal it instantly
— the Kodi home screen, OSD, and launcher are never exposed during normal
operation.

## Features

- **Unified content sequence** — a single playlist across images and
  videos, honoring stored order, per-item enable/disable, schedules
  (dates, weekdays, time-of-day incl. overnight), shuffle, and remote URL
  items.
- **Seamless transitions** — consecutive videos chain inside one Kodi
  playlist (~100ms internal hops, seamless all-video loops); the next
  image is preloaded beneath the last video of a run so video→image is an
  instant reveal; images hold through the video load gap. Works across
  the loop wraparound.
- **Managed mode** — pair the display to a Screensemble server (QR or
  on-TV confirm) and it's fully remote-managed: media pushes (SHA-256
  verified, staged `.part` → activate), shared playlist sync, settings,
  commands. Content changes apply at the next natural item boundary, so
  updates never cut a piece short.
- **Managed cache** — server media lives in a cache folder with a
  configurable size cap; unreferenced files evict least-recently-used
  first, pinned playlist items are never evicted.
- **Local mode** — runs entirely standalone: own media folder, playlist
  UI on the TV, web admin on the LAN, everything keeps working with no
  server at all.
- **Custom setup wizard** — first launch from Programs runs a step UI
  (Local vs Server, slideshow timing, server address). Fully
  remote-navigable.
- **Branded standby** — with no content, a managed display holds a dim
  brand mark on black; a local display rotates feature cards.
- **Remote-first input** — everything works from a basic D-pad remote;
  no keyboard required.
- **Screensaver inhibition** while the service runs.
- **Stall watchdog** — a hung video (player alive, clock frozen) is
  skipped automatically.

## Install

**From the Screensemble repository (recommended):** install
`repository.screensemble` once from a release zip, then install
**Screensemble Player** from it — new releases arrive through Kodi's
normal update check.

**Direct zip:** grab `script.screensemble-<version>.zip` from the latest
release, then Kodi → Settings → Add-ons → **Install from zip file**.
Restart Kodi so the service starts.

For development, copy this folder into Kodi's `addons/` directory named
`script.screensemble` and restart, or build the zip with
`python tools/build_zip.py`.

On first run the addon stays hidden until you open it from
**Programs → Screensemble**, which launches the setup wizard.

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

- `http://<device-ip>:8282/` — status, playlist ordering,
  enable/disable, per-item durations and schedules, shuffle, URL entries,
  media upload/delete, transport commands, settings.

Media management is disabled when the media folder is a remote VFS path
(`smb://`, `nfs://`, …) since Python can't write those.

## Settings

Kodi → Add-ons → My add-ons → Services → **Screensemble Player** →
Configure. Setup (mode, media folder, web admin), Connection (server
address, device name), Idle Presentation (colors, scaling), Advanced
(custom folders, media cache limit, re-run wizard).

Path pickers in settings are text fields plus a **Browse…** button that
opens Kodi's VFS browser — do not reintroduce native browse controls;
they crash Kodi's settings dialog. Changes apply live without a restart.

## Logs

Service events appear in `kodi.log` with the `[screensemble]` prefix —
`seq N/M` lines mark each item advance, `content update queued` marks a
deferred change waiting for the next boundary.
