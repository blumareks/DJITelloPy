"""Run the Tello REST API: ``python -m tello_api``.

Configuration (environment variables):
    API_KEY         required, random string of at least 32 characters
    API_HOST        address to bind to, default 127.0.0.1 (binding to all interfaces is refused)
    API_PORT        default 8000
    RECORDINGS_DIR  where recordings are written, default ./recordings
    TLS_CERTFILE / TLS_KEYFILE   optional, serve HTTPS
"""
import os
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .drone import DroneController

MIN_KEY_LENGTH = 32


def main():
    api_key = os.environ.get("API_KEY", "")
    if len(api_key) < MIN_KEY_LENGTH:
        sys.exit("API_KEY must be set to a random string of at least {} characters, e.g.\n"
                 "  python -c \"import secrets; print(secrets.token_urlsafe(32))\"".format(MIN_KEY_LENGTH))

    host = os.environ.get("API_HOST", "127.0.0.1")
    if host in ("", "0.0.0.0", "::"):
        sys.exit("Refusing to bind to all interfaces; set API_HOST to a specific address.")

    try:
        port = int(os.environ.get("API_PORT", "8000"))
    except ValueError:
        sys.exit("API_PORT must be an integer.")

    certfile = os.environ.get("TLS_CERTFILE")
    keyfile = os.environ.get("TLS_KEYFILE")
    if bool(certfile) != bool(keyfile):
        sys.exit("Set both TLS_CERTFILE and TLS_KEYFILE, or neither.")
    if not certfile and host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: serving plain HTTP on a non-loopback address; "
              "the API key is sent unencrypted. Set TLS_CERTFILE/TLS_KEYFILE.", file=sys.stderr)

    controller = DroneController(recordings_dir=Path(os.environ.get("RECORDINGS_DIR", "recordings")))
    app = create_app(controller, api_key)
    # One process only: the Tello UDP ports (8889/8890/11111) can be bound once.
    uvicorn.run(app, host=host, port=port, ssl_certfile=certfile, ssl_keyfile=keyfile)


if __name__ == "__main__":
    main()
