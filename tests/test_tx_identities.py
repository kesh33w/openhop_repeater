import pytest
from repeater.tx_identities import TxIdentities
from repeater.bridge_protocols import crossing_route_identity
from openhop_core.protocol.packet import Packet
from openhop_core.protocol.packet_utils import PathUtils
from openhop_core.protocol.constants import ROUTE_TYPE_FLOOD, ROUTE_TYPE_DIRECT, PAYLOAD_TYPE_TRACE
from nacl.signing import SigningKey

def identities(enabled=True):
    native=bytes.fromhex('bd6e'+'11'*30)
    cross=crossing_route_identity(native)
    used={native[0],cross[0],0,255}; keys={}
    for rid in ['directional','rak']:
        while True:
            key=SigningKey.generate().verify_key.encode(); c=crossing_route_identity(key)
            if key[0]!=c[0] and not {key[0],c[0]} & used: break
        used.update({key[0],c[0]});keys[rid]=key.hex()
    return TxIdentities({'tx_identity_experiment':{'enabled':enabled,'radios':keys}},native,cross)

def packet(width, tail, direct=False):
    p=Packet();p.header=ROUTE_TYPE_DIRECT if direct else ROUTE_TYPE_FLOOD
    p.payload=bytearray(b'unchanged ciphertext');p.payload_len=len(p.payload)
    p.path=bytearray(b'\x77'*width+tail[:width]);p.path_len=PathUtils.encode_path_len(width,2)
    return p

@pytest.mark.parametrize('width',[2,3])
@pytest.mark.parametrize('crossing',[False,True])
def test_tag_and_route(width,crossing):
    ids=identities();canonical=ids.crossing if crossing else ids.native
    p=packet(width,canonical);original=p.write_to();h=p.calculate_packet_hash();payload=bytes(p.payload)
    assert ids.tag(p,'rak');assert p.calculate_packet_hash()==h
    assert bytes(p.payload)==payload and p.get_path_hash_count()==2
    p.header=ROUTE_TYPE_DIRECT
    routed=ids.canonical_direct(p)
    assert routed.path[-width:]==canonical[:width]
    assert p.path[-width:]!=canonical[:width]
    assert routed is not p


def test_disabled_accepts_old_routes_but_no_new_tags():
    ids=identities();p=packet(2,ids.native);ids.tag(p,'rak');p.header=ROUTE_TYPE_DIRECT
    ids.enabled=False
    assert ids.canonical_direct(p).path[-2:]==ids.native[:2]
    p=packet(2,ids.native);raw=p.write_to();assert not ids.tag(p,'rak');assert p.write_to()==raw


def test_trace_unknown_and_origin_unchanged():
    ids=identities()
    p=packet(2,ids.native);p.header=ROUTE_TYPE_FLOOD | PAYLOAD_TYPE_TRACE<<2
    raw=p.write_to();assert not ids.tag(p,'rak');assert p.write_to()==raw
    p=packet(2,b'\x99'*32);raw=p.write_to();assert not ids.tag(p,'rak');assert p.write_to()==raw
    p=packet(2,ids.native);p.path=bytearray();p.path_len=0
    assert not ids.tag(p,'rak')

@pytest.mark.asyncio
async def test_real_sequential_scheduler_tags_both_legs():
    from tests.test_ordered_tx_integration import rig
    from openhop_core.protocol.packet import Packet
    r,rak=rig(('link','rak'))
    ids=identities();ids.radios['link']=ids.radios.pop('directional')
    r.handler.tx_identities=ids
    p=packet(2,ids.native)
    before=p.write_to()
    task=await r.handler.schedule_retransmit(p,0,0,preferred_tx_radio_id='link')
    assert await task
    frames=[Packet() for _ in r.air.frames]
    for pkt,(_,raw) in zip(frames,r.air.frames): pkt.read_from(raw)
    assert len(frames)==2
    assert frames[0].path!=frames[1].path
    assert frames[0].calculate_packet_hash()==frames[1].calculate_packet_hash()
    assert bytes(frames[0].payload)==bytes(frames[1].payload)
    assert r.air.order==['link','rak'] and not r.air.overlapped
    assert p.write_to()==before


@pytest.mark.parametrize('section', [
    {'enabled': True, 'radios': {'rak': 'zz'}},
    {'enabled': True, 'radios': {'rak': '00' * 31}},
    {'enabled': 'yes', 'radios': {}},
    {'enabled': True, 'radios': ['rak']},
    'on',
])
def test_invalid_config_disables_experiment_without_raising(section, caplog):
    ids = TxIdentities({'tx_identity_experiment': section}, bytes.fromhex('bd6e' + '11' * 30))
    assert not ids.enabled and not ids.aliases
    assert 'TX identity experiment disabled' in caplog.text


def test_unknown_radio_is_warned_and_skipped(caplog):
    good = identities()
    keys = {rid: key.hex() for rid, (key, _) in good.radios.items()}
    keys['typo'] = keys.pop('rak')
    ids = TxIdentities({'tx_identity_experiment': {'enabled': True, 'radios': keys}},
                       good.native, good.crossing, radio_ids=['directional', 'rak'])
    assert ids.enabled and set(ids.radios) == {'directional'}
    assert "unknown radio 'typo'" in caplog.text


@pytest.mark.parametrize('crossing', [False, True])
def test_one_byte_paths_are_never_tagged_or_rewritten(crossing):
    ids = identities()
    canonical = ids.crossing if crossing else ids.native
    p = packet(1, canonical)
    raw = p.write_to()
    assert not ids.tag(p, 'rak') and p.write_to() == raw
    # A different node whose single byte equals one of our aliases keeps its hop.
    alias = next(iter(ids.aliases))
    p = packet(1, alias, direct=True)
    assert ids.canonical_direct(p) is p
    assert ids.prefixes(1) == set()
