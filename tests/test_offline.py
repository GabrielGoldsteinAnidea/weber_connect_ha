"""Offline tests: prove the decoders against captured (decrypted) bytes.

Fixtures live in tests/fixtures/ and are gitignored because they contain
personal tokens/ids. If they are absent the tests skip.

Run:  python -m pytest -q      (or)   python tests/test_offline.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from weber_connect import parse_http_bodies, discover_from_ws_stream  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
OAUTH = os.path.join(FIX, "oauth_s2c.bin")
COOK = os.path.join(FIX, "cookhistory_s2c.bin")
WSBIN = os.path.join(FIX, "messaging_ws_s2c.bin")

# IDs are personal/account-linked, so they are NOT hardcoded here (and the fixtures
# that contain them are gitignored). The test instead asserts the decoder recovers
# well-formed identifiers (32-char hex) from the stream.
_HEXID = re.compile(r"^[0-9a-f]{32}$")


def _have(*paths):
    return all(os.path.exists(p) for p in paths)


def test_oauth_decode():
    if not _have(OAUTH):
        print("SKIP oauth (no fixture)"); return
    tok = None
    for body in parse_http_bodies(open(OAUTH, "rb").read()):
        t = body.decode("utf-8", "replace")
        if '"access_token"' in t:
            j = json.loads(t)
            tok = j.get("token", j)
            break
    assert tok and tok["access_token"].startswith("v2:")
    assert tok["token_type"] == "Bearer"
    assert tok["expires_in"] > 0
    print(f"OK oauth: access_token={tok['access_token']} expires_in={tok['expires_in']}")


def test_cookhistory_decode():
    if not _have(COOK):
        print("SKIP cook-history (no fixture)"); return
    snaps = None
    for body in parse_http_bodies(open(COOK, "rb").read()):
        t = body.decode("utf-8", "replace")
        if '"snapshots"' in t:
            snaps = json.loads(t)["snapshots"]
            break
    assert snaps, "no snapshots parsed"
    temps = [p["temperature"] for s in snaps for p in s["data"]["probe_status"]]
    assert temps and all(isinstance(x, int) for x in temps)
    assert 100 <= max(temps) <= 600  # plausible deg F
    print(f"OK cook-history: {len(snaps)} snapshots, temps {min(temps)}-{max(temps)}F")


def test_ws_discovery():
    if not _have(WSBIN):
        print("SKIP websocket (no fixture)"); return
    d = discover_from_ws_stream(open(WSBIN, "rb").read())
    # decoder must recover at least one well-formed (32-hex) id of each kind
    assert d["appliances"] and all(_HEXID.match(x) for x in d["appliances"])
    assert d["devices"] and all(_HEXID.match(x) for x in d["devices"])
    assert d["sessions"] and all(_HEXID.match(x) for x in d["sessions"])
    print(f"OK websocket: discovered "
          f"{len(d['appliances'])} appliance / {len(d['devices'])} device / "
          f"{len(d['sessions'])} session id(s)")


def test_ws_live_temps():
    if not _have(WSBIN):
        print("SKIP ws temps (no fixture)"); return
    from weber_connect import ws_frames, companion_temps
    raw = open(WSBIN, "rb").read()
    start = raw.find(b"\r\n\r\n"); start = start + 4 if start >= 0 else 0
    temps = []
    for op, p in ws_frames(raw, start):
        if op != 2 or len(p) < 120:
            continue
        for r in companion_temps(p[41:]):
            temps.append(r["temp_f"])
    # this capture is an idle hub OR an active cook depending on which fixture;
    # if temps present, they must be plausible degF
    if temps:
        assert all(-50 <= t <= 600 for t in temps)
        print(f"OK ws live temps: {len(temps)} readings, range {min(temps)}-{max(temps)}F")
    else:
        print("OK ws live temps: none in this fixture (hub idle) -- decoder ran clean")


if __name__ == "__main__":
    test_oauth_decode()
    test_cookhistory_decode()
    test_ws_discovery()
    test_ws_live_temps()
    print("ALL OFFLINE TESTS PASSED")
