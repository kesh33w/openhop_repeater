"""Temporary per-physical-transmitter flood routing aliases.

Payloads remain unchanged. Aliases resolve to native/crossing routes on receipt;
TRACE and originated zero-hop packets are deliberately not tagged.
"""
import copy
import logging

from repeater.bridge_protocols import crossing_route_identity
from openhop_core.protocol.constants import PAYLOAD_TYPE_TRACE

logger = logging.getLogger("TxIdentities")


class TxIdentities:
    def __init__(self, config, native, crossing=None, radio_ids=None):
        self.native = bytes(native)
        self.crossing = bytes(crossing) if crossing is not None else self.native
        self.enabled = False
        self.radios = {}
        self.aliases = {}
        try:
            self._load(config.get('tx_identity_experiment') or {}, radio_ids)
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never stop the repeater
            # Fail safe: no tags and no alias recognition, but RF keeps running.
            self.enabled = False
            self.radios.clear()
            self.aliases.clear()
            logger.error("TX identity experiment disabled: invalid configuration (%s)", exc)

    def _load(self, cfg, radio_ids):
        if not isinstance(cfg, dict):
            raise ValueError('tx_identity_experiment must be a mapping')
        enabled = cfg.get('enabled', False)
        if type(enabled) is not bool:
            raise ValueError('enabled must be true or false')
        radios = cfg.get('radios', {})
        if not isinstance(radios, dict):
            raise ValueError('radios must map radio ids to public keys')
        known = set(radio_ids) if radio_ids else None
        used = {self.native[0], self.crossing[0]}
        for rid, public in radios.items():
            if known is not None and rid not in known:
                logger.warning("TX identity for unknown radio %r ignored", rid)
                continue
            key = bytes.fromhex(public)
            if len(key) != 32:
                raise ValueError('TX identity must be a 32-byte public key')
            cross = crossing_route_identity(key) if self.crossing != self.native else key
            prefixes = {key[0], cross[0]}
            if 0 in prefixes or 255 in prefixes or prefixes & used:
                raise ValueError('TX identity prefixes collide')
            used.update(prefixes)
            self.radios[rid] = (key, cross)
            self.aliases[key] = self.native
            self.aliases[cross] = self.crossing
        self.enabled = enabled

    def prefixes(self, width, crossing_only=False):
        return {key[:width] for key, canonical in self.aliases.items()
                if not crossing_only or canonical == self.crossing}

    def canonical_direct(self, packet):
        if not self.aliases or not packet.is_route_direct() or packet.get_payload_type() == PAYLOAD_TYPE_TRACE:
            return packet
        width = packet.get_path_hash_size()
        path = bytearray(packet.path)
        changed = False
        for offset in range(0, len(path), width):
            for alias, canonical in self.aliases.items():
                if bytes(path[offset:offset+width]) == alias[:width]:
                    path[offset:offset+width] = canonical[:width]
                    changed = True
                    break
        if not changed:
            return packet
        result = copy.deepcopy(packet)
        result.path = path
        return result

    def tag(self, packet, radio_id):
        if not self.enabled or radio_id not in self.radios or not packet.is_route_flood() or packet.get_payload_type() == PAYLOAD_TYPE_TRACE:
            return False
        width = packet.get_path_hash_size()
        if len(packet.path) < width:
            return False
        last = bytes(packet.path[-width:])
        key, crossing = self.radios[radio_id]
        if last == self.native[:width]:
            packet.path[-width:] = key[:width]
        elif last == self.crossing[:width]:
            packet.path[-width:] = crossing[:width]
        else:
            return False
        return True
