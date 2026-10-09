"""Kernel tests that need no model and no network. Sandbox tests need Docker and skip without it."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from golem import licence, lint, schema, snapshot
from golem.kernel import _quotes_task
from golem.registry import Registry, RegistryError
from golem.sandbox import Sandbox, _parse_tests, _readable_by_sandbox
from golem.sandbox_tests import MARK

ROOT = Path(__file__).resolve().parents[1]
LICENCE = licence.load(ROOT / "authority.json")


class LicenceTest(unittest.TestCase):
    def test_hash_is_stable_and_file_untouched(self):
        self.assertEqual(LICENCE.sha256, licence.load(ROOT / "authority.json").sha256)
        self.assertTrue(licence.unchanged(LICENCE))

    def test_access_outside_licence_is_new_authority(self):
        found = licence.violations(
            {"shape": "function", "access": "network-read"}, LICENCE
        )
        self.assertTrue(any("new authority" in item for item in found))
        self.assertEqual(
            licence.violations({"shape": "function", "access": "pure"}, LICENCE), []
        )


class LintTest(unittest.TestCase):
    def scan(self, source):
        return lint.scan(
            source, LICENCE.data["forbidden_imports"], LICENCE.data["forbidden_calls"]
        )

    def test_network_subprocess_eval_env_and_writes_are_named(self):
        found = " | ".join(
            self.scan(
                "import socket\nfrom urllib import request\nimport subprocess\nimport os\n"
                "eval('1')\nos.environ['X']\nopen('f', 'w')\n"
            )
        )
        for word in ("socket", "urllib", "subprocess", "eval", "environ", "writing"):
            self.assertIn(word, found)

    def test_plain_reader_is_clean(self):
        self.assertEqual(
            self.scan(
                "import os, re, json\ndef run(a):\n    return {'n': len(open('/repo/x').read())}\n"
            ),
            [],
        )

    def test_tests_may_write_fixtures_but_not_reach_the_network(self):
        source = "import tempfile, os, socket\nfrom pathlib import Path\np = Path(tempfile.mkdtemp()) / 'x.log'\np.write_text('FAILED a')\nopen(p, 'w')\nos.remove(p)\n"
        found = lint.scan(
            source,
            LICENCE.data["forbidden_imports"],
            LICENCE.data["forbidden_calls"],
            for_tests=True,
        )
        self.assertEqual(found, ["new authority: imports socket"])
        self.assertTrue(any("writing" in item for item in self.scan(source)))

    def test_ordinary_string_and_regex_methods_are_not_privileges(self):
        source = "import re\nP = re.compile(r'x')\ndef run(a):\n    return {'s': a['s'].replace('a', 'b'), 'm': bool(P.match('x'))}\n"
        self.assertEqual(self.scan(source), [])

    def test_qualified_and_aliased_dangerous_calls_are_named(self):
        found = " | ".join(
            self.scan(
                "import os\nfrom os import system as sh\nos.replace('a', 'b')\nsh('ls')\n"
            )
        )
        self.assertIn("os.replace", found)
        self.assertIn("os.system", found)


class SchemaTest(unittest.TestCase):
    def test_validate(self):
        s = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
            "additionalProperties": False,
        }
        self.assertEqual(schema.validate({"n": 1}, s), [])
        self.assertTrue(schema.validate({"n": "1"}, s))
        self.assertTrue(schema.validate({}, s))
        self.assertTrue(schema.validate({"n": 1, "x": 2}, s))

    def test_maps_and_annotations(self):
        s = {
            "type": "object",
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
            "default": {},
        }
        self.assertEqual(schema.check_schema(s), [])
        self.assertEqual(schema.validate({"tests/a.py": ["aiohttp.web"]}, s), [])
        self.assertTrue(schema.validate({"tests/a.py": [1]}, s))

    def test_unsupported_keywords_are_rejected(self):
        self.assertTrue(schema.check_schema({"type": "object", "$ref": "#/x"}))
        self.assertEqual(
            schema.check_schema(
                {"type": "object", "properties": {"a": {"type": "string"}}}
            ),
            [],
        )

    def test_properties_and_required_must_be_objects_and_lists(self):
        self.assertTrue(schema.check_schema({"type": "object", "properties": ["path"]}))
        self.assertTrue(schema.check_schema({"type": "object", "properties": "path"}))
        declared = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": "path",
        }
        self.assertTrue(schema.check_schema(declared))
        self.assertEqual(schema.validate({"path": "x"}, declared), [])

    def test_outline_is_a_one_line_signature_of_what_a_tool_returns(self):
        returns = {
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "job": {"type": "string"},
                            "most": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["job"],
                    },
                },
                "by_file": {"type": "object", "additionalProperties": {"type": "integer"}},
                "note": {"type": ["string", "null"]},
            },
            "required": ["jobs", "by_file"],
        }
        self.assertEqual(
            schema.outline(returns),
            "{jobs: [{job: string, most?: [string]}], by_file: {*: integer}, note?: string | null}",
        )
        self.assertEqual(schema.outline({"type": "array"}), "array")
        self.assertEqual(schema.outline(None), "any")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.reg = Registry(self.tmp / ".golem")
        self.cand = self.tmp / "cand"
        self.cand.mkdir()
        (self.cand / "tool.py").write_text("def run(a):\n    return {}\n")

    def manifest(self, version):
        return {
            "name": "parse_thing",
            "version": version,
            "access": "pure",
            "description": "d",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "gap": {},
        }

    def test_install_versions_rollback_and_immutability(self):
        receipt = {
            "passed": True,
            "tests": {"ran": 3, "ok": 3},
            "blind_tests": {"ran": 4, "ok": 4},
            "stub_failed": 1.0,
        }
        self.assertEqual(self.reg.next_version("parse_thing"), "0.1.0")
        self.reg.install(self.cand, self.manifest("0.1.0"), receipt)
        self.assertEqual(self.reg.next_version("parse_thing"), "0.2.0")
        self.reg.install(self.cand, self.manifest("0.2.0"), receipt)
        self.assertEqual(self.reg.active(), {"parse_thing": "0.2.0"})
        self.reg.rollback("parse_thing", "0.1.0")
        self.assertEqual(self.reg.active(), {"parse_thing": "0.1.0"})
        with self.assertRaises(RegistryError):
            self.reg.install(self.cand, self.manifest("0.1.0"), receipt)
        export = json.loads(
            (self.reg.export(self.tmp / "exp") / "tools.json").read_text()
        )
        self.assertEqual(len(export), 2)
        self.assertNotIn("code", json.dumps(export))


class SnapshotTest(unittest.TestCase):
    def test_secrets_and_state_are_left_out(self):
        repo = Path(tempfile.mkdtemp())
        for rel in (
            "app.py",
            ".env",
            ".env.example",
            "keys/server.pem",
            ".golem/registry/active.json",
            "src/m.py",
        ):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text("x")
        attach = repo.parent / "ci.log"
        attach.write_text("log")
        files = snapshot.build(repo, Path(tempfile.mkdtemp()) / "snap", [attach])
        self.assertIn("app.py", files)
        self.assertIn("src/m.py", files)
        self.assertIn("_inputs/ci.log", files)
        self.assertIn(".env.example", files)
        self.assertFalse(
            any(
                name in files
                for name in (".env", "keys/server.pem", ".golem/registry/active.json")
            )
        )

    def test_resolve_refuses_escape(self):
        root = Path(tempfile.mkdtemp())
        with self.assertRaises(ValueError):
            snapshot.resolve(root, "../../etc/passwd")

    def test_secret_names_are_matched_regardless_of_case(self):
        repo = Path(tempfile.mkdtemp())
        for rel in (".ENV", ".ENV.example", "keys/Server.PEM", "id_RSA", "app.py"):
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        files = snapshot.build(repo, Path(tempfile.mkdtemp()))
        self.assertIn("app.py", files)
        self.assertIn(".ENV.example", files)
        self.assertFalse(
            any(name in files for name in (".ENV", "keys/Server.PEM", "id_RSA"))
        )
        dotted = Path(tempfile.mkdtemp())
        (dotted / ".env").mkdir()
        (dotted / ".env" / "secret.txt").write_text("x")
        (dotted / "app.py").write_text("x")
        nested = snapshot.build(dotted, Path(tempfile.mkdtemp()))
        self.assertEqual(nested, ["app.py"])

    def test_attachments_with_the_same_name_are_both_kept(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "app.py").write_text("x")
        first = Path(tempfile.mkdtemp()) / "job.log"
        second = Path(tempfile.mkdtemp()) / "job.log"
        first.write_text("AAA")
        second.write_text("BBB")
        dest = Path(tempfile.mkdtemp())
        files = snapshot.build(repo, dest, [first, second])
        inputs = [name for name in files if name.startswith("_inputs/")]
        self.assertEqual(len(inputs), 2)
        bodies = {(dest / name).read_text() for name in inputs}
        self.assertEqual(bodies, {"AAA", "BBB"})

    def test_git_lists_non_ascii_names_without_quoting_them_away(self):
        repo = Path(tempfile.mkdtemp())
        git = shutil.which("git")
        if git is None:
            self.fail("git is not on PATH")
        subprocess.run([git, "init", "-q", str(repo)], check=True)  # noqa: S603
        (repo / "café.py").write_text("print(1)\n")
        (repo / "plain.py").write_text("print(2)\n")
        subprocess.run([git, "-C", str(repo), "add", "-A"], check=True)  # noqa: S603
        files = snapshot.build(repo, Path(tempfile.mkdtemp()))
        self.assertIn("café.py", files)
        self.assertIn("plain.py", files)


class ReportTest(unittest.TestCase):
    def test_a_skip_is_reported_instead_of_failing_the_suite_silently(self):
        payload = {
            "ran": 2,
            "results": [
                {"test": "test_ok", "status": "ok", "message": ""},
                {"test": "test_skip", "status": "skipped", "message": "platform"},
            ],
        }
        report = _parse_tests(MARK + json.dumps(payload), 0, 0.1, "probe")
        self.assertFalse(report.passed)
        self.assertEqual(report.ok, ["test_ok"])
        self.assertEqual(
            [(item["test"], item["status"]) for item in report.failed],
            [("test_skip", "skipped")],
        )


class MountTest(unittest.TestCase):
    def test_nested_files_and_file_mounts_become_readable(self):
        root = Path(tempfile.mkdtemp())
        nested = root / "src" / "pkg"
        nested.mkdir(parents=True)
        target = nested / "mod.py"
        target.write_text("x")
        lone = root / "runner.py"
        lone.write_text("x")
        for path in (root, root / "src", nested):
            os.chmod(path, 0o700)
        os.chmod(target, 0o600)
        os.chmod(lone, 0o600)
        _readable_by_sandbox(root)
        _readable_by_sandbox(lone)
        self.assertTrue(nested.stat().st_mode & 0o005 == 0o005)
        self.assertTrue(target.stat().st_mode & 0o004)
        self.assertTrue(lone.stat().st_mode & 0o004)


class GapTest(unittest.TestCase):
    def test_gap_must_quote_the_task(self):
        task = "Why did CI fail on run 42,   and who owns the failing tests?"
        self.assertTrue(_quotes_task("who owns the failing tests", task))
        self.assertFalse(_quotes_task("build a parser", task))
        self.assertFalse(_quotes_task("CI", task))


@unittest.skipUnless(Sandbox.available(), "Docker is not running")
class SandboxTest(unittest.TestCase):
    def test_no_network_no_env_read_only_non_root(self):
        os.environ["GOLEM_PROBE_SECRET"] = "must-not-leak"  # noqa: S105
        snap = Path(tempfile.mkdtemp())
        bundle = Path(tempfile.mkdtemp())
        (bundle / "tool.py").write_text(
            "import os, socket\n"
            "def run(args):\n"
            "    out = {'secret': os.environ.get('GOLEM_PROBE_SECRET'), 'uid': os.getuid()}\n"
            "    try:\n        socket.create_connection(('1.1.1.1', 80), timeout=2); out['net'] = 'open'\n"
            "    except OSError:\n        out['net'] = 'blocked'\n"
            "    try:\n        open('/tool/x', 'w'); out['write'] = 'open'\n"
            "    except OSError:\n        out['write'] = 'blocked'\n"
            "    return out\n"
        )
        result = Sandbox(LICENCE, snap).invoke(bundle, "pure", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["result"],
            {"secret": None, "uid": 65534, "net": "blocked", "write": "blocked"},
        )

    def test_stub_makes_real_tests_fail(self):
        bundle = Path(tempfile.mkdtemp())
        (bundle / "tool.py").write_text(
            "def run(args):\n    return {'n': args['a'] + 1}\n"
        )
        (bundle / "test_tool.py").write_text(
            "import unittest\nfrom tool import run\n"
            "class T(unittest.TestCase):\n"
            "    def test_adds(self):\n        self.assertEqual(run({'a': 1})['n'], 2)\n"
            "    def test_more(self):\n        self.assertEqual(run({'a': 5})['n'], 6)\n"
        )
        box = Sandbox(LICENCE, Path(tempfile.mkdtemp()))
        self.assertTrue(box.run_tests(bundle, "pure").passed)
        stub = box.run_tests_against_stub(bundle, "pure")
        self.assertEqual((stub.ran, len(stub.failed)), (2, 2))


if __name__ == "__main__":
    unittest.main()
