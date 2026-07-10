from __future__ import annotations

import os
from pathlib import Path

PRIVATE_FILE_MODE = 0o600


def enforce_private_file(path: Path) -> None:
    target = Path(path)
    if target.exists():
        os.chmod(target, PRIVATE_FILE_MODE)
