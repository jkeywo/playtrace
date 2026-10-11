"""Run the M6 release gate, including an installed-wheel smoke test.

Run with: uv run python scripts/check_release.py
Artifacts are retained under .scratch/release-* for inspection.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    scratch = root / ".scratch"
    scratch.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="release-", dir=scratch))
    env = {**os.environ, "TEMP": str(work), "TMP": str(work), "TMPDIR": str(work),
           "UV_CACHE_DIR": str(root / ".uv-cache")}
    uv = shutil.which("uv")
    node = shutil.which("node")
    if not uv or not node:
        raise SystemExit("The release gate requires uv and Node.js on PATH.")

    def run(*command: str, cwd: Path = root, environment: dict | None = None) -> None:
        print(f"Checking: {' '.join(command)}", flush=True)
        subprocess.run(command, cwd=cwd, env=environment or env, check=True)

    run(sys.executable, "-m", "pytest", "-p", "no:cacheprovider", f"--basetemp={work / 'tests'}")
    run(node, "--check", "src/playtrace/review/app.js")
    run(sys.executable, "-m", "compileall", "-q", "src", "tests")
    run(uv, "build", "--out-dir", str(work / "dist"))
    run("git", "diff", "--check")
    database = work / "playtrace.db"
    run(sys.executable, "-m", "playtrace", "migrate", "--db", str(database))
    run(sys.executable, "-m", "playtrace", "doctor", "--db", str(database), "--root", str(root))

    wheel, = (work / "dist").glob("*.whl")
    installed = work / "installed"
    run(uv, "pip", "install", "--python", sys.executable, "--no-deps", "--target", str(installed), str(wheel))
    # The smoke process runs away from the checkout and verifies that its
    # imported module, grammar and browser assets came from the built wheel.
    smoke_env = {**env, "PYTHONPATH": str(installed), "PLAYTRACE_WHEEL_ROOT": str(installed)}
    smoke = (
        "import os; from pathlib import Path; import playtrace; "
        "from playtrace.cnl import TabletopRuleCompiler; "
        "from playtrace.review_server import REVIEW_ROOT; "
        "from playtrace.demo import build_demo_review; "
        "assert Path(playtrace.__file__).resolve().is_relative_to(Path(os.environ['PLAYTRACE_WHEEL_ROOT'])); "
        "TabletopRuleCompiler(); build_demo_review(); "
        "assert all((REVIEW_ROOT / name).is_file() for name in ('index.html', 'app.js', 'styles.css')); "
        "print('Installed wheel, grammar, demo and review assets verified.')"
    )
    run(sys.executable, "-c", smoke, cwd=work, environment=smoke_env)
    run(sys.executable, "-m", "playtrace", "--help", cwd=work, environment=smoke_env)
    print(f"M6 release gate passed. Artifacts: {work}", flush=True)


if __name__ == "__main__":
    main()
