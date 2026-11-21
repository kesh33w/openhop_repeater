# Changelog

Fork-specific changes to OpenHop Repeater. Upstream changes are in upstream's history. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases are HydraHop versions (`MAJOR.MINOR.PATCH`), tagged `hydrahop-vX.Y.Z` across the core, repeater, web UI and hub repositories. Releases up to 19 Nov 2026 used date tags (`kesh-YYYY.MM.DD`).

## [Unreleased]

### Added

- **One-byte antenna identity codes.** `tx_identity_experiment.one_byte` gives chosen transmitters a hand-picked 1-byte code (and an optional crossing code), so 1-byte floods, most traffic on a busy mesh, are tagged and counted in `/api/relay_stats`. A code is retired as soon as a repeater or room advertising that byte is heard, live or in the last 30 days of stored adverts. See [docs/tx-identities.md](docs/tx-identities.md#one-byte-codes).

### Fixed

- **`/api/relay_stats` could credit the wrong copy** when an older hop in the path shared a code's bytes; it now matches the newest hop, which is the one this node added.

- **`/api/relay_stats` returned a stray `codes` field** for a radio none of whose copies carried a distinct code yet. Only the documented fields are returned.

## [1.4.0] - 2026-11-20

### Added

- **Which copy did neighbours relay?** `GET /api/relay_stats` matches the relays this node hears back against the identity code on each copy of a double-sent packet, per neighbour, radio and send position (first or second). Needs per-radio identity codes; identical copies are counted as untagged. See [docs/fork/bridge-api.md](docs/fork/bridge-api.md).
- **Random double-TX order.** `fabric.topology.tx_group_orders: {primary: random}` shuffles which radio sends first for every send, so the relay comparison is fair. Default `fixed`. Saving a single transmitter clears it.
- **Bundled web UI** rebuilt from UI 131f94e (Antennas: which copy neighbours relayed).

### Changed

- Core pinned to `hydrahop-v1.4.0` (63bad05).

## [1.3.3] - 2026-11-20

### Added

- **Pause between double-TX sends.** `fabric.topology.tx_group_gaps_ms` (per TX group, `[min, max]` ms) waits a random time before the second radio sends, instead of keying up the instant the first finishes, when neighbours are rebroadcasting the first copy. The pause is outside the TX lock. Default is no pause. See [docs/ordered-dual-tx.md](docs/ordered-dual-tx.md#pause-between-the-two-sends).

### Fixed

- **A shadow radio could be configured as a TX-group transmitter.** Shadow radios are receive-only; the topology now refuses them in `tx_groups` and the send path checks again. The web UI no longer offers them as a second transmitter. `receiver` radios can still be backup transmitters.

### Changed

- Core pinned to `hydrahop-v1.3.3` (fe35c0a).

## [1.3.2] - 2026-11-20

### Fixed

- **Double-TX radios showed zero transmit airtime.** A TX group (e.g. `g3910`+`rak910`) stored only its primary radio in `packets.tx_radio_ids`, so the dashboard's airtime chart, per-radio packet rates and MQTT `tx_radio_ids` never counted the group's other radio. Every successful leg is now listed. Rows stored before this fix keep the old attribution (the per-leg `packet_egress` rows were always correct).

## [1.3.1] - 2026-11-20

### Fixed

- **Antenna signal comparison measured relays, not antennas.** `signal_gaps` now compares only the same RF frame (same packet, same relay, within 1 s) and reports how many `frames` it is based on; previously two different relays of one packet counted as "heard by both" and their best readings were compared. Coverage (`heard_by`) still counts distinct packets.
- **Bridge echoes skewed antenna figures.** Receptions of neighbours repeating a packet this node bridged are now left out of coverage, signal averages and upstream counts, and counted separately as `bridge_echoes`.
- **Antenna roles could claim a radio transmits when it does not.** Each radio now reports `transmits` (`all`, `crossings` or null) from the running topology, `recorded` (false for shadow radios, whose receptions are not stored) and `has_data`, so "no data" is distinguishable from zero.
- **Bridge antenna codes came from today's settings, not the air.** `radio_pairs[].antenna_code` is now read from the stored sent bytes; `antenna_codes[]` lists up to three with counts and `node_code` gives this node's own code.
- **An unreadable history looked like no bridge traffic.** `/api/bridge_stats` now returns an error instead of an empty report when the database read fails.
- **Installs could get different core code for the same version.** `openhop_core` is pinned to an exact commit instead of the moving `kesh/bridge` branch, in the dependencies and the `hardware` extra.
- **Bundled web UI** rebuilt from UI 5195f3b (truthful Antennas and Bridge pages).

### Changed

- Core pinned to `hydrahop-v1.3.1` (09edf94).

## [1.3.0] - 2026-11-20

### Added

- **Antenna statistics API.** `GET /api/antenna_stats` compares the antennas on each network from stored history: which antennas heard each packet, packets only one antenna heard, average signal difference, noise floor, CRC errors, upstream repeaters heard (and only heard by one antenna), and sends with channel-busy holds. Drives the web UI's new Antennas page.
- **Antennas page in the bundled web UI** (Analytics → Antennas), rebuilt from UI 2e489f9.

## [1.2.0] - 2026-11-20

### Added

- **Bridge statistics API.** `GET /api/bridge_stats` reports packets bridged in each direction, local repeats, why heard packets were not sent, and sends per antenna pair, from stored packet history. Drives the web UI's new Bridge page.
- **Bridge page in the bundled web UI** (Analytics → Bridge), rebuilt from UI a8b3efb.

## [1.1.0] - 2026-11-19

### Added

- **Alias names in MQTT status.** Each observer status message lists the antenna aliases as `hop_aliases` (`key`, `label`, e.g. `BD6E g3910→rak910`), so a map can name alias hops automatically. The SacMesh CoreScope fork reads it from trusted observers.

## [1.0.3] - 2026-11-19

### Changed

- No changes. Tagged so all four repositories share the HydraHop version.

## [1.0.2] - 2026-11-19

### Fixed

- **MQTT observers report relayed copies.** Each logical observer published a packet only the first time it was heard, so copies relayed by other repeaters (same packet, different path) were dropped. CoreScope counted roughly a third as many observations as a Heltec observer at the same site, and lost the paths showing which repeaters the site can hear. Each distinct wire image (packet plus path) is now reported once. Identical copies heard by both ears of a pair (G3 and RAK) still collapse to one report.

### Changed

- **Docs:** examples and diagrams use the radio IDs `g3909` and `g3910` (formerly `host-rf` and `bay`), matching `rak909` and `rak910`.

## [1.0.1] - 2026-11-19

### Changed

- **Docs:** TX group examples use the radio id `rak910` (the production RAK was renamed from `rak910-shadow`; it is an active receiver, not a shadow).

## [1.0.0] - 2026-11-19

### Fixed

- **Victron VE.Direct sensor** always reads at least once before checking its deadline. If opening the port took longer than the read deadline (a busy host), a frame already waiting was discarded and the reading failed. This also made a CI test fail at random.

### Changed

- **Versioning**: releases are now HydraHop semantic versions tagged `hydrahop-vX.Y.Z`; this is HydraHop 1.0.0. Date tags (`kesh-YYYY.MM.DD`) stay as history.
- **Observer `firmware_version`** reads `HydraHop <version>` instead of `OpenHop <version>`, so the fork's version isn't mistaken for an upstream release.
- **Links** point at the renamed hub, `kesh33w/hydrahop`.
- **Observer label**: MQTT status now reports `client_version` as `💧🍴 OpenHop HydraHop v<version>` and `model` as `OpenHop HydraHop`, replacing `quad-radio-bridge`. The fork is named HydraHop; README and CONTRIBUTING titles follow.

## [kesh-2026.11.19] - 2026-11-19

### Changed

- **README and CONTRIBUTING** point feedback and bug reports at the hub's issues, the single place for all four repositories.
- **Docs:** `docs/tx-identities.md` describes the web UI setting for switching modes and records that the unused key file was removed from the Pi.

## [kesh-2026.11.18.2] - 2026-11-19

### Added

- **Bundled web UI** rebuilt from openHop_RepeaterUI `1e88667`, adding the Antenna identities control.
- **Antenna identity setting**: `GET /api/antenna_identities` and `POST /api/update_antenna_identities` stage Off, Transmitting-antenna or Receiving + transmitting codes for restart. Off keeps the section, so routes learned through aliases still resolve.

## [kesh-2026.11.18.1] - 2026-11-18

### Changed

- **Bundled web UI** rebuilt from openHop_RepeaterUI `358d8ab`. It now includes the Bridge control center, which the Pi has served unmerged since 2026-11-17, and the Radio names editor. The `kesh-2026.11.18` UI lacked both, so upgrading to that release would have removed them from the live dashboard.

## [kesh-2026.11.18] - 2026-11-18

This is the first tagged fork release. It covers everything since the fork's upstream base, `openhop-dev/openhop_repeater` `dev` at `72266e2` (2026-10-27). Feature details: [docs/fork/README.md](docs/fork/README.md).

### Added

- **Channel translation bridge** (`channel_bridge`):
  - authenticated, directional channel mappings with write-only keys;
  - a durable echo ledger;
  - native queueing and airtime accounting for crossings;
  - explicit home radios for origins and companions.
- **Protocol transit**: explicit-path DM and ACK crossing (`direct_routes`), plus opt-in discovery, repeater administration and TRACE transit with distinct peer routing.
- **Bridge safeguards**:
  - per-direction flood budgets and pending limits;
  - live, saved pause;
  - per-channel and per-direction enable switches;
  - per-type overrides (`packet_types`).
- **Sender markers** (`text_prefix`) on translated messages, safe for UTF-8 and for messages that have already crossed.
- **Network topology** (`fabric.topology`):
  - two logical networks, each with one designated transmitter;
  - `receiver` and `shadow` radios;
  - per-network duplicate suppression and companion inputs;
  - logical MQTT observer streams.
- **Ordered dual TX** (`fabric.topology.tx_groups`) with per-leg results, plus the `/api/tx_groups` and `/api/update_tx_group` endpoints.
- **Bridged-only dual TX** (`fabric.topology.tx_group_scopes: {<primary>: crossings}`): only packets bridged from the other network use both transmitters. Ordinary local repeats keep a single send. This has a selector in Bridge settings.
- **Antenna identities** (`tx_identity_experiment`):
  - flood-hop aliases naming the receiving and transmitting antennas (`rx_tx`, the default) or only the transmitting antenna (`tx`);
  - an alias table in the bridge diagnostics and the web UI.
- **Per-radio CAD**:
  - acknowledged on/off control (`/api/set_cad_enabled`, CLI `get cad` and `set cad`);
  - calibration per physical radio.
- **Observability**:
  - per-radio MQTT observer identities and counters;
  - per-radio RF receive age;
  - independent reception evidence from allowlisted observers;
  - `/api/bridge_diagnostics`.
- **Internet Link (iLink) integration** for the kesh-openhop-bridge sidecar, with bounded status and controls in the Bridge API.
- **Bot RF receipts**: an authenticated Unix socket for a local bot's raw RF fallback.
- Fork documentation: the README, a feature reference, the API reference, the upstream sync guide, CONTRIBUTING and SECURITY.
- CI that runs upstream's full pre-commit gate on pushes to `kesh/bridge` and on pull requests.

### Changed

- **Discovery, guest and admin replies** go back out on the radio the request arrived on.
- **The main bridge identity** advertises on both radio sides.
- **TX outcomes** keep their reason, and suppressed duplicates are excluded from failure metrics.
- **CAD failures:** when a modem's CAD check fails or times out, the packet is now sent anyway, as upstream does (in openhop_core).
- **Topology without `fabric.tx_mode`** now repeats on the network a packet arrived on (`sticky`). An explicit `default` logs a warning.
- **Dependencies:**
  - the package depends on `kesh33w/openhop_core@kesh/bridge`;
  - the dev extra includes the build backend.
- **Fork code** now passes upstream's ruff, format and bandit gate.

### Fixed

- **Config saving:**
  - Saves were blocked when `identity_key` differed from `identity_file`, after a backup restore, or with line-wrapped key files.
  - CAD edits are now staged under the config lock, so live sections are never replaced.
- **Duplicates and echoes:**
  - The repeater's own packets heard back on another network were flooded again.
  - A re-flooded crossing was forwarded back to its source network (in core).
- **Message loss:**
  - The bridge deadline dropped the repeater's own messages that the airtime limit had delayed.
  - A one-way channel mapping let opaque copies cross in the reverse direction.
- **TRACE:** setting `packet_types.trace` alone had no effect.
- **Internet Link:**
  - Saves blocked the radio loop.
  - A hung runtime change left controls pending until restart.
  - `private_island.enabled: "false"` started the service.
- **Bot receipt server:**
  - Shutdown hung for 5 s while a bot was connected.
  - An unauthenticated client could hold the only session slot.
- **MQTT and stats:**
  - The reception subscriber now pins the paho callback API and reloads a rotated password.
  - Stats failed on installs without the hardware extra.
- **Antenna identities** (the experiment deployed on 2026-11-18):
  - Invalid config no longer stops the repeater.
  - 1-byte paths are never tagged or rewritten.
- **Tests:** the suite now passes from a clean checkout, without machine-specific paths or the `private_link` sidecar.

### Security

- Channel keys are never returned by any endpoint, including config export with secrets.
- Bot receipts compare their HMAC in constant time and fail closed. Secret files are opened with `O_NOFOLLOW` and owner/mode checks.

[Unreleased]: https://github.com/kesh33w/openhop_repeater/compare/kesh-2026.11.18.2...kesh/bridge
[kesh-2026.11.18.2]: https://github.com/kesh33w/openhop_repeater/compare/kesh-2026.11.18.1...kesh-2026.11.18.2
[kesh-2026.11.18.1]: https://github.com/kesh33w/openhop_repeater/compare/kesh-2026.11.18...kesh-2026.11.18.1
[kesh-2026.11.18]: https://github.com/kesh33w/openhop_repeater/compare/72266e2...kesh-2026.11.18
