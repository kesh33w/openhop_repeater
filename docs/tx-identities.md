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

Every MQTT status message also lists the aliases, so maps can name them
without anyone looking them up:

```json
"hop_aliases": [{"key": "<64 hex>", "label": "BD6E g3910→rak910"}, ...]
```

The label is our node's 2-byte prefix, then the receiving and transmitting
radio (`tx <radio>` for `radios` aliases). Crossing aliases use the crossing
identity's prefix and end in `(bridged)`. The keys are derived from public
values only. CoreScope names them when its `hopAliases` setting trusts this
observer's key (SacMesh CoreScope fork).

## Not tagged

- **1-byte paths**, unless `one_byte` codes are configured (below). A 1-byte
  alias is normally shared by real nodes, so it could not identify an antenna
  and would be mistaken for other nodes' hops.
- **TRACE packets.** Their path holds per-hop SNR values, not node hashes.
- **Packets we originate.** They carry no hop of ours.

## One-byte codes

Most floods on a busy mesh use 1-byte paths, which the aliases above cannot
tag. `one_byte` gives chosen transmitters a hand-picked 1-byte code, in either
mode, for bytes that no relay on that radio's mesh uses:

```yaml
tx_identity_experiment:
  one_byte:
    g3909: [d4, "14"]    # code for our native hop, code for our crossing hop
    rak909: ["12", d3]   # quote codes that are all digits; YAML reads 12 as a number
    g3910: ["44", "37"]
    rak910: ["49", 8b]
```

Give a second (crossing) code to also tag packets that crossed the bridge; with
one code only native hops are tagged. Codes may not be `00`, `ff`, our own or
our crossing identity's first byte, or repeat. Codes on other radios' meshes do
not matter, so check only the mesh each radio transmits on.

**Choosing bytes.** A byte is taken if CoreScope places a repeater or room using
it on that mesh, if the Pi heard a repeater or room advertise it there, or if it
appears among the last three hops of packets heard on that mesh's radios. On
2026-11-20 the Sacramento 909.875 mesh had 71 free bytes; `d4 12 14 d3` were
picked because they are also unused on the Bay mesh.

The Bay 910.525 mesh is far denser: 238 of 256 bytes belong to Bay relays on
CoreScope. On 2026-11-21, `44 37 49 8b` were the only bytes with no CoreScope
Bay relay, at most two local-hop sightings on the 910 radios in 30 days, and no
relay within 3 hops (nearest 4, 11, 5 and 9 hops). Expect these codes to be
retired sooner as new Bay relays appear; pick replacements the same way.

**Staying collision-free.** The moment a repeater or room advertising a key
that starts with one of the codes is heard within 2 hops, that code is retired: it stops being
tagged and stops being recognised as ours, and the repeater logs `One-byte TX
code D4 retired: relay <name> advertises d4…`. At startup, relays heard within 2 hops
in the last 30 days (stored advert packets) retire codes the same way. Relays
further away cannot hear packets addressed to this node, so they are ignored;
adverts reach the Pi from across the state, and on 2026-11-21 the nearest
relays sharing `d4`, `12` and `14` were 5, 7 and 9 hops away. `tx_identity_aliases`
lists one-byte codes with `width: 1` and the `retired` codes. Retiring lasts
until restart; to bring a code back for good, pick a new free byte.

Remaining risk: a relay that has not advertised since we last heard it could
still share a code. Its direct routes through that byte would be taken as ours,
just as already happens for our own native 1-byte hash.

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
