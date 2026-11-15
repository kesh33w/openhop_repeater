"""Local replies return through the network TX while RX repeats stay disabled."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openhop_core.protocol import LocalIdentity
from openhop_core.protocol.packet_builder import PacketBuilder
from openhop_core.protocol.constants import PAYLOAD_TYPE_ACK
from openhop_core.node.handlers.protocol_request import REQ_TYPE_GET_STATUS
from openhop_core.node.handlers.text import TXT_ACK_DELAY_MS
from repeater.handler_helpers.login import LoginHelper
from repeater.handler_helpers.protocol_request import ProtocolRequestHelper
from repeater.handler_helpers.text import TextHelper
from tests.test_network_topology import Rig, config
from tests import test_fabric_fanout as f
from tests.test_channel_bridge import packet
from tests.test_handler_helpers_path_protocol_text import _acl_with_client, _SendDest


@pytest.mark.asyncio
@pytest.mark.parametrize("ear,home", [("omni909", "local"), ("omni910", "link")])
async def test_extra_receiver_login_and_admin_status_return_on_same_network(
    tmp_path, ear, home
):
    rig = Rig(config(tmp_path, receiver=True, repeat=False))
    router = f._router_for(rig)
    router.daemon.deliver_control_data = None
    server = LocalIdentity(seed=bytes(range(32)))
    client = LocalIdentity(seed=bytes(reversed(range(32))))
    helper = LoginHelper(MagicMock(), packet_injector=router.inject_packet)
    helper.register_identity(
        "repeater",
        server,
        identity_type="repeater",
        config={
            "repeater": {
                "security": {
                    "admin_password": "offline-only",
                    "guest_password": None,
                    "allow_read_only": True,
                    "max_clients": 5,
                }
            }
        },
    )
    router.daemon.login_helper = helper
    contact = SimpleNamespace(
        public_key=server.get_public_key().hex(), type=2, out_path_len=0, out_path=b""
    )
    request = PacketBuilder.create_login_packet(contact, client, "offline-only")
    request._rx_radio_id = ear
    request._snr = 2.0
    request._rssi = -75
    try:
        await router._route_packet(request)
        await asyncio.gather(*tuple(helper._pending_tasks))
        assert rig.air.order and set(rig.air.order) == {home}
        acl = helper.get_acl_for_identity(server.get_public_key()[0])
        assert acl.get_all_clients()[0].is_admin()
        status = ProtocolRequestHelper(
            MagicMock(),
            packet_injector=router.inject_packet,
            acl_dict=helper.get_acl_dict(),
        )
        status.register_identity("repeater", server, identity_type="repeater")
        router.daemon.protocol_request_helper = status
        rig.air.frames.clear()
        query, _ = PacketBuilder.create_protocol_request(
            contact, client, REQ_TYPE_GET_STATUS
        )
        query._rx_radio_id = ear
        query._snr = 2.0
        query._rssi = -75
        await router._route_packet(query)
        assert rig.air.order == [home]
        # The general relay permission has not been relaxed.
        with pytest.raises(ValueError):
            await rig.radio.send(packet().write_to(), radio_id=home, rx_radio_id=ear)
    finally:
        rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ear,home", [("omni909", "local"), ("omni910", "link")])
async def test_extra_receiver_authenticated_text_generates_local_ack(
    tmp_path, ear, home
):
    rig = Rig(config(tmp_path, receiver=True, repeat=False))
    router = f._router_for(rig)
    router.daemon.deliver_control_data = None
    server = LocalIdentity(seed=bytes(range(32)))
    client = LocalIdentity(seed=bytes(reversed(range(32))))
    acl = _acl_with_client(client, server)
    helper = TextHelper(
        MagicMock(),
        packet_injector=router.inject_packet,
        acl_dict={server.get_public_key()[0]: acl},
    )
    helper.register_identity(
        "repeater", server, identity_type="repeater", radio_config={}
    )
    helper._on_message_received = AsyncMock()
    router.daemon.text_helper = helper
    request, _ = PacketBuilder.create_text_message(
        _SendDest(server.get_public_key()),
        client,
        "offline ACK test",
        0,
        "direct",
        None,
        0,
    )
    request._rx_radio_id = ear
    request._snr = 2.0
    request._rssi = -75
    try:
        await router._route_packet(request)
        await asyncio.sleep(TXT_ACK_DELAY_MS / 1000.0 + 0.1)
        assert rig.air.order == [home]
        result = f.Packet()
        result.read_from(rig.air.frames[0][1])
        assert result.get_payload_type() == PAYLOAD_TYPE_ACK
        assert rig.handler.rx_count <= 1
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_core_delayed_replies_capture_request_and_never_cross_network():
    rig = Rig(config(receiver=True, repeat=False))
    pending = []

    async def handler(request):
        reply = PacketBuilder.create_ack_from_bytes(
            bytes(request.payload[:4]), route_type="direct"
        )

        async def later():
            await asyncio.sleep(0.01)
            return await rig.dispatcher.send_packet(reply, wait_for_ack=False)

        pending.append(asyncio.create_task(later()))

    rig.dispatcher.register_handler(2, handler, local_delivery=True)
    requests = [
        f._direct(payload=b"ABCD", payload_type=2, path=b"", rx="omni909"),
        f._direct(payload=b"EFGH", payload_type=2, path=b"", rx="omni910"),
    ]
    try:
        await asyncio.gather(*(rig.dispatcher._dispatch(p) for p in requests))
        assert all(await asyncio.gather(*pending))
        assert sorted(rig.air.order) == ["link", "local"]
        assert rig.dispatcher._reply_ingress.get() is None
    finally:
        rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ear", ["omni909", "omni910"])
async def test_shadow_reply_context_cannot_send_even_with_explicit_target(ear):
    rig = Rig(config())
    try:
        with pytest.raises(ValueError):
            await rig.radio.send(
                packet().write_to(), radio_id="local", reply_to_radio_id=ear
            )
        p = PacketBuilder.create_ack_from_bytes(b"ABCD", route_type="direct")
        p._reply_to_radio_id = ear
        with pytest.raises(ValueError):
            await rig.dispatcher.send_packet(p, wait_for_ack=False)
        assert not rig.air.frames
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_reply_context_does_not_authorize_other_network_or_received_copy():
    rig = Rig(config(receiver=True))
    try:
        raw = packet().write_to()
        with pytest.raises(ValueError):
            await rig.radio.send(raw, radio_id="link", reply_to_radio_id="omni909")
        with pytest.raises(ValueError):
            await rig.radio.send(
                raw,
                radio_id="local",
                rx_radio_id="omni909",
                reply_to_radio_id="omni909",
            )
        with pytest.raises(ValueError):
            await rig.radio.send(raw, radio_id="omni909", reply_to_radio_id="omni909")
        assert not rig.air.frames
    finally:
        rig.close()


def test_wire_parse_clears_local_reply_privilege():
    p = packet()
    raw = p.write_to()
    p._reply_to_radio_id = "omni909"
    p._rx_radio_id = "omni910"
    p.read_from(raw)
    assert p._reply_to_radio_id is None
    assert p._rx_radio_id is None
