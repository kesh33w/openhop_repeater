# Syncing with upstream

| | |
|---|---|
| Upstream | https://github.com/openhop-dev/openhop_repeater |
| Upstream branch tracked | `dev` |
| Last merged upstream commit | `9844406` (7 Nov 2026, "Merge pull request #492 from agessaman/feat/kiss-agc-fem-control") |
| Fork main line | `kesh/bridge` |

The fork merges upstream; it never rebases. History on `kesh/bridge` is never rewritten.

## Procedure

```sh
git remote add upstream https://github.com/openhop-dev/openhop_repeater.git   # once
git fetch upstream
git switch -c sync/upstream-YYYYMMDD kesh/bridge
git merge upstream/dev
```

Then:

1. **Resolve conflicts.** The usual hotspots are listed below. Keep the fork's behaviour unless upstream has fixed the same problem.
2. **Sync core first.** Merge the matching upstream core into `openhop_core` `kesh/bridge` before the repeater, because the repeater depends on that branch.
3. **Rebuild the web UI if needed.** If the UI changed upstream, merge it into `openHop_RepeaterUI` and rebuild the bundle (see [CONTRIBUTING](../../CONTRIBUTING.md#web-ui-bundle)).
4. **Run the full gate:** `pre-commit run --all-files`.
5. **Open a PR** into `kesh/bridge` titled "Merge upstream dev (YYYY-MM-DD)", and update the base commit in the table above and in the README.

## Conflict hotspots

These upstream files carry the most fork changes:

| File | Fork changes |
|---|---|
| `repeater/engine.py` | duplicate keys per network, bridge scheduling, ordered TX, antenna identities, own-echo detection |
| `repeater/packet_router.py` | topology bookkeeping in `_route_packet`, which wraps upstream's logic in `_route_packet_on_radio` |
| `repeater/web/api_endpoints.py` | bridge, TX group, CAD and Internet Link endpoints |
| `repeater/main.py` | private link and receipt server startup and shutdown |
| `repeater/config.py`, `config_manager.py` | fabric validation, identity persistence, `apply_and_save` |
| `repeater/data_acquisition/mqtt_handler.py` | per-radio observer identities, retries |
| `repeater/web/openapi.yaml` | fork endpoints |
| `repeater/web/html/` | **do not merge by hand.** Rebuild from the UI repo. |

## Contributing back

Some fork fixes are general and could go upstream:
- transmit outcome classification;
- per-radio CAD;
- identity persistence;
- MQTT retry and credential refresh.

The iLink hooks are specific to this installation and are not intended for upstream.
