# Weber Connect — Independent Revalidation (2026-06-16)

Second pass: re-tested every prior claim against live cloud + re-decrypted the
captures from scratch. **One major prior conclusion was wrong.** Details below.
This doc supersedes `HANDOFF.md §5` and the "session-ownership" / "association"
theories.

## What held up (re-verified)

| Claim | Status | Evidence |
|-------|--------|----------|
| Offline decoders (HTTP/ws/TLV) correct | ✅ holds | `python tests/test_offline.py` → all pass; ws fixture decodes 91 readings 229–283 |
| Device-password login works | ✅ holds | `validate.py token` minted fresh `v2:…` token live today |
| Refresh-token login works | ✅ holds | offline oauth decode + live token mint |
| Auth is bearer-only, no signing/HMAC | ✅ holds | every captured request carries only `Authorization: Bearer v2:…` |
| REST cook-history shape | ✅ holds | `snapshots[].data.probe_status[] = {index, temperature}` |
| `client_id`/`client_secret` app-global | ✅ holds | identical across all streams |

## What was WRONG in the prior work

### 1. The 403 "blocker" is grill power / active-cook state — NOT a session/ownership/association gate.

`HANDOFF.md §2` claimed the **6/16 07:47 + 07:50** captures show the app reading
`6f27…`/session `53fd…` and **getting 200**, "disproving the stale theory." That
is the opposite of what the bytes say. Re-decrypted with `tools/extract_http.py`:

| Capture | Grill | App's cook-history result | App's ws telemetry |
|---------|-------|---------------------------|--------------------|
| `PCAPdroid_15_Jun_21_12_18` (golden, active cook) | **ON / cooking** | **200 ×6**, gzip snapshot JSON, paging `after_id` 1084→4681 | real frames (229→283°F) |
| `PCAPdroid_16_Jun_07_47_15` (morning) | idle/off | **403 ×3** | `\x8a\x00` pongs only, no data |
| `PCAPdroid_16_Jun_07_50_32` (morning) | idle/off | **403 ×3** | none |

Same companion device (`065d…`), same appliance (`6f27…`), same session
(`53fd…`), same bearer family (`v2:ccef…`/`v2:LbVK…` both minted from `065d`).
**The only variable is whether the grill was actively cooking.** When idle, the
**app itself** gets 403 on cook-history and 403 on `/1/messaging/device/{id}/status`,
and its websocket streams nothing.

This invalidates `HANDOFF.md §5`'s four "ruled out" items and both working
hypotheses (session-ownership binding; active-presence requirement). The PC client
got 403 because it was always tested while the grill was off — and so would the
phone. The earlier "app real token returns 403 from PC" test used the **morning
(idle) token**, when the app got 403 too.

### 2. `/2/devices/{id}/associated` being empty is a red herring.

The golden (fully-working) capture **never calls `/associated`, `/pairing`, or
`/sessions`.** The app reads cook-history directly from `appliance_id` +
`session_id`. So `{"devices":[]}` says nothing about whether data will flow.
`APK-ANALYSIS.md`'s "association is the gate" conclusion is wrong.

### 3. Temperature unit is °F (cloud path). Resolved.

Grill channel holds **227–230** across 1000 snapshots — the textbook low-and-slow
smoker band (225–230°F ≈ 109°C). Deci-°C would be ~22.8°C (room temp), impossible
for an active cook. The deci-°C note came from the **BLE** project (different
transport) and does not apply to the cloud REST/ws integers.

### 4. `POST /2/devices/register` mutates server state (side effect).

Our `client.register_device()` sends `device_name:"weber_connect"`; after running
it, `GET /2/devices/065d…` now returns `"name":"weber_connect"` (was
`"<phone name redacted>"`). The app instead updates via `POST /2/devices/{id}` (id in
path, bearer-auth, no password). Harmless but: our login rewrites the device name.
Consider sending the real device name, or switch to refresh-token login to avoid
touching it.

## Corrected blocker model

Cloud appliance data (REST cook-history + companion websocket) is gated on the
**hub being online / in an active cook**, not on the caller's identity. A
clean-room client authenticated as `065d…` should get 200 **whenever the grill is
on** — exactly when HA needs the data.

Not yet distinguished: "hub powered off" vs "hub on but idle (no active session)".
Needs one capture/test with the hub powered on but not cooking.

## The one decisive test left

Turn the grill ON (start a cook / get it online), then immediately run:

```
python tools/validate.py temps --appliance 6f27… \
    --session <current_session> --seconds 15
```

(or just `diag`, watching the `/cook-history/.../sessions` row). Expected: **200**
and live probe temps. If so, the cloud path is fully unblocked and the HA
integration is just the polling loop already sketched in `PROTOCOL.md`.

If even an active-cook grill returns 403 to the PC client, *then* re-open the
session/presence hypotheses — but the capture evidence says it won't.

## ADB live finding (2026-06-16): the app's primary local transport is BLE

Phone = Samsung SM-G986U (S20+), production build, no root. Launched
`com.weber.connect` via adb with the hub powered/idle and watched logcat:

```
BtGatt.GattService: writeCharacteristic() ... address=70918F_D   (repeated)
JoslSecurityLib: wrapOrUnWrapMessage [70:91:8F:4D:A4:AD] ... Encrypt/Decrypt
com.weber.sdk.cloud.logging.LogUploadWorker ... (cloud used only for log upload)
```

- The app talks to the hub over an **encrypted BLE GATT session** (juneOS "JOSL"
  security lib) to hub MAC **`70:91:8F:4D:A4:AD`** — *while the cloud cook-history
  is returning 403*. So the hub is online and serving data **locally over BLE**,
  not via the cloud, when idle.
- This explains "always on but cloud 403": the hub does **not** continuously relay
  to walker-cloud. The cloud path activates during an active cook; BLE is the
  always-available local transport (matches the ProspectOre add-on, which is
  cloud-free).

### Net: two viable transports, different trade-offs
- **Cloud (REST cook-history / companion ws):** works **during an active cook**;
  good for away-from-home. Validated except the final live-during-cook 200.
- **Local BLE:** works **anytime in range**, no cloud/account, read-only telemetry.
  Single-connection hub (phone app must be disconnected). Needs BT in range of the
  grill. ProspectOre already implements pairing (P-256 ECDH) + status decode.

### Temperature unit — still needs one live cross-check
Cloud REST `temperature` held 227–230 (a 225–230°F smoker setpoint → almost
certainly °F). ProspectOre says the **BLE** raw TLV field 10 is deci-Celsius.
These are different transports; the cloud likely normalizes to °F. Settle it by
comparing the app's on-screen °F to the raw cloud/BLE value during one live cook.

## Live ADB tests (2026-06-16, hub powered, probes attached) — CLOUD IS NOT PUSHING

Used PCAPdroid (ADB-controlled) + a per-host s2c volume heuristic (a 200 returns
KB of snapshot JSON / streaming; a 403 is ~empty). Reference: GOLDEN active cook =
api 57 KB / messaging 28 KB. Idle = api ~9 KB / messaging ~6 KB.

| State (all today) | api.walker-cloud.com s2c | messaging s2c | Verdict |
|---|---|---|---|
| Idle morning (6/16 07:47) | 8.9 KB | 6.0 KB | app got 403, no stream |
| **Live now, grill on** | 10.3 KB | 6.0 KB | idle-level → no cloud data |
| **Phone Bluetooth OFF 2.5 min** | (polled) 403 ×11 | — | hub still didn't stream |
| **Guided cook started** | 9.8 KB | 6.0 KB | idle-level → app got 403 too |
| GOLDEN (6/15 active cook) | 57.5 KB | 28.3 KB | streaming, 200s |

**Conclusion:** In the user's current environment the hub serves the app over
**local BLE only** (confirmed via logcat: `JoslSecurityLib`/`BtGatt` to hub MAC
`70:91:8F:4D:A4:AD`) and does **not** maintain a walker-cloud streaming session —
not when idle, not with the phone off BLE, not during a guided cook. The PC
client's 403 is therefore correct, not a bug: there is no cloud session for
anyone. The only capture that ever showed cloud data is the 6/15 golden one.

**Leading explanation:** the hub had an active cloud connection on 6/15 (it
streamed telemetry + wrote cook-history) but is not maintaining one now. The
"WiFi light" = joined the local AP, not necessarily an active connection to
`messaging.walker-cloud.com`. A hub power-cycle / re-provision to force a fresh
cloud connection is the last cloud lever to try.

### Decryption note
ADB-triggered PCAPdroid captures show "Mitm addon is running" but the walker-cloud
streams do NOT decrypt (0 keylog keys; no plaintext) — the no-pin patch evidently
doesn't cover every pinned/native path on this build, OR the mitm cert is rejected
for these hosts. The morning/golden `.pcapng` files DID carry a keylog DSB, so
those were decryptable. Use the volume heuristic (`tools/stream_sizes.py`) when
decryption isn't available.

### Hub-far test (2026-06-16, decisive): app forced to cloud still gets nothing
Hub carried 3 rooms + a floor away (out of BLE), still on WiFi; phone here on ADB.
App restarted so it could not use BLE. Over 45 s the app received: `api` 3.5 KB/19
rec (just content-token + 403s), `messaging` 6.7 KB/52 rec (idle ping/pong, no
telemetry). PC client: 24/24 poll rounds 403. **Even when the app has no local
option, the cloud has no data** — disproving "the app merely prefers BLE." The
hub does not maintain/establish a walker-cloud streaming session in this
environment, full stop. Only the 6/15 golden capture ever showed otherwise.

Why 6/15 worked is unresolved (hub had a live cloud session then). A reboot did
not restore it. Possible: the hub only relays to cloud under a condition we can't
reproduce (e.g. app-commanded "remote monitoring" handoff), or the network now
blocks the hub→walker-cloud path, or the hub silently dropped cloud and won't
re-establish without re-provisioning. Not worth more cloud poking without a new
lever.

## Bottom line for the HA goal
- **Reverse engineering: complete and correct.** Protocol, auth, decoders, and the
  websocket subscribe are all validated against the golden cook.
- **Cloud path: not viable in this environment** unless the hub can be made to
  stream to walker-cloud again (try a hub reboot). It is gated entirely on the hub
  pushing — nothing the PC client does can change that.
- **BLE path: the reliable transport** (what the hub actually uses 24/7), but needs
  Bluetooth in range of the grill. The PC has none → use an ESP32 BLE proxy or run
  on a Pi/HA box near the grill (ProspectOre add-on already implements it).
