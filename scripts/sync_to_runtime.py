from __future__ import annotations

import os
import shutil
from pathlib import Path

SOURCE_DIR = Path(os.environ.get("CTC_SOURCE_DIR", Path(__file__).resolve().parents[1])).resolve()
RUNTIME_DIR = Path(
    os.environ.get("CTC_RUNTIME_DIR", "/opt/ctc/app")
).resolve()

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".github",
    "build",
    "dist",
    "docs",
    "tests",
}
EXCLUDED_NAMES = {
    ".env",
    "providers.json",
    "runtime_config.json",
    ".coverage",
}
EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".log",
}
EXCLUDED_PATTERNS = (
    "ctc.sqlite3",
    "ctc.sqlite3-",
    "ctc-dev.sqlite3",
    "ctc-review.sqlite3",
    "providers.json.",
    "runtime_config.json.",
)


def is_excluded(path: Path) -> bool:
    rel = path.relative_to(SOURCE_DIR if path.is_relative_to(SOURCE_DIR) else RUNTIME_DIR)
    parts = rel.parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    name = path.name
    if name.startswith(".tmp") or name.startswith("tmp_") or name.endswith(".tmp"):
        return True
    if name in EXCLUDED_NAMES or name.startswith(".env."):
        return True
    if any(name.startswith(pattern) for pattern in EXCLUDED_PATTERNS):
        return True
    if name.endswith(".sqlite3") or ".sqlite3-" in name:
        return True
    if name.endswith(".db") or ".db-" in name:
        return True
    if name.endswith(".egg-info"):
        return True
    return path.suffix in EXCLUDED_SUFFIXES


def iter_source_files() -> set[Path]:
    files: set[Path] = set()
    for path in SOURCE_DIR.rglob("*"):
        if is_excluded(path):
            if path.is_dir():
                continue
            continue
        if path.is_file():
            files.add(path.relative_to(SOURCE_DIR))
    return files


def iter_managed_runtime_files() -> set[Path]:
    if not RUNTIME_DIR.exists():
        return set()
    files: set[Path] = set()
    for path in RUNTIME_DIR.rglob("*"):
        if is_excluded(path):
            if path.is_dir():
                continue
            continue
        if path.is_file():
            files.add(path.relative_to(RUNTIME_DIR))
    return files


def main() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    source_files = iter_source_files()
    runtime_files = iter_managed_runtime_files()

    copied = 0
    removed = 0
    for rel in sorted(source_files):
        src = SOURCE_DIR / rel
        dst = RUNTIME_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            shutil.copy2(src, dst)
            copied += 1

    for rel in sorted(runtime_files - source_files, reverse=True):
        target = RUNTIME_DIR / rel
        if target.exists() and not is_excluded(target):
            target.unlink()
            removed += 1

    for directory in sorted([p for p in RUNTIME_DIR.rglob("*") if p.is_dir()], reverse=True):
        if directory == RUNTIME_DIR or is_excluded(directory):
            continue
        try:
            directory.rmdir()
        except OSError:
            pass

    print(f"Synced CTC source from {SOURCE_DIR} to {RUNTIME_DIR}")
    print(f"copied_or_updated={copied} removed={removed} total_source_files={len(source_files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
