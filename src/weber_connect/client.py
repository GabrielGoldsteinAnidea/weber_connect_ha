"""Weber Connect cloud client (walker-cloud / June Cloud backend).

Stdlib-only HTTP + WebSocket client. Designed to be reused by a Home Assistant
integration.

Auth: OAuth2 with a stored refresh token (initial pairing is done out-of-band
over Bluetooth + appliance serial, which binds the account in the backend, so
there is no email/password call to replay). A client needs the refresh token
plus the app-global client_id / client_secret.

appliance_id is NOT shown in the app. The companion websocket is request/driven:
the app sends a small subscribe sequence and the server then streams frames that
carry appliance_id, device_id, the active session_id and live probe temps.
"""
from __future__ import annotations
import base64
import gzip
import json
import logging
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from .protocol import parse_companion_frame, ws_frames

_LOGGER = logging.getLogger(__name__)

API_HOST = "api.walker-cloud.com"
MSG_HOST = "messaging.walker-cloud.com"
USER_AGENT = "okhttp/5.3.0"

# Subscribe/keepalive command payloads the app sends after the websocket
# upgrade. The 0a09 command carries a CURRENT unix timestamp (15 04 <u32 LE>);
# replaying a stale one makes the server close the socket, so build it fresh.
def subscribe_payloads(now: Optional[int] = None) -> list:
    import time as _t
    ts = int(now if now is not None else _t.time())
    def stamp(t):
        return b"\x0a\x09\x15\x04" + struct.pack("<I", t)
    return [
        bytes.fromhex("0a0e"),
        bytes.fromhex("0a05"),
        stamp(ts),
        bytes.fromhex("0a07"),
        bytes.fromhex("0a0b0100"),
        bytes.fromhex("0a0e"),
        stamp(ts + 1),
        bytes.fromhex("0a05"),
        bytes.fromhex("0a07"),
    ]


@dataclass
class Token:
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 0
    obtained_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() >= self.obtained_at + self.expires_in - 60


class WeberConnectError(RuntimeError):
    pass


class WeberConnectClient:
    def __init__(self, client_id: str, client_secret: str,
                 refresh_token: Optional[str] = None,
                 device_id: Optional[str] = None,
                 device_password: Optional[str] = None,
                 device_name: str = "weber_connect",
                 app_version: str = "2.10.1.2488",
                 timeout: float = 30.0):
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.device_id = device_id
        self.device_password = device_password
        self.device_name = device_name
        self.app_version = app_version
        self.timeout = timeout
        self._token: Optional[Token] = None

    # ------------------------------------------------------------------ auth
    def authenticate(self) -> Token:
        # Prefer durable device-password login (survives refresh-token rotation);
        # fall back to refresh_token if no password is configured.
        if self.device_id and self.device_password:
            return self.register_device()
        return self._refresh()

    def _refresh(self) -> Token:
        q = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_secret": self.client_secret,
            "client_id": self.client_id,
        })
        body = self._raw_get(API_HOST, f"/2/auth/oauth/token?{q}")
        return self._store_token(json.loads(body.decode("utf-8")))

    def register_device(self) -> Token:
        """Companion-device login: POST /2/devices/register {device_id, password,
        client creds}. Returns the OAuth token grant. This is the durable login."""
        body = {
            "password": self.device_password,
            "device_id": self.device_id,
            "client_secret": self.client_secret,
            "client_id": self.client_id,
            "device_name": self.device_name,
            "device_type": "companion",
            "platform": "android",
            "platform_version": "33",
            "version": self.app_version,
        }
        data = self._raw_post(API_HOST, "/2/devices/register", body)
        return self._store_token(json.loads(data.decode("utf-8")))

    def _store_token(self, j: dict) -> Token:
        t = j.get("token", j)
        self._token = Token(
            access_token=t["access_token"],
            refresh_token=t.get("refresh_token", self.refresh_token or ""),
            token_type=t.get("token_type", "Bearer"),
            expires_in=int(t.get("expires_in", 0)),
        )
        self.refresh_token = self._token.refresh_token
        return self._token

    @property
    def token(self) -> Token:
        if self._token is None or self._token.expired:
            self.authenticate()
        return self._token  # type: ignore[return-value]

    # ------------------------------------------------------------------ REST
    def _raw_get(self, host: str, path: str, bearer: Optional[str] = None) -> bytes:
        req = urllib.request.Request(f"https://{host}{path}", method="GET")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept-Encoding", "gzip")
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            raise WeberConnectError(f"GET {path} -> HTTP {e.code}") from e

    def _raw_post(self, host: str, path: str, body_obj: dict,
                  bearer: Optional[str] = None) -> bytes:
        data = json.dumps(body_obj).encode("utf-8")
        req = urllib.request.Request(f"https://{host}{path}", data=data, method="POST")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", "application/json; charset=UTF-8")
        req.add_header("Accept-Encoding", "gzip")
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    d = gzip.decompress(d)
                return d
        except urllib.error.HTTPError as e:
            raise WeberConnectError(f"POST {path} -> HTTP {e.code}") from e

    def api_get_json(self, path: str) -> dict:
        return json.loads(self._raw_get(API_HOST, path, self.token.access_token).decode("utf-8"))

    def get_snapshots(self, appliance_id: str, session_id: str,
                      after_id: int = 0, limit: int = 1000) -> dict:
        return self.api_get_json(
            f"/cook-history/1/appliance/{appliance_id}/session/{session_id}"
            f"/snapshots?limit={limit}&after_id={after_id}")

    def iter_all_snapshots(self, appliance_id: str, session_id: str, after_id: int = 0):
        while True:
            page = self.get_snapshots(appliance_id, session_id, after_id=after_id)
            snaps = page.get("snapshots", [])
            if not snaps:
                return
            for s in snaps:
                after_id = max(after_id, s["snapshot_id"])
                yield s
            if len(snaps) < 1000:
                return

    def get_sessions(self, appliance_id: str) -> dict:
        """List cook sessions for an appliance (newest cook included)."""
        return self.api_get_json(f"/cook-history/1/appliance/{appliance_id}/sessions")

    def latest_session_id(self, appliance_id: str) -> Optional[str]:
        """Return the most recent session_id for an appliance, or None."""
        data = self.get_sessions(appliance_id)
        items = data.get("sessions", data) if isinstance(data, dict) else data
        if not isinstance(items, list) or not items:
            return None
        def keyf(it):
            return (it.get("server_timestamp") or it.get("timestamp")
                    or it.get("updated_at") or it.get("created_at") or 0)
        best = sorted(items, key=keyf)[-1]
        return best.get("session_id") or best.get("id")

    def serial_details(self, appliance_serial_number: str) -> dict:
        """Resolve an appliance serial number to its appliance record (incl. id)."""
        import urllib.parse as _u
        q = _u.urlencode({"appliance_serial_number": appliance_serial_number})
        return self.api_get_json(f"/device-registration/serial_details?{q}")

    def associated_appliances(self, device_id: str) -> dict:
        """List appliances associated with this companion device (the discovery
        endpoint: GET /2/devices/{companion_id}/associated)."""
        return self.api_get_json(f"/2/devices/{device_id}/associated")

    @staticmethod
    def latest_temps(snapshot: dict) -> dict:
        return {p["index"]: p["temperature"]
                for p in snapshot.get("data", {}).get("probe_status", [])}

    # ------------------------------------------------------------- websocket
    @staticmethod
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

    def _client_frame(self, seq: int, payload: bytes,
                      appliance: Optional[str], device: Optional[str]) -> Optional[bytes]:
        if not (appliance and device):
            return None
        body = (b"\x01\x02" + bytes.fromhex(device) + b"\x02" + bytes.fromhex(appliance)
                + struct.pack("<I", seq) + struct.pack("<H", len(payload)) + payload)
        return self._mask_frame(body)

    def live(self, seconds: float = 10.0, subscribe: bool = True,
             appliance: Optional[str] = None, device: Optional[str] = None,
             dump_path: Optional[str] = None) -> dict:
        """Open the companion websocket, optionally send the subscribe sequence,
        read for `seconds`, and return discovered ids + latest probe values.
        Writes the raw server byte stream to dump_path if given."""
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /2/messaging/websocket/companion HTTP/1.1\r\n"
            f"Host: {MSG_HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {self.token.access_token}\r\n"
            f"User-Agent: {USER_AGENT}\r\n\r\n"
        ).encode()
        # The app pings the device status endpoint before opening the ws; this
        # appears to prompt the hub to start relaying to a companion.
        status_pre = ""
        if subscribe and appliance:
            try:
                b = self._raw_get(MSG_HOST,
                    f"/1/messaging/device/{appliance}/status",
                    self.token.access_token)
                status_pre = b.decode("utf-8", "replace")[:200]
            except Exception as e:
                status_pre = f"status precall error: {e}"
        ctx = ssl.create_default_context()
        raw = socket.create_connection((MSG_HOST, 443), timeout=15)
        s = ctx.wrap_socket(raw, server_hostname=MSG_HOST)
        appliances, devices, sessions, probes = set(), set(), set(), None
        data = b""
        ws_status = ""
        handshake = ""
        try:
            s.sendall(req)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            ws_status = head.split(b"\r\n")[0].decode("latin1")
            handshake = head.decode("latin1", "replace")

            def harvest(blob):
                nonlocal probes
                for op, p in ws_frames(blob):
                    if op != 2:
                        continue
                    d = parse_companion_frame(p)
                    if d.get("appliance"): appliances.add(d["appliance"])
                    if d.get("device"): devices.add(d["device"])
                    if d.get("session"): sessions.add(d["session"])
                    if d.get("probes") and any(d["probes"]): probes = d["probes"]

            if "101" in ws_status:
                data = rest
                s.settimeout(1.5)
                harvest(data)
                ap = appliance or (next(iter(appliances)) if appliances else None)
                dv = device or (next(iter(devices)) if devices else None)
                if subscribe and ap and dv:
                    seq = 1
                    for pl in subscribe_payloads():
                        fr = self._client_frame(seq, pl, ap, dv)
                        if fr:
                            s.sendall(fr); seq += 1
                            time.sleep(0.1)
                t0 = time.time()
                while time.time() - t0 < seconds:
                    try:
                        chunk = s.recv(8192)
                    except socket.timeout:
                        if subscribe and ap and dv:
                            fr = self._client_frame(0xFFFF, bytes.fromhex("0a07"), ap, dv)
                            if fr:
                                try: s.sendall(fr)
                                except OSError: pass
                        continue
                    if not chunk:
                        break
                    data += chunk
                harvest(data)
            else:
                # upgrade rejected -- keep the handshake response for diagnosis
                data = buf
        finally:
            try: s.close()
            except OSError: pass
        if dump_path:
            try:
                with open(dump_path, "wb") as f:
                    f.write(data)
            except OSError as e:
                _LOGGER.warning("failed to write ws dump to %s: %s", dump_path, e)
        # diagnostics: opcode histogram + any close frame code/reason
        opcodes = {}
        close_info = None
        for op, fp in ws_frames(data):
            opcodes[op] = opcodes.get(op, 0) + 1
            if op == 8 and close_info is None:
                code = int.from_bytes(fp[:2], "big") if len(fp) >= 2 else None
                reason = fp[2:].decode("utf-8", "replace") if len(fp) > 2 else ""
                close_info = {"code": code, "reason": reason}
        return {"appliances": sorted(appliances), "devices": sorted(devices),
                "sessions": sorted(sessions), "probes": probes,
                "raw_bytes": len(data), "ws_status": ws_status,
                "handshake": handshake, "status_pre": status_pre,
                "opcodes": opcodes, "close": close_info,
                "first_hex": data[:80].hex()}

    def discover(self, seconds: float = 8.0) -> dict:
        """Backwards-compatible alias for live() discovery."""
        return self.live(seconds=seconds, subscribe=True)


def from_config(path: str = "secrets.local.json") -> "WeberConnectClient":
    cfg = {}
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
    rt = os.environ.get("WEBER_REFRESH_TOKEN", cfg.get("refresh_token"))
    ci = os.environ.get("WEBER_CLIENT_ID", cfg.get("client_id", ""))
    cs = os.environ.get("WEBER_CLIENT_SECRET", cfg.get("client_secret", ""))
    did = os.environ.get("WEBER_DEVICE_ID", cfg.get("device_id"))
    pw = os.environ.get("WEBER_DEVICE_PASSWORD", cfg.get("device_password"))
    if not (ci and cs and (rt or (did and pw))):
        raise WeberConnectError(
            "missing credentials: need client_id/client_secret plus either a "
            "refresh_token or device_id+device_password (see secrets.example.json)")
    return WeberConnectClient(ci, cs, refresh_token=rt, device_id=did, device_password=pw)
