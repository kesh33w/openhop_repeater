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
- `radio_pairs[]`: physical sends per heard-on and sent-on radio, `sent`/`failed`, with the pair's `antenna_code` when antenna identities are configured.
- `unattributed`: rows from radios that are no longer configured, such as IDs from before a rename.

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

- `radios[]`: packets `received` (duplicates included), `unique_packets` no other antenna on its network heard, average `rssi_avg`/`snr_avg`, `noise` (average, 10th and 90th percentile), `crc_errors` and per hour, `upstream_repeaters` and `unique_upstream_repeaters`, and `sends`, `send_failures`, `busy_holds` and `backoff_avg_ms` from listen-before-talk.
- `networks[]`: `heard_by[]` counts packets by the set of antennas that heard them, and `signal_gaps[]` gives the average RSSI/SNR of antenna `a` minus antenna `b` for packets both heard.

Radios that are no longer configured are left out.
