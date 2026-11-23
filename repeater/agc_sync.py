"""Apply ``repeater.agc_reset_interval`` to the radios that implement it.

The MeshCore CLI setting ``agc.reset.interval`` used to be saved in the config
and nothing else: no radio ever received it. On openHop Modem firmware the
periodic AGC reset exists only on the ESP32 Station boards (G2/G3), and is set
through the modem's password-protected HTTP API (``POST /api/config`` with
``agc_reset_interval_sec``), not the radio TCP protocol. USB modems
(nRF52 boards such as the RAK3401) have no AGC reset at all.

A ``modem_tcp`` radio takes part when its ``modem_tcp`` section names the HTTP
credential::

    modem_tcp:
      host: 10.30.0.9
      http_password_file: /etc/openhop_repeater/modem-http/g3909.password
      http_port: 80          # optional
      http_username: admin   # optional

The config value is the source of truth. A background thread pushes it at
startup, whenever it is asked to (after the CLI or a config save changes it),
and every ``RECHECK_SECONDS`` so a modem that was reset or reflashed is put
back. Blocking HTTP stays off the event loop; the CLI reads the cached result.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from repeater.radio_ids import canonical_radio_id

logger = logging.getLogger("AgcSync")

MAX_INTERVAL_SECONDS = 3600  # the firmware's own limit
RECHECK_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ModemHttpTarget:
    radio_id: str
    host: str
    port: int
    username: str
    password_file: str


@dataclass
class RadioAgcState:
    applied: Optional[int] = None  # the modem's reported interval, seconds
    error: Optional[str] = None
    checked_at: Optional[float] = None


def desired_interval(config: dict) -> int:
    """The configured interval in seconds, clamped to what the firmware accepts."""
    try:
        value = int((config.get("repeater") or {}).get("agc_reset_interval", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_INTERVAL_SECONDS, value))


def discover(config: dict) -> tuple[List[ModemHttpTarget], List[str]]:
    """Split configured radios into HTTP-controllable modems and the rest."""
    targets: List[ModemHttpTarget] = []
    unsupported: List[str] = []
    for entry in config.get("radios") or []:
        if not isinstance(entry, dict):
            continue
        radio_id = canonical_radio_id(entry)
        tcp = entry.get("modem_tcp") if entry.get("radio_type") == "modem_tcp" else None
        password_file = (tcp or {}).get("http_password_file")
        host = (tcp or {}).get("host")
        if not (isinstance(password_file, str) and os.path.isabs(password_file) and host):
            unsupported.append(radio_id)
            continue
        targets.append(
            ModemHttpTarget(
                radio_id=radio_id,
                host=str(host),
                port=int(tcp.get("http_port", 80)),
                username=str(tcp.get("http_username", "admin") or "admin"),
                password_file=password_file,
            )
        )
    return targets, unsupported


def _read_secret(path: str) -> str:
    # Same rules as the bridge subscriber credentials: no symlinks, owned by
    # this user, no group/world access. The value never reaches logs.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or not 1 <= info.st_size <= 256
        ):
            raise ValueError("modem HTTP password file must be a 0600 file owned by this user")
        value = stream.read(257).decode("utf-8").rstrip("\r\n")
    if not value or any(ord(c) < 32 for c in value):
        raise ValueError("modem HTTP password file is empty or malformed")
    return value


def _request(target: ModemHttpTarget, method: str, body: Optional[dict] = None) -> dict:
    url = f"http://{target.host}:{target.port}/api/config"
    token = base64.b64encode(
        f"{target.username}:{_read_secret(target.password_file)}".encode()
    ).decode()
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - fixed http URL
    request.add_header("Authorization", f"Basic {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310  # nosec B310
        return json.loads(response.read(65536).decode("utf-8"))


def _interval_from(payload: dict) -> Optional[int]:
    """Find agc_reset_interval_sec in a GET body or a POST reply's ``config``."""
    for candidate in (payload, payload.get("config") if isinstance(payload, dict) else None):
        if not isinstance(candidate, dict):
            continue
        for section in (candidate, candidate.get("radio"), candidate.get("rf")):
            if isinstance(section, dict) and "agc_reset_interval_sec" in section:
                return int(section["agc_reset_interval_sec"])
    return None


def sync_target(target: ModemHttpTarget, desired: int, request=_request) -> RadioAgcState:
    """Read the modem's interval and set it when it differs. Never raises."""
    state = RadioAgcState(checked_at=time.time())
    try:
        current = _interval_from(request(target, "GET"))
        if current is None:
            state.error = "modem firmware has no AGC reset control"
            return state
        if current != desired:
            reply = request(target, "POST", {"agc_reset_interval_sec": desired})
            current = _interval_from(reply)
            if current is None:
                current = _interval_from(request(target, "GET"))
        state.applied = current
        if current != desired:
            state.error = f"modem reports {current} s after setting {desired} s"
    except urllib.error.HTTPError as e:
        state.error = "modem HTTP password rejected" if e.code == 401 else f"modem HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        state.error = f"modem unreachable ({reason.__class__.__name__})"
    except ValueError as e:
        state.error = str(e)
    return state


class AgcIntervalSync:
    """Keeps each capable modem's AGC reset interval equal to the config."""

    def __init__(self, config: dict, *, sync_fn: Callable = sync_target):
        self._config = config
        self._sync_fn = sync_fn
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._states: Dict[str, RadioAgcState] = {}
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        # Runs even with no capable radio yet, so a credential added later is
        # picked up on the next request_sync() without a restart.
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="agc-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request_sync(self) -> None:
        """Apply the current config value soon (non-blocking)."""
        self._wake.set()

    def sync_now(self) -> Dict[str, RadioAgcState]:
        desired = desired_interval(self._config)
        targets, _ = discover(self._config)
        results = {t.radio_id: self._sync_fn(t, desired) for t in targets}
        with self._lock:
            previous, self._states = self._states, results
        for radio_id, state in results.items():
            before = previous.get(radio_id)
            if before is not None and (before.applied, before.error) == (
                state.applied,
                state.error,
            ):
                continue  # unchanged since the last check: stay quiet
            if state.error:
                logger.warning("AGC reset interval on %s: %s", radio_id, state.error)
            else:
                logger.info("AGC reset interval on %s: %s s", radio_id, state.applied)
        return results

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_now()
            except Exception:
                logger.exception("AGC reset interval sync failed")
            self._wake.wait(RECHECK_SECONDS)
            self._wake.clear()

    def summary(self) -> str:
        """Short per-radio status for the CLI, e.g. ``g3909 4s, g3910 4s, rak909 n/a``."""
        targets, unsupported = discover(self._config)
        with self._lock:
            states = dict(self._states)
        parts = []
        for target in targets:
            state = states.get(target.radio_id)
            if state is None:
                parts.append(f"{target.radio_id} pending")
            elif state.error:
                parts.append(f"{target.radio_id} error")
            else:
                parts.append(f"{target.radio_id} {state.applied}s")
        parts.extend(f"{radio_id} n/a" for radio_id in unsupported)
        return ", ".join(parts)

    def states(self) -> Dict[str, RadioAgcState]:
        with self._lock:
            return dict(self._states)
