"""Private raw-RF cancellation receipt stream for one configured 909 companion.

No RF writes and no bridge injection. Failure closes only this optional proof
stream. This certifies raw cancellation delivery, not decoded command delivery.
"""
import asyncio
import errno
import fcntl
import socket
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import time

from repeater.companion.utils import companion_receive_members

FREQUENCY = 909875000
TEST_KEY = hashlib.sha256(b'#test').digest()[:16]


def read_secret(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o077 or not 32 <= info.st_size <= 256):
            raise ValueError('Invalid receipts secret file')
        value = os.read(fd, 257)
        if not 32 <= len(value) <= 256:
            raise ValueError('Invalid receipts secret length')
        return value
    finally:
        os.close(fd)


def runtime_binding(daemon, config):
    """Re-read actual companion identity/channel/topology on every barrier."""
    if not isinstance(config, dict) or config.get('enabled') is not True:
        raise ValueError('Receipts disabled')
    if set(config) - {'socket_group_access'} != {'enabled', 'socket', 'secret_file', 'companion_key',
                       'companion_endpoint', 'receivers', 'tx_radio'}:
        raise ValueError('Invalid bot RF receipts configuration')
    if type(config.get('socket_group_access', False)) is not bool:
        raise ValueError('Invalid socket group access option')
    key = config['companion_key']
    receivers = config['receivers']
    endpoint = config['companion_endpoint']
    if (not isinstance(key, str) or len(key) != 64 or bytes.fromhex(key).hex() != key
            or not isinstance(receivers, list) or not receivers
            or any(not isinstance(r, str) or not r for r in receivers)
            or len(set(receivers)) != len(receivers)
            or not isinstance(endpoint, list) or len(endpoint) != 2
            or not isinstance(endpoint[0], str) or type(endpoint[1]) is not int):
        raise ValueError('Invalid receipt binding')
    matches = [fs for fs in daemon.companion_frame_servers
               if fs.bridge.get_public_key().hex() == key and fs.port == endpoint[1]]
    if len(matches) != 1:
        raise ValueError('Companion identity or port unavailable')
    bridge = matches[0].bridge
    if companion_receive_members(bridge) != frozenset(receivers):
        raise ValueError('Companion receivers differ')
    fabric = daemon.radio.fabric
    topology = fabric.topology
    tx = config['tx_radio']
    network = topology.network(tx)
    if (topology.networks[network] != tx or tx not in receivers
            or getattr(bridge, '_receive_home_radio_id', None) != tx):
        raise ValueError('Not the designated transmitter')
    channels = [ch for ch in bridge.channels.get_channels() if ch['name'] == '#test']
    if len(channels) != 1 or bytes.fromhex(channels[0]['secret']) not in (TEST_KEY, TEST_KEY + bytes(16)):
        raise ValueError('Companion #test key differs')
    for rid in receivers:
        radio = fabric.get_radio(rid)
        if (not fabric.has_rx_binding(rid) or topology.is_shadow(rid) or topology.network(rid) != network
                or (getattr(radio, 'frequency', None), getattr(radio, 'bandwidth', None),
                    getattr(radio, 'spreading_factor', None), getattr(radio, 'coding_rate', None))
                   != (FREQUENCY, 62500, 9, 5)
                or not callable(getattr(radio, 'rx_receipt_barrier', None))):
            raise ValueError('909 reader instrumentation unavailable')
    return dict(companion_key=key, companion_endpoint=endpoint, frequency_hz=FREQUENCY,
                receivers=receivers, tx_radio=tx,
                channel_key_hash=hashlib.sha256(TEST_KEY).hexdigest())


class ReceiptSession:
    def __init__(self, server, reader, writer):
        self.server, self.reader, self.writer = server, reader, writer
        self.queue = asyncio.Queue(maxsize=128)
        self.sequence = self.acked = 0
        self.ack_event = asyncio.Event()
        self.active = False
        self.session_id = None
        self.generation = secrets.token_hex(16)
        self.reader_generations = {}
        self.failed = None
        self.barrier_task = None
        self.last_request = time.monotonic()
        self.last_request_id = 0
        self.tasks = []

    def healthy(self):
        if self.failed:
            return False
        try:
            if self.server.binding_provider() != self.server.binding:
                return False
            if self.server.observe not in self.server.fabric._observation_callbacks:
                return False
            return all(self.server.fabric.has_rx_binding(rid)
                       and radio.rx_callback is self.server.callbacks[rid]
                       and radio.rx_receipt_available and radio.rx_receipt_generation == self.reader_generations[rid]
                       for rid, radio in self.server.radios.items())
        except Exception:
            return False

    def fail(self, reason):
        if self.failed is None:
            self.failed = reason
            self.server.failures += 1
        self.active = False
        self.writer.close()
        self.ack_event.set()

    def enqueue(self, value):
        if self.failed:
            return False
        try:
            self.queue.put_nowait(value)
            return True
        except asyncio.QueueFull:
            self.fail('queue_overflow')
            return False

    def observe(self, data, radio_id, own_tx):
        if not self.active:
            return
        if not self.healthy() or type(own_tx) is not bool or not 1 <= len(data) <= 255:
            self.fail('reader_or_frame_changed')
            return
        self.sequence += 1
        self.enqueue(dict(type='rx', seq=self.sequence, radio_id=radio_id,
                          raw=bytes(data).hex(), own_tx=own_tx))

    async def read(self):
        line = await asyncio.wait_for(self.reader.readline(), 4)
        if not line or len(line) > 2048 or not line.endswith(b'\n'):
            raise ValueError('Receipt input unavailable')
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError('Receipt input object required')
        return value

    async def write_loop(self):
        while not self.failed:
            value = await self.queue.get()
            self.writer.write(json.dumps(value, separators=(',', ':')).encode()+b'\n')
            await asyncio.wait_for(self.writer.drain(), 2)

    async def watchdog(self):
        while not self.failed:
            await asyncio.sleep(.25)
            if not self.healthy() or time.monotonic()-self.last_request > 4:
                self.fail('reader_changed_or_heartbeat_lost')

    async def barrier(self, nonce, request_id):
        try:
            if not self.healthy():
                raise ValueError('Readers changed')
            generations = await asyncio.wait_for(asyncio.gather(
                *(radio.rx_receipt_barrier() for radio in self.server.radios.values())), 2)
            if not self.healthy() or generations != list(self.reader_generations.values()):
                raise ValueError('Reader generation changed')
            frontier = self.sequence
            while self.acked < self.sequence:
                self.ack_event.clear()
                if self.failed:
                    raise ValueError('Receipt stream failed')
                await asyncio.wait_for(self.ack_event.wait(), 2)
            frontier = self.sequence
            if not self.healthy():
                raise ValueError('Readers changed after receipts')
            self.enqueue(dict(type='barrier', nonce=nonce, request_id=request_id, seq=frontier,
                              session_id=self.session_id, generation=self.generation))
        except Exception:
            self.fail('barrier_failed')

    async def run(self):
        challenge = secrets.token_hex(32)
        # Authentication happens before raw observation becomes active.
        self.writer.write(json.dumps(dict(type='hello', version=1, challenge=challenge,
                                         generation=self.generation, binding=self.server.binding)).encode()+b'\n')
        await asyncio.wait_for(self.writer.drain(), 2)
        auth = await self.read()
        session = auth.get('session_id')
        if (auth.get('type') != 'auth' or not isinstance(session, str)
                or not 16 <= len(session) <= 128 or not isinstance(auth.get('proof'), str)):
            raise ValueError('Authentication failed')
        expected = hmac.new(self.server.secret, (challenge+'\0'+session).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, auth['proof']):
            raise ValueError('Authentication failed')
        self.session_id = session
        self.reader_generations = {rid: radio.rx_receipt_generation for rid,radio in self.server.radios.items()}
        if not self.healthy():
            raise ValueError('Reader not available')
        self.enqueue(dict(type='ready', session_id=session, generation=self.generation,
                          binding=self.server.binding, sequence=0))
        self.active = True
        self.tasks = [asyncio.create_task(self.write_loop()), asyncio.create_task(self.watchdog())]
        while not self.failed:
            value = await self.read()
            self.last_request = time.monotonic()
            if value.get('type') == 'ack':
                seq = value.get('seq')
                if type(seq) is not int or seq != self.acked+1 or seq > self.sequence:
                    raise ValueError('Receipt gap or replay')
                self.acked = seq
                self.ack_event.set()
            elif value.get('type') == 'barrier':
                nonce = value.get('nonce')
                request_id = value.get('request_id')
                if (not isinstance(nonce, str) or not 16 <= len(nonce) <= 128
                        or type(request_id) is not int or request_id != self.last_request_id + 1
                        or self.barrier_task is not None and not self.barrier_task.done()):
                    raise ValueError('Invalid or overlapping barrier')
                self.last_request_id = request_id
                self.barrier_task = asyncio.create_task(self.barrier(nonce, request_id))
            else:
                raise ValueError('Unknown receipt input')

    async def close(self):
        self.active = False
        for task in self.tasks+[self.barrier_task]:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in self.tasks+[self.barrier_task] if task is not None), return_exceptions=True)
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


class RawReceiptServer:
    def __init__(self, *, fabric, binding, secret, socket_path, binding_provider, socket_group_access=False):
        self.fabric, self.binding, self.secret = fabric, binding, secret
        self.socket_path, self.binding_provider = socket_path, binding_provider
        self.radios = {rid:fabric.get_radio(rid) for rid in binding['receivers']}
        self.callbacks = {rid: radio.rx_callback for rid, radio in self.radios.items()}
        self.socket_group_access = socket_group_access
        self._lock_fd = None
        self.session = None
        self.server = None
        self.failures = 0
        self._clients = set()
        self._socket_inode = None

    def observe(self, data, rssi, snr, *, rx_radio_id, own_tx_match=False):
        # This edge is invoked before dispatcher dedup and shadow isolation.
        # Ignore every other radio including RAK and all 910 inputs.
        if rx_radio_id not in self.radios or self.session is None:
            return
        try:
            self.session.observe(data, rx_radio_id, own_tx_match)
        except Exception:
            self.session.fail('observation_failed')

    async def _client(self, reader, writer):
        task = asyncio.current_task()
        self._clients.add(task)
        # Never evict an existing bot proof connection.
        if self.session is not None:
            writer.close()
            self._clients.discard(task)
            return
        session = ReceiptSession(self, reader, writer)
        self.session = session
        try:
            await session.run()
        except (Exception, asyncio.CancelledError):
            session.fail('session_closed')
        finally:
            await session.close()
            if self.session is session:
                self.session = None
            self._clients.discard(task)

    async def start(self):
        path = Path(self.socket_path)
        if not path.is_absolute():
            raise ValueError('Receipt socket path must be absolute')
        info = path.parent.stat()
        if (info.st_uid != os.getuid() or info.st_mode & 0o022
                or (self.socket_group_access and stat.S_IMODE(info.st_mode) != 0o710)):
            raise ValueError('Receipt socket parent must be private and owned by service')
        lock = os.open(str(path)+'.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            lock_info = os.fstat(lock)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                    or lock_info.st_mode & 0o077):
                raise ValueError('Invalid receipt socket lock')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                old = path.lstat()
            except FileNotFoundError:
                old = None
            if old is not None:
                if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid():
                    raise ValueError('Refusing unowned or non-socket receipt path')
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(.2)
                try:
                    probe.connect(str(path))
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise ValueError('Cannot establish stale receipt socket') from exc
                else:
                    raise ValueError('Receipt socket is active')
                finally:
                    probe.close()
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (old.st_dev, old.st_ino):
                    raise ValueError('Receipt socket changed during validation')
                path.unlink()
            self.server = await asyncio.start_unix_server(self._client, path=str(path), limit=2048)
            self._socket_inode = path.stat().st_ino
            if self.socket_group_access:
                os.chown(path, -1, info.st_gid)
            os.chmod(path, 0o660 if self.socket_group_access else 0o600)
            self.fabric.add_observation_callback(self.observe)
            self._lock_fd = lock
        except BaseException:
            os.close(lock)
            await self.close()
            raise

    async def close(self):
        self.fabric.remove_observation_callback(self.observe)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.session:
            self.session.fail('service_shutdown')
        for task in tuple(self._clients):
            task.cancel()
        await asyncio.gather(*tuple(self._clients), return_exceptions=True)
        path = Path(self.socket_path)
        try:
            if self._socket_inode is not None and path.lstat().st_ino == self._socket_inode:
                path.unlink()
        except FileNotFoundError:
            pass
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None


async def start_for_daemon(daemon):
    config = daemon.config.get('bot_rf_receipts')
    if not config or config.get('enabled') is not True:
        return None
    binding = runtime_binding(daemon, config)
    frozen_config = json.dumps(config, sort_keys=True)
    def current_binding():
        current = daemon.config.get('bot_rf_receipts')
        if json.dumps(current, sort_keys=True) != frozen_config:
            raise ValueError('Receipt configuration changed')
        return runtime_binding(daemon, current)
    server = RawReceiptServer(fabric=daemon.radio.fabric, binding=binding,
                              secret=read_secret(config['secret_file']), socket_path=config['socket'],
                              binding_provider=current_binding, socket_group_access=config.get('socket_group_access', False))
    await server.start()
    return server
