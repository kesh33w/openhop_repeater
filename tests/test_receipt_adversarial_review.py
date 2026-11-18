"""Independent receipt protocol checks without sockets or RF writes."""

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from repeater.bot_rf_receipts import RawReceiptServer


class Radio:
    def __init__(self):
        self.rx_callback = lambda *args: None

    rx_receipt_available = True
    rx_receipt_generation = 1
    gate = None

    async def rx_receipt_barrier(self):
        if self.gate:
            await self.gate.wait()
        return self.rx_receipt_generation


class Writer:
    def __init__(self):
        self.events, self.closed = [], False

    def write(self, raw):
        self.events.append(json.loads(raw))

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def feed(reader, **event):
    reader.feed_data(json.dumps(event).encode() + b"\n")


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest_asyncio.fixture
async def connected():
    radios = {"dir909": Radio(), "v3909": Radio()}
    binding = {"receivers": list(radios)}
    server = RawReceiptServer(
        fabric=SimpleNamespace(
            get_radio=radios.__getitem__, has_rx_binding=lambda rid: True, _observation_callbacks=[]
        ),
        binding=binding,
        secret=b"x" * 32,
        socket_path="/unused",
        binding_provider=lambda: binding,
    )
    server.fabric._observation_callbacks.append(server.observe)
    reader, writer = asyncio.StreamReader(), Writer()
    task = asyncio.create_task(server._client(reader, writer))
    await until(lambda: writer.events)
    hello, session = writer.events[0], "independent-session"
    proof = hmac.new(
        server.secret, (hello["challenge"] + "\0" + session).encode(), hashlib.sha256
    ).hexdigest()
    feed(reader, type="auth", session_id=session, proof=proof)
    await until(lambda: any(e["type"] == "ready" for e in writer.events))
    yield server, server.session, radios, reader, writer
    reader.feed_eof()
    await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_barrier_waits_for_callback_ack(connected):
    server, session, radios, reader, writer = connected
    server.observe(b"\x15\x00", -70, 5, rx_radio_id="v3909")
    feed(reader, type="barrier", nonce="independent-nonce", request_id=1)
    await until(lambda: any(e["type"] == "rx" for e in writer.events))
    await asyncio.sleep(0.01)
    assert not any(e["type"] == "barrier" for e in writer.events)
    feed(reader, type="ack", seq=1)
    await until(lambda: any(e["type"] == "barrier" for e in writer.events))
    assert writer.events[-1]["seq"] == 1 and session.failed is None


@pytest.mark.asyncio
@pytest.mark.parametrize("seq", [0, 2, True])
async def test_bad_ack_closes(connected, seq):
    server, session, radios, reader, writer = connected
    server.observe(b"\x15\x00", -70, 5, rx_radio_id="dir909")
    feed(reader, type="ack", seq=seq)
    await until(lambda: writer.closed)
    assert session.failed


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [0, 2, True])
async def test_bad_request_id_closes(connected, request_id):
    server, session, radios, reader, writer = connected
    feed(reader, type="barrier", nonce="independent-nonce", request_id=request_id)
    await until(lambda: writer.closed)
    assert session.failed


@pytest.mark.asyncio
async def test_request_replay_closes(connected):
    server, session, radios, reader, writer = connected
    feed(reader, type="barrier", nonce="independent-nonce", request_id=1)
    await until(lambda: any(e["type"] == "barrier" for e in writer.events))
    feed(reader, type="barrier", nonce="independent-nonce", request_id=1)
    await until(lambda: writer.closed)
    assert session.failed


@pytest.mark.asyncio
async def test_generation_change_during_fence_revokes(connected):
    server, session, radios, reader, writer = connected
    gate = asyncio.Event()
    radios["v3909"].gate = gate
    feed(reader, type="barrier", nonce="independent-nonce", request_id=1)
    await until(lambda: session.barrier_task is not None)
    radios["v3909"].rx_receipt_generation += 1
    gate.set()
    await until(lambda: writer.closed)
    assert not any(e["type"] == "barrier" for e in writer.events)


@pytest.mark.asyncio
async def test_other_bands_ignored_overflow_revokes(connected):
    server, session, radios, reader, writer = connected
    for rid in ("rak910", "dir910", "v3910"):
        server.observe(b"\x15\x00", -70, 5, rx_radio_id=rid)
    assert session.sequence == 0
    for _ in range(129):
        server.observe(b"\x15\x00", -70, 5, rx_radio_id="v3909")
    assert session.failed == "queue_overflow" and writer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["callback", "observation", "ownership"])
async def test_lost_physical_observation_binding_revokes(connected, mutation):
    server, session, radios, reader, writer = connected
    if mutation == "callback":
        radios["v3909"].rx_callback = lambda *args: None
    elif mutation == "observation":
        server.fabric._observation_callbacks.clear()
    else:
        server.fabric.has_rx_binding = lambda rid: False
    feed(reader, type="barrier", nonce="independent-nonce", request_id=1)
    await until(lambda: writer.closed)
    assert session.failed
    assert not any(e["type"] == "barrier" for e in writer.events)


@pytest.mark.asyncio
async def test_unauthenticated_connection_cannot_hold_the_slot():
    radios = {"dir909": Radio()}
    binding = {"receivers": list(radios)}
    server = RawReceiptServer(
        fabric=SimpleNamespace(
            get_radio=radios.__getitem__, has_rx_binding=lambda rid: True, _observation_callbacks=[]
        ),
        binding=binding,
        secret=b"x" * 32,
        socket_path="/unused",
        binding_provider=lambda: binding,
    )
    server.fabric._observation_callbacks.append(server.observe)
    idle_reader, idle_writer = asyncio.StreamReader(), Writer()
    idle = asyncio.create_task(server._client(idle_reader, idle_writer))  # never authenticates
    await until(lambda: idle_writer.events)
    assert server.session is None
    reader, writer = asyncio.StreamReader(), Writer()
    bot = asyncio.create_task(server._client(reader, writer))
    await until(lambda: writer.events)
    session = "real-bot-session"
    proof = hmac.new(
        server.secret, (writer.events[0]["challenge"] + "\0" + session).encode(), hashlib.sha256
    ).hexdigest()
    feed(reader, type="auth", session_id=session, proof=proof)
    await until(lambda: any(e["type"] == "ready" for e in writer.events))
    assert server.session is not None and server.session.session_id == session
    # A second authenticated client is still refused: the bot is never evicted.
    other_reader, other_writer = asyncio.StreamReader(), Writer()
    await server._client(other_reader, other_writer)
    assert other_writer.closed and server.session.session_id == session
    for r in (reader, idle_reader):
        r.feed_eof()
    await asyncio.wait_for(asyncio.gather(bot, idle, return_exceptions=True), 5)
