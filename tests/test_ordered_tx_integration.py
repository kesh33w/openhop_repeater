"""Real scheduler/dispatcher/fabric; simulated radios only, never RF."""
import pytest
from openhop_core.rf_fabric.topology import NetworkTopology
from tests.test_fabric_fanout import _Rig, _FakeRadio, _flood


def rig(order):
    r=_Rig()
    rak=_FakeRadio('rak',r.air)
    r.radio.fabric.register_radio(rak,radio_id='rak')
    for radio in (r.local,r.link,rak):
        radio._cad_mode_explicit=False
        radio.lbt_enabled=False
    entries=[{'id':'local','network':'SMF','role':'transceiver'},
             {'id':'link','network':'SFO','role':'transceiver'},
             {'id':'rak','network':'SFO','role':'shadow'}]
    t=NetworkTopology(entries,{'networks':{'SMF':'local','SFO':'link'},
                              'tx_groups':{'link':list(order)}},'local')
    r.radio.fabric.configure_topology(t)
    r.handler.network_topology=t
    return r,rak


@pytest.mark.asyncio
@pytest.mark.parametrize('order',[('link','rak'),('rak','link')])
async def test_full_stack_order_and_dedup(order):
    r,rak=rig(order)
    pkt=_flood()
    task=await r.handler.schedule_retransmit(pkt,0,0,preferred_tx_radio_id='link')
    assert await task
    assert r.air.order==list(order)
    assert r.air.frames[0][1]==r.air.frames[1][1]
    assert not r.air.overlapped
    assert len(pkt._tx_metadata['ordered_tx_results'])==2
    # A separate logical invocation cannot create two more physical sends.
    task=await r.handler.schedule_retransmit(_flood(),0,0,preferred_tx_radio_id='link')
    assert not await task
    assert r.air.order==list(order)


@pytest.mark.asyncio
async def test_unknown_first_result_stops_second():
    r,rak=rig(('rak','link'))
    rak.fail=True
    task=await r.handler.schedule_retransmit(_flood(),0,0,preferred_tx_radio_id='link')
    assert not await task
    assert not r.air.frames
    assert r.sent_radio_ids==['rak']


@pytest.mark.asyncio
async def test_existing_cad_does_not_prevent_or_change_sequential_sends():
    r,rak=rig(('link','rak'))
    rak._cad_mode_explicit=True
    task=await r.handler.schedule_retransmit(_flood(),0,0,preferred_tx_radio_id='link')
    assert await task
    assert r.air.order == ['link','rak']
    assert rak._cad_mode_explicit is True
    assert not r.air.overlapped


@pytest.mark.asyncio
async def test_partial_success_keeps_each_leg_and_uncertainty():
    r,rak=rig(('link','rak'))
    rak.fail=True
    pkt=_flood()
    task=await r.handler.schedule_retransmit(pkt,0,0,preferred_tx_radio_id='link')
    assert await task
    assert pkt._tx_metadata['tx_outcome_uncertain']
    assert [x['success'] for x in pkt._tx_metadata['ordered_tx_results']]==[True,False]


@pytest.mark.asyncio
async def test_cancellation_keeps_partial_results():
    import asyncio
    r,rak=rig(('link','rak'))
    async def cancel(*args,**kwargs): raise asyncio.CancelledError()
    rak.send=cancel
    pkt=_flood()
    task=await r.handler.schedule_retransmit(pkt,0,0,preferred_tx_radio_id='link')
    with pytest.raises(asyncio.CancelledError): await task
    assert [x['success'] for x in pkt._tx_metadata['ordered_tx_results']]==[True,False]
    assert pkt._tx_metadata['tx_outcome_uncertain']


@pytest.mark.asyncio
async def test_second_leg_respects_remaining_shared_channel_budget():
    r,rak=rig(('link','rak'))
    # Rig has a single shared ledger; each leg costs 10 ms, limit is 15 ms.
    ledger=r.handler.airtime_budgets.for_radio('link')
    ledger._ledger.max_airtime_per_minute=15
    r.handler._egress_radio_airtime=lambda packet,radio_id: 10
    pkt=_flood()
    task=await r.handler.schedule_retransmit(pkt,0,10,preferred_tx_radio_id='link')
    assert await task
    assert r.air.order==['link']
    assert pkt._tx_metadata['ordered_tx_results'][1]['metadata']['reason']=='duty_cycle_exceeded'
