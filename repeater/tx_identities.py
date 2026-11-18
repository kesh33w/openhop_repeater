"""Temporary per-physical-transmitter flood routing aliases.

Payloads remain unchanged. Aliases resolve to native/crossing routes on receipt;
TRACE and originated zero-hop packets are deliberately not tagged.
"""
import copy
from repeater.bridge_protocols import crossing_route_identity
from openhop_core.protocol.constants import PAYLOAD_TYPE_TRACE

class TxIdentities:
    def __init__(self, config, native, crossing=None):
        cfg = config.get('tx_identity_experiment', {})
        self.enabled = cfg.get('enabled', False) is True
        self.native = bytes(native)
        self.crossing = bytes(crossing) if crossing is not None else self.native
        self.radios = {}
        self.aliases = {}
        used = {self.native[0], self.crossing[0]}
        for rid, public in cfg.get('radios', {}).items():
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
