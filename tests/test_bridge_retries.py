"""Bounded per-egress retries distinguish rejection from uncertain radio writes."""

# ruff: noqa: F811
import copy
from unittest.mock import AsyncMock

import pytest
from tests.test_channel_bridge_engine import rig_factory  # noqa: F401
from tests.test_channel_bridge import packet
from tests.test_fabric_fanout import _rx


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [False, True])
async def test_known_unsent_crossing_recovers_without_repeating_successful_home(
    rig_factory, origin
):
    rig = rig_factory()
    original = rig.link.send
    calls = 0

    async def reject_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        return False if calls == 1 else await original(*args, **kwargs)

    rig.link.send = AsyncMock(side_effect=reject_once)
    metadata = {"home_radio_id": "local"} if origin else _rx("local")
    assert await rig.handler(packet(), metadata, local_transmission=origin)
    assert rig.link.send.await_count == 2
    assert rig.air.order.count("local") == rig.air.order.count("link") == 1
    rows = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"]
    assert all(row["success"] for row in rows)
    assert await rig.handler.bridge_echoes.contains(packet())


@pytest.mark.asyncio
async def test_two_known_failures_release_reservation_without_replay_retry(rig_factory):
    rig = rig_factory()
    rig.link.send = AsyncMock(return_value=False)
    assert await rig.handler(packet(), _rx("local"))  # local repeat succeeded
    assert rig.link.send.await_count == 2
    assert not await rig.handler.bridge_echoes.contains(packet())
    assert not await rig.handler(packet(), _rx("local"))
    assert rig.link.send.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [False, True])
@pytest.mark.parametrize(
    "outcome", [None, {"ok": True, "tx_outcome_uncertain": True}, RuntimeError("after write")]
)
async def test_uncertain_crossing_never_retries_and_survives_restart(rig_factory, origin, outcome):
    first = rig_factory()
    first.link.send = (
        AsyncMock(side_effect=outcome)
        if isinstance(outcome, Exception)
        else AsyncMock(return_value=outcome)
    )
    metadata = {"home_radio_id": "local"} if origin else _rx("local")
    await first.handler(packet(), metadata, local_transmission=origin)
    assert first.link.send.await_count == 1
    rows = first.handler.storage.record_packet.call_args.kwargs["tx_egress"]
    assert rows[1]["success"] is False
    assert await first.handler.bridge_echoes.contains(packet())
    first.handler.close_channel_bridge()
    second = rig_factory()
    assert not await second.handler(copy.deepcopy(packet()), _rx("local"))
    assert not second.air.frames


@pytest.mark.asyncio
async def test_retry_respects_queue_ttl(rig_factory):
    rig = rig_factory(queue_ttl_seconds=1)
    rig.link.send = AsyncMock(return_value=False)
    assert await rig.handler(packet(), _rx("local"))
    assert rig.link.send.await_count == 1  # the one-second backoff cannot fit
    assert not await rig.handler.bridge_echoes.contains(packet())


@pytest.mark.asyncio
async def test_stale_uncertainty_is_cleared_before_new_attempt(rig_factory):
    rig = rig_factory()
    p = packet()
    p._tx_metadata = {"tx_outcome_uncertain": True}
    assert await rig.handler(p, _rx("local"))
    assert rig.air.order == ["local", "link"]
    assert all(
        row["success"] for row in rig.handler.storage.record_packet.call_args.kwargs["tx_egress"]
    )


@pytest.mark.asyncio
async def test_dispatcher_typeerror_after_write_never_falls_back_or_retries():
    from tests.test_tx_lock import _make_handler, _make_packet

    handler, p = _make_handler(), _make_packet()
    handler.dispatcher.send_packet = AsyncMock(side_effect=TypeError("inside send after write"))
    task = await handler.schedule_retransmit(
        p, 0, local_transmission=True, preferred_tx_radio_id="link"
    )
    with pytest.raises(TypeError):
        await task
    assert handler.dispatcher.send_packet.await_count == 1
    assert p._tx_metadata["tx_outcome_uncertain"] is True


@pytest.mark.asyncio
async def test_unrecognized_dispatcher_result_is_uncertain_not_success():
    from tests.test_tx_lock import _make_handler, _make_packet

    handler, p = _make_handler(), _make_packet()
    handler.dispatcher.send_packet = AsyncMock(return_value={"queued": True})
    task = await handler.schedule_retransmit(p, 0, local_transmission=True)
    assert not await task
    assert handler.dispatcher.send_packet.await_count == 1
    assert p._tx_metadata["tx_outcome_uncertain"] is True
    handler._record_packet_sent.assert_not_called()
