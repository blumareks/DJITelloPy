"""Abort behaviour with the real djitellopy.Tello class and a fake UDP socket (no drone, no network)."""
import logging
import threading
import time

import pytest
from djitellopy import Tello
import djitellopy.tello as tello_module

from tello_api.drone import DroneController, DroneCommandFailed

HOST = "192.168.99.9"


@pytest.fixture
def wire(monkeypatch):
    sent = []

    class FakeSocket:
        def sendto(self, data, _addr):
            sent.append(data.decode())

    monkeypatch.setattr(tello_module, "client_socket", FakeSocket(), raising=False)
    monkeypatch.setattr(tello_module, "threads_initialized", True)  # do not bind real UDP ports
    logging.getLogger("djitellopy").setLevel(logging.CRITICAL)
    logging.getLogger("tello_api").setLevel(logging.CRITICAL)
    yield sent
    tello_module.drones.pop(HOST, None)


def reply(text, after=0.0):
    def _put():
        time.sleep(after)
        tello_module.drones[HOST]["responses"].append(text.encode())
    threading.Thread(target=_put, daemon=True).start()


@pytest.fixture
def controller(wire, tmp_path):
    def make():
        t = Tello(host=HOST, retry_count=3)
        t.RESPONSE_TIMEOUT = 2
        return t

    c = DroneController(recordings_dir=tmp_path, tello_factory=make, keepalive_interval=0.1)
    c._tello = make()
    c._wrap_tello(c._tello)
    c._connected = True
    tello_module.drones[HOST]["state"] = {"bat": 80}
    c._tello.is_flying = True
    yield c
    c._stop.set()
    c._tello.is_flying = False  # else Tello.__del__ tries to land through the removed fake socket


def test_hovering_drone_still_gets_keepalives_after_abort(controller, wire):
    """A leftover abort flag must not stop keepalives, or the drone auto-lands after 15 s."""
    controller.abort()
    wire.clear()
    answer = threading.Event()

    def auto_ok():
        while not answer.is_set():
            if "keepalive" in wire and not tello_module.drones[HOST]["responses"]:
                tello_module.drones[HOST]["responses"].append(b"ok")
            time.sleep(0.01)

    threading.Thread(target=auto_ok, daemon=True).start()
    controller.start()
    time.sleep(0.6)
    answer.set()
    assert wire.count("keepalive") >= 1


def test_aborted_movement_is_not_resent(controller, wire):
    result = {}

    def mover():
        try:
            controller.move("up", 50)
        except DroneCommandFailed:
            result["r"] = "failed"

    t = threading.Thread(target=mover)
    t.start()
    time.sleep(0.4)
    controller.abort()
    t.join(5)
    assert result["r"] == "failed"
    assert wire.count("up 50") == 1 and wire.count("stop") == 1


def test_stale_reply_after_abort_is_not_taken_for_next_command(controller, wire):
    t = threading.Thread(target=lambda: _swallow(controller.move, "up", 50))
    t.start()
    time.sleep(0.4)
    controller.abort()
    t.join(5)
    reply("ok")            # late reply to 'stop'
    time.sleep(0.1)
    reply("error", 0.2)    # genuine reply to the next command is an error
    with pytest.raises(DroneCommandFailed):
        controller.move("left", 50)


def _swallow(fn, *args):
    try:
        fn(*args)
    except DroneCommandFailed:
        pass
