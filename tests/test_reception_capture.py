import json
import queue
import threading
import time
from openhop_core.rf_fabric.capture import ReceptionCapture
from openhop_core.rf_fabric.topology import NetworkTopology, ReceptionComparison


def test_persists_identity_and_exact_frame_and_stops(tmp_path):
    path=tmp_path/'rx.jsonl'
    c=ReceptionCapture(str(path),time.time()+.15,'bay910')
    c.emit('rx','bay910',radio_id='rak',packet_hash='identity',frame_hash='frame',own_tx_match=False)
    c.emit('rx','smf909',radio_id='unrelated')
    c.thread.join(2)
    rows=[json.loads(x) for x in path.read_text().splitlines()]
    assert [r['event'] for r in rows]==['start','rx','complete']
    assert rows[1]['packet_hash']=='identity' and rows[1]['frame_hash']=='frame'
    assert rows[-1]['reason']=='deadline' and not c.snapshot()['active']


def test_full_queue_counts_loss_without_waiting():
    c=ReceptionCapture.__new__(ReceptionCapture)
    c.network='bay910';c.lock=threading.Lock();c.active=True;c.deadline=time.time()+10
    c.queue=queue.Queue(1);c.session='test';c.dropped=0
    c.emit('rx','bay910',packet_hash='a');c.emit('rx','bay910',packet_hash='b')
    assert c.dropped==1 and c.queue.qsize()==1


def test_byte_cap_is_cumulative_across_sessions(tmp_path):
    path=tmp_path/'rx.jsonl';path.write_bytes(b'\n'*4090)
    c=ReceptionCapture(str(path),time.time()+.2,'bay910',max_bytes=8192)
    for _ in range(50):c.emit('rx','bay910',packet_hash='x'*256)
    c.thread.join(2)
    assert path.stat().st_size<=8192
    assert c.snapshot()['reason']=='byte_limit' and c.snapshot()['dropped']>0


def test_io_failure_does_not_escape_to_receiver(tmp_path):
    c=ReceptionCapture(str(tmp_path/'missing'/'rx'),time.time()+1,'bay910')
    c.thread.join(2)
    c.emit('rx','bay910',packet_hash='a')
    assert c.snapshot()['errors']==1 and c.snapshot()['reason']=='io_error'


def test_hooks_keep_shadow_policy_and_record_echo(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENHOP_RX_CAPTURE_PATH',str(tmp_path/'rx.jsonl'))
    monkeypatch.setenv('OPENHOP_RX_CAPTURE_END',str(time.time()+.25))
    monkeypatch.setenv('OPENHOP_RX_CAPTURE_NETWORK','bay910')
    t=NetworkTopology([{'id':'a','network':'smf909','role':'transceiver'},
                       {'id':'b','network':'bay910','role':'transceiver'},
                       {'id':'rak','network':'bay910','role':'shadow'}],
                      {'networks':{'smf909':'a','bay910':'b'}},'a')
    c=ReceptionComparison(t)
    c.start_tx('b',b'frame','identity')
    c.receive('rak','identity',b'frame')
    c.end_tx('bay910',b'frame')
    assert t.destinations('rak')==()
    assert c.snapshot()['receivers']['rak']['own_tx_match']==1
    c.capture.thread.join(2)
    rows=[json.loads(x) for x in (tmp_path/'rx.jsonl').read_text().splitlines()]
    assert [x['event'] for x in rows]==['start','tx_start','rx','tx_end','complete']
    assert rows[1]['packet_hash']=='identity' and rows[2]['own_tx_match'] is True


def test_close_drains_accepted_rows(tmp_path):
    c=ReceptionCapture(str(tmp_path/'rx'),time.time()+60,'bay910')
    for i in range(100):c.emit('rx','bay910',packet_hash=str(i))
    c.close()
    rows=[json.loads(x) for x in (tmp_path/'rx').read_text().splitlines()]
    assert sum(x['event']=='rx' for x in rows)==100
    assert rows[-1]['reason']=='closed' and rows[-1]['dropped']==0


def test_incomplete_tail_preserved_and_new_session_parseable(tmp_path):
    path=tmp_path/'rx';path.write_bytes(b'{"event":')
    c=ReceptionCapture(str(path),time.time()+.1,'bay910');c.thread.join(2)
    lines=path.read_text().splitlines()
    assert lines[0]=='{"event":'
    assert json.loads(lines[1])['previous_tail_incomplete'] is True
    assert json.loads(lines[-1])['event']=='complete'


import pytest
@pytest.mark.parametrize('fail_event,short', [('rx',True),('rx',False),('complete',True)])
def test_write_failures_report_error_and_in_hand_loss(tmp_path,monkeypatch,fail_event,short):
    import openhop_core.rf_fabric.capture as module
    original=module.os.fdopen
    class Wrapper:
        def __init__(self,fd,*args,**kwargs):self.file=original(fd,*args,**kwargs)
        def __enter__(self):return self
        def __exit__(self,*args):return self.file.__exit__(*args)
        def fileno(self):return self.file.fileno()
        def write(self,data):
            if json.loads(data)['event']==fail_event:
                if short:return self.file.write(data[:4])
                raise OSError('test disk failure')
            return self.file.write(data)
    monkeypatch.setattr(module.os,'fdopen',Wrapper)
    c=ReceptionCapture(str(tmp_path/'rx'),time.time()+.1,'bay910')
    c.emit('rx','bay910',packet_hash='a');c.thread.join(2)
    assert c.snapshot()['errors']==1 and c.snapshot()['reason']=='io_error'
    if fail_event=='rx':assert c.snapshot()['dropped']>=1


@pytest.mark.asyncio
@pytest.mark.parametrize('result,expected',[(True,'confirmed'),(False,'not_confirmed'),(None,'not_confirmed'),({'ok':False,'tx_outcome_uncertain':True},'uncertain'),(RuntimeError('test'),'exception')])
async def test_fabric_capture_tx_outcomes(result,expected):
    from tests.test_network_topology import Rig,config
    from unittest.mock import AsyncMock
    rig=Rig(config());fabric=rig.radio.fabric
    rows=[]
    class Sink:
        def emit(self,event,network,**fields):rows.append((event,fields))
    fabric.comparison.capture=Sink()
    rig.ears['link'].send=AsyncMock(side_effect=result) if isinstance(result,Exception) else AsyncMock(return_value=result)
    if isinstance(result,Exception):
        with pytest.raises(RuntimeError):await fabric.send(b'\x3e\x00diagnostic',radio_id='link')
    else:await fabric.send(b'\x3e\x00diagnostic',radio_id='link')
    assert rows[-1][0]=='tx_end' and rows[-1][1]['outcome']==expected
