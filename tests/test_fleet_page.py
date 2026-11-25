"""Smoke tests for the Streamlit page using Streamlit's AppTest (engine replaced by a recorder)."""
import json
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
        self.known = {}        # (ip, port) -> last DroneInfo, like the real engine remembers

    def read_status(self, drones, ca_bundle=None, numbers=None):
        self.calls.append(("read_status", [d.ip for d in drones], list(numbers or [])))
        result = [self.infos.get(d.ip, DroneInfo(reachable=False)) for d in drones]
        for drone, info in zip(drones, result):
            self.known[(drone.ip, str(drone.port))] = info
        return result

    def info_for(self, ip, port):
        return self.known.get((ip.strip(), str(port).strip()))

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


@pytest.fixture(autouse=True)
def drones_file(tmp_path, monkeypatch):
    """Never touch the real fleet_ui/drones.txt from tests."""
    path = tmp_path / "drones.txt"
    monkeypatch.setenv("FLEET_UI_DRONES_FILE", str(path))
    return path


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


# ----- save / load of drones.txt -----
SAVE_PASS = "correct horse battery"


def saved_app(monkeypatch, drones_file, setup_json):
    """Start a fresh page with a drones.txt already present."""
    drones_file.write_text(json.dumps(setup_json))
    st.cache_resource.clear()
    monkeypatch.setattr(engine_module, "FleetEngine", RecordingEngine)
    monkeypatch.setattr(engine_module, "configure_console_logging", lambda: None)
    return AppTest.from_file(PAGE, default_timeout=30).run()


def test_save_writes_file_and_a_new_session_loads_it(app, drones_file, monkeypatch):
    button(app, "Add drone").click().run()
    fill_drone(app, 0, "192.168.4.11", "k" * 40, "8000")
    fill_drone(app, 1, "192.168.4.12", "j" * 40, "8001")
    app.text_input(key="ca_bundle").set_value("/certs/ca.pem")
    app.text_input(key="passphrase").set_value(SAVE_PASS)
    app.run()
    button(app, "Save").click().run()
    assert any("API keys encrypted" in s.value for s in app.success)
    raw = drones_file.read_text()
    assert "k" * 40 not in raw and "j" * 40 not in raw and SAVE_PASS not in raw

    # new browser session: drones, port, CA bundle and the default script come back, keys stay empty
    st.cache_resource.clear()
    fresh = AppTest.from_file(PAGE, default_timeout=30).run()
    assert fresh.text_input(key="ip_0").value == "192.168.4.11" and fresh.text_input(key="port_0").value == "8000"
    assert fresh.text_input(key="ip_1").value == "192.168.4.12" and fresh.text_input(key="port_1").value == "8001"
    assert fresh.text_input(key="ca_bundle").value == "/certs/ca.pem"
    assert fresh.text_input(key="key_0").value == "" and fresh.text_input(key="key_1").value == ""
    assert [m.value for m in fresh.markdown if m.value.startswith("**drone")] == ["**drone 1**", "**drone 2**"]

    # unlock fills the keys back in
    fresh.text_input(key="passphrase").set_value(SAVE_PASS).run()
    button(fresh, "Unlock keys").click().run()
    assert fresh.text_input(key="key_0").value == "k" * 40
    assert fresh.text_input(key="key_1").value == "j" * 40
    assert any("filled in for 2" in s.value for s in fresh.success)
    assert fresh.text_input(key="passphrase").value == ""


def test_unlock_with_wrong_passphrase_fills_nothing(monkeypatch, drones_file):
    from fleet_ui import storage
    storage.save_setup(drones_file, [("192.168.4.11", "8000", "k" * 40)], "", ["land"], SAVE_PASS)
    st.cache_resource.clear()
    monkeypatch.setattr(engine_module, "FleetEngine", RecordingEngine)
    monkeypatch.setattr(engine_module, "configure_console_logging", lambda: None)
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    at.text_input(key="passphrase").set_value("not the passphrase").run()
    button(at, "Unlock keys").click().run()
    assert any("Wrong passphrase" in e.value for e in at.error)
    assert at.text_input(key="key_0").value == ""


def test_unlock_button_disabled_without_saved_keys(app):
    assert button(app, "Unlock keys").disabled


def test_save_without_passphrase_leaves_keys_out(app, drones_file):
    fill_drone(app, 0, "192.168.4.11", "k" * 40)
    app.run()
    button(app, "Save").click().run()
    assert any("API keys not saved" in s.value for s in app.success)
    assert "k" * 40 not in drones_file.read_text() and "encrypted_keys" not in drones_file.read_text()


def test_save_without_passphrase_warns_when_removing_saved_keys(monkeypatch, drones_file):
    from fleet_ui import storage
    storage.save_setup(drones_file, [("192.168.4.11", "8000", "k" * 40)], "", ["land"], SAVE_PASS)
    st.cache_resource.clear()
    monkeypatch.setattr(engine_module, "FleetEngine", RecordingEngine)
    monkeypatch.setattr(engine_module, "configure_console_logging", lambda: None)
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    button(at, "Save").click().run()
    assert any("previously saved keys were removed" in w.value for w in at.warning)
    assert "encrypted_keys" not in drones_file.read_text()


def test_short_passphrase_is_refused_and_nothing_written(app, drones_file):
    fill_drone(app, 0, "192.168.4.11", "k" * 40)
    app.text_input(key="passphrase").set_value("short")
    app.run()
    button(app, "Save").click().run()
    assert any("Not saved" in e.value and "passphrase" in e.value for e in app.error)
    assert not drones_file.exists()


def test_saved_script_is_loaded_into_the_editor(monkeypatch, drones_file):
    at = saved_app(monkeypatch, drones_file, {"version": 1, "drones": [{"ip": "192.168.4.5", "port": "8005"}],
                                              "ca_bundle": "", "script": ["takeoff", "up 70", "land"]})
    assert not at.exception
    assert at.text_input(key="ip_0").value == "192.168.4.5" and at.text_input(key="port_0").value == "8005"
    button(at, "Execute").click().run()      # key missing -> refused, but the script was parsed from the file
    assert any("API key is required" in e.value for e in at.error)
    at.text_input(key="key_0").set_value("k" * 40).run()
    button(at, "Execute").click().run()
    assert RecordingEngine.instance.calls[-1][2] == ["takeoff", "up 70", "land"]


def test_saved_script_changes_are_saved(app, drones_file):
    fill_drone(app, 0, "192.168.4.11", "k" * 40)
    app.run()
    button(app, "Save").click().run()
    assert json.loads(drones_file.read_text())["script"] == ["takeoff", "sleep 3", "land"]


def test_corrupt_file_shows_warning_and_default_form(monkeypatch, drones_file):
    drones_file.write_text("{ this is not json")
    st.cache_resource.clear()
    monkeypatch.setattr(engine_module, "FleetEngine", RecordingEngine)
    monkeypatch.setattr(engine_module, "configure_console_logging", lambda: None)
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    assert not at.exception
    assert any("Saved setup not loaded" in w.value for w in at.warning)
    assert at.text_input(key="ip_0").value == "" and at.text_input(key="port_0").value == "8000"


def test_no_file_starts_with_defaults(app):
    assert app.text_input(key="ip_0").value == "" and app.text_input(key="port_0").value == "8000"
    assert not app.warning


# ----- status shown during a run (the engine polls it before every script row) -----
def test_labels_show_status_that_the_engine_collected_during_a_run(monkeypatch):
    st.cache_resource.clear()

    class Running(RecordingEngine):
        def __init__(self):
            super().__init__()
            self.running = True

        def state(self):
            return RunState("running", 2, 3, "up 50", ("running", "running"))

    monkeypatch.setattr(engine_module, "FleetEngine", Running)
    monkeypatch.setattr(engine_module, "configure_console_logging", lambda: None)
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.session_state["drone_ids"] = [0, 1]
    at.session_state["next_drone_id"] = 2
    at.session_state["ip_0"], at.session_state["port_0"] = "192.168.4.11", "8000"
    at.session_state["ip_1"], at.session_state["port_1"] = "192.168.4.12", "8000"
    at.session_state["ca_bundle"] = ""
    at.session_state["script_base"] = ["takeoff"]
    at.session_state["load_error"] = None
    at.session_state["encrypted_keys"] = None
    at.session_state["saved_ips"] = []
    at.run()
    assert not at.exception
    engine = RecordingEngine.instance
    engine.known[("192.168.4.11", "8000")] = DroneInfo(True, True, 64, True)
    engine.known[("192.168.4.12", "8000")] = DroneInfo(True, False, 18, True)
    at.run()
    assert label_text(at, 1) == "**drone 1** :green[**flying 64%**]"
    assert label_text(at, 2) == "**drone 2** :red[**on ground 18%**]"
    st.cache_resource.clear()


def test_label_follows_the_engine_and_hides_stale_ip(app):
    fill_drone(app, 0, "192.168.4.11", "k" * 40)
    app.run()
    engine = RecordingEngine.instance
    engine.known[("192.168.4.11", "8000")] = DroneInfo(True, True, 50, True)
    app.run()
    assert label_text(app, 1) == "**drone 1** :green[**flying 50%**]"
    engine.known[("192.168.4.11", "8000")] = DroneInfo(True, False, 49, True)   # e.g. after `land`
    app.run()
    assert label_text(app, 1) == "**drone 1** :red[**on ground 49%**]"
    app.text_input(key="port_0").set_value("8001").run()                        # other endpoint: no status
    assert label_text(app, 1) == "**drone 1**"
