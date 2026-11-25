"""HTTPS client for one ``tello_api`` service. No Streamlit, no djitellopy/tello_api imports.

Security: HTTPS only, certificate validation always on (optionally against a private CA bundle),
redirects are not followed, proxies from the environment are ignored, and the API key only ever
travels in the ``X-API-Key`` header - it is never part of a URL, log line or ``repr``.
"""
import ipaddress
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests

from .steps import Step

DEFAULT_NETWORK = ipaddress.ip_network("192.168.4.0/24")

CONNECT_TIMEOUT_S = 5
TIMEOUT_ABORT_S = 5
TIMEOUT_LAND_S = 30      # a land sent to an idle drone is synchronous and takes a few seconds
TIMEOUT_FLIGHT_S = 60    # takeoff / move / turn block until the movement is done
TIMEOUT_RECORD_S = 15
TIMEOUT_STATUS_S = 5     # the status endpoint never talks to the drone, so it answers at once

MAX_TEXT = 200


@dataclass(frozen=True)
class DroneConfig:
    ip: str
    port: int
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class CallResult:
    ok: bool
    status: Optional[int]    # HTTP status, None if there was no HTTP answer
    text: str                # short, sanitised summary of the response
    data: Optional[dict] = None   # parsed JSON body of a successful response


def endpoint_key(ip, port) -> Tuple[str, str]:
    """Normalised (ip, port) used to match a drone row in the UI with its last known status."""
    port_text = str(port).strip()
    if port_text.isascii() and port_text.isdigit():
        port_text = str(int(port_text))
    return str(ip).strip(), port_text


@dataclass(frozen=True)
class DroneInfo:
    """What `GET /v1/status` told us about one drone (reachable=False if there was no answer)."""
    reachable: bool
    flying: bool = False
    battery: Optional[int] = None
    connected: bool = False


def info_from_result(result: CallResult) -> DroneInfo:
    data = result.data
    if not result.ok or not isinstance(data, dict) or not isinstance(data.get("flying"), bool):
        return DroneInfo(reachable=False)
    battery = data.get("battery")
    if not isinstance(battery, int) or isinstance(battery, bool) or not 0 <= battery <= 100:
        battery = None
    return DroneInfo(True, data["flying"], battery, data.get("connected") is True)


def clean_text(text: str, limit: int = MAX_TEXT) -> str:
    """Make text safe for a one-line log entry (no control characters, bounded length)."""
    printable = "".join(ch if ch.isprintable() else " " for ch in str(text))
    printable = " ".join(printable.split())
    return printable if len(printable) <= limit else printable[:limit - 3] + "..."


def check_drone(ip: str, port: str, api_key: str,
                network: ipaddress.IPv4Network = DEFAULT_NETWORK) -> Tuple[Optional[DroneConfig], List[str]]:
    """Validate raw form values. Returns (config, []) or (None, [error, ...])."""
    errors: List[str] = []
    address = None
    try:
        address = ipaddress.IPv4Address((ip or "").strip())
    except ValueError:
        errors.append("IP address is not a valid IPv4 address")
    if address is not None and address not in network:
        errors.append("IP address must be inside {}".format(network))

    port_text = (port or "").strip()
    port_number = None
    if port_text.isascii() and port_text.isdigit() and 1 <= int(port_text) <= 65535:
        port_number = int(port_text)
    else:
        errors.append("port must be a number from 1 to 65535")

    if not api_key:
        errors.append("API key is required")
    elif not api_key.isascii() or not api_key.isprintable():
        errors.append("API key contains characters that are not allowed")

    if errors:
        return None, errors
    return DroneConfig(str(address), port_number, api_key), []


def check_ca_bundle(path: str) -> Optional[str]:
    """Return an error message for a bad CA bundle path, None if empty (system store) or fine."""
    if not path:
        return None
    if not os.path.isfile(path):
        return "CA bundle file not found"
    return None


def _describe_response(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return clean_text(response.text)
    if isinstance(body, dict):
        if "detail" in body:
            return clean_text(body["detail"])
        if "flying" in body:
            battery = body.get("battery")
            return clean_text("{}, battery {}".format(
                "flying" if body["flying"] is True else "on ground",
                "{}%".format(battery) if isinstance(battery, int) else "unknown"))
        if "recording" in body:
            text = "recording " + ("on" if body["recording"] else "off")
            if body.get("file"):
                text += " " + str(body["file"])
            return clean_text(text)
        if "status" in body:
            return clean_text(body["status"])
    return clean_text(json.dumps(body))


class TelloApiClient:
    def __init__(self, drone: DroneConfig, ca_bundle: Optional[str] = None):
        self.drone = drone
        self._verify = ca_bundle or True   # never False

    def _call(self, method: str, path: str, body: Optional[dict], read_timeout: int) -> CallResult:
        url = "https://{}:{}{}".format(self.drone.ip, self.drone.port, path)
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(
                    method, url, json=body, headers={"X-API-Key": self.drone.api_key},
                    timeout=(CONNECT_TIMEOUT_S, read_timeout), verify=self._verify,
                    allow_redirects=False)
        except requests.exceptions.SSLError:
            return CallResult(False, None, "TLS verification failed (certificate not trusted for this IP)")
        except requests.exceptions.Timeout:
            return CallResult(False, None, "timeout")
        except requests.exceptions.ConnectionError:
            return CallResult(False, None, "connection failed")
        except requests.exceptions.RequestException as exc:
            return CallResult(False, None, "request error ({})".format(type(exc).__name__))
        text = "{} {}".format(response.status_code, _describe_response(response))
        data = None
        if response.ok:
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            data = parsed if isinstance(parsed, dict) else None
        return CallResult(response.ok, response.status_code, text, data)

    def execute(self, step: Step) -> Tuple[str, CallResult]:
        """Send one (non-sleep) step. Returns (command label for the log, result)."""
        if step.kind in ("takeoff", "land"):
            timeout = TIMEOUT_LAND_S if step.kind == "land" else TIMEOUT_FLIGHT_S
            return step.kind, self._call("POST", "/v1/" + step.kind, None, timeout)
        if step.kind == "move":
            return step.text, self._call("POST", "/v1/move/" + step.direction,
                                         {"distance_cm": step.value}, TIMEOUT_FLIGHT_S)
        if step.kind == "turn":
            return step.text, self._call("POST", "/v1/turn/" + step.direction,
                                         {"degrees": step.value}, TIMEOUT_FLIGHT_S)
        if step.kind == "record":
            return step.text, self._call("PUT", "/v1/recording", {"enabled": bool(step.value)},
                                         TIMEOUT_RECORD_S)
        raise ValueError("step '{}' is not sent to drones".format(step.kind))

    def status(self) -> Tuple[str, CallResult]:
        return "status", self._call("GET", "/v1/status", None, TIMEOUT_STATUS_S)

    def abort(self) -> Tuple[str, CallResult]:
        return "abort", self._call("POST", "/v1/abort", None, TIMEOUT_ABORT_S)

    def land(self) -> Tuple[str, CallResult]:
        return "land", self._call("POST", "/v1/land", None, TIMEOUT_LAND_S)
