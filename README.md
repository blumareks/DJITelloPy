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
The [Dockerfile](Dockerfile) uses the Red Hat UBI Python image, so log in to the Red Hat registry first (free Red Hat account required). Rootless Podman is sufficient.
```
podman login registry.redhat.io
podman build -t djitellopy .
podman run --rm -it \
  --network host \
  --read-only --tmpfs /tmp \
  --cap-drop all --security-opt no-new-privileges \
  djitellopy
```

- `--network host` is required, because the Tello sends state (UDP 8890) and video (UDP 11111) packets to the host, which does not work through container NAT.
- By default `examples/simple.py` is run (the drone **takes off**). To run another script, mount your examples and override the command:
  ```
  podman run --rm -it --network host --read-only --tmpfs /tmp \
    --cap-drop all --security-opt no-new-privileges \
    -v ./examples:/opt/app-root/src/examples:ro \
    djitellopy python examples/<script>.py
  ```
  (On SELinux hosts append `,Z` to the volume options: `:ro,Z`.)
- Alternatively use the included [docker-compose.yml](docker-compose.yml) with `podman compose up --build` (requires `podman-compose` or the docker-compose plugin).
- Examples using OpenCV need `opencv-python-headless` added to the `pip install` step in the Dockerfile.
- The pygame example needs a display and does not work in the container.

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
