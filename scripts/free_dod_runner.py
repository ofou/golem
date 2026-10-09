#!/usr/bin/env python3
"""Clone agent repositories outside this checkout and run Golem in Docker.

The clones live under ~/golem-runner (override with --root). Nothing is written
into demo/targets. Each repository is a shallow sparse checkout, then
scripts/golem-docker runs every task in the catalog entry against the same
checkout so the registry accumulates. Entries with `"dod": true` finish with
scripts/dod_check.py.

    python3 scripts/local_runner.py --rebuild
    python3 scripts/local_runner.py --dod-only
    python3 scripts/local_runner.py --only pi,cline
    python3 scripts/local_runner.py --clone-only
    python3 scripts/local_runner.py --truth

Command Code is listed and skipped: its public repository has no source.
Claude Code's public repository is the plugins tree, not the closed agent.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

GOLEM_ROOT = Path(__file__).resolve().parent.parent
CATALOG = Path(__file__).resolve().parent / "runner_repos.json"
DOCKER_SCRIPT = GOLEM_ROOT / "scripts" / "golem-docker"
DOD_CHECK = GOLEM_ROOT / "scripts" / "dod_check.py"
FORBIDDEN = (GOLEM_ROOT.parent / "demo" / "targets").resolve()
IMAGE = os.environ.get("GOLEM_IMAGE", "golem:local")
TS_EXTS = (".ts", ".tsx", ".js", ".jsx")
TS_IMPORTS = (
    re.compile(r"""\bfrom\s+["'](\.[^"']+)["']"""),
    re.compile(r"""\bimport\s+["'](\.[^"']+)["']"""),
    re.compile(r"""\bimport\s*\(\s*["'](\.[^"']+)["']\s*\)"""),
)


@dataclass(frozen=True)
class TaskSpec:
    phase: str
    task: str
    kind: str


@dataclass(frozen=True)
class Repo:
    name: str
    github: str
    sparse: tuple[str, ...]
    kind: str
    tasks: tuple[TaskSpec, ...]
    dod: bool
    skip: str
    root: str
    globs: tuple[str, ...]
    workspace: str
    crate: str

    @classmethod
    def from_json(cls, item: dict[str, object]) -> Repo:
        def text(key: str) -> str:
            value = item.get(key, "")
            return value if isinstance(value, str) else ""

        def seq(key: str) -> tuple[str, ...]:
            value = item.get(key, [])
            if not isinstance(value, list):
                return ()
            return tuple(part for part in value if isinstance(part, str))

        tasks = _tasks_from_json(item)
        return cls(
            name=text("name"),
            github=text("github"),
            sparse=seq("sparse"),
            kind=text("kind") or (tasks[0].kind if tasks else ""),
            tasks=tasks,
            dod=bool(item.get("dod")),
            skip=text("skip"),
            root=text("root"),
            globs=seq("globs"),
            workspace=text("workspace"),
            crate=text("crate"),
        )


def _tasks_from_json(item: dict[str, object]) -> tuple[TaskSpec, ...]:
    raw = item.get("tasks")
    if isinstance(raw, list) and raw:
        out: list[TaskSpec] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            task = entry.get("task", "")
            if not isinstance(task, str) or not task.strip():
                continue
            phase = entry.get("phase", "run")
            kind = entry.get("kind", "")
            out.append(
                TaskSpec(
                    phase=phase if isinstance(phase, str) and phase else "run",
                    task=task,
                    kind=kind if isinstance(kind, str) else "",
                )
            )
        if out:
            return tuple(out)
    legacy = item.get("task", "")
    if isinstance(legacy, str) and legacy.strip():
        kind = item.get("kind", "")
        return (
            TaskSpec(
                phase="run",
                task=legacy,
                kind=kind if isinstance(kind, str) else "",
            ),
        )
    return ()


def main(argv: list[str] | None = None) -> int:
    _reexec_if_old()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma-separated repo names")
    parser.add_argument(
        "--dod-only",
        action="store_true",
        help="only catalog entries with dod: true (multi-task definition-of-done sequences)",
    )
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--root", type=Path, default=Path.home() / "golem-runner")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--clone-only", action="store_true")
    parser.add_argument("--truth", action="store_true")
    parser.add_argument("--max-mb", type=int, default=1500)
    parser.add_argument("--disk-budget-mb", type=int, default=8000)
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    if root == FORBIDDEN or FORBIDDEN in root.parents:
        print(f"refusing to use {FORBIDDEN}", file=sys.stderr)
        return 2
    if args.jobs < 1:
        print("--jobs must be at least 1", file=sys.stderr)
        return 2

    selected = _select(_load_catalog(), args.only, args.dod_only)
    if (
        not args.clone_only
        and not args.truth
        and not os.environ.get("OPENROUTER_API_KEY")
    ):
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2
    needs_image = not args.truth and not args.clone_only and not _image_exists()
    if (args.rebuild or needs_image) and _rebuild() != 0:
        return 1

    src = root / "src"
    truth_dir = root / "truth"
    log_dir = root / "logs"
    src.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)
    checkouts: list[tuple[Repo, Path, str]] = []
    results: list[dict[str, object]] = []
    used_mb = 0
    for repo in selected:
        if repo.skip:
            _log(f"{repo.name}: skipped — {repo.skip}")
            results.append(
                {"name": repo.name, "status": "skipped", "reason": repo.skip}
            )
            continue
        if not repo.tasks:
            _log(f"{repo.name}: skipped — no tasks in catalog")
            results.append(
                {"name": repo.name, "status": "skipped", "reason": "no tasks"}
            )
            continue
        if used_mb >= args.disk_budget_mb or _free_mb(root) < 2500:
            _log(
                f"{repo.name}: skipped — disk budget ({used_mb} MB used, {_free_mb(root)} MB free)"
            )
            results.append(
                {"name": repo.name, "status": "skipped", "reason": "disk budget"}
            )
            continue
        dest, sha, size_mb, error = _clone(repo, src)
        if error or dest is None or sha is None or size_mb is None:
            results.append(
                {"name": repo.name, "status": "clone-failed", "reason": error}
            )
            continue
        if size_mb > args.max_mb:
            shutil.rmtree(dest, ignore_errors=True)
            _log(
                f"{repo.name}: removed checkout, {size_mb} MB is over the {args.max_mb} MB cap"
            )
            results.append(
                {
                    "name": repo.name,
                    "status": "skipped",
                    "reason": f"checkout {size_mb} MB exceeds {args.max_mb} MB",
                }
            )
            continue
        used_mb += size_mb
        _write_truth(repo, dest, sha, truth_dir)
        checkouts.append((repo, dest, sha))
        _log(
            f"{repo.name}: {sha[:12]} {size_mb} MB at {dest} "
            f"({len(repo.tasks)} task{'s' if len(repo.tasks) != 1 else ''}"
            f"{', dod' if repo.dod else ''})"
        )

    (root / "clones.json").write_text(
        json.dumps(
            [
                {
                    "name": repo.name,
                    "sha": sha,
                    "path": str(dest),
                    "dod": repo.dod,
                    "tasks": len(repo.tasks),
                }
                for repo, dest, sha in checkouts
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if args.clone_only or args.truth:
        _write_results(root, results)
        return 0 if all(row["status"] != "clone-failed" for row in results) else 1

    run_rows = _run_all(checkouts, root, log_dir, args.jobs)
    results.extend(run_rows)
    _write_results(root, results)
    failed = [row for row in run_rows if row["status"] != "ok"]
    return 1 if failed else 0


def _reexec_if_old() -> None:
    if sys.version_info >= (3, 11):  # noqa: UP036
        return
    venv = GOLEM_ROOT / ".venv" / "bin" / "python"
    if venv.is_file():
        os.execv(venv, [str(venv), *sys.argv])  # noqa: S606


def _load_catalog() -> list[Repo]:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"{CATALOG} must be a list")
    repos = [Repo.from_json(item) for item in raw if isinstance(item, dict)]
    names = [repo.name for repo in repos]
    if len(names) != len(set(names)):
        raise SystemExit("duplicate names in runner_repos.json")
    return repos


def _select(repos: list[Repo], only: str, dod_only: bool) -> list[Repo]:
    chosen = [repo for repo in repos if repo.dod] if dod_only else list(repos)
    if not only:
        return chosen
    wanted = {part.strip() for part in only.split(",") if part.strip()}
    filtered = [repo for repo in chosen if repo.name in wanted]
    missing = wanted - {repo.name for repo in filtered}
    if missing:
        raise SystemExit(f"unknown repo: {', '.join(sorted(missing))}")
    return filtered


def _exe(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} is not on PATH")
    return found


def _exec(
    cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> int:
    _log("+ " + " ".join(cmd))
    completed = subprocess.run(cmd, cwd=cwd, env=env, check=False)  # noqa: S603
    return completed.returncode


def _image_exists() -> bool:
    docker = _exe("docker")
    completed = subprocess.run(  # noqa: S603
        [docker, "image", "inspect", IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _rebuild() -> int:
    return _exec([_exe("docker"), "build", "-t", IMAGE, str(GOLEM_ROOT)])


def _clone(repo: Repo, src: Path) -> tuple[Path | None, str | None, int | None, str]:
    dest = src / repo.name
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    git = _exe("git")
    if not (dest / ".git").is_dir():
        if dest.exists():
            shutil.rmtree(dest)
        code = _exec(
            [
                git,
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--sparse",
                f"https://github.com/{repo.github}.git",
                str(dest),
            ],
            env=env,
        )
        if code != 0:
            return None, None, None, f"git clone exited {code}"
    code = _exec(
        [git, "-C", str(dest), "sparse-checkout", "set", "--cone", *repo.sparse],
        env=env,
    )
    if code != 0:
        return None, None, None, f"sparse-checkout exited {code}"
    sha = subprocess.run(  # noqa: S603
        [git, "-C", str(dest), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if sha.returncode != 0:
        return None, None, None, "rev-parse failed"
    return dest, sha.stdout.strip(), _dir_mb(dest), ""


def _dir_mb(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total // (1024 * 1024)


def _free_mb(path: Path) -> int:
    return shutil.disk_usage(path).free // (1024 * 1024)


def _write_truth(repo: Repo, dest: Path, sha: str, truth_dir: Path) -> None:
    steps = [
        (i, step)
        for i, step in enumerate(repo.tasks)
        if step.kind or (i == 0 and repo.kind)
    ]
    if not steps and repo.kind:
        steps = [(0, TaskSpec(phase="run", task="", kind=repo.kind))]
    for index, step in steps:
        kind = step.kind or repo.kind
        stem = f"{repo.name}-{step.phase}-{index}" if len(steps) > 1 else repo.name
        try:
            payload: dict[str, object] = {
                "name": repo.name,
                "sha": sha,
                "kind": kind,
                "phase": step.phase,
                "task_index": index,
            }
            payload.update(_truth_for_kind(repo, dest, kind))
        except Exception as exc:
            payload = {
                "name": repo.name,
                "sha": sha,
                "kind": kind,
                "phase": step.phase,
                "task_index": index,
                "error": str(exc),
            }
            _log(f"{repo.name}[{index}] truth failed: {exc}")
        (truth_dir / f"{stem}.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        summary = payload.get("summary")
        if isinstance(summary, str):
            _log(f"{repo.name}[{index}] truth: {summary}")


def _truth_for_kind(repo: Repo, dest: Path, kind: str) -> dict[str, object]:
    if kind == "python-imports":
        return _python_imports(dest, repo.root)
    if kind == "python-file-counts":
        return _python_file_counts(dest, repo.root)
    if kind == "plugin-files":
        return _plugin_files(dest, repo.root)
    if kind == "package-deps":
        return _package_deps(dest, repo.globs)
    if kind == "package-files":
        return _package_files(dest, repo.globs)
    if kind == "cargo-dependents":
        return _cargo_dependents(dest, repo.workspace, repo.crate)
    if kind == "ts-imports":
        return _ts_imports(dest, repo.root)
    return {"summary": f"no truth for kind {kind}"}


def _visible(repo: Path) -> list[str]:
    sys.path.insert(0, str(GOLEM_ROOT))
    from golem.snapshot import MAX_FILE_BYTES, _candidate_files, _skipped

    rels: list[str] = []
    for rel in _candidate_files(repo):
        if _skipped(rel):
            continue
        source = repo / rel
        try:
            if not source.is_file() or source.is_symlink():
                continue
            if source.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        rels.append(rel)
    return rels


def _python_imports(repo: Path, root: str) -> dict[str, object]:
    modules: dict[str, str] = {}
    for rel in _visible(repo):
        path = Path(rel)
        if path.parts[0] != root or path.suffix != ".py":
            continue
        if path.name == "__init__.py" and path.parent == Path(root):
            continue
        modules[_module_name(path)] = rel
    known = set(modules)
    imported_by: dict[str, set[str]] = defaultdict(set)
    importers = 0
    for module, rel in modules.items():
        targets = _import_targets(repo / rel, module, known)
        targets.discard(module)
        if not targets:
            continue
        importers += 1
        for target in targets:
            imported_by[target].add(module)
    ranking = sorted(
        ((len(users), name) for name, users in imported_by.items()),
        reverse=True,
    )
    top = ranking[0][0] if ranking else 0
    most = [name for count, name in ranking if count == top and top]
    summary = (
        f"{len(modules)} modules, {importers} import another tools module, "
        f"most imported ({top}): {', '.join(most) or 'none'}"
    )
    return {
        "summary": summary,
        "modules": len(modules),
        "importers": importers,
        "most": [{"module": name, "importers": top} for name in most],
    }


def _module_name(path: Path) -> str:
    parts = list(path.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = path.stem
    return ".".join(parts)


def _import_targets(path: Path, module: str, known: set[str]) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.update(_known_prefix(alias.name, known))
        elif isinstance(node, ast.ImportFrom):
            base = _relative_base(
                module, path.name == "__init__.py", node.level, node.module
            )
            if base is None:
                continue
            names = [alias.name for alias in node.names if alias.name != "*"]
            children = []
            for name in names:
                child = f"{base}.{name}" if base else name
                if child in known:
                    children.append(child)
            found.update(children)
            # `from . import sibling` names the sibling. `from package import symbol`
            # loads package, and so does a relative import of a symbol that is not itself a module.
            if base in known and (node.level == 0 or node.module or not children):
                found.add(base)
    return found


def _known_prefix(name: str, known: set[str]) -> set[str]:
    if name in known:
        return {name}
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in known:
            return {candidate}
        parts.pop()
    return set()


def _relative_base(
    module: str, is_init: bool, level: int, imported: str | None
) -> str | None:
    if level == 0:
        return imported or ""
    parts = module.split(".")
    if not is_init:
        parts = parts[:-1]
    drop = level - 1
    if drop:
        if drop > len(parts):
            return None
        parts = parts[:-drop]
    if imported:
        parts.extend(imported.split("."))
    return ".".join(parts)


def _plugin_files(repo: Path, root: str) -> dict[str, object]:
    counts: dict[str, int] = defaultdict(int)
    for rel in _visible(repo):
        parts = Path(rel).parts
        if len(parts) > 2 and parts[0] == root:
            counts[parts[1]] += 1
    rows = []
    for directory in sorted(counts):
        manifest = repo / root / directory / ".claude-plugin" / "plugin.json"
        name = directory
        if manifest.is_file():
            try:
                parsed = json.loads(manifest.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict) and isinstance(parsed.get("name"), str):
                name = parsed["name"]
        rows.append({"dir": directory, "name": name, "files": counts[directory]})
    top = max((row["files"] for row in rows), default=0)
    most = [row for row in rows if row["files"] == top and top]
    names = ", ".join(f"{row['dir']} ({row['files']})" for row in most) or "none"
    return {
        "summary": f"{len(rows)} plugins, most files: {names}",
        "plugins": rows,
        "most": most,
    }


def _package_deps(repo: Path, globs: tuple[str, ...]) -> dict[str, object]:
    paths = _package_paths(repo, globs)
    names: dict[str, str] = {}
    raw: dict[str, Path] = {}
    for path in paths:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("name"), str):
            continue
        names[parsed["name"]] = str(path.parent.relative_to(repo))
        raw[parsed["name"]] = path
    edges: dict[str, set[str]] = {name: set() for name in names}
    for name, path in raw.items():
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            continue
        for field in ("dependencies", "devDependencies", "peerDependencies"):
            table = parsed.get(field)
            if not isinstance(table, dict):
                continue
            for dep in table:
                if isinstance(dep, str) and dep in names and dep != name:
                    edges[name].add(dep)
    return _graph_summary(names, edges)


def _package_paths(repo: Path, globs: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for pattern in globs:
        if pattern.endswith("/*"):
            parent = repo / pattern[:-2]
            if not parent.is_dir():
                continue
            for child in sorted(parent.iterdir()):
                manifest = child / "package.json"
                if manifest.is_file():
                    found.append(manifest)
            continue
        manifest = repo / pattern / "package.json"
        if manifest.is_file():
            found.append(manifest)
    return found


def _package_files(repo: Path, globs: tuple[str, ...]) -> dict[str, object]:
    visible = set(_visible(repo))
    rows: list[dict[str, object]] = []
    for path in _package_paths(repo, globs):
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("name"), str):
            continue
        package_dir = path.parent.relative_to(repo).as_posix()
        prefix = package_dir + "/"
        count = sum(
            1 for rel in visible if rel == package_dir or rel.startswith(prefix)
        )
        rows.append({"name": parsed["name"], "dir": package_dir, "files": count})
    rows.sort(key=lambda row: str(row["name"]))
    summary = f"{len(rows)} packages, file counts: " + ", ".join(
        f"{row['name']}={row['files']}" for row in rows
    )
    return {"summary": summary, "packages": rows}


def _python_file_counts(repo: Path, root: str) -> dict[str, object]:
    counts: dict[str, int] = defaultdict(int)
    for rel in _visible(repo):
        path = Path(rel)
        if path.parts[0] != root or path.suffix != ".py":
            continue
        if len(path.parts) < 3:
            continue
        counts[path.parts[1]] += 1
    rows = [{"dir": name, "files": counts[name]} for name in sorted(counts)]
    top = max((row["files"] for row in rows), default=0)
    most = [row["dir"] for row in rows if row["files"] == top and top]
    summary = (
        f"{sum(counts.values())} .py files under {root}/, "
        f"most in ({top}): {', '.join(most) or 'none'}"
    )
    return {
        "summary": summary,
        "subdirs": rows,
        "most": [{"dir": name, "files": top} for name in most],
    }


def _graph_summary(
    directories: dict[str, str], edges: dict[str, set[str]]
) -> dict[str, object]:
    nodes = set(directories)
    edge_list = sorted((src, dest) for src, dests in edges.items() for dest in dests)
    order = _topo(nodes, edges)
    cycle = None if order is not None else _one_cycle(nodes, edges)
    if order is not None:
        summary = f"{len(nodes)} packages, {len(edge_list)} edges, order is acyclic"
    else:
        summary = f"{len(nodes)} packages, {len(edge_list)} edges, cycle: {' -> '.join(cycle or [])}"
    return {
        "summary": summary,
        "packages": [
            {"name": name, "dir": directories[name], "depends_on": sorted(edges[name])}
            for name in sorted(directories)
        ],
        "edges": [{"from": src, "to": dest} for src, dest in edge_list],
        "order": order,
        "cycle": cycle,
    }


def _topo(nodes: set[str], edges: dict[str, set[str]]) -> list[str] | None:
    """edges[A] are packages A depends on, which must come first."""
    incoming = dict.fromkeys(nodes, 0)
    dependents: dict[str, set[str]] = {node: set() for node in nodes}
    for src, dests in edges.items():
        for dest in dests:
            if dest not in nodes or src not in nodes:
                continue
            incoming[src] += 1
            dependents[dest].add(src)
    ready = sorted(node for node, degree in incoming.items() if degree == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        nxt = []
        for child in sorted(dependents[node]):
            incoming[child] -= 1
            if incoming[child] == 0:
                nxt.append(child)
        ready.extend(nxt)
        ready.sort()
    if len(order) != len(nodes):
        return None
    return order


def _one_cycle(nodes: set[str], edges: dict[str, set[str]]) -> list[str] | None:
    color: dict[str, int] = {}
    stack: list[str] = []

    def walk(node: str) -> list[str] | None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            state = color.get(nxt, 0)
            if state == 0:
                found = walk(nxt)
                if found:
                    return found
            elif state == 1:
                return stack[stack.index(nxt) :] + [nxt]
        stack.pop()
        color[node] = 2
        return None

    for node in sorted(nodes):
        if color.get(node, 0) == 0:
            found = walk(node)
            if found:
                return found
    return None


def _cargo_dependents(repo: Path, workspace: str, focus: str) -> dict[str, object]:
    root = repo / workspace
    data = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    ws = data.get("workspace")
    if not isinstance(ws, dict):
        return {
            "summary": f"{workspace}/Cargo.toml has no [workspace]",
            "error": "no workspace",
        }
    members = [item for item in ws.get("members", []) if isinstance(item, str)]
    excluded = {item for item in ws.get("exclude", []) if isinstance(item, str)}
    crates: dict[str, Path] = {}
    for member in _expand_members(root, members):
        rel = member.relative_to(root).as_posix()
        if rel in excluded:
            continue
        manifest = tomllib.loads((member / "Cargo.toml").read_text(encoding="utf-8"))
        package = manifest.get("package")
        if isinstance(package, dict) and isinstance(package.get("name"), str):
            crates[package["name"]] = member
    dependents: dict[str, set[str]] = {name: set() for name in crates}
    by_path = {path.resolve(): name for name, path in crates.items()}
    for name, path in crates.items():
        manifest = tomllib.loads((path / "Cargo.toml").read_text(encoding="utf-8"))
        for dep in _crate_deps(manifest, path, by_path):
            if dep in dependents and dep != name:
                dependents[dep].add(name)
    ranking = sorted(
        ((len(users), name) for name, users in dependents.items()),
        reverse=True,
    )
    top5 = [{"crate": name, "dependents": count} for count, name in ranking[:5]]
    focus_n = len(dependents.get(focus, ()))
    top_text = ", ".join(f"{row['crate']} {row['dependents']}" for row in top5)
    summary = f"{len(crates)} crates, {focus} has {focus_n} direct dependents, top: {top_text}"
    return {
        "summary": summary,
        "crates": len(crates),
        "focus": focus,
        "focus_dependents": focus_n,
        "top5": top5,
    }


def _expand_members(workspace: Path, members: list[str]) -> list[Path]:
    found: list[Path] = []
    for member in members:
        if any(char in member for char in "*?[]"):
            for path in sorted(workspace.glob(member)):
                if (path / "Cargo.toml").is_file():
                    found.append(path)
            continue
        path = workspace / member
        if (path / "Cargo.toml").is_file():
            found.append(path)
    return found


def _crate_deps(
    manifest: dict[str, object], crate_dir: Path, by_path: dict[Path, str]
) -> set[str]:
    found: set[str] = set()
    for field in ("dependencies", "dev-dependencies", "build-dependencies"):
        found.update(_dep_table(manifest.get(field), crate_dir, by_path))
    return found


def _dep_table(table: object, crate_dir: Path, by_path: dict[Path, str]) -> set[str]:
    if not isinstance(table, dict):
        return set()
    found: set[str] = set()
    for key, value in table.items():
        if not isinstance(key, str):
            continue
        package = key
        if isinstance(value, dict):
            renamed = value.get("package")
            if isinstance(renamed, str):
                package = renamed
            rel = value.get("path")
            if isinstance(rel, str):
                resolved = (crate_dir / rel).resolve()
                if resolved in by_path:
                    package = by_path[resolved]
        found.add(package)
    return found


def _ts_imports(repo: Path, root: str) -> dict[str, object]:
    files = [
        rel
        for rel in _visible(repo)
        if rel.startswith(root + "/") and Path(rel).suffix in {".ts", ".tsx"}
    ]
    file_set = {(repo / rel).resolve() for rel in files}
    imported_by: dict[str, set[str]] = defaultdict(set)
    importers = 0
    for rel in files:
        path = repo / rel
        targets = _ts_targets(path, repo / root, file_set)
        targets.discard(str(Path(rel)))
        if not targets:
            continue
        importers += 1
        for target in targets:
            imported_by[target].add(rel)
    ranking = sorted(
        ((len(users), name) for name, users in imported_by.items()),
        reverse=True,
    )
    top = ranking[0][0] if ranking else 0
    most = [name for count, name in ranking if count == top and top]
    summary = (
        f"{len(files)} files, {importers} import another src file, "
        f"most imported ({top}): {', '.join(most) or 'none'}"
    )
    return {
        "summary": summary,
        "files": len(files),
        "importers": importers,
        "most": [{"file": name, "importers": top} for name in most],
    }


def _ts_targets(path: Path, src_root: Path, files: set[Path]) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    found: set[str] = set()
    specs = [
        match.group(1) for pattern in TS_IMPORTS for match in pattern.finditer(text)
    ]
    for spec in specs:
        resolved = _resolve_ts(path, src_root, spec.split("?", 1)[0].split("#", 1)[0])
        if resolved in files:
            found.add(str(resolved.relative_to(src_root.parent)))
    return found


def _resolve_ts(importer: Path, src_root: Path, spec: str) -> Path | None:
    if not spec.startswith("."):
        return None
    base = (importer.parent / spec).resolve()
    candidates = [base]
    if base.suffix:
        candidates.extend(base.with_suffix(ext) for ext in TS_EXTS)
    else:
        candidates.extend(base.with_suffix(ext) for ext in TS_EXTS)
        candidates.extend(base / f"index{ext}" for ext in TS_EXTS)
    for candidate in candidates:
        try:
            candidate.relative_to(src_root)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _run_all(
    checkouts: list[tuple[Repo, Path, str]],
    root: Path,
    log_dir: Path,
    jobs: int,
) -> list[dict[str, object]]:
    log_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(_run_repo_sequence, repo, dest, sha, root, log_dir): repo.name
            for repo, dest, sha in checkouts
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {
                    "name": name,
                    "status": "failed",
                    "exit": 1,
                    "reason": str(exc),
                }
                _log(f"{name}: sequence crashed — {exc}")
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("name", "")))
    return rows


def _run_repo_sequence(
    repo: Repo,
    dest: Path,
    sha: str,
    root: Path,
    log_dir: Path,
) -> dict[str, object]:
    work = root / "work" / repo.name
    work.mkdir(parents=True, exist_ok=True)
    golem_dir = dest / ".golem"
    if golem_dir.exists():
        shutil.rmtree(golem_dir)
        _log(f"{repo.name}: cleared previous .golem for a fresh registry")

    env = os.environ.copy()
    env["GOLEM_WORK"] = str(work)
    env["GOLEM_IMAGE"] = IMAGE
    step_rows: list[dict[str, object]] = []
    for index, step in enumerate(repo.tasks):
        log_path = log_dir / f"{repo.name}-{index:02d}-{step.phase}.log"
        _log(
            f"{repo.name}: task {index + 1}/{len(repo.tasks)} "
            f"({step.phase}), log {log_path}"
        )
        with log_path.open("w", encoding="utf-8") as handle:
            handle.write(
                f"github {repo.github}\nsha {sha}\nphase {step.phase}\n"
                f"task {step.task}\n\n"
            )
            handle.flush()
            code = subprocess.run(  # noqa: S603
                [_exe("bash"), str(DOCKER_SCRIPT), str(dest), "run", step.task],
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                check=False,
            ).returncode
        step_row = _step_row(index, step, code, log_path)
        step_rows.append(step_row)
        _log(f"{repo.name}[{index}] exit {code} — {_brief(log_path)}")
        if code != 0:
            return {
                "name": repo.name,
                "status": "failed",
                "exit": code,
                "dod": repo.dod,
                "steps": step_rows,
                "failed_at": index,
                "log": str(log_path),
            }

    dod_ok: bool | None = None
    dod_log = ""
    if repo.dod:
        dod_path = log_dir / f"{repo.name}-dod_check.log"
        _log(f"{repo.name}: dod_check → {dod_path}")
        with dod_path.open("w", encoding="utf-8") as handle:
            dod_code = subprocess.run(  # noqa: S603
                [sys.executable, str(DOD_CHECK), str(dest)],
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            ).returncode
        dod_ok = dod_code == 0
        dod_log = str(dod_path)
        _log(
            f"{repo.name}: dod_check {'PASS' if dod_ok else 'FAIL'} "
            f"(exit {dod_code}) — {_dod_brief(dod_path)}"
        )
        if not dod_ok:
            return {
                "name": repo.name,
                "status": "failed",
                "exit": dod_code,
                "dod": True,
                "dod_ok": False,
                "dod_log": dod_log,
                "steps": step_rows,
                "reason": "dod_check failed",
            }

    last = step_rows[-1] if step_rows else {}
    return {
        "name": repo.name,
        "status": "ok",
        "exit": 0,
        "dod": repo.dod,
        "dod_ok": dod_ok,
        "dod_log": dod_log,
        "spend": last.get("spend", ""),
        "registry_after": last.get("registry_after", ""),
        "installed": any(bool(row.get("installed")) for row in step_rows),
        "steps": step_rows,
        "log": str(last.get("log", "")),
    }


def _step_row(
    index: int,
    step: TaskSpec,
    code: int,
    log_path: Path,
) -> dict[str, object]:
    text = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.is_file()
        else ""
    )
    return {
        "index": index,
        "phase": step.phase,
        "kind": step.kind,
        "status": "ok" if code == 0 else "failed",
        "exit": code,
        "spend": _line_after(text, "[spend] "),
        "registry_after": _line_after(text, "[registry] after: "),
        "installed": "[install] " in text,
        "log": str(log_path),
    }


def _brief(log_path: Path) -> str:
    if not log_path.is_file():
        return "no log"
    lines = [
        line
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith(
            ("[spend] ", "[registry] after", "[install] ", "[verdict] ", "[refused] ")
        )
    ]
    return " | ".join(lines[-6:]) or "no verdict yet"


def _dod_brief(log_path: Path) -> str:
    if not log_path.is_file():
        return "no log"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    fails = [line for line in lines if line.startswith("FAIL")]
    summary = [line for line in lines if "checks pass" in line]
    return " | ".join(fails + summary) or "see log"


def _line_after(text: str, prefix: str) -> str:
    for line in reversed(text.splitlines()):
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _write_results(root: Path, rows: list[dict[str, object]]) -> None:
    (root / "results.json").write_text(
        json.dumps(rows, indent=2) + "\n", encoding="utf-8"
    )


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
