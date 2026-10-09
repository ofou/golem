#!/usr/bin/env python3
"""Run the definition of done end to end on one real repository, then check it.

    python scripts/dod_demo.py [--root ~/golem-runner] [--only 1,2] [--keep] [--evidence NAME]

The target is aio-libs/aiohttp at the head of CI run 37533646169 (a sparse checkout of aiohttp/
and tests/), with the logs of two of that run's jobs attached. Each task is its own
`golem run`: a new process and new sessions, on the same .golem registry.

  1 build    the skipped tests per test file in each attached CI log
  2 build    the aiohttp/ modules each test file imports
  3 manage   a reusable view of how the installed tools fit together
  4 combine  a different question that needs 1 and 2 together

Nothing in a task names a tool, and no task says which tools to build or reuse. Then
scripts/dod_check.py reads what the runs recorded, and with --evidence the registry,
the runs, the journals, and the logs are copied to evidence/NAME.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

GOLEM_ROOT = Path(__file__).resolve().parent.parent
GITHUB = "aio-libs/aiohttp"
SHA = "9311df8d3ae2821c964b9a8eb5d68d18b59336f2"
JOBS = {
    "ios-cp314-macos.log": "112509788981",
    "ubuntu-3.11.log": "112509789907",
}
TASKS = [
    (
        "build",
        (
            "CI run 37533646169 on this repository failed. From the two attached job logs, how many tests were "
            "skipped in each job, per test file, and which test file had the most skipped tests in each job?"
        ),
    ),
    (
        "build",
        (
            "Which aiohttp/ source modules does each test file under tests/ import, and which aiohttp/ module is "
            "imported by the most test files?"
        ),
    ),
    (
        "manage",
        (
            "The Golem registry in this repository will keep growing. Give me a reusable way to see how the installed "
            "tools fit together: for every installed tool, its version, the task words its gap quotes, how many times "
            "it was called and how many of those calls failed, and which other installed tools' input fields its "
            "output fields can fill. Then use it to answer for the tools installed now."
        ),
    ),
    (
        "combine",
        (
            "In the attached CI job logs, which test file had the most skipped tests in each job, and which aiohttp/ "
            "source modules does each of those test files import?"
        ),
    ),
]
WITH_LOGS = {1, 4}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.home() / "golem-runner")
    parser.add_argument("--only", default="", help="task numbers to run, e.g. 1,2 (default: all)")
    parser.add_argument("--keep", action="store_true", help="keep the existing .golem instead of starting empty")
    parser.add_argument("--evidence", default="", help="copy the outcome to evidence/NAME")
    args = parser.parse_args()

    repo = checkout(args.root / "src" / "aiohttp")
    logs = fetch_logs(args.root / "inputs" / "aiohttp")
    log_dir = args.root / "logs" / "dod-demo"
    log_dir.mkdir(parents=True, exist_ok=True)
    if not args.keep and (repo / ".golem").exists():
        shutil.rmtree(repo / ".golem")
        print(f"cleared {repo / '.golem'}: the registry starts empty")

    chosen = {int(item) for item in args.only.split(",") if item.strip()} or set(range(1, len(TASKS) + 1))
    for number, (phase, task) in enumerate(TASKS, 1):
        if number not in chosen:
            continue
        attach = [item for log in logs for item in ("--attach", str(log))] if number in WITH_LOGS else []
        command = [sys.executable, "-m", "golem", "--repo", str(repo), "run", task, *attach]
        log_path = log_dir / f"{number}-{phase}.log"
        print(f"\n=== task {number} ({phase}), a new process; log {log_path}\n{task}", flush=True)
        with log_path.open("w", encoding="utf-8", buffering=1) as handle:
            proc = subprocess.Popen(  # noqa: S603
                command, cwd=GOLEM_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout or []:
                handle.write(line)
                if line.startswith(("[session]", "[create]", "[probe]", "[verdict]", "[install]", "[call]", "[spend]", "[refused]")):
                    print("  " + line.rstrip()[:220], flush=True)
            code = proc.wait()
        if code != 0:
            print(f"task {number} exited {code}; see {log_path}")
            return code

    check_path = log_dir / "dod-check.txt"
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(GOLEM_ROOT / "scripts" / "dod_check.py"), str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    check_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    print("\n" + result.stdout + result.stderr)
    if args.evidence:
        save_evidence(GOLEM_ROOT / "evidence" / args.evidence, repo, logs, log_dir)
    return result.returncode


def checkout(dest: Path) -> Path:
    git = shutil.which("git") or "git"
    if not (dest / ".git").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        run([git, "clone", "--filter=blob:none", "--no-checkout", "--quiet", f"https://github.com/{GITHUB}.git", str(dest)])
        run([git, "-C", str(dest), "sparse-checkout", "set", "--cone", "aiohttp", "tests"])
    head = subprocess.run(  # noqa: S603
        [git, "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if head != SHA:
        run([git, "-C", str(dest), "fetch", "--quiet", "--depth", "1", "origin", SHA])
        run([git, "-C", str(dest), "checkout", "--quiet", SHA])
    return dest


def fetch_logs(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, job in JOBS.items():
        path = dest / name
        if not path.is_file() or path.stat().st_size == 0:
            gh = shutil.which("gh") or "gh"
            text = subprocess.run(  # noqa: S603
                [gh, "run", "view", "-R", GITHUB, "--job", job, "--log"], capture_output=True, text=True, check=True
            ).stdout
            path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def save_evidence(dest: Path, repo: Path, logs: list[Path], log_dir: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    golem = repo / ".golem"
    for name in ("registry", "runs"):
        if (golem / name).is_dir():
            shutil.copytree(golem / name, dest / name)
    for name in ("usage.jsonl", "gaps.jsonl", "ledger.jsonl"):
        if (golem / name).is_file():
            shutil.copy2(golem / name, dest / name)
    for path in sorted(log_dir.iterdir()):
        shutil.copy2(path, dest / path.name)
    (dest / "SOURCE.md").write_text(
        f"# Where this evidence comes from\n\n- Repository: {GITHUB} at {SHA}, sparse checkout of aiohttp/ and tests/.\n"
        + "".join(
            f"- Attached `{log.name}`: `gh run view -R {GITHUB} --job {JOBS[log.name]} --log`\n" for log in logs
        )
        + "- Runs: `python scripts/dod_demo.py`, one `golem run` process per task, on one registry.\n"
        + "- Check: `python scripts/dod_check.py` over that registry, in dod-check.txt.\n",
        encoding="utf-8",
    )
    print(f"evidence saved to {dest}")


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
