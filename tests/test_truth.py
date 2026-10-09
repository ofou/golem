"""Ground truth used by the definition-of-done runner."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "local_runner.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("local_runner", RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {RUNNER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["local_runner"] = module
    spec.loader.exec_module(module)
    return module


class TruthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def test_a_py_file_directly_under_the_root_is_not_a_subdirectory(self):
        repo = Path(tempfile.mkdtemp())
        for rel in (
            "tools/loose.py",
            "tools/sub/a.py",
            "tools/sub/nested/b.py",
            "tools/other/c.py",
        ):
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        result = self.runner._python_file_counts(repo, "tools")
        self.assertEqual(
            {row["dir"]: row["files"] for row in result["subdirs"]},
            {"sub": 2, "other": 1},
        )

    def test_a_relative_sibling_import_does_not_count_the_parent_package(self):
        repo = Path(tempfile.mkdtemp())
        bar = repo / "tools" / "pkg" / "bar.py"
        bar.parent.mkdir(parents=True)
        bar.write_text("from . import baz\n")
        known = {"tools.pkg", "tools.pkg.bar", "tools.pkg.baz"}
        targets = self.runner._import_targets(bar, "tools.pkg.bar", known)
        self.assertEqual(targets, {"tools.pkg.baz"})

    def test_an_absolute_from_import_still_counts_the_package(self):
        repo = Path(tempfile.mkdtemp())
        source = repo / "tools" / "other.py"
        source.parent.mkdir(parents=True)
        source.write_text("from tools.pkg import baz\n")
        known = {"tools.pkg", "tools.pkg.baz", "tools.other"}
        targets = self.runner._import_targets(source, "tools.other", known)
        self.assertEqual(targets, {"tools.pkg", "tools.pkg.baz"})
