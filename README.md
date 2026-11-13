# Kesh's OpenHop Repeater bridge fork

This is Kesh's fork of [OpenHop Repeater](https://github.com/openhop-dev/openhop_repeater), the Python MeshCore repeater daemon. It adds opt-in channel translation between two radios while retaining native local repeating. Kesh uses it on a Raspberry Pi with radios on different frequencies.

Start with the [Kesh OpenHop bridge project hub](https://github.com/kesh33w/kesh-openhop-bridge) for the system overview, routing diagram, compatible source pins, reconstruction instructions, and recorded validation. This repository contains the Repeater daemon changes on `kesh/bridge`.

## What this fork adds

Native OpenHop routing runs first. For eligible group text, the bridge can repeat the original encrypted payload locally and create a separate copy for a mapped radio and channel. It authenticates and decrypts with the source channel key, then re-encrypts with the destination channel key.

- Channel mappings are directional. Configure a reverse mapping for replies.
- Optional per-mapping `text_prefix` adds a visible bridge marker, for example `↔ Dan: hello`. See [marker configuration and limits](docs/bridge-text-markers.md).
- Local and crossing policies are independent. Dropping one copy does not cancel an allowed copy on the other path.
- Translation accepts ordinary group text in flood packets. Signed group subtypes, direct messages, and control traffic do not use this translation path.
- Separately enabled routes forward explicit-path direct messages and acknowledgements without decrypting them. Optional discovery and repeater-administration settings require explicit distinct peer routing and add flood path learning, encrypted requests/responses and reciprocal PATH transit. TRACE has its own SNR-progress handling. See [protocol coverage and limits](docs/bridge-protocol-routing.md).
- Bridge sends use native radio queues and airtime accounting. Queue limits, expiry, and bounded retries distinguish known-unsent packets from uncertain transmission outcomes.
- Durable echo reservations cover original and translated packet hashes across restarts.
- Per-direction flood budgets bound crossing traffic without spending the other direction's budget. Optional [independent reception evidence](docs/bridge-reception-evidence.md) distinguishes modem TX confirmation from destination-side RF observation.
- Daemon origins and companions have explicit home radios. Companion receive filters retain radio provenance.
- Saved routing settings require a restart. Live pause/resume is persisted and applied without restart. Pausing invalidates queued crossings. Distinct-mode native local repeats continue; legacy direct routes retain their crossing-only semantics. Already-dispatched radio sends may finish. Channel keys are write-only through the settings API.

The bridge requires authorized keys for both channels and handles plaintext during translation. Encryption across distinct channel keys is not end-to-end. Channel authentication does not verify an individual sender's identity.

OpenHop's radio backends, dashboard, policy engine, and other inherited capabilities remain upstream work. The [preserved upstream README](UPSTREAM-README.md) documents that background.

## Optional iLink

iLink, or Internet Link, connects the Pi to Kesh's MeshCore Observer-based V4.3 TFT and OLED firmware over TLS. Kesh uses it as a temporary multibyte compatibility workaround for an older repeater in his installation.

The current sidecar and endpoints admit only eligible packets with two- or three-byte path hashes on approved iLink routes. One-byte traffic stays on normal RF. Multibyte traffic also retains normal RF forwarding under its own policy. This does not provide automatic fallback selection or guaranteed delivery.

This Repeater fork contains the integration hooks and bounded Internet Link status and controls. The Python `private_link` sidecar and enrollment source live in the [project hub](https://github.com/kesh33w/kesh-openhop-bridge). The TFT and OLED firmware are separate components, as is the OpenHop USB bench modem. Optional link failures do not prevent local RF startup.

## Source and component boundaries

| Component | Fork source | Role |
| --- | --- | --- |
| Repeater daemon | [This branch](https://github.com/kesh33w/openhop_repeater/tree/kesh/bridge) | Channel translation, routing, queues, bridge settings, and iLink integration |
| Core | [openhop_core, kesh/bridge](https://github.com/kesh33w/openhop_core/tree/kesh/bridge) | Radio provenance and transmission outcomes |
| Repeater UI | [openHop_RepeaterUI, kesh/bridge](https://github.com/kesh33w/openHop_RepeaterUI/tree/kesh/bridge) | Bridge configuration and Internet Link controls |
| iLink sidecar and enrollment | [Project hub](https://github.com/kesh33w/kesh-openhop-bridge) | Python link service, enrollment, and integration tests |
| USB bench modem | [openhop_modem, kesh/heltec-bench](https://github.com/kesh33w/openhop_modem/tree/kesh/heltec-bench) | Separate Heltec T096 and V4.3 USB bench targets |
| V4.3 OLED iLink firmware | [MeshCore, kesh/v43-oled-ilink](https://github.com/kesh33w/MeshCore/tree/kesh/v43-oled-ilink) | Standard V4.3 OLED endpoint with phone Wi-Fi setup and enrollment |
| V4.3 TFT iLink firmware | [MeshCore, kesh/v43-tft-ilink](https://github.com/kesh33w/MeshCore/tree/kesh/v43-tft-ilink) | MeshCore Observer-based TFT endpoint with Wi-Fi setup and enrollment |

Useful implementation entry points are [channel translation and validation](repeater/channel_bridge.py), [native send handling](repeater/engine.py), [bridge settings](repeater/web/bridge_settings.py), and [Internet Link controls](repeater/web/internet_link.py). The [tests](tests) cover bridge planning, direct routes, home assignments, retries, and echo handling.

## Reconstruct and test the fork

Use the hub's [pinned reconstruction workflow](https://github.com/kesh33w/kesh-openhop-bridge#reconstruct-and-test) and [components.current.json](https://github.com/kesh33w/kesh-openhop-bridge/blob/kesh/openhop-bridge/components.current.json). This is a dated source snapshot, not necessarily the latest branch tips or the software installed on a Pi. The original `components.json` remains available for historical reconstruction. Materialization checks out those exact commits without deploying services or using hardware.

For a new reconstruction workspace, use Python 3.14 for the hub's recorded backend environment:

```sh
git clone --branch kesh/openhop-bridge --single-branch https://github.com/kesh33w/kesh-openhop-bridge.git
cd kesh-openhop-bridge
python3 tools/materialize.py --manifest components.current.json
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pip install --no-deps -e sources/openhop_core -e sources/openhop_repeater
PYTHONPATH=.:sources/openhop_core/src:sources/openhop_repeater:tools .venv/bin/python -m pytest -p bench_test_guard private_link/tests tests -q --timeout=10
```

The hub tests use simulated radios and loopback sockets. Follow its [UI build and test instructions](https://github.com/kesh33w/kesh-openhop-bridge#repeater-ui) for the matching frontend, and the [TFT build guide](https://github.com/kesh33w/MeshCore/blob/kesh/v43-tft-ilink/OPENHOP-BRIDGE-TFT.md) or [OLED build guide](https://github.com/kesh33w/MeshCore/blob/kesh/v43-oled-ilink/OPENHOP-BRIDGE-OLED.md) for the matching hardware.

To inspect the latest Repeater source alone:

```sh
git clone --branch kesh/bridge --single-branch https://github.com/kesh33w/openhop_repeater.git
```

The inherited [package metadata](pyproject.toml) still references upstream Core. A standalone install does not reconstruct the pinned bridge system. The upstream installers, Docker images, and web or CLI updaters in the preserved guide are upstream references, not verified deployment instructions for this fork. Use the hub workflow for compatible components.

Runtime channel keys, certificates, Wi-Fi credentials, enrollment state, and live routes are installation inputs. Reconstruction does not activate a configured Pi or iLink service.

## Validation limits

The hub records backend and UI checks, builds, and a bounded hardware test in which one encrypted recipient message and its return ACK passed through the Pi and TFT. Those results do not establish endurance or field reliability. See the hub's [firmware and verification record](https://github.com/kesh33w/kesh-openhop-bridge#firmware-and-verification) for the scope of each run and [firmware releases](https://github.com/kesh33w/kesh-openhop-bridge/releases) for source manifests and checksums.

## Upstream and license

This fork builds on [OpenHop Repeater](https://github.com/openhop-dev/openhop_repeater) and [OpenHop Core](https://github.com/openhop-dev/openhop_core). The inherited README is preserved in [UPSTREAM-README.md](UPSTREAM-README.md), with its original attribution and upstream links. Its screenshots and local documentation links refer to the inherited files retained here.

The Repeater remains under the [MIT License](LICENSE). See the hub's [credits and licenses](https://github.com/kesh33w/kesh-openhop-bridge/blob/kesh/openhop-bridge/NOTICE.md) for the related OpenHop, MeshCore, and third-party acknowledgements.

For explicit per-radio MQTT identities, see [Per-radio observer identities](docs/separate-observer-identities.md).

## Bridge control API

All endpoints use the existing authenticated API. See the [OpenAPI contract](repeater/web/openapi.yaml) for request and response fields.

- `GET /api/bridge_settings` returns redacted saved settings, the running snapshot, the current crossing pause and restart requirement. A missing daemon loop produces an explicitly unknown running comparison.
- `POST /api/update_bridge_settings` validates and saves channel, direction, protocol and safeguard settings. It does not restart, send test traffic or change active routes. The current pause is preserved.
- `POST /api/update_bridge_controls` with `{"paused": true}` persists a crossing pause off the radio event loop, then applies it. `false` resumes eligible new work. Persistence must succeed before the running state changes. A timed-out caller receives an unknown outcome; the accepted operation may still finish, so refresh status before another action.
- `GET /api/bridge_diagnostics` reports running traffic budgets and bounded independent-reception evidence. No plaintext, channel keys or raw packet payloads are returned.

Each mapping and protocol transit direction supports an optional `enabled` boolean, defaulting to true for existing configurations. `disabled_channels` names channels whose source or destination mappings are held without deleting keys. `protocols.discovery`, `protocols.repeater_admin` and `protocols.trace` default to false; they require `peer_routing: distinct` and use enabled transit directions. The default `legacy` mode preserves existing direct DM/ACK behavior. Changing modes requires a restart and relearning cross-network contact paths. Distinct mode separates local and crossing routes with a public route marker, not another signed peer identity. Zero-hop CONTROL discovery and adverts remain local.

Observer evidence is host-configured and disabled by default. An allowlisted observer hearing a matching crossing is distinct from modem TX confirmation and never proves delivery to a particular recipient. A missing observation is unconfirmed, not a proven loss. This implementation has simulated and local UI coverage; it does not claim cross-frequency RF field validation.
