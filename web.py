"""Local web admin for Screensemble — a stripped-down single-display
management surface served from inside the addon (Local mode only).

A ThreadingHTTPServer runs on a daemon thread in the service process and
calls the StandbyController directly — no auth, intended for the gallery's
isolated LAN. Endpoint shapes mirror what the future server will use so
the two management surfaces stay interchangeable.
"""

import hashlib
import hmac
import json
import os
import secrets
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import xbmc
import xbmcgui
import xbmcvfs

from standby import PROFILE

# Seconds the managed link must be down before the degraded local admin
# opens — a temporary outage never exposes it.
DEGRADED_AFTER = 120

import medialist
from standby import ADDON_PATH, resolve_path, restart_app
from medialist import IMAGE_EXTS, VIDEO_EXTS

WWW_DIR = os.path.join(ADDON_PATH, "resources", "www")
HOME = xbmcgui.Window(10000)

# Home-window properties shared with the wizard/standby/script processes.
PROP_SETUP_URL = "screensemble.setup_url"     # full /setup?k=… URL (QR target)
PROP_ADMIN_URL = "screensemble.admin_url"     # http://ip:port/
PROP_WEB_SETUP = "screensemble.websetup"      # "1" while a phone is in /setup
PROP_PAIR_URL = "screensemble.pairurl"        # pending server URL offered to the TV
PROP_PAIR_INFO = "screensemble.pairinfo"      # server /api/info JSON (name, owner…)
PROP_PAIR_ID = "screensemble.pairid"          # server's pairing_id for accept/decline

WEB_SETUP_STALE = 5.0      # seconds without a ping before the wizard resumes

# Pairing requires the server to identify itself via GET /api/info:
# product must be "screensemble-server" and version >= this. The server can
# likewise gate us via its min_client_version field — checked both ways.
MIN_SERVER_VERSION = "0.2.0"

# Whitelisted editable settings: id -> ("bool"|"int"|"str")
EDITABLE_SETTINGS = {
    "local_media_folder": "str",
    "local_lock": "bool",
    "local_password": "str",
    "maintenance_timeout": "int",
    "tls_insecure": "bool",
    "idle_color": "str",
    "letterbox_color": "str",
    "idle_scaling": "int",
    "idle_type": "int",
    "idle_image": "str",
    "ensemble_hold": "bool",
    "loop_count": "int",
    "schedule_on": "bool",
    "schedule_days": "str",
    "schedule_from": "str",
    "schedule_to": "str",
    "off_image": "str",

    "device_name": "str",
}

# Settings the phone setup wizard may write (includes server/pairing fields).
SETUP_SETTINGS = dict(EDITABLE_SETTINGS, **{
    "player_mode": "int",
    "server_address": "str",
    "server_media_dir": "str",
    "setup_complete": "bool",
})


def log(msg):
    xbmc.log(f"[screensemble] {msg}", xbmc.LOGINFO)


def lan_ip():
    """Best-effort LAN IPv4. xbmc.getIPAddress() can be empty/loopback early
    in Kodi boot, so fall back to a UDP-connect trick that only picks the
    outbound interface's address (no traffic is actually sent)."""
    ip = xbmc.getIPAddress()
    if ip and not ip.startswith("127."):
        return ip
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 80))  # TEST-NET-1: routed? picks iface only
        addr = s.getsockname()[0]
        s.close()
        return addr if not addr.startswith("127.") else ""
    except Exception:
        return ""


def _addon_read(addon, sid, kind):
    if kind == "bool":
        return addon.getSettingBool(sid)
    if kind == "int":
        return addon.getSettingInt(sid)
    return addon.getSetting(sid)


def _parse_version(v):
    try:
        return tuple(int(p) for p in str(v).split("."))
    except (TypeError, ValueError):
        return (0,)


def version_at_least(have, need):
    h, n = _parse_version(have), _parse_version(need)
    length = max(len(h), len(n))
    return h + (0,) * (length - len(h)) >= n + (0,) * (length - len(n))


def fetch_server_info(url, timeout=4.0):
    """Ask the offered server who it is (GET /api/info) so the on-TV
    confirm can show a name/owner, not just a raw URL. Any failure is
    non-fatal — the dialog falls back to the URL alone."""
    import urllib.request

    base = url if "://" in url else f"http://{url}"
    try:
        with urllib.request.urlopen(f"{base}/api/info", timeout=timeout) as r:
            if r.status == 200:
                data = json.loads(r.read().decode("utf-8", "replace"))
                return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"server info probe failed ({base}): {e}")
    return {}


def post_json(base, path, payload, timeout=4.0):
    """POST JSON to a Screensemble server endpoint. Returns (dict, None) on a
    2xx JSON response, else (None, human-readable error)."""
    import urllib.request

    url = base if "://" in base else f"http://{base}"
    req = urllib.request.Request(
        url + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8", "replace"))
            return None, data.get("reason") or f"HTTP {e.code}"
        except Exception:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


PASSWORD_RULE = "at least 8 characters, one letter, one digit"


def _password_ok(pw):
    p = str(pw)
    return (
        len(p) >= 8
        and any(c.isalpha() for c in p)
        and any(c.isdigit() for c in p)
    )


def _addon_write(addon, sid, kind, value):
    # The local lock holds the same bar as the conductor login — an empty
    # value is always fine (lock disabled), a set one must pass the rule.
    if sid == "local_password" and str(value) and not _password_ok(value):
        raise ValueError(f"password needs {PASSWORD_RULE}")
    if kind == "bool":
        addon.setSettingBool(sid, bool(value))
    elif kind == "int":
        addon.setSettingInt(sid, int(value))
    else:
        addon.setSetting(sid, str(value))


def make_handler(controller, addon, on_settings_saved, state):
    """state is the AdminServer instance: it owns the per-boot pairing token,
    the setup-page heartbeat, and pending pair offers."""

    class AdminHandler(BaseHTTPRequestHandler):
        server_version = "ScreensembleAdmin/0.1"

        def log_message(self, *args):  # keep request spam out of kodi.log
            pass

        # -- helpers ------------------------------------------------------

        def _json(self, obj, status=200, cors=False):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _static(self, path, ctype):
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._json({"error": "not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _managed(self):
            """Paired to a server — the human-facing admin surface is off
            until the link has been down long enough to matter. A display
            whose wizard finished in server mode but never paired has no
            conductor credential and isn't managed: the local admin and the
            /setup//pair flows must stay open or it can never enroll."""
            return (
                controller.config.get("player_mode") == 1
                and controller.config.get("setup_complete")
                and bool(controller.config.get("server_auth_token"))
            )

        def _degraded(self):
            """Managed but the conductor has been unreachable for a while —
            the reduced local admin (status, settings, transport, reconnect)
            opens so the display stays serviceable without the server."""
            if not self._managed():
                return False
            since = getattr(controller, "_offline_since", None)
            return (
                since is not None
                and time.monotonic() - since >= DEGRADED_AFTER
            )

        # Human-facing surface that stays open when the display is managed:
        # the admin UI itself, status reads, settings, transport commands,
        # and the lock's own endpoints. Playlist/content mutation and the
        # setup/pairing flows remain conductor-owned.
        _MANAGED_GET = {
            "/", "/admin.html", "/api/status", "/api/media", "/api/info",
            "/api/settings", "/login", "/favicon.ico", "/favicon.png",
            "/logo.png", "/wordmark.png",
        }
        _MANAGED_POST = {
            "/api/command", "/api/settings", "/api/login", "/api/logout",
        }

        def _managed_block(self):
            """Gate a request in managed mode. While the link is up only the
            machine endpoints are open and / shows a notice; once degraded
            the reduced human surface opens. Returns True when handled."""
            u = urllib.parse.urlparse(self.path)
            if self._degraded():
                if self.command == "GET" and u.path in self._MANAGED_GET:
                    return False
                if self.command == "POST" and u.path in self._MANAGED_POST:
                    return False
                self._json(
                    {"error": "content is managed by the conductor"},
                    403,
                )
                return True
            if self.command == "GET" and u.path in (
                "/api/status", "/api/media", "/api/info",
                # Browser chrome on the managed notice — a blocked
                # favicon/logo is only console noise, not a boundary.
                "/favicon.ico", "/favicon.png", "/logo.png", "/wordmark.png",
            ):
                return False
            if u.path == "/":
                self._pair_page(
                    "Managed display",
                    "This display is managed by a Screensemble conductor — "
                    "its admin opens locally if the link stays down.",
                )
            else:
                self._json({"error": "display is managed by a server"}, 403)
            return True

        def _pair_page(self, title, msg, close=False):
            """Tiny response for /pair* navigations. Success tabs auto-close
            (they were window.open()ed by the server page); failure pages
            stay open so the user can actually read the reason."""
            script = "<script>window.close()</script>" if close else ""
            body = (
                "<!doctype html><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{title}</title>"
                "<body style='background:#000;color:#dde3e8;font:20px sans-serif;"
                "display:flex;align-items:center;justify-content:center;height:100vh;"
                "margin:0;text-align:center'>"
                f"<div><h2 style='color:#3ec1f3'>{title}</h2><p>{msg}</p>"
                "<p style='color:#556570;font-size:14px'>You can close this tab.</p></div>"
                f"{script}</body>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _media_dir(self):
            """Local media folder as a filesystem path, or None if remote
            (smb:// etc. can't be written/listed with plain os calls)."""
            folder = resolve_path(
                controller.config.get("local_media_folder")
                or medialist.DEFAULT_MEDIA_DIR
            )
            if not folder or "://" in folder:  # smb://, nfs:// etc. aren't writable
                return None
            return folder

        def _image_size(self, path):
            """Cheap image header probe for the metadata column."""
            try:
                with open(path, "rb") as f:
                    data = f.read(131072)
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    return struct.unpack(">II", data[16:24])
                if data[:6] in (b"GIF87a", b"GIF89a"):
                    return struct.unpack("<HH", data[6:10])
                if data[:2] == b"BM":
                    return struct.unpack("<ii", data[18:26])
                if data[:2] == b"\xff\xd8":  # JPEG: walk segments to SOF
                    i = 2
                    while i + 9 < len(data):
                        if data[i] != 0xFF:
                            i += 1
                            continue
                        marker = data[i + 1]
                        if marker in (0xC0, 0xC1, 0xC2, 0xC5, 0xC6, 0xC7,
                                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                            h, w = struct.unpack(">HH", data[i + 5:i + 9])
                            return w, h
                        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                            i += 2
                            continue
                        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
            except Exception:
                return None
            return None

        def _media_list(self):
            """All playlist entries — folder files plus remote URL items —
            in effective play order with per-item metadata."""
            state = medialist.load_state()
            disabled = set(state["disabled"])
            try:
                current = controller.current_name()
            except Exception:
                current = ""
            entries = {}
            folder = self._media_dir()
            if folder is not None:
                try:
                    for n in os.listdir(folder):
                        full = os.path.join(folder, n)
                        if not os.path.isfile(full):
                            continue
                        ext = os.path.splitext(n)[1].lower()
                        kind = (
                            "video" if ext in VIDEO_EXTS
                            else "image" if ext in IMAGE_EXTS
                            else "other"
                        )
                        entries[n] = {
                            "name": n,
                            "size": os.path.getsize(full),
                            "kind": kind,
                            "remote": False,
                            "src": "/media/" + urllib.parse.quote(n),
                        }
                        if kind == "image":
                            dims = self._image_size(full)
                            if dims:
                                entries[n]["w"], entries[n]["h"] = dims
                except OSError as e:
                    return {"error": str(e)}
            for u in state["urls"]:
                ext = os.path.splitext(u["url"].split("?")[0])[1].lower()
                entries[u["name"]] = {
                    "name": u["name"],
                    "size": 0,
                    "kind": "image" if ext in IMAGE_EXTS else "video",
                    "remote": True,
                    "src": u["url"],
                }
            items = []
            for n in medialist.sort_names(list(entries), state):
                it = entries[n]
                it["enabled"] = n not in disabled
                it["playing"] = bool(
                    current
                    and (os.path.basename(current) == n or current == it["src"])
                )
                it["duration"] = state["durations"].get(n)
                it["schedule"] = state["schedules"].get(n)
                items.append(it)
            return {"items": items, "shuffle": state["shuffle"]}

        def _key_ok(self, u):
            q = urllib.parse.parse_qs(u.query)
            return hmac.compare_digest((q.get("k") or [""])[0], state.token)

        # -- optional local lock ------------------------------------------
        # When the user enables the screen lock, the local admin surface gets
        # a password gate too. Stateless cookie: sha256(password|boot-token) —
        # sessions die on Kodi restart, which suits a display appliance.

        def _lock_on(self):
            # A set admin password gates the web admin on its own —
            # local_lock only controls the on-TV exit prompt.
            if controller.config.get("local_password"):
                return True
            # Degraded mode: the conductor's own verifier gates the admin.
            return self._degraded() and bool(self._server_hash())

        def _lock_secret(self):
            raw = f"{controller.config.get('local_password')}|{state.token}"
            return hashlib.sha256(raw.encode()).hexdigest()[:40]

        def _server_hash(self):
            """The conductor's admin verifier pushed over the socket —
            scrypt 'salt_hex:hash_hex', never a raw password."""
            try:
                with open(os.path.join(PROFILE, "server_auth.hash")) as f:
                    return f.read().strip()
            except OSError:
                return ""

        def _server_secret(self):
            raw = f"{self._server_hash()}|{state.token}"
            return hashlib.sha256(raw.encode()).hexdigest()[:40]

        def _server_password_ok(self, pw):
            h = self._server_hash()
            if ":" not in h:
                return False
            salt_hex, want_hex = h.split(":", 1)
            try:
                # Match better-auth's scrypt exactly: Node treats a string
                # salt as the UTF-8 bytes of the hex *text* (not decoded
                # hex), normalizes the password NFKC, and needs the raised
                # maxmem for N=16384,r=16 (~33MB working set).
                import unicodedata
                got = hashlib.scrypt(
                    unicodedata.normalize("NFKC", pw).encode(),
                    salt=salt_hex.encode(),
                    n=16384, r=16, p=1, dklen=64,
                    maxmem=128 * 16384 * 16 * 2,
                ).hex()
            except (ValueError, OSError):
                return False
            return hmac.compare_digest(got, want_hex)

        def _authed(self):
            secrets_ok = {self._lock_secret()}
            if self._degraded() and self._server_hash():
                secrets_ok.add(self._server_secret())
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "ss_lock" and any(
                    hmac.compare_digest(v, s) for s in secrets_ok
                ):
                    return True
            return False

        # Paths that stay reachable while locked — the login page itself, its
        # assets, and the token-gated setup/pairing flows.
        _LOCK_OPEN = {
            "/login", "/api/login", "/api/logout",
            "/setup", "/setup.html", "/pair",
            "/favicon.ico", "/favicon.png", "/logo.png", "/wordmark.png",
            "/api/info",
        }

        def _lock_open(self, u):
            return u.path in self._LOCK_OPEN or u.path.startswith("/api/setup/")

        def _lock_required(self, u):
            if u.path.startswith("/api/"):
                self._json({"error": "authentication required"}, 401)
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()

        # -- routes -------------------------------------------------------

        def _cross_site(self):
            """CSRF guard for state-changing requests: browsers attach
            Origin (and Sec-Fetch-Site) to cross-site POST/DELETE, so a page
            elsewhere on the LAN can't drive this admin. Non-browser clients
            send neither and pass."""
            if (self.headers.get("Sec-Fetch-Site") or "") == "cross-site":
                return True
            origin = self.headers.get("Origin")
            if not origin:
                return False
            host = urllib.parse.urlparse(origin).netloc
            return host.lower() != (self.headers.get("Host") or "").lower()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if self._managed() and self._managed_block():
                return
            if (
                self._lock_on()
                and not self._lock_open(u)
                and not self._authed()
            ):
                return self._lock_required(u)
            if u.path == "/login":
                self._static(os.path.join(WWW_DIR, "login.html"), "text/html")
            elif u.path in ("/", "/admin.html"):
                self._static(os.path.join(WWW_DIR, "admin.html"), "text/html")
            elif u.path in ("/setup", "/setup.html"):
                if not self._key_ok(u):
                    self._json({"error": "open this page via the on-screen QR code"}, 403)
                    return
                self._static(os.path.join(WWW_DIR, "setup.html"), "text/html")
            elif u.path == "/icons.js":
                self._static(os.path.join(WWW_DIR, "icons.js"), "text/javascript")
            elif u.path in ("/favicon.ico", "/favicon.png", "/logo.png", "/wordmark.png"):
                ctype = "image/x-icon" if u.path.endswith(".ico") else "image/png"
                self._static(os.path.join(WWW_DIR, u.path.lstrip("/")), ctype)
            elif u.path == "/api/setup/state":
                if not self._key_ok(u):
                    self._json({"error": "bad key"}, 403)
                    return
                self._json(
                    {
                        "setup_complete": controller.config["setup_complete"],
                        "settings": {
                            sid: (
                                _addon_read(addon, sid, k)
                                if sid != "local_password"
                                else ""  # never echo a stored password
                            )
                            for sid, k in SETUP_SETTINGS.items()
                        },
                    }
                )
            elif u.path == "/api/setup/ping":
                # Heartbeat from an open /setup page — keeps the TV wizard in
                # its "setup in progress" state while the phone is working.
                if self._key_ok(u):
                    state.last_ping = time.time()
                    self._json({"ok": True})
                else:
                    self._json({"error": "bad key"}, 403)
            elif u.path == "/pair":
                # GET so a scanning server page can simply open this URL in a
                # new tab — no CORS. s= may be any full URL, so a cloud-hosted
                # server works too. Disabled once setup is complete so a
                # configured display can't be re-paired off the LAN.
                q = urllib.parse.parse_qs(u.query)
                # A server-mode display set up by hand has no token yet and
                # still needs to pair; anything else that's set up is locked.
                if controller.config["setup_complete"] and (
                    controller.config.get("player_mode") != 1
                    or controller.config.get("server_auth_token")
                ):
                    self._pair_page(
                        "Pairing disabled",
                        "Setup is already complete on this display. To pair it "
                        "to a different server, reset it in addon settings first.",
                    )
                    return
                if not hmac.compare_digest((q.get("t") or [""])[0], state.token):
                    self._pair_page("Pairing failed", "Open the on-screen QR code link.")
                    return
                server_url = (q.get("s") or [""])[0].strip()
                code = (q.get("a") or [""])[0].strip()
                if not server_url:
                    self._pair_page("Pairing failed", "Missing server URL.")
                    return
                if not code:
                    self._pair_page(
                        "Pairing failed",
                        "Missing pairing code — get one from the server's "
                        "Pair page.",
                    )
                    return
                if HOME.getProperty(PROP_PAIR_URL) or controller._pair_dialog:
                    self._pair_page(
                        "Pairing already pending",
                        "A pairing request is already waiting on the display — "
                        "confirm or cancel it there first.",
                    )
                    return
                # REQUIRED identity probe — the offer is refused unless the
                # target reports itself as a Screensemble server, and both
                # sides' version requirements are checked before the TV is
                # even asked.
                info = fetch_server_info(server_url)
                if info.get("product") != "screensemble-server":
                    self._pair_page(
                        "Pairing failed",
                        f"{server_url} did not identify as a Screensemble "
                        "server (GET /api/info missing or wrong product) — "
                        "check the URL.",
                    )
                    return
                if not version_at_least(info.get("version"), MIN_SERVER_VERSION):
                    self._pair_page(
                        "Pairing failed",
                        f"Server is v{info.get('version') or '?'} but this "
                        f"display needs v{MIN_SERVER_VERSION}+ — update the "
                        "server.",
                    )
                    return
                min_client = info.get("min_client_version")
                our_version = addon.getAddonInfo("version")
                if min_client and not version_at_least(our_version, min_client):
                    self._pair_page(
                        "Pairing failed",
                        f"Server requires client v{min_client}+ but this "
                        f"addon is v{our_version} — update the addon.",
                    )
                    return
                # Client reports itself to the server with the one-time code
                # the server issued — the server checks it and its own
                # minimum version before the TV is ever asked. The device
                # token is only issued after the on-TV confirm.
                from link import hardware_id

                resp, err = post_json(
                    server_url,
                    "/api/pair/request",
                    {
                        "product": "screensemble-client",
                        "version": our_version,
                        "name": addon.getSetting("device_name"),
                        "client_url": state.admin_url().rstrip("/"),
                        "hardware_id": hardware_id(),
                        "code": code,
                    },
                )
                if err or not resp or not resp.get("accepted"):
                    reason = (
                        (resp or {}).get("reason")
                        or err
                        or "server did not accept the pairing"
                    )
                    self._pair_page("Pairing rejected by server", str(reason))
                    return
                # Both sides accepted — the TV confirms with the human now.
                HOME.setProperty(PROP_PAIR_ID, str(resp.get("pairing_id") or ""))
                HOME.setProperty(PROP_PAIR_INFO, json.dumps(info))
                HOME.setProperty(PROP_PAIR_URL, server_url)
                log(f"pair offer from {server_url} accepted — awaiting on-TV confirm")
                self._pair_page(
                    "Pairing request sent",
                    "The display is asking for confirmation — press Pair on "
                    "the display's remote to finish.",
                    close=True,
                )
            elif u.path == "/api/info":
                # Client identity — servers/dashboards can probe and gate on
                # this the same way we gate /pair on the server's version.
                self._json(
                    {
                        "product": "screensemble-client",
                        "version": addon.getAddonInfo("version"),
                        "name": controller.config.get("device_name") or "",
                        "mode": (
                            "local" if controller.config["player_mode"] == 0 else "server"
                        ),
                    }
                )
            elif u.path == "/api/status":
                try:
                    current = controller.current_name()
                except Exception:
                    current = ""
                free_gb = None
                try:
                    import shutil
                    free_gb = round(
                        shutil.disk_usage(
                            self._media_dir() or "C:\\"
                        ).free / (1024 ** 3), 1
                    )
                except Exception:
                    pass
                self._json(
                    {
                        "mode": "local" if controller.config["player_mode"] == 0 else "server",
                        "setup_complete": controller.config["setup_complete"],
                        "playing": controller.player.playing,
                        "current": current,
                        "free_gb": free_gb,
                        "maintenance": controller.maintenance,
                        "degraded": self._degraded(),
                        "paused": xbmc.getCondVisibility("Player.Paused"),
                        "standby_window": controller.window_open,
                        "videos": len(controller.video_files),
                        "images": len(controller.slideshow_files),
                        "prepared": controller.prepared,
                    }
                )
            elif u.path == "/api/settings":
                self._json(
                    {
                        sid: (
                            _addon_read(addon, sid, k)
                            if sid != "local_password"
                            else ""  # never echo a stored password
                        )
                        for sid, k in EDITABLE_SETTINGS.items()
                    }
                )
            elif u.path == "/api/browse":
                # Directory picker for folder fields. Browses via Kodi's
                # in-process JSON-RPC (Files.*) so Kodi's web server does
                # not need to be enabled. Key-gated until setup completes;
                # open afterwards, matching the rest of the admin.
                if (not controller.config["setup_complete"]
                        and not self._key_ok(u)):
                    self._json({"error": "bad key"}, 403)
                    return
                path = urllib.parse.parse_qs(u.query).get("path", [""])[0]
                entries, error = [], None
                try:
                    if not path:
                        r = json.loads(xbmc.executeJSONRPC(json.dumps({
                            "jsonrpc": "2.0", "id": 1,
                            "method": "Files.GetSources",
                            "params": {"media": "files"},
                        })))
                        for s in r.get("result", {}).get("sources", []):
                            entries.append({
                                "name": s.get("label") or s.get("file"),
                                "path": s.get("file"),
                            })
                        # GetSources only lists configured shares — add the
                        # real filesystem roots too so the picker is useful
                        # on a fresh install. Paths picked from these stay
                        # real OS paths (the media scan uses os.listdir).
                        seen = {e["path"] for e in entries}

                        def add(name, p):
                            if p and p not in seen:
                                seen.add(p)
                                entries.append({"name": name, "path": p})

                        add("Kodi data (special://home)",
                            xbmcvfs.translatePath("special://home"))
                        if sys.platform == "win32":
                            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                                d = f"{letter}:\\"
                                if os.path.isdir(d):
                                    add(f"Local disk ({letter}:)", d)
                        else:
                            for p in ("/storage/emulated/0", "/sdcard",
                                      "/mnt", "/media", "/"):
                                if os.path.isdir(p):
                                    add(p, p)
                        if not entries:
                            path = "special://"
                    if path:
                        r = json.loads(xbmc.executeJSONRPC(json.dumps({
                            "jsonrpc": "2.0", "id": 1,
                            "method": "Files.GetDirectory",
                            "params": {
                                "directory": path, "media": "files",
                                "sort": {"method": "label"},
                            },
                        })))
                        if "error" in r:
                            error = r["error"].get("message", "cannot list folder")
                        else:
                            entries = [
                                {"name": f.get("label") or f.get("file"),
                                 "path": f.get("file")}
                                for f in r.get("result", {}).get("files", [])
                                if f.get("filetype") == "directory"
                            ]
                except Exception as e:
                    error = str(e)
                out = {"path": path, "entries": entries}
                if error:
                    out["error"] = error
                self._json(out)
            elif u.path == "/api/media":
                result = self._media_list()
                self._json(result if isinstance(result, dict) else {"items": result})
            elif u.path.startswith("/media/"):
                folder = self._media_dir()
                name = os.path.basename(
                    urllib.parse.unquote(u.path[len("/media/"):])
                )
                full = os.path.join(folder, name) if folder else ""
                if not full or not os.path.isfile(full):
                    self._json({"error": "not found"}, 404)
                    return
                ctype = {
                    ".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".gif": "image/gif",
                    ".bmp": "image/bmp", ".webp": "image/webp",
                    ".mp4": "video/mp4", ".webm": "video/webm",
                }.get(os.path.splitext(name)[1].lower(),
                      "application/octet-stream")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(os.path.getsize(full)))
                    self.end_headers()
                    with open(full, "rb") as f:
                        while True:
                            chunk = f.read(262144)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except OSError as e:
                    self._json({"error": str(e)}, 500)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if self._cross_site():
                self._json({"error": "cross-site request refused"}, 403)
                return
            if self._managed() and self._managed_block():
                return
            if (
                self._lock_on()
                and not self._lock_open(u)
                and not self._authed()
            ):
                return self._lock_required(u)
            q = urllib.parse.parse_qs(u.query)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            if u.path == "/api/login":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                user = str(data.get("username") or "").strip().lower()
                pw = str(data.get("password") or "")
                user_ok = hmac.compare_digest(user, "admin")
                local_ok = user_ok and hmac.compare_digest(
                    pw.encode(),
                    str(controller.config.get("local_password") or "").encode(),
                )
                server_ok = (
                    user_ok
                    and self._degraded()
                    and self._server_password_ok(pw)
                )
                if self._lock_on() and (local_ok or server_ok):
                    secret = (
                        self._lock_secret()
                        if local_ok
                        else self._server_secret()
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header(
                        "Set-Cookie",
                        f"ss_lock={secret}; Path=/; HttpOnly; SameSite=Lax",
                    )
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')
                else:
                    self._json(
                        {"error": "incorrect username or password"}, 401
                    )
            elif u.path == "/api/logout":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Set-Cookie", "ss_lock=; Path=/; HttpOnly; Max-Age=0"
                )
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            elif u.path == "/api/setup":
                # Phone setup wizard writes the full SETUP_SETTINGS set,
                # including player_mode/setup_complete — token required.
                if not self._key_ok(u):
                    self._json({"error": "bad key"}, 403)
                    return
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    self._json({"error": "bad json"}, 400)
                    return
                saved = {}
                for sid, kind in SETUP_SETTINGS.items():
                    if sid in data:
                        try:
                            _addon_write(addon, sid, kind, data[sid])
                        except ValueError as e:
                            self._json({"error": str(e)}, 400)
                            return
                        saved[sid] = _addon_read(addon, sid, kind)
                state.last_ping = 0.0
                HOME.clearProperty(PROP_WEB_SETUP)
                on_settings_saved()
                log(f"web setup saved: {sorted(saved)}")
                self._json({"saved": saved})
            elif u.path == "/api/settings":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    self._json({"error": "bad json"}, 400)
                    return
                saved = {}
                for sid, kind in EDITABLE_SETTINGS.items():
                    if sid in data:
                        try:
                            _addon_write(addon, sid, kind, data[sid])
                        except ValueError as e:
                            self._json({"error": str(e)}, 400)
                            return
                        saved[sid] = _addon_read(addon, sid, kind)
                on_settings_saved()
                self._json({"saved": saved})
            elif u.path == "/api/media/order":
                # Persist play order; takes effect immediately.
                try:
                    order = json.loads(body or b"{}").get("order", [])
                except ValueError:
                    self._json({"error": "bad json"}, 400)
                    return
                ms = medialist.load_state()
                ms["order"] = [str(n) for n in order]
                medialist.save_state(ms)
                controller.defer_content_refresh()
                log(f"media order saved ({len(ms['order'])} entries)")
                self._json({"ok": True})
            elif u.path == "/api/media/toggle":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    self._json({"error": "bad json"}, 400)
                    return
                name = os.path.basename(str(data.get("name", "")))
                if not name:
                    self._json({"error": "no filename"}, 400)
                    return
                ms = medialist.load_state()
                disabled = set(ms["disabled"])
                if data.get("enabled"):
                    disabled.discard(name)
                else:
                    disabled.add(name)
                ms["disabled"] = sorted(disabled)
                medialist.save_state(ms)
                controller.defer_content_refresh()
                log(f"media {'enabled' if data.get('enabled') else 'disabled'}: {name}")
                self._json({"ok": True, "enabled": bool(data.get("enabled"))})
            elif u.path == "/api/media/url":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    self._json({"error": "bad json"}, 400)
                    return
                name = str(data.get("name", "")).strip()
                url = str(data.get("url", "")).strip()
                if not name or not url.startswith(("http://", "https://", "rtsp://", "rtmp://")):
                    self._json({"error": "name and http(s)/rtsp URL required"}, 400)
                    return
                ms = medialist.load_state()
                for u in ms["urls"]:
                    if u["name"] == name:
                        u["url"] = url
                        break
                else:
                    ms["urls"].append({"name": name, "url": url})
                medialist.save_state(ms)
                controller.defer_content_refresh()
                log(f"media url saved: {name} -> {url}")
                self._json({"ok": True})
            elif u.path == "/api/media/duration":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    self._json({"error": "bad json"}, 400)
                    return
                name = str(data.get("name", ""))
                secs = data.get("seconds")
                ms = medialist.load_state()
                try:
                    secs = float(secs)
                    if secs > 0:
                        ms["durations"][name] = secs
                    else:
                        ms["durations"].pop(name, None)
                except (TypeError, ValueError):
                    ms["durations"].pop(name, None)
                medialist.save_state(ms)
                controller.defer_content_refresh()
                self._json({"ok": True})
            elif u.path == "/api/media/schedule":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    self._json({"error": "bad json"}, 400)
                    return
                name = str(data.get("name", ""))
                sched = data.get("schedule")
                ms = medialist.load_state()
                if isinstance(sched, dict) and any(sched.values()):
                    ms["schedules"][name] = sched
                else:
                    ms["schedules"].pop(name, None)
                medialist.save_state(ms)
                controller.defer_content_refresh()
                self._json({"ok": True})
            elif u.path == "/api/media/shuffle":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    self._json({"error": "bad json"}, 400)
                    return
                ms = medialist.load_state()
                ms["shuffle"] = bool(data.get("shuffle"))
                medialist.save_state(ms)
                controller.defer_content_refresh()
                self._json({"ok": True, "shuffle": ms["shuffle"]})
            elif u.path == "/api/upload":
                name = os.path.basename((q.get("name") or [""])[0])
                folder = self._media_dir()
                if not name or folder is None:
                    self._json({"error": "no filename or remote media folder"}, 400)
                    return
                try:
                    with open(os.path.join(folder, name), "wb") as f:
                        f.write(body)
                except OSError as e:
                    self._json({"error": str(e)}, 500)
                    return
                controller.defer_content_refresh()
                log(f"web upload: {name} ({len(body)} bytes)")
                self._json({"ok": True, "name": name, "size": len(body)})
            elif u.path == "/api/command":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                self._run_command(data.get("cmd", ""), data)
            else:
                self._json({"error": "not found"}, 404)

        def do_DELETE(self):
            u = urllib.parse.urlparse(self.path)
            if self._cross_site():
                self._json({"error": "cross-site request refused"}, 403)
                return
            if self._managed() and self._managed_block():
                return
            if (
                self._lock_on()
                and not self._lock_open(u)
                and not self._authed()
            ):
                return self._lock_required(u)
            q = urllib.parse.parse_qs(u.query)
            if u.path != "/api/media":
                self._json({"error": "not found"}, 404)
                return
            name = os.path.basename((q.get("name") or [""])[0])
            folder = self._media_dir()
            if not name:
                self._json({"error": "no filename"}, 400)
                return
            if folder is None and not any(
                u["name"] == name
                for u in medialist.load_state()["urls"]
            ):
                self._json({"error": "media folder is a remote path"}, 400)
                return
            state = medialist.load_state()
            url_entry = next(
                (u for u in state["urls"] if u["name"] == name), None
            )
            if url_entry is not None:
                state["urls"].remove(url_entry)
            elif folder is not None:
                try:
                    os.remove(os.path.join(folder, name))
                except OSError as e:
                    self._json({"error": str(e)}, 500)
                    return
            changed = False
            for key in ("order", "disabled"):
                if name in state[key]:
                    state[key].remove(name)
                    changed = True
            for key in ("durations", "schedules"):
                if name in state[key]:
                    del state[key][name]
                    changed = True
            if changed or url_entry is not None:
                medialist.save_state(state)
            controller.defer_content_refresh()
            log(f"web delete: {name}")
            self._json({"ok": True})

        def _run_command(self, cmd, data=None):
            if cmd == "play_item":
                name = (data or {}).get("name", "")
                if controller.maintenance:
                    controller.resume_from_menu(with_content=False)
                controller.play_item(name)
            elif cmd == "stop":
                if controller.player.isPlaying():
                    controller.player.stop()
            elif cmd == "restart":
                controller.rescan_media()
                controller.request_content()
            elif cmd in ("prepare", "arm"):
                controller.prepare()
            elif cmd in ("perform", "go"):
                controller.perform()
            elif cmd == "pause":
                controller.player.pause()
            elif cmd in ("next", "prev"):
                if controller.maintenance:
                    controller.resume_from_menu(with_content=False)
                controller._advance_seq(1 if cmd == "next" else -1)
            elif cmd == "resume":
                controller.resume_from_menu()
            elif cmd == "exit_ui":
                controller.request_exit()
            elif cmd == "restart_app":
                restart_app()
            elif cmd == "reboot":
                xbmc.executebuiltin("Reboot")
            else:
                self._json({"error": "unknown command"}, 400)
                return
            log(f"web command: {cmd}")
            self._json({"ok": True})

    return AdminHandler


class AdminServer:
    """ThreadingHTTPServer wrapper. Owns the per-boot setup token, the
    /setup heartbeat (drives the TV's "setup in progress" state), and the
    pending pair offer. stop() shuts everything down on service exit."""

    def __init__(self, controller, addon, on_settings_saved, port=8282):
        self.port = port
        self.token = secrets.token_hex(4)
        self.last_ping = 0.0      # last /api/setup/ping timestamp
        handler = make_handler(controller, addon, on_settings_saved, self)
        self._server = ThreadingHTTPServer(("0.0.0.0", port), handler)
        self._server.daemon_threads = True
        self._thread = None
        self._watchdog = None
        self._stop = threading.Event()

    def admin_url(self):
        ip = lan_ip()
        return f"http://{ip}:{self.port}/" if ip else ""

    def _publish_urls(self):
        ip = lan_ip()
        if not ip:
            return False
        HOME.setProperty(PROP_ADMIN_URL, f"http://{ip}:{self.port}/")
        HOME.setProperty(
            PROP_SETUP_URL, f"http://{ip}:{self.port}/setup?k={self.token}"
        )
        return True

    def start(self):
        self._publish_urls()  # may fail pre-network; watchdog retries
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._watchdog = threading.Thread(target=self._setup_watch, daemon=True)
        self._watchdog.start()
        log(f"web admin listening on 0.0.0.0:{self.port}")

    def _setup_watch(self):
        """While a phone has /setup open (heartbeat pings), flag the home
        window so the on-screen wizard shows 'setup in progress'."""
        while not self._stop.wait(1.0):
            # The IP may not have been known at service start — republish
            # the QR/admin URLs once it is.
            if not HOME.getProperty(PROP_SETUP_URL):
                self._publish_urls()
            active = time.time() - self.last_ping < WEB_SETUP_STALE
            cur = HOME.getProperty(PROP_WEB_SETUP)
            if active and cur != "1":
                HOME.setProperty(PROP_WEB_SETUP, "1")
            elif not active and cur:
                HOME.clearProperty(PROP_WEB_SETUP)

    def stop(self):
        self._stop.set()
        HOME.clearProperty(PROP_SETUP_URL)
        HOME.clearProperty(PROP_ADMIN_URL)
        HOME.clearProperty(PROP_WEB_SETUP)
        HOME.clearProperty(PROP_PAIR_URL)
        HOME.clearProperty(PROP_PAIR_INFO)
        HOME.clearProperty(PROP_PAIR_ID)
        self._server.shutdown()
        self._server.server_close()
