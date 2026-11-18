from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import pytest
from openhop_core.rf_fabric.logical_receiver import LogicalReceiver
from openhop_core.rf_fabric.fabric import RFFabric
from openhop_core.rf_fabric.topology import NetworkTopology
from tests.test_channel_bridge import packet


def topology():
    entries = [
        ("dir909", "smf909", "transceiver"),
        ("v3909", "smf909", "receiver"),
        ("dir910", "sfo910", "transceiver"),
        ("v3910", "sfo910", "receiver"),
        ("rak", "sfo910", "shadow"),
    ]
    return NetworkTopology(
        [dict(id=r, network=n, role=t) for r, n, t in entries],
        {
            "networks": {"smf909": "dir909", "sfo910": "dir910"},
            "enable_receiver_forwarding": True,
        },
        "dir909",
    )


def raw(value=None):
    p = packet()
    if value is not None:
        p.payload[-1] = value
    return p.write_to()


@pytest.mark.parametrize("first,second", [("dir909", "v3909"), ("v3909", "dir909")])
def test_first_late_copy_and_path_variant(first, second):
    times = iter([10, 11, 12])
    walls = iter([100, 101, 102])
    r = LogicalReceiver(topology(), clock=lambda: next(times), wall_clock=lambda: next(walls))
    initial = []
    updates = []
    r.subscribe("smf909", initial.append)
    r.subscribe("smf909", updates.append, updates=True)
    data = bytearray(raw())
    r.observe(data, -100, 2, rx_radio_id=first)
    original = bytes(data)
    data[-1] ^= 1
    r.observe(original, -80, 5, rx_radio_id=second)
    p = packet()
    p.path.append(0x42)
    p.path_len += 1
    r.observe(p.write_to(), -70, 7, rx_radio_id=first)
    assert len(initial) == 1 and [e.kind for e in updates] == [
        "first",
        "copy",
        "variant",
    ]
    assert len({e.event_id for e in updates}) == 1
    assert all(
        e.first.data == original and e.first.at == 100 and e.first.monotonic_at == 10
        for e in updates
    )
    assert initial[0].first.radio_id == first and initial[0].first.rssi == -100
    assert updates[-1].observation_count == 3 and len(updates[-1].observations) == 3
    assert updates[-1].observations[1].radio_id == second
    assert initial[0].observations == (initial[0].first,)
    with pytest.raises(FrozenInstanceError):
        initial[0].first.rssi = 123


def test_concurrent_ears_and_bounded_history():
    r = LogicalReceiver(topology())
    initial = []
    updates = []
    r.subscribe("smf909", initial.append)
    r.subscribe("smf909", updates.append, updates=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda n: r.observe(raw(), -90, n, rx_radio_id=["dir909", "v3909"][n % 2]),
                range(40),
            )
        )
    assert len(initial) == 1 and len(updates) == 40
    assert [x.observation_count for x in updates] == list(range(1, 41))
    assert r.snapshot()["history_not_retained"] == 8 and len(updates[-1].observations) == 32


def test_network_shadow_membership_isolation():
    r = LogicalReceiver(topology())
    events = []
    for stream in ["smf909", "sfo910", "shadow:rak"]:
        r.subscribe(stream, events.append)
    for rid in ["dir909", "v3909", "dir910", "v3910", "rak"]:
        r.observe(raw(), rx_radio_id=rid)
    assert [e.stream_id for e in events] == ["smf909", "sfo910", "shadow:rak"] and len(
        {e.event_id for e in events}
    ) == 3
    assert (
        r.members("sfo910") == frozenset(["dir910", "v3910"])
        and r.network("shadow:rak") == "sfo910"
    )
    with pytest.raises(ValueError):
        r.members("missing")
    with pytest.raises(ValueError):
        r.stream_for_radio("missing")


def test_echo_never_consumes_real_receive():
    r = LogicalReceiver(topology())
    events = []
    r.subscribe("smf909", events.append)
    assert r.observe(raw(), rx_radio_id="dir909", own_tx_match=True) is None
    assert r.snapshot()["cache_size"] == 0
    r.observe(raw(), -101, 1, rx_radio_id="v3909")
    r.observe(raw(), rx_radio_id="dir909", own_tx_match=True)
    assert (
        len(events) == 1
        and events[0].first.radio_id == "v3909"
        and r.snapshot()["own_tx_suppressed"] == 2
    )


def test_backpressure_retries_first_for_only_refusing_subscriber():
    r = LogicalReceiver(topology())
    offered = []
    accepted = []
    other = []

    def callback(e):
        offered.append(e)
        if len(offered) == 1:
            return False
        accepted.append(e)

    r.subscribe("smf909", callback)
    r.subscribe("smf909", other.append)
    r.observe(raw(), -100, 1, rx_radio_id="v3909")
    r.observe(raw(), -60, 10, rx_radio_id="dir909")
    r.observe(raw(), -50, 12, rx_radio_id="dir909")
    assert len(accepted) == 1 and len(other) == 1 and len(offered) == 2
    assert offered[0].event_id == offered[1].event_id
    assert accepted[0].observation == accepted[0].first == offered[0].first
    assert accepted[0].first.rssi == -100 and r.snapshot()["subscriber_refusals"] == 1


def test_callback_failure_and_no_retroactive_subscription():
    r = LogicalReceiver(topology())
    bad = []
    good = []
    late = []

    def failing(e):
        bad.append(e)
        if len(bad) == 1:
            raise RuntimeError("full")

    r.subscribe("smf909", failing)
    r.subscribe("smf909", good.append)
    r.observe(raw(), rx_radio_id="dir909")
    r.subscribe("smf909", late.append)
    r.observe(raw(), rx_radio_id="v3909")
    assert len(bad) == 2 and len(good) == 1 and not late and r.snapshot()["subscriber_errors"] == 1
    r.observe(raw(123), rx_radio_id="v3909")
    assert len(late) == 1


def test_no_capacity_eviction_and_fixed_expiry():
    now = [0]
    r = LogicalReceiver(topology(), capacity=1, ttl=10, clock=lambda: now[0])
    events = []
    r.subscribe("smf909", events.append)
    r.observe(raw(), rx_radio_id="dir909")
    assert r.observe(raw(123), rx_radio_id="dir909") is None
    now[0] = 9
    r.observe(raw(), rx_radio_id="v3909")
    assert len(events) == 1 and r.snapshot()["capacity_drops"] == 1
    now[0] = 10
    r.observe(raw(), rx_radio_id="dir909")
    assert len(events) == 2 and events[0].event_id != events[1].event_id


def test_invalid_unknown_closed_and_unsubscribe():
    r = LogicalReceiver(topology(), subscriber_capacity=1)
    events = []
    token = r.subscribe("smf909", events.append)
    with pytest.raises(ValueError):
        r.subscribe("sfo910", events.append)
    assert (
        r.observe(b"", rx_radio_id="dir909") is None
        and r.observe(bytes(513), rx_radio_id="dir909") is None
    )
    with pytest.raises(ValueError):
        r.observe(raw(), rx_radio_id="other")
    assert r.snapshot()["cache_size"] == 0
    r.unsubscribe(token)
    r.observe(raw(), rx_radio_id="dir909")
    assert not events
    r.close()
    with pytest.raises(RuntimeError):
        r.subscribe("smf909", events.append)
    assert r.observe(raw(), rx_radio_id="dir909") is None and r.snapshot()["closed_drops"] == 1


def test_fabric_keeps_physical_forwarding_and_shadow_gate():
    f = RFFabric()
    f._radios.update({rid: SimpleNamespace() for rid in topology().members})
    f._default_radio_id = "dir909"
    f.configure_topology(topology())
    logical = []
    physical = []
    observed = []
    primary_token = f.logical_receiver.subscribe("smf909", logical.append)
    shadow_token = f.logical_receiver.subscribe("shadow:rak", logical.append)
    f.set_legacy_rx_callback(
        lambda data, rssi, snr, *, rx_radio_id=None: physical.append(rx_radio_id)
    )
    f.add_observation_callback(lambda data, rssi, snr, **kw: observed.append(kw["rx_radio_id"]))
    for rid in ["v3909", "dir909", "rak"]:
        f._on_radio_rx(rid, raw(), -90, 5)
    assert [e.stream_id for e in logical] == ["smf909", "shadow:rak"]
    assert physical == ["v3909", "dir909"] and observed == ["v3909", "dir909", "rak"]
    old = f.logical_receiver
    old_topology, old_comparison = f.topology, f.comparison
    with pytest.raises(RuntimeError, match="require restart"):
        f.configure_topology(topology())
    assert f.logical_receiver is old and not old.snapshot()["closed"]
    assert f.topology is old_topology and f.comparison is old_comparison
    f._on_radio_rx("dir909", raw(123), -90, 5)
    assert len(logical) == 3
    old.unsubscribe(primary_token)
    old.unsubscribe(shadow_token)
    f.configure_topology(topology())
    assert old.snapshot()["closed"]
    f._on_radio_rx("dir909", raw(), -90, 5)
    assert len(logical) == 3


def test_three_active_ears_share_one_event_without_changing_transmitter():
    radios = [
        dict(id="directional", network="band", role="transceiver"),
        dict(id="omni1", network="band", role="receiver"),
        dict(id="omni2", network="band", role="receiver"),
        dict(id="other", network="otherband", role="transceiver"),
        dict(id="other1", network="otherband", role="receiver"),
        dict(id="other2", network="otherband", role="receiver"),
    ]
    policy = {
        "networks": {"band": "directional", "otherband": "other"},
        "enable_receiver_forwarding": True,
    }
    topo = NetworkTopology(radios, policy, "directional")
    receiver = LogicalReceiver(topo)
    first, updates = [], []
    receiver.subscribe("band", first.append)
    receiver.subscribe("band", updates.append, updates=True)
    for radio_id in ["omni2", "directional", "omni1", "omni2"]:
        receiver.observe(raw(), rx_radio_id=radio_id)
    assert len(first) == 1
    assert [ob.radio_id for ob in updates[-1].observations] == [
        "omni2",
        "directional",
        "omni1",
        "omni2",
    ]
    assert len({event.event_id for event in updates}) == 1
    assert receiver.members("band") == frozenset(["directional", "omni1", "omni2"])
    assert topo.networks["band"] == "directional"

    second_band = []
    receiver.subscribe("otherband", second_band.append, updates=True)
    for radio_id in ["other1", "other2", "other"]:
        receiver.observe(raw(), rx_radio_id=radio_id)
    assert [e.kind for e in second_band] == ["first", "copy", "copy"]
    assert [ob.radio_id for ob in second_band[-1].observations] == [
        "other1",
        "other2",
        "other",
    ]
    assert second_band[0].event_id != first[0].event_id
    assert topo.networks == {"band": "directional", "otherband": "other"}
