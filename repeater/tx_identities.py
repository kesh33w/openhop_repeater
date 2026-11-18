"""Diagnostic per-antenna flood routing aliases.

When this node repeats a flood, it normally appends its own hash as the last
hop. With the experiment enabled, that hop is replaced by an alias that names
the antenna pair involved:

* ``rx_tx`` mode (default): one alias per (receiving radio, transmitting radio)
  pair, so anyone watching the mesh can tell which antenna heard the packet and
  which one sent it on. Pair aliases are derived from the node's own public key
  and the two radio ids, so they are stable across restarts and need no keys.
* ``tx`` mode: one alias per transmitting radio, from the configured
  ``radios`` public keys (the original 2026-11-18 experiment).

Configured ``radios`` aliases are always recognised, in both modes and while
disabled, so routes that remote nodes learned through them keep working.

Payloads, headers, hop counts and the packet hash are unchanged; only our own
final hop differs. Aliases resolve back to the native or crossing identity on
receipt. Not tagged:

* TRACE packets, whose path carries per-hop SNR values rather than hashes;
* packets we originate, which carry no hop of ours;
* 1-byte paths (see MIN_ALIAS_WIDTH).
"""

import copy
import hashlib
import logging

from repeater.bridge_protocols import crossing_route_identity
from openhop_core.protocol.constants import PAYLOAD_TYPE_TRACE

logger = logging.getLogger("TxIdentities")

# With 1-byte path hashes an alias is a single byte that many real nodes share,
# so a tag cannot identify an antenna and recognising one would claim other
# nodes' hops. Tag and recognise aliases only at 2- and 3-byte widths.
MIN_ALIAS_WIDTH = 2
MODES = ("rx_tx", "tx")
_PAIR_DOMAIN = b"openhop:tx-identity:rx-tx:v1\0"


class TxIdentities:
    def __init__(self, config, native, crossing=None, radio_ids=None):
        self.native = bytes(native)
        self.crossing = bytes(crossing) if crossing is not None else self.native
        self.enabled = False
        self.mode = "rx_tx"
        self.radios = {}  # tx radio id -> (alias, crossing alias)
        self.pairs = {}  # (rx radio id, tx radio id) -> (alias, crossing alias)
        self.aliases = {}  # alias -> canonical identity it stands for
        try:
            self._load(config.get("tx_identity_experiment") or {}, radio_ids)
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never stop the repeater
            # Fail safe: no tags and no alias recognition, but RF keeps running.
            self.enabled = False
            self.radios.clear()
            self.pairs.clear()
            self.aliases.clear()
            logger.error("TX identity experiment disabled: invalid configuration (%s)", exc)

    def _load(self, cfg, radio_ids):
        if not isinstance(cfg, dict):
            raise ValueError("tx_identity_experiment must be a mapping")
        enabled = cfg.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        mode = cfg.get("mode", "rx_tx")
        if mode not in MODES:
            raise ValueError("mode must be rx_tx or tx")
        radios = cfg.get("radios", {})
        if not isinstance(radios, dict):
            raise ValueError("radios must map radio ids to public keys")
        known = set(radio_ids) if radio_ids else None
        used = {self.native[0], self.crossing[0]}
        for rid, public in radios.items():
            if known is not None and rid not in known:
                logger.warning("TX identity for unknown radio %r ignored", rid)
                continue
            key = bytes.fromhex(public)
            if len(key) != 32:
                raise ValueError("TX identity must be a 32-byte public key")
            cross = crossing_route_identity(key) if self.crossing != self.native else key
            prefixes = {key[0], cross[0]}
            if 0 in prefixes or 255 in prefixes or prefixes & used:
                raise ValueError("TX identity prefixes collide")
            used.update(prefixes)
            self._add(key, cross)
            self.radios[rid] = (key, cross)
        # Only nodes that configured the experiment recognise pair aliases.
        # Keeping the section with enabled: false still resolves learned routes.
        if mode == "rx_tx" and cfg:
            for rx in radio_ids or ():
                for tx in radio_ids:
                    self.pairs[(rx, tx)] = self._derive_pair(rx, tx)
        self.mode = mode
        self.enabled = enabled

    def _add(self, key, cross):
        self.aliases[key] = self.native
        self.aliases[cross] = self.crossing

    def _derive_pair(self, rx, tx):
        """A stable alias whose 2-byte prefix is unique among all of ours."""
        taken = {key[:MIN_ALIAS_WIDTH] for key in self.aliases}
        taken |= {self.native[:MIN_ALIAS_WIDTH], self.crossing[:MIN_ALIAS_WIDTH]}
        seed = _PAIR_DOMAIN + self.native + str(rx).encode() + b"\0" + str(tx).encode()
        for counter in range(256):
            key = hashlib.sha256(seed + bytes([counter])).digest()
            cross = crossing_route_identity(key) if self.crossing != self.native else key
            heads = {key[:MIN_ALIAS_WIDTH], cross[:MIN_ALIAS_WIDTH]}
            expected = 1 if cross is key else 2  # Without a bridge there is no crossing alias.
            if (
                key[0] in (0, 255)
                or cross[0] in (0, 255)
                or heads & taken
                or len(heads) != expected
            ):
                continue
            self._add(key, cross)
            return key, cross
        raise ValueError(f"No free alias for radio pair {rx!r}->{tx!r}")

    def prefixes(self, width, crossing_only=False):
        if width < MIN_ALIAS_WIDTH:
            return set()
        return {
            key[:width]
            for key, canonical in self.aliases.items()
            if not crossing_only or canonical == self.crossing
        }

    def table(self):
        """Every alias as {prefix, crossing_prefix, rx, tx} for status displays."""
        rows = [
            {
                "prefix": key[:MIN_ALIAS_WIDTH].hex(),
                "crossing_prefix": cross[:MIN_ALIAS_WIDTH].hex(),
                "rx": rx,
                "tx": tx,
            }
            for (rx, tx), (key, cross) in self.pairs.items()
        ]
        rows += [
            {
                "prefix": key[:MIN_ALIAS_WIDTH].hex(),
                "crossing_prefix": cross[:MIN_ALIAS_WIDTH].hex(),
                "rx": None,
                "tx": tx,
            }
            for tx, (key, cross) in self.radios.items()
        ]
        return rows

    def canonical_direct(self, packet):
        """Map our aliases in a direct route back to the native/crossing identity.

        The whole route is normalised so the next-hop and loop guards see every
        hop of ours. Only 2- and 3-byte hashes are considered, where an alias
        matching an unrelated node is about 1 in 2,000 per hop rather than 1 in 30.
        """
        if (
            not self.aliases
            or not packet.is_route_direct()
            or packet.get_payload_type() == PAYLOAD_TYPE_TRACE
        ):
            return packet
        width = packet.get_path_hash_size()
        if width < MIN_ALIAS_WIDTH:
            return packet
        lookup = {alias[:width]: canonical[:width] for alias, canonical in self.aliases.items()}
        path = bytearray(packet.path)
        changed = False
        for offset in range(0, len(path) - width + 1, width):
            hop = bytes(path[offset : offset + width])
            if hop in lookup:
                path[offset : offset + width] = lookup[hop]
                changed = True
        if not changed:
            return packet
        result = copy.deepcopy(packet)
        result.path = path
        return result

    def tag(self, packet, radio_id):
        """Replace our final flood hop with the alias for this antenna pair.

        ``radio_id`` is the transmitting radio; the receiving radio comes from
        the packet. A hop that is already one of our aliases is re-tagged, so a
        copy made from an already-tagged packet still names its own transmitter.
        """
        if (
            not self.enabled
            or not packet.is_route_flood()
            or packet.get_payload_type() == PAYLOAD_TYPE_TRACE
        ):
            return False
        width = packet.get_path_hash_size()
        if width < MIN_ALIAS_WIDTH or len(packet.path) < width:
            return False
        pair = None
        if self.mode == "rx_tx":
            pair = self.pairs.get((getattr(packet, "_rx_radio_id", None), radio_id))
        pair = pair or self.radios.get(radio_id)
        if pair is None:
            return False
        last = bytes(packet.path[-width:])
        canonical = (
            self.native[:width]
            if last == self.native[:width]
            else (
                self.crossing[:width]
                if last == self.crossing[:width]
                else next(
                    (
                        target[:width]
                        for alias, target in self.aliases.items()
                        if alias[:width] == last
                    ),
                    None,
                )
            )
        )
        if canonical is None:
            return False
        key, crossing = pair
        packet.path[-width:] = (
            crossing if canonical == self.crossing[:width] and self.crossing != self.native else key
        )[:width]
        return True
