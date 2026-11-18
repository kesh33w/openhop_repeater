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
    host-rf: "<64 hex>"
```

To stop tagging but keep resolving routes that other nodes already learned, set
`enabled: false` and leave the rest of the section in place. Removing the
section entirely also stops recognising the aliases.

An invalid section never stops the repeater. It disables the experiment and logs
`TX identity experiment disabled: invalid configuration`. Unknown radio ids are
logged and skipped.

## Reading the aliases

At startup the repeater logs one line per alias:

```
TX identity alias 3f9a (crossing c41e): rx=bay tx=host-rf
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

The first experiment (2026-11-18) was deployed by copying four files into the
release's virtualenv on the Pi (`/opt/openhop-production/releases/08d976a…`).
That code is now committed to `feat/ordered-dual-tx` and
`feat/ordered-tx-groups`. Deploy those branches through the normal upgrade
instead of copying files again, so the running code always matches git and an
upgrade cannot silently revert it.

The first deploy script also generated Ed25519 key pairs and stored their
private halves in `/etc/openhop_repeater/tx-experiment-identities.json`. Only
the public keys are used, as route aliases. Nothing reads that file, and it can
be deleted.

Configuration notes:

- Add or change the section with a text editor, or through any openHop
  settings save. openHop rewrites `config.yaml` with `yaml.safe_dump` on every
  settings save, so comments in that file never survive a save from the web UI
  either.
- Any tool that replaces `config.yaml` must keep its owner and group
  (`repeater:repeater`) as well as its mode. The first deploy kept the mode but
  not the owner, and the service failed to start for about a minute.
