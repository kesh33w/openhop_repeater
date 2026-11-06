"""Every broker must renew authentication before its token expires."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from repeater.data_acquisition.mqtt_handler import _BrokerConnection


@pytest.mark.parametrize(
    "broker_index,expected_fraction",
    [(0, 0.80), (1, 0.85), (2, 0.90), (3, 0.95), (4, 0.95), (7, 0.95), (20, 0.95)],
)
def test_refresh_is_staggered_and_capped_before_expiry(broker_index, expected_fraction):
    # No identity, token, network client, or real timer is created by this test.
    connection = _BrokerConnection.__new__(_BrokerConnection)
    connection.broker = {"name": "test broker"}
    connection.broker_index = broker_index
    connection.jwt_expiry_minutes = 10
    connection._jwt_refresh_timer = MagicMock()
    previous_timer = connection._jwt_refresh_timer
    lifetime_seconds = connection.jwt_expiry_minutes * 60
    expected_delay = lifetime_seconds * expected_fraction
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with patch("repeater.data_acquisition.mqtt_handler.threading.Timer") as timer:
        connection._schedule_jwt_refresh()

    previous_timer.cancel.assert_called_once_with()
    delay, callback = timer.call_args.args
    assert delay == pytest.approx(expected_delay)
    assert 0 < delay < lifetime_seconds
    assert callback == connection.reconnect_for_token_expiry
    assert timer.return_value.daemon is True
    timer.return_value.start.assert_called_once_with()

    with patch("repeater.data_acquisition.mqtt_handler.datetime") as clock:
        clock.now.return_value = now
        connection._connect_time = now - timedelta(seconds=expected_delay - 1)
        assert not connection.should_reconnect_for_token_expiry()
        connection._connect_time = now - timedelta(seconds=expected_delay + 1)
        assert connection.should_reconnect_for_token_expiry()
