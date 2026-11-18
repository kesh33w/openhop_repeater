"""A second ear may add forwarding eligibility, never another local ACK/delivery."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from openhop_core.protocol import LocalIdentity
from openhop_core.protocol.packet_builder import PacketBuilder
from openhop_core.node.handlers.text import TXT_ACK_DELAY_MS
from repeater.handler_helpers.text import TextHelper
from tests import test_fabric_fanout as f
from tests.test_network_topology import Rig, config
from tests.test_handler_helpers_path_protocol_text import _acl_with_client, _SendDest


@pytest.mark.asyncio
@pytest.mark.parametrize("home,ear", [("local", "omni909"), ("link", "omni910")])
@pytest.mark.parametrize("receiver_first", [True, False])
@pytest.mark.parametrize("concurrent", [True, False])
async def test_two_ears_deliver_authenticated_local_text_and_ack_once(
    tmp_path, home, ear, receiver_first, concurrent
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
    helper.register_identity("repeater", server, identity_type="repeater", radio_config={})
    helper._on_message_received = AsyncMock()
    router.daemon.text_helper = helper
    request, _ = PacketBuilder.create_text_message(
        _SendDest(server.get_public_key()),
        client,
        "offline duplicate local delivery",
        0,
        "direct",
        None,
        0,
    )
    rig.dispatcher.register_handler(request.get_payload_type(), router._route_packet)
    try:
        order = (ear, home) if receiver_first else (home, ear)

        async def receive(rid):
            await rig.dispatcher._process_received_packet(
                request.write_to(), -75, 2, rx_radio_id=rid
            )

        if concurrent:
            await asyncio.gather(*(receive(rid) for rid in order))
        else:
            for rid in order:
                await receive(rid)
        await asyncio.sleep(TXT_ACK_DELAY_MS / 1000 + 0.1)
        assert helper._on_message_received.await_count == 1
        assert rig.air.order == [home]
    finally:
        rig.close()
