"""Playlist state for the local media folder — per-asset ordering,
enable/disable, per-item image duration, scheduling, remote URL entries,
and shuffle. Persisted in addon_data/media_state.json:

    {
      "order": ["a.mp4", "b.mp4"],
      "disabled": ["c.mp4"],
      "durations": {"img.png": 15},          // seconds, images only
      "schedules": {
        "promo.mp4": {
          "start": "2026-01-01",             // optional, inclusive
          "end":   "2026-02-01",             // optional, inclusive
          "days":  [0, 1, 2],                // optional, 0=Monday
          "from":  "08:00",                  // optional, local time
          "to":    "18:00"                   // optional; to < from wraps
        }
      },
      "urls": [{"name": "Lobby cam", "url": "https://…"}],
      "shuffle": false
    }

New files not in `order` play at the end (alphabetically). Entries whose
schedule doesn't match the current time are skipped like disabled ones.
"""

import datetime
import json
import os
import random
import time

import xbmc

from idle import PROFILE

STATE_PATH = os.path.join(PROFILE, "media_state.json")
MANIFEST_PATH = os.path.join(PROFILE, "media_manifest.json")


def _log(msg):
    xbmc.log(f"[screensemble] {msg}", xbmc.LOGINFO)

# Built-in media folder — exists on every Kodi install, survives addon
# updates, and is the playback source when local_media_folder is empty.
# Pointing the display at a custom folder is an advanced settings option.
DEFAULT_MEDIA_DIR = os.path.join(PROFILE, "media")
os.makedirs(DEFAULT_MEDIA_DIR, exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv",
              ".mpg", ".mpeg", ".ts"}

_EMPTY = {
    "order": [],
    "disabled": [],
    "durations": {},
    "schedules": {},
    "urls": [],
    "shuffle": False,
}


def load_state():
    state = dict(_EMPTY)
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        state["order"] = [str(x) for x in d.get("order", [])]
        state["disabled"] = [str(x) for x in d.get("disabled", [])]
        state["durations"] = {
            str(k): float(v) for k, v in d.get("durations", {}).items()
        }
        state["schedules"] = {
            str(k): v for k, v in d.get("schedules", {}).items()
            if isinstance(v, dict)
        }
        state["urls"] = [
            {"name": str(u.get("name", "")), "url": str(u.get("url", ""))}
            for u in d.get("urls", [])
            if isinstance(u, dict) and u.get("url")
        ]
        state["shuffle"] = bool(d.get("shuffle", False))
    except Exception:
        pass
    return state


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def sort_names(names, state):
    """Effective play order: names listed in state['order'] first (in that
    order, existing entries only), then the rest alphabetically. When
    shuffle is on, the whole list is randomized instead."""
    names = list(names)
    if state.get("shuffle"):
        random.shuffle(names)
        return names
    seen = set(names)
    ordered = [n for n in state["order"] if n in seen]
    tail = sorted(n for n in names if n not in set(ordered))
    return ordered + tail


def _parse_hhmm(value):
    try:
        h, m = str(value).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, TypeError):
        return None


def schedule_active(sched, now=None):
    """Is an entry with this schedule dict eligible right now? Missing
    fields are unconstrained; a from > to window wraps midnight."""
    if not sched:
        return True
    now = now or datetime.datetime.now()
    try:
        start = sched.get("start")
        if start and now.date() < datetime.date.fromisoformat(start):
            return False
        end = sched.get("end")
        if end and now.date() > datetime.date.fromisoformat(end):
            return False
        days = sched.get("days")
        if days and now.weekday() not in [int(d) for d in days]:
            return False
        frm = _parse_hhmm(sched.get("from"))
        to = _parse_hhmm(sched.get("to"))
        if frm is not None or to is not None:
            cur = now.hour * 60 + now.minute
            frm = 0 if frm is None else frm
            to = 24 * 60 if to is None else to
            if frm <= to:
                return frm <= cur < to
            return cur >= frm or cur < to  # overnight window
    except (ValueError, TypeError):
        return True  # unparsable schedule: don't hide the asset
    return True


def is_enabled(name, state):
    return name not in set(state["disabled"])


# -- managed-mode cache ------------------------------------------------------
# media_manifest.json records when each media file was last part of the
# playlist: {name: {"seen": epoch}}. The managed display's media folder is
# a cache for server content, so when it grows past the configured cap,
# files the playlist no longer references are evicted oldest-first.

def _load_manifest():
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            m = json.load(f)
            return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(m):
    try:
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(m, f)
    except OSError as e:
        _log(f"manifest save failed: {e}")


def touch_manifest(names):
    """Record the items the playlist currently references — evictions
    expire least-recently-touched files first."""
    if not names:
        return
    m = _load_manifest()
    now = time.time()
    for n in names:
        m[n] = {"seen": now}
    _save_manifest(m)


def pinned_names(state=None):
    """Items the playlist references — never cache-evicted."""
    state = state or load_state()
    return (
        set(state["order"]) | set(state["disabled"])
        | set(state["durations"]) | set(state["schedules"])
        | {u["name"] for u in state["urls"]}
    )


def enforce_cache_limit(folder, budget_bytes):
    """Managed-cache cap: when the media folder exceeds the budget, files
    the playlist doesn't reference are evicted least-recently-seen first.
    Pinned items are never removed; a playlist that alone exceeds the
    budget just logs a warning."""
    if (
        not budget_bytes or budget_bytes <= 0
        or not folder or "://" in folder
    ):
        return
    try:
        entries = [
            (n, os.stat(os.path.join(folder, n)))
            for n in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, n))
        ]
    except OSError:
        return
    total = sum(st.st_size for _, st in entries)
    if total <= budget_bytes:
        return
    manifest = _load_manifest()
    pinned = pinned_names()
    evictable = sorted(
        (e for e in entries if e[0] not in pinned),
        key=lambda e: manifest.get(e[0], {}).get("seen", 0.0),
    )
    for n, st in evictable:
        if total <= budget_bytes:
            break
        try:
            os.remove(os.path.join(folder, n))
        except OSError as e:
            _log(f"cache evict {n} failed: {e}")
            continue
        total -= st.st_size
        manifest.pop(n, None)
        _log(f"cache evict {n} ({st.st_size} bytes)")
    _save_manifest(manifest)
    if total > budget_bytes:
        _log(
            f"cache over budget: {total} > {budget_bytes} bytes — "
            "the playlist itself exceeds the cap"
        )


def has_schedules(state):
    """Any entry actually carrying a schedule — controls whether the
    service re-scans on a timer for time-window changes."""
    return any(state["schedules"].values())
