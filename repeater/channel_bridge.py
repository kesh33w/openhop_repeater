"""Opt-in channel crossings. Shared-channel authentication does not verify a sender.

The caller performs ordinary routing first, then schedules each returned copy through
its existing radio queue. This module never transmits or reports TX success.
"""

import asyncio
import copy
import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_GRP_TXT,
    PH_ROUTE_MASK,
    ROUTE_TYPE_FLOOD,
)
from openhop_core.protocol.crypto import CryptoUtils

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
    enabled: bool = True


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
            if not rule.enabled or rule.source != home or rule.source_channel != source.name:
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


class LedgerBusyError(RuntimeError):
    """The bounded durable ledger queue cannot accept more work."""


@dataclass(frozen=True)
class _FrozenDigest:
    value: bytes

    def calculate_packet_hash(self):
        return self.value


class AsyncEchoLedger:
    """One ordered worker owns the SQLite connection and every transaction.

    Calls await committed results. Cancelling a caller never cancels an already
    submitted operation: a possible reservation must remain until expiry. The
    synchronous startup and close helpers exist for startup validation and test
    teardown; running daemons use aclose after their send tasks have drained.
    """

    def __init__(self, path, *, ttl=300, capacity=1000, clock=time.time, max_pending=64):
        if type(max_pending) is not int or not 1 <= max_pending <= 1024:
            raise ValueError("Invalid echo worker queue limit")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bridge-echo")
        self._lock = threading.Lock()
        self._pending = 0
        self._max_pending = max_pending
        self._closing = False
        self._close_future = None
        try:
            # Fail startup on corrupt/unwritable storage before accepting traffic.
            self._ledger = self._executor.submit(
                EchoLedger, path, ttl=ttl, capacity=capacity, clock=clock
            ).result()
        except BaseException:
            self._executor.shutdown(wait=True)
            raise

    def _submit(self, operation):
        with self._lock:
            if self._closing:
                raise RuntimeError("Echo ledger is closing")
            if self._pending >= self._max_pending:
                raise LedgerBusyError("Echo ledger worker queue full")
            self._pending += 1
            try:
                future = self._executor.submit(operation)
            except BaseException:
                self._pending -= 1
                raise

        def finished(_):
            with self._lock:
                self._pending -= 1

        future.add_done_callback(finished)
        return future

    async def _run(self, operation):
        future = self._submit(operation)
        # Wrap/shield keeps cancellation from deleting an accepted durable write.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        return await asyncio.shield(wrapped)

    @staticmethod
    def _freeze(packet):
        return _FrozenDigest(bytes(packet.calculate_packet_hash()))

    async def contains(self, packet):
        frozen = self._freeze(packet)
        return await self._run(lambda: self._ledger.contains(frozen))

    async def reserve(self, source, outgoing):
        source, outgoing = self._freeze(source), self._freeze(outgoing)
        return await self._run(lambda: self._ledger.reserve(source, outgoing))

    async def finish(self, token, *, success):
        return await self._run(lambda: self._ledger.finish(token, success=success))

    async def _test_operation(self, callback):
        """Ordered fault/inspection hook for isolated tests; never return a cursor."""
        return await self._run(lambda: callback(self._ledger))

    def _begin_close(self):
        with self._lock:
            if self._close_future is None:
                self._closing = True
                # Close is queued after accepted work and is not capacity-rejected.
                self._close_future = self._executor.submit(self._ledger.close)
                self._executor.shutdown(wait=False)
            return self._close_future

    async def aclose(self):
        await asyncio.shield(asyncio.wrap_future(self._begin_close()))

    def close(self):
        """Synchronous fixture/startup cleanup; active daemon shutdown uses aclose."""
        self._begin_close().result()
        self._executor.shutdown(wait=True)


class BridgeSettings:
    """Validated configuration, with no hardware or database side effects."""

    def __init__(self, config):
        from repeater.policy_engine import PolicyEngine

        cfg = config["channel_bridge"]
        allowed = {
            "enabled",
            "paused",
            "disabled_channels",
            "protocols",
            "peer_routing",
            "reception_evidence",
            "traffic_limits",
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
        from repeater.bridge_evidence import EvidenceSettings
        from repeater.bridge_traffic import TrafficLimits

        self.evidence_settings = EvidenceSettings.from_config(config)
        self.traffic_limits = TrafficLimits.from_config(cfg.get("traffic_limits", {}))
        self.config_snapshot = copy.deepcopy(cfg)
        self.paused = cfg.get("paused", False)
        self.pause_generation = 0
        if type(self.paused) is not bool:
            raise ValueError("channel_bridge.paused must be a boolean")
        protocols = cfg.get("protocols", {})
        protocol_names = {"discovery", "repeater_admin", "trace"}
        if (
            not isinstance(protocols, dict)
            or set(protocols) - protocol_names
            or any(type(value) is not bool for value in protocols.values())
        ):
            raise ValueError("Invalid channel_bridge.protocols")
        self.protocols = {name: protocols.get(name, False) for name in protocol_names}
        self.peer_routing = cfg.get("peer_routing", "legacy")
        if self.peer_routing not in ("legacy", "distinct"):
            raise ValueError("channel_bridge.peer_routing must be legacy or distinct")
        if any(self.protocols.values()) and self.peer_routing != "distinct":
            raise ValueError(
                "New bridge protocols require distinct peer routing and contact rediscovery"
            )
        self.native_identity = None
        self.crossing_identity = None
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
        seen_routes = set()
        for rule in routes:
            if (
                not isinstance(rule, dict)
                or set(rule) - {"source_radio", "target_radio", "enabled"}
                or not {"source_radio", "target_radio"} <= set(rule)
                or type(rule.get("enabled", True)) is not bool
            ):
                raise ValueError("Invalid channel_bridge direct route fields")
            source, target = rule["source_radio"], rule["target_radio"]
            if source not in ids or target not in ids or source == target:
                raise ValueError("Direct routes require distinct configured radios")
            if source in seen_routes:
                raise ValueError("Direct routes require one destination per source radio")
            seen_routes.add(source)
            if rule.get("enabled", True):
                self.direct_routes[source] = target
        self.configured_direct_sources = frozenset(seen_routes)
        channels = cfg.get("channels", {})
        if not isinstance(channels, dict) or len(channels) > 64 or (not channels and not routes):
            raise ValueError("channel_bridge requires named channel keys or direct routes")
        try:
            parsed = [Channel(name, bytes.fromhex(key)) for name, key in channels.items()]
        except (TypeError, ValueError):
            raise ValueError(
                "channel_bridge channel keys must be 16 or 32 byte hex strings"
            ) from None
        disabled = cfg.get("disabled_channels", [])
        if (
            not isinstance(disabled, list)
            or any(not isinstance(name, str) for name in disabled)
            or len(set(disabled)) != len(disabled)
            or set(disabled) - channels.keys()
        ):
            raise ValueError("Disabled channels must name distinct configured channels")
        self.disabled_channels = frozenset(disabled)
        mappings = cfg.get("mappings", [])
        if not isinstance(mappings, list) or len(mappings) > 128:
            raise ValueError("channel_bridge.mappings must be a bounded list")
        crossings = []
        for rule in mappings:
            required = {
                "source_radio",
                "target_radio",
                "source_channel",
                "target_channel",
            }
            if (
                not isinstance(rule, dict)
                or not required <= set(rule)
                or set(rule) - (required | {"enabled"})
                or type(rule.get("enabled", True)) is not bool
            ):
                raise ValueError("Invalid channel_bridge mapping fields")
            if rule["source_radio"] not in ids or rule["target_radio"] not in ids:
                raise ValueError("Unknown radio in channel_bridge mapping")
            crossings.append(
                Crossing(
                    Destination("radio", rule["source_radio"]),
                    Destination("radio", rule["target_radio"]),
                    rule["source_channel"],
                    rule["target_channel"],
                    rule.get("enabled", True)
                    and rule["source_channel"] not in self.disabled_channels
                    and rule["target_channel"] not in self.disabled_channels,
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

    def bind_routing_identity(self, public_key):
        """Bind the daemon identity without adding secrets to saved settings."""
        from repeater.bridge_protocols import crossing_route_identity

        self.native_identity = bytes(public_key)
        self.crossing_identity = (
            crossing_route_identity(public_key)
            if self.peer_routing == "distinct"
            else self.native_identity
        )

    @staticmethod
    def is_direct_transit(packet, local_prefix, *, local_transmission=False):
        """Capture received next-hop eligibility before native routing consumes it."""
        from repeater.bridge_protocols import is_legacy_direct

        return not local_transmission and is_legacy_direct(packet, local_prefix)

    def set_paused(self, paused):
        """Apply a persisted pause on the event loop and invalidate queued crossings."""
        if type(paused) is not bool:
            raise ValueError("channel_bridge.paused must be a boolean")
        if paused and not self.paused:
            self.pause_generation += 1
        self.paused = paused
        self.config_snapshot["paused"] = paused

    def plan(self, packet, home, context, *, direct_transit=False, protocol_flood=False):
        if home not in self.radio_ids:
            return ()
        if (
            direct_transit
            and (self.peer_routing == "distinct" or home in self.configured_direct_sources)
            and (self.paused or home not in self.direct_routes)
        ):
            return ()
        if direct_transit and home in self.direct_routes:
            sends = (
                PlannedSend(
                    Destination("radio", self.direct_routes[home]), copy.deepcopy(packet), True
                ),
            )
        elif protocol_flood and home in self.direct_routes:
            sends = (PlannedSend(Destination("radio", home), copy.deepcopy(packet), False),)
            if not self.paused:
                crossed = copy.deepcopy(packet)
                if self.peer_routing == "distinct":
                    width = crossed.get_path_hash_size()
                    # Replace only the native hop this forwarding decision added.
                    # The peer payload and all earlier route entries stay intact.
                    if (
                        self.crossing_identity is None
                        or self.native_identity is None
                        or len(crossed.path) < width
                        or bytes(crossed.path[-width:]) != self.native_identity[:width]
                    ):
                        crossed = None
                    else:
                        crossed.path[-width:] = self.crossing_identity[:width]
                if crossed is not None:
                    sends += (
                        PlannedSend(Destination("radio", self.direct_routes[home]), crossed, True),
                    )
        else:
            sends = self.planner.plan(
                packet, Destination("radio", home), allow_crossing=not self.paused
            )
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
