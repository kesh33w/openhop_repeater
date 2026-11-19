# Antenna identities in flood paths

When this node repeats a flood packet it adds its own hash as the last hop.
With `tx_identity_experiment` enabled, that hop is replaced by an alias, so
anyone looking at the packet on the mesh (an observer, CoreScope, a companion
viewing the path) can tell which of our antennas were involved.

Only our own final hop changes. The payload, header, hop count and packet hash
stay the same, so other nodes still treat the copies as one packet. Routes that
other nodes learn through an alias are mapped back to our real identity when
they come back to us.

## Modes

| Mode | What our hop names | Where aliases come from |
|---|---|---|
| `rx_tx` (default) | The antenna that heard the packet **and** the one that sent it | Derived from the node's public key and the two radio ids. Stable across restarts, no key files. |
| `tx` | The antenna that sent it | The `radios` public keys (the original 2026-11-18 experiment) |

`radios` aliases are always recognised in both modes, so routes learned during
the first experiment keep working after switching to `rx_tx`.

```yaml
tx_identity_experiment:
  enabled: true
  mode: rx_tx            # or tx
  radios:                # optional in rx_tx mode; keep any already deployed
    g3909: "<64 hex>"
```

To stop tagging but keep resolving routes that other nodes already learned, set
`enabled: false` and leave the rest of the section in place. Removing the
section entirely also stops recognising the aliases.

An invalid section never stops the repeater. It disables the experiment and logs
`TX identity experiment disabled: invalid configuration`. Unknown radio ids are
logged and skipped.

## Turning it on or off in the web UI

**Bridge settings → Transmitters → Antenna identities** offers three choices:

| Choice | Config |
|---|---|
| Off (single identity) | `enabled: false`. The section and its keys are kept, so routes learned through an alias still resolve. |
| Transmitting antenna | `enabled: true`, `mode: tx`. Needs `radios` keys. |
| Receiving + transmitting antenna | `enabled: true`, `mode: rx_tx` |

Saving applies at the next restart. The panel shows the saved and running modes, plus **Validate & restart…**. The same setting is available as `GET /api/antenna_identities` and `POST /api/update_antenna_identities {"mode": "off" | "tx" | "rx_tx"}`.

To retire the experiment completely, switch it Off and leave it for a few days, then remove the section from `config.yaml`. Any node still holding an old alias route falls back to flooding and relearns the single identity.

## Reading the aliases

At startup the repeater logs one line per alias:

```
TX identity alias 3f9a (crossing c41e): rx=g3910 tx=g3909
```

The same table is in the stats API as `tx_identity_aliases`
(`prefix`, `crossing_prefix`, `rx`, `tx`; `rx` is null for `radios` aliases).
The crossing prefix is used when the packet crossed the bridge.

## Not tagged

- **1-byte paths.** A 1-byte alias is shared by many real nodes, so it could not
  identify an antenna and would be mistaken for other nodes' hops. Aliases are
  only used, and only recognised, with 2- and 3-byte path hashes.
- **TRACE packets.** Their path holds per-hop SNR values, not node hashes.
- **Packets we originate.** They carry no hop of ours.

## Deploying

Deploy through the hub's upgrade guide, never by copying files into an
installed virtualenv, so the running code always matches git and an upgrade
cannot silently revert it. The first experiment (2026-11-18) was copied into a
release's virtualenv; that code is committed on `kesh/bridge` and has been
released since `kesh-2026.11.18`.

To switch modes, use **Bridge settings → Transmitters → Antenna identities** in
the web UI (`GET /api/antenna_identities`, `POST /api/update_antenna_identities`).
It saves the choice and applies it on restart. Production runs `rx_tx`.

The first deploy script also generated Ed25519 key pairs and stored their
private halves in `/etc/openhop_repeater/tx-experiment-identities.json`. Only
the public keys were ever used, as route aliases. The file was removed from the
Pi on 2026-11-19.

Configuration notes:

- Add or change the section with a text editor, or through any openHop
  settings save. openHop rewrites `config.yaml` with `yaml.safe_dump` on every
  settings save, so comments in that file never survive a save from the web UI
  either.
- Any tool that replaces `config.yaml` must keep its owner and group
  (`repeater:repeater`) as well as its mode. The first deploy kept the mode but
  not the owner, and the service failed to start for about a minute.
