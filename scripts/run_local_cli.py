"""Run the checkout's CLI without depending on editable-install ``.pth`` files."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    """Load the package from this checkout and delegate to its real CLI."""

    project_root = Path(__file__).resolve().parents[1]
    source_root = project_root / "src"
    if not (source_root / "iracing_ai_engineer" / "cli.py").is_file():
        raise RuntimeError(f"local source tree is incomplete: {source_root}")
    sys.path.insert(0, str(source_root))

    from iracing_ai_engineer.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
