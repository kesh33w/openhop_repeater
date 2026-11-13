"""Offline-only indicator compatibility, budget, settings, and echo regression tests."""
import copy
import logging

import pytest
from openhop_core.protocol.crypto import CryptoUtils
from openhop_core.protocol.packet import Packet
from openhop_core.protocol.packet_builder import PacketBuilder

from repeater.channel_bridge import BridgeSettings, ChannelBridge, Crossing, EchoLedger
from repeater.web.bridge_settings import bridge_candidate, bridge_view
from tests.test_channel_bridge import A, B, C1, C2, packet
from tests.test_channel_bridge_engine import config, rig_factory  # noqa: F401
from tests import test_fabric_fanout as fabric_tests


def planner(prefix='[B]', reverse='[A]'):
    return ChannelBridge([C1, C2], [
        Crossing(A, B, C1.name, C2.name, text_prefix=prefix),
        Crossing(B, A, C2.name, C1.name, text_prefix=reverse),
    ])


def plaintext(p, channel):
    return CryptoUtils.mac_then_decrypt(channel.secret[:16], channel.secret, bytes(p.payload[1:]))


def make_config(tmp_path, prefix):
    section = config(tmp_path)
    section['mappings'][0]['text_prefix'] = prefix
    return {'radios': [{'id': 'local'}, {'id': 'link'}], 'channel_bridge': section}


@pytest.mark.parametrize('home,source,target,expected', [(A,C1,C2,'[B]'),(B,C2,C1,'[A]')])
@pytest.mark.parametrize('sender,body', [('Dan','Hello'), ('💧Kesh','Café 🛰 reply: yes')])
def test_marker_preserves_sender_body_timestamp_and_wire_format(home, source, target, expected, sender, body):
    p = PacketBuilder.create_group_datagram(source.name, None, body, sender,
        [{'name':source.name, 'secret':source.key}], timestamp=123456)
    p.path, p.path_len = bytearray([0x11, 0x22]), 2
    before = p.write_to()
    local, crossed = planner().plan(p, home)
    assert local.packet.write_to() == p.write_to() == before
    out = crossed.packet
    assert out.header == p.header and out.path == p.path and out.path_len == p.path_len
    assert out.payload_len == len(out.payload)
    result = plaintext(out, target)
    assert result[:5] == plaintext(p, source)[:5]
    assert result[5:].rstrip(b'\0').decode() == f'{expected} {sender}: {body}'
    # Standard parser accepts the actual bytes; encryption remains channel-authenticated.
    parsed = Packet()
    assert parsed.read_from(out.write_to())
    assert bytes(parsed.payload) == bytes(out.payload)
    assert plaintext(parsed, target) == result
    with pytest.raises(ValueError):
        plaintext(out, source)


@pytest.mark.parametrize('prefix', ['', '↔', '[B]', '🌉', 'é'*16])
def test_text_budget_exactly_160_bytes_is_accepted(prefix):
    marker_bytes = len(prefix.encode()) + bool(prefix)
    body = 'x' * (160 - marker_bytes - len('Test sender: '.encode()))
    local, cross = planner(prefix).plan(packet(message=body), A)
    content = plaintext(cross.packet, C2)[5:].rstrip(b'\0')
    assert len(content) == 160
    assert content.endswith(body.encode())
    assert local.packet.payload == packet(message=body).payload


@pytest.mark.parametrize('prefix', ['↔', '[B]', '🌉', 'é'*16])
def test_over_budget_skips_only_crossing_and_logs_no_content(prefix, caplog):
    marker_bytes = len(prefix.encode()) + 1
    body = 'x' * (161 - marker_bytes - len('Test sender: '.encode()))
    source = packet(message=body)
    with caplog.at_level(logging.WARNING, logger='repeater.channel_bridge'):
        sends = planner(prefix).plan(source, A)
    assert len(sends) == 1 and sends[0].packet.write_to() == source.write_to()
    assert '161 UTF-8 bytes (maximum 160)' in caplog.text
    assert prefix not in caplog.text and body not in caplog.text and 'Test sender' not in caplog.text


def test_multibyte_body_budget_counts_bytes_not_characters(caplog):
    p = packet(message='é'*72)  # 13 + 144 = 157; [B] plus space makes 161.
    assert len(plaintext(p, C1)[5:].rstrip(b'\0')) == 157
    assert len(planner('[B]').plan(p, A)) == 1
    assert len(planner('B').plan(p, A)) == 2


def test_disabled_marker_has_identical_ciphertext_and_defaults(tmp_path):
    source = packet()
    old = ChannelBridge([C1,C2], [Crossing(A,B,C1.name,C2.name)])
    assert planner('').plan(source,A)[1].packet.write_to() == old.plan(source,A)[1].packet.write_to()
    settings = BridgeSettings(make_config(tmp_path, ''))
    assert [rule.text_prefix for rule in settings.planner.crossings] == ['', '']


def test_same_channel_marker_still_uses_normal_packet_and_echo_ledger(tmp_path):
    bridge = ChannelBridge([C1], [Crossing(A,B,C1.name,C1.name,text_prefix='[B]')])
    source = packet()
    out = bridge.plan(source,A)[1].packet
    assert plaintext(out,C1)[5:].rstrip(b'\0') == b'[B] Test sender: bench only'
    with_ledger = EchoLedger(tmp_path/'ledger.sqlite')
    try:
        token = with_ledger.reserve(source,out)
        assert token and with_ledger.contains(source) and with_ledger.contains(out)
        with_ledger.finish(token,success=True)
    finally:
        with_ledger.close()
    restored = EchoLedger(tmp_path/'ledger.sqlite')
    try:
        assert restored.contains(out) and restored.reserve(out,source) is None
    finally:
        restored.close()


@pytest.mark.parametrize('prefix', [None, True, 1, [], 'x'*33, 'é'*17, '🌉'*9, ' x', 'x ', 'x:y', '\n', 'x\0y', '\t', '\x7f', '\u0085', '\u2028', '\u2029', '\u202e', '\ud800', '\u200b'])
def test_invalid_markers_fail_closed_in_config_and_api_atomically(tmp_path,prefix):
    cfg=make_config(tmp_path,prefix)
    before=copy.deepcopy(cfg)
    with pytest.raises(ValueError):
        BridgeSettings(cfg)
    with pytest.raises(ValueError):
        bridge_candidate(cfg, {})
    cfg['channel_bridge']['enabled']=False
    with pytest.raises(ValueError):
        bridge_candidate(cfg, {})
    cfg['channel_bridge']['enabled']=True
    assert cfg == before


@pytest.mark.parametrize('prefix', ['', '↔', '[B]', '🌉', 'é'*16, 'x'*32])
def test_settings_round_trip_retains_marker_and_keys(tmp_path,prefix):
    cfg=make_config(tmp_path,prefix)
    before=copy.deepcopy(cfg)
    view=bridge_view(cfg)
    assert view['settings']['mappings'][0]['text_prefix'] == prefix
    candidate=bridge_candidate(cfg, view['settings'])
    assert candidate['channels'] == cfg['channel_bridge']['channels']
    assert candidate['mappings'] == cfg['channel_bridge']['mappings']
    assert cfg == before


@pytest.mark.asyncio
@pytest.mark.parametrize('home,index,target', [('local',0,C2),('link',1,C1)])
async def test_engine_reserves_final_marked_bytes_and_stops_echo_after_restart(rig_factory,tmp_path,home,index,target):
    mappings=config(tmp_path)['mappings']
    mappings[0]['text_prefix']='[B]'
    mappings[1]['text_prefix']='[A]'
    first=rig_factory(mappings=mappings)
    p=packet(C1 if index==0 else C2)
    assert await first.handler(p,fabric_tests._rx(home))
    outgoing=copy.deepcopy(first.sent_packets[1][1])
    assert plaintext(outgoing,target)[5:].rstrip(b'\0').startswith(b'[B]' if index==0 else b'[A]')
    assert await first.handler.bridge_echoes.contains(p)
    assert await first.handler.bridge_echoes.contains(outgoing)
    outgoing.path,outgoing.path_len=bytearray(),0
    first.handler.close_channel_bridge()
    second=rig_factory(mappings=mappings)
    assert not await second.handler(outgoing,fabric_tests._rx('link' if home=='local' else 'local'))
    assert not second.air.frames


@pytest.mark.asyncio
async def test_engine_over_budget_keeps_local_repeat_and_does_not_reserve_or_transmit_crossing(rig_factory,tmp_path):
    mappings=config(tmp_path)['mappings']
    mappings[0]['text_prefix']='[B]'
    rig=rig_factory(mappings=mappings)
    p=packet(message='é'*72)
    assert await rig.handler(p,fabric_tests._rx('local'))
    assert rig.air.order == ['local']
    assert not await rig.handler.bridge_echoes.contains(p)


@pytest.mark.parametrize('key_length', [16,32])
def test_16_and_32_byte_keys_round_trip_with_a_marker(key_length):
    from repeater.channel_bridge import Channel
    c1=Channel('source', bytes(range(key_length)))
    c2=Channel('target', bytes(range(32,32+key_length)))
    p=PacketBuilder.create_group_datagram(c1.name,None,'Hi','Sender',[{'name':c1.name,'secret':c1.key}],timestamp=123456)
    local,out=ChannelBridge([c1,c2],[Crossing(A,B,c1.name,c2.name,text_prefix='🌉')]).plan(p,A)
    assert local.packet.write_to()==p.write_to()
    assert plaintext(out.packet,c2)[5:].rstrip(b'\0').decode()=='🌉 Sender: Hi'


@pytest.mark.parametrize('reason', ['paused','disabled_mapping','disabled_channel','policy'])
def test_markers_never_bypass_crossing_gates(tmp_path,reason):
    cfg=make_config(tmp_path,'[B]')
    section=cfg['channel_bridge']
    if reason=='paused': section['paused']=True
    elif reason=='disabled_mapping': section['mappings'][0]['enabled']=False
    elif reason=='disabled_channel': section['disabled_channels']=[C2.name]
    else: section['crossing_policy']={'enabled':True,'default_action':'drop'}
    settings=BridgeSettings(cfg)
    sends=settings.plan(packet(),'local',{})
    assert len(sends)==1 and not sends[0].crossing
    assert sends[0].packet.write_to()==packet().write_to()


@pytest.mark.asyncio
async def test_failed_marked_send_releases_both_echo_hashes(rig_factory,tmp_path):
    from unittest.mock import AsyncMock
    mappings=config(tmp_path)['mappings']
    mappings[0]['text_prefix']='[B]'
    rig=rig_factory(mappings=mappings)
    rig.link.send=AsyncMock(return_value=False)
    p=packet()
    assert await rig.handler(p,fabric_tests._rx('local'))
    assert rig.air.order==['local']
    assert not await rig.handler.bridge_echoes.contains(p)
    rows=rig.handler.storage.record_packet.call_args.kwargs['tx_egress']
    assert [row['success'] for row in rows]==[True,False]
    assert rows[1]['failure_reason']=='Native TX rejected, failed, or expired'


@pytest.mark.parametrize('first_marker,second_marker', [('↔','↔'), ('↔','[B]'), ('[B]','[B]'), ('[A]','[B]'), ('via Bay','via Sac'), ('🌉','[B]')])
@pytest.mark.parametrize('same_channel', [False,True])
def test_independent_bridge_cycle_converges_without_growing_markers(tmp_path,first_marker,second_marker,same_channel):
    target=C1 if same_channel else C2
    channels=[C1] if same_channel else [C1,C2]
    first=ChannelBridge(channels,[Crossing(A,B,C1.name,target.name,text_prefix=first_marker)])
    second=ChannelBridge(channels,[Crossing(B,A,target.name,C1.name,text_prefix=second_marker)])
    ledgers=[EchoLedger(tmp_path/'one.sqlite'),EchoLedger(tmp_path/'two.sqlite')]
    p=packet()
    crossings=0
    try:
        for index in range(12):
            current,ledger,home=((first,ledgers[0],A) if index%2==0 else (second,ledgers[1],B))
            if ledger.contains(p): break
            sends=current.plan(p,home,repeat_local=False)
            assert len(sends)==1 # Must stop via ledger, not overflowing message budget.
            out=sends[0].packet
            token=ledger.reserve(p,out)
            if token is None: break
            ledger.finish(token,success=True)
            p=out
            crossings+=1
        else: pytest.fail('independent bridge loop did not converge')
        assert crossings<=3
        channel=target if crossings%2 else C1
        text=plaintext(p,channel)[5:].rstrip(b'\0').decode()
        assert text.endswith('Test sender: bench only')
        assert text.count(first_marker)==1
        if second_marker!=first_marker: assert text.count(second_marker)<=1
    finally:
        for ledger in ledgers: ledger.close()


@pytest.mark.parametrize('sender,marker,expected', [
    ('↔ Dan','↔','↔ Dan'),
    ('[B] ↔ Dan','↔','[B] ↔ Dan'),
    ('[B] Dan','[B]','[B] Dan'),
    ('[A] [B] Dan','[B]','[A] [B] Dan'),
    ('[B]Dan','[B]','[B] [B]Dan'),
    ('via Bay Dan','via Bay','via Bay Dan'),
])
def test_existing_exact_sender_label_is_idempotent(sender,marker,expected):
    p=PacketBuilder.create_group_datagram(C1.name,None,'[B] in body only',sender,[{'name':C1.name,'secret':C1.key}],timestamp=123456)
    out=planner(marker).plan(p,A)[1].packet
    assert plaintext(out,C2)[5:].rstrip(b'\0').decode()==expected+': [B] in body only'


def test_body_marker_does_not_prevent_marking_sender():
    out=planner('[B]').plan(packet(message='[B] in body only'),A)[1].packet
    assert plaintext(out,C2)[5:].rstrip(b'\0')==b'[B] Test sender: [B] in body only'
