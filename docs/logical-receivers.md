# Logical local-RF receivers

Requires coordinated OpenHop Core logical-receiver support. Each topology network supplies one observation stream. Active `transceiver` and `receiver` members join it; a `shadow` stays in `shadow:<radio_id>`. Names are configured network identifiers, not inferred frequency or IATA.

Subscribe MQTT observer identities once:

```yaml
mqtt_logical_observers:
  directional_a: network_a
  directional_b: network_b
  comparison_radio: shadow:comparison_radio
```

Keys are existing MQTT observer radio identities. This replaces `mqtt_observation_groups`; do not configure both. Broker destinations, coordinates, credentials and private identities remain in deployment configuration.

A companion can replace its physical `receive_radio_ids` allowlist with:

```yaml
home_radio_id: directional_a
receive_network: network_a
```

Home must be that network's designated transmitter. Other networks and shadows are excluded. Existing channel, authentication, command and rate-limit policies still apply; legacy allowlists remain supported. A legacy allowlist covers whole networks: each listed non-shadow radio brings in every non-shadow radio of its network, because identical copies are deduplicated per network before companions see them, so listening on only one ear would lose whatever another ear heard first (fixed in HydraHop 1.8.1). Shadows are heard only when listed.

To add an ear, configure a unique physical radio with compatible air parameters and the same network. Verify it as a shadow before promoting it to `receiver` under the existing forwarding policy. Restart with rollback protection: live topology replacement with subscribers is rejected. Consumer membership lists need no changes. Tests cover three active ears on each of two networks. Additional receivers never acquire TX ownership.

## Delivery contract

Core `LogicalReception` events preserve first arrival, packet hash, event ID, physical radio, wire hash, timestamps and signal readings. Subscribers can request later copy/variant updates. MQTT publishes the first admitted reception, not the strongest later signal. Reports preserve physical `rx_radio_id` and add logical stream/event IDs.

Companions use shared membership and existing protocol-aware router deduplication, rather than the MQTT event queue. Physical copies continue through routing: a later path or receiver can still reach a permitted destination. Application delivery and destination-specific forwarding must remain distinct. Remote MQTT is not local RF; this feature adds no MQTT fallback.

Defaults: 300-second first-arrival TTL, 10,000 identities across streams, 32 retained observations per identity, 64 subscribers. State is in memory. New subscribers do not replay admitted identities. Capacity refuses new observations without evicting suppression state; forwarding continues. Queue refusal retries the original first event only on another physical copy. Callbacks must be bounded and nonblocking. Counters expose loss/refusals/errors, not durable delivery guarantees.

`/api/stats` exposes `logical_receiver`; MQTT status exposes `logical_observation.receive_mode`. Known own-TX echoes are excluded, without claiming remote reception or perfect echo recognition.

Validation covers both arrival orders, concurrent and late copies, path variants, echoes, queue refusal, shadows, three ears per band, legacy companions and protected topology replacement. Roll back code and configuration together, never the live echo ledger. No RF test traffic is necessary.
