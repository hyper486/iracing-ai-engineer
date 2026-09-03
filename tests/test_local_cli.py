from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_local_cli_launcher_uses_checkout_from_any_working_directory(tmp_path: Path):
    launcher = Path("scripts/run_local_cli.py").resolve()
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(launcher), "--help"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "shadow" in result.stdout
    assert "offline-demo" in result.stdout
    assert "fuel-replay" in result.stdout
    assert "collect-live" in result.stdout
    assert "m0-accept" in result.stdout
