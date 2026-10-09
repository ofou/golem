"""The definition-of-done check on a recorded registry, without a model or Docker."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

CHECK = Path(__file__).resolve().parents[1] / "scripts" / "dod_check.py"


def load_check():
    spec = importlib.util.spec_from_file_location("dod_check", CHECK)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {CHECK}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dod_check"] = module
    spec.loader.exec_module(module)
    return module


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


class DodCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dod = load_check()

    def test_a_value_one_tool_returned_and_the_next_took_is_a_chain(self):
        run = {
            "task": "Which test file skipped most, and what does it import?",
            "calls": [
                {"tool": "skips@0.1.0", "ok": True, "args": {"log": "ios.log"},
                 "result": {"by_file": {"tests/test_web.py": 9}, "most": ["tests/test_web.py"]}},
                {"tool": "imports@0.1.0", "ok": True, "args": {"files": ["tests/test_web.py"]},
                 "result": {"tests/test_web.py": ["aiohttp/web.py"]}},
                {"tool": "imports@0.1.0", "ok": True, "args": {"files": ["tests/test_other.py"]},
                 "result": {}},
            ],
        }
        self.assertEqual(
            self.dod.chains(run),
            {("skips@0.1.0", "imports@0.1.0"): {"tests/test_web.py"}},
        )

    def test_values_the_task_already_names_are_not_a_chain(self):
        run = {
            "task": "What does tests/test_web.py import?",
            "calls": [
                {"tool": "a@0.1.0", "ok": True, "args": {}, "result": {"files": ["tests/test_web.py"]}},
                {"tool": "b@0.1.0", "ok": True, "args": {"file": "tests/test_web.py"}, "result": {}},
            ],
        }
        self.assertEqual(self.dod.chains(run), {})

    def test_five_checks_pass_on_a_registry_that_meets_the_definition(self):
        repo = Path(tempfile.mkdtemp())
        golem = repo / ".golem"
        tools = {
            "skips": ("20261009-010000-aaaaaa", "repository-read", "how many tests were skipped in each job"),
            "imports": ("20261009-020000-bbbbbb", "repository-read", "which aiohttp/ source modules does each"),
            "chains": ("20261009-030000-cccccc", "registry-read", "which installed tools can feed another"),
        }
        tasks = {
            "20261009-010000-aaaaaa": "From the logs, how many tests were skipped in each job, per file?",
            "20261009-020000-bbbbbb": "Which aiohttp/ source modules does each test file import?",
            "20261009-030000-cccccc": "Tell me which installed tools can feed another installed tool.",
            "20261009-040000-dddddd": "Which test file skipped most in each job, and what does it import?",
        }
        for name, (run_id, access, quote) in tools.items():
            bundle = golem / "registry" / name / "0.1.0"
            bundle.mkdir(parents=True)
            (bundle / "manifest.json").write_text(json.dumps({
                "name": name, "access": access, "description": f"{name} tool",
                "gap": {"task_quote": quote}, "created_by": {"run": run_id},
            }))
            (bundle / "receipt.json").write_text(json.dumps({
                "passed": True, "tests": {"ran": 3, "ok": 3}, "blind_tests": {"ran": 4, "ok": 4},
                "stub_failed": 1.0,
            }))
        (golem / "registry" / "active.json").write_text(
            json.dumps({"tools": dict.fromkeys(tools, "0.1.0")})
        )
        for run_id, task in tasks.items():
            built = [f"{name}@0.1.0" for name, (rid, _a, _q) in tools.items() if rid == run_id]
            events = [
                {"kind": "licence", "text": "shem sha256 abc"},
                {"kind": "task", "text": task},
                *({"kind": "create", "text": f"{tool} attempt 1/3"} for tool in built),
                *({"kind": "verdict", "text": f"{tool}: PASS"} for tool in built),
                *({"kind": "install", "text": f"{tool} installed"} for tool in built),
                {"kind": "licence", "text": "sha256 abc unchanged"},
            ]
            write_jsonl(golem / "runs" / run_id / "events.jsonl", events)
            (golem / "runs" / run_id / "result.md").write_text("an answer\n")
        combine = golem / "runs" / "20261009-040000-dddddd"
        write_jsonl(combine / "calls.jsonl", [
            {"tool": "skips@0.1.0", "ok": True, "args": {"log": "ios.log"}, "result": {"most": ["tests/test_web.py"]}},
            {"tool": "imports@0.1.0", "ok": True, "args": {"files": ["tests/test_web.py"]}, "result": {}},
        ])
        write_jsonl(golem / "usage.jsonl", [
            {"run": "20261009-040000-dddddd", "tool": "skips@0.1.0", "ok": True},
            {"run": "20261009-040000-dddddd", "tool": "imports@0.1.0", "ok": True},
        ])
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.dod.check(repo)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("5/5 checks pass", out.getvalue())
        self.assertIn("chained: yes", out.getvalue())
        self.assertIn("skips@0.1.0 -> imports@0.1.0", out.getvalue())


if __name__ == "__main__":
    unittest.main()
