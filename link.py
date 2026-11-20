"""Server link — managed-mode connection back to the Screensemble server.

The display dials OUT over a persistent WebSocket (ws:// or wss://) so a
server anywhere — LAN or internet — can reach it with no inbound ports
on the display's network. One socket carries enrollment, periodic status
reports, and server→display commands (restart, arm/go, play_item,
get_media). Reconnects with backoff; local playback is unaffected while
the link is down.
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import xbmc
import xbmcaddon
import xbmcgui

import medialist
from idle import PROFILE, resolve_path, restart_app

LINK_PROPERTY = "screensemble.link"


def log(msg):
    xbmc.log(f"[screensemble] link: {msg}", xbmc.LOGINFO)


def hardware_id():
    """Stable device identity — a salted hash of the primary MAC so the
    server can rebind this display to the same registry entry across
    addon reinstalls/resets. Falls back to a persisted random id."""
    try:
        mac = (xbmc.getInfoLabel("Network.MacAddress") or "").strip()
        if mac:
            import hashlib
            return hashlib.sha256(
                f"screensemble:{mac.lower()}".encode()
            ).hexdigest()[:12]
    except Exception:
        pass
    path = os.path.join(PROFILE, "device.id")
    try:
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
        import uuid
        did = uuid.uuid4().hex[:12]
        with open(path, "w") as f:
            f.write(did)
        return did
    except Exception:
        return ""


# -- minimal RFC6455 client ----------------------------------------------
# Kodi ships no websocket lib. Text frames only; client frames are masked
# per spec; server frames arrive unmasked. Fragmentation is ignored —
# our messages are small JSON.


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """pull_file must stay on the paired server — never follow a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, "redirect refused", headers, fp)


_NO_REDIRECT = urllib.request.build_opener(_RefuseRedirect)

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 32 * 1024 * 1024  # server messages are small JSON commands


def _ws_connect(url, timeout=8.0):
    u = urllib.parse.urlparse(url if "://" in url else f"ws://{url}")
    host = u.hostname
    port = u.port or (443 if u.scheme == "wss" else 80)
    path = u.path or "/"
    raw = socket.create_connection((host, port), timeout)
    sock = (
        ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        if u.scheme == "wss"
        else raw
    )
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    resp = b""
    # Byte-at-a-time so a frame sent right after the 101 stays in the
    # socket for _recv instead of being swallowed with the headers.
    while not resp.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise OSError("handshake closed")
        resp += chunk
    head = resp[:-4]
    lines = head.split(b"\r\n")
    status = lines[0]
    if b" 101" not in status:
        raise OSError(f"handshake refused: {status.decode('utf-8','replace')}")
    headers = {}
    for line in lines[1:]:
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    want = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode()).digest()
    ).decode()
    if headers.get("sec-websocket-accept") != want:
        raise OSError("handshake refused: bad Sec-WebSocket-Accept")
    return sock


def _send_frame(sock, opcode, data=b""):
    mask = os.urandom(4)
    header = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + mask + masked)


def _send(sock, text):
    _send_frame(sock, 0x1, text.encode())


def _recv(sock):
    """One frame -> (opcode, payload). Raises OSError on close/EOF.
    socket.timeout only escapes while idle between frames — once a frame
    has started it is read to the end so the stream never desyncs."""
    started = [False]

    def read(n):
        buf = b""
        stalls = 0
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except socket.timeout:
                if not started[0] and not buf:
                    raise
                stalls += 1
                if stalls > 30:
                    raise OSError("frame stalled")
                continue
            if not chunk:
                raise OSError("closed")
            started[0] = True
            buf += chunk
        return buf

    b1, b2 = read(2)
    opcode = b1 & 0x0F
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", read(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", read(8))[0]
    if n > MAX_FRAME:
        raise OSError(f"frame too large ({n} bytes)")
    if b2 & 0x80:
        mask = read(4)
        data = bytes(x ^ mask[i % 4] for i, x in enumerate(read(n)))
    else:
        data = read(n)
    return opcode, data


# -- link -----------------------------------------------------------------


class ServerLink:
    """Background thread owning the ws:// connection to the server."""

    STATUS_EVERY = 5.0

    def __init__(self, controller):
        self.c = controller
        self.addon = xbmcaddon.Addon()
        self._stop = False
        self.sock = None
        self._send_lock = threading.Lock()
        self._auth_rejected = 0.0   # last hello the server refused

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass

    def _ws_url(self):
        addr = (self.c.config.get("server_address") or "").strip()
        if not addr:
            return None
        scheme = "wss" if addr.startswith("https://") else "ws"
        return f"{scheme}://{addr.split('://', 1)[-1].rstrip('/')}/ws/player"

    def _status(self):
        free_gb = None
        try:
            import shutil

            folder = resolve_path(
                self.c.config.get("local_media_folder")
                or medialist.DEFAULT_MEDIA_DIR
            )
            free_gb = round(
                shutil.disk_usage(folder).free / (1024 ** 3), 1
            )
        except Exception:
            pass
        return {
            "mode": "server",
            "setup_complete": self.c.config.get("setup_complete"),
            "current": self.c.current_name(),
            "playing": self.c.player.playing,
            "armed": self.c.armed,
            "maintenance": self.c.maintenance,
            "idle_window": self.c.window_open,
            "items": len(self.c.sequence),
            "free_gb": free_gb,
            "pending_update": self.c._pending_refresh,
        }

    def _media_items(self):
        state = getattr(self.c, "_media_state", None) or medialist.load_state()
        current = self.c.current_name()
        # The catalog keeps disabled and scheduled-off items — the sequence
        # alone only knows what is playing right now.
        catalog = getattr(self.c, "catalog", None) or [
            {"name": s["name"], "kind": s["kind"],
             "remote": s.get("remote", False), "size": None}
            for s in self.c.sequence
        ]
        in_seq = {s["name"] for s in self.c.sequence}
        return [
            {
                "name": s["name"],
                "kind": s["kind"],
                "remote": s.get("remote", False),
                "size": s.get("size"),
                "enabled": s["name"] not in state["disabled"],
                "active": s["name"] in in_seq,
                "duration": state["durations"].get(s["name"]),
                "schedule": state["schedules"].get(s["name"]),
                "playing": s["name"] == current,
            }
            for s in catalog
        ]

    def _reply(self, obj):
        try:
            with self._send_lock:
                _send(self.sock, json.dumps(obj))
        except OSError:
            pass

    def _on_message(self, msg):
        t = msg.get("type")
        if t == "hello_ack":
            if msg.get("error"):
                # Auth rejected — the registry entry has a different
                # token bound. Re-pair the display (or delete the stale
                # server-side entry) rather than retrying forever.
                log(f"link: rejected by server: {msg['error']}")
                self._auth_rejected = time.time()
                return
            # Server-assigned id (manual setup without a pairing flow) —
            # remember it so reconnects bind the same registry entry.
            pid = msg.get("player_id")
            if pid and not self.addon.getSetting("paired_id"):
                self.addon.setSetting("paired_id", pid)
            return
        if t == "get_media":
            self._reply(
                {"type": "media", "id": msg.get("id"),
                 "items": self._media_items()}
            )
            return
        if t == "media_op":
            self._media_op(msg)
            return
        if t == "get_settings":
            self._reply(
                {"type": "settings", "id": msg.get("id"),
                 "settings": self._read_settings()}
            )
            return
        if t == "set_setting":
            self._write_settings(msg)
            return
        if t == "pull_file":
            threading.Thread(
                target=self._pull_file, args=(msg,), daemon=True
            ).start()
            return
        if t == "get_file":
            self._send_file(msg)
            return
        if t != "command":
            return
        cmd, rid = msg.get("cmd"), msg.get("id")
        err = None
        try:
            if cmd == "arm":
                self.c.arm()
            elif cmd == "go":
                self.c.go()
            elif cmd == "restart":
                self.c.rescan_media()
                self.c.request_content()
            elif cmd == "resume":
                self.c.resume_from_menu()
            elif cmd == "stop":
                self.c.armed = False
                if self.c.player.isPlaying():
                    self.c.player.stop()
            elif cmd == "play_item":
                self.c.play_item(msg.get("name") or "")
            elif cmd == "restart_app":
                restart_app()
            elif cmd == "reboot":
                xbmc.executebuiltin("Reboot")
            else:
                err = f"unknown command {cmd}"
        except Exception as e:
            err = str(e)
        self._reply(
            {"type": "command_result", "id": rid,
             "ok": err is None, "error": err}
        )
        self._reply({"type": "status", "status": self._status()})

    # -- server-proxied admin API -----------------------------------------

    def _media_dir(self):
        # Pushed media lands where the managed playlist scans — the
        # server cache folder (or the built-in dir when unset).
        if self.c.config.get("player_mode") == 1:
            folder = self.c.config.get("server_media_dir")
        else:
            folder = self.c.config.get("local_media_folder")
        return resolve_path(folder or medialist.DEFAULT_MEDIA_DIR)

    def _read_settings(self):
        from web import EDITABLE_SETTINGS, _addon_read
        return {
            sid: _addon_read(self.addon, sid, k)
            for sid, k in EDITABLE_SETTINGS.items()
        }

    def _write_settings(self, msg):
        from web import EDITABLE_SETTINGS, _addon_read, _addon_write
        saved = {}
        for sid, kind in EDITABLE_SETTINGS.items():
            data = msg.get("settings") or {}
            if sid in data:
                _addon_write(self.addon, sid, kind, data[sid])
                saved[sid] = _addon_read(self.addon, sid, kind)
        xbmcgui.Window(10000).setProperty("screensemble.command", "reload")
        self._reply(
            {"type": "settings_saved", "id": msg.get("id"),
             "saved": saved}
        )

    def _media_op(self, msg):
        """Playlist mutations — same semantics as the local admin API."""
        op = msg.get("op")
        rid = msg.get("id")
        name = os.path.basename(str(msg.get("name") or ""))
        err = None
        try:
            state = medialist.load_state()
            if op == "order":
                state["order"] = [str(n) for n in msg.get("order", [])]
            elif op == "toggle":
                disabled = set(state["disabled"])
                if msg.get("enabled"):
                    disabled.discard(name)
                else:
                    disabled.add(name)
                state["disabled"] = sorted(disabled)
            elif op == "url":
                uname = str(msg.get("url_name") or name).strip()
                url = str(msg.get("url") or "").strip()
                if not uname or not url.startswith(
                    ("http://", "https://", "rtsp://", "rtmp://")
                ):
                    raise ValueError("name and stream URL required")
                for u in state["urls"]:
                    if u["name"] == uname:
                        u["url"] = url
                        break
                else:
                    state["urls"].append({"name": uname, "url": url})
            elif op == "duration":
                try:
                    secs = float(msg.get("seconds"))
                    if secs > 0:
                        state["durations"][name] = secs
                    else:
                        state["durations"].pop(name, None)
                except (TypeError, ValueError):
                    state["durations"].pop(name, None)
            elif op == "schedule":
                sched = msg.get("schedule")
                if isinstance(sched, dict) and any(sched.values()):
                    state["schedules"][name] = sched
                else:
                    state["schedules"].pop(name, None)
            elif op == "shuffle":
                state["shuffle"] = bool(msg.get("shuffle"))
            elif op == "delete":
                url_entry = next(
                    (u for u in state["urls"] if u["name"] == name), None
                )
                if url_entry is not None:
                    state["urls"].remove(url_entry)
                else:
                    folder = self._media_dir()
                    os.remove(os.path.join(folder, name))
                for key in ("order", "disabled"):
                    state[key] = [n for n in state[key] if n != name]
                for key in ("durations", "schedules"):
                    state[key].pop(name, None)
            else:
                raise ValueError(f"unknown op {op}")
            medialist.save_state(state)
            # Apply at the next item boundary — the current piece keeps
            # playing so remote edits never cut it short.
            self.c.defer_content_refresh()
        except Exception as e:
            err = str(e)
        self._reply(
            {"type": "media_op_result", "id": rid,
             "ok": err is None, "error": err}
        )

    def _pull_file(self, msg):
        """Server-pushed media: download from the server over plain HTTP
        — still outbound-only for the display."""
        rid = msg.get("id")
        name = os.path.basename(str(msg.get("name") or "file.bin"))
        url = msg.get("url")

        def progress(received, total, state="receiving", error=None):
            self._reply(
                {"type": "push_progress", "job": rid, "name": name,
                 "received": received, "total": total,
                 "state": state, "error": error}
            )

        self.c.pull_started()
        dest = None
        try:
            if not url:
                raise ValueError("no url")
            # Only fetch from the paired server over HTTP(S) — never file://
            # or another host a forged message names.
            pu = urllib.parse.urlparse(str(url))
            server = urllib.parse.urlparse(self._ws_url() or "")
            if pu.scheme not in ("http", "https") or (
                (pu.hostname or "").lower() != (server.hostname or "").lower()
            ):
                raise ValueError("pull url is not on the paired server")
            folder = self._media_dir()
            if not folder or "://" in folder:
                raise ValueError("media folder is not local")
            os.makedirs(folder, exist_ok=True)
            with _NO_REDIRECT.open(url, timeout=30) as r:
                total = int(r.headers.get("Content-Length") or 0)
                dest = os.path.join(folder, name + ".part")
                got = last = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if got - last > 1048576:
                            last = got
                            progress(got, total)
            # Verify integrity before activating — a truncated download
            # never replaces a good copy.
            want = (msg.get("sha256") or "").lower()
            if want:
                h = hashlib.sha256()
                with open(dest, "rb") as f:
                    for chunk in iter(lambda: f.read(1048576), b""):
                        h.update(chunk)
                if h.hexdigest() != want:
                    raise ValueError("sha256 mismatch")
            os.replace(dest, os.path.join(folder, name))
            dest = None
            progress(got, total, "done")
            log(f"pulled {name}: {got} bytes")
        except Exception as e:
            if dest:
                try:
                    os.remove(dest)
                except OSError:
                    pass
            progress(0, 0, "error", str(e))
            log(f"pull {name} failed: {e}")
        finally:
            self.c.pull_finished()

    def _send_file(self, msg):
        """Proxy a media file back to the server — thumbnails/previews
        in the server UI. Capped to keep the socket sane."""
        rid = msg.get("id")
        name = os.path.basename(str(msg.get("name") or ""))
        err, data = None, ""
        try:
            folder = self._media_dir()
            full = os.path.join(folder, name) if folder else ""
            if not full or not os.path.isfile(full):
                raise FileNotFoundError(name)
            if os.path.getsize(full) > 8 * 1024 * 1024:
                raise ValueError("file too large to proxy")
            with open(full, "rb") as f:
                data = base64.b64encode(f.read()).decode()
        except Exception as e:
            err = str(e)
        self._reply(
            {"type": "file", "id": rid, "name": name,
             "data": data, "error": err}
        )

    def _hello(self):
        # Per-device auth token issued by the server at QR pairing; the
        # server rejects any hello without it.
        token = self.c.config.get("server_auth_token") or ""
        return {
            "type": "hello",
            "product": "screensemble-client",
            "version": self.addon.getAddonInfo("version"),
            "name": self.c.config.get("device_name") or "",
            "hardware_id": hardware_id(),
            "pairing_id": self.addon.getSetting("paired_id") or "",
            "token": token,
        }

    def _run(self):
        backoff = 2.0
        while not self._stop:
            url = self._ws_url()
            if not url:
                time.sleep(10)
                continue
            if not self.c.config.get("server_auth_token"):
                # Never paired — the server would refuse us. Wait for the
                # QR pairing flow to store a token.
                xbmcgui.Window(10000).setProperty(LINK_PROPERTY, "unpaired")
                time.sleep(10)
                continue
            try:
                self.sock = _ws_connect(url)
                _send(self.sock, json.dumps(self._hello()))
                self.sock.settimeout(1.0)
                log(f"connected to {url}")
                xbmcgui.Window(10000).setProperty(LINK_PROPERTY, "connected")
                backoff = 2.0
                last_status = 0.0
                while not self._stop:
                    if time.monotonic() - last_status >= self.STATUS_EVERY:
                        self._reply(
                            {"type": "status", "status": self._status()}
                        )
                        last_status = time.monotonic()
                    try:
                        opcode, data = _recv(self.sock)
                    except socket.timeout:
                        continue
                    if opcode == 0x8:
                        raise OSError("server closed")
                    if opcode == 0x9:
                        _send_frame(self.sock, 0xA, data)
                        continue
                    if opcode != 0x1:
                        continue
                    try:
                        self._on_message(
                            json.loads(data.decode("utf-8", "replace"))
                        )
                    except Exception as e:
                        log(f"message handling failed: {e}")
            except OSError as e:
                log(f"link down ({e}); retry in {backoff:.0f}s")
            finally:
                xbmcgui.Window(10000).setProperty(LINK_PROPERTY, "offline")
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
            # Auth rejected — retry slowly; the server keeps refusing
            # until the display is re-paired or its stale registry
            # entry is removed.
            if time.time() - self._auth_rejected < 300:
                backoff = max(backoff, 300.0)
            waited = 0.0
            while waited < backoff and not self._stop:
                time.sleep(0.5)
                waited += 0.5
            backoff = min(backoff * 2, 60.0)
