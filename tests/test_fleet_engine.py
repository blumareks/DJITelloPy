import json
import logging
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fleet_ui.client import (CallResult, DroneConfig, DroneInfo, TelloApiClient, check_ca_bundle,
                             check_drone, info_from_result)
from fleet_ui.engine import (ABORTED, FINISHED, HALTED, LANDED, FleetEngine)
from fleet_ui.steps import parse_script

MAKE_CERTS = Path(__file__).resolve().parent.parent / "fleet_ui" / "make_certs.sh"
KEY1 = "key-one-" + "x" * 30
KEY2 = "key-two-" + "y" * 30


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    out = tmp_path_factory.mktemp("certs")
    subprocess.run(["sh", str(MAKE_CERTS), "-o", str(out), "127.0.0.1"], check=True,
                   capture_output=True)
    # a second, unrelated CA to prove untrusted certificates are rejected
    other = tmp_path_factory.mktemp("other")
    subprocess.run(["sh", str(MAKE_CERTS), "-o", str(other), "127.0.0.1"], check=True,
                   capture_output=True)
    return out, other


class FakeDrone:
    """A tiny HTTPS server that mimics the tello_api endpoints used by the fleet UI."""

    def __init__(self, certs_dir, key):
        self.key = key
        self.requests = []             # (monotonic time, method, path, body)
        self.responses = {}            # path -> (status, json body)
        self.hold = {}                 # path -> threading.Event released to answer
        self.entered = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                owner.requests.append((time.monotonic(), self.command, self.path, body,
                                      self.headers.get("X-API-Key")))
                if self.headers.get("X-API-Key") != owner.key:
                    return self._send(401, {"detail": "Invalid or missing API key"})
                if self.path in ("/v1/abort", "/v1/land"):
                    # like the real service: an abort/land interrupts a running movement
                    for pending in owner.hold.values():
                        pending.set()
                else:
                    owner.entered.set()
                gate = owner.hold.get(self.path)
                if gate is not None:
                    gate.wait(10)
                    return self._send(502, {"detail": "Drone command failed"})
                status, payload = owner.responses.get(self.path, (200, {"status": "ok"}))
                self._send(status, payload)

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = do_PUT = do_GET = _handle

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(certs_dir / "127.0.0.1" / "cert.pem"),
                                str(certs_dir / "127.0.0.1" / "key.pem"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def command_requests(self):
        """Requests other than the GET /v1/status that is sent before every step."""
        return [r for r in self.requests if r[2] != "/v1/status"]

    def paths(self):
        return [r[2] for r in self.command_requests()]

    def close(self):
        for gate in self.hold.values():
            gate.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fleet(certs):
    drones = [FakeDrone(certs[0], KEY1), FakeDrone(certs[0], KEY2)]
    yield drones
    for d in drones:
        d.close()


def configs(fleet):
    return [DroneConfig("127.0.0.1", fleet[0].port, KEY1), DroneConfig("127.0.0.1", fleet[1].port, KEY2)]


def steps(text):
    result = parse_script(text)
    assert not result.errors
    return result.steps


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_lockstep_order_requests_and_log_format(fleet, certs):
    engine = FleetEngine()
    ca = str(certs[0] / "ca.pem")
    assert engine.start(configs(fleet), steps("takeoff\nup 50\nturn left 90\nrecord on\nland"), ca)
    engine.join(10)
    assert engine.state().status == FINISHED
    for drone in fleet:
        assert [(r[1], r[2], r[3]) for r in drone.command_requests()] == [
            ("POST", "/v1/takeoff", None),
            ("POST", "/v1/move/up", {"distance_cm": 50}),
            ("POST", "/v1/turn/left", {"degrees": 90}),
            ("PUT", "/v1/recording", {"enabled": True}),
            ("POST", "/v1/land", None),
        ]
    assert fleet[0].requests[0][4] == KEY1 and fleet[1].requests[0][4] == KEY2
    assert all(r[4] == KEY1 for r in fleet[0].requests) and all(r[4] == KEY2 for r in fleet[1].requests)
    lines = engine.logs()
    # per drone: 5 commands + a status poll before each of the 5 steps + 1 final poll = 11 lines
    assert len(lines) == 22
    assert any(l.endswith("drone 1, 127.0.0.1, takeoff, 200 ok") for l in lines)
    assert any(l.endswith("drone 2, 127.0.0.1, up 50, 200 ok") for l in lines)


def test_forward_and_back_are_sent_to_the_move_endpoint(fleet, certs):
    engine = FleetEngine()
    engine.start(configs(fleet), steps("takeoff\nforward 120\nback 40\nland"), str(certs[0] / "ca.pem"))
    engine.join(10)
    assert engine.state().status == FINISHED
    for drone in fleet:
        assert [(r[1], r[2], r[3]) for r in drone.command_requests()][1:3] == [
            ("POST", "/v1/move/forward", {"distance_cm": 120}),
            ("POST", "/v1/move/back", {"distance_cm": 40}),
        ]
    assert any("drone 1, 127.0.0.1, forward 120, 200 ok" in l for l in engine.logs())


def test_next_step_waits_for_slowest_drone(fleet, certs, monkeypatch):
    real_execute = TelloApiClient.execute

    def execute(self, step):
        if self.drone.port == fleet[1].port and step.kind == "takeoff":
            time.sleep(0.4)            # drone 2 is slow to start its takeoff
        return real_execute(self, step)

    monkeypatch.setattr(TelloApiClient, "execute", execute)
    engine = FleetEngine()
    started = time.monotonic()
    engine.start(configs(fleet), steps("takeoff\nup 30"), str(certs[0] / "ca.pem"))
    engine.join(10)
    assert engine.state().status == FINISHED
    # drone 1 answered takeoff at once but only got "up 30" after drone 2 had finished takeoff
    assert fleet[0].command_requests()[1][0] - started >= 0.4


def test_sleep_timing(fleet, certs):
    engine = FleetEngine()
    started = time.monotonic()
    engine.start(configs(fleet), steps("sleep 0.5\ntakeoff"), str(certs[0] / "ca.pem"))
    engine.join(10)
    assert fleet[0].command_requests()[0][0] - started >= 0.5
    assert engine.state().status == FINISHED


def test_abort_during_sleep_sends_stop_and_skips_rest(fleet, certs):
    engine = FleetEngine()
    ca = str(certs[0] / "ca.pem")
    engine.start(configs(fleet), steps("sleep 30\ntakeoff"), ca)
    assert wait_for(lambda: engine.state().current_step == 1)
    began = time.monotonic()
    engine.abort()
    engine.join(10)
    assert time.monotonic() - began < 5
    assert engine.state().status == ABORTED
    for drone in fleet:
        assert drone.paths() == ["/v1/abort"]
    assert any("abort, 200 ok" in l for l in engine.logs())


def test_abort_during_request_treats_502_as_expected(fleet, certs):
    fleet[0].hold["/v1/move/up"] = threading.Event()
    engine = FleetEngine()
    ca = str(certs[0] / "ca.pem")
    engine.start(configs(fleet), steps("up 100\nland"), ca)
    assert wait_for(fleet[0].entered.is_set)
    engine.abort()                   # run's own drone list is used while running
    engine.join(10)
    state = engine.state()
    assert state.status == ABORTED
    assert "/v1/land" not in fleet[0].paths()
    assert fleet[0].paths()[-1] == "/v1/abort"
    assert not any(s.startswith("failed") for s in state.drone_status)


def test_land_all_while_running_and_when_idle(fleet, certs):
    ca = str(certs[0] / "ca.pem")
    engine = FleetEngine()
    engine.start(configs(fleet), steps("sleep 30\nup 50"), ca)
    assert wait_for(lambda: engine.state().current_step == 1)
    engine.land_all()
    engine.join(10)
    assert engine.state().status == LANDED
    assert all(d.paths() == ["/v1/land"] for d in fleet)

    idle = FleetEngine()
    idle.land_all(configs(fleet), ca)
    assert all(d.paths() == ["/v1/land", "/v1/land"] for d in fleet)


def test_abort_when_idle_uses_given_drones(fleet, certs):
    engine = FleetEngine()
    engine.abort(configs(fleet), str(certs[0] / "ca.pem"))
    assert [d.paths() for d in fleet] == [["/v1/abort"], ["/v1/abort"]]
    assert engine.state().status == "idle"


def test_failure_halts_run_and_nothing_more_is_sent(fleet, certs):
    fleet[1].responses["/v1/takeoff"] = (409, {"detail": "already flying"})
    engine = FleetEngine()
    engine.start(configs(fleet), steps("takeoff\nup 50\nland"), str(certs[0] / "ca.pem"))
    engine.join(10)
    state = engine.state()
    assert state.status == HALTED
    assert "drone 2" in state.message and "drone 1" not in state.message
    assert fleet[0].paths() == ["/v1/takeoff"] and fleet[1].paths() == ["/v1/takeoff"]
    assert any("409 already flying" in l for l in engine.logs())


def test_wrong_key_is_reported_and_halts(fleet, certs):
    bad = [DroneConfig("127.0.0.1", fleet[0].port, "wrong-key-" + "z" * 30), configs(fleet)[1]]
    engine = FleetEngine()
    engine.start(bad, steps("takeoff"), str(certs[0] / "ca.pem"))
    engine.join(10)
    assert engine.state().status == HALTED
    assert any("401" in l for l in engine.logs())


def test_keys_never_in_logs_or_repr(fleet, certs, caplog):
    caplog.set_level(logging.DEBUG)
    engine = FleetEngine()
    cfgs = configs(fleet)
    engine.start(cfgs, steps("takeoff\nland"), str(certs[0] / "ca.pem"))
    engine.join(10)
    engine.abort(cfgs, str(certs[0] / "ca.pem"))
    everything = "\n".join(engine.logs()) + caplog.text + repr(cfgs) + repr(engine.state())
    assert KEY1 not in everything and KEY2 not in everything


def test_tls_verification_is_on(fleet, certs):
    # no CA bundle: the private CA is not in the system store -> must fail, not fall back
    result = TelloApiClient(configs(fleet)[0])._call("POST", "/v1/takeoff", None, 5)
    assert not result.ok and "TLS verification failed" in result.text
    # certificate signed by a different CA -> rejected
    wrong_ca = str(certs[1] / "ca.pem")
    result = TelloApiClient(configs(fleet)[0], wrong_ca)._call("POST", "/v1/takeoff", None, 5)
    assert not result.ok and "TLS verification failed" in result.text
    assert fleet[0].requests == []        # key never reached the server


def test_plain_http_service_is_not_used(fleet, certs):
    # https:// against a port that does not speak TLS must fail rather than downgrade
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]

    def serve_plain():
        conn, _ = s.accept()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        conn.close()

    threading.Thread(target=serve_plain, daemon=True).start()
    try:
        result = TelloApiClient(DroneConfig("127.0.0.1", port, KEY1),
                                str(certs[0] / "ca.pem"))._call("POST", "/v1/abort", None, 1)
    finally:
        s.close()
    assert not result.ok


def test_second_start_while_running_is_refused(fleet, certs):
    engine = FleetEngine()
    ca = str(certs[0] / "ca.pem")
    assert engine.start(configs(fleet), steps("sleep 30"), ca)
    assert not engine.start(configs(fleet), steps("takeoff"), ca)
    engine.abort()
    engine.join(10)
    assert engine.start(configs(fleet), steps("takeoff"), ca)      # restarts from step 1
    engine.join(10)
    assert engine.state().status == FINISHED


def test_no_drones_or_steps_does_not_start():
    engine = FleetEngine()
    assert not engine.start([], steps("takeoff"))
    assert not engine.start([DroneConfig("127.0.0.1", 1, "k")], [])


STATUS_FLYING = {"connected": True, "flying": True, "recording": False, "recording_file": None, "battery": 87}
STATUS_GROUND = {"connected": True, "flying": False, "recording": False, "recording_file": None, "battery": 42}


def test_read_status_returns_flying_and_battery_per_drone(fleet, certs):
    fleet[0].responses["/v1/status"] = (200, STATUS_FLYING)
    fleet[1].responses["/v1/status"] = (200, STATUS_GROUND)
    engine = FleetEngine()
    infos = engine.read_status(configs(fleet), str(certs[0] / "ca.pem"))
    assert infos == [DroneInfo(True, True, 87, True), DroneInfo(True, False, 42, True)]
    for drone, key in zip(fleet, (KEY1, KEY2)):
        assert [(r[1], r[2], r[3], r[4]) for r in drone.requests] == [("GET", "/v1/status", None, key)]
    lines = engine.logs()
    assert any(l.endswith("drone 1, 127.0.0.1, status, 200 flying, battery 87%") for l in lines)
    assert any(l.endswith("drone 2, 127.0.0.1, status, 200 on ground, battery 42%") for l in lines)
    assert engine.state().status == "idle"          # reading status is not a run


def test_read_status_uses_given_drone_numbers(fleet, certs):
    engine = FleetEngine()
    engine.read_status(configs(fleet)[1:], str(certs[0] / "ca.pem"), [2])
    assert any("drone 2, 127.0.0.1, status" in l for l in engine.logs())


def test_read_status_wrong_key_and_unreachable(fleet, certs):
    bad = [DroneConfig("127.0.0.1", fleet[0].port, "wrong-key-" + "z" * 30)]
    infos = FleetEngine().read_status(bad, str(certs[0] / "ca.pem"))
    assert infos == [DroneInfo(reachable=False)]
    # no CA bundle -> TLS verification fails, reported as unreachable
    assert FleetEngine().read_status(configs(fleet)[:1]) == [DroneInfo(reachable=False)]
    assert FleetEngine().read_status([]) == []


def test_read_status_works_while_a_run_is_in_progress(fleet, certs):
    fleet[0].responses["/v1/status"] = (200, STATUS_FLYING)
    engine = FleetEngine()
    ca = str(certs[0] / "ca.pem")
    engine.start(configs(fleet), steps("sleep 30"), ca)
    assert wait_for(lambda: engine.state().current_step == 1)
    assert engine.read_status(configs(fleet)[:1], ca)[0].flying is True
    assert engine.is_running
    engine.abort()
    engine.join(10)


def test_info_from_result_is_defensive():
    ok = lambda data: CallResult(True, 200, "x", data)
    assert info_from_result(ok({"flying": True, "battery": 55, "connected": True})) == DroneInfo(True, True, 55, True)
    assert info_from_result(ok({"flying": False, "battery": None})).battery is None
    assert info_from_result(ok({"flying": False, "battery": 101})).battery is None
    assert info_from_result(ok({"flying": False, "battery": True})).battery is None
    assert info_from_result(ok({"flying": "yes"})) == DroneInfo(reachable=False)
    assert info_from_result(ok(None)) == DroneInfo(reachable=False)
    assert info_from_result(CallResult(False, 401, "401 x", None)) == DroneInfo(reachable=False)


def status_paths(drone):
    return [r[2] for r in drone.requests]


def test_status_is_sent_to_every_drone_before_every_step(fleet, certs):
    fleet[0].responses["/v1/status"] = (200, STATUS_FLYING)
    fleet[1].responses["/v1/status"] = (200, STATUS_GROUND)
    engine = FleetEngine()
    engine.start(configs(fleet), steps("takeoff\nsleep 0.1\nup 50\nland"), str(certs[0] / "ca.pem"))
    engine.join(10)
    assert engine.state().status == FINISHED
    for drone in fleet:
        # one status before each of the 4 rows (sleep included) and one at the end
        assert status_paths(drone) == ["/v1/status", "/v1/takeoff", "/v1/status", "/v1/status",
                                       "/v1/move/up", "/v1/status", "/v1/land", "/v1/status"]
        assert all(r[4] in (KEY1, KEY2) for r in drone.requests)
    assert sum("status, 200" in l for l in engine.logs()) == 10


def test_status_results_update_the_latest_known_info(fleet, certs):
    fleet[0].responses["/v1/status"] = (200, STATUS_FLYING)
    fleet[1].responses["/v1/status"] = (200, STATUS_GROUND)
    engine = FleetEngine()
    assert engine.info_for("127.0.0.1", fleet[0].port) is None
    engine.start(configs(fleet), steps("takeoff"), str(certs[0] / "ca.pem"))
    engine.join(10)
    assert engine.info_for("127.0.0.1", fleet[0].port) == DroneInfo(True, True, 87, True)
    assert engine.info_for(" 127.0.0.1 ", str(fleet[1].port)) == DroneInfo(True, False, 42, True)
    assert engine.info_for("127.0.0.1", 1) is None


def test_status_value_changes_during_the_run(fleet, certs):
    fleet[0].responses["/v1/status"] = (200, STATUS_GROUND)
    engine = FleetEngine()
    ca = str(certs[0] / "ca.pem")
    engine.start(configs(fleet)[:1], steps("sleep 0.3\nsleep 30"), ca)
    assert wait_for(lambda: engine.info_for("127.0.0.1", fleet[0].port) is not None)
    assert engine.info_for("127.0.0.1", fleet[0].port).flying is False
    fleet[0].responses["/v1/status"] = (200, STATUS_FLYING)         # the drone took off meanwhile
    assert wait_for(lambda: engine.info_for("127.0.0.1", fleet[0].port).flying is True)
    engine.abort()
    engine.join(10)


def test_unreachable_status_does_not_stop_the_run_but_the_step_does(fleet, certs):
    fleet[0].responses["/v1/status"] = (500, {"detail": "boom"})
    engine = FleetEngine()
    engine.start(configs(fleet)[:1], steps("takeoff\nland"), str(certs[0] / "ca.pem"))
    engine.join(10)
    assert engine.state().status == FINISHED                       # status is informational only
    assert engine.info_for("127.0.0.1", fleet[0].port) == DroneInfo(reachable=False)
    assert "/v1/takeoff" in status_paths(fleet[0]) and "/v1/land" in status_paths(fleet[0])


def test_abort_after_status_poll_sends_no_more_steps(fleet, certs):
    engine = FleetEngine()
    ca = str(certs[0] / "ca.pem")
    engine.start(configs(fleet), steps("sleep 30\ntakeoff"), ca)
    assert wait_for(lambda: engine.state().current_step == 1 and "/v1/status" in status_paths(fleet[0]))
    engine.abort()
    engine.join(10)
    assert engine.state().status == ABORTED
    assert "/v1/takeoff" not in status_paths(fleet[0])
    assert status_paths(fleet[0]).count("/v1/status") == 1         # no end-of-run poll after an abort


def test_status_polling_keeps_keys_out_of_logs(fleet, certs):
    engine = FleetEngine()
    engine.start(configs(fleet), steps("takeoff"), str(certs[0] / "ca.pem"))
    engine.join(10)
    assert KEY1 not in "\n".join(engine.logs()) and KEY2 not in "\n".join(engine.logs())


def test_check_drone():
    cfg, errs = check_drone(" 192.168.4.11 ", "8000", "secret")
    assert errs == [] and cfg.ip == "192.168.4.11" and cfg.port == 8000
    assert "secret" not in repr(cfg)
    for ip in ("10.0.0.1", "192.168.22.5", "192.168.4.256", "", "abc", "192.168.4.1/24"):
        assert check_drone(ip, "8000", "k")[0] is None
    for port in ("", "0", "65536", "x", "-1", "80 80"):
        assert check_drone("192.168.4.5", port, "k")[0] is None
    assert check_drone("192.168.4.5", "8000", "")[0] is None
    assert check_drone("192.168.4.5", "8000", "k\ney")[0] is None


def test_check_ca_bundle(tmp_path):
    assert check_ca_bundle("") is None
    assert check_ca_bundle(str(tmp_path / "missing.pem"))
    pem = tmp_path / "ca.pem"
    pem.write_text("x")
    assert check_ca_bundle(str(pem)) is None
