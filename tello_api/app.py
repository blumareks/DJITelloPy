"""REST API for flying a DJI Tello."""
import logging
import secrets
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path as PathParam, Request, Security
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .drone import Conflict, DroneCommandFailed, DroneController

logger = logging.getLogger("tello_api")

STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """
Control a DJI Tello drone over HTTP.

**Trying it out in this page:** click **Authorize**, paste the value of the server's `API_KEY`,
then use **Try it out** on any endpoint. Every call except the docs themselves needs the key.

Each flight command blocks until the drone has finished the movement (up to a few seconds), and
only one command can run at a time - a second request meanwhile gets `409`.

**This moves a real drone.** Make sure the area is clear, and keep `POST /v1/land` at hand.
"""

TAGS = [
    {"name": "Flight", "description": "Take off, land, move and turn."},
    {"name": "Camera", "description": "Record the drone's video to an mp4 file on the server."},
    {"name": "Status", "description": "Read the current state without sending anything to the drone."},
]


class Direction(str, Enum):
    up = "up"
    down = "down"
    left = "left"
    right = "right"


class TurnDirection(str, Enum):
    left = "left"
    right = "right"


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    distance_cm: int = Field(ge=20, le=500, description="Distance in cm (Tello limits: 20-500)",
                             examples=[50])


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    degrees: int = Field(ge=1, le=360, description="Angle in degrees", examples=[90])


class RecordingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(description="true starts recording, false stops it", examples=[True])


class CommandResult(BaseModel):
    status: str = Field(default="ok", examples=["ok"])
    command: str = Field(examples=["move up 50"])


class RecordingResult(BaseModel):
    recording: bool = Field(description="Whether a recording is active after this call")
    file: Optional[str] = Field(default=None, description="File name inside the server's RECORDINGS_DIR",
                                examples=["tello-20261101-013000.mp4"])


class StatusResult(BaseModel):
    connected: bool = Field(description="Whether the server has entered SDK mode with the drone")
    flying: bool
    recording: bool
    recording_file: Optional[str]
    battery: Optional[int] = Field(description="Percent; null until the first state packet arrives",
                                   examples=[87])


class ErrorResult(BaseModel):
    detail: str


ERROR_RESPONSES = {
    401: {"model": ErrorResult, "description": "Missing or invalid `X-API-Key`"},
    409: {"model": ErrorResult,
          "description": "Not allowed in the current state (already flying, not flying, "
                         "or another command is still running)"},
    502: {"model": ErrorResult, "description": "The drone did not accept or answer the command"},
}


def create_app(controller: DroneController, api_key: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        controller.start()
        yield
        await run_in_threadpool(controller.shutdown)

    # The default docs pages load their JavaScript from a CDN, which does not work when the
    # Pi/laptop is only connected to the Tello's Wi-Fi. Serve Swagger UI from local files instead.
    app = FastAPI(title="Tello REST API", version="1.0.0", description=DESCRIPTION,
                  openapi_tags=TAGS, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/docs", include_in_schema=False)
    def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=app.title + " - Swagger UI",
            swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui/swagger-ui.css",
            swagger_favicon_url="/static/swagger-ui/favicon-32x32.png",
        )

    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False,
                                  description="Value of the server's `API_KEY` environment variable")
    expected_key = api_key.encode()

    def require_api_key(request: Request, key: Optional[str] = Security(api_key_header)):
        if key is None or not secrets.compare_digest(key.encode(), expected_key):
            logger.warning("Rejected request with missing/invalid API key from %s",
                           request.client.host if request.client else "unknown")
            raise HTTPException(status_code=401, detail="Invalid or missing API key",
                                headers={"WWW-Authenticate": "ApiKey"})

    @app.exception_handler(Conflict)
    async def conflict_handler(_request: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DroneCommandFailed)
    async def command_failed_handler(_request: Request, _exc: DroneCommandFailed):
        # Details are logged by the controller; do not leak drone/network internals to the client.
        return JSONResponse(status_code=502, content={"detail": "Drone command failed"})

    router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)], responses=ERROR_RESPONSES)

    @router.post("/takeoff", response_model=CommandResult, tags=["Flight"], summary="Take off",
                 description="Automatic take-off to about 1 m. Returns once the drone is hovering. "
                             "`409` if it is already flying.")
    def takeoff():
        controller.takeoff()
        return CommandResult(command="takeoff")

    @router.post("/land", response_model=CommandResult, tags=["Flight"], summary="Land",
                 description="Automatic landing. Always sent to the drone, even if the server thinks "
                             "it is already on the ground.")
    def land():
        controller.land()
        return CommandResult(command="land")

    @router.post("/move/{direction}", response_model=CommandResult, tags=["Flight"],
                 summary="Move up, down, left or right",
                 description="Fly the given distance in cm. `409` if the drone is not flying.")
    def move(body: MoveRequest,
             direction: Direction = PathParam(description="Direction to fly")):
        controller.move(direction.value, body.distance_cm)
        return CommandResult(command="move {} {}".format(direction.value, body.distance_cm))

    @router.post("/turn/{direction}", response_model=CommandResult, tags=["Flight"],
                 summary="Turn left or right",
                 description="Rotate in place by the given angle. `right` is clockwise, `left` is "
                             "counter-clockwise. `409` if the drone is not flying.")
    def turn(body: TurnRequest,
             direction: TurnDirection = PathParam(description="Rotation direction")):
        controller.turn(direction.value, body.degrees)
        return CommandResult(command="turn {} {}".format(direction.value, body.degrees))

    @router.put("/recording", response_model=RecordingResult, tags=["Camera"],
                summary="Start or stop video recording",
                description="Turns the drone's video stream on and records it to an H.264 mp4 in the "
                            "server's `RECORDINGS_DIR`; turning it off finishes the file. Idempotent: "
                            "starting while already recording returns the current file. Works on the "
                            "ground too.")
    def recording(body: RecordingRequest):
        filename = controller.set_recording(body.enabled)
        return RecordingResult(recording=body.enabled, file=filename)

    @router.get("/status", response_model=StatusResult, tags=["Status"], summary="Current state",
                description="Server-side view of the drone. Does not send anything to the drone, "
                            "so it is safe to call at any time, even during a movement.")
    def status():
        return controller.status()

    app.include_router(router)
    return app
