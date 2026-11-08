"""Offline observer identity tests. Fixed public test seeds are never credentials."""
import base64
import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from nacl.signing import VerifyKey
from openhop_core import LocalIdentity

from repeater.data_acquisition.observer_identity import _load_seed, load_observer_identities
from tests.test_mqtt_radio_feeds import PUB, config, packet, rig


def write_seed(tmp_path, name, seed_byte):
    path = tmp_path / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"{seed_byte:02x}" * 32 + "\n")
    return str(path)


def separate_config(tmp_path):
    settings = config()
    settings['mqtt_brokers'].update(
        separate_observer_identities=True,
        observer_identities={
            'host-rf': {'name': 'Sacramento observer', 'private_key_file': write_seed(tmp_path, 'smf.seed', 1)},
            'bay': {'name': 'Bay observer', 'private_key_file': write_seed(tmp_path, 'sfo.seed', 2)},
        },
    )
    return settings


def test_stable_distinct_observers_sign_and_publish_with_matching_keys(tmp_path):
    settings = separate_config(tmp_path)
    before = copy.deepcopy(settings)
    p, clients, messages = rig(settings)
    again, _, _ = rig(settings)
    assert p.local_identity.get_public_key().hex().upper() == PUB
    assert len({c.public_key for c in p.connections}) == 2
    assert [c.public_key for c in p.connections] == [c.public_key for c in again.connections]
    assert all(c.public_key != PUB for c in p.connections)
    for conn, radio_id, region in zip(p.connections, ['host-rf', 'bay'], ['SMF', 'SFO']):
        conn.broker['audience'] = 'broker.invalid'
        token = conn._generate_jwt()
        header, claims, signature = token.split('.')
        body = json.loads(base64.urlsafe_b64decode(claims + '=' * (-len(claims) % 4)))
        assert body['publicKey'] == conn.public_key
        VerifyKey(bytes.fromhex(conn.public_key)).verify(f'{header}.{claims}'.encode(), bytes.fromhex(signature))
        assert conn.base_topic == f'meshcore/{region}/{conn.public_key}'
        payload = packet(radio_id, ['bay' if radio_id == 'host-rf' else 'host-rf'])
        p.publish_packet(payload)
        topic, result = messages[-1]
        assert topic == conn.base_topic + '/packets'
        assert result['origin_id'] == conn.public_key
        assert result['origin'] == conn.node_name
        assert result['rx_radio_id'] == radio_id
        assert payload['origin_id'] == PUB
    # Broker copies prevent runtime identity selection from modifying saved config.
    assert settings == before
    assert all(not c.connect.called for c in clients)


def test_status_and_offline_use_feed_name_key_and_radio_only(tmp_path):
    p, _, messages = rig(separate_config(tmp_path), telemetry={'host-rf': {'packets_received': 7}, 'bay': {'packets_received': 9}})
    for state in ['online', 'offline']:
        p.publish_status(state=state)
        for conn, (topic, payload) in zip(p.connections, messages[-2:]):
            assert topic == conn.base_topic + '/status'
            assert payload['origin_id'] == conn.public_key
            assert payload['origin'] == conn.node_name
            assert payload['status'] == state
            assert payload['stats']['packets_received'] in (7, 9)
            assert [r['id'] for r in payload['radios']] == [conn.radio_id]


def test_one_radio_feed_still_publishes_status(tmp_path):
    settings = separate_config(tmp_path)
    settings['radios'] = settings['radios'][:1]
    settings['mqtt_brokers']['brokers'] = settings['mqtt_brokers']['brokers'][:1]
    del settings['mqtt_brokers']['observer_identities']['bay']
    p, _, messages = rig(settings)
    p.publish_status()
    assert len(messages) == 1
    assert messages[0][1]['origin'] == 'Sacramento observer'


def test_missing_key_disables_affected_radio_without_rf_fallback(tmp_path):
    settings = separate_config(tmp_path)
    Path(settings['mqtt_brokers']['observer_identities']['host-rf']['private_key_file']).unlink()
    p, clients, messages = rig(settings)
    assert not p.connections[0].enabled
    assert p.connections[0].local_identity is None
    assert p.connections[0].public_key == 'UNCONFIGURED'
    p.connections[0].connect()
    p.publish_packet(packet('host-rf', ['bay']))
    assert not clients[0].connect.called and messages == []
    assert p.connections[1].enabled
    assert not Path(settings['mqtt_brokers']['observer_identities']['host-rf']['private_key_file']).exists()


def test_duplicate_keys_disable_both_radios(tmp_path):
    settings = separate_config(tmp_path)
    settings['mqtt_brokers']['observer_identities']['bay']['private_key_file'] = settings['mqtt_brokers']['observer_identities']['host-rf']['private_key_file']
    p, _, _ = rig(settings)
    assert all(not c.enabled and c.local_identity is None for c in p.connections)


def test_rf_identity_cannot_be_reused(tmp_path):
    settings = separate_config(tmp_path)
    rf_identity = LocalIdentity(seed=bytes([1]) * 32)
    identities, errors = load_observer_identities(settings, rf_identity)
    assert 'host-rf' not in identities and 'RF repeater' in errors['host-rf']


@pytest.mark.parametrize('bad', ['false', None, 0, 1])
def test_invalid_mode_fails_closed(tmp_path, bad):
    settings = separate_config(tmp_path)
    settings['mqtt_brokers']['separate_observer_identities'] = bad
    p, _, _ = rig(settings)
    assert all(not c.enabled for c in p.connections)


def test_missing_identity_and_unscoped_broker_fail_closed(tmp_path):
    settings = separate_config(tmp_path)
    del settings['mqtt_brokers']['observer_identities']['host-rf']
    del settings['mqtt_brokers']['brokers'][1]['radio_id']
    p, _, _ = rig(settings)
    assert all(not c.enabled for c in p.connections)


def test_disabled_mode_does_not_load_keys(tmp_path):
    settings = separate_config(tmp_path)
    settings['mqtt_brokers']['separate_observer_identities'] = False
    with patch('repeater.data_acquisition.observer_identity._load_seed', side_effect=AssertionError):
        p, _, _ = rig(settings)
    assert all(c.public_key == PUB for c in p.connections)


def test_custom_old_public_key_topic_cannot_override_identity(tmp_path):
    settings = separate_config(tmp_path)
    settings['mqtt_brokers']['brokers'][0]['base_topic'] = f'meshcore/SMF/{PUB}'
    p, _, _ = rig(settings)
    assert not p.connections[0].enabled


def test_disabled_destinations_stay_disabled_and_share_radio_key(tmp_path):
    settings = separate_config(tmp_path)
    original = settings['mqtt_brokers']['brokers'][0]
    settings['mqtt_brokers']['brokers'].append(dict(original, name='Other destination', host='other.invalid', enabled=False))
    p, _, messages = rig(settings)
    assert p.connections[2].public_key == p.connections[0].public_key
    assert not p.connections[2].enabled
    p.publish_packet(packet('host-rf', ['bay']))
    assert len(messages) == 1


def test_projected_payload_used_for_neighbors_and_custom_mqtt(tmp_path):
    settings = separate_config(tmp_path)
    settings['mqtt_brokers']['brokers'][0].update(format='mqtt', neighbors=True)
    p, _, messages = rig(settings)
    conn = p.connections[0]
    payload = {'radio_id': 'host-rf', 'origin_id': PUB, 'origin': 'RF repeater', 'neighbors': []}
    original = copy.deepcopy(payload)
    p.publish_neighbors(payload)
    p.publish_mqtt(payload, 'diagnostic')
    assert len(messages) == 2
    assert all(data['origin_id'] == conn.public_key and data['origin'] == conn.node_name for _, data in messages)
    assert payload == original


def test_key_loader_rejects_unsafe_or_corrupt_files_without_echoing_data(tmp_path):
    path = Path(write_seed(tmp_path, 'key', 1))
    assert _load_seed(str(path)) == bytes([1]) * 32
    path.chmod(0o644)
    with pytest.raises(ValueError, match='group or others'):
        _load_seed(str(path))
    path.chmod(0o600)
    path.write_text('DO NOT ECHO THIS INVALID KEY')
    with pytest.raises(ValueError, match='32-byte hex seed'):
        _load_seed(str(path))
    alias = tmp_path / 'alias'
    alias.symlink_to(path)
    with pytest.raises(ValueError, match='symbolic link'):
        _load_seed(str(alias))
    with pytest.raises(ValueError, match='absolute path'):
        _load_seed('relative.seed')


def test_runtime_selection_unchanged_after_pending_config_save(tmp_path):
    settings = separate_config(tmp_path)
    p, _, messages = rig(settings)
    key = p.connections[0].public_key
    settings['mqtt_brokers']['separate_observer_identities'] = False
    settings['mqtt_brokers']['observer_identities']['host-rf']['name'] = 'Pending name'
    p.publish_packet(packet('host-rf', ['bay']))
    assert messages[0][1]['origin_id'] == key
    assert messages[0][1]['origin'] == 'Sacramento observer'


def test_one_radio_explicitly_retains_rf_identity_and_history(tmp_path):
    settings = separate_config(tmp_path)
    settings['mqtt_brokers']['observer_identities']['host-rf'] = {
        'name': 'Sacramento observer', 'use_repeater_identity': True,
    }
    p, _, messages = rig(settings)
    sac, bay = p.connections
    assert sac.local_identity is p.local_identity
    assert sac.public_key == PUB
    assert sac.base_topic == f'meshcore/SMF/{PUB}'
    assert bay.public_key != PUB
    p.publish_packet(packet('host-rf', ['bay']))
    p.publish_packet(packet('bay', ['host-rf']))
    assert [data['origin_id'] for _, data in messages] == [PUB, bay.public_key]
    assert [data['origin'] for _, data in messages] == ['Sacramento observer', 'Bay observer']
    assert p.local_identity.get_public_key().hex().upper() == PUB


def test_explicit_rf_choice_and_bay_use_correct_jwt_user_and_signature(tmp_path):
    settings = separate_config(tmp_path)
    settings['mqtt_brokers']['observer_identities']['host-rf'] = {
        'name': 'Existing Sacramento name', 'use_repeater_identity': True,
    }
    rf = LocalIdentity(seed=bytes([3]) * 32)
    with patch('tests.test_mqtt_radio_feeds.Identity', return_value=rf):
        p, clients, _ = rig(settings)
    for conn, client in zip(p.connections, clients):
        conn.use_jwt_auth = True
        conn.broker['audience'] = 'broker.invalid'
        conn._set_credentials()
        supplied = client.username_pw_set.call_args.kwargs
        assert supplied['username'] == f'v1_{conn.public_key}'
        header, claims, signature = supplied['password'].split('.')
        VerifyKey(bytes.fromhex(conn.public_key)).verify(
            f'{header}.{claims}'.encode(), bytes.fromhex(signature)
        )
        assert conn.public_key == conn.local_identity.get_public_key().hex().upper()
    assert p.connections[0].local_identity is rf
    assert p.connections[1].public_key != p.connections[0].public_key
    assert all(not client.connect.called for client in clients)
