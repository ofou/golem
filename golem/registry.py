"""The persistent registry, kept in the repository it serves.

    .golem/registry/active.json                 which version of each tool is live
    .golem/registry/<name>/<version>/           immutable once written
        manifest.json  tool.py  test_tool.py  test_blind.py  receipt.json
    .golem/usage.jsonl                          every call to an installed tool
    .golem/gaps.jsonl                           every gap a task exposed

Rollback moves the active pointer. It never deletes a version.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
EXPORT_FORMAT = """The registry export is /registry/tools.json, /registry/usage.json, /registry/gaps.json (access "registry-read" only):
    tools.json  [{"name", "version", "active": bool, "manifest": {"name", "version", "access", "description",
                 "input_schema", "output_schema", "gap", "created_by"}, "receipt": {"passed": bool,
                 "tests": {"ran", "ok"}, "blind_tests": {"ran", "ok"}, "stub_failed": float, "created_at"}}]
                 A tool's gap {"task_quote", "why_needed", ...} is inside its manifest, never at the top level.
    usage.json  [{"ts", "run", "tool": "name@version", "ok": bool, "seconds"}]   one row per call of an installed tool
    gaps.json   [{"ts", "run", "tool": "name@version", "gap": {"task_quote", "why_needed", ...}}]"""
BUNDLE_FILES = (
    "manifest.json",
    "tool.py",
    "test_tool.py",
    "test_blind.py",
    "receipt.json",
)


class RegistryError(Exception):
    pass


class Registry:
    def __init__(self, golem_dir: Path):
        self.golem_dir = Path(golem_dir)
        self.root = self.golem_dir / "registry"

    def active(self) -> dict[str, str]:
        path = self.root / "active.json"
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data.get("tools", {}))

    def bundle(self, name: str, version: str) -> Path:
        _check_name(name)
        path = self.root / name / version
        if not (path / "manifest.json").is_file():
            raise RegistryError(f"{name}@{version} is not in the registry")
        return path

    def manifest(self, name: str, version: str) -> dict:
        return json.loads(
            (self.bundle(name, version) / "manifest.json").read_text(encoding="utf-8")
        )

    def receipt(self, name: str, version: str) -> dict:
        return json.loads(
            (self.bundle(name, version) / "receipt.json").read_text(encoding="utf-8")
        )

    def versions(self, name: str) -> list[str]:
        folder = self.root / name
        if not folder.is_dir():
            return []
        found = [
            item.name for item in folder.iterdir() if (item / "manifest.json").is_file()
        ]
        return sorted(found, key=_version_key)

    def next_version(self, name: str) -> str:
        existing = self.versions(name)
        if not existing:
            return "0.1.0"
        major, minor, _patch = _version_key(existing[-1])
        return f"{major}.{minor + 1}.0"

    def listing(self) -> list[dict]:
        rows = []
        for name, version in sorted(self.active().items()):
            manifest = self.manifest(name, version)
            rows.append(
                {
                    "name": name,
                    "version": version,
                    "access": manifest["access"],
                    "description": manifest["description"],
                    "input_schema": manifest["input_schema"],
                    "output_schema": manifest["output_schema"],
                }
            )
        return rows

    def install(self, candidate: Path, manifest: dict, receipt: dict) -> str:
        name, version = manifest["name"], manifest["version"]
        _check_name(name)
        target = self.root / name / version
        if target.exists():
            raise RegistryError(
                f"{name}@{version} already exists; versions are immutable"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=target.parent))
        for filename in ("tool.py", "test_tool.py", "test_blind.py"):
            source = Path(candidate) / filename
            if source.is_file():
                shutil.copy2(source, staging / filename)
        _write_json(staging / "manifest.json", manifest)
        _write_json(staging / "receipt.json", receipt)
        staging.chmod(
            0o755
        )
        os.replace(staging, target)
        self._set_active(name, version)
        return f"{name}@{version}"

    def rollback(self, name: str, version: str) -> str:
        self.bundle(name, version)
        previous = self.active().get(name)
        self._set_active(name, version)
        return f"{name}: {previous} -> {version}"

    def _set_active(self, name: str, version: str) -> None:
        tools = self.active()
        tools[name] = version
        self.root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            self.root / "active.json", {"tools": dict(sorted(tools.items()))}
        )

    def record(self, journal: str, entry: dict) -> None:
        self.golem_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": round(time.time(), 3), **entry}
        with open(self.golem_dir / f"{journal}.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def journal(self, journal: str) -> list[dict]:
        path = self.golem_dir / f"{journal}.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def export(self, dest: Path) -> Path:
        """What a registry-read tool may see: manifests, receipts, history, usage. No code."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        tools = []
        for folder in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if not folder.is_dir():
                continue
            for version in self.versions(folder.name):
                manifest = self.manifest(folder.name, version)
                receipt = self.receipt(folder.name, version)
                tools.append(
                    {
                        "name": folder.name,
                        "version": version,
                        "active": self.active().get(folder.name) == version,
                        "manifest": manifest,
                        "receipt": {
                            key: receipt.get(key)
                            for key in (
                                "passed",
                                "tests",
                                "blind_tests",
                                "stub_failed",
                                "created_at",
                            )
                        },
                    }
                )
        _write_json(dest / "tools.json", tools)
        _write_json(dest / "usage.json", self.journal("usage"))
        _write_json(dest / "gaps.json", self.journal("gaps"))
        return dest


def _check_name(name: str) -> None:
    if not NAME.fullmatch(name or ""):
        raise RegistryError(
            f"invalid tool name {name!r}: use 3-41 chars of a-z, 0-9, _"
        )


def _version_key(version: str) -> tuple[int, int, int]:
    parts = (version.split(".") + ["0", "0", "0"])[:3]
    return tuple(int(part) if part.isdigit() else 0 for part in parts)  # type: ignore[return-value]


def _write_json(path: Path, data: object) -> None:
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _write_json_atomic(path: Path, data: object) -> None:
    tmp = Path(path).with_suffix(".tmp")
    _write_json(tmp, data)
    os.replace(tmp, path)
