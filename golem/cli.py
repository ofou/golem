"""golem run | registry | verify | rollback | licence | login | credits"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from golem import auth, schema, snapshot
from golem import licence as licence_mod
from golem.registry import Registry
from golem.sandbox import Sandbox


def _default_licence() -> Path:
    """Golem's own licence: inside the package when installed from a wheel, at the
    repository root in a source checkout or the Docker image."""
    here = Path(__file__).resolve().parent
    packaged = here / "authority.json"
    return packaged if packaged.is_file() else here.parent / "authority.json"


DEFAULT_LICENCE = _default_licence()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="golem",
        description="An agent that builds, tests, and keeps its own tools.",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="repository Golem works in (default: current directory)",
    )
    parser.add_argument(
        "--licence",
        default=None,
        help="licence file (default: <repo>/.golem/authority.json, else Golem's)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="do one task")
    run_p.add_argument("task")
    run_p.add_argument(
        "--attach",
        action="append",
        default=[],
        help="file to give the task (repeatable)",
    )

    sub.add_parser("registry", help="list installed tools, versions, and usage")
    verify = sub.add_parser(
        "verify",
        help="re-run every installed tool's tests in the sandbox and check its files against the receipt (no key needed)",
    )
    verify.add_argument(
        "--attach",
        action="append",
        default=[],
        help="file the tools' tests read under /inputs (repeatable)",
    )
    roll = sub.add_parser("rollback", help="point a tool back to an earlier version")
    roll.add_argument("name")
    roll.add_argument("version")
    sub.add_parser("licence", help="print the licence and its sha256")
    login = sub.add_parser(
        "login", help="log in with OpenRouter (OAuth PKCE) and store a key for Golem"
    )
    login.add_argument(
        "--headless",
        action="store_true",
        help="paste a code instead of using a localhost callback",
    )
    login.add_argument(
        "--port", type=int, default=0, help="callback port (default: any free port)"
    )
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="print the URL without opening a browser",
    )
    sub.add_parser(
        "credits", help="credits left on the OpenRouter account behind the key in use"
    )

    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    if args.licence:
        licence_path = Path(args.licence)
        if not licence_path.is_file():
            print(f"licence not found: {licence_path}", file=sys.stderr)
            return 2
    else:
        licence_path = repo / ".golem" / "authority.json"
        if not licence_path.is_file():
            licence_path = DEFAULT_LICENCE
    registry = Registry(repo / ".golem")

    if args.command == "licence":
        lic = licence_mod.load(licence_path)
        print(lic.raw.decode("utf-8").rstrip())
        print(f"sha256 {lic.sha256}  ({licence_path})")
        return 0
    if args.command == "registry":
        return _print_registry(registry)
    if args.command == "rollback":
        print(registry.rollback(args.name, args.version))
        return 0
    if args.command == "login":
        return _login(args)
    if args.command == "credits":
        return _credits()
    if args.command == "verify":
        return _verify(
            repo,
            registry,
            licence_mod.load(licence_path),
            [Path(item) for item in args.attach],
        )

    key = auth.api_key()
    if not key:
        print(
            "No OpenRouter key: run `golem login`, or set OPENROUTER_API_KEY",
            file=sys.stderr,
        )
        return 2
    if not Sandbox.available():
        print(
            "Docker is not running; Golem will not run generated code without its sandbox",
            file=sys.stderr,
        )
        return 2
    from golem.agent import build_run, run_task

    lic = licence_mod.load(licence_path)
    run = build_run(repo, args.task, lic, key, [Path(item) for item in args.attach])
    answer = asyncio.run(run_task(run))
    print("\n" + "=" * 72 + "\n" + answer)
    return 0


def _login(args) -> int:
    try:
        path = auth.login(
            headless=args.headless, port=args.port, open_browser=not args.no_browser
        )
    except (auth.LoginError, OSError) as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Key stored in {path} (mode 0600). OPENROUTER_API_KEY, when set, still takes precedence."
    )
    return _credits()


def _credits() -> int:
    key = auth.api_key()
    if not key:
        print(
            "No OpenRouter key: run `golem login`, or set OPENROUTER_API_KEY",
            file=sys.stderr,
        )
        return 2
    try:
        data = auth.credits(key)
    except OSError as exc:
        print(f"could not read credits: {exc}", file=sys.stderr)
        return 1
    total, used = (
        float(data.get("total_credits") or 0),
        float(data.get("total_usage") or 0),
    )
    print(
        f"OpenRouter credits: ${total - used:.2f} left of ${total:.2f} (key from {auth.key_source()})"
    )
    return 0


def _verify(repo: Path, registry: Registry, lic, attachments: list[Path]) -> int:
    """Re-prove every installed tool without any model or key: its files still hash to the
    receipt, its own and blind tests pass in the sandbox, and they still fail against stubs."""
    from golem.kernel import MIN_STUB_FAIL_RATIO, call_tool, result_digest

    active = registry.active()
    if not active:
        print("registry: empty")
        return 0
    if not Sandbox.available():
        print(
            "Docker is not running; installed tools are only ever run in the sandbox",
            file=sys.stderr,
        )
        return 2
    work = Path(tempfile.mkdtemp(prefix="golem-verify-"))
    failures, command = 0, ""
    try:
        snap = work / "snapshot"
        snapshot.build(repo, snap, attachments)
        export = registry.export(work / "registry")
        for name, version in sorted(active.items()):
            sandbox = Sandbox(lic, snap, export)
            bundle, manifest, receipt = (
                registry.bundle(name, version),
                registry.manifest(name, version),
                registry.receipt(name, version),
            )
            hashes = {
                fname: hashlib.sha256(
                    (bundle / fname).read_text(encoding="utf-8").encode("utf-8")
                ).hexdigest()
                for fname in receipt["sha256"]
            }
            same = hashes == receipt["sha256"]
            own = sandbox.run_tests(bundle, manifest["access"], "test_tool.py")
            blind = sandbox.run_tests(bundle, manifest["access"], "test_blind.py")
            stubs = [
                sandbox.run_tests_against_stub(bundle, manifest["access"], kind=kind)
                for kind in ("raises", "empty")
            ]
            survivors = {test for report in stubs for test in report.ok}
            total = max((report.ran for report in stubs), default=0)
            stub_ratio = (total - len(survivors)) / total if total else 0.0
            prober = Sandbox(lic, snap, export)
            probes = [
                (item, call_tool(prober, bundle, manifest, item["args"]))
                for item in receipt.get("probes") or []
                if item.get("args") is not None
            ]
            probes_ok = sum(1 for _item, out in probes if out.get("ok"))
            unchanged = sum(
                1
                for item, out in probes
                if out.get("ok") and result_digest(out["result"]) == item.get("result_sha256")
            )
            ok = (
                same
                and own.passed
                and blind.passed
                and stub_ratio >= MIN_STUB_FAIL_RATIO
                and probes_ok == len(probes)
            )
            failures += not ok
            command = own.command
            print(
                f"{name}@{version}: {'OK' if ok else 'FAIL'} | files {'match the receipt' if same else 'CHANGED since they were tested'}"
                f" | own {own.summary()} | blind {blind.summary()} | stubs fail {int(stub_ratio * 100)}%"
                + (
                    f" | probes {probes_ok}/{len(probes)} ok, {unchanged}/{len(probes)} return what they returned when tested"
                    if probes
                    else ""
                )
            )
            for item, out in probes:
                if not out.get("ok"):
                    print(f"    probe {json.dumps(item['args'])[:120]}: {out.get('error', '')[:160]}")
            for item in (own.failed + blind.failed)[:6]:
                print(f"    {item['status']} {item['test']}: {item['message'][:160]}")
        print(f"sandbox: {command}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failures else 0


def _print_registry(registry: Registry) -> int:
    active = registry.active()
    usage = registry.journal("usage")
    if not active:
        print("registry: empty")
        return 0
    for name, version in sorted(active.items()):
        manifest = registry.manifest(name, version)
        receipt = registry.receipt(name, version)
        calls = [row for row in usage if row["tool"].startswith(f"{name}@")]
        print(
            f"{name}@{version}  [{manifest['access']}]  versions: {', '.join(registry.versions(name))}"
        )
        print(f"    {manifest['description']}")
        print(f"    returns {schema.outline(manifest['output_schema'])[:300]}")
        print(
            f"    tests {receipt['tests']['ok']}/{receipt['tests']['ran']}, blind {receipt['blind_tests']['ok']}/{receipt['blind_tests']['ran']}, "
            f"stub failed {int(receipt['stub_failed'] * 100)}%, probes {sum(1 for p in receipt.get('probes') or [] if p.get('ok'))}/{len(receipt.get('probes') or [])} | "
            f"calls {len(calls)}, errors {sum(1 for row in calls if not row['ok'])}"
        )
        print(f"    gap: {json.dumps(manifest['gap'].get('task_quote', ''))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
