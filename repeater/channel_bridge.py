"""Opt-in channel crossings. Shared-channel authentication does not verify a sender.

The caller performs ordinary routing first, then schedules each returned copy through
its existing radio queue. This module never transmits or reports TX success.
"""

import copy
import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_ACK,
    PAYLOAD_TYPE_GRP_TXT,
    PAYLOAD_TYPE_MULTIPART,
    PAYLOAD_TYPE_TXT_MSG,
    PH_ROUTE_MASK,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
)
from openhop_core.protocol.crypto import CryptoUtils
from openhop_core.protocol.packet_utils import PathUtils

from repeater.radio_ids import configured_radio_ids


@dataclass(frozen=True)
class Destination:
    kind: str
    name: str

    def __post_init__(self):
        if self.kind != "radio" or not self.name:
            raise ValueError("Only named radio destinations are implemented")


@dataclass(frozen=True)
class Channel:
    name: str
    key: bytes = field(repr=False)

    def __post_init__(self):
        if not self.name or len(self.key) not in (16, 32):
            raise ValueError("Channels require a name and a 16 or 32 byte key")

    @property
    def secret(self):
        return self.key.ljust(32, b"\0")

    @property
    def selector(self):
        key = self.key[:16] if self.secret[16:] == bytes(16) else self.key
        return hashlib.sha256(key).digest()[0]


@dataclass(frozen=True)
class Crossing:
    source: Destination
    target: Destination
    source_channel: str
    target_channel: str


@dataclass(frozen=True)
class PlannedSend:
    destination: Destination
    packet: object
    crossing: bool


class ChannelBridge:
    """Plan independent local repeats and explicitly mapped plain group crossings."""

    def __init__(self, channels, crossings):
        self.channels = {c.name: c for c in channels}
        if len(self.channels) != len(channels):
            raise ValueError("Duplicate channel names")
        self.crossings = tuple(crossings)
        seen = set()
        for rule in self.crossings:
            if rule.source == rule.target:
                raise ValueError("A crossing needs distinct destinations")
            if rule.source_channel not in self.channels or rule.target_channel not in self.channels:
                raise ValueError("Unknown channel in crossing")
            index = (rule.source, rule.target, rule.source_channel)
            if index in seen:
                raise ValueError("Ambiguous crossing rules")
            seen.add(index)

    def authenticate(self, packet):
        # Other route types, signed group subtypes, DMs and control fail closed.
        if packet.get_payload_type() != PAYLOAD_TYPE_GRP_TXT:
            return None
        if packet.header & PH_ROUTE_MASK != ROUTE_TYPE_FLOOD:
            return None
        payload = bytes(packet.payload)
        if len(payload) < 19 or (len(payload) - 3) % 16:
            return None
        matches = []
        for channel in self.channels.values():
            if payload[0] != channel.selector:
                continue
            try:
                plain = CryptoUtils.mac_then_decrypt(
                    channel.secret[:16], channel.secret, payload[1:]
                )
            except ValueError:
                continue
            # Count every MAC match before parsing. A second matching configured
            # identity is ambiguous even if only one yields supported plaintext.
            matches.append((channel, plain))
        if len(matches) != 1:
            return None
        channel, plain = matches[0]
        if len(plain) < 6 or plain[4] != 0:
            return None
        try:
            content = plain[5:].rstrip(b"\0").decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
        if "\0" in content or ": " not in content:
            return None
        return channel, plain

    def plan(self, packet, home, *, repeat_local=True, allow_crossing=True):
        if home is None:
            # Origins must carry an explicit home; path length is not evidence.
            return ()
        sends = [PlannedSend(home, copy.deepcopy(packet), False)] if repeat_local else []
        authenticated = self.authenticate(packet) if allow_crossing else None
        if authenticated is None:
            return tuple(sends)
        source, plain = authenticated
        for rule in self.crossings:
            if rule.source != home or rule.source_channel != source.name:
                continue
            target = self.channels[rule.target_channel]
            outgoing = copy.deepcopy(packet)
            outgoing.payload = bytearray([target.selector]) + CryptoUtils.encrypt_then_mac(
                target.secret[:16], target.secret, plain
            )
            outgoing.payload_len = len(outgoing.payload)
            sends.append(PlannedSend(rule.target, outgoing, True))
        return tuple(sends)


class EchoLedger:
    """Bounded restart-safe reservations for original and translated packet hashes.

    Reserve before dispatch. Commit only after confirmed TX, release on a known
    failure. A crash leaves a reservation until expiry, deliberately suppressing
    a possibly transmitted echo. Never evict live reservations to admit traffic.
    Store hashes only, with no keys or plaintext. The owner closes the ledger.
    """

    def __init__(self, path: Path, *, ttl=300, capacity=1000, clock=time.time):
        if not 1 <= ttl <= 3600 or not 1 <= capacity <= 10000:
            raise ValueError("Invalid echo ledger limits")
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS echoes (token TEXT, hash TEXT, expires REAL, state TEXT, PRIMARY KEY(token, hash))"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS echo_hash ON echoes(hash)")
        self.db.commit()

    @staticmethod
    def digest(packet):
        return packet.calculate_packet_hash().hex()

    def contains(self, packet):
        now = self.clock()
        return (
            self.db.execute(
                "SELECT 1 FROM echoes WHERE hash=? AND expires>? LIMIT 1",
                (self.digest(packet), now),
            ).fetchone()
            is not None
        )

    def reserve(self, source, outgoing):
        hashes = sorted({self.digest(source), self.digest(outgoing)})
        token = hashlib.sha256("|".join(hashes).encode()).hexdigest()
        now = self.clock()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("DELETE FROM echoes WHERE expires<=?", (now,))
            if any(
                self.db.execute("SELECT 1 FROM echoes WHERE hash=?", (h,)).fetchone()
                for h in hashes
            ):
                return None
            count = self.db.execute("SELECT count(*) FROM echoes").fetchone()[0]
            if count + len(hashes) > self.capacity:
                return None
            self.db.executemany(
                "INSERT INTO echoes VALUES (?, ?, ?, 'pending')",
                [(token, h, now + self.ttl) for h in hashes],
            )
        return token

    def finish(self, token, *, success):
        with self.db:
            if success:
                self.db.execute("UPDATE echoes SET state='sent' WHERE token=?", (token,))
            else:
                self.db.execute("DELETE FROM echoes WHERE token=? AND state='pending'", (token,))

    def close(self):
        self.db.close()


class BridgeSettings:
    """Validated configuration, with no hardware or database side effects."""

    def __init__(self, config):
        from repeater.policy_engine import PolicyEngine

        cfg = config["channel_bridge"]
        allowed = {
            "enabled",
            "channels",
            "mappings",
            "direct_routes",
            "echo_database",
            "echo_ttl_seconds",
            "echo_capacity",
            "max_pending_crossings",
            "queue_ttl_seconds",
            "origin_home",
            "local_policy",
            "crossing_policy",
        }
        if set(cfg) - allowed:
            raise ValueError("Unknown channel_bridge setting")
        ids = configured_radio_ids(config)
        if len(ids) != 2:
            raise ValueError("channel_bridge requires exactly two distinct named radios")
        self.radio_ids = tuple(ids)
        self.origin_home = cfg.get("origin_home")
        if self.origin_home is not None and self.origin_home not in ids:
            raise ValueError("channel_bridge.origin_home must name a configured radio")
        from repeater.bridge_homes import validate_bridge_homes

        validate_bridge_homes(config)
        routes = cfg.get("direct_routes", [])
        if not isinstance(routes, list) or len(routes) > len(ids):
            raise ValueError("channel_bridge.direct_routes must be a bounded list")
        self.direct_routes = {}
        for rule in routes:
            if not isinstance(rule, dict) or set(rule) != {"source_radio", "target_radio"}:
                raise ValueError("Invalid channel_bridge direct route fields")
            source, target = rule["source_radio"], rule["target_radio"]
            if source not in ids or target not in ids or source == target:
                raise ValueError("Direct routes require distinct configured radios")
            if source in self.direct_routes:
                raise ValueError("Direct routes require one destination per source radio")
            self.direct_routes[source] = target
        channels = cfg.get("channels", {})
        if not isinstance(channels, dict) or len(channels) > 64 or (not channels and not routes):
            raise ValueError("channel_bridge requires named channel keys or direct routes")
        try:
            parsed = [Channel(name, bytes.fromhex(key)) for name, key in channels.items()]
        except (TypeError, ValueError):
            raise ValueError(
                "channel_bridge channel keys must be 16 or 32 byte hex strings"
            ) from None
        mappings = cfg.get("mappings", [])
        if not isinstance(mappings, list) or len(mappings) > 128:
            raise ValueError("channel_bridge.mappings must be a bounded list")
        crossings = []
        for rule in mappings:
            if not isinstance(rule, dict) or set(rule) != {
                "source_radio",
                "target_radio",
                "source_channel",
                "target_channel",
            }:
                raise ValueError("Invalid channel_bridge mapping fields")
            if rule["source_radio"] not in ids or rule["target_radio"] not in ids:
                raise ValueError("Unknown radio in channel_bridge mapping")
            crossings.append(
                Crossing(
                    Destination("radio", rule["source_radio"]),
                    Destination("radio", rule["target_radio"]),
                    rule["source_channel"],
                    rule["target_channel"],
                )
            )
        self.planner = ChannelBridge(parsed, crossings)
        self.database = Path(cfg.get("echo_database", ""))
        if not self.database.is_absolute():
            raise ValueError("channel_bridge.echo_database must be an explicit absolute path")
        self.ttl = self._integer(cfg, "echo_ttl_seconds", 300, 10, 3600)
        self.capacity = self._integer(cfg, "echo_capacity", 1000, 2, 10000)
        self.max_pending = self._integer(cfg, "max_pending_crossings", 64, 1, 1000)
        self.queue_ttl = self._integer(cfg, "queue_ttl_seconds", 30, 1, 120)
        if self.ttl <= self.queue_ttl:
            raise ValueError("Echo TTL must exceed queue TTL")
        for key in ("local_policy", "crossing_policy"):
            policy = cfg.get(key, {})
            if not isinstance(policy, dict) or policy.get("default_action", "allow") not in {
                "allow",
                "drop",
                "log_only",
            }:
                raise ValueError("Invalid bridge policy")
            if any(
                not isinstance(r, dict)
                or r.get("action", "allow") not in {"allow", "drop", "log_only"}
                for r in policy.get("rules", [])
            ):
                raise ValueError("Invalid bridge policy rule")
        self.local_policy = PolicyEngine(cfg.get("local_policy"))
        self.crossing_policy = PolicyEngine(cfg.get("crossing_policy"))

    @staticmethod
    def _integer(cfg, name, default, low, high):
        value = cfg.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"channel_bridge.{name} must be an integer in [{low}, {high}]")
        return value

    @classmethod
    def from_config(cls, config):
        cfg = config.get("channel_bridge")
        if cfg is None:
            return None
        if not isinstance(cfg, dict) or type(cfg.get("enabled", False)) is not bool:
            raise ValueError("channel_bridge.enabled must be a boolean")
        return cls(config) if cfg.get("enabled", False) else None

    @staticmethod
    def is_direct_transit(packet, local_prefix, *, local_transmission=False):
        """Capture received next-hop eligibility before native routing consumes it."""
        if local_transmission or packet.header & PH_ROUTE_MASK != ROUTE_TYPE_DIRECT:
            return False
        if not PathUtils.is_valid_path_len(packet.path_len):
            return False
        width, count = packet.get_path_hash_size(), packet.get_path_hash_count()
        if not count or len(packet.path) != width * count:
            return False
        if bytes(packet.path[:width]) != bytes(local_prefix[:width]):
            return False
        kind = packet.get_payload_type()
        if kind in (PAYLOAD_TYPE_TXT_MSG, PAYLOAD_TYPE_ACK):
            return True
        payload = packet.payload or b""
        return (
            kind == PAYLOAD_TYPE_MULTIPART
            and len(payload) >= 5
            and payload[0] & 0x0F == PAYLOAD_TYPE_ACK
        )

    def plan(self, packet, home, context, *, direct_transit=False):
        if home not in self.radio_ids:
            return ()
        if direct_transit and home in self.direct_routes:
            sends = (
                PlannedSend(
                    Destination("radio", self.direct_routes[home]), copy.deepcopy(packet), True
                ),
            )
        else:
            sends = self.planner.plan(packet, Destination("radio", home))
        allowed = []
        for send in sends:
            policy = self.crossing_policy if send.crossing else self.local_policy
            ctx = {
                **context,
                "tx_radio_id": send.destination.name,
                "bridge_crossing": send.crossing,
            }
            if policy.evaluate(send.packet, ctx).action != "drop":
                allowed.append(send)
        return tuple(allowed)
