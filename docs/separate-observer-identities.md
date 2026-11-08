# Per-radio observer identities

The MQTT publisher can give each receiving radio an explicit observer identity.
One radio can keep the repeater identity. Other radios use persistent observer-only
Ed25519 private keys. These keys are not registered with the RF engine and do not change
repeater adverts, packet signing, routing, or iLink.

CoreScope identifies an observer by its public key. A distinct key gives Bay its
own observer entry on unmodified CoreScope. Keeping Sacramento on the existing
BD6E identity preserves its public-key history and repeater-node association.
The new Bay identity starts a separate history and has no automatic link to the
BD6E repeater page. Old retained Bay status may remain until the receiver expires
it; this feature does not erase history or retained MQTT records.

## Configuration

This example uses existing radio IDs. Keep the current broker entries, radio
settings, region codes and enable flags. Only SacMesh should remain enabled for
the initial rollout. Adding identity settings does not enable a destination.

```yaml
mqtt_brokers:
  separate_observer_identities: true
  observer_identities:
    host-rf:
      name: "Sacramento observer"
      use_repeater_identity: true
    bay:
      name: "Bay observer"
      private_key_file: /etc/openhop_repeater/observer-identities/bay.seed
  # Retain the existing brokers list. Each feed must have its radio_id and IATA.
```

Use the desired existing Sacramento display name when preparing the real config.
Choose Bay's display name before activation. Each map entry requires `name` plus
exactly one identity choice. At most one radio can use the repeater identity.
The map keys must be configured radio IDs. The default mode is false, which
preserves the existing shared-identity behavior.

Each private key file contains exactly one of these hexadecimal encodings:

- A 32-byte Ed25519 seed: 64 hexadecimal characters.
- A 64-byte MeshCore firmware expanded private key: 128 hexadecimal characters,
  preserving the full `[32-byte scalar][32-byte nonce]` representation. This is
  not a seed followed by a public key; do not truncate or rehash it.

Uppercase and lowercase hex are accepted, with an optional single LF or CRLF
newline. Spaces, prefixes, extra lines, binary files, and other encodings are
rejected. Verify the derived public key against the supplied identity before
activation; character count alone cannot establish the intended key format.
Do not use `convert_firmware_key.sh` for observer keys: it changes the RF
identity and prints its input.

The key must be a regular file at an absolute path, readable by the service
account, with no group or other access.
Use owner-only permissions such as 0600 or 0400. Symbolic-link key files, missing
files, invalid content, duplicate keys, and an unselected copy of the RF key are
rejected. The service never creates, repairs, or rotates a missing key. Back up
new keys securely on the Pi; they are not included in config exports.

Identity settings are restart-required. The config API accepts these two fields
through `POST /api/update_mqtt_config` and returns safe file references through
config reads. Older UI saves that omit them preserve the saved values. Current
Observer controls do not yet provide dedicated identity/name editors. A pending
save does not change the running signer until restart.

## Publishing and failures

Every feed uses its selected key for the JWT signature and claim, MQTT username,
canonical `meshcore/<IATA>/<PUBLIC_KEY>` topic, and payload `origin_id`. The
configured name becomes `origin`. A custom `base_topic` that disagrees with this
canonical topic blocks the feed instead of publishing under the wrong identity.

Packets are selected only by receiving radio. A bridge retransmission on the
other radio does not make it a second RF observation. Unattributed packets and
node-wide diagnostics are omitted from radio-specific feeds. Online and offline
status use the same identity and selected radio profile.

A bad observer identity blocks only its feed when the map is otherwise valid.
An invalid map blocks all separate-identity feeds. The RF repeater continues
using its original identity. `/api/mqtt_status` reports the applied mode,
observer name/public key, and a safe `identity_error` for blocked feeds.

RX/TX packet counts come from each USB/TCP modem driver's cached counters, not
host-wide router totals. Unsupported counters are omitted. Existing airtime
telemetry is a channel budget and can be shared by radios on the same channel;
it is not a physical-radio packet count.

## Rollout checks

1. Preserve the live config and source in a protected local backup. Record the
   current RF public key, both radio IDs, region codes, enabled destinations,
   and existing observer display names. Confirm only SacMesh is enabled.
2. Provision one Bay observer private key on the Pi after explicit approval. Do not use
   test fixtures, print the key, copy it to source control, or transfer it to
   the Mac. Record only its public key for verification.
3. Install the reviewed source changes and map the existing Sacramento radio to
   `use_repeater_identity: true`; map Bay to its new key file. Keep all other
   broker enable flags unchanged. Restart only with approval.
4. Verify the service, RF public key and both radios. Confirm Sacramento still
   publishes to its previous BD6E topic and Bay uses its distinct key in its
   authenticated connection, topic and packet/status payloads.
5. Check natural reception independently in SacMesh for both regions. Confirm
   names, frequencies, radio counters and no cross-band duplicates. Restart once
   during the approved validation window to confirm the Bay public key is stable.
6. Confirm the other destinations remain disabled with no reconnect attempts.
   Report server-side observations separately from mocked local tests.

Rollback restores the previous source/config and restarts the service. Keep the
new Bay key in its protected location so a later retry retains the same identity.
Do not delete observer history or publish retained-message tombstones implicitly.

## Offline tests

The observer tests use fixed public test keys and mocked MQTT clients. They do
not connect to brokers or create operational credentials. Run the identity,
config, counter and existing MQTT suites before preparing deployment. Key
provisioning, live authentication and SacMesh ingestion still require a live
acceptance check.
