# Review: ProspectOre/weber-connect-home-assistant-addon

Reviewed commit on `main` (v0.1.0, Jun 2026). Files of substance:
`weber_connect_ble/app/{saber_frames,weber_ble_pair,weber_ble_scan,weber_status_bridge,weber_panel}.py`,
a static HTML ingress panel, HA add-on `config.yaml`, contract tests, CI.

## What it is
A **local, read-only BLE bridge**: it connects to the Weber Connect Hub over
Bluetooth LE, reads probe/appliance status, and publishes Home Assistant MQTT
discovery + state. **No Weber cloud, no account, no tokens.** One-tap pairing
(press the button on the hub). This is exactly the end goal we were chasing —
probe temps in HA — but via BLE instead of the cloud.

## Bottom line / recommendation
**This is the path to use.** It sidesteps the entire cloud-authorization wall we
hit (device→appliance association, 403s, presence gating). Our cloud work wasn't
wasted — it independently decoded the *same* protocol — but for getting temps
into HA, this add-on already does it locally and correctly. I'd recommend:
adopt/run this add-on (or fork it), and fold our findings in where they add
value (notably the cloud path, if you ever want away-from-home access).

## It validates our reverse engineering
The hub speaks the **same "appliance payload" TLV protocol over BLE as over the
cloud websocket.** Their `saber_frames.py` and our `protocol.py` agree:
- Message = `[message_version, type, payload]`; payload is 1-byte-tag/1-byte-len
  **TLV** — identical to what we found.
- The command types match our captured websocket subscribe bytes exactly:
  `0x05 FETCH_STATUS`, `0x07 FETCH_APPLIANCE_STATUS`, `0x09 SET_DEVICE_SETTINGS`
  (the one carrying the unix timestamp), `0x0E FETCH_APPLIANCE_CAPABILITIES`,
  `0x0B FETCH_PROGRAM_DETAILS`. Our `0a 05 / 0a 07 / 0a 09…` frames were these.
- So Weber's cloud websocket is essentially **proxying the BLE appliance frames**
  — same wire format, two transports. Good cross-confirmation.

## Corrections to our decode (defer to theirs — it's authoritative)
1. **Probe temperature unit:** probe temp is TLV **field 10 = deci-Celsius**
   (`value/10 = °C`, then ×9/5+32 = °F), sentinel `-32768` = no reading. Our
   cloud notes called field-10 values "°F"; that needs revisiting. The hub also
   exposes explicit cavity fields: **14 = display °F, 15 = display °C**, and
   deci-C cavity temps at fields 1/2/13.
2. **Richer per-probe fields** we hadn't mapped: slot_index(1), session_id(2),
   state(12), probe_type(19/4), battery(22), case_temp(24), ambient_temp(25),
   segment_temps(23, list), time remaining/elapsed, serial(20), sku(21).
3. **Envelope framing:** there's a transport envelope with a **CRC-8/MAXIM-DOW**
   (poly 0x8C) footer and a `verification_code` — the same code concept as the
   cloud's `/2/devices/pairing/{pin}/companion`.

## The pairing/crypto (the piece we couldn't do via cloud)
- **NIST P-256 (SECP256R1) ECDH.** The companion generates a keypair; public key
  is the 64-byte X‖Y point.
- Flow: `0x70 HANDSHAKE_GREETING` (companion_id[16] + 32-byte nonce) → app sends
  `0x0A PAIRING_REQUEST` (companion_id + companion_pubkey[64] + display name) →
  hub emits `0xF1 PAIRING_REQUIRED` (press the hub button) → on confirm,
  `0xF2 HANDSHAKE_SUCCESS` and `0x85 PAIRING_RESPONSE` carrying the
  **appliance_id + appliance public key**.
- That pairing response is how you learn the `appliance_id` locally — the value
  the app never shows and the cloud wouldn't give us. ECDH(our priv, hub pub) is
  the basis for the encrypted "JOSL" secure session.
- **Caveat:** they do **not** implement the JOSL secure-session decryptor;
  encrypted response bodies are left as ciphertext. Status/probe reads work over
  the unencrypted "null session," which is enough for telemetry but means
  **commands (start cook, setpoints, Wi-Fi config) aren't available** — it's
  read-only by design.

## Engineering quality — solid
- Clean module split; `bleak` (BLE), `cryptography` (P-256), `paho-mqtt`.
- Proper HA add-on `config.yaml`: ingress panel, `host_dbus`, `udev`,
  NET_ADMIN/NET_RAW, `mqtt:want` service discovery, atomic JSON writes, secrets
  in `/data` at 0600.
- Contract tests (`tests/`) assert probe-slot state mapping, pause/handoff
  behavior; CI + CodeQL configured.
- Thoughtful UX for the hub's **single-BLE-connection** limit: the hub serves
  one connection and won't advertise while connected, so the panel has a
  **"Use with Phone"** handoff that disconnects for a window so the Weber app can
  attach, then auto-reconnects. This is the key operational constraint to know.

## Limitations / risks
- Experimental (v0.1.0, low adoption). Read-only (no control).
- Requires a **Bluetooth adapter on the HA host within BLE range of the grill.**
  If your HA box isn't near the grill (your earlier "Bluetooth won't work"),
  the standard fix is an **ESPHome Bluetooth Proxy** (an ESP32 near the grill
  relays BLE to HA over Wi-Fi) — HA supports this natively and `bleak` via the
  HA Bluetooth stack can use it. Worth confirming the add-on uses HA's Bluetooth
  (it talks D-Bus/BlueZ directly here, so a proxy may or may not be transparent —
  test, or run the add-on on a host with a local adapter).
- Phone-app contention: while the add-on holds the connection, the phone app
  can't see the hub (and vice-versa).
- JOSL encryption unimplemented → no secure commands; relies on status frames
  staying readable in the null session.

## How this maps to our repo
- Our `protocol.py` TLV decoder is correct in shape; **align the temperature
  units to deci-Celsius** and add the richer field map above.
- Their BLE `appliance payload` == our cloud websocket payload, so a single
  `parse_appliance_status(tlv)` could serve both transports.
- If away-from-home (cloud) access is ever wanted, our cloud auth findings
  (device-password companion login + the association requirement) remain the
  reference — but local BLE is the better foundation and needs none of it.
