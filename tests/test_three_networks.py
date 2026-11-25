"""A channel bridge across three networks: only mapped channels cross, each once."""

import copy
import hashlib

import pytest

from repeater.bridge_traffic import DirectionalTrafficGuard
from repeater.channel_bridge import BridgeSettings, Channel, EchoLedger
from repeater.network_topology import topology_from_config
from tests import test_fabric_fanout as f
from tests.test_channel_bridge import C1, C2, packet
from tests.test_network_topology import Rig
from tests.test_network_topology import config as two_network_config

C3 = Channel("test-c", hashlib.sha256(b"disposable-test-c").digest()[:16])


def _map(source, target, source_channel, target_channel):
    return {
        "source_radio": source,
        "target_radio": target,
        "source_channel": source_channel.name,
        "target_channel": target_channel.name,
    }


def config(tmp_path):
    cfg = two_network_config(tmp_path)
    cfg["radios"].append(
        dict(
            id="far",
            network="927",
            role="transceiver",
            radio_type="modem_tcp",
            radio={"frequency": 927875000},
        )
    )
    cfg["fabric"]["topology"]["networks"]["927"] = "far"
    bridge = cfg["channel_bridge"]
    bridge["channels"][C3.name] = C3.key.hex()
    bridge["mappings"] += [
        _map("local", "far", C1, C3),
        _map("far", "local", C3, C1),
        _map("link", "far", C2, C3),
        _map("far", "link", C3, C2),
    ]
    return cfg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rid,channel,others",
    [
        ("local", C1, {"link", "far"}),
        ("link", C2, {"local", "far"}),
        ("far", C3, {"local", "link"}),
    ],
)
async def test_mapped_channel_reaches_every_other_network_once(tmp_path, rid, channel, others):
    rig = Rig(config(tmp_path))
    try:
        p = packet(channel)
        p._rx_radio_id = rid
        assert await rig.handler(p, f._rx(rid))
        assert rig.air.order[0] == rid
        assert sorted(rig.air.order[1:]) == sorted(others)
        assert not await rig.handler(copy.deepcopy(p), f._rx(rid))
        assert len(rig.air.frames) == 3
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_unmapped_channel_stays_on_its_network(tmp_path):
    cfg = config(tmp_path)
    # Without a link -> far mapping, link traffic on C2 crosses to local only.
    cfg["channel_bridge"]["mappings"] = [
        m
        for m in cfg["channel_bridge"]["mappings"]
        if not (m["source_radio"] == "link" and m["target_radio"] == "far")
    ]
    rig = Rig(cfg)
    try:
        p = packet(C2)
        p._rx_radio_id = "link"
        assert await rig.handler(p, f._rx("link"))
        assert rig.air.order == ["link", "local"]
    finally:
        rig.close()


def test_traffic_guard_tracks_every_direction(tmp_path):
    rig = Rig(config(tmp_path))
    try:
        guard = rig.handler.bridge_traffic
        assert guard.max_directions == 6
        ids = ("local", "link", "far")
        for source in ids:
            for target in ids:
                if source != target:
                    assert guard.admit(source, target, is_flood=False) is None
        assert guard.admit("x", "y", is_flood=False) == "Bridge direction unavailable"
    finally:
        rig.close()


def test_two_radio_guard_default_is_unchanged():
    guard = DirectionalTrafficGuard()
    assert guard.admit("a", "b", is_flood=False) is None
    assert guard.admit("b", "a", is_flood=False) is None
    assert guard.admit("a", "c", is_flood=False) == "Bridge direction unavailable"


def test_settings_accept_three_radios(tmp_path):
    settings = BridgeSettings(config(tmp_path))
    assert settings.radio_ids == ("local", "link", "far")


def test_networks_must_not_share_a_frequency(tmp_path):
    cfg = config(tmp_path)
    cfg["radios"][-1]["radio"]["frequency"] = 909875000
    with pytest.raises(ValueError, match="own frequency"):
        topology_from_config(cfg)


class _Hash:
    def __init__(self, value):
        self.value = value

    def calculate_packet_hash(self):
        return self.value


def test_ledger_lets_one_source_cross_to_each_network_once(tmp_path):
    ledger = EchoLedger(tmp_path / "echo.sqlite")
    try:
        source, to_b, to_c = _Hash(b"src"), _Hash(b"to-b"), _Hash(b"to-c")
        first = ledger.reserve(source, to_b)
        assert first is not None
        assert ledger.reserve(source, to_c) is not None
        # The same crossing twice, or a copy already sent elsewhere, is refused.
        assert ledger.reserve(source, to_b) is None
        assert ledger.reserve(_Hash(b"other"), to_c) is None
        # A packet we sent (an outgoing copy) heard back never crosses again.
        assert ledger.reserve(to_b, _Hash(b"bounce")) is None
        # Releasing a failed crossing frees only that crossing.
        ledger.finish(first, success=False)
        assert ledger.reserve(source, to_b) is not None
    finally:
        ledger.close()


def test_ledger_legacy_rows_without_roles_still_block(tmp_path):
    ledger = EchoLedger(tmp_path / "echo.sqlite")
    try:
        assert ledger.reserve(_Hash(b"src"), _Hash(b"to-b")) is not None
        ledger.db.execute("DELETE FROM echo_roles")
        ledger.db.commit()
        assert ledger.reserve(_Hash(b"src"), _Hash(b"to-c")) is None
    finally:
        ledger.close()
