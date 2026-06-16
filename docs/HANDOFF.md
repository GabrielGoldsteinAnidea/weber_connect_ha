# Weber Connect — Reverse-Engineering Handoff

Authoritative entry point for continuing this work (e.g., VS Code + Claude).
Read this first; it supersedes any conflicting statements in the older docs.

Goal: get Weber Connect **probe temperatures into Home Assistant**. Two transports
exist — Weber's **cloud** (REST + websocket) and **local BLE**. This repo decoded
both; the cloud live-data path is blocked by a server-side authorization gate
(details below). A working local-BLE community add-on exists (see
`docs/REVIEW-prospectore-addon.md`).

---

## 1. Repo layout

```
src/weber_connect/
  protocol.py   # pure decoders: HTTP/1.1 bodies, websocket frames, companion TLV
  client.py     # WeberConnectClient: device-password/refresh auth, REST, ws
tools/
  validate.py   # CLI: token | diag | resolve | assoc | temps  (+ --access-token)
  tls_probe.py  # curl_cffi fingerprint test (chrome/safari/edge)
  tls_probe2.py # tls_client okhttp/android fingerprint test
  tlsdec.py     # standalone TLS decryptor for the pcaps (keylog from DSB)
tests/test_offline.py   # decoder tests vs captured fixtures (all pass)
tests/fixtures/         # decrypted capture bytes (gitignored; personal)
docs/PROTOCOL.md            # the wire protocol (auth, REST, websocket)
docs/APK-ANALYSIS.md        # APK static analysis (auth model) — see corrections below
docs/REVIEW-prospectore-addon.md  # review of the local-BLE HA add-on
secrets.local.json      # device creds (gitignored)
```

Run: `python tests/test_offline.py` (offline, always works).
Live: `python tools/validate.py <cmd>` — needs creds in secrets.local.json.

---

## 2. PCAP inventory (all on the user's Desktop\weber, NOT in repo)

All captured with **PCAPdroid + the mitm addon** on the phone; each pcapng embeds
the TLS keylog (DSB block), so `tlsdec.py` decrypts them with no extra keys.
Decrypted per-stream bodies for the live captures are under the scratch
`outputs/dec4/` during analysis; regenerate with `tlsdec.py`.

| File | When | Size | Contents / notes |
|------|------|------|------|
| `PCAPdroid_15_Jun_17_31_02.pcapng` | 6/15 17:31 | 1.5 MB | **Pre-pinning-bypass.** 409 TLS streams, ~199 api + 147 messaging, but the app was UNPATCHED so every walker stream is a TLS handshake the app aborts with `certificate_unknown` (cert pinning). No app-layer data. This is what proved pinning. |
| `PCAPdroid_15_Jun_17_35_07.pcapng` | 6/15 17:35 | 1.0 MB | Same as above (pre-bypass), 267 streams, all walker handshakes aborted. |
| `PCAPdroid_15_Jun_20_37_00.pcapng` | 6/15 20:37 | 1.7 MB | **Post-bypass attempt #1.** 3rd-party SDKs decrypt; walker still closed because of the *in-app* OkHttp pin (host `*.walker-cloud.com`). Led to the DEX pin-string patch. |
| `PCAPdroid_15_Jun_21_12_18.pcapng` | 6/16 01:12 UTC | 11 MB | **THE GOLDEN CAPTURE** (pin patched). Full plaintext walker traffic during an active cook: OAuth, cook-history snapshots (probe temps), the messaging **websocket with live telemetry**, device register (with password), content-token. Most decoding came from here. |
| `PCAPdroid_16_Jun_07_47_15.pcapng` | 6/16 07:47 | 729 KB | **Morning, app working.** Small. App polling cook-history for appliance `6f27…`/session `53fd…` → **200**. Shows the *current* working bearer `v2:<ACCESS_TOKEN_REDACTED>`. |
| `PCAPdroid_16_Jun_07_50_32.pcapng` | 6/16 07:50 | 444 KB | Same as above; confirms appliance/device/session are current and the app gets 200 with that bearer. |

Key point: the 07:47 / 07:50 captures **disprove** the earlier "appliance is stale /
not associated" theory — `6f27…` and device `065d…` are current and the app reads
them fine.

---

## 3. How decryption works

PCAPdroid (mitm mode) writes the TLS secrets into the pcapng as a Decryption
Secrets Block. `tools/tlsdec.py` parses the DSB, reassembles TCP, and decrypts
TLS 1.2 (CLIENT_RANDOM) and TLS 1.3 (handshake+traffic secrets), AES-GCM and
ChaCha20. Usage: `python tlsdec.py <file.pcapng> out.pkl` (it prints per-stream
sizes; bodies via the helpers in `protocol.py`).

---

## 4. Decoded protocol (validated)

Full detail in `docs/PROTOCOL.md`. Summary:

- **Hosts:** `api.walker-cloud.com` (REST), `messaging.walker-cloud.com` (websocket),
  `cdn.walker-cloud.com` (assets), `devices-ota.walker-cloud.com` (firmware).
  Backend is "Walker / June Cloud"; hub firmware is **juneOS2**.
- **Auth (no signing anywhere — bearer only):**
  - Device login: `POST /2/devices/register` with
    `{device_id, password, client_id, client_secret, device_type:"companion", ...}`
    → returns `{token:{access_token:"v2:…", refresh_token:"v2:…", expires_in:21599}}`.
  - Refresh: `GET /2/auth/oauth/token?grant_type=refresh_token&refresh_token=…&client_secret=…&client_id=…`.
    **Refresh tokens are single-use/rotating** — they die when the app refreshes.
    The **device password is the durable credential.**
  - `client_id`/`client_secret` are app-global (same for all users).
- **Temperatures (REST):**
  `GET /cook-history/1/appliance/{appliance_id}/session/{session_id}/snapshots?limit=1000&after_id=N`
  → JSON `snapshots[].data.probe_status[] = {index, temperature}`. Append-only;
  page with `after_id`.
  `GET /cook-history/1/appliance/{id}/sessions` lists sessions.
- **Live telemetry (websocket):** `GET /2/messaging/websocket/companion` (Upgrade,
  Bearer). Custom binary protocol: frame =
  `01 01 <appliance:16> 01 <device:16> <seq u32le> <len u16le> <payload>`; payload is
  TLV (1-byte tag, 1-byte len). Client first sends a subscribe sequence
  (`0a0e, 0a05, 0a09<unix-ts>, 0a07, 0a0b0100, …`) where `0a` = msg-version and the
  2nd byte = command (0x05 FETCH_STATUS, 0x07 FETCH_APPLIANCE_STATUS, etc.). Probe
  temp lives at TLV field `0x0a` (int16 LE). **Per the ProspectOre BLE decoder this
  is deci-Celsius** (÷10 = °C); our cloud notes earlier mislabeled it °F — verify.
  `companion_temps()` in `protocol.py` extracts it.
- **Recovered IDs/creds (user's — in secrets.local.json):**
  - device_id (companion): `065d…`
  - device password: `<DEVICE_PASSWORD_REDACTED>`
  - client_id: `qyw4CGeb/i93BrA0KAUuGtPyKImr+nUKc8lHxFdt`
  - client_secret: `<CLIENT_SECRET_REDACTED>`
  - appliance serial: `0713… (serial redacted)`
  - appliance_id: `6f27…` (CURRENT, not stale)
  - a known session_id: `53fd…`

---

## 5. The cloud blocker (current understanding — IMPORTANT)

A standalone client cannot read appliance data even with a **valid** token.
Methodically ruled OUT as the cause:

1. **Token validity** — RULED OUT. Probing with a bad token returns **401**;
   the app's real token returns **403**. The server authenticates our token fine;
   it's an *authorization* denial.
2. **HTTP headers** — RULED OUT. We extracted the app's exact 200-returning
   request byte-for-byte; our client sends identical headers (Authorization, Host,
   Connection, Accept-Encoding, User-Agent). Nothing else, no cookies, no signature.
3. **TLS fingerprint (JA3)** — RULED OUT. Same request via plain Python ssl,
   curl_cffi (chrome/safari/edge), and tls_client (okhttp4_android 7–13) → **all 403**.
4. **Source IP / WAF-IP** — RULED OUT. The PC and phone egress the **same external
   IP**; the app (phone) gets 200 and the PC gets 403 from that same IP.

What's LEFT (the working hypotheses):
- **Server-side session/ownership binding.** The access token is honored only for
  the app's *currently-active session*; a second/foreign caller using the same
  token is rejected 403. (Analogy: the BLE hub also serves only one connection at a
  time.) Not yet tested: force-stop the Weber app, then retry the same token from
  the PC — if it 200s, this is it.
- **Active-presence requirement.** Appliance reads may require the caller to hold an
  active **companion websocket** for that appliance. Our websocket upgrades (101)
  then the server immediately sends a CLOSE (2-byte, opcode 8) and streams nothing —
  even with the app's valid token. So we never establish "presence." Why our ws is
  closed is the key unknown (single-session conflict with the running app? missing a
  post-subscribe step? a per-connection token in the subscribe we can't reproduce?).

Note: `GET /2/devices/{device}/associated` returned `{"success":true,"devices":[]}`
yet the app reads `6f27…` fine — so "associated" is probably the *companion-device
sharing group*, NOT the appliance list. Don't treat empty `devices` as "no grill."

---

## 6. Assumptions (things believed but not 100% proven)

- The websocket close is caused by the app holding the single allowed session (vs a
  malformed/incomplete subscribe). Untested.
- Probe-temp field `0x0a` is deci-Celsius (from the BLE project); our live-cook
  values (≈229–283) were read as °F and "looked right," so the unit needs a
  definitive check against a known reading.
- `expires_in` ~21600s (6h) for the access token; refresh token rotates single-use.
- The 07:47/07:50 morning bearer `LbVK…` is long expired now — any live test needs a
  freshly captured bearer (or mint via device-password register).

---

## 7. Concrete next experiments (cloud, in priority order)

1. **Session-ownership test.** Capture a fresh bearer (open app briefly), then
   FORCE-STOP the Weber app, then immediately:
   `python tools/validate.py temps --access-token v2:<fresh> --appliance 6f27… --session <current>`
   200 ⇒ ownership gate (build the HA client to be the sole active session).
2. **Keep our own websocket open while polling.** If #1 still 403, modify
   `client.live()` to hold the ws open in a thread and call cook-history *while*
   connected. First solve *why the ws is CLOSED* — capture the app's full ws session
   bytes (client→server frames over time) from a fresh active-cook capture and diff
   against `subscribe_payloads()`. The `0a09 15 04 <ts>` frame carries a unix
   timestamp; check for any other per-connection/nonce field we're not reproducing.
3. **Compare app vs our ws at the TLS-record level** in a single fresh capture where
   BOTH the app and our client connect, to see exactly what the server sends our
   connection vs the app's.
4. **Re-examine the APK for the messaging/cook-history authorization** (how the
   "active session" is registered). androguard OOMs on the 11MB dex in a small
   sandbox; on a real machine use `jadx` to decompile and search
   `messaging`, `companion`, `cook-history`, `Authorization`, `session` usage.

If the cloud session-binding proves intractable, the **local BLE** path
(`docs/REVIEW-prospectore-addon.md`) is the pragmatic route and needs no cloud.

---

## 8. Environment notes (Windows, user's machine)

- `python` = Python 3.13 (WindowsApps store build). `pip` shim is broken (points at a
  removed 3.7); always use `python -m pip install <pkg> --user`.
- Installed there: `requests`-free (client is stdlib urllib), `curl_cffi`,
  `tls_client` (+ `typing_extensions`). Sandbox here also has `scapy`,
  `cryptography`, `androguard`.
- Run scripts in a **plain terminal**, not the VS Code debugger (debugpy launcher
  intermittently fails to attach; output also goes to the Debug Console).
- The folder's editor/sync occasionally appends NUL bytes / truncates files written
  by the assistant — if a script "runs but does nothing," check it isn't truncated
  (missing `main()`), and strip trailing `\x00`.
