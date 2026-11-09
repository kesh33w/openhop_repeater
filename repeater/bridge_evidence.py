"""Independent destination reception reports, never endpoint delivery receipts.

No packet bytes, plaintext, channel keys, or credentials are kept in diagnostics.
The bounded in-memory matcher retains digests and path metadata only. It trusts
only configured broker/topic identities; MQTT broker ACLs are the trust boundary,
not a cryptographic signature from an individual receiver.
"""

import copy
import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from openhop_core.protocol.constants import PAYLOAD_TYPE_TRACE
from openhop_core.protocol.packet import Packet

from repeater.radio_ids import configured_radio_ids

PUBLIC_KEY = re.compile(r"[a-fA-F0-9]{64}\Z")
REGION = re.compile(r"[A-Za-z0-9_-]{1,16}\Z")
HEX_PACKET = re.compile(r"(?:[a-fA-F0-9]{2}){2,255}\Z")


@dataclass(frozen=True)
class Observer:
    broker: str
    region: str
    public_key: str
    radio_id: str

    @property
    def topic(self):
        return f"meshcore/{self.region}/{self.public_key}/packets"


@dataclass(frozen=True)
class EvidenceSettings:
    enabled: bool = False
    window_seconds: int = 120
    capacity: int = 256
    observers: tuple = ()

    @classmethod
    def from_config(cls, config):
        cfg = (config.get("channel_bridge") or {}).get("reception_evidence", {})
        if not isinstance(cfg, dict) or set(cfg) - {
            "enabled",
            "window_seconds",
            "capacity",
            "observers",
        }:
            raise ValueError("Invalid bridge reception evidence settings")
        enabled = cfg.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("Evidence enabled must be boolean")
        window, capacity = cfg.get("window_seconds", 120), cfg.get("capacity", 256)
        if (
            type(window) is not int
            or not 10 <= window <= 600
            or type(capacity) is not int
            or not 1 <= capacity <= 1000
        ):
            raise ValueError("Invalid bridge evidence bounds")
        entries = cfg.get("observers", [])
        if not isinstance(entries, list) or len(entries) > 32 or (enabled and not entries):
            raise ValueError("Evidence requires a bounded independent observer list")
        radios = configured_radio_ids(config)
        observers, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "broker",
                "region",
                "public_key",
                "radio_id",
            }:
                raise ValueError("Invalid reception observer")
            broker, region, public_key, radio_id = (
                entry[key] for key in ("broker", "region", "public_key", "radio_id")
            )
            if (
                not isinstance(broker, str)
                or not broker.strip()
                or len(broker) > 128
                or not isinstance(region, str)
                or not REGION.fullmatch(region)
                or not isinstance(public_key, str)
                or not PUBLIC_KEY.fullmatch(public_key)
                or not isinstance(radio_id, str)
                or radio_id not in radios
            ):
                raise ValueError("Invalid reception observer identity or destination")
            # Preserve topic key case: MQTT topics are case-sensitive. Identity
            # comparisons are case-insensitive but subscriptions must be exact.
            identity = (broker, region, public_key.lower())
            if identity in seen:
                raise ValueError("Duplicate reception observer")
            seen.add(identity)
            observers.append(Observer(broker, region, public_key, radio_id))
        return cls(enabled, window, capacity, tuple(observers))


def _timestamp(value):
    if not isinstance(value, str) or not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value):
        raise ValueError("Observer timestamp must include a timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    if not math.isfinite(result):
        raise ValueError("Invalid observer timestamp")
    return result


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _snapshot(packet):
    raw = bytes(packet.write_to())
    payload = bytes(packet.payload)
    trace_hops = None
    if (
        packet.get_payload_type() == PAYLOAD_TYPE_TRACE
        and packet.is_route_direct()
        and len(payload) >= 9
        and packet.get_path_hash_size() == 1
    ):
        width = 1 << (payload[8] & 3)
        if (len(payload) - 9) % width == 0:
            trace_hops = (len(payload) - 9) // width
    return {
        "raw_digest": hashlib.sha256(raw).digest(),
        "payload_digest": hashlib.sha256(payload).digest(),
        "header": packet.header,
        "transport": tuple(packet.transport_codes),
        "path": bytes(packet.path),
        "width": packet.get_path_hash_size(),
        "count": packet.get_path_hash_count(),
        "flood": packet.is_route_flood(),
        "type": packet.get_payload_type(),
        "trace_hops": trace_hops,
    }


def _match(outgoing, received):
    if any(
        outgoing[key] != received[key] for key in ("header", "transport", "payload_digest", "width")
    ):
        return None
    if outgoing["raw_digest"] == received["raw_digest"]:
        return "outgoing_bytes"
    if (
        outgoing["trace_hops"] is not None
        and outgoing["count"] <= received["count"] <= outgoing["trace_hops"]
        and received["path"].startswith(outgoing["path"])
    ):
        return "trace_progress"
    if (
        outgoing["flood"]
        and outgoing["type"] != PAYLOAD_TYPE_TRACE
        and outgoing["path"]
        and received["path"].startswith(outgoing["path"])
    ):
        return "downstream_flood_path"
    return None


class ReceptionEvidence:
    def __init__(self, settings=None, *, clock=time.time, monotonic=time.monotonic):
        self.settings = settings or EvidenceSettings()
        self.clock, self.monotonic = clock, monotonic
        self._lock = threading.RLock()
        self._excluded = set()
        self._pending = OrderedDict()
        self._counts = Counter()
        self._feeds = {
            observer.broker: {
                "broker": observer.broker,
                "state": "not_connected",
                "accepted": 0,
                "ignored": 0,
            }
            for observer in self.settings.observers
        }
        self._observers = {(o.broker, o.topic): o for o in self.settings.observers}

    def exclude_identities(self, identities):
        with self._lock:
            self._excluded.update(str(key).lower() for key in identities)

    def topics(self, broker):
        if not self.settings.enabled:
            return []
        with self._lock:
            return [
                o.topic
                for o in self.settings.observers
                if o.broker == broker and o.public_key.lower() not in self._excluded
            ]

    def feed_state(self, broker, state):
        allowed = {
            "not_connected",
            "subscribing",
            "connected",
            "subscription_failed",
            "disconnected",
            "insecure_broker",
            "no_independent_observers",
        }
        with self._lock:
            if broker in self._feeds and state in allowed:
                self._feeds[broker]["state"] = state

    def _expire(self):
        now = self.monotonic()
        for item in self._pending.values():
            view = item["view"]
            if view["reception_state"] == "waiting" and now > item["deadline"]:
                view["reception_state"] = "unconfirmed"
                self._counts["unconfirmed"] += 1

    def begin(self, packet, source_radio, target_radio):
        if not self.settings.enabled:
            return None
        with self._lock:
            self._expire()
            if len(self._pending) >= self.settings.capacity:
                # Evict completed diagnostics, never an active waiting attempt.
                removable = next(
                    (
                        key
                        for key, item in self._pending.items()
                        if item["view"]["reception_state"] != "waiting"
                    ),
                    None,
                )
                if removable is None:
                    self._counts["capacity_rejected"] += 1
                    return None
                del self._pending[removable]
            now, tick = self.clock(), self.monotonic()
            token = uuid.uuid4().hex
            snap = _snapshot(packet)
            self._pending[token] = {
                "snapshot": snap,
                "started": now,
                "deadline": tick + self.settings.window_seconds,
                "view": {
                    "id": token,
                    "source_radio": source_radio,
                    "target_radio": target_radio,
                    "packet_type": snap["type"],
                    "tx_state": "pending",
                    "reception_state": "waiting",
                    "started_at": _iso(now),
                    "deadline": _iso(now + self.settings.window_seconds),
                    "observers": [],
                },
            }
            self._counts["tracked"] += 1
            return token

    def complete(self, token, outcome):
        if outcome not in {"tx_confirmed", "tx_failed", "tx_uncertain"}:
            raise ValueError("Invalid bridge TX evidence outcome")
        with self._lock:
            item = self._pending.get(token)
            if item is None or item["view"]["tx_state"] != "pending":
                return
            item["view"]["tx_state"] = outcome
            self._counts[outcome] += 1
            if outcome == "tx_failed":
                item["view"]["reception_state"] = "not_applicable"
                item["view"]["observers"] = []
            elif item["view"]["observers"]:
                self._mark_observed(item)
            self._expire()

    def _mark_observed(self, item):
        view = item["view"]
        if view["reception_state"] != "observed":
            view["reception_state"] = "observed"
            self._counts["observed"] += 1

    def ingest(self, broker, topic, payload, *, retained=False):
        """Consume one exact allowlisted RX report. Never route or transmit it."""
        if not self.settings.enabled:
            return False
        with self._lock:
            try:
                observer = self._observers.get((broker, topic))
                if (
                    observer is None
                    or retained
                    or observer.public_key.lower() in self._excluded
                    or not isinstance(payload, (bytes, bytearray))
                    or len(payload) > 16384
                ):
                    raise ValueError("Untrusted observer report")
                data = json.loads(payload)
                if (
                    not isinstance(data, dict)
                    or data.get("direction") != "rx"
                    or not isinstance(data.get("origin_id"), str)
                    or data["origin_id"].lower() != observer.public_key.lower()
                ):
                    raise ValueError("Observer identity mismatch")
                now, observed = self.clock(), _timestamp(data.get("timestamp"))
                if not -2 <= now - observed <= self.settings.window_seconds:
                    raise ValueError("Stale observer report")
                raw = data.get("raw")
                if not isinstance(raw, str) or not HEX_PACKET.fullmatch(raw):
                    raise ValueError("Invalid observer packet")
                packet = Packet()
                packet.read_from(bytes.fromhex(raw))
                snap = _snapshot(packet)
            except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
                self._counts["ignored"] += 1
                if broker in self._feeds:
                    self._feeds[broker]["ignored"] += 1
                return False
            self._expire()
            self._counts["accepted"] += 1
            self._feeds[broker]["accepted"] += 1
            self._feeds[broker]["last_report_at"] = _iso(now)
            for item in self._pending.values():
                view = item["view"]
                if (
                    self.monotonic() > item["deadline"]
                    or view["tx_state"] == "tx_failed"
                    or observer.radio_id != view["target_radio"]
                    or observed < item["started"] - 2
                    or observed > item["started"] + self.settings.window_seconds
                ):
                    continue
                match = _match(item["snapshot"], snap)
                if match is None:
                    continue
                previous = next(
                    (
                        row
                        for row in view["observers"]
                        if row["public_key"].lower() == observer.public_key.lower()
                    ),
                    None,
                )
                progress = snap["count"] if snap["trace_hops"] is not None else None
                if previous is not None:
                    if progress is None or progress <= previous.get("trace_progress", -1):
                        continue
                    view["observers"].remove(previous)
                elif len(view["observers"]) >= 16:
                    continue
                report = {
                    "public_key": observer.public_key,
                    "broker": broker,
                    "radio_id": observer.radio_id,
                    "observed_at": _iso(observed),
                    "received_at": _iso(now),
                    "match": match,
                }
                if progress is not None:
                    report["trace_progress"] = progress
                view["observers"].append(report)
                if view["tx_state"] != "pending":
                    self._mark_observed(item)
            return True

    def status(self):
        with self._lock:
            self._expire()
            return {
                "enabled": self.settings.enabled,
                "window_seconds": self.settings.window_seconds,
                "meaning": "Independent destination RF reports, not endpoint delivery receipts",
                "counts": dict(self._counts),
                "feeds": copy.deepcopy(list(self._feeds.values())),
                "recent": [
                    copy.deepcopy(item["view"]) for item in list(self._pending.values())[-50:]
                ][::-1],
            }
