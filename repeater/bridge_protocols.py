"""Opaque MeshCore transit through the bridge's shared repeater identity.

Only unscoped routes cross. CONTROL is zero-hop discovery and stays local.
TRACE's path stores SNR samples, not routing hashes; its route is in the payload.
Peer ciphertext is structurally checked here but only endpoints authenticate it.
"""

import hashlib
import math

from openhop_core.protocol.constants import (
    MAX_PACKET_PAYLOAD,
    MAX_PATH_SIZE,
    PAYLOAD_TYPE_ACK,
    PAYLOAD_TYPE_ANON_REQ,
    PAYLOAD_TYPE_MULTIPART,
    PAYLOAD_TYPE_PATH,
    PAYLOAD_TYPE_REQ,
    PAYLOAD_TYPE_RESPONSE,
    PAYLOAD_TYPE_TRACE,
    PAYLOAD_TYPE_TXT_MSG,
    PH_ROUTE_MASK,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
)
from openhop_core.protocol.packet_utils import PathUtils

_PEER_TYPES = {PAYLOAD_TYPE_REQ, PAYLOAD_TYPE_RESPONSE, PAYLOAD_TYPE_TXT_MSG, PAYLOAD_TYPE_PATH}
_ADMIN_TYPES = {
    PAYLOAD_TYPE_REQ,
    PAYLOAD_TYPE_RESPONSE,
    PAYLOAD_TYPE_ANON_REQ,
    PAYLOAD_TYPE_PATH,
    # CLI text is encrypted like a DM; the bridge cannot inspect its subtype.
    PAYLOAD_TYPE_TXT_MSG,
    PAYLOAD_TYPE_ACK,
}
_DISCOVERY_TYPES = {PAYLOAD_TYPE_TXT_MSG, PAYLOAD_TYPE_ACK, PAYLOAD_TYPE_PATH}


def crossing_route_identity(public_key):
    """Derive a stable public route marker, not a signed endpoint or secret.

    A distinct first byte separates native and crossing hops at every supported
    route width. Domain separation makes the marker stable across restarts.
    """
    public_key = bytes(public_key)
    if len(public_key) != 32:
        raise ValueError("Distinct bridge routing requires the full native public key")
    marker = bytearray(hashlib.sha256(b"openhop:bridge:crossing:v1\0" + public_key).digest())
    if marker[0] == public_key[0]:
        marker[0] ^= 1
    return bytes(marker)


def _valid_frame(packet):
    payload = bytes(packet.payload or b"")
    path = bytes(packet.path or b"")
    return (
        packet.header >> 6 == 0
        and 0 < len(payload) <= MAX_PACKET_PAYLOAD
        and PathUtils.is_valid_path_len(packet.path_len)
        and len(path) == packet.get_path_hash_size() * packet.get_path_hash_count()
        and len(path) <= MAX_PATH_SIZE
        and len(path) + len(payload) + 2 <= 255
    )


def _valid_peer_payload(packet):
    size = len(packet.payload or b"")
    kind = packet.get_payload_type()
    if kind in _PEER_TYPES:
        return size >= 20 and (size - 4) % 16 == 0
    if kind == PAYLOAD_TYPE_ANON_REQ:
        return size >= 51 and (size - 35) % 16 == 0
    return kind == PAYLOAD_TYPE_ACK and size >= 4


def _enabled(packet, protocols):
    kind = packet.get_payload_type()
    return (
        protocols.get("discovery", False)
        and kind in _DISCOVERY_TYPES
        or protocols.get("repeater_admin", False)
        and kind in _ADMIN_TYPES
    )


def is_protocol_flood_candidate(packet, protocols):
    """Scope the unsigned marker to wire types the protocol bridge can emit."""
    return (
        packet.header & PH_ROUTE_MASK == ROUTE_TYPE_FLOOD
        and _enabled(packet, protocols)
        and _valid_frame(packet)
        and _valid_peer_payload(packet)
    )


def is_protocol_flood(packet, local_prefix, protocols, crossing_prefix=None):
    """Check the received flood before native routing appends this bridge hop."""
    if not is_protocol_flood_candidate(packet, protocols):
        return False
    width = packet.get_path_hash_size()
    own = bytes(local_prefix[:width])
    forbidden = {own}
    if crossing_prefix is not None:
        forbidden.add(bytes(crossing_prefix[:width]))
    return len(own) == width and all(
        bytes(packet.path[i : i + width]) not in forbidden
        for i in range(0, len(packet.path), width)
    )


def _is_next_hop(packet, local_prefix):
    width = packet.get_path_hash_size()
    own = bytes(local_prefix[:width])
    path = bytes(packet.path)
    return (
        len(own) == width
        and len(path) >= width
        and path[:width] == own
        and all(path[i : i + width] != own for i in range(width, len(path), width))
    )


def is_legacy_direct(packet, local_prefix):
    """Apply wire validation to the existing opt-in DM/ACK crossing path too."""
    if packet.header & PH_ROUTE_MASK != ROUTE_TYPE_DIRECT or not _valid_frame(packet):
        return False
    kind = packet.get_payload_type()
    payload = bytes(packet.payload)
    valid = (
        kind in (PAYLOAD_TYPE_TXT_MSG, PAYLOAD_TYPE_ACK)
        and _valid_peer_payload(packet)
        or kind == PAYLOAD_TYPE_MULTIPART
        and len(payload) >= 5
        and payload[0] & 0x0F == PAYLOAD_TYPE_ACK
    )
    return valid and _is_next_hop(packet, local_prefix)


def is_protocol_direct(packet, local_prefix, protocols):
    """Validate encrypted admin/PATH transit before native next-hop consumption."""
    return (
        packet.header & PH_ROUTE_MASK == ROUTE_TYPE_DIRECT
        and _enabled(packet, protocols)
        and _valid_frame(packet)
        and _valid_peer_payload(packet)
        and _is_next_hop(packet, local_prefix)
    )


def is_trace_transit(packet, local_prefix):
    """Match the next immutable TRACE hop against 1/2/4/8 bytes of our key."""
    if (
        packet.get_payload_type() != PAYLOAD_TYPE_TRACE
        or packet.header & PH_ROUTE_MASK != ROUTE_TYPE_DIRECT
        or not _valid_frame(packet)
        or packet.get_path_hash_size() != 1
        or packet.path_len >= 63
        or len(packet.payload) < 10
    ):
        return False
    width = 1 << (packet.payload[8] & 3)
    route = bytes(packet.payload[9:])
    if len(route) % width or len(route) // width > 63:
        return False
    offset = packet.path_len * width
    return (
        offset < len(route)
        and len(local_prefix) >= width
        and route[offset : offset + width] == bytes(local_prefix[:width])
    )


def append_trace_snr(packet, local_prefix, snr):
    """Advance validated TRACE by one real ingress sample; never rewrite its route."""
    if not is_trace_transit(packet, local_prefix):
        return False
    if not isinstance(snr, (int, float)) or not math.isfinite(snr):
        return False
    packet.path = bytearray(packet.path) + bytes([int(max(-32, min(31.75, snr)) * 4) & 0xFF])
    packet.path_len += 1
    return True


def trace_record_fields(packet):
    """Separate the requested TRACE route from its received SNR progress."""
    payload = bytes(packet.payload or b"")
    width = 1 << (payload[8] & 3) if len(payload) >= 9 else 1
    route = payload[9:]
    hops = [route[i : i + width].hex().upper() for i in range(0, len(route), width)]
    details = [
        {"hash": hops[i], "snr_raw": value, "snr_db": (value if value < 128 else value - 256) / 4}
        for i, value in enumerate(packet.path)
        if i < len(hops)
    ]
    return {
        "is_trace": True,
        "original_path": hops,
        "path_hash": "[" + ", ".join(hops[:8] + (["..."] if len(hops) > 8 else [])) + "]",
        "path_hash_size": width,
        "forwarded_path": None,
        "path_snr_details": details,
        "path_snrs": [f"{row['snr_raw']}({row['snr_db']:.1f}dB)" for row in details],
    }
