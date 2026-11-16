"""MQTT consumes shared logical streams, preserving first physical facts."""
import queue
import pytest
from openhop_core.rf_fabric.logical_receiver import LogicalReceiver
from repeater.data_acquisition.logical_observer import LogicalObserver
from tests.test_logical_observer import setup, wait_for
from tests.test_channel_bridge import packet

@pytest.mark.parametrize('first', ['local','omni909'])
def test_shared_pair_and_separate_shadow(first,tmp_path):
    rig,pusher,records=setup(tmp_path)
    receiver=LogicalReceiver(rig.topology,wall_clock=lambda:1234.5)
    worker=LogicalObserver({'local':'909','link':'910','rak':'shadow:rak'},rig.topology,pusher,receiver=receiver)
    try:
        raw=packet().write_to()
        receiver.observe(raw,-90,4,rx_radio_id=first)
        receiver.observe(raw,-70,8,rx_radio_id='omni909' if first=='local' else 'local')
        receiver.observe(raw,-80,6,rx_radio_id='rak')
        wait_for(lambda:len(records)==2)
        pair=next(p for rid,p in records if rid=='local')
        assert pair['rx_radio_id']==first
        assert pair['logical_receiver_id']=='909'
        assert pair['receive_event_id']
        assert pair['RSSI']=='-90'
        assert worker.snapshot()['cache_size']==0
        assert worker.snapshot()['receive_mode']=='shared_logical'
        assert receiver.snapshot()['unique_events']==2
        with pytest.raises(ValueError):worker.receive(raw,-90,4,rx_radio_id=first)
    finally:worker.close()
    assert receiver.snapshot()['subscriber_count']==0


def test_queue_refusal_retries_original_first_without_duplicate(tmp_path):
    rig,pusher,records=setup(tmp_path);receiver=LogicalReceiver(rig.topology)
    worker=LogicalObserver({'local':'909'},rig.topology,pusher,receiver=receiver)
    original=worker._queue.put_nowait
    def reject(row):raise queue.Full
    worker._queue.put_nowait=reject
    try:
        raw=packet().write_to();first=receiver.observe(raw,-90,4,rx_radio_id='omni909')
        assert receiver.snapshot()['subscriber_refusals']==1
        worker._queue.put_nowait=original
        receiver.observe(raw,-50,8,rx_radio_id='local')
        receiver.observe(raw,-40,9,rx_radio_id='local')
        wait_for(lambda:len(records)==1)
        assert records[0][1]['receive_event_id']==first.event_id
        assert records[0][1]['rx_radio_id']=='omni909'
        assert records[0][1]['RSSI']=='-90'
    finally:worker.close()


def test_invalid_stream_bindings_fail_closed(tmp_path):
    rig,pusher,records=setup(tmp_path);receiver=LogicalReceiver(rig.topology)
    for groups in [{'local':'missing'},{'local':'910'},{'local':'909','link':'909'},{'rak':'910'}]:
        with pytest.raises(ValueError):LogicalObserver(groups,rig.topology,pusher,receiver=receiver)
