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
        self._aborted = threading.Event()
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
                # Lock held means no command is in flight, so a leftover abort flag (set by
                # POST /v1/abort while hovering) must not block the keepalive; otherwise the
                # hovering drone would auto-land after 15 s without anyone asking for it.
                self._aborted.clear()
                tello.send_keepalive()
            except TelloException:
                logger.warning("Keepalive failed", exc_info=True)
            finally:
                self._lock.release()

    # -- helpers -----------------------------------------------------------------------------

    def _drain_responses(self):
        if self._tello is not None and hasattr(self._tello, "get_own_udp_object"):
            try:
                udp_obj = self._tello.get_own_udp_object()
                if isinstance(udp_obj, dict) and "responses" in udp_obj:
                    udp_obj["responses"].clear()
            except Exception:
                pass

    def _wrap_tello(self, tello):
        """Wrap command sending methods to respect self._aborted without retrying after abort."""
        if not hasattr(tello, "send_control_command") or not hasattr(tello, "send_command_with_return"):
            return

        def wrapped_send_command_with_return(command: str, timeout: int = getattr(tello, "RESPONSE_TIMEOUT", 7)) -> str:
            if self._aborted.is_set():
                raise TelloException(f"Command '{command}' aborted")

            diff = time.time() - tello.last_received_command_timestamp
            if diff < tello.TIME_BTW_COMMANDS:
                tello.LOGGER.debug("Waiting {} seconds to execute command: {}...".format(diff, command))
                time.sleep(diff)

            if self._aborted.is_set():
                raise TelloException(f"Command '{command}' aborted")

            tello.LOGGER.info("Send command: '{}'".format(command))
            timestamp = time.time()

            from djitellopy.tello import client_socket
            client_socket.sendto(command.encode("utf-8"), tello.address)

            responses = tello.get_own_udp_object()["responses"]

            while not responses:
                if self._aborted.is_set():
                    raise TelloException(f"Command '{command}' aborted")
                if time.time() - timestamp > timeout:
                    message = "Aborting command '{}'. Did not receive a response after {} seconds".format(command, timeout)
                    tello.LOGGER.warning(message)
                    return message
                time.sleep(0.05)

            if self._aborted.is_set():
                raise TelloException(f"Command '{command}' aborted")

            tello.last_received_command_timestamp = time.time()
            first_response = responses.pop(0)
            try:
                response = first_response.decode("utf-8")
            except UnicodeDecodeError as e:
                tello.LOGGER.error(e)
                return "response decode error"
            response = response.rstrip("\r\n")
            tello.LOGGER.info("Response {}: '{}'".format(command, response))
            return response

        def wrapped_send_control_command(command: str, timeout: int = getattr(tello, "RESPONSE_TIMEOUT", 7)) -> bool:
            if self._aborted.is_set():
                raise TelloException(f"Command '{command}' aborted")
            response = "max retries exceeded"
            for i in range(0, tello.retry_count):
                if self._aborted.is_set():
                    raise TelloException(f"Command '{command}' aborted")
                response = tello.send_command_with_return(command, timeout=timeout)
                if self._aborted.is_set():
                    raise TelloException(f"Command '{command}' aborted")
                if "ok" in response.lower():
                    return True
                tello.LOGGER.debug("Command attempt #{} failed for command: '{}'".format(i, command))

            tello.raise_result_error(command, response)
            return False

        tello.send_command_with_return = wrapped_send_command_with_return
        tello.send_control_command = wrapped_send_control_command

    @contextmanager
    def _session(self):
        if not self._lock.acquire(blocking=False):
            raise Conflict("Another command is still running")
        try:
            self._aborted.clear()
            if self._tello is None:
                self._tello = self._factory()
                self._wrap_tello(self._tello)
            if not self._connected:
                self._tello.connect()
                self._connected = True
            self._drain_responses()
            yield self._tello
            if self._aborted.is_set():
                raise TelloException("Command aborted")
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

    def abort(self):
        """Hover immediately. Can be called at any time, even while another command runs."""
        self._aborted.set()
        tello = self._tello
        if tello is not None:
            if hasattr(tello, "send_command_without_return"):
                tello.send_command_without_return("stop")
            elif hasattr(tello, "stop"):
                tello.stop()

    def land(self):
        # Always sent, even if we think the drone is on the ground: our flag could be stale.
        # If another command is currently running, preempt it without waiting for return.
        if not self._lock.acquire(blocking=False):
            self._aborted.set()
            tello = self._tello
            if tello is not None:
                if hasattr(tello, "send_command_without_return"):
                    tello.send_command_without_return("land")
                elif hasattr(tello, "land"):
                    tello.land()
                if hasattr(tello, "is_flying"):
                    tello.is_flying = False
            return

        try:
            self._aborted.clear()
            if self._tello is None:
                self._tello = self._factory()
                self._wrap_tello(self._tello)
            if not self._connected:
                self._tello.connect()
                self._connected = True
            self._drain_responses()
            self._tello.land()
        except TelloException as exc:
            logger.error("Drone command failed: %s", exc)
            raise DroneCommandFailed() from exc
        finally:
            self._lock.release()

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
