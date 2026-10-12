"""Run the pure renderer contracts when the optional Node runtime is available."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_plugin_replay_change_projection():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the optional replay JavaScript contracts")
    script = Path(__file__).with_name("replay_ui.cjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
