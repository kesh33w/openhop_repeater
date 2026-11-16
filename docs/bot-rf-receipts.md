# Raw-RF cancellation receipts

This stream proves delivery of local raw RF observations to the fallback cancellation callback. It does not prove complete decoded command delivery or RF reception of bytes the host has not read.

## Hooks and ordering

The USB and TCP modem reader loops checkpoint only after parsing their preceding buffer. Each fence posts completion on the same event loop as preceding physical RX callbacks. CRC, malformed framing, disconnect and callback faults advance the reader generation. The fabric observation hook precedes dispatcher deduplication. Ordinary bridge delivery continues when the optional proof subscriber fails.

The backend binds exactly the configured 909 directional/V3 membership, actual companion public key and listening port, designated TX, #test channel key, and initialized driver frequency/modulation attributes. Fabric callback ownership and observation registration are checked throughout the session. The endpoint host is a configuration assertion; it is not independently resolved against the listener bind address. The attributes are not a new radio readback.

An authenticated private Unix stream assigns ordered receipt sequences. The bot completes raw cancellation before acknowledging each receipt. A barrier waits for both reader fences and all corresponding acknowledgements. Requests use a strictly increasing session-local integer and echoed nonce, with constant memory. The final one-use permit follows channel readback; new observations invalidate unused permits. Unread modem/kernel bytes and arrivals after this cut are outside the proof.

Queue overflow, reader generation changes, callback replacement/removal, channel or membership changes, lost acknowledgements, malformed protocol and heartbeat loss revoke fallback permission. They do not stop ordinary radio reception. A reconnect requires a new baseline and full grace interval.

## Private socket lifecycle

Default socket mode is 0600. Optional `socket_group_access: true` requires an existing service-owned directory with exact mode 0710 and creates only the new socket as 0660 with that directory's group. No existing directory or secret permissions are changed. The client separately verifies expected UID/GID, group membership and connected peer credentials. Root coordinates the deployment-specific UID and container mount plan.

A service-owned 0600 lock file is held with nonblocking flock for the server lifetime. Startup can remove an owned stale socket only after connection refusal and unchanged inode verification. Active sockets, symlinks, ordinary files and unowned paths are refused. Close removes only this server's socket inode. Secret files remain private 0600 and use raw 32–256-byte shared material; never put real secrets in this tree.

## Validation and deployment

The combined bot/backend receipt suite passes241 tests, including real temporary Unix sockets and actual USB/TCP reader workers with fake modem I/O. All modem write methods in the reader tests raise. The production no-send check ran180seconds, acknowledged31 records and delivered27 natural raw callbacks, then passed its final barrier. No qualifying MQTT-only reply was observed in that window.

Configure this optional service only with the matching bot client, a private secret, exact companion identity and current receiver membership. Use a rollback-protected restart and run the bot validate_receipts.py helper before enabling fallback. Keep configuration and keys outside Git. Restore old source/config/image on failure without rewinding live databases or durable claims.

The companion channel store may expose the exact16-byte #test key or its32-byte zero-padded representation; both are accepted, while other lengths/padding are rejected.
