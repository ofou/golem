"""The command line: which licence a run gets, and verify on a registry larger than one task's
sandbox budget. The GitHub Action relies on both."""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from golem import cli, jev, kernel, licence, snapshot
from golem.registry import Registry
from golem.sandbox import Sandbox

from tests.test_loop import BLIND_TESTS, TASK, args

ROOT = Path(__file__).resolve().parents[1]


class LicenceChoiceTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())

    def test_a_named_licence_that_does_not_exist_is_an_error(self):
        missing = self.repo / "nope.json"
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(
                cli.main(
                    ["--repo", str(self.repo), "--licence", str(missing), "licence"]
                ),
                2,
            )
        self.assertIn("licence not found", err.getvalue())

    def test_a_named_licence_wins_over_the_repository_licence(self):
        (self.repo / ".golem").mkdir()
        loose = json.loads((ROOT / "authority.json").read_text())
        loose["budget"]["max_usd_per_task"] = 500
        (self.repo / ".golem" / "authority.json").write_text(json.dumps(loose))
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.main(
                [
                    "--repo",
                    str(self.repo),
                    "--licence",
                    str(ROOT / "authority.json"),
                    "licence",
                ]
            )
        self.assertEqual(
            out.getvalue().splitlines()[-1].split()[1],
            licence.load(ROOT / "authority.json").sha256,
        )
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.main(["--repo", str(self.repo), "licence"])
        self.assertIn('"max_usd_per_task": 500', out.getvalue())


@unittest.skipUnless(Sandbox.available(), "Docker is not running")
class VerifyBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("demo\n")
        self.log = self.tmp / "build.log"
        self.log.write_text(
            "FAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n"
        )
        snap = self.tmp / "snap"
        files = snapshot.build(self.repo, snap, [self.log])
        lic = licence.load(ROOT / "authority.json")
        self.run_ = kernel.Run(
            repo=self.repo,
            task=TASK,
            licence=lic,
            registry=Registry(self.repo / ".golem"),
            sandbox=Sandbox(lic, snap),
            snapshot_dir=snap,
            files=files,
            api_key="unused",
            client=None,
            hooks=None,
            run_dir=self.repo / ".golem" / "runs" / "t",
        )

    def test_verify_checks_more_tools_than_one_task_may_run_in_the_sandbox(self):
        with (
            mock.patch.object(jev, "ask", return_value=jev.Reply(error="offline test")),
            mock.patch(
                "golem.tester.write_blind_tests",
                new=mock.AsyncMock(return_value=BLIND_TESTS),
            ),
        ):
            for name, quote in (
                ("count_failures", "name the failing tests"),
                ("list_failures", "Count how many lines"),
            ):
                made = asyncio.run(
                    kernel._make_tool(
                        self.run_,
                        args(
                            name=name, gap={"task_quote": quote, "why_needed": "exact"}
                        ),
                    )
                )
                self.assertEqual(made["status"], "passed", made)
                kernel.install_tool_tool(self.run_)["function"]["execute"](
                    {"candidate_id": made["candidate_id"]}
                )
        tight = json.loads((ROOT / "authority.json").read_text())
        tight["budget"]["max_sandbox_calls_per_task"] = 4
        tight_path = self.tmp / "tight.json"
        tight_path.write_text(json.dumps(tight))
        argv = [
            "--repo",
            str(self.repo),
            "--licence",
            str(tight_path),
            "verify",
            "--attach",
            str(self.log),
        ]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(argv), 0, out.getvalue())
        self.assertIn("count_failures@0.1.0: OK", out.getvalue())
        self.assertIn("list_failures@0.1.0: OK", out.getvalue())


if __name__ == "__main__":
    unittest.main()
