"""Read the checked-out branch and commit from `.git` files without running git."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


@dataclass(frozen=True)
class HeadInfo:
    branch: str | None
    commit: str | None

    @property
    def label(self) -> str:
        if self.branch:
            return self.branch
        if self.commit:
            return f"detached-{self.commit[:7]}"
        return "nogit"


def read_head(root: Path) -> HeadInfo:
    try:
        located = _locate_git_dirs(root)
        if located is None:
            return HeadInfo(None, None)
        git_dir, common_dir = located
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            return HeadInfo(branch, _read_ref(git_dir, common_dir, ref))
        return HeadInfo(None, head if SHA_RE.fullmatch(head) else None)
    except (OSError, UnicodeDecodeError):
        return HeadInfo(None, None)


def _locate_git_dirs(root: Path) -> tuple[Path, Path] | None:
    for candidate in (root, *root.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return dot_git, dot_git
        if dot_git.is_file():
            content = dot_git.read_text(encoding="utf-8").strip()
            if not content.startswith("gitdir:"):
                return None
            git_dir = (candidate / content[7:].strip()).resolve()
            common_dir = git_dir
            commondir_file = git_dir / "commondir"
            if commondir_file.is_file():
                common_dir = (git_dir / commondir_file.read_text(encoding="utf-8").strip()).resolve()
            return git_dir, common_dir
    return None


def _read_ref(git_dir: Path, common_dir: Path, ref: str) -> str | None:
    if ".." in Path(ref).parts:
        return None
    for base in (git_dir, common_dir):
        path = base / ref
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            return value if SHA_RE.fullmatch(value) else None
    packed = common_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref and SHA_RE.fullmatch(parts[0]):
                return parts[0]
    return None
