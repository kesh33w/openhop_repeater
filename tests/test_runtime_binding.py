from types import SimpleNamespace
import pytest
from openhop_core.companion.channel_store import ChannelStore
from openhop_core.companion.models import Channel
from openhop_core.rf_fabric.fabric import RFFabric
from openhop_core.rf_fabric.topology import NetworkTopology
from repeater.bot_rf_receipts import TEST_KEY, runtime_binding, start_for_daemon


class Radio:
    frequency = 909875000
    bandwidth = 62500
    spreading_factor = 9
    coding_rate = 5
    rx_receipt_available = True
    rx_receipt_generation = 0

    def set_rx_callback(self, callback):
        self.rx_callback = callback

    async def rx_receipt_barrier(self):
        return self.rx_receipt_generation


def topology(extra=False):
    entries = [
        dict(id="dir909", network="smf", role="transceiver"),
        dict(id="v3909", network="smf", role="receiver"),
        dict(id="dir910", network="sfo", role="transceiver"),
    ]
    if extra:
        entries.append(dict(id="extra909", network="smf", role="receiver"))
    return NetworkTopology(
        entries,
        dict(networks={"smf": "dir909", "sfo": "dir910"}, enable_receiver_forwarding=True),
        "dir909",
    )


@pytest.fixture
def setup(tmp_path):
    fabric = RFFabric()
    for rid in ["dir909", "v3909", "dir910"]:
        fabric.register_radio(Radio(), radio_id=rid)
    fabric.configure_topology(topology())
    fabric.arm()
    channels = ChannelStore()
    channels.set(1, Channel("#test", TEST_KEY + bytes(16)))
    bridge = SimpleNamespace(
        channels=channels,
        get_public_key=lambda: b"a" * 32,
        _receive_network="smf",
        _receive_fabric=fabric,
        _receive_home_radio_id="dir909",
    )
    secret = tmp_path / "secret"
    secret.write_bytes(b"x" * 32)
    secret.chmod(0o600)
    config = dict(
        enabled=True,
        socket=str(tmp_path / "proof.sock"),
        secret_file=str(secret),
        companion_key=(b"a" * 32).hex(),
        companion_endpoint=["127.0.0.1", 5000],
        receivers=["dir909", "v3909"],
        tx_radio="dir909",
    )
    daemon = SimpleNamespace(
        config={"bot_rf_receipts": config},
        radio=SimpleNamespace(fabric=fabric),
        companion_frame_servers=[SimpleNamespace(bridge=bridge, port=5000)],
    )
    return daemon, config, fabric, bridge


def test_actual_channel_and_logical_membership_binding(setup):
    daemon, config, fabric, bridge = setup
    value = runtime_binding(daemon, config)
    assert value["receivers"] == ["dir909", "v3909"] and value["frequency_hz"] == 909875000


@pytest.mark.parametrize(
    "mutation",
    ["frequency", "bandwidth", "key", "port", "home", "callback", "third", "optional", "unknown"],
)
def test_runtime_drift_fails_closed(setup, mutation):
    daemon, config, fabric, bridge = setup
    if mutation == "frequency":
        fabric.get_radio("v3909").frequency = 910525000
    elif mutation == "bandwidth":
        fabric.get_radio("v3909").bandwidth = 125000
    elif mutation == "key":
        bridge.channels.set(1, Channel("#test", b"z" * 16))
    elif mutation == "port":
        daemon.companion_frame_servers[0].port = 5001
    elif mutation == "home":
        bridge._receive_home_radio_id = "dir910"
    elif mutation == "callback":
        fabric.get_radio("v3909").rx_callback = lambda *_: None
    elif mutation == "third":
        fabric.register_radio(Radio(), radio_id="extra909")
        fabric.configure_topology(topology(True))
    elif mutation == "optional":
        config["socket_group_access"] = "true"
    elif mutation == "unknown":
        config["typo"] = True
    with pytest.raises(ValueError):
        runtime_binding(daemon, config)


@pytest.mark.asyncio
async def test_enabled_startup_and_dynamic_channel_revalidation(setup):
    daemon, config, fabric, bridge = setup
    server = await start_for_daemon(daemon)
    try:
        assert server.observe in fabric._observation_callbacks
        assert server.binding_provider() == server.binding
        bridge.channels.set(1, Channel("#test", b"z" * 16))
        with pytest.raises(ValueError):
            server.binding_provider()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_disabled_never_opens_socket(setup):
    daemon, config, fabric, bridge = setup
    config["enabled"] = False
    assert await start_for_daemon(daemon) is None


@pytest.mark.asyncio
async def test_startup_optional_group_and_configuration_change(setup):
    daemon, config, fabric, bridge = setup
    from pathlib import Path

    Path(config["socket"]).parent.chmod(0o710)
    config["socket_group_access"] = True
    server = await start_for_daemon(daemon)
    try:
        assert Path(config["socket"]).stat().st_mode & 0o777 == 0o660
        config["companion_endpoint"] = ["other-host", 5000]
        with pytest.raises(ValueError):
            server.binding_provider()
    finally:
        await server.close()


@pytest.mark.parametrize("suffix", [b"", bytes(16), b"\x01" + bytes(15), bytes(15), bytes(17)])
def test_channel_key_representation(setup, suffix):
    daemon, config, _, bridge = setup
    bridge.channels.set(1, Channel("#test", TEST_KEY + suffix))
    if suffix in (b"", bytes(16)):
        assert runtime_binding(daemon, config)["frequency_hz"] == 909875000
    else:
        with pytest.raises(ValueError, match="key differs"):
            runtime_binding(daemon, config)
