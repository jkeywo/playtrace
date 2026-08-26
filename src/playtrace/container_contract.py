"""Static and optional Docker checks for the isolated simulation contract."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from playtrace.adapters import SyntheticProjectAdapter
from playtrace.models import SourceMedium, StrictModel
from playtrace.plugins import registry as simulation_plugins
from playtrace.simulation import DockerSimulationRunner, SimulationConfig


class ContainerDiagnostic(StrictModel):
    ok: bool
    checks: dict[str, bool]
    details: dict[str, Any]


def _canonical_repository_bytes(path: Path) -> bytes:
    """Return the LF bytes stored by Git for a checked-out text lock file."""

    data = path.read_bytes()
    return data.replace(b"\r\n", b"\n")


def verify_container_contract(
    root: str | Path,
    *,
    smoke: bool = False,
    image: str | None = None,
    image_digest: str | None = None,
) -> ContainerDiagnostic:
    root = Path(root)
    lock = json.loads((root / "container" / "simulation-image.lock.json").read_text("utf-8"))
    dockerfile = (root / "container" / "Dockerfile").read_text("utf-8")
    dependency_lock = root / lock["dependency_lock"]
    actual_hash = hashlib.sha256(_canonical_repository_bytes(dependency_lock)).hexdigest()
    locked_base = lock["base_image"]
    runtime_stage = dockerfile.rsplit(f"FROM {locked_base}", maxsplit=1)[-1]
    install_command = "uv sync --locked --no-dev --no-editable"
    runtime_import_command = (
        'RUN /app/.venv/bin/python -c "import playtrace, playtrace.sim_worker"'
    )
    runtime_venv_copy = (
        "COPY --from=builder --chown=playtrace:playtrace /app/.venv /app/.venv"
    )
    runtime_copy_commands = tuple(
        line.strip() for line in runtime_stage.splitlines() if line.startswith("COPY ")
    )
    checks = {
        "base_image_digest_pinned": bool(
            re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", locked_base)
        ),
        "dockerfile_uses_locked_base": dockerfile.count(f"FROM {locked_base}") == 2,
        "dependency_lock_matches": actual_hash == lock["dependency_lock_sha256"],
        "locked_install": install_command in dockerfile,
        "non_editable_package_install": install_command in dockerfile,
        "runtime_venv_copied": runtime_venv_copy in runtime_copy_commands,
        "runtime_package_importable": runtime_import_command in runtime_stage,
        "runtime_source_excluded": runtime_copy_commands == (runtime_venv_copy,),
        "non_root_runtime": f"USER {lock['runtime_user']}" in dockerfile,
        "docker_available": shutil.which("docker") is not None,
    }
    command = DockerSimulationRunner(
        "playtrace-simulation", image_digest="sha256:" + "0" * 64
    ).command(Path("input"), Path("output"))
    controls = lock["required_runner_controls"]
    checks["secure_runner_controls"] = all(item in command for item in controls)
    checks["network_disabled"] = command[command.index("--network") + 1] == "none"
    details: dict[str, Any] = {"base_image": locked_base, "lock_sha256": actual_hash}
    if smoke:
        valid_digest = bool(
            image_digest and re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest)
        )
        valid_image = bool(image and re.fullmatch(r"[^@\s]+", image))
        if not valid_image or not valid_digest:
            checks["docker_smoke"] = False
            details["docker_smoke"] = "smoke requires an image and exact sha256:<64 lowercase hex> digest"
        elif not checks["docker_available"]:
            checks["docker_smoke"] = False
            details["docker_smoke"] = "docker executable not found"
        else:
            try:
                runner = DockerSimulationRunner(image, image_digest=image_digest)
                result = runner.run(
                    simulation_plugins.for_project("crisis-wardens"),
                    SyntheticProjectAdapter(SourceMedium.SYNTHETIC).snapshot(),
                    SimulationConfig(
                        id="container-smoke",
                        scenario_id="container-smoke:missing-scenario",
                        character_ids=(),
                        max_turns=1,
                    ),
                )
                checks["docker_smoke"] = bool(result.blockers)
                details["docker_smoke"] = {
                    "run_id": result.id,
                    "worker_result_read": True,
                    "blocker_count": len(result.blockers),
                }
            except Exception as error:
                checks["docker_smoke"] = False
                details["docker_smoke"] = f"worker mount smoke failed: {error}"[:1000]
            details["smoke_image"] = f"{image}@{image_digest}"
    required = {key: value for key, value in checks.items() if key != "docker_available"}
    return ContainerDiagnostic(ok=all(required.values()), checks=checks, details=details)
