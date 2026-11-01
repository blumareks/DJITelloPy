"""Thread-safe wrapper around :class:`djitellopy.Tello` used by the REST API."""
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

import av
from djitellopy import Tello, TelloException

logger = logging.getLogger("tello_api")

# Names of the Tello methods used for each supported direction.
_MOVES = {
    "up": "move_up",
    "down": "move_down",
    "left": "move_left",
    "right": "move_right",
}
_TURNS = {
    "right": "rotate_clockwise",
    "left": "rotate_counter_clockwise",
}


class Conflict(Exception):
    """The request cannot be performed in the current state (busy, not flying, ...)."""


class DroneCommandFailed(Exception):
    """The drone did not accept or did not answer a command."""


class VideoRecorder:
    """Encodes frames from a ``BackgroundFrameRead`` (created with ``with_queue=True``) to an H.264 mp4."""

    def __init__(self, reader, path: Path, fps: int = 30):
        self._reader = reader
        self.path = path
        self._fps = fps
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="video-recorder", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=10)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self):
        # Fragmented mp4 stays playable if the process or the Pi loses power mid-recording.
        container = av.open(str(self.path), "w", options={"movflags": "frag_keyframe+empty_moov"})
        stream = None
        shape = None
        started = 0.0
        last_pts = -1
        try:
            while not self._stop.is_set():
                frame = self._reader.get_queued_frame()
                if frame is None:
                    time.sleep(0.005)
                    continue

                if stream is None:
                    shape = frame.shape
                    stream = container.add_stream("libx264", rate=self._fps)
                    stream.height, stream.width = shape[0], shape[1]
                    stream.pix_fmt = "yuv420p"
                    stream.options = {"preset": "veryfast", "crf": "28"}
                    # Millisecond timestamps keep the playback speed correct even if frames are dropped.
                    stream.codec_context.time_base = Fraction(1, 1000)
                    started = time.monotonic()
                elif frame.shape != shape:
                    continue

                video_frame = av.VideoFrame.from_ndarray(frame, format="rgb24")
                last_pts = max(last_pts + 1, int((time.monotonic() - started) * 1000))
                video_frame.pts = last_pts
                video_frame.time_base = Fraction(1, 1000)
                for packet in stream.encode(video_frame):
                    container.mux(packet)

            if stream is not None:
                for packet in stream.encode():
                    container.mux(packet)
        except Exception:
            logger.exception("Video recording failed")
        finally:
            container.close()


class DroneController:
    """Serialises all access to one Tello and tracks flight/recording state.

    Tello commands share a single UDP response channel, so only one command may run
    at a time. A request that arrives while another is running gets :class:`Conflict`.
    """

    def __init__(
        self,
        recordings_dir: Path = Path("recordings"),
        tello_factory: Callable[[], Tello] = Tello,
        keepalive_interval: float = 5.0,
    ):
        self._recordings_dir = recordings_dir
        self._factory = tello_factory
        self._keepalive_interval = keepalive_interval
        self._tello: Optional[Tello] = None
        self._connected = False
        self._recorder: Optional[VideoRecorder] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._keepalive_thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self):
        """Start the keepalive thread (the Tello lands by itself after 15 s without commands)."""
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, name="keepalive", daemon=True)
        self._keepalive_thread.start()

    def shutdown(self):
        self._stop.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=self._keepalive_interval + 1)
        with self._lock:
            if self._recorder is not None:
                self._recorder.stop()
                self._recorder = None
            if self._tello is not None:
                self._tello.end()  # lands if flying, stops the video stream
                self._tello = None
                self._connected = False

    def _keepalive_loop(self):
        while not self._stop.wait(self._keepalive_interval):
            tello = self._tello
            if tello is None or not tello.is_flying:
                continue
            if not self._lock.acquire(blocking=False):
                continue  # a command is running, which counts as activity
            try:
                tello.send_keepalive()
            except TelloException:
                logger.warning("Keepalive failed", exc_info=True)
            finally:
                self._lock.release()

    # -- helpers -----------------------------------------------------------------------------

    @contextmanager
    def _session(self):
        if not self._lock.acquire(blocking=False):
            raise Conflict("Another command is still running")
        try:
            if self._tello is None:
                self._tello = self._factory()
            if not self._connected:
                self._tello.connect()
                self._connected = True
            yield self._tello
        except TelloException as exc:
            logger.error("Drone command failed: %s", exc)
            raise DroneCommandFailed() from exc
        finally:
            self._lock.release()

    def _recording_active(self) -> bool:
        return self._recorder is not None and self._recorder.is_alive()

    # -- commands ----------------------------------------------------------------------------

    def takeoff(self):
        with self._session() as tello:
            if tello.is_flying:
                raise Conflict("Drone is already flying")
            tello.takeoff()

    def land(self):
        # Always sent, even if we think the drone is on the ground: our flag could be stale.
        with self._session() as tello:
            tello.land()

    def move(self, direction: str, distance_cm: int):
        with self._session() as tello:
            if not tello.is_flying:
                raise Conflict("Drone is not flying")
            getattr(tello, _MOVES[direction])(distance_cm)

    def turn(self, direction: str, degrees: int):
        with self._session() as tello:
            if not tello.is_flying:
                raise Conflict("Drone is not flying")
            getattr(tello, _TURNS[direction])(degrees)

    def set_recording(self, enabled: bool) -> Optional[str]:
        """Start or stop recording. Returns the file name of the active/finished recording, if any."""
        with self._session() as tello:
            if enabled:
                if self._recording_active():
                    return self._recorder.path.name
                self._recordings_dir.mkdir(parents=True, exist_ok=True)
                path = self._recordings_dir / "tello-{}.mp4".format(datetime.now().strftime("%Y%m%d-%H%M%S"))
                try:
                    tello.streamon()
                    reader = tello.get_frame_read(with_queue=True)
                except TelloException:
                    try:
                        tello.streamoff()
                    except TelloException:
                        pass
                    raise
                self._recorder = VideoRecorder(reader, path)
                self._recorder.start()
                return path.name

            recorder, self._recorder = self._recorder, None
            if recorder is None:
                return None
            recorder.stop()
            tello.streamoff()
            return recorder.path.name

    def status(self) -> dict:
        battery = None
        if self._tello is not None:
            try:
                battery = self._tello.get_battery()
            except TelloException:
                pass
        recorder = self._recorder if self._recording_active() else None
        return {
            "connected": self._connected,
            "flying": bool(self._tello is not None and self._tello.is_flying),
            "recording": recorder is not None,
            "recording_file": recorder.path.name if recorder else None,
            "battery": battery,
        }
