"""Offline failure, migration, and concurrency boundaries for active ears."""
import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from repeater.channel_bridge import EchoLedger
from tests.test_active_receiver_order_review import active_rig, incoming
from tests.test_channel_bridge import C1, C2, packet
from tests import test_fabric_fanout as f


@pytest.mark.parametrize('mode', ['legacy', 'unknown', 'stale', 'source'])
def test_ledger_role_metadata_is_conservative_and_survives_reopen(tmp_path, mode):
    path = tmp_path / 'ledger.sqlite'
    source, target = packet(C1), packet(C2)
    ledger = EchoLedger(path)
    token = ledger.reserve(source, target, separate_source=mode != 'legacy')
    ledger.finish(token, success=True)
    if mode == 'unknown':
        ledger.db.execute("UPDATE echo_roles SET role='corrupt'")
    if mode == 'stale':
        # A rollback release may replace echoes without knowing the side table.
        ledger.db.execute('UPDATE echoes SET expires=expires+1')
    ledger.db.commit()
    ledger.close()
    ledger = EchoLedger(path)
    try:
        assert ledger.contains_received(source) is (mode != 'source')
        assert ledger.contains_received(target)
        assert ledger.contains(source)  # Downgrade reader remains conservative.
        assert ledger.reserve(source, target, separate_source=True) is None
        assert len(ledger.db.execute('PRAGMA table_info(echoes)').fetchall()) == 4
    finally:
        ledger.close()


def test_failed_pending_ledger_releases_role_metadata(tmp_path):
    ledger = EchoLedger(tmp_path / 'ledger.sqlite')
    try:
        source, target = packet(C1), packet(C2)
        token = ledger.reserve(source, target, separate_source=True)
        ledger.finish(token, success=False)
        assert not ledger.contains(source)
        assert not ledger.contains_received(target)
        assert not ledger.db.execute('SELECT * FROM echo_roles').fetchall()
        assert ledger.reserve(source, target, separate_source=True)
    finally:
        ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['cancel', 'exception', 'waiter_cancel'])
async def test_local_delivery_pending_failure_never_replays_side_effect(tmp_path, failure):
    rig = active_rig(tmp_path)
    router = f._router_for(rig)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def local(p):
        calls.append(p)
        entered.set()
        await release.wait()
        if failure == 'exception':
            raise RuntimeError('after local effect')
        return True
    router._route_packet_on_radio = local
    first = incoming('mapped', C1, 'omni909')
    second = copy.deepcopy(first)
    second._rx_radio_id = 'local'
    a = asyncio.create_task(router._route_packet(first))
    b = None
    try:
        await entered.wait()
        b = asyncio.create_task(router._route_packet(second))
        await asyncio.sleep(0)
        if failure == 'cancel':
            a.cancel()
        elif failure == 'waiter_cancel':
            b.cancel()
        release.set()
        await asyncio.wait_for(asyncio.gather(a, b, return_exceptions=True), 1)
        await router._route_packet(second)
        assert len(calls) == 1
        assert all(future.done() and future.result() for _, future in router._network_deliveries.values())
    finally:
        release.set()
        for task in (a, b):
            if task and not task.done():
                task.cancel()
        rig.close()


@pytest.mark.asyncio
async def test_local_delivery_cap_does_not_evict_pending_results(tmp_path):
    rig = active_rig(tmp_path)
    router = f._router_for(rig)
    router._route_packet_on_radio = AsyncMock()
    try:
        for i in range(1024):
            router._network_deliveries[i] = (0, asyncio.get_running_loop().create_future())
        await router._route_packet(incoming('mapped', C1, 'local'))
        router._route_packet_on_radio.assert_not_awaited()
        assert len(router._network_deliveries) == 1024
        assert router._cap_drop_count == 1
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_intermediate_observation_does_not_consume_final_local_delivery(tmp_path):
    rig = active_rig(tmp_path)
    router = f._router_for(rig)
    router._route_packet_on_radio = AsyncMock(return_value=True)
    try:
        first = incoming('native_direct', C1, 'omni909')
        final = copy.deepcopy(first)
        final._rx_radio_id = 'local'
        final.path = bytearray()
        final.path_len = 0
        assert first.calculate_packet_hash() == final.calculate_packet_hash()
        await router._route_packet(first)
        await router._route_packet(final)
        assert router._route_packet_on_radio.await_count == 2
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_pending_destination_claim_survives_expiry_until_send_finishes(tmp_path, monkeypatch):
    rig = active_rig(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.ears['link'].send
    async def delayed(raw):
        entered.set()
        await release.wait()
        return await original(raw)
    rig.ears['link'].send = delayed
    a = asyncio.create_task(rig.handler(incoming('mapped', C1, 'omni909'), f._rx('omni909')))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert None in rig.handler._forward_destinations.values()
        # Expire completed caches without changing the event loop clock.
        for key in rig.handler._forward_destinations:
            assert rig.handler._forward_destinations[key] is None
        rig.handler.channel_bridge.ttl = -1
        b = asyncio.create_task(rig.handler(incoming('mapped', C1, 'local'), f._rx('local')))
        await asyncio.sleep(.02)
        release.set()
        await asyncio.wait_for(asyncio.gather(a, b), 2)
        assert sorted(rig.air.order) == ['link', 'local']
        assert all(value is not None for value in rig.handler._forward_destinations.values())
    finally:
        release.set()
        if not a.done():
            a.cancel()
        rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain', [False, True])
async def test_failed_destination_retries_only_when_definitely_unsent(tmp_path, uncertain):
    rig = active_rig(tmp_path)
    try:
        original = rig.ears['link'].send
        calls = []
        if uncertain:
            async def ambiguous(raw):
                calls.append(raw)
                return {'tx_outcome_uncertain': True, 'ok': False}
            rig.ears['link'].send = ambiguous
        else:
            rig.ears['link'].is_connected = False
        await rig.handler(incoming('mapped', C1, 'omni909'), f._rx('omni909'))
        rig.ears['link'].is_connected = True
        rig.ears['link'].send = original
        await rig.handler(incoming('mapped', C1, 'local'), f._rx('local'))
        assert sorted(rig.air.order) == (['local'] if uncertain else ['link', 'local'])
        if uncertain:
            assert len(calls) == 1
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_transmitted_wire_echo_dropped_but_original_other_ear_admitted(tmp_path):
    rig = active_rig(tmp_path)
    router = f._router_for(rig)
    source = incoming('mapped', C1, 'local')
    rig.dispatcher.register_handler(source.get_payload_type(), router._route_packet)
    try:
        await rig.dispatcher._process_received_packet(source.write_to(), -90, 8, rx_radio_id='local')
        before = len(router._network_deliveries)
        for rid, raw in list(rig.air.frames):
            await rig.dispatcher._process_received_packet(raw, -90, 8, rx_radio_id='omni909' if rid == 'local' else 'omni910')
        assert len(router._network_deliveries) == before
        await rig.dispatcher._process_received_packet(source.write_to(), -90, 8, rx_radio_id='omni909')
        assert sorted(rig.air.order) == ['link', 'local']
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_cancelled_primary_keeps_claim_pending_until_ack_extras_finish(tmp_path):
    from openhop_core.protocol.constants import PAYLOAD_TYPE_ACK
    from tests.test_network_topology import Rig, config
    cfg = config(tmp_path, receiver=True)
    cfg['channel_bridge']['direct_routes'] = [{'source_radio': 'local', 'target_radio': 'link'}]
    rig = Rig(cfg)
    rig.handler.multi_acks = 1
    scheduled = []
    ready = asyncio.Event()
    async def schedule(*args, **kwargs):
        task = asyncio.get_running_loop().create_future()
        scheduled.append(task)
        if len(scheduled) == 2:
            ready.set()
        return task
    rig.handler._schedule_egress_tx = schedule
    p = f._direct(payload=b'ack2\x00\x19', payload_type=PAYLOAD_TYPE_ACK, rx='omni909')
    main = asyncio.create_task(rig.handler(p, f._rx('omni909')))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        main.cancel()
        await asyncio.gather(main, return_exceptions=True)
        assert scheduled[-1].cancelled()
        assert not scheduled[0].done()
        assert rig.handler._forward_destinations
        assert all(value is None for value in rig.handler._forward_destinations.values())
        scheduled[0].set_result(True)
        await asyncio.sleep(0)
        assert all(value is not None for value in rig.handler._forward_destinations.values())
    finally:
        for task in scheduled:
            if not task.done():
                task.cancel()
        rig.close()
