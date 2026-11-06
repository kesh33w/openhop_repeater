import copy
import json
from unittest.mock import MagicMock, patch
import pytest
from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from repeater.data_acquisition.storage_utils import PacketRecord

PUB = 'AB' * 32
class Identity:
    def get_public_key(self): return bytes.fromhex(PUB)

def config():
    radios=[{'id':'host-rf','radio_type':'modem_usb','radio':{'frequency':909875000,'bandwidth':62500,'spreading_factor':9,'coding_rate':5,'tx_power':19}},
            {'id':'bay','radio_type':'modem_usb','radio':{'frequency':910525000,'bandwidth':62500,'spreading_factor':7,'coding_rate':5,'tx_power':19}}]
    common={'enabled':True,'host':'broker.invalid','port':443,'transport':'websockets','format':'letsmesh','use_jwt_auth':False,'tls':{'enabled':True,'insecure':False},'neighbors':False}
    return {'repeater':{'node_name':'test'},'radio':radios[0]['radio'],'radios':radios,'fabric':{'default_radio':'host-rf','tx_mode':'sticky'},'mqtt_brokers':{'iata_code':'SMF','status_interval':0,'brokers':[dict(common,name='SMF feed',radio_id='host-rf',iata_code='SMF'),dict(common,name='SFO feed',radio_id='bay',iata_code='SFO')]}}

def rig(settings=None, telemetry=None):
    clients=[];messages=[]
    def client(**kwargs):
        c=MagicMock();c.configured_id=kwargs['client_id'];c.connect.side_effect=AssertionError('No external connections permitted')
        c.publish.side_effect=lambda topic,payload,**kw: messages.append((topic,json.loads(payload)))
        clients.append(c);return c
    with patch('repeater.data_acquisition.mqtt_handler.mqtt.Client',side_effect=client):
        p=MeshCoreToMqttPusher(Identity(),settings or config(),stats_provider=lambda:{'uptime_secs':12,'packets_received':999,'packets_sent':888,'errors':777},radio_stats_provider=lambda:telemetry or {})
    for connection in p.connections:connection._running=True
    return p,clients,messages

def packet(rx,tx):
    record=PacketRecord.from_packet_record({'timestamp':1700000000.,'type':4,'route':1,'rssi':-80,'snr':4.,'length':40,'score':.5,'payload_length':32,'packet_hash':'12'*8,'raw_packet':bytes(range(40)).hex(),'rx_radio_id':rx,'tx_radio_ids':tx},origin='test',origin_id=PUB,include_radio_ids=True)
    return record.to_dict()

@pytest.mark.parametrize('rx,tx,iata',[('host-rf',['bay'],'SMF'),('bay',['host-rf'],'SFO')])
def test_reception_feed_uses_ingress_not_bridge_egress(rx,tx,iata):
    p,clients,messages=rig();payload=packet(rx,tx);original=copy.deepcopy(payload)
    assert payload['direction']=='rx'
    p.publish_packet(payload)
    assert [t for t,_ in messages]==[f'meshcore/{iata}/{PUB}/packets']
    assert messages[0][1]['rx_radio_id']==rx and messages[0][1]['tx_radio_ids']==tx
    assert messages[0][1]['observer_radio_id']==rx
    assert messages[0][1]['origin_id']==PUB
    assert payload==original
    assert all(not c.connect.called for c in clients)

@pytest.mark.parametrize('rx',[None,'unmapped'])
def test_unattributed_packet_is_not_claimed_by_either_feed(rx):
    p,_,messages=rig();p.publish_packet(packet(rx,['host-rf','bay']));assert messages==[]

def test_feed_connections_have_distinct_stable_client_ids():
    p,clients,_=rig();other,again,_=rig()
    assert len(p.connections)==2
    assert len({c.configured_id for c in clients})==2
    assert [c.configured_id for c in clients]==[c.configured_id for c in again]
    assert [c.iata_code for c in p.connections]==['SMF','SFO']

def test_each_status_uses_its_radio_profile_and_metrics():
    telemetry={'host-rf':{'errors':2,'rx_air_secs':7,'tx_air_secs':3,'packets_received':11},'bay':{'errors':4,'rx_air_secs':9,'tx_air_secs':5,'packets_received':22}}
    p,_,messages=rig(telemetry=telemetry);p.publish_status()
    assert len(messages)==2
    for (topic,data),radio,frequency in zip(messages,['host-rf','bay'],['909.875','910.525']):
        assert data['radio'].startswith(frequency+',')
        assert [r['id'] for r in data['radios']]==[radio]
        assert data['observer_radio_id']==radio
        assert data['origin_id']==PUB
        assert data['stats']['packets_received']==telemetry[radio]['packets_received']
        assert data['stats']['errors']==telemetry[radio]['errors']
        assert data['stats']['uptime_secs']==12
        assert 'packets_sent' not in data['stats']

def test_missing_per_radio_counters_never_use_node_totals():
    p,_,messages=rig();p.publish_status()
    assert all('packets_received' not in d['stats'] and 'errors' not in d['stats'] for _,d in messages)

def test_status_with_no_matching_profile_is_not_published():
    p,_,messages=rig();p.publish('status',{'status':'online','stats':{'packets_received':999}})
    assert messages==[]

def test_legacy_broker_still_receives_the_unchanged_mixed_feed():
    settings=config();settings['mqtt_brokers']['brokers']=[{k:v for k,v in settings['mqtt_brokers']['brokers'][0].items() if k not in ('radio_id','iata_code')}]
    p,clients,messages=rig(settings);payload=packet('bay',['host-rf']);p.publish_packet(payload)
    assert len(messages)==1 and messages[0][0]==f'meshcore/SMF/{PUB}/packets'
    assert clients[0].configured_id==f'meshcore_{PUB}_broker.invalid_letsmesh'
    assert p.connections[0].iata_code=='SMF'
