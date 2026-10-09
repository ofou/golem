"""The blind test writer: a different model, from a different vendor, that sees the
tool's interface, the task, and the repository, but never the implementation.

Its suite is pinned per tool name and interface, so revising the code cannot make
the blind tests go away.
"""

from __future__ import annotations

import ast
import json
import re

from openrouter_agent import call_model, max_cost, step_count_is

from golem.registry import EXPORT_FORMAT

INSTRUCTIONS = """You write acceptance tests for a Python tool you cannot see.

You get the tool's name, description, input and output JSON schemas, its access level, and the task that caused it.
You may read the repository and attachments with list_files and read_file to find concrete expected values.

Write ONE Python file, test_blind.py, using only the standard library and unittest:
- `from tool import run`; `run(args: dict) -> dict`.
- At least 4 tests. Each asserts concrete expected values taken from real files or from inputs you construct, not just types.
- Cover the main case, an edge case, and invalid or missing input as the schemas describe it.
- Inside the test sandbox, repository file `X` is at `/repo/X` (repository-read tools only), attachment `_inputs/Y` is at `/inputs/Y`, the registry export is at `/registry/` (registry-read tools only). No network, no writes outside /tmp.
- For a registry-read tool, the brief carries registry_export_format. Fixtures you build must follow it exactly.
- Never test private helpers. Never import anything except unittest, tool, and other standard-library modules.

Reply with only the file, in one ```python code block."""

_BLOCK = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
_JSON = re.compile(r"\{.*\}", re.DOTALL)

REVIEW = """You wrote an acceptance test for a tool you cannot see. The implementer disputes one test.
Re-check the test against the real files (use list_files and read_file) and the tool's description and schemas.
Keep the test unless its expectation is false about the real inputs or asks for something the description and schemas do not promise.
Reply with only JSON: {"verdict": "keep" or "drop", "why": "<one sentence citing what you checked>"}"""


async def review_dispute(
    client,
    model: str,
    manifest: dict,
    test_name: str,
    test_source: str,
    reason: str,
    read_tools: list,
    budget_usd: float,
    hooks,
    stop=None,
    plugins=None,
    reasoning=None,
) -> dict:
    brief = {
        "tool": {
            key: manifest[key]
            for key in (
                "name",
                "description",
                "access",
                "input_schema",
                "output_schema",
            )
        },
        "disputed_test": test_name,
        "test_source": test_source[:4000],
        "implementer_says": reason[:1000],
    }
    request = {
        "model": model,
        "instructions": REVIEW,
        "input": json.dumps(brief),
        "tools": read_tools,
        "stop_when": [step_count_is(8), max_cost(budget_usd), *(stop or [])],
        "hooks": hooks,
    }
    if plugins:
        request["plugins"] = plugins
    if reasoning:
        request["reasoning"] = reasoning
    text = await call_model(client, request).get_text()
    match = _JSON.search(text or "")
    try:
        verdict = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        verdict = {}
    if verdict.get("verdict") not in ("keep", "drop"):
        return {
            "verdict": "keep",
            "why": "the review did not return a clear verdict, so the test stays",
        }
    return {"verdict": verdict["verdict"], "why": str(verdict.get("why", ""))[:400]}


COMPOUND = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.With,
    ast.For,
    ast.If,
    ast.Try,
    ast.While,
)


def statement_at(suite: str, line: int) -> str | None:
    """The innermost simple statement covering `line`, e.g. the assertion that failed."""
    found: ast.stmt | None = None
    for node in ast.walk(ast.parse(suite)):
        if not isinstance(node, ast.stmt):
            continue
        covers = not isinstance(node, COMPOUND) and node.lineno <= line <= (
            node.end_lineno or node.lineno
        )
        if covers and (found is None or node.lineno >= found.lineno):
            found = node
    return ast.get_source_segment(suite, found) if found else None


def test_source(suite: str, test_name: str) -> str | None:
    tree = ast.parse(suite)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == test_name
        ):
            return ast.get_source_segment(suite, node)
    return None


def drop_test(suite: str, test_name: str) -> str:
    tree = ast.parse(suite)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            node.body = [
                item
                for item in node.body
                if not (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == test_name
                )
            ] or [ast.Pass()]
    return ast.unparse(tree) + "\n"


def count_tests(suite: str) -> int:
    return sum(
        1
        for node in ast.walk(ast.parse(suite))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    )


async def write_blind_tests(
    client,
    model: str,
    manifest: dict,
    task: str,
    read_tools: list,
    budget_usd: float,
    hooks,
    stop=None,
    plugins=None,
    reasoning=None,
) -> str:
    brief = {
        "name": manifest["name"],
        "description": manifest["description"],
        "access": manifest["access"],
        "input_schema": manifest["input_schema"],
        "output_schema": manifest["output_schema"],
        "task": task[:4000],
    }
    if manifest["access"] == "registry-read":
        brief["registry_export_format"] = EXPORT_FORMAT
    request = {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": f"Write test_blind.py for this tool:\n{brief}",
        "tools": read_tools,
        "stop_when": [step_count_is(10), max_cost(budget_usd), *(stop or [])],
        "hooks": hooks,
    }
    if plugins:
        request["plugins"] = plugins
    if reasoning:
        request["reasoning"] = reasoning
    result = call_model(client, request)
    text = await result.get_text()
    match = _BLOCK.search(text or "")
    code = (match.group(1) if match else text or "").strip()
    if "import unittest" not in code or "from tool import run" not in code:
        raise ValueError(
            "the blind test writer did not return a unittest file that imports run from tool"
        )
    return code + "\n"


def calls_in(suite: str, test_name: str, limit: int = 3) -> list[str]:
    """The run(...) calls one test makes, as source: the inputs a failing blind test used,
    without the values it expects. Told only "test_blind_04: AssertionError", a builder
    cannot tell that the test called run({"only": []}), and spent three attempts guessing."""
    try:
        tree = ast.parse(suite)
    except SyntaxError:
        return []
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == test_name:
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "run"
                ):
                    text = ast.unparse(sub)[:300]
                    if text not in calls:
                        calls.append(text)
    return calls[:limit]


def anonymize(suite: str) -> tuple[str, dict]:
    """Rename every test method to test_blind_NN and drop docstrings and comments. The builder
    only ever sees failing test names, and a name like test_most_imported_is_tools_registry
    hands it the expected value. Returns the renamed suite and {new name: original name}."""
    tree = ast.parse(suite)
    names = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(
                item, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and item.name.startswith("test"):
                new = f"test_blind_{len(names) + 1:02d}"
                names[new], item.name = item.name, new
                first = item.body[0] if item.body else None
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    item.body = item.body[1:] or [ast.Pass()]
    return ast.unparse(tree) + "\n", names
