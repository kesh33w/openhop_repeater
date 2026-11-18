from types import SimpleNamespace as NS
import pytest
from repeater.bot_rf_receipts import runtime_binding, TEST_KEY


def rig(receivers):
    key='ab'*32
    bridge=NS(get_public_key=lambda:bytes.fromhex(key),
              _receive_radio_ids=frozenset(receivers),_receive_home_radio_id='host-rf',
              channels=NS(get_channels=lambda:[{'name':'#test','secret':TEST_KEY.hex()}]))
    radio=NS(frequency=909875000,bandwidth=62500,spreading_factor=9,coding_rate=5,rx_receipt_barrier=lambda:None)
    topology=NS(network=lambda rid:'smf909',networks={'smf909':'host-rf'},is_shadow=lambda rid:False)
    fabric=NS(topology=topology,get_radio=lambda rid:radio,has_rx_binding=lambda rid:True)
    daemon=NS(companion_frame_servers=[NS(bridge=bridge,port=5050)],radio=NS(fabric=fabric))
    config=dict(enabled=True,socket='/test',secret_file='/secret',companion_key=key,
                companion_endpoint=['10.40.0.100',5050],receivers=receivers,tx_radio='host-rf')
    return daemon,config

@pytest.mark.parametrize('receivers',[['host-rf'],['host-rf','omni909'],['host-rf','omni909','extra909']])
def test_variable_receiver_membership(receivers):
    d,c=rig(receivers)
    assert runtime_binding(d,c)['receivers']==receivers

@pytest.mark.parametrize('receivers',[[],['host-rf','host-rf'],['host-rf',''],['host-rf',None]])
def test_invalid_membership(receivers):
    d,c=rig(receivers)
    with pytest.raises(ValueError):runtime_binding(d,c)

def test_mismatch_still_rejected():
    d,c=rig(['host-rf']);c['receivers']=['host-rf','missing']
    with pytest.raises(ValueError,match='receivers differ'):runtime_binding(d,c)
