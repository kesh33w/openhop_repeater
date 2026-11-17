from types import SimpleNamespace as NS
from unittest.mock import Mock
from concurrent.futures import Future
from repeater.handler_helpers.mesh_cli import MeshCLI


def cli_with_radios():
    r = NS(lbt_enabled=True, perform_cad=lambda: None)
    api = NS(apply_cad_mode=Mock(return_value={'success': True}))
    daemon = NS(radio=NS(fabric=NS(radios={'smf': r, 'sfo': r}, default_radio_id='smf')),
                http_server=NS(app=NS(api=api)))
    cli = MeshCLI('/tmp/test', {}, NS(daemon=daemon))
    return cli, api


def test_get_all_and_one():
    cli, api = cli_with_radios()
    assert cli.handle_command(b'', 'get cad', True) == '> smf: on; sfo: on'
    assert cli.handle_command(b'', 'get cad sfo', True) == '> sfo: on'
    api.apply_cad_mode.assert_not_called()


def test_set_requires_admin_and_explicit_radio():
    cli, api = cli_with_radios()
    assert 'Admin' in cli.handle_command(b'', 'set cad off smf', False)
    assert 'Specify' in cli.handle_command(b'', 'set cad off', True)
    assert 'Error' in cli.handle_command(b'', 'set cad maybe smf', True)
    assert 'Unknown' in cli.handle_command(b'', 'set cad off missing', True)
    api.apply_cad_mode.assert_not_called()


def test_set_uses_shared_service_and_reports_saved():
    cli, api = cli_with_radios()
    assert cli.handle_command(b'', 'set cad off smf', True).startswith('Pending')
    assert cli._cad_job.result(timeout=2) == 'saved smf'
    api.apply_cad_mode.assert_called_once_with({'radio_id': 'smf', 'enabled': False})
    assert 'saved smf' in cli.handle_command(b'', 'get cad', True)


def test_failures_and_duplicate_pending():
    cli, api = cli_with_radios()
    cli._cad_job = Future()
    assert 'pending' in cli.handle_command(b'', 'set cad off smf', True)
    api.apply_cad_mode.assert_not_called()
    cli._cad_job.set_result('old')
    api.apply_cad_mode.return_value = {'success': False, 'error': 'ACK failed'}
    cli.handle_command(b'', 'set cad on smf', True)
    assert cli._cad_job.result(timeout=2) == 'Error: ACK failed'
    assert 'ACK failed' in cli.handle_command(b'', 'get cad', True)
