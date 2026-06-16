# Home Assistant integration — design + operational notes

## What you configure

Only your **companion identity**, recovered once from a decrypted app capture:

- `device_id` — the app/companion install id (e.g. `065d…`)
- `device_password` — the durable secret for that companion
- (`client_id` / `client_secret` are app-global and baked into the integration —
  the same for every Weber user; not user-provided.)

Everything else is **auto-discovered at runtime**, so it survives re-pairs and
new cooks:

- **Appliance** (the grill): `GET /2/devices/{device_id}/associated` →
  `devices[].oven_id` (= appliance_id), plus `name`, `model_number`,
  `serial_number` (great for the HA device registry). The serial is *discovered*,
  not entered.
- **Active session**: the latest from
  `GET /cook-history/1/appliance/{oven_id}/sessions`, or off the companion
  websocket (it streams the active `session_id`).

## How it reads temperatures

- **REST cook-history polling (recommended for HA).** Poll
  `/cook-history/1/appliance/{oven_id}/session/{session_id}/snapshots?after_id=N`
  every ~5–10 s, where `N` is the highest `snapshot_id` seen. Append-only series;
  the hub writes a snapshot roughly every ~1.5 s. This is stateless/read-only, so
  it **coexists with the phone app** (no contention) and **backfills gaps**: on
  restart, resume from your saved `after_id` and you get every reading you missed.
- **Websocket push (optional).** `GET /2/messaging/websocket/companion` streams
  telemetry frames automatically after a subscribe. Lower latency, but it's a
  single messaging channel that can contend with the phone app and needs
  reconnect logic. Prefer REST polling for robustness.

Temperatures from both paths are **deci-Celsius** (`raw/10 = °C`,
`raw/10*9/5+32 = °F`). Expose one HA sensor per probe `index`; `temperature == 0`
or the websocket sentinel `-32768` means "no probe in that channel."

`tools/monitor.py` is a working reference of the polling + backfill loop.

## ⚠️ Operational caveats users must know

**Cloud data only exists while the hub holds a healthy cloud session.** This is
the #1 source of "it stopped working." Watch the hub's **wireless LED**:
**solid green = connected** (good); **blinking orange = needs setup**;
**blinking green = connecting**; **blinking red = error**; **off = hub off**. In
the Weber app a **gray cloud icon = not cloud-connected** → you'll get HTTP 403
and no data (the app itself is locked out too).

### Flaky WiFi (important)
The hub's WiFi is a cheap **2.4 GHz, 1×1, WiFi-4 (b/g/n) Murata radio**, and it
**flaps on modern WiFi/mesh systems** (notably UniFi). Symptoms: the hub keeps
re-DHCPing / re-ARPing and never holds a connection to `*.walker-cloud.com`, so
the cloud goes silent (the integration shows the grill unavailable). It can also
**hang for hours** — once observed stalled ~16 h until the WiFi was bounced.

To stabilize it, put the hub on a **dedicated 2.4 GHz IoT SSID** and on that
network **disable**:

- **WiFi-6 / 802.11ax**, OFDMA, MU-MIMO (force b/g/n)
- **Fast Roaming (802.11r)** and **BSS Transition (802.11v)**
- **Minimum RSSI** (it kicks the hub when idle/sleepy)
- Set **channel width = 20 MHz**, a clean low channel (1/6/11)
- Security **WPA2 only**, **PMF = Optional** (the WICED radio dislikes WPA3/PMF)
- If multi-AP, **lock the hub to the nearest AP** so it doesn't roam.

Healthy looks like a **sustained, two-way TLS connection** to a messaging IP
(`50.112.61.107` / `32.184.56.52` / `35.162.167.46`) that stays open — not a
"connect → 103-byte push → close after 20 s → retry" loop.

### Keep it powered
A solid **yellow** battery icon = low battery; **red** ≈ ≤5%. If the battery
dies the hub powers off → drops its cloud session → no data. For continuous
monitoring, **leave the hub on the charger** (you can charge and monitor at once).

### Pairing
The hub binds to one companion at a time. A **factory reset (Reset Port pinhole,
in the middle between the two stickers)** unpairs it from everyone — including
this integration — and re-pairing must be done from the phone app (BLE/WiFi
setup). After a normal re-pair the appliance id is stable (serial-derived), so
the integration keeps working; if it ever changes, re-discovery via `/associated`
picks it up automatically.

## Decoded fields (from live experiments)

### Companion-websocket frames — `payload = 0x0a <cmd> <TLVs>`
Decoder: `weber_connect.protocol.parse_appliance_status(payload)`.

**`cmd 0x80` — per-probe status** (one `0x04` sub-message per *connected* probe):

| TLV | meaning |
|-----|---------|
| `0x01` | probe index (0-based) |
| `0x0a` (i16) | current temp, **deci-°C** (`-32768` = no reading) |
| `0x0c` | **state: `2`=connected/no-target, `9`=cooking (below target), `7`=done (at/above target)** |
| `0x05` (i32) | timer remaining (ms) |
| `0x06` (i32) | elapsed (ms) |
| `0x0e` (8 B) | present only when a target is set; a time/counter, **not** the target value |
| *block absent* | probe disconnected / out of range → mark sensor unavailable |

**`cmd 0x83` — device info:** `name`, `serial_number`, `ssid`, `wifi_mac`,
`firmware`, **`rssi` (dBm)**, `battery` (%).

**Not available over the cloud:** the probe **target/setpoint value**. The hub
telemeters current temp + doneness *state* only; the setpoint stays app-side.
"Done" is therefore derived from the `0x0c` state, not a target comparison.

## Suggested HA entities

Per probe (index 0..N): a **temperature** sensor (REST cook-history, robust) and —
if you also run the websocket — a **doneness** state (`cooking`/`done`) and a
**timer** (s). Plus device-level (websocket `0x83`): **WiFi signal (RSSI)**,
**firmware** version, **battery** %. A binary "hub online" follows from whether the
appliance is reachable / a session is active. v1 of the bundled custom component
uses REST temps (no contention, survives the phone app running); the doneness/
timer/RSSI sensors are a documented websocket phase-2 enrichment (the
`parse_appliance_status` decoder is ready for it).
