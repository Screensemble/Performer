# Screensemble Player (Kodi addon)

Service addon that turns a Kodi device into a Screensemble signage display.
See `AGENTS.md` for architecture notes and `../screensemble-design-spec.md`
(in the parent folder) for the full design.

Current state: **Proof of concept** — idle presentation rendering and the
video-to-idle transition, built to answer spec open question #2 (which
mechanism draws the idle screen?) and #7 (can playback be prepared
invisibly?).

## Install on a PC/Mac/Linux Kodi

1. Zip the contents of this folder so `addon.xml` is at the zip root
   (zip name doesn't matter).
2. Kodi → Settings → Add-ons → **Install from zip file** → pick the zip.
3. Restart Kodi (or disable/enable the addon) so the service starts.

For faster iteration, you can also copy/link this folder directly into Kodi's
`addons/` directory (`%APPDATA%\Kodi\addons\` on Windows) — it must be named
`script.screensemble` — then restart Kodi.

## Configure

Kodi → Add-ons → My add-ons → Services → **Screensemble Player** → Configure:

- **Idle engine**: `Dedicated window` (the candidate design) or
  `ShowPicture builtin` (comparison only).
- **Type**: `Solid color` + `Idle color`, or `Static image` + pick a file.
- **Scaling** (image mode): Contain / Cover / Stretch; `Letterbox color`
  fills unused space in Contain mode.
- **Test video**: pick a local video file — required for the transition test.
- **Debug overlay**: shows a status line on the idle screen; leave on for PoC.

Restart Kodi after changing settings (the PoC reads them once at startup).

## What to test

The idle presentation appears automatically when the service starts.

While the idle window is up (window engine only):

| Key | Action |
|---|---|
| `P` or `Enter` | Play the configured test video fullscreen |
| `A` | "Arm" — start then immediately pause. **Watch for a first-frame flash over the idle screen** |
| `G` | "Go" — resume paused playback |
| `M` | Maintenance — deliberately reveal the Kodi UI; the OK dialog returns you to idle |

Everything else (ESC, back, arrows) is swallowed — the idle window should not
be closable by accident.

### Pass/fail observations (spec section 12)

- **Idle at boot**: does the idle screen come up by itself? How much Kodi UI
  is visible before it appears? (On desktop Kodi some home-screen flash at
  startup is expected; on the kiosk target the addon would be the launcher —
  note the difference.)
- **Idle → playback**: does the video take over cleanly, no intermediate UI?
- **Playback → idle** (primary criterion): when the video ends naturally, when
  you stop it (`X`), and on error — does Kodi's home screen or OSD flash
  between the last frame and the idle window? Roughly how long is the gap?
- **Arm**: after pressing `A`, is the video's first frame visible? Is the
  paused state stable? Does `G` resume promptly?
- **Repeat**: run the cycle many times — leaks, drift, UI flashes?

## Logs

All events are timestamped in `kodi.log` (Windows: `%APPDATA%\Kodi\kodi.log`)
with the `[screensemble]` prefix — `playback ended t=...` vs `idle shown
t=...` gives the transition gap in seconds.

## Known limitations (PoC)

- `ShowPicture` engine can't intercept keys or attach the debug overlay —
  this is itself a finding in favor of the window approach.
- Settings are read once at startup; live reconfiguration comes later.
- No server connection, asset cache, or scheduling yet — PoC scope only.
