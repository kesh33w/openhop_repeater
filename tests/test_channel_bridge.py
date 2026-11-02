import hashlib

import pytest
from openhop_core.protocol.constants import PAYLOAD_TYPE_TXT_MSG, PH_TYPE_SHIFT
from openhop_core.protocol.crypto import CryptoUtils
from openhop_core.protocol.packet_builder import PacketBuilder

from repeater.channel_bridge import Channel, ChannelBridge, Crossing, Destination, EchoLedger

A = Destination("radio", "a")
B = Destination("radio", "b")
C1 = Channel("test-a", hashlib.sha256(b"disposable-test-a").digest()[:16])
C2 = Channel("test-b", hashlib.sha256(b"disposable-test-b").digest()[:16])


def packet(channel=C1, message="bench only"):
    p = PacketBuilder.create_group_datagram(
        channel.name,
        None,
        message,
        "Test sender",
        [{"name": channel.name, "secret": channel.key}],
        timestamp=123456,
    )
    p.path = bytearray([0x11, 0x22])
    p.path_len = 2
    return p


def bridge(channels=(C1, C2)):
    return ChannelBridge(
        channels, [Crossing(A, B, C1.name, C2.name), Crossing(B, A, C2.name, C1.name)]
    )


@pytest.mark.parametrize("home,source,target", [(A, C1, C2), (B, C2, C1)])
def test_bidirectional_copies_preserve_plaintext_path_and_source(home, source, target):
    p = packet(source)
    original = p.write_to()
    sends = bridge().plan(p, home)
    assert len(sends) == 2
    assert sends[0].packet.write_to() == original == p.write_to()
    out = sends[1].packet
    assert out.path == p.path and out.header == p.header
    assert bytes(out.payload) != bytes(p.payload)
    assert CryptoUtils.mac_then_decrypt(
        target.secret[:16], target.secret, bytes(out.payload[1:])
    ) == CryptoUtils.mac_then_decrypt(source.secret[:16], source.secret, bytes(p.payload[1:]))


def test_crossing_denied_does_not_disable_repeat():
    assert [s.destination for s in bridge().plan(packet(), A, allow_crossing=False)] == [A]
    assert [s.destination for s in bridge().plan(packet(), A, repeat_local=False)] == [B]


def test_origin_needs_home_not_zero_hops():
    p = packet()
    p.path, p.path_len = bytearray(), 0
    assert bridge().plan(p, None) == ()
    assert len(bridge().plan(p, A)) == 2


def test_same_key_passthrough_with_one_named_channel():
    b = ChannelBridge([C1], [Crossing(A, B, C1.name, C1.name)])
    p = packet()
    assert b.plan(p, A)[1].packet.write_to() == p.write_to()


def test_ambiguous_key_aliases_fail_closed():
    b = bridge((C1, C2, Channel("alias", C1.key)))
    assert len(b.plan(packet(), A)) == 1


def test_selector_collision_does_not_authenticate_wrong_key():
    wrong = next(
        Channel("collision", i.to_bytes(16, "little"))
        for i in range(10000)
        if hashlib.sha256(i.to_bytes(16, "little")).digest()[0] == C1.selector
        and i.to_bytes(16, "little") != C1.key
    )
    b = ChannelBridge([wrong, C2], [Crossing(A, B, wrong.name, C2.name)])
    assert len(b.plan(packet(), A)) == 1


@pytest.mark.parametrize("mutation", ["mac", "alignment", "subtype", "dm", "utf8"])
def test_unsupported_or_invalid_crossing_fails_closed(mutation):
    p = packet()
    if mutation == "mac":
        p.payload[1] ^= 1
    elif mutation == "alignment":
        p.payload = p.payload[:-1]
    elif mutation == "dm":
        p.header = 1 | PAYLOAD_TYPE_TXT_MSG << PH_TYPE_SHIFT
    else:
        plain = bytearray(
            CryptoUtils.mac_then_decrypt(C1.secret[:16], C1.secret, bytes(p.payload[1:]))
        )
        if mutation == "subtype":
            plain[4] = 2
        else:
            plain[6] = 255
        p.payload = bytearray([C1.selector]) + CryptoUtils.encrypt_then_mac(
            C1.secret[:16], C1.secret, bytes(plain)
        )
    assert len(bridge().plan(p, A)) == 1


def test_ledger_failure_restart_echo_and_expiry(tmp_path):
    now = [1000]
    path = tmp_path / "echo.sqlite"
    p = packet()
    out = bridge().plan(p, A)[1].packet
    ledger = EchoLedger(path, ttl=10, capacity=2, clock=lambda: now[0])
    token = ledger.reserve(p, out)
    assert token and ledger.contains(p) and ledger.contains(out)
    assert ledger.reserve(p, out) is None
    assert ledger.reserve(packet(message="other"), packet(C2, "other")) is None
    ledger.finish(token, success=False)
    assert not ledger.contains(out)
    token = ledger.reserve(p, out)
    ledger.close()
    ledger = EchoLedger(path, ttl=10, capacity=2, clock=lambda: now[0])
    assert ledger.contains(out)  # pending TX has uncertain outcome after restart
    ledger.finish(token, success=True)
    ledger.finish(token, success=False)  # cannot erase confirmed send
    assert ledger.contains(out)
    now[0] += 11
    assert not ledger.contains(out)
    assert ledger.reserve(p, out)
    ledger.close()


def test_invalid_destinations_and_rules():
    with pytest.raises(ValueError):
        Destination("internet", "island")
    with pytest.raises(ValueError):
        ChannelBridge([C1], [Crossing(A, B, C1.name, "missing")])
