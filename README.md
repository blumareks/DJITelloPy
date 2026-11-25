# DJITelloPy
## [中文文档 (Chinese version of this readme)](README_CN.md)

DJI Tello drone python interface using the official [Tello SDK](https://dl-cdn.ryzerobotics.com/downloads/tello/20180910/Tello%20SDK%20Documentation%20EN_1.3.pdf) and [Tello EDU SDK](https://dl-cdn.ryzerobotics.com/downloads/Tello/Tello%20SDK%202.0%20User%20Guide.pdf). This library has the following features:

- implementation of all tello commands
- easily retrieve a video stream
- receive and parse state packets
- control a swarm of drones
- support for python >= 3.6

Feel free to contribute!

## Install using pip
```
pip install djitellopy
```

For Linux distributions with both python2 and python3 (e.g. Debian, Ubuntu, ...) you need to run
```
pip3 install djitellopy
```

## Install in developer mode
Using the commands below you can install the repository in an _editable_ way. This allows you to modify the library and use the modified version as if you had installed it regularly.

```
git clone https://github.com/damiafuentes/DJITelloPy.git
cd DJITelloPy
pip install -e .
```

## Run locally
Before running anything, power on the Tello and connect the computer (or Raspberry Pi) to the drone's Wi-Fi network (`TELLO-XXXXXX`). The drone is reachable at `192.168.10.1`.

### Option 1: Python virtual environment
```
git clone https://github.com/damiafuentes/DJITelloPy.git
cd DJITelloPy
python3 -m venv .venv
source .venv/bin/activate
pip install --only-binary=:all: -r requirements.txt
pip install --no-deps -e .
python examples/simple.py
```

- `--only-binary=:all:` makes pip use prebuilt wheels and fail immediately instead of trying to compile packages (useful on a Raspberry Pi).
- `--no-deps` skips `opencv-python`, which the library itself does not import. It is only needed by some examples (`take-picture.py`, `record-video.py`, `manual-control-opencv.py`). For those, additionally run `pip install --only-binary=:all: opencv-python-headless` (or `opencv-python` if you have a display).

### Option 2: Podman (e.g. Raspberry Pi 5)
The [Dockerfile](Dockerfile) uses the Red Hat UBI Python image, so log in to the Red Hat registry first (free Red Hat account required). Rootless Podman is sufficient. The container starts the [REST API](#rest-api) by default.
```
podman login registry.redhat.io
podman build -t djitellopy .

export API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
echo "$API_KEY"   # keep it, clients need it
mkdir -p recordings
podman run --rm -it \
  --network host \
  -e API_KEY -e API_HOST=<ip-of-the-pi-on-your-lan> \
  --read-only --tmpfs /tmp \
  --cap-drop all --security-opt no-new-privileges \
  -v ./recordings:/recordings:Z,U \
  djitellopy
```

- `--network host` is required, because the Tello sends state (UDP 8890) and video (UDP 11111) packets to the host, which does not work through container NAT.
- Alternatively use the included [docker-compose.yml](docker-compose.yml) with `API_KEY=... API_HOST=... podman compose up --build` (requires `podman-compose` or the docker-compose plugin).
- To run an example script instead of the API, mount the examples and override the command:
  ```
  podman run --rm -it --network host --read-only --tmpfs /tmp \
    --cap-drop all --security-opt no-new-privileges \
    -v ./examples:/opt/app-root/src/examples:ro,Z \
    djitellopy python examples/<script>.py
  ```
- Examples using OpenCV need `opencv-python-headless` added to the `pip install` step in the Dockerfile.
- The pygame example needs a display and does not work in the container.

## REST API
[`tello_api/`](tello_api/) is a small [FastAPI](https://fastapi.tiangolo.com/) service that turns HTTP requests into Tello commands.

### Try it in Swagger UI
Start the service, then open **`http://<API_HOST>:<API_PORT>/docs`** in a browser (e.g. `http://127.0.0.1:8000/docs`):

1. Click **Authorize**, paste your `API_KEY`, click **Authorize** again.
2. Expand an endpoint, click **Try it out**, edit the example body, click **Execute**.

A safe first check without flying: `GET /v1/status`, then `PUT /v1/recording` (works on the ground). Then `POST /v1/takeoff`, a move, `POST /v1/land`.

- Swagger UI is bundled in the repo ([`tello_api/static/swagger-ui/`](tello_api/static/swagger-ui/)), so the page works with no internet access, e.g. when you are only connected to the Tello's Wi-Fi.
- `/docs` and `/openapi.json` need no key (they only describe the API); every `/v1/...` call does.
- The machine-readable spec is at `/openapi.json` and committed as [`tello_api/docs/openapi.json`](tello_api/docs/openapi.json). Import it into Postman, Insomnia, Bruno, or a code generator. Regenerate it after changing the API with `python -m tello_api.export_openapi` (a test fails if it is stale).

**Network setup:** the Pi's Wi-Fi is connected to the Tello (`TELLO-XXXXXX`), so reach the API through the Pi's other interface (e.g. Ethernet, or a second Wi-Fi adapter). Set `API_HOST` to that interface's IP address.

### Run without a container
```
pip install --only-binary=:all: -r requirements.txt -r tello_api/requirements.txt
pip install --no-deps -e .
export API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m tello_api            # listens on 127.0.0.1:8000
```

### Configuration (environment variables)
| Variable | Default | Description |
|---|---|---|
| `API_KEY` | _required_ | Random secret, at least 32 characters. Sent by clients in the `X-API-Key` header. |
| `API_HOST` | `127.0.0.1` | Address to bind to. `0.0.0.0` is refused; use a specific IP. |
| `API_PORT` | `8000` | Port. |
| `RECORDINGS_DIR` | `./recordings` | Where `.mp4` recordings are saved. |
| `TLS_CERTFILE`, `TLS_KEYFILE` | unset | Serve HTTPS. Without them the API key travels unencrypted, so only use plain HTTP on a trusted network. |

### Endpoints
All endpoints are under `/v1` and need the `X-API-Key` header. Request bodies are JSON.

| Method & path | Body | Action |
|---|---|---|
| `POST /v1/takeoff` | – | Take off |
| `POST /v1/abort` | – | Abort current movement and hover in place |
| `POST /v1/land` | – | Land (interrupts any running command) |
| `POST /v1/move/{up\|down\|left\|right\|forward\|back}` | `{"distance_cm": 20..500}` | Fly that distance (`forward`/`back` follow the direction the drone faces) |
| `POST /v1/turn/{left\|right}` | `{"degrees": 1..360}` | Rotate |
| `PUT /v1/recording` | `{"enabled": true\|false}` | Start/stop recording to an H.264 `.mp4` |
| `GET /v1/status` | – | `connected`, `flying`, `recording`, `recording_file`, `battery` |

Responses: `200` ok, `401` bad/missing key, `409` not allowed now (already flying, not flying, or another command still running), `422` invalid input, `502` the drone did not accept the command.

```
curl -X POST -H "X-API-Key: $API_KEY" http://<host>:8000/v1/takeoff
curl -X POST -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
     -d '{"distance_cm": 100}' http://<host>:8000/v1/move/up
curl -X POST -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
     -d '{"distance_cm": 100}' http://<host>:8000/v1/move/forward
curl -X POST -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
     -d '{"degrees": 90}' http://<host>:8000/v1/turn/right
curl -X PUT  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
     -d '{"enabled": true}' http://<host>:8000/v1/recording
curl -X POST -H "X-API-Key: $API_KEY" http://<host>:8000/v1/abort
curl -X POST -H "X-API-Key: $API_KEY" http://<host>:8000/v1/land
```

Notes:
- Each call blocks until the drone has finished the movement (several seconds) and returns then. Only one command runs at a time; a second one gets `409` (except `POST /v1/abort` and `POST /v1/land`, which interrupt running commands).
- A keepalive is sent while flying so the Tello does not auto-land after 15 s of silence. On shutdown the service lands the drone and stops recording.
- Run a single instance only (the Tello UDP ports can be bound once), i.e. do not use multiple workers.

### Tests
The tests use a fake drone, so no Tello is needed:
```
pip install -r tello_api/requirements-dev.txt
python -m pytest tests
```

## Fleet UI

A Streamlit page (`fleet_ui/`) that controls several `tello_api` services (one per drone) at once. It talks
to them over HTTPS only and does not import `djitellopy` or `tello_api`.

### 1. HTTPS certificates
Plain HTTP is not supported, because the API key would travel unencrypted. Create a private CA and one
certificate per drone IP (needs `openssl`; output goes to `fleet_ui/certs/`, which git ignores):
```
fleet_ui/make_certs.sh 192.168.4.11 192.168.4.12
```
Copy `certs/<ip>/cert.pem` and `key.pem` to the matching Pi (the key is secret) and start its service:
```
API_KEY=... API_HOST=192.168.4.11 TLS_CERTFILE=cert.pem TLS_KEYFILE=key.pem python -m tello_api
```
`certs/ca.pem` is the CA bundle the UI needs. Keep `certs/ca.key` on this machine only.

### 2. Run the UI
```
pip install -r fleet_ui/requirements.txt
streamlit run fleet_ui/fleet_ui.py --server.address 127.0.0.1 --browser.gatherUsageStats false   # from the repository root
```
(Starting it from inside `fleet_ui/` instead picks up `fleet_ui/.streamlit/config.toml`, which sets the same.)
Set the CA bundle in the page, or start with `FLEET_UI_CA_BUNDLE=fleet_ui/certs/ca.pem`.

### 3. Using it
- **Drones**: IP (must be inside `192.168.4.0/24`), port and API key per drone. Keys are only kept in the
  browser session and are never logged (see **Save** below for storing them encrypted).
- **Script**: one instruction per row; blank rows and `#` comments are ignored. The whole script is checked
  before anything is sent and errors are shown with row numbers.

  | Instruction | Meaning |
  |---|---|
  | `takeoff`, `land` | take off / land |
  | `up\|down\|left\|right\|forward\|back <cm>` | move, 20-500 cm |
  | `turn left\|right <degrees>` | rotate, 1-360 |
  | `record on\|off` | start / stop video recording |
  | `sleep <seconds>` | pause the whole fleet (max 600) |

- **Execute** first asks every drone for its status (`GET /v1/status`), then sends step N to all drones at
  once, and starts step N+1 (again preceded by a status check) when all have answered. The status check
  runs before every row, `sleep` rows included, and once more at the end, so the green/red
  `flying NN%` / `on ground NN%` labels next to each drone (battery %) refresh during the run. The check is
  informational: a drone that does not answer it shows `no answer` but does not stop the run by itself. If
  any drone fails a step the run halts and nothing more is sent automatically.
- **Abort** makes all drones hover and stops the run. **Land all** lands all drones and stops the run. Both
  work during a run and when idle (using the drone rows currently in the form).
- **Status** asks every drone's service for its state (`GET /v1/status`, which never moves a drone) and
  shows it next to the drone label: green `flying NN%` when flying, red `on ground NN%` when on the ground
  (NN = battery), gray `no answer` if the service did not answer. It also works during a run. The result is
  a snapshot from the moment of the click and is hidden if you edit that drone's IP.
- **Save** writes IP, port, CA bundle path and the script to `fleet_ui/drones.txt` (JSON; override the path
  with `FLEET_UI_DRONES_FILE`; git ignores it). The page loads this file when it starts. **API keys are
  secrets, so they are never written in clear text:** enter a passphrase (8+ characters) before **Save** and
  the keys are stored encrypted (AES-256-GCM, key derived with scrypt); on the next start enter the same
  passphrase and press **Unlock keys** to fill them in. Without a passphrase the keys are left out and you
  type them each time. The passphrase is never stored, and a lost passphrase cannot be recovered. Saving
  without a passphrase removes keys saved earlier. The file is created with owner-only permissions.
- Every call is logged as `drone N, ip, command, response` in the terminal running Streamlit and in the page.

### Security notes
The page itself has no login: keep it on loopback or a trusted host. Certificates are always verified; there
is no option to turn that off.

### Tests
```
pip install -r fleet_ui/requirements-dev.txt
python -m pytest tests/test_fleet_steps.py tests/test_fleet_engine.py tests/test_fleet_storage.py tests/test_fleet_page.py
```

## Usage
### API Reference
See [djitellopy.readthedocs.io](https://djitellopy.readthedocs.io/en/latest/) for a full reference of all classes and methods available.

### Simple example
```python
from djitellopy import Tello

tello = Tello()

tello.connect()
tello.takeoff()

tello.move_left(100)
tello.rotate_counter_clockwise(90)
tello.move_forward(100)

tello.land()
```

### More examples
In the [examples](examples/) directory there are some code examples.
Comments in the examples are mostly in both english and chinese.

- [taking a picture](examples/take-picture.py)
- [recording a video](examples/record-video.py)
- [flying a swarm (multiple Tellos at once)](examples/simple-swarm.py)
- [simple controlling using your keyboard](examples/manual-control-opencv.py)
- [mission pad detection](examples/mission-pads.py)
- [fully featured manual control using pygame](examples/manual-control-pygame.py)

### Notes
- If you are using the `streamon` command and the response is `Unknown command` means you have to update the Tello firmware. That can be done through the Tello app.
- Mission pad detection and navigation is only supported by the Tello EDU.
- Bright environment is necessary for successful use of mission pads.
- Connecting to an existing wifi network is only supported by the Tello EDU.
- When connected to an existing wifi network video streaming is not available (TODO: needs confirmation with the new SDK3 `port` commands)

## DJITelloPy in the media and in the wild
- \>6 Million views Youtube: [Drone Programming With Python Course](https://youtu.be/LmEcyQnfpDA?t=1282)
- German magazine "Make": ["KI steuert Follow-Me-Drohne" (paywall)](https://www.heise.de/select/make/2021/6/2116016361503211330), [authors notes](https://www.jentsch.io/ki-artikel-im-aktuellen-make-magazin-6-21/), [github repo](https://github.com/msoftware/tello-tracking)
- Webinar on learn.droneblocks.io: ["DJITelloPy Drone Coding"](https://learn.droneblocks.io/p/djitellopy), [github repo](https://learn.droneblocks.io/p/djitellopy)
- Universities & Schools using DJITelloPy in projects or in class:
    - [Ball State University in Muncie, Indiana](https://www.bsu.edu/)
    - [Technical University Kaiserslautern](https://www.uni-kl.de/)
    - [Lehrereinnenfortbildung Baden-Württemberg](https://lehrerfortbildung-bw.de/zsl_as_es/)
    - [Seminar für Ausbildung und Fortbildung der Lehrkräfte Freiburg (Berufliche Schulen)](https://bs-fr.seminare-bw.de/,Lde/Startseite)
    - [Sha Tin College, Hong Kong](https://shatincollege.edu.hk/)
    - [University of São Paulo](https://www5.usp.br/)
    - [add yours...](https://github.com/damiafuentes/DJITelloPy/edit/master/README.md)

## Authors

* **Damià Fuentes Escoté**
* **Jakob Löw**
* [and more](https://github.com/damiafuentes/DJITelloPy/graphs/contributors)

## License

This project is licensed under the MIT License - see the [LICENSE.txt](LICENSE.txt) file for details
