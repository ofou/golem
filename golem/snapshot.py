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
        name = _unique_input_name(attachment.name, copied)
        target = dest / INPUTS / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(attachment, target)
        copied.append(f"{INPUTS}/{name}")
    return sorted(copied)


def _candidate_files(repo: Path) -> list[str]:
    try:
        git = shutil.which("git")
        if git is None:
            raise OSError("git is not on PATH")
        out = subprocess.run(  # noqa: S603
            [
                git,
                "-C",
                str(repo),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        ).stdout
        files = [
            item.decode("utf-8", "surrogateescape") for item in out.split(b"\0") if item
        ]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    return [str(path.relative_to(repo)) for path in repo.rglob("*") if path.is_file()]


KEEP_FOLDED = {name.casefold() for name in KEEP}
SKIP_DIRS_FOLDED = {name.casefold() for name in SKIP_DIRS}


def _secret_name(name: str) -> bool:
    """Secret filenames, matched the same way on Linux and macOS."""
    folded = name.casefold()
    if folded in KEEP_FOLDED:
        return False
    return any(
        fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in SKIP_PATTERNS
    )


def _skipped(rel: str) -> bool:
    parts = Path(rel).parts
    if any(
        part.casefold() in SKIP_DIRS_FOLDED or _secret_name(part) for part in parts[:-1]
    ):
        return True
    return _secret_name(parts[-1])


def _unique_input_name(name: str, copied: list[str]) -> str:
    """Two attachments can share a basename. Both have to survive under _inputs/."""
    candidate = name
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 2
    while f"{INPUTS}/{candidate}" in copied:
        candidate = f"{stem}-{number}{suffix}"
        number += 1
    return candidate


def resolve(root: Path, rel: str) -> Path:
    """Resolve a model-supplied path inside the snapshot, or raise."""
    root = Path(root).resolve()
    candidate = (root / rel.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path {rel!r} is outside the repository snapshot")
    return candidate
