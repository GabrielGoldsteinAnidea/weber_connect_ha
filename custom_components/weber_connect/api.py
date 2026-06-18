"""Minimal, self-contained Weber Connect cloud client for Home Assistant.

Blocking (stdlib urllib) by design — the coordinator calls it via
hass.async_add_executor_job, so no extra dependencies are needed. Mirrors the
validated logic in the weber_connect reference package.

Auth: companion device-password login (durable). Discovery: /associated lists the
appliance(s). Data: REST cook-history snapshots (append-only; paged by after_id).
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request

from .const import APP_CLIENT_ID, APP_CLIENT_SECRET

API_HOST = "api.walker-cloud.com"
MSG_HOST = "messaging.walker-cloud.com"
USER_AGENT = "okhttp/5.3.0"

# probe state byte (TLV 0x0c) -> doneness, from live experiments
_PROBE_STATE = {0x02: "idle", 0x09: "cooking", 0x07: "done"}


class WeberAuthError(Exception):
    """Authentication / authorization failure (bad creds, or appliance not paired)."""


class WeberError(Exception):
    """Generic transport/API error."""


def deci_c_to_f(raw: int) -> float:
    """Raw deci-Celsius (cook-history/ws unit) -> °F."""
    return round(raw / 10.0 * 9.0 / 5.0 + 32.0, 1)


class WeberCloud:
    """Thin synchronous client around the walker-cloud REST API."""

    def __init__(self, device_id: str, device_password: str, timeout: float = 30.0):
        self.device_id = device_id
        self.device_password = device_password
        self.timeout = timeout
        self._token: str | None = None
        self._token_exp: float = 0.0

    # ------------------------------------------------------------------ http
    def _open(self, req: urllib.request.Request) -> bytes:
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data

    def authenticate(self) -> str:
        body = {
            "password": self.device_password,
            "device_id": self.device_id,
            "client_secret": APP_CLIENT_SECRET,
            "client_id": APP_CLIENT_ID,
            "device_name": "home-assistant",
            "device_type": "companion",
            "platform": "android",
            "platform_version": "33",
            "version": "2.10.1.2488",
        }
        req = urllib.request.Request(
            f"https://{API_HOST}/2/devices/register",
            data=json.dumps(body).encode("utf-8"), method="POST",
        )
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", "application/json; charset=UTF-8")
        req.add_header("Accept-Encoding", "gzip")
        try:
            data = self._open(req)
        except urllib.error.HTTPError as e:
            raise WeberAuthError(f"device login failed: HTTP {e.code}") from e
        except OSError as e:
            raise WeberError(f"network error during login: {e}") from e
        j = json.loads(data.decode("utf-8"))
        tok = j.get("token", j)
        self._token = tok["access_token"]
        self._token_exp = time.time() + int(tok.get("expires_in", 21000))
        return self._token

    def _token_value(self) -> str:
        if not self._token or time.time() >= self._token_exp - 60:
            self.authenticate()
        return self._token  # type: ignore[return-value]

    def _get_json(self, path: str) -> dict:
        req = urllib.request.Request(f"https://{API_HOST}{path}", method="GET")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept-Encoding", "gzip")
        req.add_header("Authorization", "Bearer " + self._token_value())
        try:
            data = self._open(req)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._token = None  # force re-auth next call
            if e.code in (401, 403):
                raise WeberAuthError(f"GET {path} -> HTTP {e.code}") from e
            raise WeberError(f"GET {path} -> HTTP {e.code}") from e
        except OSError as e:
            raise WeberError(f"GET {path}: {e}") from e
        return json.loads(data.decode("utf-8"))

    # ------------------------------------------------------------------ api
    def discover_appliances(self) -> list[dict]:
        """List appliances paired to this companion device."""
        j = self._get_json(f"/2/devices/{self.device_id}/associated")
        out = []
        for d in j.get("devices", []):
            out.append({
                "id": d.get("oven_id") or d.get("appliance_id"),
                "name": d.get("name") or "Weber Connect Hub",
                "serial": d.get("serial_number"),
                "model": d.get("model_number"),
            })
        return [a for a in out if a["id"]]

    def latest_session_id(self, appliance_id: str) -> str | None:
        j = self._get_json(f"/cook-history/1/appliance/{appliance_id}/sessions")
        items = j.get("sessions", j) if isinstance(j, dict) else j
        if not isinstance(items, list) or not items:
            return None

        def keyf(it):
            return (it.get("server_timestamp") or it.get("timestamp")
                    or it.get("updated_at") or it.get("created_at") or 0)

        best = sorted(items, key=keyf)[-1]
        return best.get("session_id") or best.get("id")

    def get_snapshots(self, appliance_id: str, session_id: str, after_id: int = 0):
        """Return (snapshots, max_snapshot_id) for new snapshots after `after_id`."""
        snaps_all = []
        aid = after_id
        while True:
            j = self._get_json(
                f"/cook-history/1/appliance/{appliance_id}/session/{session_id}"
                f"/snapshots?limit=1000&after_id={aid}")
            snaps = j.get("snapshots", [])
            if not snaps:
                break
            snaps_all.extend(snaps)
            aid = max(aid, snaps[-1]["snapshot_id"])
            if len(snaps) < 1000:
                break
        return snaps_all, aid
    # (websocket helpers are module-level functions at the bottom of this file)

    # ----------------------------------------------------- websocket (doneness)
    def companion_status(self, appliance_id: str, seconds: float = 5.0,
                         raw_sink: list | None = None) -> dict | None:
        """Open the companion websocket, subscribe, and read one status frame.
        Returns {probe_index: {"state": cooking/done/idle, "present": True}} or
        None if the socket was contended/closed (phone app holding the session).

        If `raw_sink` (a list) is given, every received binary frame is appended to
        it as raw bytes for offline protocol decoding, and the read runs the full
        `seconds` window (no early break) to capture as many frames as possible."""
        token = self._token_value()
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /2/messaging/websocket/companion HTTP/1.1\r\n"
            f"Host: {MSG_HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\nUser-Agent: {USER_AGENT}\r\n\r\n"
        ).encode()
        ctx = ssl.create_default_context()
        try:
            raw = socket.create_connection((MSG_HOST, 443), timeout=15)
            s = ctx.wrap_socket(raw, server_hostname=MSG_HOST)
        except OSError as e:
            raise WeberError(f"ws connect: {e}") from e
        probes: dict | None = None
        data = b""
        try:
            s.sendall(req)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head = buf.split(b"\r\n", 1)[0]
            if b"101" not in head:
                return None
            data = buf.partition(b"\r\n\r\n")[2]
            seq = 1
            for pl in _subscribe_payloads():
                s.sendall(_client_frame(seq, pl, appliance_id, self.device_id))
                seq += 1
                time.sleep(0.05)
            s.settimeout(1.0)
            t0 = time.time()
            while time.time() - t0 < seconds:
                try:
                    chunk = s.recv(8192)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                data += chunk
                for op, p in _ws_frames(data):
                    if op == 2 and len(p) > 43:
                        st = _parse_ws_status(p[41:])
                        if st:
                            probes = st
                # when capturing raw frames, read the whole window; else stop early
                if probes and raw_sink is None:
                    break
            if raw_sink is not None:
                for op, p in _ws_frames(data):
                    if op == 2 and len(p) > 43:
                        raw_sink.append(bytes(p))
        finally:
            try:
                s.close()
            except OSError:
                pass
        return probes


# --------------------------------------------------------------- ws helpers
def _mask_frame(payload: bytes) -> bytes:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    hdr = bytes([0x82])  # FIN + binary
    if n < 126:
        hdr += bytes([0x80 | n])
    elif n < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return hdr + mask + masked


def _client_frame(seq: int, payload: bytes, appliance: str, device: str) -> bytes:
    body = (b"\x01\x02" + bytes.fromhex(device) + b"\x02" + bytes.fromhex(appliance)
            + struct.pack("<I", seq) + struct.pack("<H", len(payload)) + payload)
    return _mask_frame(body)


def _subscribe_payloads() -> list[bytes]:
    ts = int(time.time())

    def stamp(t):
        return b"\x0a\x09\x15\x04" + struct.pack("<I", t)

    return [bytes.fromhex("0a0e"), bytes.fromhex("0a05"), stamp(ts),
            bytes.fromhex("0a07"), bytes.fromhex("0a0b0100"), bytes.fromhex("0a0e"),
            stamp(ts + 1), bytes.fromhex("0a05"), bytes.fromhex("0a07")]


def _ws_frames(buf: bytes, start: int = 0):
    i = start
    while i + 2 <= len(buf):
        b0, b1 = buf[i], buf[i + 1]
        op = b0 & 0x0F
        masked = b1 >> 7
        ln = b1 & 0x7F
        i += 2
        if ln == 126:
            ln = int.from_bytes(buf[i:i + 2], "big"); i += 2
        elif ln == 127:
            ln = int.from_bytes(buf[i:i + 8], "big"); i += 8
        if masked:
            i += 4
        if i + ln > len(buf):
            break
        yield op, buf[i:i + ln]
        i += ln


def _parse_ws_status(payload: bytes) -> dict | None:
    """cmd 0x80 status frame -> {probe_index: {state, present}}."""
    if len(payload) < 2 or payload[0] != 0x0A or payload[1] != 0x80:
        return None
    body = payload[2:]
    probes: dict = {}
    i = 0
    while i + 2 <= len(body):
        tag = body[i]; ln = body[i + 1]
        if i + 2 + ln > len(body):
            break
        v = body[i + 2:i + 2 + ln]
        if tag == 0x04 and ln > 20:
            f = {}
            j = 0
            while j + 2 <= len(v):
                t = v[j]; l = v[j + 1]
                if j + 2 + l > len(v):
                    break
                f[t] = v[j + 2:j + 2 + l]
                j += 2 + l
            idx = f[0x01][0] if 0x01 in f and f[0x01] else None
            if idx is not None:
                state = _PROBE_STATE.get(f[0x0c][0], "unknown") if 0x0c in f and f[0x0c] else "unknown"
                probes[idx] = {"state": state, "present": True}
        i += 2 + ln
    return probes
