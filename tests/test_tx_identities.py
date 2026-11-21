import pytest
from types import SimpleNamespace
from repeater.tx_identities import TxIdentities
from repeater.bridge_protocols import crossing_route_identity
from openhop_core.protocol.packet import Packet
from openhop_core.protocol.packet_utils import PathUtils
from openhop_core.protocol.constants import ROUTE_TYPE_FLOOD, ROUTE_TYPE_DIRECT, PAYLOAD_TYPE_TRACE
from nacl.signing import SigningKey


def identities(enabled=True):
    native = bytes.fromhex("bd6e" + "11" * 30)
    cross = crossing_route_identity(native)
    used = {native[0], cross[0], 0, 255}
    keys = {}
    for rid in ["directional", "rak"]:
        while True:
            key = SigningKey.generate().verify_key.encode()
            c = crossing_route_identity(key)
            if key[0] != c[0] and not {key[0], c[0]} & used:
                break
        used.update({key[0], c[0]})
        keys[rid] = key.hex()
    return TxIdentities(
        {"tx_identity_experiment": {"enabled": enabled, "radios": keys}}, native, cross
    )


def packet(width, tail, direct=False):
    p = Packet()
    p.header = ROUTE_TYPE_DIRECT if direct else ROUTE_TYPE_FLOOD
    p.payload = bytearray(b"unchanged ciphertext")
    p.payload_len = len(p.payload)
    p.path = bytearray(b"\x77" * width + tail[:width])
    p.path_len = PathUtils.encode_path_len(width, 2)
    return p


@pytest.mark.parametrize("width", [2, 3])
@pytest.mark.parametrize("crossing", [False, True])
def test_tag_and_route(width, crossing):
    ids = identities()
    canonical = ids.crossing if crossing else ids.native
    p = packet(width, canonical)
    h = p.calculate_packet_hash()
    payload = bytes(p.payload)
    assert ids.tag(p, "rak")
    assert p.calculate_packet_hash() == h
    assert bytes(p.payload) == payload and p.get_path_hash_count() == 2
    p.header = ROUTE_TYPE_DIRECT
    routed = ids.canonical_direct(p)
    assert routed.path[-width:] == canonical[:width]
    assert p.path[-width:] != canonical[:width]
    assert routed is not p


def test_disabled_accepts_old_routes_but_no_new_tags():
    ids = identities()
    p = packet(2, ids.native)
    ids.tag(p, "rak")
    p.header = ROUTE_TYPE_DIRECT
    ids.enabled = False
    assert ids.canonical_direct(p).path[-2:] == ids.native[:2]
    p = packet(2, ids.native)
    raw = p.write_to()
    assert not ids.tag(p, "rak")
    assert p.write_to() == raw


def test_trace_unknown_and_origin_unchanged():
    ids = identities()
    p = packet(2, ids.native)
    p.header = ROUTE_TYPE_FLOOD | PAYLOAD_TYPE_TRACE << 2
    raw = p.write_to()
    assert not ids.tag(p, "rak")
    assert p.write_to() == raw
    p = packet(2, b"\x99" * 32)
    raw = p.write_to()
    assert not ids.tag(p, "rak")
    assert p.write_to() == raw
    p = packet(2, ids.native)
    p.path = bytearray()
    p.path_len = 0
    assert not ids.tag(p, "rak")


@pytest.mark.asyncio
async def test_real_sequential_scheduler_tags_both_legs():
    from tests.test_ordered_tx_integration import rig
    from openhop_core.protocol.packet import Packet

    r, rak = rig(("link", "rak"))
    ids = identities()
    ids.radios["link"] = ids.radios.pop("directional")
    r.handler.tx_identities = ids
    p = packet(2, ids.native)
    before = p.write_to()
    task = await r.handler.schedule_retransmit(p, 0, 0, preferred_tx_radio_id="link")
    assert await task
    frames = [Packet() for _ in r.air.frames]
    for pkt, (_, raw) in zip(frames, r.air.frames):
        pkt.read_from(raw)
    assert len(frames) == 2
    assert frames[0].path != frames[1].path
    assert frames[0].calculate_packet_hash() == frames[1].calculate_packet_hash()
    assert bytes(frames[0].payload) == bytes(frames[1].payload)
    assert r.air.order == ["link", "rak"] and not r.air.overlapped
    assert p.write_to() == before


@pytest.mark.parametrize(
    "section",
    [
        {"enabled": True, "radios": {"rak": "zz"}},
        {"enabled": True, "radios": {"rak": "00" * 31}},
        {"enabled": "yes", "radios": {}},
        {"enabled": True, "radios": ["rak"]},
        "on",
    ],
)
def test_invalid_config_disables_experiment_without_raising(section, caplog):
    ids = TxIdentities({"tx_identity_experiment": section}, bytes.fromhex("bd6e" + "11" * 30))
    assert not ids.enabled and not ids.aliases
    assert "TX identity experiment disabled" in caplog.text


def test_unknown_radio_is_warned_and_skipped(caplog):
    good = identities()
    keys = {rid: key.hex() for rid, (key, _) in good.radios.items()}
    keys["typo"] = keys.pop("rak")
    ids = TxIdentities(
        {"tx_identity_experiment": {"enabled": True, "radios": keys}},
        good.native,
        good.crossing,
        radio_ids=["directional", "rak"],
    )
    assert ids.enabled and set(ids.radios) == {"directional"}
    assert "unknown radio 'typo'" in caplog.text


@pytest.mark.parametrize("crossing", [False, True])
def test_one_byte_paths_are_never_tagged_or_rewritten(crossing):
    ids = identities()
    canonical = ids.crossing if crossing else ids.native
    p = packet(1, canonical)
    raw = p.write_to()
    assert not ids.tag(p, "rak") and p.write_to() == raw
    # A different node whose single byte equals one of our aliases keeps its hop.
    alias = next(iter(ids.aliases))
    p = packet(1, alias, direct=True)
    assert ids.canonical_direct(p) is p
    assert ids.prefixes(1) == set()


def pair_identities(enabled=True, radio_ids=("directional", "rak", "bay"), **extra):
    native = bytes.fromhex("bd6e" + "11" * 30)
    section = {"enabled": enabled, "mode": "rx_tx", **extra}
    return TxIdentities(
        {"tx_identity_experiment": section},
        native,
        crossing_route_identity(native),
        radio_ids=list(radio_ids),
    )


def test_pair_aliases_are_unique_stable_and_tabled():
    ids = pair_identities()
    assert len(ids.pairs) == 9
    heads = [key[:2] for key in ids.aliases] + [ids.native[:2], ids.crossing[:2]]
    assert len(heads) == len(set(heads))
    assert pair_identities().pairs == ids.pairs  # Same node, same radios: same aliases.
    rows = {(r["rx"], r["tx"]): r["prefix"] for r in ids.table()}
    assert rows[("bay", "rak")] == ids.pairs[("bay", "rak")][0][:2].hex()


@pytest.mark.parametrize("crossing", [False, True])
def test_tag_names_receiving_and_transmitting_antenna(crossing):
    ids = pair_identities()
    canonical = ids.crossing if crossing else ids.native
    seen = set()
    for rx in ("directional", "bay"):
        for tx in ("directional", "rak"):
            p = packet(2, canonical)
            p._rx_radio_id = rx
            assert ids.tag(p, tx)
            key, cross = ids.pairs[(rx, tx)]
            assert bytes(p.path[-2:]) == (cross if crossing else key)[:2]
            seen.add(bytes(p.path[-2:]))
            p.header = ROUTE_TYPE_DIRECT
            assert bytes(ids.canonical_direct(p).path[-2:]) == canonical[:2]
    assert len(seen) == 4


def test_retag_of_tagged_copy_names_its_own_transmitter():
    ids = pair_identities()
    p = packet(2, ids.native)
    p._rx_radio_id = "bay"
    assert ids.tag(p, "directional") and ids.tag(p, "rak")
    assert bytes(p.path[-2:]) == ids.pairs[("bay", "rak")][0][:2]


def test_legacy_tx_aliases_still_recognised_in_pair_mode():
    legacy = identities()
    keys = {rid: key.hex() for rid, (key, _) in legacy.radios.items()}
    ids = TxIdentities(
        {"tx_identity_experiment": {"enabled": True, "radios": keys}},
        legacy.native,
        legacy.crossing,
        radio_ids=["directional", "rak"],
    )
    assert ids.mode == "rx_tx" and ids.pairs
    old_route = packet(2, legacy.radios["rak"][0], direct=True)
    assert bytes(ids.canonical_direct(old_route).path[-2:]) == legacy.native[:2]
    # Without a known receiving radio, the per-transmitter alias is used.
    p = packet(2, ids.native)
    assert ids.tag(p, "rak") and bytes(p.path[-2:]) == legacy.radios["rak"][0][:2]


def test_no_experiment_section_means_no_aliases():
    ids = TxIdentities({}, bytes.fromhex("bd6e" + "11" * 30), radio_ids=["a", "b"])
    assert not ids.enabled and not ids.aliases and ids.table() == []


@pytest.mark.asyncio
async def test_single_transmitter_send_is_tagged_with_its_pair():
    from tests.test_ordered_tx_integration import rig

    r, _ = rig(("link", "rak"))
    ids = pair_identities(radio_ids=("local", "link", "rak"))
    r.handler.tx_identities = ids
    p = packet(2, ids.native)
    p._rx_radio_id = "link"
    task = await r.handler.schedule_retransmit(p, 0, 0, preferred_tx_radio_id="local")
    assert await task
    sent = Packet()
    sent.read_from(r.air.frames[-1][1])
    assert bytes(sent.path[-2:]) == ids.pairs[("link", "local")][0][:2]


def test_status_labels_name_every_alias_key():
    ids = pair_identities(radio_ids=("g3910", "rak910"))
    rows = ids.status_labels()
    by_key = {bytes.fromhex(r["key"]): r["label"] for r in rows}
    # Every recognised alias (plain and crossing) is labelled exactly once.
    assert set(by_key) == set(ids.aliases)
    key, cross = ids.pairs[("g3910", "rak910")]
    assert by_key[key] == "BD6E g3910→rak910"
    crossing_head = ids.crossing[:2].hex().upper()
    assert by_key[cross] == f"{crossing_head} g3910→rak910 (bridged)"


def test_status_labels_include_legacy_tx_aliases():
    ids = identities()
    labels = {r["label"] for r in ids.status_labels()}
    assert "BD6E tx directional" in labels and "BD6E tx rak" in labels


ONE_BYTE = {"directional": ["d4", "14"], "rak": [12, "d3"]}


def one_byte_identities(one_byte=ONE_BYTE, **extra):
    native = bytes.fromhex("bd6e" + "11" * 30)
    section = {"enabled": True, "mode": "tx", "one_byte": one_byte, **extra}
    return TxIdentities(
        {"tx_identity_experiment": section},
        native,
        crossing_route_identity(native),
        radio_ids=["directional", "rak", "bay"],
    )


@pytest.mark.parametrize(
    "crossing,radio,code",
    [
        (False, "directional", "d4"),
        (True, "directional", "14"),
        (False, "rak", "12"),
        (True, "rak", "d3"),
    ],
)
def test_one_byte_codes_tag_and_route_back(crossing, radio, code):
    ids = one_byte_identities()
    assert ids.enabled
    canonical = ids.crossing if crossing else ids.native
    p = packet(1, canonical)
    h = p.calculate_packet_hash()
    assert ids.tag(p, radio)
    assert p.path.hex() == "77" + code and p.calculate_packet_hash() == h
    # Re-tagging a copy made from a tagged packet names the new transmitter.
    other = "rak" if radio == "directional" else "directional"
    assert ids.tag(p, other) and p.path.hex() != "77" + code
    p.header = ROUTE_TYPE_DIRECT
    p.path[-1:] = bytes.fromhex(code)
    assert ids.canonical_direct(p).path[-1:] == canonical[:1]
    assert bytes.fromhex(code) in ids.prefixes(1, crossing_only=crossing)


def test_radio_without_one_byte_code_leaves_one_byte_paths_alone():
    ids = one_byte_identities()
    p = packet(1, ids.native)
    raw = p.write_to()
    assert not ids.tag(p, "bay") and p.write_to() == raw
    # 2-byte paths are not tagged in tx mode without per-radio keys either.
    assert not ids.tag(packet(2, ids.native), "directional")


def test_single_code_tags_native_hops_only():
    ids = one_byte_identities({"directional": ["d4"]})
    assert ids.tag(packet(1, ids.native), "directional")
    p = packet(1, ids.crossing)
    raw = p.write_to()
    assert not ids.tag(p, "directional") and p.write_to() == raw
    assert ids.prefixes(1, crossing_only=True) == set()


def test_relay_advert_retires_its_byte_and_restores_our_hop():
    ids = one_byte_identities()
    p = packet(1, ids.native)
    assert ids.tag(p, "directional") and p.path[-1:] == b"\xd4"
    assert not ids.note_advert("d4" + "00" * 31, 1, "a companion")  # chat nodes don't relay
    assert ids.note_advert("d4" + "00" * 31, 2, "New Repeater")
    assert not ids.note_advert("d4" + "11" * 31, 3, "same byte again")
    assert ids.retired.keys() == {b"\xd4"}
    # A packet already carrying the retired code goes out with our real hop.
    assert not ids.tag(p, "directional") and p.path[-1:] == ids.native[:1]
    fresh = packet(1, ids.native)
    assert not ids.tag(fresh, "directional") and fresh.path[-1:] == ids.native[:1]
    d = packet(1, b"\xd4", direct=True)
    assert ids.canonical_direct(d) is d
    assert b"\xd4" not in ids.prefixes(1)
    # Other codes keep working.
    assert ids.tag(packet(1, ids.crossing), "directional")
    row = next(r for r in ids.table() if r["tx"] == "directional" and r.get("width") == 1)
    assert row["retired"] == ["d4"]


@pytest.mark.parametrize(
    "one_byte",
    [
        {"directional": ["bd", "14"]},  # our own native byte
        {"directional": ["00", "14"]},
        {"directional": ["d4", "d4"]},
        {"directional": ["d4", "14"], "rak": ["14", "12"]},
        {"directional": ["d4", "14", "12"]},
        {"directional": ["d4d4"]},
        ["d4"],
    ],
)
def test_invalid_one_byte_config_disables_experiment(one_byte, caplog):
    ids = one_byte_identities(one_byte)
    assert not ids.enabled and ids.one_byte == {} and ids.prefixes(1) == set()


def test_one_byte_codes_work_alongside_rx_tx_pair_aliases():
    ids = one_byte_identities(mode="rx_tx")
    for width in (2, 1):
        p = packet(width, ids.native)
        p._rx_radio_id = "bay"
        assert ids.tag(p, "directional")
    assert p.path[-1:] == b"\xd4"


def advert_raw(pubkey_hex, kind, hops):
    """A flood advert as stored in packets.raw_packet (1-byte hops)."""
    payload = bytes.fromhex(pubkey_hex) + b"\x00" * 68 + bytes([0x80 | kind]) + b"name"
    return (bytes([0x11, hops]) + bytes(range(1, hops + 1)) + payload).hex()


def test_far_relays_do_not_retire_codes():
    ids = one_byte_identities()
    assert not ids.note_advert("d4" + "00" * 31, 2, "far away", hops=5)
    assert ids.note_advert("d4" + "00" * 31, 2, "neighbour of a neighbour", hops=2)
    assert ids.retired.keys() == {b"\xd4"}


def test_stored_relay_adverts_retire_codes_at_startup(tmp_path):
    import time
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler
    from repeater.handler_helpers.advert import AdvertHelper

    db = SQLiteHandler(tmp_path)
    now = time.time()
    adverts = [
        ("12" + "00" * 31, 2, 0, now - 3600),  # repeater in range: retires 12
        ("d3" + "00" * 31, 3, 7, now - 3600),  # room far away
        ("d3" + "00" * 31, 3, 1, now - 3600),  # ...also heard nearby: retires d3
        ("d4" + "00" * 31, 1, 0, now - 3600),  # companion: never relays
        ("14" + "00" * 31, 2, 0, now - 90 * 86400),  # too old
        ("14" + "11" * 31, 2, 6, now - 3600),  # too far
    ]
    for key, kind, hops, ts in adverts:
        db.store_packet(
            {
                "timestamp": ts,
                "type": 4,
                "route": 1,
                "length": 120,
                "transmitted": False,
                "is_duplicate": False,
                "raw_packet": advert_raw(key, kind, hops),
            }
        )
    found = db.get_relay_advert_hops(now - 30 * 86400)
    assert found["d3" + "00" * 31] == (3, 1)
    ids = one_byte_identities()
    storage = SimpleNamespace(sqlite_handler=db, get_neighbors=lambda: {})
    helper = AdvertHelper(None, storage, tx_identities=ids)
    assert helper.retire_codes_from_stored_adverts() == 2
    assert ids.retired.keys() == {b"\x12", b"\xd3"}
