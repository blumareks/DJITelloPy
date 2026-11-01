"""Write the OpenAPI spec to tello_api/docs/openapi.json: ``python -m tello_api.export_openapi``."""
import json
import tempfile
from pathlib import Path

from .app import create_app
from .drone import DroneController

OUTPUT = Path(__file__).parent / "docs" / "openapi.json"


def main():
    # The spec does not depend on the key or on a drone; nothing is started or connected here.
    app = create_app(DroneController(Path(tempfile.gettempdir())), api_key="x" * 32)
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(app.openapi(), indent=2) + "\n")
    print("wrote", OUTPUT)


if __name__ == "__main__":
    main()
