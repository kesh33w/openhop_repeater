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

## Ordered TX groups

| Endpoint | Purpose |
|---|---|
| `GET /api/tx_groups` | Saved and running transmitter order and `scope` for each network, and whether a restart is pending. |
| `POST /api/update_tx_group` | `{"primary_radio_id": "bay", "radio_ids": ["rak910-shadow", "bay"], "scope": "crossings"}` saves the order for a restart; live radios are never changed. `scope` is optional: `all` (default) or `crossings`, meaning bridged packets only. |

## Antenna identities

| Endpoint | Purpose |
|---|---|
| `GET /api/antenna_identities` | `saved`, `running` and `restart_required` mode (`off`, `tx` or `rx_tx`), plus `tx_available` (whether per-transmitter keys exist). |
| `POST /api/update_antenna_identities` | `{"mode": "off"}` and the like. Saved for restart. `off` keeps the section so learned routes still resolve. |

## CAD

| Endpoint | Purpose |
|---|---|
| `GET /api/cad_radios` | Physical radios with their air settings, CAD support and host CAD state. |
| `POST /api/set_cad_enabled` | `{"radio_id": "bay", "enabled": false}`. Applied only after the modem acknowledges, then saved for restart. |
| CAD calibration endpoints | Upstream endpoints. The fork adds `radio_id` to each; unknown ids are rejected rather than falling back to the default radio. |

## Internet Link

| Endpoint | Purpose |
|---|---|
| `GET /api/internet_link` | Status: running, listening, peers with their last inbound frame age, routes and limits. Credentials are never returned. |
| `POST /api/update_internet_link` | Either `{"enabled": bool}` (service on/off) or limits (`send_timeout`, `max_inflight`, `max_rx_tasks`). Each runtime change is bounded to 20 s; slower changes report `outcome: unknown`. |
