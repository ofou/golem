"""The loop: one task, up to N fresh sessions. Each install ends a session; the next
session starts from nothing but the task, a handoff note, and the registry.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from openrouter_agent import (
    HooksManager,
    OpenRouter,
    call_model,
    max_cost,
    step_count_is,
)
from openrouter_agent.hooks_types import HookEntry

from golem import kernel, schema, snapshot
from golem.licence import Licence, unchanged
from golem.registry import EXPORT_FORMAT, Registry
from golem.sandbox import Sandbox

SDK_TURN_LIMIT = 20
FALLBACK_USD_PER_INPUT_TOKEN = 5 / 1_000_000
FALLBACK_USD_PER_OUTPUT_TOKEN = 25 / 1_000_000
FALLBACK_MIN_USD_PER_CALL = 0.05

INSTRUCTIONS = """You are Golem, an agent working inside one software repository. You answer the task with evidence.

How you work:
- Read files with list_files and read_file. Attachments are under _inputs/.
- Use an installed Golem tool whenever one fits. Installed tools are listed below and are callable directly.
- Installed tools compose. Each lists what it returns; pass the values one tool returns as another tool's arguments.
  Before make_tool, check whether installed tools, alone or chained, already answer the task. If an installed tool
  returns an error, read it and retry with corrected arguments before you consider revising it.
- When the task needs an exact, repeatable operation you would otherwise do by hand or guess (parsing, mapping, resolving,
  counting, cross-referencing), and no installed tool does it, build it with make_tool. Copy the words of the task the tool
  serves into gap.task_quote. Do not build tools the task does not need.
- A tool is standard-library Python: tool.py defines run(args: dict) -> dict and returns JSON-serializable data.
  It runs in a sandbox with no network, no environment, no subprocess, and a read-only filesystem.
  Repository file X is at /repo/X (access "repository-read" only). Attachment _inputs/Y is at /inputs/Y (all tools).
  """ + EXPORT_FORMAT + """
  Do not build tools to find out what a file contains; read it, or rely on the formats above.
  Use the least access that works. Prefer arguments over hardcoded paths.
  Make each tool do one thing (read one input format, or resolve one relation) so later tasks can reuse and chain it.
- tests: test_tool.py with unittest, `from tool import run`, at least 3 tests asserting concrete values. Tests must fail
  against a stub that raises. Arguments that break input_schema never reach run(): the call raises ValueError, as a
  real call is refused. Test invalid input only that way, and declare in input_schema everything run() accepts.
  Another model writes blind tests for the same interface; you will see their failures.
  If a blind test asserts something false about the real inputs, call make_tool again with disputes=[{test, reason}]
  citing the evidence. The test writer re-checks; only tests it agrees are wrong are dropped, and drops are recorded.
- probe: 1-3 argument objects for the real calls this task makes with the tool. They run on the real repository and
  attachments exactly as installed calls will, and an error or a result that breaks output_schema fails the candidate.
  You see each result. Install only when they answer the task; otherwise fix the code and call make_tool again.
- When make_tool passes, call install_tool. The session then ends and a fresh session continues with the tool loaded,
  so end that turn with a short handoff: what is done, what is left.
- Final answer: the result for the task, short and concrete, then one line listing the Golem tools you used and created.
  Never present a number or fact you did not read or compute with a tool."""


def build_run(
    repo: Path, task: str, licence: Licence, api_key: str, attachments: list[Path]
) -> kernel.Run:
    repo = Path(repo).resolve()
    golem_dir = repo / ".golem"
    registry = Registry(golem_dir)
    work = Path(tempfile.mkdtemp(prefix="golem-run-"))
    snap_dir = work / "snapshot"
    files = snapshot.build(repo, snap_dir, attachments)
    export_dir = registry.export(work / "registry")
    run = kernel.Run(
        repo=repo,
        task=task,
        licence=licence,
        registry=registry,
        sandbox=Sandbox(licence, snap_dir, export_dir),
        snapshot_dir=snap_dir,
        files=files,
        api_key=api_key,
        client=OpenRouter(api_key=api_key),
        hooks=None,
        run_dir=golem_dir / "runs",
    )
    run.run_dir = golem_dir / "runs" / run.run_id
    run.hooks = _spend_hooks(run, "builder")
    run.tester_hooks = _spend_hooks(run, "tester")
    return run


def _spend_hooks(run: kernel.Run, role: str) -> HooksManager:
    """Charge every model call to the task ledger and remember which model served which role."""

    def on_model_call(payload, _context=None):
        usage = payload.get("usage") or {}
        cost = usage.get("cost")
        if cost is None:
            cost = (
                usage.get("input_tokens", 0) * FALLBACK_USD_PER_INPUT_TOKEN
                + usage.get("output_tokens", 0) * FALLBACK_USD_PER_OUTPUT_TOKEN
            )
            cost = max(cost, FALLBACK_MIN_USD_PER_CALL)
        model = payload.get("model") or "unknown"
        run.models_by_role.setdefault(role, set()).add(model)
        run.charge(cost, f"{role}:{model}")

    hooks = HooksManager()
    hooks.on("PostModelCall", HookEntry(handler=on_model_call))
    return hooks


async def run_task(run: kernel.Run) -> str:
    licence = run.licence
    run.say("licence", f"{licence.data['name']} sha256 {licence.sha256}")
    active = run.registry.active()
    run.say(
        "registry",
        "before: "
        + (
            ", ".join(f"{name}@{version}" for name, version in sorted(active.items()))
            or "empty"
        ),
    )
    run.say("task", run.task)

    handoff, answer = "", ""
    sessions = licence.budget("max_sessions_per_task")
    for session in range(1, sessions + 1):
        run.installed_now.clear()
        tools = (
            kernel.read_tools(run)
            + [kernel.make_tool_tool(run), kernel.install_tool_tool(run)]
            + kernel.installed_tools(run)
        )
        loaded = [name for name in run.registry.active()]
        run.say(
            "session",
            f"{session}/{sessions} starts fresh; loads {len(loaded)} installed tool(s): {', '.join(loaded) or 'none'}",
        )
        remaining = max(licence.budget("max_usd_per_task") - run.spent, 0.01)
        prompt = (
            run.task
            if not handoff
            else f"{run.task}\n\nHandoff from the previous session:\n{handoff}"
        )
        request = {
            "model": licence.model("builder"),
            "instructions": INSTRUCTIONS + "\n\nInstalled tools:\n" + _listing(run),
            "input": prompt,
            "tools": tools,
            "stop_when": [
                step_count_is(
                    min(licence.budget("max_steps_per_session"), SDK_TURN_LIMIT - 2)
                ),
                max_cost(remaining),
                lambda _options: run.next_step_may_overrun(),
                lambda _options: bool(run.installed_now),
            ],
            "hooks": run.hooks,
        }
        if licence.data["models"].get("builder_plugins"):
            request["plugins"] = licence.data["models"]["builder_plugins"]
        if licence.data["models"].get("builder_reasoning"):
            request["reasoning"] = licence.data["models"]["builder_reasoning"]
        result = call_model(run.client, request)
        try:
            text = await result.get_text()
        except RuntimeError as exc:
            run.say("session", f"{session}/{sessions} stopped by the SDK: {exc}")
            text = f"The session ended before a final answer: {exc}"
        if run.installed_now and session < sessions and not run.over_budget():
            handoff = text.strip()[:3000]
            run.say(
                "respawn",
                f"installed {', '.join(run.installed_now)}; starting a fresh session",
            )
            continue
        answer = text.strip() or "The session ended without an answer."
        break

    run.say(
        "models",
        "; ".join(
            f"{role}: {', '.join(sorted(models))}"
            for role, models in sorted(run.models_by_role.items())
        ),
    )
    run.say("spend", f"${run.spent:.4f} of ${licence.budget('max_usd_per_task')} cap")
    run.say(
        "registry",
        "after: "
        + (
            ", ".join(
                f"{name}@{version}"
                for name, version in sorted(run.registry.active().items())
            )
            or "empty"
        ),
    )
    run.say(
        "licence",
        f"sha256 {licence.sha256} {'unchanged' if unchanged(licence) else 'CHANGED'}",
    )
    run.run_dir.mkdir(parents=True, exist_ok=True)
    (run.run_dir / "result.md").write_text(answer + "\n", encoding="utf-8")
    return answer


def _listing(run: kernel.Run) -> str:
    """One line per installed tool: what it does, what it returns, and how its calls have gone."""
    rows = run.registry.listing()
    if not rows:
        return "(none yet)"
    usage = run.registry.journal("usage")
    lines = []
    for row in rows:
        ref = f"{row['name']}@{row['version']}"
        calls = [item for item in usage if item.get("tool") == ref]
        lines.append(
            json.dumps(
                {
                    **{key: row[key] for key in ("name", "version", "access", "description")},
                    "returns": schema.outline(row["output_schema"])[:600],
                    "calls": len(calls),
                    "failed_calls": sum(1 for item in calls if not item.get("ok")),
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)
