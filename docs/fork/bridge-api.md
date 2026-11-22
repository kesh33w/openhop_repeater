# Fork web API

These endpoints are added by the fork. Like the rest of `/api`, they require a JWT (`Authorization: Bearer …`) or an API key (`X-API-Key`). Request and response fields are defined in [`repeater/web/openapi.yaml`](../../repeater/web/openapi.yaml). CI checks the code against that spec, and the web UI's API client is generated from it.

Every response has the form `{"success": bool, "data": …}` or `{"success": false, "error": "…"}`. Check `success` before using `data`.

## Bridge settings and controls

| Endpoint | Purpose |
|---|---|
| `GET /api/bridge_settings` | The saved settings with channel keys redacted. Also: the running snapshot, the current pause state, and whether a restart is needed. If the daemon loop is unavailable, the running state is reported as unknown. |
| `POST /api/update_bridge_settings` | Validates and saves channels, mappings, directions, protocols and safeguards. **Restart required.** It never sends traffic and never changes the pause state. For a channel that is already saved, leave `key` blank to keep its key. |
| `POST /api/update_bridge_controls` | `{"paused": true\|false}`. Saved first (off the radio loop), then applied live. A timed-out request reports `outcome: unknown`; refresh before acting again. |

Channel keys are **write-only**: no endpoint returns them, including config export with secrets.

## Diagnostics

`GET /api/bridge_diagnostics` returns bounded process-local state, with no packet bytes or keys:

- `traffic`: the per-direction limits and counters (pending, admitted, rate-limited, queue-full).
- `reception`: independent-observer feeds and the most recent crossings. Each crossing has a separate `tx_state` and `reception_state`.
- `antenna_identities`: `enabled`, `mode`, and `aliases[]` of `{prefix, crossing_prefix, rx, tx}`.

A modem-confirmed TX and an independent observation are different kinds of evidence. Neither proves that a person received the message.

`GET /api/bridge_stats?hours=24` (1–168) summarises bridge traffic from the stored packet history, so it survives restarts and needs no extra tracking:

- `networks[]`: per topology network, non-duplicate packets `heard`, `repeated_local`, `bridged_out`, `not_sent` with the top `reasons`, and `originated`.
- `directions[]` and `local[]`: bridged packets per direction and local repeats per network, with time `buckets` (5 min up to 6 h, else 1 h).
- `radio_pairs[]`: physical sends per heard-on and sent-on radio, `sent`/`failed`, with the code the pair actually went out as: `antenna_code` (most used) and `antenna_codes[]` (up to three, with `sends`), read from the last hop of the stored sent bytes of flood sends. `node_code` is this node's own hash, the code used when antenna identities are off; its 1-byte form on 1-byte paths is counted as the same code.
- `unattributed`: rows from radios that are no longer configured, such as IDs from before a rename.

A database read failure returns `success: false`, never an empty report that would look like no traffic.

Network figures count a packet once even when a TX group sent it on two antennas; radio pairs count each antenna. A packet sent on both networks counts as both local and bridged.

## Ordered TX groups

| Endpoint | Purpose |
|---|---|
| `GET /api/tx_groups` | Saved and running transmitter order and `scope` for each network, and whether a restart is pending. |
| `POST /api/update_tx_group` | `{"primary_radio_id": "g3910", "radio_ids": ["rak910", "g3910"], "scope": "crossings"}` saves the order for a restart; live radios are never changed. `scope` is optional: `all` (default) or `crossings`, meaning bridged packets only. |

## Antenna identities

| Endpoint | Purpose |
|---|---|
| `GET /api/antenna_identities` | `saved`, `running` and `restart_required` mode (`off`, `tx` or `rx_tx`), plus `tx_available` (whether per-transmitter keys exist). |
| `POST /api/update_antenna_identities` | `{"mode": "off"}` and the like. Saved for restart. `off` keeps the section so learned routes still resolve. |

## CAD

| Endpoint | Purpose |
|---|---|
| `GET /api/cad_radios` | Physical radios with their air settings, CAD support and host CAD state. |
| `POST /api/set_cad_enabled` | `{"radio_id": "g3910", "enabled": false}`. Applied only after the modem acknowledges, then saved for restart. |
| CAD calibration endpoints | Upstream endpoints. The fork adds `radio_id` to each; unknown ids are rejected rather than falling back to the default radio. |

## Internet Link

| Endpoint | Purpose |
|---|---|
| `GET /api/internet_link` | Status: running, listening, peers with their last inbound frame age, routes and limits. Credentials are never returned. |
| `POST /api/update_internet_link` | Either `{"enabled": bool}` (service on/off) or limits (`send_timeout`, `max_inflight`, `max_rx_tasks`). Each runtime change is bounded to 20 s; slower changes report `outcome: unknown`. |

`GET /api/antenna_stats?hours=24` (1–168) compares antennas on the same network, from stored history:

- `radios[]`: packets `received` (duplicates and bridge echoes included), `bridge_echoes`, `unique_packets` no other antenna on its network heard, average `rssi_avg`/`snr_avg` (echoes excluded), `noise` (average, 10th and 90th percentile), `crc_errors` and per hour, `crc_after_sends` (an estimate of the CRC errors that follow our own sends on that network: `per_send`, `share`, `estimated`, `other_per_hour`, from five-minute buckets fitted with a line; null with under two hours of samples), `upstream_repeaters` and `unique_upstream_repeaters`, and `sends`, `send_failures`, `busy_holds` and `backoff_avg_ms` from listen-before-talk. State flags: `transmits` (`all`, `crossings`, or null when the topology never sends on it), `recorded` (false for shadow radios, whose receptions are not stored) and `has_data` (false when nothing was stored in the window).
- `networks[]`: two separate questions.
  - **Coverage:** `heard_by[]` counts distinct packets by the set of antennas that heard them, from any relay.
  - **Signal:** `signal_gaps[]` gives the average RSSI/SNR of antenna `a` minus antenna `b` over the *same RF frames* only: the same packet from the same relay, heard within `match_window_seconds` (1 s), with `frames` saying how many. Different relays of one packet would compare the relays, not the antennas. RSSI is not calibrated between radio models; SNR is the fairer comparison.

Bridge echoes (receptions of neighbours repeating a packet this node bridged) are left out of coverage, signal and upstream counts. `topology_source` says whether roles and TX membership came from the running topology or the saved config. Radios that are no longer configured are left out.

`GET /api/relay_stats?hours=24` (1–168) answers "which copy did neighbours relay?" for packets a TX group sent through two radios. A neighbour relays whichever copy it hears first, and its relay keeps that copy's identity code in front of its own hop, so the relays this node hears back name the radio (and whether its copy went first or second). Per network: `double_sent`, `tagged` (copies with different codes), `untagged` (identical copies: identity codes off, 1-byte paths without `one_byte` codes, or direct routes), `relayed`; `radios[]` with their `code`, `sent_first`, `relayed`, `relayed_when_first` and `relayed_when_second`; and `neighbours[]` (top 25) with relays `by_radio`. A relay counts once per packet and neighbour, however many of this node's radios heard it. `identity_codes`, `identity_mode` and `random_order` report the settings the comparison depends on. Use per-radio codes (`tx_identity_experiment.mode: tx`) and `tx_group_orders: random`, so each radio goes first about half the time.
