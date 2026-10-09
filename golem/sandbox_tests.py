"""Runs inside the sandbox container. Discovers and runs unittest tests in /tool and
prints the usual verbose output followed by one machine-readable result line.

This file is mounted read-only at /golem/tests.py. It is kernel code, not generated code.
"""

import json
import sys
import traceback
import unittest

MARK = "<<<GOLEM_TESTS>>>"


def _message(err) -> str:
    exc = err[1]
    return f"{type(exc).__name__}: {exc}"[:400]


def _line(err):
    """The line in the test file where the test failed, so a disputed assertion can be cut out exactly."""
    frames = [
        frame
        for frame in traceback.extract_tb(err[2])
        if frame.filename.startswith("/tool/test_")
    ]
    return frames[-1].lineno if frames else None


class Recording(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def _add(self, test, status, message="", line=None):
        self.records.append(
            {
                "test": test.id().rsplit(".", 1)[-1],
                "id": test.id(),
                "status": status,
                "message": message,
                "line": line,
            }
        )

    def addSuccess(self, test):
        super().addSuccess(test)
        self._add(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._add(test, "FAIL", _message(err), _line(err))

    def addError(self, test, err):
        super().addError(test, err)
        self._add(test, "ERROR", _message(err), _line(err))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._add(test, "skipped", str(reason)[:200])

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._add(test, "expected failure", _message(err))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._add(test, "FAIL", "unexpected success")


def main() -> None:
    pattern = sys.argv[1] if len(sys.argv) > 1 else "test_*.py"
    sys.path.insert(0, "/tool")
    suite = unittest.TestLoader().discover(
        "/tool", pattern=pattern, top_level_dir="/tool"
    )
    result = unittest.TextTestRunner(
        stream=sys.stderr, verbosity=2, resultclass=Recording
    ).run(suite)
    sys.stderr.flush()
    if not isinstance(result, Recording):
        raise TypeError(f"expected Recording result, got {type(result).__name__}")
    print(MARK + json.dumps({"ran": result.testsRun, "results": result.records}))


if __name__ == "__main__":
    main()
