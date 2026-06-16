"""Low-level wire-format decoders for the Weber Connect / walker-cloud protocol.

Pure functions, no I/O, so they can be unit-tested against captured bytes.
"""
from __future__ import annotations
import gzip
import struct
from typing import Iterator, Optional


# --------------------------------------------------------------------------- HTTP/1.1
def _dechunk(raw: bytes) -> bytes:
    out = bytearray(); i = 0
    while i < len(raw):
        nl = raw.find(b"\r\n", i)
        if nl < 0:
            break
        try:
            ln = int(raw[i:nl].split(b";")[0], 16)
        except ValueError:
            break
        if ln == 0:
            break
        out += raw[nl + 2:nl + 2 + ln]
        i = nl + 2 + ln + 2
    return bytes(out)


def parse_http_bodies(data: bytes) -> list[bytes]:
    """Split a raw HTTP/1.1 response byte stream into decoded body payloads
    (handles keep-alive pipelining, chunked transfer-encoding, gzip)."""
    out: list[bytes] = []
    i = 0
    while True:
        j = data.find(b"HTTP/1.1 ", i)
        if j < 0:
            break
        he = data.find(b"\r\n\r\n", j)
        if he < 0:
            break
        head = data[j:he].decode("latin1")
        bs = he + 4
        nxt = data.find(b"HTTP/1.1 ", bs)
        se = nxt if nxt > 0 else len(data)
        raw = data[bs:se]
        h: dict[str, str] = {}
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                h[k.strip().lower()] = v.strip()
        body = raw
        if h.get("transfer-encoding", "").lower() == "chunked":
            body = _dechunk(raw)
        elif "content-length" in h:
            try:
                body = raw[:int(h["content-length"])]
            except ValueError:
                pass
        if "gzip" in h.get("content-encoding", ""):
            try:
                body = gzip.decompress(body)
            except Exception:
                pass
        out.append(body)
        i = se
    return out


# --------------------------------------------------------------------------- WebSocket
def ws_frames(buf: bytes, start: int = 0) -> Iterator[tuple[int, bytes]]:
    """Yield (opcode, payload) for each websocket frame. Handles masked
    (client) and unmasked (server) frames and 7/16/64-bit lengths."""
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
        mask = b""
        if masked:
            mask = buf[i:i + 4]; i += 4
        if i + ln > len(buf):
            break
        p = buf[i:i + ln]; i += ln
        if masked:
            p = bytes(x ^ mask[k % 4] for k, x in enumerate(p))
        yield op, p


# --------------------------------------------------------------------------- companion telemetry frame
# Server frame layout (observed):
#   01 01 <appliance_id:16> 01 <device_id:16> <seq:u32 LE> <len:u16 LE> <payload>
# Inside <payload>:
#   ... 08 10 <session_id:16> ...        session id (field 8, len 0x10)
#   ... 03 10 <8 x int16 LE>  ...        probe temperatures (field 3, len 0x10)
SESSION_MARKER = b"\x08\x10"  # field 8, len 16 -> session_id

# TLV field tags inside the companion telemetry payload
TAG_SESSION = 0x08
TAG_TEMP = 0x0a     # int16 LE, DECI-CELSIUS (value/10 = °C). -32768 = no probe.
TAG_INDEX = 0x02    # probe channel index
TAG_ARRAY = 0x03    # 16-byte array (not the live temp; usually zeros)
TEMP_NONE = -32768  # sentinel: no probe inserted / no reading


def deci_c_to_f(raw: int) -> float:
    """Convert a raw deci-Celsius temperature (the unit used by both the
    companion-websocket TLV field 0x0a AND the REST cook-history
    `probe_status[].temperature`) to °F. Verified live: raw 250 -> 77.0°F."""
    return round(raw / 10.0 * 9.0 / 5.0 + 32.0, 1)


def _tlv(buf: bytes):
    """Iterate (tag, value) over a 1-byte-tag / 1-byte-length TLV stream."""
    i = 0
    while i + 2 <= len(buf):
        tag = buf[i]; ln = buf[i + 1]
        if i + 2 + ln > len(buf):
            break
        yield tag, buf[i + 2:i + 2 + ln]
        i += 2 + ln


def companion_temps(payload: bytes, depth: int = 0, out=None):
    """Recursively extract probe temperatures from a companion frame payload
    (the bytes after the 41-byte frame header). Returns list of
    {"index": int, "temp_f": int}. A probe sub-message is any TLV node that
    carries a TAG_TEMP (0x0a) field of length 2."""
    if out is None:
        out = []
    fields = {}
    children = []
    for tag, v in _tlv(payload):
        fields.setdefault(tag, []).append(v)
        children.append((tag, v))
    if TAG_TEMP in fields:
        for tv in fields[TAG_TEMP]:
            if len(tv) == 2:
                raw = struct.unpack("<h", tv)[0]
                if raw == TEMP_NONE:        # no probe / no reading
                    continue
                if -600 <= raw <= 4000:     # plausible: -60°C .. 400°C (deci-C)
                    idx = fields.get(TAG_INDEX, [b"\xff"])[0][0] if fields.get(TAG_INDEX) else -1
                    out.append({"index": idx, "raw": raw,
                                "temp_c": round(raw / 10.0, 1),
                                "temp_f": deci_c_to_f(raw)})
    if depth < 4:
        for tag, v in children:
            if len(v) >= 4 and tag not in (TAG_SESSION, TAG_ARRAY):
                companion_temps(v, depth + 1, out)
    return out


def parse_companion_frame(p: bytes) -> dict:
    """Decode a server->client companion telemetry frame into a dict with any of
    appliance / device / session (hex) and probes (list of {index, temp_f})."""
    out: dict = {}
    if len(p) < 35:
        return out
    out["appliance"] = p[2:18].hex()
    out["device"] = p[19:35].hex()
    if len(p) >= 39:
        out["seq"] = struct.unpack_from("<I", p, 35)[0]
    j = p.find(SESSION_MARKER)
    if j >= 0 and j + 2 + 16 <= len(p):
        out["session"] = p[j + 2:j + 2 + 16].hex()
    if len(p) >= 41:
        temps = companion_temps(p[41:])
        if temps:
            out["probes"] = temps
    return out


def discover_from_ws_stream(server_bytes: bytes) -> dict:
    """Given a raw server->client websocket byte stream (after the HTTP upgrade),
    return discovered appliance/device/session ids and last non-zero probes."""
    start = server_bytes.find(b"\r\n\r\n")
    start = start + 4 if start >= 0 else 0
    appliances, devices, sessions = set(), set(), set()
    probes: Optional[list[int]] = None
    for op, p in ws_frames(server_bytes, start):
        if op != 2:
            continue
        d = parse_companion_frame(p)
        if d.get("appliance"):
            appliances.add(d["appliance"])
        if d.get("device"):
            devices.add(d["device"])
        if d.get("session"):
            sessions.add(d["session"])
        if d.get("probes") and any(d["probes"]):
            probes = d["probes"]
    return {
        "appliances": sorted(appliances),
        "devices": sorted(devices),
        "sessions": sorted(sessions),
        "probes": probes,
    }


# --------------------------------------------------------------------------- appliance status
# Server companion frames are: <41-byte frame header> then payload = b"\x0a" + <cmd> + <TLVs>.
# Mapped by live experiments (see docs/HA-INTEGRATION-NOTES.md):
CMD_STATUS = 0x80        # per-probe live status (temps, doneness, timer)
CMD_DEVICE_INFO = 0x83   # hub name / serial / wifi / firmware / rssi / battery
# probe state byte (TLV 0x0c) inside a probe sub-message:
PROBE_STATE = {0x02: "idle", 0x09: "cooking", 0x07: "done"}  # 9=below target, 7=at/above (done)


def _probe_block(v: bytes) -> dict:
    """Parse one 0x04 per-probe sub-message (TLV) into a friendly dict."""
    f = {}
    i = 0
    while i + 2 <= len(v):
        t = v[i]; ln = v[i + 1]
        if i + 2 + ln > len(v):
            break
        f[t] = v[i + 2:i + 2 + ln]
        i += 2 + ln
    out: dict = {"present": True}
    out["index"] = f[0x01][0] if 0x01 in f and f[0x01] else None
    if 0x0a in f and len(f[0x0a]) == 2:
        raw = struct.unpack("<h", f[0x0a])[0]
        if raw != TEMP_NONE:
            out["raw"] = raw
            out["temp_c"] = round(raw / 10.0, 1)
            out["temp_f"] = deci_c_to_f(raw)
    if 0x0c in f and len(f[0x0c]) == 1:
        s = f[0x0c][0]
        out["state_code"] = s
        out["state"] = PROBE_STATE.get(s, "unknown")
        out["has_target"] = s in (0x07, 0x09)
        out["done"] = (s == 0x07)
    if 0x05 in f and len(f[0x05]) == 4:
        out["timer_ms"] = struct.unpack("<i", f[0x05])[0]
    if 0x06 in f and len(f[0x06]) == 4:
        out["elapsed_ms"] = struct.unpack("<i", f[0x06])[0]
    return out


def parse_appliance_status(payload: bytes) -> dict:
    """Decode a server companion frame payload (the bytes AFTER the 41-byte frame
    header). For a status frame (cmd 0x80) returns
    {"cmd", "session", "probes":[{index, temp_c, temp_f, state, done, timer_ms,...}]};
    for a device-info frame (cmd 0x83) returns {name, serial, ssid, wifi_mac,
    firmware, rssi, battery}. NOTE: the probe *target/setpoint* value is not present
    in these frames (only current temp + doneness state)."""
    if len(payload) < 2 or payload[0] != 0x0a:
        return {}
    cmd = payload[1]
    body = payload[2:]
    fields: dict = {}
    probes = []
    i = 0
    while i + 2 <= len(body):
        tag = body[i]; ln = body[i + 1]
        if i + 2 + ln > len(body):
            break
        v = body[i + 2:i + 2 + ln]
        if tag == 0x04 and ln > 20:
            probes.append(_probe_block(v))
        else:
            fields.setdefault(tag, v)
        i += 2 + ln
    if cmd == CMD_STATUS:
        sess = fields.get(0x08)
        return {"cmd": cmd, "session": sess.hex() if sess else None, "probes": probes}
    if cmd == CMD_DEVICE_INFO:
        def s(tag):
            return fields[tag].decode("ascii", "replace") if tag in fields else None
        rssi = None
        if 0x0a in fields and len(fields[0x0a]) == 4:
            rssi = struct.unpack("<i", fields[0x0a])[0]
        return {"cmd": cmd, "name": s(0x07), "serial": s(0x09), "ssid": s(0x10),
                "wifi_mac": s(0x1f), "firmware": s(0x13), "rssi": rssi,
                "battery": fields[0x01][0] if 0x01 in fields and fields[0x01] else None}
    return {"cmd": cmd}
