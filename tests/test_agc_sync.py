"""repeater.agc_reset_interval reaches the modems that implement it."""

from __future__ import annotations

import io
import os
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from repeater import agc_sync
from repeater.agc_sync import (
    AgcIntervalSync,
    RadioAgcState,
    desired_interval,
    discover,
    sync_target,
)
from repeater.handler_helpers.mesh_cli import MeshCLI


def _config(tmp_path, interval=4):
    secret = tmp_path / "g3909.password"
    secret.write_text("s3cret\n")
    os.chmod(secret, 0o600)
    return {
        "repeater": {"agc_reset_interval": interval},
        "radios": [
            {
                "id": "g3909",
                "radio_type": "modem_tcp",
                "modem_tcp": {"host": "10.0.0.9", "http_password_file": str(secret)},
            },
            # No HTTP credential: not controllable.
            {"id": "g3910", "radio_type": "modem_tcp", "modem_tcp": {"host": "10.0.0.10"}},
            {"id": "rak909", "radio_type": "modem_usb", "modem_usb": {"port": "/dev/x"}},
        ],
    }


def test_discover_and_desired_interval(tmp_path):
    cfg = _config(tmp_path)
    targets, unsupported = discover(cfg)
    assert [(t.radio_id, t.host, t.port, t.username) for t in targets] == [
        ("g3909", "10.0.0.9", 80, "admin")
    ]
    assert unsupported == ["g3910", "rak909"]
    assert desired_interval(cfg) == 4
    assert desired_interval({"repeater": {"agc_reset_interval": 99999}}) == 3600
    assert desired_interval({"repeater": {"agc_reset_interval": "x"}}) == 0
    assert desired_interval({}) == 0


def _fake(responses):
    calls = []

    def request(target, method, body=None):
        calls.append((method, body))
        reply = responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return request, calls


def test_sync_sets_only_when_different(tmp_path):
    (target,), _ = discover(_config(tmp_path))
    request, calls = _fake([{"agc_reset_interval_sec": 4}])
    assert sync_target(target, 4, request=request).applied == 4
    assert calls == [("GET", None)]

    request, calls = _fake(
        [
            {"agc_reset_interval_sec": 0},
            {"status": "saved", "config": {"agc_reset_interval_sec": 4}},
        ]
    )
    state = sync_target(target, 4, request=request)
    assert (state.applied, state.error) == (4, None)
    assert calls == [("GET", None), ("POST", {"agc_reset_interval_sec": 4})]


def test_sync_reports_failures_without_raising(tmp_path):
    (target,), _ = discover(_config(tmp_path))
    unauthorized = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO())
    cases = [
        ([unauthorized], "modem HTTP password rejected"),
        ([urllib.error.URLError(OSError("down"))], "modem unreachable (OSError)"),
        ([{"radio": {}}], "modem firmware has no AGC reset control"),
        (
            [{"agc_reset_interval_sec": 0}, {"config": {"agc_reset_interval_sec": 0}}],
            "modem reports 0 s after setting 4 s",
        ),
    ]
    for responses, error in cases:
        request, _ = _fake(responses)
        assert sync_target(target, 4, request=request).error == error


def test_password_file_must_be_private(tmp_path):
    cfg = _config(tmp_path)
    path = cfg["radios"][0]["modem_tcp"]["http_password_file"]
    assert agc_sync._read_secret(path) == "s3cret"
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="0600"):
        agc_sync._read_secret(path)


def test_sync_now_and_summary(tmp_path):
    cfg = _config(tmp_path)
    seen = []

    def fake_sync(target, desired):
        seen.append((target.radio_id, desired))
        return RadioAgcState(applied=desired)

    sync = AgcIntervalSync(cfg, sync_fn=fake_sync)
    assert sync.summary() == "g3909 pending, g3910 n/a, rak909 n/a"
    sync.sync_now()
    assert seen == [("g3909", 4)]
    assert sync.summary() == "g3909 4s, g3910 n/a, rak909 n/a"
    sync._states["g3909"] = RadioAgcState(error="modem HTTP password rejected")
    assert sync.summary() == "g3909 error, g3910 n/a, rak909 n/a"


def test_cli_reports_and_triggers_modem_sync(tmp_path):
    cfg = _config(tmp_path, interval=8)
    sync = AgcIntervalSync(cfg, sync_fn=lambda t, d: RadioAgcState(applied=d))
    sync.request_sync = MagicMock()
    daemon = SimpleNamespace(agc_sync=sync)

    def live_update(sections):
        # The real ConfigManager asks the daemon's sync to run after a save.
        if "repeater" in sections:
            daemon.agc_sync.request_sync()

    manager = SimpleNamespace(
        save_to_file=MagicMock(return_value=True),
        live_update_daemon=MagicMock(side_effect=live_update),
        daemon=daemon,
    )
    cli = MeshCLI("/tmp/cfg.yaml", cfg, manager)

    assert cli._cmd_set("agc.reset.interval 5") == "OK - interval rounded to 4, applying to modems"
    assert cfg["repeater"]["agc_reset_interval"] == 4
    sync.request_sync.assert_called_once()
    sync.sync_now()
    assert cli._cmd_get("agc.reset.interval") == "> 4 (g3909 4s, g3910 n/a, rak909 n/a)"


def test_logs_only_changes(tmp_path, caplog):
    cfg = _config(tmp_path)
    sync = AgcIntervalSync(cfg, sync_fn=lambda t, d: RadioAgcState(applied=d))
    with caplog.at_level("INFO", logger="AgcSync"):
        sync.sync_now()
        sync.sync_now()
    assert [r.getMessage() for r in caplog.records] == ["AGC reset interval on g3909: 4 s"]
