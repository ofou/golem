"""A read-only copy of the repository a task is about, plus the task's attachments.

The model reads it through list_files and read_file. Repository-read tools see it
mounted read-only at /repo inside the sandbox. Secrets and Golem's own state are
left out.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".golem",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
}
SKIP_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*.p12",
    "*.pfx",
    ".npmrc",
    ".pypirc",
    ".netrc",
)
KEEP = (".env.example", ".env.sample", ".env.template", ".env.dist")
MAX_FILE_BYTES = 1_000_000
INPUTS = "_inputs"


def build(repo: Path, dest: Path, attachments: list[Path] | None = None) -> list[str]:
    repo, dest = Path(repo).resolve(), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for rel in _candidate_files(repo):
        if _skipped(rel):
            continue
        source = repo / rel
        if (
            not source.is_file()
            or source.is_symlink()
            or source.stat().st_size > MAX_FILE_BYTES
        ):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(rel)
    for attachment in attachments or []:
        attachment = Path(attachment)
        target = dest / INPUTS / attachment.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(attachment, target)
        copied.append(f"{INPUTS}/{attachment.name}")
    return sorted(copied)


def _candidate_files(repo: Path) -> list[str]:
    try:
        git = shutil.which("git")
        if git is None:
            raise OSError("git is not on PATH")
        out = subprocess.run(  # noqa: S603 - fixed argv, path from shutil.which
            [
                git,
                "-C",
                str(repo),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        files = [line for line in out.splitlines() if line]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    return [str(path.relative_to(repo)) for path in repo.rglob("*") if path.is_file()]


def _skipped(rel: str) -> bool:
    parts = Path(rel).parts
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return True
    if parts[-1] in KEEP:
        return False
    return any(fnmatch.fnmatch(parts[-1], pattern) for pattern in SKIP_PATTERNS)


def resolve(root: Path, rel: str) -> Path:
    """Resolve a model-supplied path inside the snapshot, or raise."""
    root = Path(root).resolve()
    candidate = (root / rel.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path {rel!r} is outside the repository snapshot")
    return candidate
