"""Local entry point: ``python run.py``.

Adds ``src/`` to the path so the package imports without an install.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from barkbox.app import main  # noqa: E402

if __name__ == "__main__":
    main()
