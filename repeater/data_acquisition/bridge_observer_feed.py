"""Read-only subscriptions on an existing broker connection.

No extra login, credentials, wildcard subscription, publishing, or RF injection.
An observer failure never takes down the publisher or native RF routing.
"""

import logging
import threading

logger = logging.getLogger(__name__)


class BridgeObserverFeed:
    RETRY_SECONDS = 10

    def __init__(self, connection, evidence):
        self.connection, self.evidence = connection, evidence
        self.name = connection.broker["name"]
        self.topics = evidence.topics(self.name)
        self._timer = None
        self._mid = None
        self._generation = 0
        self._lock = threading.RLock()
        self._active = False
        self._message_errors = 0
        connection.client.on_message = self.on_message
        connection.client.on_subscribe = self.on_subscribe

    def connected(self):
        with self._lock:
            self._cancel()
            self._generation += 1
            self._active = True
            tls = self.connection.tls
            if not isinstance(tls, dict) or not tls.get("enabled") or tls.get("insecure"):
                self._active = False
                self.evidence.feed_state(self.name, "insecure_broker")
                return
            if not self.topics:
                self.evidence.feed_state(self.name, "no_independent_observers")
                return
            self._subscribe()

    def _cancel(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._mid = None

    def _retry(self, generation):
        with self._lock:
            if self._active and generation == self._generation:
                self._timer = None
                self._subscribe()

    def _subscribe(self):
        self._cancel()
        if not self._active:
            return
        self.evidence.feed_state(self.name, "subscribing")
        try:
            result, mid = self.connection.client.subscribe([(topic, 0) for topic in self.topics])
            if int(result) != 0:
                raise ValueError("Subscription unavailable")
            self._mid = mid
        except Exception:  # noqa: BLE001 - optional reads must not break the publisher
            self.evidence.feed_state(self.name, "subscription_failed")
        # Also bounds missing SUBACK; only the current attempt can acknowledge.
        self._timer = threading.Timer(self.RETRY_SECONDS, self._retry, (self._generation,))
        self._timer.daemon = True
        self._timer.start()

    def on_subscribe(self, client, userdata, mid, granted, properties=None):
        with self._lock:
            if not self._active or mid != self._mid:
                return
            try:
                accepted = len(granted) == len(self.topics) and all(
                    int(getattr(code, "value", code)) in (0, 1, 2) for code in granted
                )
            except (ValueError, TypeError):
                accepted = False
            if accepted:
                self._cancel()
                self.evidence.feed_state(self.name, "connected")
            else:
                self._mid = None
                self.evidence.feed_state(self.name, "subscription_failed")
                # Existing timer retries this denial without disconnecting the publisher.

    def on_message(self, client, userdata, message):
        try:
            with self._lock:
                if not self._active:
                    return
            self.evidence.ingest(
                self.name, message.topic, message.payload, retained=bool(message.retain)
            )
        except Exception:  # noqa: BLE001 - keep optional reads off the publisher's failure path
            # Paho re-raises callback errors by default, stopping the shared
            # network thread. Even topic decoding can fail before ingest runs.
            # Bound diagnostics and never log the report or exception text.
            with self._lock:
                self._message_errors = min(self._message_errors + 1, 2**31 - 1)
                first_error = self._message_errors == 1
            if first_error:
                logger.warning(
                    "Independent observer report processing failed; further errors suppressed"
                )

    def disconnected(self):
        with self._lock:
            self._active = False
            self._generation += 1
            self._cancel()
            self.evidence.feed_state(self.name, "disconnected")
