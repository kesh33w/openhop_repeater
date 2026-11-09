# Cross-frequency protocol routing

The bridge separates native local repeater hops from cross-frequency hops when distinct peer routing is enabled. It does not impersonate remote endpoints, decrypt their peer payloads or translate their identities. Existing channel keys and mappings are unchanged.

## Opt-in settings

Within the existing `channel_bridge` section, add these settings when the traffic is wanted:

```yaml
peer_routing: distinct
protocols:
  discovery: false
  repeater_admin: false
  trace: false
```

The default peer routing mode is `legacy`, preserving existing explicit-path DM/ACK direction behavior. The three protocol switches default to false and require `peer_routing: distinct`. Enabling a new protocol in legacy mode is rejected.

Changing peer routing mode requires a restart and rediscovery of cross-network contact paths. Existing paths using the native prefix as a crossing cannot remain valid at the same time as unambiguous local routing. Do not keep an old learned path or assume an automatic fallback. Existing channels, keys and mappings are retained.

The three protocol switches use the existing `direct_routes` direction list, with each source radio mapped to the other configured radio. Set a direction's optional `enabled` field to false to retain its definition without permitting its crossings. The two directions are independent. Routing changes require a restart. Live global pause is separate. In distinct mode it leaves native local repeating available. Legacy mode retains its historical crossing-only interpretation of DM/ACK next hops; disabled or paused legacy crossings have no local fallback.

- `discovery` permits unscoped flood DM, ACK and PATH packets to cross. This lets endpoints with an imported contact but no route learn a route across the bridge.
- `repeater_admin` permits unscoped flood and direct REQ, RESPONSE, ANON_REQ and PATH packets. It also permits flood text and ACK packets because repeater CLI commands use the same encrypted packet type as ordinary peer text. The bridge cannot distinguish those encrypted subtypes.
- `trace` permits unscoped direct TRACE packets whose next requested hop matches the distinct crossing routing identifier. Requested route hashes may be 1, 2, 4 or 8 bytes. Each crossing appends one actual ingress SNR sample. The immutable requested route is not rewritten, and no synthetic SNR is inserted.

Peer ciphertext, MACs and anonymous-request public keys remain unchanged. Structural checks reject malformed lengths, reserved versions, inconsistent path encodings and invalid TRACE progress. These checks are not authentication of the sender. Endpoints still authenticate and decrypt every peer exchange.

## Path learning

Let K be the native repeater public-key prefix and V the distinct crossing routing prefix. For a received flood, native routing appends K once. The local copy retains K. The crossing copy replaces only that newly appended hop with V before echo reservation and transmission. Earlier hops and peer ciphertext remain unchanged.

An endpoint on the same RF side therefore learns K, and its direct K route stays on the ingress radio. An endpoint across the bridge learns V, and its direct V route crosses to the configured target. The marker is consumed using the same native direct, ACK and multipart-ACK machinery, including deduplication and airtime handling. A V route without an allowed configured direction is dropped, never repeated locally.

V is the SHA-256 of the bytes `openhop:bridge:crossing:v1` followed by a NUL byte and the full native 32-byte public key. If V's first byte equals K's first byte, V's first byte is XORed with 1. This deterministic public routing identifier needs no stored secret. It is not a signed endpoint identity, cannot authenticate a peer and is not advertised as another node. The current full identifier is returned as `crossing_route_id` in bridge status. It stays stable across restarts while the native identity remains unchanged.

K and V differ at every supported width, but either can collide with an unrelated node's short prefix. This is a wire-protocol ambiguity, especially at one byte. Prefer wider ordinary paths and TRACE hashes where endpoints support them. Do not treat routing markers as access control. Unauthenticated observations never rotate the marker or change forwarding policy automatically.

An endpoint's encrypted PATH response can return by flood. It carries the route the request actually traversed. The initiating endpoint authenticates that PATH, learns its outgoing route, and sends a reciprocal direct PATH. The bridge consumes V and emits the remaining route on the other radio. It consumes K and emits the remaining route locally for same-side peers. Subsequent direct requests and responses use those learned routes. The bridge never guesses or reverses an encrypted route on the endpoint's behalf.

Ordinary path hashes support widths 1, 2 and 3. A flood already containing K or V cannot cross again, even if native flood-loop detection is disabled. Configured native loop checks count both prefixes only for the eligible unscoped peer floods that can carry V. Group text, adverts and scoped traffic retain their existing native loop behavior. New direct administration/PATH crossings require V to be the unique crossing prefix at the next hop. Physical-neighbor accounting does not treat V as an independent neighbor within that peer-flood scope. An unrelated packet class with a colliding prefix is not classified as a virtual hop. Consecutive V-to-K or V-to-V crossing hops are rejected, because they do not describe another RF receive at this bridge. A native K-starting route remains native, even when a later short prefix equals V.

TRACE uses the same K-local/V-crossing distinction at widths 1, 2, 4 and 8. A crossing whose next requested hop is immediately followed by K or V is rejected before queue admission or transmission. Native K-starting TRACE routes retain their local behavior. Each forwarded step appends one real received SNR, clamped to the native signed-byte range. There is no extra logical hop or fabricated extra sample.

## Boundaries

- CONTROL neighbor discovery is explicitly zero-hop in MeshCore and stays local. These settings do not turn it into cross-network neighbor discovery.
- Received advertisements stay on their radio. Remote contacts must be imported or otherwise known to their endpoints. This change does not enable cross-network advertisements.
- Transport-scoped flood/direct traffic does not cross under these settings. Scope authorization is not inferred from an ordinary route or channel mapping.
- Locally injected companion and daemon packets retain their explicit home radio. These new protocol settings apply to RF-received traffic. This avoids inventing a return path for a local origin.
- A direct crossing denied by its configured direction, pause or crossing policy has no same-radio fallback. An unconfigured V direction is also denied. Native K local repeating remains available in distinct mode. Legacy mode keeps its pre-existing behavior for a source with no direct route configured.
- A successful modem send is not proof that the remote endpoint received or accepted a request. Independent reception reports are optional and have their own trust boundary.

## Queue and echo behavior

Each direction has its own bounded queue admission and flood token bucket. Defaults are a burst of three floods with 12 tokens replenished per minute, and at most 16 pending crossings per direction. These apply even when `traffic_limits` is absent, including to existing group-text mappings after upgrade. Eligible group, discovery and administration floods share that direction's token bucket. Direct packets, including TRACE, bypass flood tokens but still require queue capacity. The existing global queue ceiling and per-radio duty-cycle gates also apply. Excess traffic is dropped, not replayed later.

Pause invalidates already queued crossings, including when the operator resumes before their send timer fires. The engine checks that generation under its TX lock before every attempt. Native local repeats do not use this crossing gate.

TRACE duplicate identity includes its progress count. Durable echo reservations cover both the received progress and the outgoing progress, so a restart does not repeat an already attempted step. Later return traversal remains eligible because its progress differs. Uncertain TX outcomes retain their reservations and are not retried. Known-unsent outcomes can use the existing bounded retry.

## Source and validation

The routing behavior was compared with the [legacy routing implementation](https://github.com/kesh33w/meshcore-bridge/blob/5eea466fd2a4fab9c87b5bf22a192d6269fc7777/host/routing.ts), its [packet validation](https://github.com/kesh33w/meshcore-bridge/blob/5eea466fd2a4fab9c87b5bf22a192d6269fc7777/host/packet.ts), and OpenHop Core at `55c44ca`. The legacy physical-radio/virtual-hop expansion is not used here. OpenHop retains its native repeater identity across the radios; distinct mode adds only an unsigned crossing route marker.

`tests/test_bridge_protocols.py` runs real OpenHop dispatcher/fabric and endpoint crypto/PATH handlers with fake radios. It covers both directions and same-side peers, encrypted login/path learning and reciprocal PATH, direct admin requests/responses, widths 1–3, TRACE widths 1/2/4/8 and return progress, restart echoes, pause/resume, rate isolation, malformed packets and excluded traffic. No live RF, MQTT or remote repeater test messages are sent by those fixtures. Passing them is not a field reliability claim.
