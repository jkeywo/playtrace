"""Container entrypoint for isolated simulation execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from playtrace.models import DesignSnapshot, StrictModel
from playtrace.plugins import registry as simulation_plugins
from playtrace.simulation import (
    SimulationConfig,
    SimulationJsonInputError,
    load_bounded_json_file,
)


class SimulationWorkerRequest(StrictModel):
    plugin_id: str
    snapshot: DesignSnapshot
    config: SimulationConfig
    runner: str = "docker"
    container_image_digest: str | None = None


def main() -> None:
    request_path, output_path = map(Path, sys.argv[1:3])
    try:
        request = SimulationWorkerRequest.model_validate(
            load_bounded_json_file(request_path)
        )
    except SimulationJsonInputError as error:
        raise SystemExit(
            f"invalid simulation JSON input ({error.category}): {error.message}"
        ) from error
    except ValidationError as error:
        raise SystemExit(f"invalid simulation worker request: {error}") from error
    try:
        plugin = simulation_plugins.create(request.plugin_id)
    except KeyError as error:
        raise SystemExit(str(error)) from error
    result = plugin.run(
        request.snapshot,
        request.config,
        runner=request.runner,
    )
    result = result.model_copy(
        update={
            "provenance": result.provenance.model_copy(
                update={
                    "container_image_digest": request.container_image_digest
                }
            )
        }
    )
    rendered = json.dumps(
        result.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    output_path.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
