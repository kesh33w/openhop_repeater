"""Dedicated TLS-only receive transport. Never participates in publishing."""

import logging
import os
import re
import ssl
import stat
import threading
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt

from .bridge_observer_feed import BridgeObserverFeed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubscriberSettings:
    broker: str
    url: str
    username: str
    password_file: str

    @classmethod
    def parse(cls, entries, observers):
        if not isinstance(entries, list) or len(entries) > 16:
            raise ValueError("Invalid reception subscriber list")
        result, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "broker",
                "url",
                "username",
                "password_file",
            }:
                raise ValueError("Invalid reception subscriber settings")
            if any(
                not isinstance(value, str)
                or not value
                or len(value) > 1024
                or any(ord(c) < 32 for c in value)
                for value in entry.values()
            ):
                raise ValueError("Invalid reception subscriber value")
            parsed = urlsplit(entry["url"])
            if (
                parsed.scheme != "wss"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
                or not 1 <= (443 if parsed.port is None else parsed.port) <= 65535
                or not os.path.isabs(entry["password_file"])
            ):
                raise ValueError("Reception subscribers require a verified WSS endpoint")
            name = entry["broker"]
            if name in seen or name not in {o.broker for o in observers}:
                raise ValueError("Subscriber must name one distinct reception broker")
            seen.add(name)
            result.append(cls(**entry))
        return tuple(result)


def _password(path):
    # Reject symlinks and group/world access; inspect the opened file, not a
    # preceding stat. Credentials never enter config, diagnostics or log text.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or not 1 <= info.st_size <= 4096
        ):
            raise ValueError("Invalid subscriber credential file")
        value = stream.read(4097).decode("utf-8").rstrip("\r\n")
        if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid subscriber credential file")
        return value


class BridgeSubscriber:
    """Own network loop and reconnect backoff, isolated from publisher clients."""

    def __init__(self, settings, evidence):
        self.settings, self.evidence = settings, evidence
        self.broker = {"name": settings.broker}
        self.tls = {"enabled": True, "insecure": False}
        self.client = None
        self.feed = None
        self._lock = threading.RLock()
        self._stopped = True

    def connect(self):
        with self._lock:
            if not self._stopped:
                return
            if not self.evidence.topics(self.settings.broker):
                self.evidence.feed_state(self.settings.broker, "no_independent_observers")
                return
            self._stopped = False
            failed = False
            try:
                endpoint = urlsplit(self.settings.url)
                self.client = mqtt.Client(
                    client_id="bridge-rx-" + uuid.uuid4().hex,
                    clean_session=True,
                    protocol=mqtt.MQTTv311,
                    transport="websockets",
                )
                self.client.tls_set_context(ssl.create_default_context())
                self.client.ws_set_options(path=endpoint.path or "/mqtt")
                self.client.username_pw_set(
                    self.settings.username, _password(self.settings.password_file)
                )
                self.client.reconnect_delay_set(min_delay=5, max_delay=120)
                self.feed = BridgeObserverFeed(self, self.evidence, include_status=True)
                self.client.on_connect = self._connected
                self.client.on_disconnect = self._disconnected
                self.client.on_connect_fail = self._connect_failed
                self.client.connect_async(endpoint.hostname, endpoint.port or 443, keepalive=60)
                self.client.loop_start()
            except Exception:  # noqa: BLE001 - optional transport must not stop publishers
                # Never report exception text: transports can include credentials.
                failed = True
        if failed:
            try:
                self.disconnect()
            except Exception:  # noqa: BLE001 - cleanup must not expose transport secrets
                logger.warning("Independent reception subscriber cleanup failed")
            self.evidence.feed_state(self.settings.broker, "connection_failed")

    def _connect_failed(self, client, userdata):
        with self._lock:
            if not self._stopped and client is self.client:
                self.evidence.feed_state(self.settings.broker, "connection_failed")

    def _connected(self, client, userdata, flags, rc, properties=None):
        with self._lock:
            if self._stopped or client is not self.client:
                return
            if int(getattr(rc, "value", rc)) == 0:
                self.feed.connected()
            else:
                self.feed.disconnected()
                self.evidence.feed_state(self.settings.broker, "connection_failed")

    def _disconnected(self, client, userdata, rc, properties=None):
        with self._lock:
            if client is self.client and self.feed:
                self.feed.disconnected()

    def disconnect(self):
        with self._lock:
            self._stopped = True
            if self.feed:
                self.feed.disconnected()
            client = self.client
        # Do not join the network thread while holding its callback lock.
        if client:
            try:
                client.disconnect()
            finally:
                client.loop_stop()
