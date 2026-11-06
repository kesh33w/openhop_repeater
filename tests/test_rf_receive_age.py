import time
from types import SimpleNamespace
import pytest

from repeater import engine
from tests import test_fabric_fanout as fabric_tests
from tests.test_channel_bridge import packet
from tests.test_channel_bridge_engine import rig_factory
from tests.test_storage_collector_ws_stats_throttle import _make_collector


@pytest.fixture
def radio_age_rig(rig_factory, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(engine, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    rig = rig_factory()
    yield rig, clock
    rig.handler.cleanup()


def ages(handler):
    return {row['radio_id']: row['seconds_since_last_rx'] for row in handler.get_rf_receive_ages()}


@pytest.mark.asyncio
async def test_raw_rx_attributes_each_radio_before_packet_parsing(radio_age_rig):
    rig, clock = radio_age_rig
    assert ages(rig.handler) == {'local': None, 'link': None}
    await rig.dispatcher._process_received_packet(b'', -90, 4., rx_radio_id='local')
    clock[0] = 105.
    await rig.dispatcher._process_received_packet(b'', -80, 5., rx_radio_id='link')
    clock[0] = 110.
    assert ages(rig.handler) == {'local': 10., 'link': 5.}
    clock[0] = 115.
    await rig.dispatcher._process_received_packet(b'', -90, 4., rx_radio_id='local')
    assert ages(rig.handler) == {'local': 0., 'link': 10.}
    assert not rig.air.frames


@pytest.mark.asyncio
async def test_unknown_ingress_does_not_refresh_a_default_radio(radio_age_rig):
    rig, _ = radio_age_rig
    for radio_id in (None, 'missing'):
        await rig.dispatcher._process_received_packet(b'', -90, 4., rx_radio_id=radio_id)
    assert ages(rig.handler) == {'local': None, 'link': None}


@pytest.mark.asyncio
async def test_private_link_routing_context_is_not_rf_reception(radio_age_rig):
    rig, clock = radio_age_rig
    await rig.dispatcher._process_received_packet(b'', -90, 4., rx_radio_id='local')
    clock[0] = 108.
    peer = packet()
    peer._private_peer = True
    peer._private_peer_deadline = time.monotonic() + 5
    peer._rx_radio_id = 'link'
    peer._rssi = peer._snr = None
    await fabric_tests._router_for(rig)._route_packet(peer)
    assert ages(rig.handler) == {'local': 8., 'link': None}


@pytest.mark.asyncio
async def test_http_and_websocket_heartbeats_report_age_without_resetting_it(radio_age_rig):
    rig, clock = radio_age_rig
    await rig.dispatcher._process_received_packet(b'', -90, 4., rx_radio_id='link')
    collector = _make_collector()
    collector.repeater_handler = rig.handler
    try:
        clock[0] = 125.
        assert rig.handler.get_stats()['rf_receive_ages'] == rig.handler.get_rf_receive_ages()
        collector._broadcast_stats_once()
        assert collector.websocket_broadcast_stats.call_args.args[0]['system_stats']['rf_receive_ages'] == [
            {'radio_id': 'local', 'seconds_since_last_rx': None},
            {'radio_id': 'link', 'seconds_since_last_rx': 25.},
        ]
        clock[0] = 130.
        collector._broadcast_stats_once()
        assert ages(rig.handler) == {'local': None, 'link': 30.}
    finally:
        collector.close()


def test_cleanup_unsubscribes_the_raw_receive_callback(radio_age_rig):
    rig, _ = radio_age_rig
    callback = rig.handler._record_rf_receive
    assert callback in rig.dispatcher._raw_rx_subscribers
    rig.handler.cleanup()
    assert callback not in rig.dispatcher._raw_rx_subscribers
