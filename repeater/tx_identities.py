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
* 1-byte paths (see MIN_ALIAS_WIDTH), unless ``one_byte`` codes are set.

``one_byte`` opts chosen radios into tagging 1-byte paths with hand-picked
codes. A 1-byte code is only safe while no real relay in radio range uses that
byte, so pick bytes that are free on the radio's own mesh (no repeater or room
heard advertising it, none seen as a local hop) and keep watching: the first
time a repeater or room advertising a matching key is heard within
MAX_COLLISION_HOPS, that code is retired until restart, and adverts stored in
the last 30 days retire it again at startup. Relays further away cannot hear
packets addressed to us, so they do not collide.

    one_byte:
      g3909: [d4, "14"]   # native code, crossing code (bridged nodes)
      rak909: ["12", d3]
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
# Advert contact types that relay and so own a hop byte (repeater, room server).
RELAY_CONTACT_TYPES = (2, 3)
# A relay collides with a one-byte code only if it can hear direct packets meant
# for us: one that hears our neighbours, so its advert reaches us in <= 1 relay.
# One extra hop of margin.
MAX_COLLISION_HOPS = 2
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
        self.one_byte = {}  # tx radio id -> (code, crossing code), one byte each
        self.retired = {}  # one-byte code -> why it stopped being used
        try:
            self._load(config.get("tx_identity_experiment") or {}, radio_ids)
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never stop the repeater
            # Fail safe: no tags and no alias recognition, but RF keeps running.
            self.enabled = False
            self.radios.clear()
            self.pairs.clear()
            self.aliases.clear()
            self.one_byte.clear()
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
        self._load_one_byte(cfg.get("one_byte", {}), known)
        # Only nodes that configured the experiment recognise pair aliases.
        # Keeping the section with enabled: false still resolves learned routes.
        if mode == "rx_tx" and cfg:
            for rx in radio_ids or ():
                for tx in radio_ids:
                    self.pairs[(rx, tx)] = self._derive_pair(rx, tx)
        self.mode = mode
        self.enabled = enabled

    def _load_one_byte(self, section, known):
        if not isinstance(section, dict):
            raise ValueError("one_byte must map radio ids to [code, crossing code]")
        used = {self.native[0], self.crossing[0], 0, 255}
        for rid, codes in section.items():
            if known is not None and rid not in known:
                logger.warning("One-byte TX code for unknown radio %r ignored", rid)
                continue
            if isinstance(codes, (str, int)):
                codes = [codes]
            # Without a crossing code, bridged (crossing) hops from this radio stay untagged.
            if not isinstance(codes, (list, tuple)) or len(codes) not in (1, 2):
                raise ValueError("one_byte needs [code] or [code, crossing code] per radio")
            # YAML reads unquoted 12 or 14 as integers; treat them as the hex text written.
            parsed = [bytes.fromhex(f"{c:02d}" if isinstance(c, int) else str(c)) for c in codes]
            if any(len(code) != 1 for code in parsed):
                raise ValueError("one_byte codes must be one byte (two hex digits)")
            if {code[0] for code in parsed} & used or len(set(parsed)) != len(parsed):
                raise ValueError("one_byte codes collide with each other or our own hash")
            used.update(code[0] for code in parsed)
            self.one_byte[rid] = (parsed[0], parsed[1] if len(parsed) == 2 else None)

    def _lookup(self, width, include_retired=False):
        """Each alias prefix at ``width`` -> the canonical prefix it stands for."""
        if width == 1:
            lookup = {}
            for code, cross in self.one_byte.values():
                lookup[code] = self.native[:1]
                if cross is not None:
                    lookup[cross] = self.crossing[:1]
            return {k: v for k, v in lookup.items() if include_retired or k not in self.retired}
        if width < MIN_ALIAS_WIDTH:
            return {}
        return {alias[:width]: canonical[:width] for alias, canonical in self.aliases.items()}

    def retire(self, code, reason):
        """Stop tagging with and recognising a one-byte code. True if it was live."""
        code = bytes(code[:1])
        live = any(code in pair for pair in self.one_byte.values())
        if not live or code in self.retired:
            return False
        self.retired[code] = reason
        logger.warning("One-byte TX code %s retired: %s", code.hex().upper(), reason)
        return True

    def note_advert(self, public_key, contact_type_id, name=None, hops=0):
        """Retire a one-byte code when a nearby repeater or room advertises it.

        ``hops`` is how many relays the advert passed through to reach us.
        """
        if (
            not self.one_byte
            or contact_type_id not in RELAY_CONTACT_TYPES
            or hops is None
            or hops > MAX_COLLISION_HOPS
        ):
            return False
        try:
            key = bytes.fromhex(public_key) if isinstance(public_key, str) else bytes(public_key)
        except ValueError:
            return False
        if len(key) != 32:
            return False
        return self.retire(
            key[:1], f"relay {name or key[:4].hex()} advertises {key[:4].hex()} {hops} hop(s) away"
        )

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
        return {
            alias
            for alias, canonical in self._lookup(width).items()
            if not crossing_only or canonical == self.crossing[:width]
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
        rows += [
            {
                "prefix": code.hex(),
                "crossing_prefix": cross.hex() if cross is not None else None,
                "rx": None,
                "tx": tx,
                "width": 1,
                "retired": sorted(c.hex() for c in (code, cross) if c in self.retired),
            }
            for tx, (code, cross) in self.one_byte.items()
        ]
        return rows

    def status_labels(self):
        """Every alias key with a short label, for observer status messages.

        Lets a map such as CoreScope name alias hops. Keys are derived from our
        public key and radio ids (or are configured public keys), so publishing
        them reveals nothing private. A crossing alias is labelled with the
        crossing identity's prefix and marked as bridged.
        """
        native = self.native[:MIN_ALIAS_WIDTH].hex().upper()
        crossing = self.crossing[:MIN_ALIAS_WIDTH].hex().upper()
        entries = [((rx, tx), pair) for (rx, tx), pair in self.pairs.items()]
        entries += [((None, tx), pair) for tx, pair in self.radios.items()]
        rows = []
        for (rx, tx), (key, cross) in entries:
            route = f"{rx}→{tx}" if rx is not None else f"tx {tx}"
            rows.append({"key": key.hex(), "label": f"{native} {route}"})
            if cross != key:
                rows.append({"key": cross.hex(), "label": f"{crossing} {route} (bridged)"})
        return rows

    def canonical_direct(self, packet):
        """Map our aliases in a direct route back to the native/crossing identity.

        The whole route is normalised so the next-hop and loop guards see every
        hop of ours. Only 2- and 3-byte hashes are considered, where an alias
        matching an unrelated node is about 1 in 2,000 per hop rather than 1 in 30,
        plus configured one-byte codes that are not retired.
        """
        if not packet.is_route_direct() or packet.get_payload_type() == PAYLOAD_TYPE_TRACE:
            return packet
        width = packet.get_path_hash_size()
        lookup = self._lookup(width)
        if not lookup:
            return packet
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
        if len(packet.path) < width or width < 1:
            return False
        if width == 1:
            pair = self.one_byte.get(radio_id)
        elif width >= MIN_ALIAS_WIDTH:
            pair = None
            if self.mode == "rx_tx":
                pair = self.pairs.get((getattr(packet, "_rx_radio_id", None), radio_id))
            pair = pair or self.radios.get(radio_id)
        else:
            return False
        if pair is None:
            return False
        last = bytes(packet.path[-width:])
        if last == self.native[:width]:
            canonical = self.native[:width]
        elif last == self.crossing[:width]:
            canonical = self.crossing[:width]
        else:
            canonical = self._lookup(width, include_retired=True).get(last)
        if canonical is None:
            return False
        key, crossing = pair
        code = (
            crossing if canonical == self.crossing[:width] and self.crossing != self.native else key
        )
        if code is None:
            return False
        code = code[:width]
        if code in self.retired:
            # Put our real hop back rather than leave a retired code on air.
            packet.path[-width:] = canonical
            return False
        packet.path[-width:] = code
        return True
