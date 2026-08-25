"""Container entrypoint for isolated simulation execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playtrace.models import DesignSnapshot
from playtrace.plugins.crisis_wardens import CrisisWardensSimulationPlugin
from playtrace.simulation import SimulationConfig


PLUGINS = {CrisisWardensSimulationPlugin.id: CrisisWardensSimulationPlugin}


def main() -> None:
    request_path, output_path = map(Path, sys.argv[1:3])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    plugin_type = PLUGINS.get(request["plugin_id"])
    if plugin_type is None:
        raise SystemExit(f"unknown simulation plugin: {request['plugin_id']}")
    result = plugin_type().run(
        DesignSnapshot.model_validate(request["snapshot"]),
        SimulationConfig.model_validate(request["config"]),
        runner=request.get("runner", "docker"),
    )
    result = result.model_copy(
        update={
            "provenance": result.provenance.model_copy(
                update={
                    "container_image_digest": request.get("container_image_digest")
                }
            )
        }
    )
    output_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
