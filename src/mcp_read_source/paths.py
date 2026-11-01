"""Shared file-safety rules for reading and indexing registered projects."""
from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path


MAX_FILE_BYTES = 1024 * 1024
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".vs", ".venv", "venv", "node_modules", "vendor",
    "dist", "build", "target", "bin", "obj", "coverage", ".next", ".nuxt", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".local", "private", "ai-context",
}
SKIP_FILES = {
    ".env", ".envrc", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "credentials", "credentials.json", "secrets", "secrets.json", "id_rsa", "id_ed25519",
}
SKIP_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".enc", ".lhub"}
GENERATED_RE = re.compile(
    r"(^|/)migrations/|\.designer\.cs$|\.g\.cs$|\.g\.i\.cs$|modelsnapshot\.cs$", re.IGNORECASE,
)


def is_skipped_path(relative: Path | str) -> bool:
    """Accept a relative Path, or a relative posix string (cheaper in hot loops)."""
    parts = [part.casefold() for part in (relative.split("/") if isinstance(relative, str) else relative.parts)]
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return True
    filename = parts[-1] if parts else ""
    if filename in SKIP_FILES or filename.startswith((".env.", "credentials.", "secrets.")):
        return True
    return os.path.splitext(filename)[1] in SKIP_SUFFIXES


def is_generated(relative_posix: str) -> bool:
    return GENERATED_RE.search(relative_posix) is not None


def iter_index_files(root: Path, suffixes: tuple[str, ...]) -> Iterator[tuple[str, int, int]]:
    """Yield (relative posix path, size, mtime_ns) for indexable files, never following symlinks."""
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                    continue
                name = entry.name
                if entry.is_dir(follow_symlinks=False):
                    if name.casefold() not in SKIP_DIRS:
                        pending.append((Path(entry.path), f"{prefix}{name}/"))
                    continue
                if not name.casefold().endswith(suffixes):
                    continue
                relative = f"{prefix}{name}"
                if is_generated(relative) or is_skipped_path(relative):
                    continue
                stat = entry.stat(follow_symlinks=False)
                if stat.st_size <= MAX_FILE_BYTES:
                    yield relative, stat.st_size, stat.st_mtime_ns
            except OSError:
                continue
