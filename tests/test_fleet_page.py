"""Smoke tests for the Streamlit page using Streamlit's AppTest (engine replaced by a recorder)."""
from pathlib import Path

import pytest
import streamlit as st
from streamlit.proto.TextInput_pb2 import TextInput

from streamlit.testing.v1 import AppTest

import fleet_ui.engine as engine_module
from fleet_ui.client import DroneInfo
from fleet_ui.engine import IDLE, RunState

PAGE = str(Path(__file__).resolve().parent.parent / "fleet_ui" / "fleet_ui.py")


class RecordingEngine:
    instance = None

    def __init__(self):
        RecordingEngine.instance = self
        self.calls = []
        self.running = False
        self.infos = {}        # ip -> DroneInfo returned by read_status

    def read_status(self, drones, ca_bundle=None, numbers=None):
        self.calls.append(("read_status", [d.ip for d in drones], list(numbers or [])))
        return [self.infos.get(d.ip, DroneInfo(reachable=False)) for d in drones]

    def state(self):
        return RunState(IDLE)

    @property
    def is_running(self):
        return self.running

    def logs(self):
        return ["12:00:00 drone 1, 192.168.4.11, takeoff, 200 ok"]

    def clear_log(self):
        self.calls.append(("clear_log",))

    def start(self, drones, steps, ca_bundle=None):
        self.calls.append(("start", list(drones), [s.text for s in steps], ca_bundle))
        return True

    def abort(self, drones=(), ca_bundle=None):
        self.calls.append(("abort", list(drones)))

    def land_all(self, drones=(), ca_bundle=None):
        self.calls.append(("land_all", list(drones)))


@pytest.fixture
def app(monkeypatch):
    st.cache_resource.clear()
    monkeypatch.setattr(engine_module, "FleetEngine", RecordingEngine)
    monkeypatch.setattr(engine_module, "configure_console_logging", lambda: None)
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    assert not at.exception
    yield at
    st.cache_resource.clear()


def button(at, label):
    return next(b for b in at.button if b.label == label)


def fill_drone(at, index, ip, key, port="8000"):
    at.text_input(key="ip_{}".format(index)).set_value(ip)
    at.text_input(key="port_{}".format(index)).set_value(port)
    at.text_input(key="key_{}".format(index)).set_value(key)


def test_initial_page_shows_errors_for_empty_drone(app):
    messages = [e.value for e in app.error]
    assert any("drone 1: IP address" in m for m in messages)
    assert any("API key is required" in m for m in messages)


def test_api_key_input_is_masked(app):
    assert app.text_input(key="key_0").proto.type == TextInput.PASSWORD


def test_add_and_remove_drones_numbering(app):
    button(app, "Add drone").click().run()
    assert [m.value for m in app.markdown if m.value.startswith("**drone")] == ["**drone 1**", "**drone 2**"]
    button(app, "Remove").click().run()      # removes the first row
    assert [m.value for m in app.markdown if m.value.startswith("**drone")] == ["**drone 1**"]
    assert app.session_state.drone_ids == [1]


def test_execute_with_invalid_input_is_refused(app):
    button(app, "Execute").click().run()
    assert RecordingEngine.instance.calls == []
    assert any("Not started" in e.value for e in app.error)


def test_execute_abort_and_land_wiring(app):
    fill_drone(app, 0, "192.168.4.11", "k" * 40)
    app.run()
    assert not [e for e in app.error if "drone 1" in e.value]
    button(app, "Execute").click().run()
    name, drones, steps, bundle = RecordingEngine.instance.calls[-1]
    assert name == "start" and steps == ["takeoff", "sleep 3", "land"] and bundle is None
    assert drones[0].ip == "192.168.4.11" and drones[0].port == 8000

    button(app, "Abort (hover all)").click().run()
    assert RecordingEngine.instance.calls[-1][0] == "abort"
    assert RecordingEngine.instance.calls[-1][1][0].ip == "192.168.4.11"
    button(app, "Land all").click().run()
    assert RecordingEngine.instance.calls[-1][0] == "land_all"


def test_ip_outside_network_is_rejected(app):
    fill_drone(app, 0, "10.0.0.5", "k" * 40)
    app.run()
    assert any("must be inside 192.168.4.0/24" in e.value for e in app.error)


def test_duplicate_endpoint_is_rejected(app):
    button(app, "Add drone").click().run()
    fill_drone(app, 0, "192.168.4.11", "k" * 40)
    fill_drone(app, 1, "192.168.4.11", "j" * 40)
    app.run()
    assert any("same IP and port" in e.value for e in app.error)


def test_missing_ca_bundle_is_reported(app):
    app.text_input(key="ca_bundle").set_value("/nonexistent/ca.pem").run()
    assert any("CA bundle file not found" in e.value for e in app.error)


def test_log_panel_and_clear(app):
    assert any("takeoff, 200 ok" in c.value for c in app.code)
    button(app, "Clear log").click().run()
    assert RecordingEngine.instance.calls[-1] == ("clear_log",)


def test_controls_disabled_while_running(monkeypatch):
    st.cache_resource.clear()

    class Busy(RecordingEngine):
        def __init__(self):
            super().__init__()
            self.running = True

        def state(self):
            return RunState("running", 1, 3, "takeoff", ("running",))

    monkeypatch.setattr(engine_module, "FleetEngine", Busy)
    monkeypatch.setattr(engine_module, "configure_console_logging", lambda: None)
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    assert not at.exception
    assert button(at, "Execute").disabled
    assert button(at, "Add drone").disabled
    assert not button(at, "Abort (hover all)").disabled and not button(at, "Land all").disabled
    assert at.text_input(key="ip_0").disabled
    st.cache_resource.clear()


def label_text(at, number):
    return next(m.value for m in at.markdown if m.value.startswith("**drone {}**".format(number)))


def test_status_shows_green_flying_and_red_ground_with_battery(app):
    button(app, "Add drone").click().run()
    fill_drone(app, 0, "192.168.4.11", "k" * 40)
    fill_drone(app, 1, "192.168.4.12", "j" * 40)
    app.run()
    assert label_text(app, 1) == "**drone 1**"          # nothing shown before Status is pressed
    RecordingEngine.instance.infos = {"192.168.4.11": DroneInfo(True, True, 87, True),
                                      "192.168.4.12": DroneInfo(True, False, 42, True)}
    button(app, "Status").click().run()
    assert RecordingEngine.instance.calls[-1] == ("read_status", ["192.168.4.11", "192.168.4.12"], [1, 2])
    assert label_text(app, 1) == "**drone 1** :green[**flying 87%**]"
    assert label_text(app, 2) == "**drone 2** :red[**on ground 42%**]"
    assert not [c for c in RecordingEngine.instance.calls if c[0] in ("start", "abort", "land_all")]


def test_status_unreachable_unknown_battery_and_stale_ip(app):
    fill_drone(app, 0, "192.168.4.11", "k" * 40)
    app.run()
    button(app, "Status").click().run()
    assert label_text(app, 1) == "**drone 1** :gray[no answer]"
    RecordingEngine.instance.infos = {"192.168.4.11": DroneInfo(True, False, None, True)}
    button(app, "Status").click().run()
    assert label_text(app, 1) == "**drone 1** :red[**on ground ?%**]"
    app.text_input(key="ip_0").set_value("192.168.4.99").run()      # IP edited: old result is hidden
    assert label_text(app, 1) == "**drone 1**"


def test_status_only_asks_valid_drones_and_refuses_when_none(app):
    button(app, "Status").click().run()
    assert any("No valid drone to ask" in e.value for e in app.error)
    assert not [c for c in RecordingEngine.instance.calls if c[0] == "read_status"]
    button(app, "Add drone").click().run()
    fill_drone(app, 1, "192.168.4.12", "j" * 40)       # drone 1 stays invalid
    app.run()
    button(app, "Status").click().run()
    assert RecordingEngine.instance.calls[-1] == ("read_status", ["192.168.4.12"], [2])


def test_status_button_is_available_while_running(monkeypatch):
    st.cache_resource.clear()

    class Busy(RecordingEngine):
        def __init__(self):
            super().__init__()
            self.running = True

        def state(self):
            return RunState("running", 1, 3, "takeoff", ("running",))

    monkeypatch.setattr(engine_module, "FleetEngine", Busy)
    monkeypatch.setattr(engine_module, "configure_console_logging", lambda: None)
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    assert not button(at, "Status").disabled
    st.cache_resource.clear()
