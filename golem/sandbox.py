"""Where generated code runs: a throwaway container with no network, no environment,
a read-only filesystem, no Linux capabilities, a non-root user, and hard limits.

Mounts, all read-only:
    /tool       the candidate or installed bundle
    /golem      the kernel's runner (sandbox_runner.py)
    /inputs     the task's attachments, for every tool
    /repo       the repository snapshot, only for repository-read tools
    /registry   the registry export (manifests, receipts, usage), only for registry-read tools

The process that holds the OpenRouter key never imports or executes generated code.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from golem.licence import Licence
from golem.sandbox_runner import MARK
from golem.sandbox_tests import MARK as TESTS_MARK

RUNNER = Path(__file__).with_name("sandbox_runner.py")
TESTS_RUNNER = Path(__file__).with_name("sandbox_tests.py")
STUBS = {
    "raises": """def run(args):
    raise NotImplementedError("golem stub: there is no implementation here")
""",
    "empty": """def run(args):
    return {}
""",
}
MAX_OUTPUT = 20_000  # stderr kept for the log
MAX_STDOUT = 2_000_000  # stdout kept for parsing; the 120 s timeout bounds it anyway
MAX_RESULT = 20_000  # a tool result larger than this would flood the model's context


@dataclass
class TestReport:
    passed: bool
    ran: int
    ok: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    output: str = ""
    seconds: float = 0.0
    command: str = ""

    def summary(self) -> str:
        return f"{len(self.ok)}/{self.ran} passed" if self.ran else "no tests ran"


class SandboxError(Exception):
    pass


class Sandbox:
    def __init__(
        self, licence: Licence, snapshot_dir: Path, registry_export: Path | None = None
    ):
        self.licence = licence
        self.snapshot_dir = Path(snapshot_dir)
        self.registry_export = registry_export
        self.calls = 0
        # Everything a sandbox container mounts lives next to the snapshot, so the paths exist on the
        # Docker host even when Golem itself runs in a container that talks to the host's Docker.
        self.runtime = self.snapshot_dir.parent / "golem-runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.runner = self.runtime / "runner.py"
        self.tests_runner = self.runtime / "tests.py"
        shutil.copyfile(RUNNER, self.runner)
        shutil.copyfile(TESTS_RUNNER, self.tests_runner)

    @staticmethod
    def available() -> bool:
        docker = shutil.which("docker")
        if docker is None:
            return False
        probe = subprocess.run(  # noqa: S603 - fixed argv, path from shutil.which
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return probe.returncode == 0

    # -- public ------------------------------------------------------------

    def run_tests(
        self, bundle: Path, access: str, pattern: str = "test_*.py"
    ) -> TestReport:
        code, out, seconds, shown = self._docker(
            bundle, access, ["python", "/golem/tests.py", pattern], stdin=None
        )
        return _parse_tests(out, code, seconds, shown)

    def run_tests_against_stub(
        self,
        bundle: Path,
        access: str,
        pattern: str = "test_*.py",
        kind: str = "raises",
    ) -> TestReport:
        """Run the same tests against a stub that raises, or one that returns {}. Good tests fail on both."""
        with tempfile.TemporaryDirectory(prefix="stub-", dir=self.runtime) as tmp:
            stub = Path(tmp)
            stub.chmod(
                0o755
            )  # mkdtemp makes 0700, which uid 65534 cannot read on Linux
            for test_file in Path(bundle).glob("test_*.py"):
                shutil.copy2(test_file, stub / test_file.name)
            (stub / "tool.py").write_text(STUBS[kind], encoding="utf-8")
            return self.run_tests(stub, access, pattern)

    def invoke(self, bundle: Path, access: str, args: dict) -> dict:
        started = time.monotonic()
        code, out, _seconds, _shown = self._docker(
            bundle, access, ["python", "/golem/runner.py"], stdin=json.dumps(args)
        )
        for line in reversed(out.splitlines()):
            if line.startswith(MARK):
                size = len(line) - len(MARK)
                if size > MAX_RESULT:
                    return {
                        "ok": False,
                        "error": f"the result is {size} characters, over the {MAX_RESULT} limit: return less "
                        "(filter by an argument, summarize, or page)",
                        "seconds": round(time.monotonic() - started, 2),
                    }
                payload = json.loads(line[len(MARK) :])
                payload["seconds"] = round(time.monotonic() - started, 2)
                return payload
        return {
            "ok": False,
            "error": f"sandbox exited {code} without a result: {out[-800:]}",
            "seconds": round(time.monotonic() - started, 2),
        }

    # -- docker ------------------------------------------------------------

    def _docker(
        self, bundle: Path, access: str, command: list[str], stdin: str | None
    ) -> tuple[int, str, float, str]:
        limit = self.licence.budget("max_sandbox_calls_per_task")
        if self.calls >= limit:
            raise SandboxError(f"sandbox call budget exhausted ({limit} per task)")
        self.calls += 1
        box = self.licence.data["sandbox"]
        name = f"golem-{uuid.uuid4().hex[:12]}"
        mounts = [
            (Path(bundle).resolve(), "/tool"),
            (self.runner.resolve(), "/golem/runner.py"),
            (self.tests_runner.resolve(), "/golem/tests.py"),
        ]
        inputs = self.snapshot_dir / "_inputs"
        if inputs.is_dir():
            mounts.append((inputs.resolve(), "/inputs"))
        if access == "repository-read":
            mounts.append((self.snapshot_dir.resolve(), "/repo"))
        if access == "registry-read" and self.registry_export is not None:
            mounts.append((Path(self.registry_export).resolve(), "/registry"))
        for host, _inside in mounts:
            _readable_by_sandbox(host, files=(host == mounts[0][0]))
        docker = shutil.which("docker")
        if docker is None:
            raise SandboxError("docker is not on PATH")
        cmd = [
            docker,
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            box.get("network", "none"),
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",  # noqa: S108 - sandbox scratch, not a host secret path
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(box["pids"]),
            "--memory",
            box["memory"],
            "--cpus",
            str(box["cpus"]),
            "--user",
            "65534:65534",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "HOME=/tmp",
            "--workdir",
            "/tool",
        ]
        if stdin is not None:
            cmd.append("--interactive")
        for host, inside in mounts:
            cmd += ["--volume", f"{host}:{inside}:ro"]
        cmd += [box["image"], *command]
        shown = " ".join(
            cmd[2 : cmd.index("--volume")] if "--volume" in cmd else cmd[2:]
        )
        shown = (
            shown.replace(f"--name {name} ", "")
            + " "
            + " ".join(f"-v {inside}:ro" for _host, inside in mounts)
        )
        shown = f"docker run {shown} {box['image']} {' '.join(command)}"
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - argv built above from licence + fixed paths
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=box["timeout_seconds"],
                check=False,
            )
            # A result line used to be cut by keeping only the tail of stdout+stderr, which a
            # large result could push out entirely ("exited 0 without a result").
            output = proc.stdout[-MAX_STDOUT:] + "\n" + proc.stderr[-MAX_OUTPUT:]
            return proc.returncode, output, round(time.monotonic() - started, 2), shown
        except subprocess.TimeoutExpired:
            subprocess.run(  # noqa: S603
                [docker, "kill", name], capture_output=True, check=False
            )
            return (
                124,
                f"timed out after {box['timeout_seconds']}s",
                round(time.monotonic() - started, 2),
                shown,
            )


def _parse_tests(output: str, code: int, seconds: float, command: str) -> TestReport:
    payload = None
    for line in reversed(output.splitlines()):
        if line.startswith(TESTS_MARK):
            payload = json.loads(line[len(TESTS_MARK) :])
            break
    if payload is None:
        failed = [
            {
                "test": "(runner)",
                "status": "ERROR",
                "message": f"exit {code}: {_last_line(output)}",
            }
        ]
        return TestReport(
            passed=False,
            ran=0,
            failed=failed,
            output=output,
            seconds=seconds,
            command=command,
        )
    ok = [item["test"] for item in payload["results"] if item["status"] == "ok"]
    failed = [
        {
            "test": item["test"],
            "status": item["status"],
            "message": item["message"],
            "line": item.get("line"),
        }
        for item in payload["results"]
        if item["status"] not in ("ok", "skipped", "expected failure")
    ]
    ran = int(payload["ran"])
    passed = ran > 0 and not failed and len(ok) == ran
    return TestReport(
        passed=passed,
        ran=ran,
        ok=ok,
        failed=failed,
        output=output,
        seconds=seconds,
        command=command,
    )


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:400] if lines else ""


def _readable_by_sandbox(path: Path, files: bool = False) -> None:
    """The sandbox runs as uid 65534, so every directory Golem mounts needs read and search
    permission for others, and the tool bundle's files need read. tempfile makes 0700
    directories, which Docker Desktop ignores and Linux enforces ("No module named 'tool'").
    Only Golem's own copies are mounted: bundles, the snapshot, the export, the runners."""
    if not path.is_dir():
        return
    mode = path.stat().st_mode
    if mode & 0o005 != 0o005:
        path.chmod(mode | 0o055)
    if files:
        for item in path.iterdir():
            if item.is_file() and not item.stat().st_mode & 0o004:
                item.chmod(item.stat().st_mode | 0o044)
