# Destination reception and traffic limits

The bridge distinguishes three outcomes:

- `tx_confirmed`: the destination radio confirmed its send.
- `observed`: an allowed independent receiver reported matching RF reception on the destination network within the observation window.
- `unconfirmed`: no matching report arrived before the window closed. This does not mean transmission failed.

The TX and reception states remain separate. An independent report can resolve reception even when the modem's own TX outcome is uncertain. These reports are not acknowledgements from an end recipient and do not establish that a user read or decrypted a message.

## Configure independent receivers

Reception evidence is disabled by default. Add receiver identities to the host's `channel_bridge.reception_evidence` configuration. Each entry names an existing MQTT broker connection, the exact region and public-key topic segments used by that independent receiver, and the destination radio/network it observes. Public-key letter case must match the actual MQTT topic.

```yaml
channel_bridge:
  reception_evidence:
    enabled: true
    window_seconds: 120
    capacity: 256
    observers:
      - broker: SFO feed
        region: SFO
        public_key: REPLACE_WITH_INDEPENDENT_RECEIVER_64_HEX_PUBLIC_KEY
        radio_id: bay
  traffic_limits:
    per_minute: 12
    burst: 3
    max_pending_per_direction: 16
```

Restart after changing these settings. The broker must allow reading the exact `meshcore/<region>/<public_key>/packets` topics over verified TLS. Existing broker credentials are reused; no extra accounts, keys, wildcard subscriptions, or publications are created. The observer list must describe receivers independent of the bridge, not the bridge's own per-radio observer feeds. The RF signer and all loaded local observer identities are excluded automatically.

The reader accepts only live, non-retained RX reports whose topic, `origin_id`, destination network, timezone-aware timestamp, and parsed packet match the configured receiver and current attempt. It rejects reports older than the observation window or more than two seconds in the future. A two-second clock allowance accounts for receiver clock skew at submission. Both receivers and bridge should use synchronized clocks.

Exact outgoing bytes match directly. Flood copies can also match a longer downstream path with the outgoing path as its prefix and identical header, transport codes, and encrypted payload. TRACE can match a valid growing SNR path with the same route/tag payload. Ordinary direct packets require an exact outgoing-byte match. A report on the source network does not confirm destination reception.

Broker access controls are the identity trust boundary. Reports are not individually cryptographically signed. Never treat an arbitrary broker publisher, an unverified receiver location, or a bridge-owned observer as independent evidence. Outages, denied subscriptions, missing observers, or an exhausted tracking capacity leave reception unknown and do not interrupt native RF forwarding.

## Directional traffic safeguards

Each crossing direction has a separate pending limit. Flood traffic additionally uses a token bucket, initially permitting a burst of three and replenishing at twelve packets per minute by default. Direct traffic bypasses the flood bucket but still consumes pending capacity. Local same-radio repeats do not use these crossing limits.

Admission consumes a token even if the later send fails. This bounds repeated unsuccessful traffic; the existing bounded native retry belongs to the same admission. A full queue does not consume a token. Pending capacity is released after completion, error, or cancellation. Existing native duty-cycle, queue expiry, packet validation, and durable echo protections still apply. Budgets and diagnostics reset on restart.

Limits accept integers: `per_minute` 1 to 600, `burst` 1 to 100, and `max_pending_per_direction` 1 to 1000. Evidence accepts 10 to 600 seconds, 1 to 1000 tracked attempts, and up to 32 observer entries. Active waiting attempts are never evicted to admit more evidence records.

## Diagnostics and privacy

The authenticated `/api/bridge_diagnostics` endpoint returns active directional budgets, admissions and drops, observer subscription state, and at most fifty recent reception records. Each record shows source/destination radios, TX state, reception state, and matching observer metadata. A failed or missing SUBACK is retried after ten seconds without disrupting the publisher. Disconnect and shutdown cancel pending subscription work.

No channel keys, credentials, plaintext, raw packet bytes, or encrypted payloads are returned or persisted by this feature. Matching retains only bounded in-memory digests and path metadata; observer input does not enter RF routing, storage, or outgoing MQTT publication.
