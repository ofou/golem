"""The kernel loop without a model: make_tool -> blind tests -> sandbox -> stub check ->
install_tool -> the installed tool runs in the sandbox through its proxy.

The model calls are replaced here and only here: the "builder" arguments and the
"blind tests" below are test fixtures, not capabilities. Nothing in this file is
installed anywhere but a temporary directory.
"""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from golem import cli, jev, kernel, licence, snapshot, tester
from golem.registry import Registry
from golem.sandbox import Sandbox

ROOT = Path(__file__).resolve().parents[1]
TASK = "Count how many lines in the attached build log are failures, and name the failing tests."

CODE = """import re

def run(args):
    text = open("/inputs/" + args["log_file"], encoding="utf-8").read()
    names = re.findall(r"^FAILED (\\S+)", text, re.M)
    return {"failures": len(names), "tests": names}
"""
OWN_TESTS = """import unittest
from tool import run

class T(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(run({"log_file": "build.log"})["failures"], 2)
    def test_names(self):
        self.assertEqual(run({"log_file": "build.log"})["tests"], ["tests/test_a.py::test_x", "tests/test_b.py::test_y"])
    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            run({"log_file": "nope.log"})
"""
BLIND_TESTS = """import unittest
from tool import run

class Blind(unittest.TestCase):
    def test_count(self):
        self.assertEqual(run({"log_file": "build.log"})["failures"], 2)
    def test_first(self):
        self.assertEqual(run({"log_file": "build.log"})["tests"][0], "tests/test_a.py::test_x")
    def test_type(self):
        self.assertIsInstance(run({"log_file": "build.log"})["tests"], list)
    def test_second(self):
        self.assertIn("tests/test_b.py::test_y", run({"log_file": "build.log"})["tests"])
"""


def args(**overrides):
    base = {
        "name": "count_failures",
        "description": "Count FAILED lines in a build log under /inputs and list the failing test ids.",
        "access": "pure",
        "input_schema": {
            "type": "object",
            "properties": {"log_file": {"type": "string"}},
            "required": ["log_file"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "failures": {"type": "integer"},
                "tests": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["failures", "tests"],
        },
        "code": CODE,
        "tests": OWN_TESTS,
        "gap": {
            "task_quote": "name the failing tests",
            "why_needed": "exact count and ids, not a guess",
        },
        "probe": [{"log_file": "build.log"}],
    }
    base.update(overrides)
    return base


@unittest.skipUnless(Sandbox.available(), "Docker is not running")
class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        repo = self.tmp / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("demo\n")
        log = self.tmp / "build.log"
        log.write_text(
            "ok tests/test_c.py::test_z\nFAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n"
        )
        clean = self.tmp / "clean.log"
        clean.write_text("ok tests/test_c.py::test_z\n")
        snap = self.tmp / "snap"
        files = snapshot.build(repo, snap, [log, clean])
        lic = licence.load(ROOT / "authority.json")
        registry = Registry(repo / ".golem")
        self.run_ = kernel.Run(
            repo=repo,
            task=TASK,
            licence=lic,
            registry=registry,
            sandbox=Sandbox(lic, snap),
            snapshot_dir=snap,
            files=files,
            api_key="unused",
            client=None,
            hooks=None,
            run_dir=repo / ".golem" / "runs" / "t",
        )
        self.patches = [
            mock.patch.object(jev, "ask", return_value=jev.Reply(error="offline test")),
            mock.patch(
                "golem.tester.write_blind_tests",
                new=mock.AsyncMock(return_value=BLIND_TESTS),
            ),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def make(self, **overrides):
        return asyncio.run(kernel._make_tool(self.run_, args(**overrides)))

    def test_verify_reproves_installed_tools_and_catches_a_changed_file(self):
        made = self.make()
        kernel.install_tool_tool(self.run_)["function"]["execute"](
            {"candidate_id": made["candidate_id"]}
        )
        argv = [
            "--repo",
            str(self.tmp / "repo"),
            "verify",
            "--attach",
            str(self.tmp / "build.log"),
        ]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(argv), 0, out.getvalue())
        self.assertIn("count_failures@0.1.0: OK", out.getvalue())
        self.assertIn("probes 1/1 ok, 1/1 return what they returned when tested", out.getvalue())
        tool_file = self.run_.registry.bundle("count_failures", "0.1.0") / "tool.py"
        tool_file.chmod(0o644)
        tool_file.write_text(tool_file.read_text() + "\n# edited after install\n")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(argv), 1)
        self.assertIn("CHANGED since they were tested", out.getvalue())

    def test_create_test_install_and_reuse(self):
        made = self.make()
        self.assertEqual(made["status"], "passed", made)
        install = kernel.install_tool_tool(self.run_)["function"]["execute"](
            {"candidate_id": made["candidate_id"]}
        )
        self.assertEqual(install["status"], "installed")
        self.assertEqual(self.run_.registry.active(), {"count_failures": "0.1.0"})
        proxies = kernel.installed_tools(self.run_)
        self.assertEqual([p["function"]["name"] for p in proxies], ["count_failures"])
        result = proxies[0]["function"]["execute"]({"log_file": "build.log"})
        self.assertEqual(
            result,
            {
                "failures": 2,
                "tests": ["tests/test_a.py::test_x", "tests/test_b.py::test_y"],
            },
        )
        usage = self.run_.registry.journal("usage")
        self.assertEqual(usage[-1]["tool"], "count_failures@0.1.0")
        receipt = self.run_.registry.receipt("count_failures", "0.1.0")
        self.assertEqual(
            (
                receipt["tests"]["ok"],
                receipt["blind_tests"]["ok"],
                receipt["stub_failed"],
            ),
            (3, 4, 1.0),
        )
        self.assertEqual(receipt["probes"][0]["args"], {"log_file": "build.log"})
        self.assertEqual(receipt["probes"][0]["result_sha256"], kernel.result_digest(result))
        calls = [
            json.loads(line)
            for line in (self.run_.run_dir / "calls.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            (calls[-1]["tool"], calls[-1]["args"], calls[-1]["result"]),
            ("count_failures@0.1.0", {"log_file": "build.log"}, result),
        )

    def test_the_builder_sees_its_probes_answer_on_the_real_inputs(self):
        made = self.make()
        self.assertEqual(made["status"], "passed", made)
        self.assertEqual(
            made["probes"],
            [
                {
                    "args": {"log_file": "build.log"},
                    "ok": True,
                    "result": json.dumps(
                        {"failures": 2, "tests": ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]}
                    ),
                }
            ],
        )

    def test_a_tool_that_passes_its_tests_but_fails_a_real_call_is_not_installable(self):
        made = self.make(probe=[{"log_file": "build.log"}, {"log_file": "ci.log"}])
        self.assertEqual(made["status"], "failed", made)
        self.assertTrue(
            any(reason.startswith("probe 2: FileNotFoundError") for reason in made["reasons"]),
            made["reasons"],
        )
        self.assertEqual(
            [item["ok"] for item in made["probes"]], [True, False]
        )
        install = kernel.install_tool_tool(self.run_)["function"]["execute"](
            {"candidate_id": made["candidate_id"]}
        )
        self.assertEqual(install["status"], "refused")

    def test_a_probe_result_that_breaks_the_output_schema_fails_the_candidate(self):
        made = self.make(
            code=CODE.replace('"tests": names}', '"tests": names or None}'),
            probe=[{"log_file": "build.log"}, {"log_file": "clean.log"}],
        )
        self.assertEqual(made["status"], "failed", made)
        self.assertEqual(made["reasons"], [
            "probe 2: output does not match the output schema ($.tests: expected array, got NoneType)"
        ])

    def test_a_call_that_arrives_without_its_arguments_says_so(self):
        made = asyncio.run(kernel._make_tool(self.run_, {}))
        self.assertEqual(made["status"], "refused")
        self.assertIn("arrived without name, description", made["reason"])
        self.assertIn("cut off", made["reason"])
        self.assertEqual((self.run_.made, sum(self.run_.attempts.values())), (0, 0))

    def test_a_missing_or_malformed_probe_is_refused_without_spending_an_attempt(self):
        missing = self.make(probe=None)
        self.assertEqual(missing["status"], "refused")
        self.assertIn("probe", missing["reason"])
        wrong = self.make(probe=[{"path": "build.log"}])
        self.assertEqual(wrong["status"], "refused")
        self.assertIn("probe 1: $: missing required 'log_file'", wrong["reason"])
        self.assertEqual((self.run_.made, sum(self.run_.attempts.values())), (0, 0))

    def test_privilege_request_is_refused_before_anything_runs(self):
        made = self.make(code="import urllib.request\n" + CODE)
        self.assertEqual(made["status"], "refused")
        self.assertIn("new authority: imports urllib.request", made["reasons"])
        self.assertEqual(self.run_.registry.active(), {})

    def test_vacuous_tests_fail_the_stub_check(self):
        vacuous = (
            "import unittest\nfrom tool import run\nclass T(unittest.TestCase):\n"
            + "".join(
                f"    def test_{i}(self):\n        self.assertTrue(callable(run))\n"
                for i in range(3)
            )
        )
        made = self.make(tests=vacuous)
        self.assertEqual(made["status"], "failed")
        self.assertTrue(any("vacuous" in reason for reason in made["reasons"]), made)

    def test_a_result_that_breaks_its_own_output_schema_fails_in_testing(self):
        strict = dict(args()["output_schema"], additionalProperties=False)
        made = self.make(output_schema=strict, code=CODE.replace('"tests": names}', '"tests": names, "extra": 1}'))
        self.assertEqual(made["status"], "failed", made)
        self.assertTrue(
            any("output_schema" in item["message"] for item in made["your_test_failures"]),
            made["your_test_failures"],
        )

    def test_arguments_the_input_schema_forbids_are_refused_in_testing(self):
        strict = dict(args()["input_schema"], additionalProperties=False)
        refuses = OWN_TESTS + (
            "    def test_unknown_argument(self):\n"
            "        with self.assertRaises(ValueError):\n"
            '            run({"log_file": "build.log", "extra": 1})\n'
        )
        made = self.make(input_schema=strict, tests=refuses)
        self.assertEqual(made["status"], "passed", made)

    def test_failing_implementation_is_not_installable(self):
        made = self.make(
            code=CODE.replace('"failures": len(names)', '"failures": len(names) + 1')
        )
        self.assertEqual(made["status"], "failed")
        install = kernel.install_tool_tool(self.run_)["function"]["execute"](
            {"candidate_id": made["candidate_id"]}
        )
        self.assertEqual(install["status"], "refused")
        self.assertEqual(self.run_.registry.active(), {})

    def test_a_wrong_blind_test_can_be_disputed_and_dropped_only_with_the_reviewers_agreement(
        self,
    ):
        wrong = BLIND_TESTS.replace(
            "    def test_second(self):",
            '    def test_wrong(self):\n        self.assertEqual(run({"log_file": "build.log"})["failures"], 99)\n\n    def test_second(self):',
        )
        with mock.patch(
            "golem.tester.write_blind_tests", new=mock.AsyncMock(return_value=wrong)
        ):
            first = self.make()
            self.assertEqual(first["status"], "failed")
            self.assertEqual(
                [item["test"] for item in first["blind_test_failures"]],
                ["test_blind_04"],
            )
            dispute = [
                {
                    "test": "test_blind_04",
                    "reason": "build.log has 2 FAILED lines, not 99",
                }
            ]
            with mock.patch(
                "golem.tester.review_dispute",
                new=mock.AsyncMock(return_value={"verdict": "keep", "why": "stays"}),
            ):
                kept = self.make(disputes=dispute)
            self.assertEqual(kept["status"], "failed")
            with mock.patch(
                "golem.tester.review_dispute",
                new=mock.AsyncMock(
                    return_value={"verdict": "drop", "why": "log has 2"}
                ),
            ):
                dropped = self.make(disputes=dispute)
        self.assertEqual(dropped["status"], "passed", dropped)
        receipt = self.run_.candidates[dropped["candidate_id"]].receipt
        self.assertEqual(
            [item["test"] for item in receipt["blind_tests_dropped"]], ["test_blind_04"]
        )
        shadow = receipt["blind_tests_dropped"][0]["jev_shadow"]
        as_run = tester.anonymize(wrong)[0]
        self.assertIn("99", tester.statement_at(as_run, shadow["assertion_line"]) or "")
        self.assertIn("offline test", shadow["error"])

    def test_blind_failures_reach_the_builder_without_their_expected_values(self):
        made = self.make(
            code=CODE.replace('"failures": len(names)', '"failures": len(names) + 1')
        )
        self.assertEqual(made["status"], "failed")
        self.assertIn(
            {
                "test": "test_blind_01",
                "error": "AssertionError",
                "calls": ["run({'log_file': 'build.log'})"],
            },
            made["blind_test_failures"],
        )
        self.assertNotIn("3 != 2", json.dumps(made["blind_test_failures"]))
        self.assertNotIn("test_count", json.dumps(made["blind_test_failures"]))

    def test_jev_sends_a_vague_gap_back_once_without_spending_an_attempt(self):
        vague = jev.Reply(
            ok=True,
            probs={"exact_op": 0.95, "clear::failures": 0.1, "clear::tests": 0.8},
            model="typesafe/jev-1.13-20260917",
        )
        with mock.patch.object(jev, "ask", return_value=vague):
            first = self.make()
            self.assertEqual(first["status"], "sent back")
            self.assertEqual(first["checks"], ["clear::failures=0.10"])
            self.assertEqual((self.run_.made, sum(self.run_.attempts.values())), (0, 0))
            second = self.make()
        self.assertEqual(second["status"], "passed", second)
        self.assertEqual(
            self.run_.candidates[second["candidate_id"]].receipt["jev_gate"]["outcome"],
            "build",
        )

    def test_shadow_checks_are_logged_but_change_nothing(self):
        duplicate = jev.Reply(
            ok=True,
            probs={
                "exact_op": 0.95,
                "same_job::count_failures": 0.97,
                "clear::failures": 0.8,
                "clear::tests": 0.8,
            },
            model="typesafe/jev-1.13-20260917",
        )
        with mock.patch.object(jev, "ask", return_value=duplicate):
            made = self.make()
        self.assertEqual(made["status"], "passed", made)
        gate = self.run_.candidates[made["candidate_id"]].receipt["jev_gate"]
        self.assertEqual(
            (gate["acting"], gate["shadow"], gate["outcome"]),
            ([], ["same_job::count_failures=0.97"], "build"),
        )

    def test_schemas_sent_as_json_strings_are_decoded(self):
        base = args()
        made = self.make(
            input_schema=json.dumps(base["input_schema"]),
            output_schema=json.dumps(base["output_schema"]),
            gap=json.dumps(base["gap"]),
        )
        self.assertEqual(made["status"], "passed", made)

    def test_renaming_a_tool_does_not_reset_the_attempt_cap(self):
        broken = CODE.replace('"failures": len(names)', '"failures": len(names) + 1')
        for _ in range(3):
            self.assertEqual(self.make(code=broken)["status"], "failed")
        renamed = self.make(name="count_failures_again", code=broken)
        self.assertEqual(renamed["status"], "refused")
        self.assertIn("renaming", renamed["reason"])

    def test_gap_that_does_not_quote_the_task_is_refused(self):
        made = self.make(gap={"task_quote": "build a log parser", "why_needed": "x"})
        self.assertEqual(made["status"], "refused")
        self.assertIn("gap", made["reason"])


if __name__ == "__main__":
    unittest.main()


class BlindBriefTest(unittest.TestCase):
    def test_a_registry_read_tool_brief_carries_the_export_format(self):
        sent = {}

        class Result:
            async def get_text(self):
                return "```python\nimport unittest\nfrom tool import run\n```"

        def fake_call(_client, request):
            sent.update(request)
            return Result()

        manifest = dict(args(access="registry-read"), name="registry_report")
        with mock.patch.object(tester, "call_model", new=fake_call):
            asyncio.run(
                tester.write_blind_tests(None, "m", manifest, TASK, [], 0.01, None)
            )
        self.assertIn("is inside its manifest", sent["input"])
        sent.clear()
        with mock.patch.object(tester, "call_model", new=fake_call):
            asyncio.run(tester.write_blind_tests(None, "m", args(), TASK, [], 0.01, None))
        self.assertNotIn("registry_export_format", sent["input"])
