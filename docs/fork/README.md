# Fork feature reference

Everything this fork adds to upstream OpenHop Repeater, in one place. Each feature lists:
- what it does;
- how to turn it on;
- its default;
- where the detailed doc and the code live.

All features are opt-in. Changes to anything in `channel_bridge`, `fabric` or `radios` take effect after a restart. The exceptions are live pause, CAD mode and Internet Link on/off.

- [Bridging](#bridging)
  - [Channel translation](#channel-translation)
  - [Protocol transit](#protocol-transit)
  - [Bridge safeguards and live pause](#bridge-safeguards-and-live-pause)
  - [Sender markers](#sender-markers)
- [Multi-radio](#multi-radio)
  - [Network topology, receivers and shadows](#network-topology-receivers-and-shadows)
  - [Ordered dual TX](#ordered-dual-tx)
  - [Antenna identities](#antenna-identities)
- [Radio control](#radio-control)
- [Observability](#observability)
- [Integrations](#integrations)
  - [Internet Link (iLink)](#internet-link-ilink)
  - [Bot RF receipts](#bot-rf-receipts)
- [Fixes to inherited behaviour](#fixes-to-inherited-behaviour)
- [Code map](#code-map)
- [Web API](bridge-api.md) · [Syncing with upstream](upstream-sync.md)

---

## Bridging

All bridge settings live under `channel_bridge`. The bridge is off unless `channel_bridge.enabled: true`. Native OpenHop routing always runs first: each network keeps repeating its own traffic, and the bridge only adds a separate crossing copy where a rule allows it.

### Channel translation

The bridge authenticates and decrypts a group text message with the source channel key, then re-encrypts it with the destination channel key and sends it on the other network.

```yaml
channel_bridge:
  enabled: true
  channels:                 # name -> 32 or 64 hex key. Write-only through the API.
    Public: "…"
    "#910public": "…"
  mappings:                 # directional; add the reverse for replies
    - source_radio: host-rf
      target_radio: bay
      source_channel: Public
      target_channel: "#910public"
      enabled: true         # optional; false keeps the rule but stops crossings
  disabled_channels: []     # hold a channel's rules without deleting its key
```

- Only ordinary group text in flood packets is translated. Signed group subtypes, direct messages and control traffic never take this path.
- A channel named by a rule on a side, as source or target, is under explicit control there. It never also crosses as an opaque copy, so a one-way mapping stays one-way.
- **Not end-to-end:** the repeater handles plaintext while it translates. Channel authentication proves a channel member sent the message, not which member.
- Code: `repeater/channel_bridge.py` (planning, validation, echo ledger), `repeater/engine.py` (`_schedule_bridge_plan`), `repeater/web/bridge_settings.py`.

### Protocol transit

Traffic crosses the bridge unchanged and still encrypted, on explicit direction lists:

| Setting | What crosses | Default |
|---|---|---|
| `direct_routes` | Explicit-path DMs and ACKs | none |
| `peer_routing: distinct` | Separates local hops from crossing hops with a public route marker. Required by the three settings below. | `legacy` |
| `protocols.discovery` | Unscoped flood DM, ACK and PATH, so endpoints can learn routes across the bridge | false |
| `protocols.repeater_admin` | Encrypted requests and responses to remote repeaters | false |
| `protocols.trace` | TRACE with SNR progress | false |
| `packet_types.<type>.flood` and `.direct` | Per-type overrides | inherited |

Changing `peer_routing` requires a restart, and contacts must relearn their paths. Details: [bridge-protocol-routing.md](../bridge-protocol-routing.md).

### Bridge safeguards and live pause

These are on whenever the bridge is enabled:
- per-direction flood budgets (`traffic_limits.per_minute`, `burst`, `max_pending_per_direction`);
- a queue limit (`max_pending_crossings`) and expiry (`queue_ttl_seconds`, default 30);
- a durable SQLite echo ledger (`echo_database`), so a crossed packet heard coming back is never crossed again, even after a restart.

Crossings use the normal radio queues and airtime accounting. Your own messages delayed by the airtime limit can wait longer than `queue_ttl_seconds`; crossings cannot.

**Pause** (`POST /api/update_bridge_controls {"paused": true}`) is saved first, then applied live without a restart. It also drops crossings already queued. Native local repeating continues. Details: [bridge-reception-evidence.md](../bridge-reception-evidence.md).

### Sender markers

`text_prefix` on a mapping adds a visible marker to translated messages, for example `↔ Dan: hello`. Limits:
- up to 32 UTF-8 bytes;
- never truncates: if the result would exceed MeshCore's 160-byte text limit, the crossing is skipped instead;
- not added twice when a message already carries the marker.

Details: [bridge-text-markers.md](../bridge-text-markers.md).

---

## Multi-radio

### Network topology, receivers and shadows

`fabric.topology` describes two logical networks. Each network has one designated transmitter, and any number of extra radios with one of these roles:
- `receiver`: forwards what it hears, but only through its network's transmitter;
- `shadow`: listens for comparison only and never causes a transmission or reply.

```yaml
radios:
  - {id: host-rf, network: SMF, role: transceiver, radio: {...}}
  - {id: rak909,  network: SMF, role: receiver,    radio: {...}}
  - {id: bay,     network: SFO, role: transceiver, radio: {...}}
fabric:
  tx_mode: sticky            # default with topology; repeats stay on the ingress network
  topology:
    networks: {SMF: host-rf, SFO: bay}
    enable_receiver_forwarding: true
```

Every reception keeps its physical radio id ("provenance") and its network. Duplicate suppression, echo detection and companion receive filters are per network. Radios on one network must share air settings; the two networks must use different frequencies.

Details:
- [logical-receivers.md](../logical-receivers.md);
- the topology and receiver design in core: `openhop_core/rf_fabric/topology.py` and `logical_receiver.py`.

### Ordered dual TX

`fabric.topology.tx_groups` sends one logical packet through two antennas on the same network, one after the other, in a configured order:

```yaml
fabric:
  topology:
    tx_groups:
      bay: [rak910-shadow, bay]   # primary must be included; at most two radios
```

- Each leg goes through the normal TX lock, airtime ledger and duplicate checks.
- If the first leg's outcome is uncertain, the remaining legs are not sent.
- `tx_group_scopes: {bay: crossings}` limits the group to **bridged packets only**. Packets crossing in from the other network go out on both antennas for extra reach, while ordinary local repeats use the primary alone, so local airtime is unchanged. The default is `all`. See [bridged packets only](../ordered-dual-tx.md#bridged-packets-only).
- Configure it in the web UI (Bridge settings → Sequential transmitters) or through `POST /api/update_tx_group`. It takes effect after a restart.

Details: [ordered-dual-tx.md](../ordered-dual-tx.md).

### Antenna identities

`tx_identity_experiment` replaces this node's last hop in repeated floods with an alias. In the default `rx_tx` mode the alias names the antenna that heard the packet and the one that sent it.
- Aliases are derived from the node key, so they are stable and need no key files.
- Only 2- and 3-byte paths are tagged.
- Routes that other nodes learn through an alias resolve back to this node.
- The alias table appears under Bridge settings → Activity, in `GET /api/bridge_diagnostics`, and in the startup log.
- Turn it off, or switch modes, under **Bridge settings → Transmitters → Antenna identities**. It applies on restart.

Details: [tx-identities.md](../tx-identities.md).

---

## Radio control

| Feature | How | Doc |
|---|---|---|
| CAD on/off per radio | CAD Calibration page, `POST /api/set_cad_enabled`, or CLI `set cad on\|off RADIO_ID`. Applied only after the modem acknowledges; saved as `radios[].radio.cad.enabled`. | [cad-mode.md](../cad-mode.md) |
| CAD calibration per radio | `radio_id` on every calibration endpoint; thresholds saved per radio | [per-radio-cad.md](../per-radio-cad.md) |

When a modem's channel-activity check fails or times out, the packet is still sent, as upstream does. The USB serial session reconnects only after three CAD timeouts in a row. That behaviour lives in core.

---

## Observability

| Feature | Config | Doc |
|---|---|---|
| Per-radio MQTT observer identities | `mqtt_brokers.separate_observer_identities`, `observer_identities` | [separate-observer-identities.md](../separate-observer-identities.md) |
| One MQTT stream per network (logical receivers) | `mqtt_logical_observers` | [logical-receivers.md](../logical-receivers.md) |
| Per-radio RF receive age | always on; `rf_receive_ages` in stats, shown on the dashboard | n/a |
| Independent reception evidence | `channel_bridge.reception_evidence` | [bridge-reception-evidence.md](../bridge-reception-evidence.md) |
| Bridge diagnostics | `GET /api/bridge_diagnostics` | [bridge-api.md](bridge-api.md#diagnostics) |
| Transmit outcomes per leg | in packet egress records and stats | [ordered-dual-tx.md](../ordered-dual-tx.md) |

MQTT credentials refresh before they expire, and connections retry after a failed first handshake, for every broker.

---

## Integrations

### Internet Link (iLink)

`private_island` connects compatible MeshCore Observer firmware (V4.3 TFT/OLED) over TLS for approved routes with 2- or 3-byte paths. It adds to RF and never replaces it.
- The sidecar and its enrollment server live in [kesh-openhop-bridge](https://github.com/kesh33w/kesh-openhop-bridge). This repo has the integration hooks and the Bridge settings controls (`GET /api/internet_link`, `POST /api/update_internet_link`).
- `private_island.enabled` must be the boolean `true` or `false`.
- If the link fails, local RF startup continues.
- Settings are saved off the radio loop while holding the enrollment server's lock, so a peer enrolled at the same moment is never lost.

### Bot RF receipts

`bot_rf_receipts` exposes a private, authenticated Unix socket. A local chat bot uses it to confirm it has seen raw RF receptions before it falls back to its own handling.
- Only a client that proves the shared secret takes the single session slot.
- On any doubt the stream fails closed.

Details: [bot-rf-receipts.md](../bot-rf-receipts.md).

---

## Fixes to inherited behaviour

The fork changes some upstream behaviour that its features rely on, and fixes defects it found along the way. Highlights (full list in the [CHANGELOG](../../CHANGELOG.md)):

- **Transmit outcomes** distinguish *confirmed*, *definitely not sent* (safe to retry) and *uncertain* (never retried). Suppressed duplicates don't count as failures.
- **Config saving**:
  - A file-backed `identity_key` is never written into `config.yaml`, and a mismatch with the key file no longer blocks saves.
  - CAD edits are staged on a copy and applied only after a successful write.
- **Replies** (discovery, guest, admin) go back out on the radio the request came in on.
- **Companions** have an explicit home radio and receive filter.
- **MQTT** pins the paho callback API version and reloads rotated subscriber credentials.
- **Stats** keep working on installs without the hardware extra.

## Code map

| Concern | Where |
|---|---|
| Packet processing, duplicate suppression, scheduling | `repeater/engine.py` |
| Bridge planning, translation, echo ledger | `repeater/channel_bridge.py`, `bridge_protocols.py`, `bridge_traffic.py`, `bridge_evidence.py`, `bridge_homes.py` |
| Topology from config | `repeater/network_topology.py`, `repeater/config.py` (`validate_fabric_config`) |
| Ordered TX, antenna identities | `repeater/ordered_tx.py`, `repeater/tx_identities.py` |
| Web API | `repeater/web/api_endpoints.py`, `bridge_settings.py`, `bridge_controls.py`, `internet_link.py`, `tx_group_settings.py`, `openapi.yaml` |
| MQTT observers | `repeater/data_acquisition/mqtt_handler.py`, `observer_identity.py`, `logical_observer.py`, `bridge_subscriber.py` |
| Bot receipts | `repeater/bot_rf_receipts.py` |
| Bundled web UI | `repeater/web/html/` (built from openHop_RepeaterUI; do not edit by hand) |
