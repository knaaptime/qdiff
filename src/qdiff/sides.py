"""Stage the two sides of a diff in scratch space.

A side is a directory (taken as it sits, uncommitted changes included) or a
git ref. Either way qdiff works on a private copy: a directory is cloned
into a scratch dir, a ref is checked out as a detached worktree. Rendering
never touches the author's tree, so nothing there is overwritten or deleted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

# Top-level names not copied from a directory side: VCS data, qdiff's own
# output, and environment trees that quarto never reads.
SKIP = {".git", ".qdiff", ".pixi", "node_modules", ".venv"}


class QdiffError(RuntimeError):
    """A failure to report to the user and exit on."""


@dataclass
class Side:
    label: str          # "old" or "new"
    spec: str           # what the user passed
    tree: str           # the staged copy qdiff renders in
    worktree_repo: str | None = None  # repo owning the worktree, for a ref


def repo_for(path: str) -> str | None:
    """The top level of the git repo containing path, or None."""
    if not os.path.isdir(path):
        return None
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def is_git_ref(spec: str, repo: str | None) -> bool:
    """True when spec is not a path and resolves to a commit in repo."""
    if repo is None or os.path.exists(spec):
        return False
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}"],
        capture_output=True,
    )
    return proc.returncode == 0


def clone_tree(src: str, dest: str) -> None:
    """Copy a directory tree, cheaply where the filesystem allows.

    On macOS `cp -c` makes APFS clones (copy-on-write, near-free even for
    large data directories); elsewhere `cp --reflink=auto` does the same on
    filesystems that support it. Skipped top-level names are removed after.
    """
    flag = "-c" if sys.platform == "darwin" else "--reflink=auto"
    proc = subprocess.run(["cp", "-R", flag, src, dest], capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest, symlinks=True,
                        ignore=lambda d, names: SKIP & set(names) if d == src else set())
        return
    for name in SKIP:
        path = os.path.join(dest, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)


def stage(label: str, spec: str, repo: str | None, scratch: str) -> Side:
    """Stage one side under scratch/<label>."""
    dest = os.path.join(scratch, label)
    if os.path.isdir(spec):
        clone_tree(os.path.abspath(spec), dest)
        return Side(label, spec, dest)
    if not is_git_ref(spec, repo):
        raise QdiffError(
            f"{spec!r} is neither a directory nor a git ref"
            + (f" in {repo}" if repo else " (no git repo to resolve it in)")
        )
    proc = subprocess.run(
        ["git", "-C", repo, "worktree", "add", "--detach", dest, spec],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise QdiffError(f"git worktree add {spec} failed:\n{proc.stderr.strip()}")
    return Side(label, spec, dest, worktree_repo=repo)


def release(side: Side) -> None:
    """Remove a staged side; a worktree is deregistered from its repo."""
    if side.worktree_repo:
        subprocess.run(
            ["git", "-C", side.worktree_repo, "worktree", "remove", "--force", side.tree],
            capture_output=True,
        )
    shutil.rmtree(side.tree, ignore_errors=True)
    if side.worktree_repo:
        subprocess.run(["git", "-C", side.worktree_repo, "worktree", "prune"],
                       capture_output=True)
