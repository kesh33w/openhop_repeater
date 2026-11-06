import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from openhop_core.protocol import LocalIdentity
from openhop_core.protocol.packet_builder import PacketBuilder
from repeater.handler_helpers.discovery import DiscoveryHelper
from repeater.handler_helpers.login import LoginHelper
from repeater.handler_helpers.protocol_request import ProtocolRequestHelper
from openhop_core.node.handlers.protocol_request import REQ_TYPE_GET_STATUS
from tests import test_fabric_fanout as fabric_tests
from tests.test_channel_bridge_engine import rig_factory

class Identity:
    def get_public_key(self): return bytes(range(32))

@pytest.mark.asyncio
@pytest.mark.parametrize('radio', ['local', 'link'])
async def test_discovery_reply_returns_on_receiving_radio(rig_factory, radio):
    rig = rig_factory(); router = fabric_tests._router_for(rig)
    router.daemon.deliver_control_data = None
    helper = DiscoveryHelper(Identity(), packet_injector=router.inject_packet, response_jitter_ms=0)
    router.daemon.discovery_helper = helper
    request = PacketBuilder.create_discovery_request(tag=123, filter_mask=4)
    request._snr = 2.0; request._rssi = -75; request._rx_radio_id = radio
    await router._route_packet(request)
    await asyncio.gather(*tuple(helper._pending_tasks))
    assert rig.air.order == [radio]
    assert rig.sent_packets[0][1]._rx_radio_id is None

@pytest.mark.asyncio
@pytest.mark.parametrize('radio', ['local', 'link'])
async def test_guest_login_returns_on_receiving_radio(rig_factory, radio):
    rig = rig_factory(); router = fabric_tests._router_for(rig)
    router.daemon.deliver_control_data = None
    helper = LoginHelper(MagicMock(), packet_injector=router.inject_packet)
    server = LocalIdentity(seed=bytes(range(32))); client = LocalIdentity(seed=bytes(reversed(range(32))))
    helper.register_identity('repeater', server, identity_type='repeater', config={'repeater':{'security':{'admin_password':'unit-only-admin','guest_password':None,'allow_read_only':True,'max_clients':5}}})
    router.daemon.login_helper = helper
    contact=SimpleNamespace(public_key=server.get_public_key().hex(),type=2,out_path_len=0,out_path=b'')
    request=PacketBuilder.create_login_packet(contact, client, '')
    request._rx_radio_id = radio; request._snr = 2.; request._rssi = -75
    await router._route_packet(request)
    await asyncio.gather(*tuple(helper._pending_tasks))
    assert rig.air.order and all(x==radio for x in rig.air.order)
    assert all(packet._rx_radio_id is None for _,packet in rig.sent_packets)
    acl=helper.get_acl_for_identity(server.get_public_key()[0])
    assert len(acl.get_all_clients())==1
    assert not acl.get_all_clients()[0].is_admin()
    status = ProtocolRequestHelper(MagicMock(), packet_injector=router.inject_packet, acl_dict=helper.get_acl_dict())
    status.register_identity('repeater', server, identity_type='repeater')
    router.daemon.protocol_request_helper = status
    rig.air.frames.clear(); rig.sent_packets.clear()
    query, _ = PacketBuilder.create_protocol_request(contact, client, REQ_TYPE_GET_STATUS)
    query._rx_radio_id = radio; query._snr = 2.; query._rssi = -75
    await router._route_packet(query)
    assert rig.air.order == [radio]
    assert rig.sent_packets[0][1]._rx_radio_id is None

@pytest.mark.asyncio
async def test_simultaneous_requests_do_not_share_return_radio(rig_factory):
    rig = rig_factory(); router = fabric_tests._router_for(rig)
    router.daemon.deliver_control_data = None
    helper = DiscoveryHelper(Identity(), packet_injector=router.inject_packet, response_jitter_ms=0)
    router.daemon.discovery_helper = helper
    requests=[]
    for tag,radio in enumerate(('local','link'),1):
        packet=PacketBuilder.create_discovery_request(tag=tag,filter_mask=4)
        packet._rx_radio_id=radio; packet._snr=2.;packet._rssi=-75;requests.append(packet)
    await asyncio.gather(*(router._route_packet(p) for p in requests))
    await asyncio.gather(*tuple(helper._pending_tasks))
    assert sorted(rig.air.order)==['link','local']
    assert router._reply_home_radio.get() is None
