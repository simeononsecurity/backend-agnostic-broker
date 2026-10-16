"""Generate docs/openapi.yaml from the live FastAPI app."""

from __future__ import annotations

import yaml

from broker.app import create_app
from broker.config import Settings


def main() -> None:
    app = create_app(Settings(auth_enabled=False, registry_path="registry.yaml"))
    spec = app.openapi()
    with open("docs/openapi.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(spec, f, sort_keys=False)
    print("openapi paths:", list(spec["paths"].keys()))


if __name__ == "__main__":
    main()
