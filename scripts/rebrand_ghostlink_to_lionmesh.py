#!/usr/bin/env python3
"""
Replace remaining legacy LionMesh naming with LionMesh naming.

Run this script from the repository root:

    python scripts/rebrand_lionmesh_to_lionmesh.py

It updates text-based project files only and skips .git, caches, images, archives,
virtual environments and compiled files.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".github",  # CI file is already LionMesh-safe; keep this if desired
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
}

TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".ini",
    ".conf",
    ".example",
    ".service",
    ".sh",
    ".html",
    ".css",
    ".js",
    ".json",
    ".yml",
    ".yaml",
}

REPLACEMENTS = [
    ("LIONMESH-MESH", "LIONMESH-MESH"),
    ("LIONMESH", "LIONMESH"),
    ("LionMesh", "LionMesh"),
    ("lionmesh", "lionmesh"),
    ("LION-", "LION-"),
]


def should_skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".7z", ".exe", ".dll", ".so", ".pyc"}:
        return True
    return False


def is_text_candidate(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if path.name in {"Dockerfile", "LICENSE", ".gitignore"}:
        return True
    return False


def main() -> None:
    changed = []

    for path in ROOT.rglob("*"):
        if not path.is_file() or should_skip(path) or not is_text_candidate(path):
            continue

        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        updated = original
        for old, new in REPLACEMENTS:
            updated = updated.replace(old, new)

        if updated != original:
            path.write_text(updated, encoding="utf-8", newline="\n")
            changed.append(path.relative_to(ROOT))

    print("Rebranding complete.")
    if changed:
        print("Changed files:")
        for item in changed:
            print(f" - {item}")
    else:
        print("No legacy LionMesh references found.")


if __name__ == "__main__":
    main()
