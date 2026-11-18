from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openhop_core.hardware.tcp_radio import TCPLoRaRadio
from openhop_core.hardware.usb_radio import USBLoRaRadio
from repeater.data_acquisition.storage_collector import StorageCollector


def collector(radios):
    storage = StorageCollector.__new__(StorageCollector)
    fabric = SimpleNamespace(get_radio=lambda rid: radios[rid])
    storage.repeater_handler = SimpleNamespace(
        _fabric_endpoints=lambda: (fabric, list(radios)), rx_count=999, forwarded_count=888)
    return storage


@pytest.mark.parametrize("driver", [USBLoRaRadio, TCPLoRaRadio])
def test_cached_modem_counters_are_attributed_to_physical_radio(driver):
    left = Mock(spec=driver)
    right = Mock(spec=driver)
    left.get_status.return_value = {"rx_count": 12, "tx_count": 7}
    right.get_status.return_value = {"rx_count": 4, "tx_count": 0}
    assert collector({"left": left, "right": right})._get_radio_stats() == {
        "left": {"packets_received": 12, "packets_sent": 7},
        "right": {"packets_received": 4, "packets_sent": 0},
    }


def test_other_drivers_never_query_hardware_or_use_router_totals():
    radio = SimpleNamespace(get_status=Mock(side_effect=AssertionError("hardware query")))
    assert collector({"left": radio})._get_radio_stats() == {}
    radio.get_status.assert_not_called()


@pytest.mark.parametrize("value", [None, True, -1, "5", 5.5])
def test_unavailable_or_invalid_counts_are_omitted(value):
    radio = Mock(spec=USBLoRaRadio)
    radio.get_status.return_value = {"rx_count": value}
    assert collector({"left": radio})._get_radio_stats() == {}


def test_one_failed_endpoint_does_not_hide_other_counters():
    left = Mock(spec=USBLoRaRadio)
    left.get_status.side_effect = RuntimeError("disconnected")
    right = Mock(spec=TCPLoRaRadio)
    right.get_status.return_value = {"tx_count": 5}
    assert collector({"left": left, "right": right})._get_radio_stats() == {
        "right": {"packets_sent": 5}}


def test_missing_hardware_extra_does_not_break_packet_counters(monkeypatch):
    import builtins
    from types import SimpleNamespace

    from repeater.data_acquisition.storage_collector import StorageCollector

    real_import = builtins.__import__

    def no_hardware(name, *args, **kwargs):
        if name.startswith("openhop_core.hardware"):
            raise ImportError("hardware extra not installed")
        return real_import(name, *args, **kwargs)

    collector = StorageCollector.__new__(StorageCollector)
    collector.repeater_handler = SimpleNamespace(_fabric_endpoints=lambda: (None, ["a"]))
    monkeypatch.setattr(builtins, "__import__", no_hardware)
    collector._apply_radio_packet_counters(lambda rid: {})  # must not raise
