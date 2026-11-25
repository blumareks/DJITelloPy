"""Run engine: executes a validated script on all drones in lockstep. No Streamlit code here.

Run state lives in this object (not in the Streamlit script), so UI reruns never interrupt a run.
The run happens in a background thread on a snapshot of drones + steps taken at start.
"""
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Callable, Deque, List, Optional, Sequence, Tuple

from .client import CallResult, DroneConfig, DroneInfo, TelloApiClient, clean_text, info_from_result
from .steps import Step

logger = logging.getLogger("fleet_ui")

IDLE, RUNNING, FINISHED, HALTED, ABORTED, LANDED = (
    "idle", "running", "finished", "halted", "aborted", "landed")

ClientFactory = Callable[[DroneConfig, Optional[str]], TelloApiClient]


def configure_console_logging() -> None:
    """Print log lines to the terminal running Streamlit (idempotent)."""
    if any(getattr(h, "_fleet_ui", False) for h in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler._fleet_ui = True
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@dataclass(frozen=True)
class RunState:
    status: str = IDLE
    current_step: int = 0           # 1-based position in the validated step list
    total_steps: int = 0
    step_text: str = ""
    drone_status: Tuple[str, ...] = ()
    message: str = ""


class FleetEngine:
    def __init__(self, client_factory: ClientFactory = TelloApiClient, max_log_lines: int = 2000):
        self._factory = client_factory
        self._lock = threading.Lock()
        self._state = RunState()
        self._log: Deque[str] = deque(maxlen=max_log_lines)
        self._stop = threading.Event()
        self._stop_reason = ABORTED
        self._thread: Optional[threading.Thread] = None
        self._active: Optional[Tuple[Tuple[DroneConfig, ...], Optional[str]]] = None

    # ----- read side (used by the UI) -----
    def state(self) -> RunState:
        with self._lock:
            return self._state

    @property
    def is_running(self) -> bool:
        return self.state().status == RUNNING

    def logs(self) -> List[str]:
        with self._lock:
            return list(self._log)

    def clear_log(self) -> None:
        with self._lock:
            self._log.clear()

    def join(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ----- control side -----
    def start(self, drones: Sequence[DroneConfig], steps: Sequence[Step],
              ca_bundle: Optional[str] = None) -> bool:
        """Start a run from step 1. Returns False if a run is already in progress."""
        drones, steps = tuple(drones), tuple(steps)
        if not drones or not steps:
            return False
        with self._lock:
            if self._state.status == RUNNING:
                return False
            self._stop.clear()
            self._state = RunState(RUNNING, 0, len(steps), "", ("waiting",) * len(drones))
            self._active = (drones, ca_bundle)
            self._thread = threading.Thread(target=self._run, args=(drones, steps, ca_bundle),
                                            name="fleet-run", daemon=True)
            thread = self._thread
        thread.start()
        return True

    def abort(self, drones: Sequence[DroneConfig] = (), ca_bundle: Optional[str] = None) -> None:
        """Hover all drones and stop the run. During a run the run's own drone list is used."""
        self._emergency(ABORTED, "abort", drones, ca_bundle)

    def land_all(self, drones: Sequence[DroneConfig] = (), ca_bundle: Optional[str] = None) -> None:
        """Land all drones and stop the run. During a run the run's own drone list is used."""
        self._emergency(LANDED, "land", drones, ca_bundle)

    def read_status(self, drones: Sequence[DroneConfig], ca_bundle: Optional[str] = None,
                    numbers: Optional[Sequence[int]] = None) -> List[DroneInfo]:
        """Ask every drone's service for its state (flying, battery). Safe during a run.

        ``numbers`` are the drone numbers shown in the UI/log (default 1..N).
        """
        drones = tuple(drones)
        if not drones:
            return []
        numbers = tuple(numbers) if numbers else tuple(range(1, len(drones) + 1))
        clients = [self._factory(d, ca_bundle) for d in drones]

        def send(i: int) -> DroneInfo:
            label, result = clients[i].status()
            self._log_call(numbers[i], drones[i], label, result.text)
            return info_from_result(result)

        return self._fan_out(len(drones), send)

    # ----- internals -----
    def _emit(self, line: str) -> None:
        logger.info(line)
        stamped = "{} {}".format(time.strftime("%H:%M:%S"), line)
        with self._lock:
            self._log.append(stamped)

    def _log_call(self, number: int, drone: DroneConfig, command: str, response: str) -> None:
        self._emit("drone {}, {}, {}, {}".format(number, drone.ip, command, clean_text(response)))

    def _update(self, **changes) -> None:
        with self._lock:
            self._state = replace(self._state, **changes)

    def _set_drone_status(self, index: int, text: str) -> None:
        with self._lock:
            status = list(self._state.drone_status)
            status[index] = text
            self._state = replace(self._state, drone_status=tuple(status))

    @staticmethod
    def _fan_out(count: int, work: Callable[[int], CallResult]) -> List[CallResult]:
        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(work, range(count)))

    def _emergency(self, reason: str, command: str, drones: Sequence[DroneConfig],
                   ca_bundle: Optional[str]) -> None:
        with self._lock:
            if self._state.status == RUNNING and self._active:
                drones, ca_bundle = self._active
                self._stop_reason = reason
        self._stop.set()
        drones = tuple(drones)
        if not drones:
            return
        clients = [self._factory(d, ca_bundle) for d in drones]

        def send(i: int) -> CallResult:
            label, result = clients[i].abort() if command == "abort" else clients[i].land()
            self._log_call(i + 1, drones[i], label, result.text)
            return result

        self._fan_out(len(drones), send)

    def _run(self, drones: Tuple[DroneConfig, ...], steps: Tuple[Step, ...],
             ca_bundle: Optional[str]) -> None:
        clients = [self._factory(d, ca_bundle) for d in drones]
        outcome, message = FINISHED, "all steps completed"
        try:
            for position, step in enumerate(steps, start=1):
                if self._stop.is_set():
                    break
                self._update(current_step=position, step_text=step.text,
                             drone_status=("running",) * len(drones))
                if step.kind == "sleep":
                    self._sleep(step)
                    continue
                failed = self._run_step(drones, clients, step)
                if self._stop.is_set():
                    break
                if failed:
                    outcome = HALTED
                    message = "step {} ({}) failed on {}; nothing further was sent".format(
                        position, step.text, ", ".join("drone {}".format(i + 1) for i in failed))
                    break
            if self._stop.is_set():
                outcome = self._stop_reason
                message = ("aborted: drones hover" if outcome == ABORTED
                           else "land all requested")
        except Exception:  # keep the thread from dying silently; details stay in the terminal
            logger.exception("fleet run crashed")
            outcome, message = HALTED, "internal error, see terminal"
        finally:
            with self._lock:
                self._active = None
                self._state = replace(self._state, status=outcome, message=message)

    def _sleep(self, step: Step) -> None:
        interrupted = self._stop.wait(step.value)
        self._emit("fleet, -, {}, {}".format(step.text, "interrupted" if interrupted else "done"))

    def _run_step(self, drones: Tuple[DroneConfig, ...], clients: List[TelloApiClient],
                  step: Step) -> List[int]:
        """Send one step to all drones at once. Returns the indexes of drones that failed."""

        def send(i: int) -> CallResult:
            if self._stop.is_set():
                self._set_drone_status(i, "skipped")
                return CallResult(False, None, "skipped")
            label, result = clients[i].execute(step)
            self._log_call(i + 1, drones[i], label, result.text)
            if result.ok:
                self._set_drone_status(i, "ok")
            elif self._stop.is_set():
                self._set_drone_status(i, "interrupted")   # expected: our own abort/land
            else:
                self._set_drone_status(i, "failed: " + result.text)
            return result

        results = self._fan_out(len(drones), send)
        return [i for i, r in enumerate(results) if not r.ok]
