"""Build a repository whose registry holds one installed tool, without a model, for the action's
CI job: `python -m tests.fixture_registry DIR` writes DIR/repo (with .golem/registry) and
DIR/build.log, the attachment the tool's tests read. The tool is test_loop's fixture, made and
installed through the real kernel and sandbox."""

import asyncio
import sys
from pathlib import Path
from unittest import mock

from golem import jev, kernel, licence, snapshot, tester
from golem.registry import Registry
from golem.sandbox import Sandbox

from tests.test_loop import BLIND_TESTS, TASK, args

ROOT = Path(__file__).resolve().parents[1]


def build(where: Path) -> None:
    repo = where / "repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("fixture\n")
    log = where / "build.log"
    log.write_text(
        "ok tests/test_c.py::test_z\nFAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n"
    )
    snap = where / "snapshot"
    files = snapshot.build(repo, snap, [log])
    lic = licence.load(ROOT / "authority.json")
    run = kernel.Run(
        repo=repo,
        task=TASK,
        licence=lic,
        registry=Registry(repo / ".golem"),
        sandbox=Sandbox(lic, snap),
        snapshot_dir=snap,
        files=files,
        api_key="unused",
        client=None,
        hooks=None,
        run_dir=repo / ".golem" / "runs" / "fixture",
    )
    with (
        mock.patch.object(jev, "ask", return_value=jev.Reply(error="offline fixture")),
        mock.patch.object(
            tester, "write_blind_tests", new=mock.AsyncMock(return_value=BLIND_TESTS)
        ),
    ):
        made = asyncio.run(kernel._make_tool(run, args()))
    if made.get("status") != "passed":
        raise SystemExit(f"fixture tool did not pass: {made}")
    kernel.install_tool_tool(run)["function"]["execute"](
        {"candidate_id": made["candidate_id"]}
    )
    print(f"installed {run.registry.active()} in {repo}")


if __name__ == "__main__":
    build(Path(sys.argv[1]))
