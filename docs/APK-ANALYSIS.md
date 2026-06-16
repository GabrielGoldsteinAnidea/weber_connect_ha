# Weber Connect — APK auth analysis (overnight deep dive)

> **CORRECTION (see docs/HANDOFF.md for the authoritative state):** later captures
> (6/16 07:47, 07:50) proved the appliance `6f27…` and device `065d…` are **current,
> not stale**, and that `/2/devices/{id}/associated` returning empty does NOT mean
> "no grill." The real blocker is a server-side **authorization/session gate**: a
> *valid* token (401 vs 403 confirms it) returns 403 from our client but 200 from the
> app, with identical headers, TLS fingerprint (incl. okhttp), and the SAME external
> IP. Read HANDOFF.md §5 for the ruled-out causes and remaining hypotheses.


Goal: find the app's "authentication algorithm" and figure out why a standalone
client gets **403 / silent websocket** while the app works.

## TL;DR

There is **no client-side cryptographic auth to reverse**. Every Weber request
carries only `Authorization: Bearer v2:…`. There is no request signing, no HMAC,
no API key, no AWS SigV4. The `HmacSHA` symbol in the APK belongs to **Instabug**
(crash SDK), not Weber. The native `libjsecclient.so` is a C++ BLE/Wi‑Fi
provisioning/security client used during setup — it does not sign cloud requests.

The blocker is **not** a missing algorithm. It's **appliance authorization
state**: our companion device currently has **no appliance associated** with it
server-side, so every appliance-scoped call is `403 "Not authorized"` and the
companion websocket accepts the upgrade (101) but streams nothing.

## The real auth model (recovered from the capture)

### 1. Companion device login (the bootstrap)
The app authenticates as a **companion device** using a device id + a device
password it generated at first run:

```
POST /2/devices/register
Content-Type: application/json
{
  "password":      "<DEVICE_PASSWORD_REDACTED>",   # 16-byte device secret
  "device_id":     "065d…",   # app-install id
  "client_secret": "<CLIENT_SECRET_REDACTED>",
  "client_id":     "qyw4CGeb/i93BrA0KAUuGtPyKImr+nUKc8lHxFdt",
  "device_name":   "<phone name redacted>",
  "device_type":   "companion",
  "platform":      "android", "platform_version":"33", "version":"2.10.1.2488"
}
```
Response (this **is** the token grant):
```json
{"success":true,"token":{"access_token":"v2:ccef…","refresh_token":"v2:8u19…",
 "token_type":"Bearer","expires_in":21599,"scope":""}}
```

So the **device password is the durable credential.** The refresh token is just
a renewal of it:
```
GET /2/auth/oauth/token?grant_type=refresh_token&refresh_token=v2:8u19…
    &client_secret=…&client_id=…
```

### 2. Content token (CDN), informational
`GET /content-token/token` → a JWT `{"sub":"<device_id>","exp":…,"group_ids":[]}`.
`group_ids` was **empty in the original working capture too**, so it is *not* the
gate.

### 3. Appliance association (the actual gate)
Appliances are linked to a **companion device**, discovered/claimed via:
- `GET  /2/devices/{companionId}/associated`   ← **lists the device's appliances**
- `GET  /2/devices/pairing`                     ← pairing
- `POST /2/devices/pairing/{pin}/companion`     ← claim an appliance with a PIN
- `GET  /device-registration/serial_details?serial_number=<serial>`  (serial → appliance)

Appliance data then lives at (all require the appliance to be associated):
- `GET /cook-history/1/appliance/{appliance_id}/sessions`
- `GET /cook-history/1/appliance/{appliance_id}/session/{session_id}/snapshots`
- `wss /2/messaging/websocket/companion` (live telemetry)

## Evidence the gate is association, not crypto

| Endpoint | In capture (app live) | Standalone now | Meaning |
|---|---|---|---|
| `/content-token/token` | 200 | **200** | token valid |
| `/2/devices/{id}` | (n/a) | **200** | device valid: `<phone name redacted>` |
| `/3/devices/{id}/tokens` | 200 | **200** | device valid |
| `/device-registration/serial_details?serial_number=…` | (n/a) | **400 "…no items for appliance serial"** | serial maps to **no appliance** |
| `/cook-history/.../sessions` | **200** | **403 Not authorized** | appliance not associated now |
| `/1/messaging/device/{id}/status` | **403** | **403** | always 403 (red herring) |
| `wss companion` | 101 **+ streams** | 101 **+ 0 bytes** | no association → no stream |

Same token (`v2:ccef…`), same appliance id, byte-identical websocket subscribe,
identical 101 handshake (no `permessage-deflate` negotiated in either) — yet
streaming worked then and not now. The only thing that changed is the
**server-side association/presence** of appliance ↔ this companion device.

## Why re-pairing in the app didn't fix our client

`serial_details` returns **no appliance** for the grill serial, and
`group_ids` is empty → as far as the cloud is concerned, **this companion device
has no appliance linked right now**. The app may be operating on a different
active token/session, or the association requires completing the
`/2/devices/pairing/{pin}/companion` claim (PIN comes from the grill during
setup). Our recovered credentials never performed that claim — they inherited an
association that has since lapsed.

## What to try next (in order)

1. `python tools/validate.py assoc` → calls `GET /2/devices/{device}/associated`.
   - If it lists an appliance → use that `appliance_id`; cook-history + ws should
     then work (run `temps --appliance <id>`).
   - If empty → the grill is genuinely not linked to this companion; it must be
     (re)claimed.
2. If empty, capture the app **pairing/claim** once (open app → add/ýre-pair the
   hub) so we can see `POST /2/devices/pairing/{pin}/companion` and replicate the
   claim with our device credentials.

## Credentials recovered (yours — keep private)

- device_id (companion):  `065d…`
- device password:        `<DEVICE_PASSWORD_REDACTED>`
- refresh_token:          `v2:<REFRESH_TOKEN_REDACTED>`
- client_id:              `qyw4CGeb/i93BrA0KAUuGtPyKImr+nUKc8lHxFdt`
- client_secret:          `<CLIENT_SECRET_REDACTED>`
- appliance serial:       `0713… (serial redacted)`
- appliance_id (pre-repair, now stale): `6f27…`

## Bottom line

The protocol and auth are fully understood; there is no hidden algorithm. A
standalone client can authenticate as the companion device (device_id +
password). The one remaining requirement for live data is that the **grill be
associated with that companion device** — an account/pairing state, not a
cryptographic secret. Confirm/repair that link via `assoc` and, if needed, the
pairing claim.

---

# RESOLVED auth model (verified live)

Verified end-to-end against the live cloud:

1. **Companion-device login works** — `POST /2/devices/register` with
   `{device_id, password, client_id, client_secret, device_type:"companion"}`
   returns a fresh OAuth token. This is the durable login (the refresh_token
   rotates/expires; the device password does not). Our client now uses this by
   default (`WeberConnectClient(..., device_id=, device_password=)`).
   - Confirmed: after the refresh_token went **401 (dead)**, device-password
     login still authenticated successfully (200).

2. **The grill is NOT linked to our companion device.**
   `GET /2/devices/{companion_id}/associated` → `{"success":true,"devices":[]}`.
   Empty ⇒ no appliance is associated with device `065d…`. This is the single
   reason every appliance call returns 403 and the websocket streams nothing.

3. **Association/pairing flow** (how an appliance gets linked to a companion):
   - `GET  /2/devices/pairing`
   - `POST /2/devices/pairing/{pin}/companion`  — claim; `{pin}` is the
     **verificationCode** produced by the hub's **Wi‑Fi setup / provisioning**
     (SoftAP) flow (`wifiSetup` / `Provision` / `verificationCode` in the APK).
   - `POST /2/devices/{companion_id}/dissociate` — unlink.
   - Hub firmware is **juneOS2** (internal sockets
     `/data/juneOS2/appliancemanager-{bluetooth,command}.sock`).

## Why re-pairing in the app didn't help our client
Our recovered companion (`065d…`) authenticates but has **no appliance linked**.
The app, after re-pairing, is operating against a companion identity that *is*
linked — either a different `companion_id`, or it re-ran the Wi‑Fi-setup claim
that our credentials never performed. Linking is **per companion device**.

## To make a standalone client work — two options
**A. Adopt the app's current linked companion.** Capture the app once while it's
working; read the **current** `device_id` + `password` (from
`POST /2/devices/register`) and the `appliance_id` (from any cook-history call or
the companion websocket). Put those in `secrets.local.json`. Our client then
authenticates and reads temps with no further work.

**B. Link our own companion device to the grill.** Drive the Wi‑Fi-setup
provisioning to obtain a `verificationCode`, then `POST /2/devices/pairing/
{code}/companion` with our device token. This makes `065d…` an owner; afterwards
`/associated` lists the grill and everything works. This requires putting the
hub into setup mode (hardware), so it's heavier than option A.

## Bottom line (final)
- Auth: **solved** (device-password companion login).
- Protocol/decoders: **solved** (REST temps + websocket telemetry).
- Remaining: the grill must be **associated** with the companion device the
  client uses. That's an account/pairing state, established via Wi‑Fi-setup
  provisioning — not a cryptographic secret. Option A (adopt the app's current
  linked credentials from one capture) is the fast path.
