"""Resolve USB identities without opening any real serial device."""

from types import SimpleNamespace

import pytest

from openhop_core.hardware import usb_radio


PATH909 = "/dev/serial/by-path/platform-test-usb-0:1.4:1.0-port0"
PATH910 = "/dev/serial/by-path/platform-test-usb-0:1.2:1.0-port0"


def port(device, serial="0001"):
    return SimpleNamespace(device=device, vid=0x10C4, pid=0xEA60, serial_number=serial)


def driver(path):
    radio = usb_radio.USBLoRaRadio.__new__(usb_radio.USBLoRaRadio)
    radio.port = path
    radio.baudrate = 921600
    radio._port_identity = None
    return radio


@pytest.fixture
def serial_inventory(monkeypatch):
    links = {PATH909: "/dev/ttyUSB1", PATH910: "/dev/ttyUSB0"}
    ports = [port("/dev/ttyUSB0"), port("/dev/ttyUSB1")]
    attempts = []

    class FakeSerial:
        def open(self):
            attempts.append(self.port)
            self.device = links.get(self.port, self.port)
            if self.device not in {p.device for p in ports}:
                raise usb_radio.serial.SerialException("configured port unavailable")

    monkeypatch.setattr(usb_radio.serial, "Serial", FakeSerial)
    monkeypatch.setattr(usb_radio.list_ports, "comports", lambda: list(ports))
    monkeypatch.setattr(usb_radio.os.path, "realpath", lambda value: links.get(value, value))
    return links, ports, attempts


def test_duplicate_serials_remain_on_configured_paths_when_enumeration_reverses(serial_inventory):
    _, ports, attempts = serial_inventory
    radios = [driver(PATH909), driver(PATH910)]
    assert [r._open_serial_sync().device for r in radios] == ["/dev/ttyUSB1", "/dev/ttyUSB0"]
    assert all(r._port_identity == (0x10C4, 0xEA60, "0001") for r in radios)
    ports.reverse()
    assert [r._open_serial_sync().device for r in radios] == ["/dev/ttyUSB1", "/dev/ttyUSB0"]
    assert attempts == [PATH909, PATH910, PATH909, PATH910]


@pytest.mark.parametrize("path,other", [(PATH909, PATH910), (PATH910, PATH909)])
def test_unplug_fails_closed_and_replug_follows_same_path(serial_inventory, path, other):
    links, ports, attempts = serial_inventory
    radio = driver(path)
    old_device = radio._open_serial_sync().device
    del links[path]
    ports[:] = [p for p in ports if p.device != old_device]
    with pytest.raises(usb_radio.serial.SerialException, match="unavailable"):
        radio._open_serial_sync()
    assert attempts[-1] == path
    assert links[other] in {p.device for p in ports}
    links[path] = "/dev/ttyUSB7"
    ports.insert(0, port("/dev/ttyUSB7"))
    assert radio._open_serial_sync().device == "/dev/ttyUSB7"
    assert attempts == [path, path, path]


def test_unique_by_id_rediscovery_behavior_is_unchanged(serial_inventory):
    links, ports, _ = serial_inventory
    path = "/dev/serial/by-id/usb-RAK-unique"
    links[path] = "/dev/ttyACM0"
    ports.append(port("/dev/ttyACM0", "unique-rak"))
    radio = driver(path)
    assert radio._open_serial_sync().device == "/dev/ttyACM0"
    del links[path]
    ports[:] = [p for p in ports if p.serial_number != "unique-rak"]
    ports.append(port("/dev/ttyACM9", "unique-rak"))
    assert radio._resolve_serial_port() == "/dev/ttyACM9"
    assert radio._open_serial_sync().device == "/dev/ttyACM9"
