"""A temporary broker outage during startup must not disable the feed forever."""

from unittest.mock import patch

import pytest

from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher, _BrokerConnection

MODULE = "repeater.data_acquisition.mqtt_handler"


def connection(enabled=True):
    broker = {
        "name": "test broker",
        "host": "broker.invalid",
        "port": 443,
        "enabled": enabled,
        "transport": "websockets",
        "tls": {"enabled": True, "insecure": False},
        "keepalive": 55,
    }
    with patch(f"{MODULE}.mqtt.Client"):
        return _BrokerConnection(broker, None, "AB" * 32, "SMF", 10, "", "", 0, "test")


def pusher_with(*connections):
    pusher = MeshCoreToMqttPusher.__new__(MeshCoreToMqttPusher)
    pusher.connections = list(connections)
    pusher._shutdown_requested = False
    return pusher


@pytest.mark.parametrize("delayed", [False, True])
def test_initial_handshake_failure_retries_then_recovers(delayed):
    conn = connection()
    conn.client.connect.side_effect = [OSError("WebSocket handshake failed"), None]
    pusher = pusher_with(conn)
    with patch(f"{MODULE}.threading.Timer") as timer:
        if delayed:
            pusher._delayed_connect(conn)
        else:
            pusher.connect()
        delay, retry = timer.call_args.args
        assert delay == 5
        assert conn.has_pending_reconnect()
        conn.client.loop_start.assert_not_called()
        retry()
        assert conn._reconnect_timer is None
        conn.client.loop_start.assert_called_once_with()
        assert conn.client.connect.call_count == 2
        conn.client.tls_set.assert_called_once()
        conn.client.tls_insecure_set.assert_called_once_with(False)
        conn._on_connect(conn.client, None, None, 0)
    assert conn.is_connected()
    assert conn._reconnect_attempts == 0


def test_repeated_initial_failure_uses_existing_backoff():
    conn = connection()
    conn.client.connect.side_effect = OSError("broker unavailable")
    with patch(f"{MODULE}.threading.Timer") as timer:
        with pytest.raises(OSError):
            conn.connect()
        for expected_delay in (5, 10, 20, 40, 80, 160, 300, 300):
            delay, retry = timer.call_args.args
            assert delay == expected_delay
            retry()
    assert not conn.is_connected()
    assert conn.has_pending_reconnect()


def test_disabled_feed_does_not_connect_or_retry():
    conn = connection(enabled=False)
    with patch(f"{MODULE}.threading.Timer") as timer:
        conn.connect()
    conn.client.connect.assert_not_called()
    timer.assert_not_called()


def test_initial_failure_does_not_prevent_other_brokers_from_connecting():
    failed = connection()
    healthy = connection()
    failed.client.connect.side_effect = OSError("broker unavailable")
    with patch(f"{MODULE}.threading.Timer") as timer:
        pusher_with(failed, healthy).connect()
        assert [call.args[0] for call in timer.call_args_list] == [5, 30]
        timer.call_args_list[-1].args[1]()
    healthy.client.connect.assert_called_once()
    healthy.client.loop_start.assert_called_once_with()


def test_shutdown_cancels_pending_retry_and_ignores_late_callback():
    conn = connection()
    conn.client.connect.side_effect = OSError("broker unavailable")
    with patch(f"{MODULE}.threading.Timer") as timer:
        with pytest.raises(OSError):
            conn.connect()
        retry = timer.call_args.args[1]
        conn.disconnect()
        timer.return_value.cancel.assert_called_once_with()
        retry()
        conn._schedule_reconnect()
        assert timer.call_count == 1
    assert conn._shutdown_requested
    assert conn._reconnect_timer is None
    assert conn.client.connect.call_count == 1


def test_shutdown_during_failed_connect_does_not_schedule_retry():
    conn = connection()

    def fail_during_shutdown(*args, **kwargs):
        conn.disconnect()
        raise OSError("broker unavailable")

    conn.client.connect.side_effect = fail_during_shutdown
    with patch(f"{MODULE}.threading.Timer") as timer:
        with pytest.raises(OSError):
            conn.connect()
    assert conn._shutdown_requested
    timer.assert_not_called()


def test_explicit_connect_can_resume_after_shutdown():
    conn = connection()
    conn.disconnect()
    conn.connect()
    assert not conn._shutdown_requested
    conn.client.connect.assert_called_once()
    conn.client.loop_start.assert_called_once()
