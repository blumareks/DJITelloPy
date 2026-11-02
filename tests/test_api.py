import threading
import time

import numpy as np
import pytest
from djitellopy import TelloException
from fastapi.testclient import TestClient

from tello_api.app import create_app
from tello_api.drone import DroneController, VideoRecorder

KEY = "k" * 40
HEADERS = {"X-API-Key": KEY}


class FakeReader:
    def __init__(self, n_frames=0, size=(64, 48)):
        w, h = size
        self._frames = [np.full((h, w, 3), i * 5 % 255, dtype=np.uint8) for i in range(n_frames)]

    def get_queued_frame(self):
        return self._frames.pop(0) if self._frames else None


class FakeTello:
    def __init__(self):
        self.calls = []
        self.is_flying = False
        self.fail_next = False
        self.block = None  # threading.Event the next command waits for
        self.reader = FakeReader()

    def _do(self, name, *args):
        if self.block is not None:
            self.block.wait(5)
        if self.fail_next:
            self.fail_next = False
            raise TelloException("secret internal detail 192.168.10.1")
        self.calls.append((name,) + args)

    def connect(self):
        self._do("connect")

    def takeoff(self):
        self._do("takeoff")
        self.is_flying = True

    def land(self):
        self._do("land")
        self.is_flying = False

    def stop(self):
        self._do("stop")

    def send_command_without_return(self, command):
        if self.fail_next:
            self.fail_next = False
            raise TelloException("secret internal detail 192.168.10.1")
        self.calls.append((command,))
        if command == "land":
            self.is_flying = False

    def move_up(self, x): self._do("up", x)
    def move_down(self, x): self._do("down", x)
    def move_left(self, x): self._do("left", x)
    def move_right(self, x): self._do("right", x)
    def rotate_clockwise(self, x): self._do("cw", x)
    def rotate_counter_clockwise(self, x): self._do("ccw", x)
    def streamon(self): self._do("streamon")
    def streamoff(self): self._do("streamoff")
    def send_keepalive(self): self._do("keepalive")
    def get_battery(self): return 77

    def get_frame_read(self, with_queue=False, max_queue_len=32):
        assert with_queue
        return self.reader

    def end(self):
        self.calls.append(("end",))


@pytest.fixture
def tello():
    return FakeTello()


@pytest.fixture
def client(tello, tmp_path):
    controller = DroneController(recordings_dir=tmp_path, tello_factory=lambda: tello, keepalive_interval=0.05)
    with TestClient(create_app(controller, KEY)) as c:
        c.tello = tello
        c.recordings = tmp_path
        yield c


# -- authentication ----------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": KEY + "x"}])
def test_rejects_missing_or_wrong_key(client, headers):
    assert client.post("/v1/takeoff", headers=headers).status_code == 401
    assert client.post("/v1/abort", headers=headers).status_code == 401
    assert client.post("/v1/land", headers=headers).status_code == 401
    assert client.get("/v1/status", headers=headers).status_code == 401
    assert client.tello.calls == []


# -- flight commands ---------------------------------------------------------------------------

def test_takeoff_and_land(client):
    assert client.post("/v1/takeoff", headers=HEADERS).json() == {"status": "ok", "command": "takeoff"}
    assert client.get("/v1/status", headers=HEADERS).json()["flying"] is True
    assert client.post("/v1/land", headers=HEADERS).status_code == 200
    assert client.get("/v1/status", headers=HEADERS).json()["flying"] is False
    assert [c[0] for c in client.tello.calls if c[0] in ("takeoff", "land")] == ["takeoff", "land"]


def test_takeoff_twice_conflicts(client):
    client.post("/v1/takeoff", headers=HEADERS)
    assert client.post("/v1/takeoff", headers=HEADERS).status_code == 409


@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
def test_move(client, direction):
    client.post("/v1/takeoff", headers=HEADERS)
    r = client.post("/v1/move/" + direction, json={"distance_cm": 50}, headers=HEADERS)
    assert r.status_code == 200
    assert (direction, 50) in client.tello.calls


@pytest.mark.parametrize("direction,cmd", [("right", "cw"), ("left", "ccw")])
def test_turn(client, direction, cmd):
    client.post("/v1/takeoff", headers=HEADERS)
    r = client.post("/v1/turn/" + direction, json={"degrees": 90}, headers=HEADERS)
    assert r.status_code == 200
    assert (cmd, 90) in client.tello.calls


def test_move_and_turn_require_flying(client):
    assert client.post("/v1/move/up", json={"distance_cm": 50}, headers=HEADERS).status_code == 409
    assert client.post("/v1/turn/left", json={"degrees": 90}, headers=HEADERS).status_code == 409
    assert not any(c[0] in ("up", "ccw") for c in client.tello.calls)


@pytest.mark.parametrize("body", [
    {}, {"distance_cm": 19}, {"distance_cm": 501}, {"distance_cm": "x"}, {"distance_cm": 50.5},
    {"distance_cm": 50, "extra": 1},
])
def test_move_validation(client, body):
    client.post("/v1/takeoff", headers=HEADERS)
    assert client.post("/v1/move/up", json=body, headers=HEADERS).status_code == 422


@pytest.mark.parametrize("body", [{}, {"degrees": 0}, {"degrees": 361}, {"degrees": -5}, {"degrees": "a"}])
def test_turn_validation(client, body):
    client.post("/v1/takeoff", headers=HEADERS)
    assert client.post("/v1/turn/left", json=body, headers=HEADERS).status_code == 422


def test_unknown_direction(client):
    assert client.post("/v1/move/forward", json={"distance_cm": 50}, headers=HEADERS).status_code == 422
    assert client.post("/v1/turn/up", json={"degrees": 5}, headers=HEADERS).status_code == 422


def test_drone_error_is_generic_502(client):
    client.tello.fail_next = False
    client.post("/v1/takeoff", headers=HEADERS)
    client.tello.fail_next = True
    r = client.post("/v1/move/up", json={"distance_cm": 50}, headers=HEADERS)
    assert r.status_code == 502
    assert r.json() == {"detail": "Drone command failed"}
    assert "192.168" not in r.text


def test_concurrent_command_gets_409(client):
    client.post("/v1/takeoff", headers=HEADERS)
    client.tello.block = threading.Event()
    result = {}

    def slow():
        result["r"] = client.post("/v1/move/up", json={"distance_cm": 50}, headers=HEADERS)

    t = threading.Thread(target=slow)
    t.start()
    time.sleep(0.3)
    try:
        assert client.post("/v1/turn/left", json={"degrees": 10}, headers=HEADERS).status_code == 409
    finally:
        client.tello.block.set()
        t.join()
    assert result["r"].status_code == 200


def test_abort_idle(client):
    client.post("/v1/takeoff", headers=HEADERS)
    r = client.post("/v1/abort", headers=HEADERS)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "command": "abort"}
    assert ("stop",) in client.tello.calls


def test_abort_mid_move(client):
    client.post("/v1/takeoff", headers=HEADERS)
    client.tello.block = threading.Event()
    result = {}

    def slow():
        result["r"] = client.post("/v1/move/up", json={"distance_cm": 50}, headers=HEADERS)

    t = threading.Thread(target=slow)
    t.start()
    time.sleep(0.3)
    try:
        r_abort = client.post("/v1/abort", headers=HEADERS)
        assert r_abort.status_code == 200
        assert r_abort.json() == {"status": "ok", "command": "abort"}
        assert ("stop",) in client.tello.calls
    finally:
        client.tello.block.set()
        t.join()
    assert result["r"].status_code == 502
    assert result["r"].json() == {"detail": "Drone command failed"}

    # Next command works afterwards
    r_next = client.post("/v1/move/down", json={"distance_cm": 50}, headers=HEADERS)
    assert r_next.status_code == 200
    assert ("down", 50) in client.tello.calls


def test_land_mid_move(client):
    client.post("/v1/takeoff", headers=HEADERS)
    client.tello.block = threading.Event()
    result = {}

    def slow():
        result["r"] = client.post("/v1/move/up", json={"distance_cm": 50}, headers=HEADERS)

    t = threading.Thread(target=slow)
    t.start()
    time.sleep(0.3)
    try:
        r_land = client.post("/v1/land", headers=HEADERS)
        assert r_land.status_code == 200
        assert r_land.json() == {"status": "ok", "command": "land"}
        assert ("land",) in client.tello.calls
        assert client.get("/v1/status", headers=HEADERS).json()["flying"] is False
    finally:
        client.tello.block.set()
        t.join()
    assert result["r"].status_code == 502


def test_no_resend_after_abort(tmp_path):
    class RetryingTello(FakeTello):
        def __init__(self):
            super().__init__()
            self.retry_count = 3
            self.RESPONSE_TIMEOUT = 7
            self.TIME_BTW_COMMANDS = 0
            self.last_received_command_timestamp = 0
            self.address = ("127.0.0.1", 8889)
            self.LOGGER = type("Logger", (), {
                "info": lambda *a: None, "debug": lambda *a: None,
                "warning": lambda *a: None, "error": lambda *a: None,
            })()
            self._responses = []

        def get_own_udp_object(self):
            return {"responses": self._responses, "state": {}}

        def raise_result_error(self, cmd, resp):
            raise TelloException(f"Command '{cmd}' failed: {resp}")

        def send_command_with_return(self, command: str, timeout: int = 7) -> str:
            self._do(command)
            return "error"

        def send_control_command(self, command: str, timeout: int = 7) -> bool:
            return False

    tello = RetryingTello()
    controller = DroneController(recordings_dir=tmp_path, tello_factory=lambda: tello)
    with TestClient(create_app(controller, KEY)) as c:
        c.post("/v1/takeoff", headers=HEADERS)
        tello.block = threading.Event()
        result = {}

        def slow():
            result["r"] = c.post("/v1/move/up", json={"distance_cm": 50}, headers=HEADERS)

        t = threading.Thread(target=slow)
        t.start()
        time.sleep(0.3)
        try:
            c.post("/v1/abort", headers=HEADERS)
        finally:
            tello.block.set()
            t.join()
        assert result["r"].status_code == 502
        up_calls = [call for call in tello.calls if call[0] in ("up 50", "up")]
        assert len(up_calls) == 1


def test_keepalive_sent_while_flying(client):
    client.post("/v1/takeoff", headers=HEADERS)
    time.sleep(0.4)
    assert ("keepalive",) in client.tello.calls


def test_shutdown_lands_drone(tello, tmp_path):
    controller = DroneController(recordings_dir=tmp_path, tello_factory=lambda: tello)
    with TestClient(create_app(controller, KEY)) as c:
        c.post("/v1/takeoff", headers=HEADERS)
    assert ("end",) in tello.calls


# -- recording ---------------------------------------------------------------------------------

def test_recording_on_off(client):
    client.tello.reader = FakeReader(n_frames=30)
    r = client.put("/v1/recording", json={"enabled": True}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["recording"] is True
    filename = r.json()["file"]
    assert filename.startswith("tello-") and filename.endswith(".mp4")

    status = client.get("/v1/status", headers=HEADERS).json()
    assert status["recording"] is True and status["recording_file"] == filename and status["battery"] == 77

    # starting again is idempotent and returns the same file
    assert client.put("/v1/recording", json={"enabled": True}, headers=HEADERS).json()["file"] == filename

    time.sleep(0.5)
    r = client.put("/v1/recording", json={"enabled": False}, headers=HEADERS)
    assert r.json() == {"recording": False, "file": filename}
    assert ("streamon",) in client.tello.calls and ("streamoff",) in client.tello.calls
    assert (client.recordings / filename).stat().st_size > 0
    assert client.get("/v1/status", headers=HEADERS).json()["recording"] is False


def test_stop_recording_when_not_recording(client):
    r = client.put("/v1/recording", json={"enabled": False}, headers=HEADERS)
    assert r.status_code == 200 and r.json() == {"recording": False, "file": None}


def test_recording_validation(client):
    assert client.put("/v1/recording", json={"enabled": "maybe"}, headers=HEADERS).status_code == 422
    assert client.put("/v1/recording", json={}, headers=HEADERS).status_code == 422


def test_video_recorder_writes_playable_h264(tmp_path):
    import av

    path = tmp_path / "out.mp4"
    rec = VideoRecorder(FakeReader(n_frames=20), path)
    rec.start()
    time.sleep(0.5)
    rec.stop()

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        assert stream.codec_context.name == "h264"
        assert (stream.width, stream.height) == (64, 48)
        assert sum(1 for _ in container.decode(video=0)) > 0


# -- OpenAPI / Swagger UI ----------------------------------------------------------------------

def test_swagger_ui_is_served_locally_without_credentials(client):
    page = client.get("/docs")
    assert page.status_code == 200
    assert "swagger-ui" in page.text.lower()
    # nothing may be fetched from the internet: the Pi is usually only on the Tello's Wi-Fi
    assert "http://" not in page.text and "https://" not in page.text.replace("https://fastapi", "")
    assert "cdn.jsdelivr" not in page.text
    for asset in ("swagger-ui-bundle.js", "swagger-ui.css", "favicon-32x32.png"):
        assert asset in page.text or asset == "favicon-32x32.png"
        r = client.get("/static/swagger-ui/" + asset)
        assert r.status_code == 200 and len(r.content) > 100


def test_openapi_spec_describes_every_endpoint(client):
    spec = client.get("/openapi.json").json()
    expected = {
        ("post", "/v1/takeoff"), ("post", "/v1/abort"), ("post", "/v1/land"),
        ("post", "/v1/move/{direction}"), ("post", "/v1/turn/{direction}"),
        ("put", "/v1/recording"), ("get", "/v1/status"),
    }
    found = {(m, p) for p, ops in spec["paths"].items() for m in ops}
    assert found == expected  # the docs routes themselves are not part of the API

    assert spec["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey", "in": "header", "name": "X-API-Key",
        "description": "Value of the server's `API_KEY` environment variable",
    }
    for (m, p) in expected:
        op = spec["paths"][p][m]
        assert op["summary"] and op["description"] and op["tags"], (m, p)
        assert op["security"] == [{"APIKeyHeader": []}], (m, p)
        assert {"200", "401", "409", "502"} <= set(op["responses"]), (m, p)

    schemas = spec["components"]["schemas"]
    move_ref = spec["paths"]["/v1/move/{direction}"]["post"]["parameters"][0]["schema"]["$ref"]
    turn_ref = spec["paths"]["/v1/turn/{direction}"]["post"]["parameters"][0]["schema"]["$ref"]
    assert schemas[move_ref.rsplit("/", 1)[1]]["enum"] == ["up", "down", "left", "right"]
    assert schemas[turn_ref.rsplit("/", 1)[1]]["enum"] == ["left", "right"]

    move_body = spec["components"]["schemas"]["MoveRequest"]["properties"]["distance_cm"]
    assert (move_body["minimum"], move_body["maximum"]) == (20, 500)
    turn_body = spec["components"]["schemas"]["TurnRequest"]["properties"]["degrees"]
    assert (turn_body["minimum"], turn_body["maximum"]) == (1, 360)


def test_documented_error_bodies_match_reality(client):
    unauthorized = client.post("/v1/takeoff")
    conflict = client.post("/v1/move/up", json={"distance_cm": 50}, headers=HEADERS)
    for r in (unauthorized, conflict):
        assert set(r.json()) == {"detail"}


def test_committed_openapi_file_is_up_to_date(client):
    import json
    from tello_api.export_openapi import OUTPUT
    assert json.loads(OUTPUT.read_text()) == client.get("/openapi.json").json(), \
        "run: python -m tello_api.export_openapi"
