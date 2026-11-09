import asyncio
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import cherrypy
import pytest
from openhop_core.rf_fabric import FabricRadio

from repeater.cad_radio import apply_cad_settings, physical_radios, resolve_cad_radio
from repeater.config import BaselineCrcCounterRadio
from repeater.config_manager import ConfigManager
from repeater.web.api_endpoints import APIEndpoints
from repeater.web.cad_calibration_engine import CADCalibrationEngine


class Radio:
    def __init__(self, frequency, sf, peak):
        self.frequency, self.spreading_factor = frequency, sf
        self.bandwidth, self.coding_rate = 62500, 8
        self._custom_cad_peak, self._custom_cad_min = peak, 10
        self._custom_cad_symbol_num = 2
        self.calls = []

    def set_rx_callback(self, cb):
        pass

    async def perform_cad(self, **kwargs):
        self.calls.append(("cad", kwargs))
        return {"cad_done": True, "detected": True}

    def set_custom_cad_thresholds(self, peak, min_val):
        self.calls.append(("set", peak, min_val))
        self._custom_cad_peak, self._custom_cad_min = peak, min_val

    def set_custom_cad_symbol_num(self, num):
        self.calls.append(("symbols", num))
        self._custom_cad_symbol_num = num


@pytest.fixture
def api(tmp_path, monkeypatch):
    smf, bay = Radio(909875000, 9, 24), Radio(910525000, 7, 22)
    config = {
        "radio": {"cad": {"peak_threshold": 24, "min_threshold": 10, "symbol_num": 2}},
        "radios": [
            {
                "id": "host-rf",
                "radio": {"cad": {"peak_threshold": 24, "min_threshold": 10, "symbol_num": 2}},
            },
            {
                "id": "bay",
                "radio": {"cad": {"peak_threshold": 22, "min_threshold": 9, "symbol_num": 4}},
            },
        ],
        "fabric": {"default_radio": "host-rf"},
    }
    daemon = SimpleNamespace(
        radio=FabricRadio(radios=[(smf, "host-rf"), (bay, "bay")], default_radio_id="host-rf"),
        config=config,
    )
    api = APIEndpoints.__new__(APIEndpoints)
    api.config, api.daemon_instance, api.event_loop = config, daemon, None
    api.cad_calibration = CADCalibrationEngine(daemon)
    api.config_manager = ConfigManager(
        str(tmp_path / "config.yaml"), config=config, daemon_instance=daemon
    )
    api.smf, api.bay = smf, bay
    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="POST", json={}, params={}))
    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}, status=200))
    return api


@pytest.fixture
def loop(api):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    api.event_loop = api.cad_calibration.event_loop = loop
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join()
    loop.close()


def body(**kwargs):
    cherrypy.request.json = kwargs


def test_resolves_physical_not_default_proxy(api):
    assert resolve_cad_radio(api.daemon_instance, "bay") == ("bay", api.bay)
    assert resolve_cad_radio(api.daemon_instance) == ("host-rf", api.smf)
    assert resolve_cad_radio(api.daemon_instance, " ") == ("host-rf", api.smf)
    api.daemon_instance.radio = BaselineCrcCounterRadio(api.daemon_instance.radio)
    assert resolve_cad_radio(api.daemon_instance, "bay") == ("bay", api.bay)


@pytest.mark.parametrize("radio_id", ["missing", 42, {}, []])
def test_rejects_unknown_without_fallback(api, radio_id):
    with pytest.raises(ValueError):
        resolve_cad_radio(api.daemon_instance, radio_id)
    assert not api.smf.calls and not api.bay.calls


def test_discovery_is_read_only_and_reports_runtime_fields(api):
    response = api.cad_radios()["data"]
    assert response["default_radio_id"] == "host-rf"
    assert response["active_radio_id"] is None
    assert response["running"] is False
    bay = response["radios"][1]
    assert bay["radio_id"] == "bay"
    assert (bay["frequency"], bay["spreading_factor"], bay["current_cad_peak"]) == (
        910525000,
        7,
        22,
    )
    assert not api.smf.calls and not api.bay.calls


def test_manual_only_calls_selected_radio(api, loop):
    body(radio_id="bay", samples=2, det_peak=21, det_min=9)
    result = api.cad_manual_check()
    assert result["success"]
    assert result["data"]["radio_id"] == "bay"
    assert result["data"]["detections"] == 2
    assert len(api.bay.calls) == 2
    assert not api.smf.calls
    assert not api.cad_calibration.busy


def test_legacy_manual_without_id_still_uses_default(api, loop):
    body(samples=1)
    assert api.cad_manual_check()["success"]
    assert len(api.smf.calls) == 1 and not api.bay.calls


def test_save_bay_is_isolated_and_survives_startup(api):
    old_smf = copy.deepcopy(api.config["radio"])
    body(radio_id="bay", peak=20, min_val=8, cad_symbol_num=4)
    result = api.save_cad_settings()
    assert result["success"]
    assert result["settings"]["radio_id"] == "bay"
    assert api.config["radio"] == old_smf
    assert api.config["radios"][0]["radio"] == old_smf
    assert not api.smf.calls
    assert api.bay._custom_cad_peak == 20
    apply_cad_settings(api.daemon_instance)
    assert api.smf._custom_cad_peak == 24
    assert api.bay._custom_cad_peak == 20
    assert api.bay._custom_cad_symbol_num == 4
    import yaml

    with open(api.config_manager.config_path) as saved_file:
        saved = yaml.safe_load(saved_file)
    assert saved["radios"][1]["radio"]["cad"]["peak_threshold"] == 20


def test_default_save_syncs_legacy_preserves_other_inheritance(api):
    del api.config["radios"][1]["radio"]["cad"]
    body(peak=25, min_val=11)
    assert api.save_cad_settings()["success"]
    assert api.config["radio"]["cad"]["peak_threshold"] == 25
    assert api.config["radios"][0]["radio"]["cad"]["peak_threshold"] == 25
    assert api.config["radios"][1]["radio"]["cad"]["peak_threshold"] == 24
    assert not api.bay.calls
    apply_cad_settings(api.daemon_instance)
    assert api.bay._custom_cad_peak == 24


def test_save_failure_does_not_change_live_or_config(api):
    before = copy.deepcopy(api.config)
    api.config_manager.save_to_file = Mock(return_value=False)
    body(radio_id="bay", peak=20, min_val=8)
    assert not api.save_cad_settings()["success"]
    assert api.config == before
    assert not api.bay.calls and not api.smf.calls


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("cad_calibration_start", {}),
        ("cad_manual_check", {}),
        ("save_cad_settings", {"peak": 22, "min_val": 10}),
    ],
)
def test_unknown_rejected_by_all_mutating_endpoints(api, endpoint, payload):
    body(radio_id="not-a-radio", **payload)
    assert not getattr(api, endpoint)()["success"]
    assert not api.bay.calls and not api.smf.calls


def test_busy_start_does_not_replace_session(api):
    engine = api.cad_calibration
    engine.running, engine.radio_id = True, "bay"
    engine.session_config = {"cad_symbol_num": 4}
    body(radio_id="host-rf", cad_symbol_num=16)
    assert not api.cad_calibration_start()["success"]
    assert engine.session_config == {"cad_symbol_num": 4}
    assert engine.radio_id == "bay"
    assert not api.cad_manual_check()["success"]
    body(radio_id="host-rf", peak=20, min_val=8)
    assert not api.save_cad_settings()["success"]
    assert not api.cad_calibration_stop()["success"]
    assert engine.running


def test_start_pins_radio_even_if_default_changes(api, monkeypatch):
    class DeferredThread:
        def __init__(self, target, args):
            self.target, self.args = target, args

        def start(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr("repeater.web.cad_calibration_engine.threading.Thread", DeferredThread)
    body(radio_id="bay", cad_symbol_num=4)
    assert api.cad_calibration_start()["success"]
    assert api.cad_calibration.session_radio is api.bay
    assert api.cad_calibration.radio_id == "bay"
    assert api.cad_calibration.session_config["cad_symbol_num"] == 4
    api.daemon_instance.radio.set_default_radio("host-rf")
    assert api.cad_calibration.session_radio is api.bay
    assert api.cad_radios()["data"]["active_radio_id"] == "bay"


def test_worker_calls_pinned_bay_and_broadcasts_identity(api, loop):
    body(radio_id="bay", samples=1, delay=0)
    assert api.cad_calibration_start()["success"]
    api.cad_calibration.calibration_thread.join(10)
    assert not api.cad_calibration.busy
    assert api.bay.calls and not api.smf.calls
    messages = api.cad_calibration.message_queue
    assert all(m["radio_id"] == "bay" for m in messages)
    assert any(m["type"] == "completed" for m in messages)
    ranges = [m["test_ranges"] for m in messages if "test_ranges" in m]
    assert ranges and all(r["spreading_factor"] == 7 for r in ranges)


def test_stopping_worker_still_blocks_new_start(api):
    api.cad_calibration.running = False
    api.cad_calibration.calibration_thread = SimpleNamespace(is_alive=lambda: True)
    body(radio_id="bay")
    assert not api.cad_calibration_start()["success"]


def test_sse_filtered_replay_and_repeat_queue_reset(api, monkeypatch):
    monkeypatch.setattr("repeater.web.api_endpoints.time.sleep", lambda _: None)
    engine = api.cad_calibration
    engine.message_queue = [
        {"type": "result", "radio_id": "host-rf"},
        {"type": "result", "radio_id": "bay", "det_peak": 22},
    ]
    stream = api.cad_calibration_stream("bay")
    assert "connected" in next(stream)
    assert '"det_peak": 22' in next(stream)
    assert "keepalive" in next(stream)
    engine.clear_message_queue()
    engine.message_queue.append({"type": "result", "radio_id": "bay", "det_peak": 21})
    assert '"det_peak": 21' in next(stream)
    stream.close()
    with pytest.raises(cherrypy.HTTPError):
        api.cad_calibration_stream("wrong")


def test_legacy_single_radio_save_and_startup(api):
    api.daemon_instance.radio = api.smf
    api.config.pop("radios")
    body(peak=23, min_val=11, cad_symbol_num=8)
    assert api.save_cad_settings()["success"]
    assert physical_radios(api.daemon_instance)[1] == "radio0"
    assert api.config["radio"]["cad"]["peak_threshold"] == 23
    apply_cad_settings(api.daemon_instance)
    assert api.smf._custom_cad_symbol_num == 8


def test_manual_activity_not_reported_as_calibration_on_reload(api):
    api.cad_calibration.radio_id = "host-rf"
    api.cad_calibration.manual_running = True
    state = api.cad_radios()["data"]
    assert state["running"] is False
    assert state["active_radio_id"] is None
    assert api.cad_calibration.busy


def test_timeout_cancels_pending_sample_and_keeps_busy_until_cleanup(api, loop):
    engine = api.cad_calibration
    entered = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    async def slow_check(*args, **kwargs):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            cleanup_started.set()
            while not release_cleanup.is_set():
                await asyncio.sleep(0.001)

    engine.test_cad_config = slow_check
    future = engine._submit_cad_test(api.bay, 22, 10)
    assert entered.wait(2)
    engine.running = True
    try:
        with pytest.raises(TimeoutError):
            engine._wait_cad_result(future, timeout=0.01)
        assert cleanup_started.wait(2)
        assert engine.busy
        body(radio_id="host-rf")
        assert not api.cad_calibration_start()["success"]
    finally:
        release_cleanup.set()
    import time

    deadline = time.monotonic() + 2
    while engine.busy and time.monotonic() < deadline:
        time.sleep(0.001)
    assert not engine.busy


def test_stopped_worker_emits_terminal_event(api, loop):
    entered = threading.Event()

    async def slow_check(*args, **kwargs):
        entered.set()
        await asyncio.sleep(30)

    api.cad_calibration.test_cad_config = slow_check
    body(radio_id="bay")
    assert api.cad_calibration_start()["success"]
    assert entered.wait(2)
    api.cad_calibration.stop_calibration()
    # Worker exit can precede coroutine cancellation cleanup; busy must cover both.
    import time
    deadline = time.monotonic() + 2
    while api.cad_calibration.busy and time.monotonic() < deadline:
        time.sleep(0.001)
    assert not api.cad_calibration.busy
    assert api.cad_calibration.message_queue[-1]["type"] == "stopped"
    assert api.cad_calibration.message_queue[-1]["radio_id"] == "bay"


@pytest.mark.parametrize(
    "payload,content_type,expected",
    [
        (b"", None, 200),
        (b"", "application/json", 200),
        (b'{"radio_id":"bay"}', "application/json", 200),
        (b'{"radio_id":"host-rf"}', "application/json", 200),
        (b'["bay"]', "application/json", 400),
    ],
)
def test_stop_wsgi_accepts_legacy_empty_and_checks_named_radio(
    api, monkeypatch, payload, content_type, expected
):
    from io import BytesIO
    from wsgiref.util import setup_testing_defaults

    monkeypatch.setattr(cherrypy, "request", cherrypy._ThreadLocalProxy("request"))
    monkeypatch.setattr(cherrypy, "response", cherrypy._ThreadLocalProxy("response"))
    api.cad_calibration.running = True
    api.cad_calibration.radio_id = "bay"
    app = cherrypy.Application(SimpleNamespace(api=api))
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        REQUEST_METHOD="POST",
        PATH_INFO="/api/cad_calibration_stop",
        CONTENT_LENGTH=str(len(payload)),
    )
    environ["wsgi.input"] = BytesIO(payload)
    if content_type:
        environ["CONTENT_TYPE"] = content_type
    status = []
    response = app(environ, lambda value, headers, exc_info=None: status.append(value))
    data = b"".join(response)
    if hasattr(response, "close"):
        response.close()
    assert int(status[0].split()[0]) == expected, data
    if expected == 200 and payload != b'{"radio_id":"host-rf"}':
        assert json.loads(data)["success"]
        assert not api.cad_calibration.running
    else:
        assert api.cad_calibration.running
