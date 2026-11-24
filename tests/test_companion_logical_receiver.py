"""Logical companion subscriptions preserve routing and physical provenance."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from repeater.bridge_homes import validate_companion_home
from repeater.companion.utils import companion_receive_members
from repeater.main import RepeaterDaemon
from repeater.packet_router import PacketRouter
from tests.test_channel_bridge import packet
from tests.test_network_topology import Rig, config


@pytest.fixture
def logical():
    cfg = config(receiver=True)
    shadow = copy.deepcopy(cfg["radios"][3])
    shadow.update(id="rak", role="shadow")
    cfg["radios"].append(shadow)
    rig = Rig(cfg)
    daemon = RepeaterDaemon(cfg, radio=rig.radio)
    daemon.repeater_handler = rig.handler
    bot = SimpleNamespace()
    daemon._configure_companion_routing(
        bot,
        {
            "home_radio_id": "local",
            "receive_network": "909",
        },
    )
    daemon.companion_bridges = {1: bot}
    yield daemon, bot, rig
    rig.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"home_radio_id": "local", "receive_network": "missing"},
        {"home_radio_id": "local", "receive_network": None},
        {"home_radio_id": "local", "receive_network": "shadow:rak"},
        {"home_radio_id": "link", "receive_network": "909"},
        {"home_radio_id": "omni909", "receive_network": "909"},
        {"receive_network": "909"},
        {"home_radio_id": "local", "receive_network": "909", "receive_radio_ids": None},
    ],
)
def test_invalid_subscription_rejected_in_config_and_runtime(logical, settings):
    daemon, _, _ = logical
    with pytest.raises(ValueError):
        validate_companion_home(daemon.config, settings)
    with pytest.raises(ValueError):
        daemon._configure_companion_routing(SimpleNamespace(), settings)


@pytest.mark.asyncio
async def test_raw_and_decoded_share_membership_and_keep_provenance(logical):
    daemon, bot, _ = logical
    assert companion_receive_members(bot) == frozenset(["local", "omni909"])
    router = PacketRouter(daemon)
    router._policy_blocks_companion = lambda p, m: False
    fs = SimpleNamespace(bridge=bot, companion_hash="0x01", push_rx_raw=Mock())
    daemon.companion_frame_servers = [fs]
    for rid in ["local", "omni909", "link", "omni910", "rak", None, "unknown"]:
        allowed = rid in ("local", "omni909")
        p = packet()
        p._rx_radio_id = rid
        assert bool(router._companion_bridges_for_packet(p, {"rx_radio_id": rid})) == allowed
        assert p._rx_radio_id == rid
        fs.push_rx_raw.reset_mock()
        await daemon._on_raw_rx_for_companions(b"frame", -90, 5, rx_radio_id=rid)
        assert fs.push_rx_raw.called == allowed
    p._injected_origin_hash = "0x01"
    assert router._companion_bridges_for_packet(p, {"rx_radio_id": "omni909"}) == {}
    fs.push_rx_raw.reset_mock()
    await daemon._on_raw_rx_for_companions(
        b"frame", -90, 5, rx_radio_id="omni909", exclude_hash="0x01"
    )
    fs.push_rx_raw.assert_not_called()
    router._policy_blocks_companion = lambda p, m: True
    assert router._companion_bridges_for_packet(packet(), {"rx_radio_id": "omni909"}) == {}


def test_missing_or_reconfigured_resolver_fails_closed(logical):
    _, bot, rig = logical
    fabric = rig.radio.fabric
    original = fabric.logical_receiver
    fabric.logical_receiver = None
    assert companion_receive_members(bot) == frozenset()
    fabric.logical_receiver = SimpleNamespace(members=lambda network: {"local", "rak"})
    assert companion_receive_members(bot) == frozenset()
    fabric.logical_receiver = original
    bot._receive_home_radio_id = "link"
    assert companion_receive_members(bot) == frozenset()
    bot._receive_home_radio_id = "local"
    assert companion_receive_members(bot) == frozenset(["local", "omni909"])


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["local", "omni909"])
async def test_concurrent_logical_deliveries_once_with_original_ingress(logical, first):
    daemon, bot, rig = logical
    daemon.dispatcher = SimpleNamespace(packet_filter=SimpleNamespace(window_seconds=300))
    router = PacketRouter(daemon)
    router._policy_blocks_companion = lambda p, m: False
    delivered = []

    async def route(p):
        await asyncio.sleep(0.01)
        assert router._companion_bridges_for_packet(p, {"rx_radio_id": p._rx_radio_id}) == {1: bot}
        delivered.append(p._rx_radio_id)
        return True

    router._route_packet_on_radio = route
    p = packet()
    p._rx_radio_id = first
    q = copy.copy(p)
    q._rx_radio_id = "omni909" if first == "local" else "local"
    await asyncio.gather(router._route_packet(p), router._route_packet(q))
    await router._route_packet(q)
    assert delivered == [first]
    # Logical membership does not change the explicit reply transmitter.
    assert rig.topology.reply_destination("omni909") == "local"


@pytest.mark.asyncio
async def test_confirmed_egress_respects_logical_filter_without_raw_loopback(logical):
    daemon, bot, _ = logical
    router = PacketRouter(daemon)
    router._policy_blocks_companion = lambda p, m: False
    router._fan_out_to_bridges = AsyncMock()
    daemon._on_raw_rx_for_companions = AsyncMock()
    await router.deliver_bridge_egress(packet(), packet(), "local")
    assert router._fan_out_to_bridges.call_args.args[1] == {1: bot}
    daemon._on_raw_rx_for_companions.assert_not_awaited()
    router._fan_out_to_bridges.reset_mock()
    await router.deliver_bridge_egress(packet(), packet(), "link")
    router._fan_out_to_bridges.assert_not_awaited()


def test_third_active_ear_joins_without_companion_configuration_change(logical):
    daemon, bot, rig = logical
    cfg = copy.deepcopy(daemon.config)
    third = copy.deepcopy(cfg["radios"][2])
    third["id"] = "third909"
    cfg["radios"].append(third)
    from repeater.network_topology import topology_from_config
    from tests.test_fabric_fanout import _FakeRadio

    rig.radio.fabric.register_radio(_FakeRadio("third909", rig.air), radio_id="third909")
    rig.radio.fabric.configure_topology(topology_from_config(cfg))
    assert companion_receive_members(bot) == frozenset(["local", "omni909", "third909"])
    router = PacketRouter(daemon)
    router._policy_blocks_companion = lambda p, m: False
    assert router._companion_bridges_for_packet(packet(), {"rx_radio_id": "third909"}) == {1: bot}


@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        (["local"], {"local", "omni909"}),
        (["omni909"], {"local", "omni909"}),
        (["local", "link"], {"local", "omni909", "link", "omni910"}),
        (["rak"], {"rak"}),
    ],
)
def test_receive_radio_ids_cover_whole_network(logical, listed, expected):
    """A frame is delivered once per network (identical bytes are deduplicated
    across that network's radios), so listening on one radio of a network
    would lose every frame another of its receivers heard first."""
    daemon, _, _ = logical
    bot = SimpleNamespace()
    daemon._configure_companion_routing(
        bot, {"home_radio_id": "local", "receive_radio_ids": listed}
    )
    assert companion_receive_members(bot) == frozenset(expected)


def test_single_radio_companion_gets_copy_heard_first_by_other_receiver(logical):
    daemon, _, _ = logical
    bot = SimpleNamespace()
    daemon._configure_companion_routing(
        bot, {"home_radio_id": "local", "receive_radio_ids": ["local"]}
    )
    daemon.companion_bridges = {1: bot}
    router = PacketRouter(daemon)
    router._policy_blocks_companion = lambda p, m: False
    # omni909 heard it first; the dispatcher drops local's identical copy.
    assert router._companion_bridges_for_packet(packet(), {"rx_radio_id": "omni909"}) == {1: bot}
    assert router._companion_bridges_for_packet(packet(), {"rx_radio_id": "omni910"}) == {}
