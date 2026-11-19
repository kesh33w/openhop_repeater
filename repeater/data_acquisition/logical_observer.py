"""Bounded physical-RX observations, independent of forwarding and storage.

Each distinct wire image is reported once per logical observer. Copies of one
transmission heard by several ears (a G3 and its RAK) collapse to one report,
but relayed copies of the same packet arrive with a different path and are
reported separately, as single-radio observer firmware does. Those path
variants show which repeaters this site can hear.
"""

import hashlib
import queue
import threading
import time
from collections import Counter, OrderedDict

from openhop_core.protocol.packet import Packet
from openhop_core.protocol import Identity, parse_advert_payload, decode_appdata
from openhop_core.protocol.constants import MAX_PATH_SIZE, MAX_ADVERT_DATA_SIZE, PAYLOAD_TYPE_ADVERT
from .storage_utils import PacketRecord


class LogicalObserver:
    def __init__(
        self,
        groups,
        topology,
        pusher,
        *,
        ttl=300,
        capacity=10000,
        queue_capacity=1024,
        clock=time.monotonic,
        wall_clock=time.time,
        receiver=None,
    ):
        if (
            not isinstance(groups, dict)
            or not groups
            or topology is None
            or not 1 <= ttl <= 3600
            or not 1 <= capacity <= 100000
            or not 1 <= queue_capacity <= 10000
        ):
            raise ValueError("Invalid logical observer settings")
        self.receiver = receiver
        self._subscriptions = []
        self._stream_logical = {}
        if receiver is not None:
            resolved = {}
            for logical, stream_id in groups.items():
                if not isinstance(stream_id, str) or stream_id in self._stream_logical:
                    raise ValueError("Logical MQTT bindings require unique stream IDs")
                resolved[logical] = sorted(receiver.members(stream_id))
                self._stream_logical[stream_id] = logical
            groups = resolved
        self.members = {}
        self.groups = {}
        for logical, members in groups.items():
            if (
                not isinstance(logical, str)
                or not isinstance(members, list)
                or not members
                or (receiver is None and len(members) not in (1, 2))
                or logical not in members
                or len(set(members)) != len(members)
            ):
                raise ValueError("Invalid logical observer membership")
            if any(rid not in topology.members or rid in self.members for rid in members):
                raise ValueError("Unknown or overlapping logical observer membership")
            networks = {topology.network(rid) for rid in members}
            if len(networks) != 1:
                raise ValueError("Logical observers must share one network")
            if len(members) > 1 and (
                topology.members[logical][1] != "transceiver"
                or any(topology.members[rid][1] != "receiver" for rid in members if rid != logical)
            ):
                raise ValueError("Paired observers need a transmitter and active receiver")
            if not any(conn.radio_id == logical for conn in pusher.connections):
                raise ValueError("Logical observer has no MQTT connection")
            self.groups[logical] = tuple(members)
            for rid in members:
                self.members[rid] = (logical, next(iter(networks)))
        self.pusher, self.ttl, self.capacity = pusher, ttl, capacity
        self.clock, self.wall_clock = clock, wall_clock
        self._seen = OrderedDict()
        self._queue = queue.Queue(maxsize=queue_capacity)
        self._lock = threading.Lock()
        self._counts = Counter()
        self._unique = Counter()
        self._closed = False
        self._stop = threading.Event()
        for conn in pusher.connections:
            if conn.radio_id in self.groups:
                conn.logical_observer_members = self.groups[conn.radio_id]
                conn.logical_observer_worker = self
                conn.logical_observer_receive_only = (
                    len(self.groups[conn.radio_id]) == 1
                    and topology.members[conn.radio_id][1] != "transceiver"
                )
        self._thread = threading.Thread(target=self._run, name="logical-mqtt-observer", daemon=True)
        self._thread.start()
        if receiver is not None:
            try:
                for stream_id in self._stream_logical:
                    self._subscriptions.append(
                        receiver.subscribe(stream_id, self.receive_logical, updates=True)
                    )
            except BaseException:
                self.close()
                raise

    def receive_logical(self, event):
        """Consume one shared local-RF event without maintaining another dedup cache.

        The receiver offers the first reception, then every later one. A later
        reception is reported only when its wire image (path included) differs
        from every earlier reception of the same packet in the event history.
        """
        if event.stream_id not in self._stream_logical:
            return True
        obs = event.observation
        if event.kind != "first":
            retained = any(o is obs for o in event.observations)
            repeated = obs.wire_hash == event.first.wire_hash or any(
                o.wire_hash == obs.wire_hash for o in event.observations if o is not obs
            )
            if repeated or not retained:
                # Unretained history cannot prove novelty; never risk re-reporting.
                with self._lock:
                    self._counts["duplicates"] += 1
                return True
        return self.receive(
            obs.data, obs.rssi, obs.snr, rx_radio_id=obs.radio_id, _logical_event=event
        )

    def receive(
        self, data, rssi, snr, *, rx_radio_id=None, own_tx_match=False, _logical_event=None
    ):
        if self.receiver is not None and _logical_event is None:
            raise ValueError("Shared observers accept logical events, not physical callbacks")
        membership = self.members.get(rx_radio_id)
        if membership is None:
            return
        if own_tx_match:
            with self._lock:
                self._counts["own_tx_suppressed"] += 1
            return
        try:
            packet = Packet()
            packet.read_from(data)
            if (
                not packet.payload
                or len(packet.path) > MAX_PATH_SIZE
                or packet.get_path_hash_size() > 3
            ):
                raise ValueError("Invalid packet shape")
            digest = packet.calculate_packet_hash().hex()
        except Exception:
            with self._lock:
                self._counts["invalid"] += 1
            return
        logical, network = membership
        # RAK has its own independent observer on the same network. Its identity
        # cache must not consume either paired unit's reception opportunity.
        # Keyed by the full wire image so path variants are reported separately.
        key = (logical, network, hashlib.sha256(bytes(data)).hexdigest())
        with self._lock:
            if self._closed:
                self._counts["closed_drops"] += 1
                return
            now = self.clock()
            while self._seen and now - next(iter(self._seen.values())) >= self.ttl:
                self._seen.popitem(last=False)
            if self.receiver is None and key in self._seen:
                self._counts["duplicates"] += 1
                return
            if self.receiver is None and len(self._seen) >= self.capacity:
                self._counts["capacity_drops"] += 1
                return
            # Freeze all physical facts before any routing mutation or worker wait.
            row = dict(
                timestamp=(
                    _logical_event.observation.at
                    if _logical_event is not None
                    else self.wall_clock()
                ),
                type=packet.get_payload_type(),
                route=packet.header & 3,
                payload_length=packet.payload_len,
                raw_packet=bytes(data).hex(),
                packet_hash=digest,
                rssi=rssi,
                snr=snr,
                rx_radio_id=rx_radio_id,
            )
            if _logical_event is not None:
                row["logical_receiver_id"] = _logical_event.stream_id
                row["receive_event_id"] = _logical_event.event_id
            try:
                self._queue.put_nowait((logical, network, row))
            except queue.Full:
                self._counts["queue_drops"] += 1
                return False
            if self.receiver is None:
                self._seen[key] = now
            self._counts["accepted"] += 1
            return True

    def _run(self):
        while not self._stop.is_set():
            try:
                logical, network, row = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if row["type"] == PAYLOAD_TYPE_ADVERT:
                    try:
                        packet = Packet()
                        packet.read_from(bytes.fromhex(row["raw_packet"]))
                        parsed = parse_advert_payload(bytes(packet.payload))
                        key = bytes.fromhex(parsed["pubkey"])
                        app = parsed["appdata"]
                        if len(app) > MAX_ADVERT_DATA_SIZE:
                            raise ValueError("Oversized advert")
                        decode_appdata(app)
                        signed = key + parsed["timestamp"].to_bytes(4, "little") + app
                        if not Identity(key).verify(signed, bytes.fromhex(parsed["signature"])):
                            raise ValueError("Invalid advert signature")
                    except Exception:
                        with self._lock:
                            self._counts["invalid"] += 1
                        continue
                with self._lock:
                    self._unique[logical] += 1
                record = PacketRecord.from_packet_record(
                    row,
                    origin=getattr(self.pusher, "node_name", ""),
                    origin_id=getattr(self.pusher, "public_key", ""),
                    include_radio_ids=True,
                ).to_dict()
                record.update(
                    logical_observer_id=logical,
                    observation_network=network,
                    observation_source="physical_rf",
                )
                for key in ("logical_receiver_id", "receive_event_id"):
                    if key in row:
                        record[key] = row[key]
                # Receive metadata has no TX claim; forwarding owns separate records.
                self.pusher.publish_packet(record)
                with self._lock:
                    self._counts["publish_attempts"] += 1
            except Exception:
                with self._lock:
                    self._counts["publish_errors"] += 1
            finally:
                self._queue.task_done()

    def snapshot(self):
        with self._lock:
            return {
                **self._counts,
                "unique_received": dict(self._unique),
                "cache_size": len(self._seen),
                "queue_size": self._queue.qsize(),
                "closed": self._closed,
                "worker_alive": self._thread.is_alive(),
                "receive_mode": "shared_logical"
                if self.receiver is not None
                else "legacy_physical",
            }

    def close(self):
        if self.receiver is not None:
            for token in self._subscriptions:
                self.receiver.unsubscribe(token)
            self._subscriptions.clear()
        with self._lock:
            self._closed = True
        # Bounded shutdown; pending reports are explicitly counted, never flushed
        # synchronously on the radio/event-loop thread.
        self._stop.set()
        self._thread.join(timeout=2)
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            with self._lock:
                self._counts["shutdown_drops"] += 1
