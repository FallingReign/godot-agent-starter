#!/usr/bin/env python3
"""One public, cross-platform entry point for the project kit.

This module is intentionally a thin orchestration layer.  The existing tools
remain authoritative for their domains; this file gives them one predictable
command surface, establishes the target project without shell-dependent cwd
tricks, and normalises results for humans and automation.

Exit codes:
    0  command completed successfully
    1  delegated operation failed
    2  invalid command-line usage (argparse)
    3  command was safely refused or a prerequisite is unavailable
  130  interrupted
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

TOOLS = Path(__file__).resolve().parent / "tools"
sys.path.insert(0, str(TOOLS))
import project_context  # noqa: E402
import engine_discovery  # noqa: E402
import providers  # noqa: E402

MIN_PYTHON = (3, 10)
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 3

_PROVIDER_EXECUTABLES = {"claude", "copilot", "gemini"}
_ENGINE_STAGES = {"import", "typecheck", "resources", "gut", "smoke"}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class CliError(RuntimeError):
    """An expected, user-actionable command failure."""

    def __init__(self, message: str, *, code: int = EXIT_FAILED,
                 status: str = "failed") -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS lets the same options work before or after a subcommand without
    # a child parser's unused default overwriting the value parsed by its parent.
    parser.add_argument(
        "--project",
        default=argparse.SUPPRESS,
        help="target project directory (default: current directory)",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit one machine-readable JSON result",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified project-kit command line.")
    _add_common_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser(
        "doctor", help="read-only, offline environment readiness summary"
    )
    _add_common_options(doctor)
    doctor.set_defaults(action="doctor")

    setup = commands.add_parser(
        "setup", help="perform one explicitly selected setup operation"
    )
    _add_common_options(setup)
    setup_commands = setup.add_subparsers(dest="setup_command", required=True)
    setup_specs = (
        ("repair", "apply safe repository-local repairs", None),
        ("editor", "update user editor settings", None),
        ("repository", "initialise Git without staging or committing", None),
        ("import", "run the initial Godot import", None),
        ("format", "format game code with the locked local tool", None),
        ("profiles", "list available import profiles", None),
        ("dependency", "acquire one exact locked dependency", "dependency"),
        ("audit-dependencies", "authenticate every canonical lock source", None),
        ("layout", "select whether the game lives at the kit root or in src", "layout"),
        ("name", "set the game project name", "name"),
        ("profile", "apply one named import profile", "profile"),
    )
    for name, help_text, value_name in setup_specs:
        command = setup_commands.add_parser(name, help=help_text)
        _add_common_options(command)
        if value_name == "dependency":
            command.add_argument("value", choices=("gdtoolkit", "gut", "mermaid"))
        elif value_name == "layout":
            command.add_argument("value", choices=("root", "src"))
        elif value_name is not None:
            command.add_argument("value")
        command.set_defaults(action="setup")

    integrity = commands.add_parser(
        "integrity", help="review-only integrity trust operations"
    )
    _add_common_options(integrity)
    integrity_commands = integrity.add_subparsers(
        dest="integrity_command", required=True
    )
    accept = integrity_commands.add_parser(
        "accept", help="human-only acceptance of reviewed protected-file changes"
    )
    _add_common_options(accept)
    accept.set_defaults(action="integrity_accept")

    verify = commands.add_parser("verify", help="run the project verification gate")
    _add_common_options(verify)
    verify.add_argument(
        "--strict",
        action="store_true",
        help="refuse a green result containing unsupported SKIP stages",
    )
    verify.add_argument(
        "--stage",
        action="append",
        default=[],
        metavar="NAME",
        help="run one low-level gate stage; repeat for more than one",
    )
    verify.add_argument(
        "--fast",
        action="store_true",
        help="skip the import stage in a normal diagnostic run",
    )
    verify.add_argument(
        "--static",
        action="store_true",
        help="run every non-engine gate stage without discovering or launching Godot",
    )
    verify.set_defaults(action="verify")

    self_test = commands.add_parser(
        "self-test",
        help="run the kit control-plane regression suite without starting Godot",
    )
    _add_common_options(self_test)
    self_test.set_defaults(action="self_test")

    architecture = commands.add_parser(
        "architecture", help="maintain the generated module-dependency record"
    )
    _add_common_options(architecture)
    architecture_commands = architecture.add_subparsers(
        dest="architecture_command", required=True
    )
    architecture_update = architecture_commands.add_parser(
        "update", help="regenerate the module graph after an approved boundary change"
    )
    _add_common_options(architecture_update)
    architecture_update.set_defaults(action="architecture_update")

    sanitize = commands.add_parser(
        "sanitize", help="inspect or apply deterministic source cleanups"
    )
    _add_common_options(sanitize)
    sanitize.add_argument(
        "--write", action="store_true", help="apply the reported mechanical cleanups"
    )
    sanitize.set_defaults(action="sanitize")

    schema = commands.add_parser(
        "schema", help="inspect the validated shape of kit-owned JSON artefacts"
    )
    _add_common_options(schema)
    schema_commands = schema.add_subparsers(dest="schema_command", required=True)
    schema_describe = schema_commands.add_parser(
        "describe", help="show the exact accepted fields for one artefact"
    )
    _add_common_options(schema_describe)
    schema_describe.add_argument("value", choices=("proposal", "shape"))
    schema_describe.set_defaults(action="schema_describe")

    godot_docs = commands.add_parser(
        "godot-docs", help="build or query the installed engine's API reference"
    )
    _add_common_options(godot_docs)
    godot_docs_commands = godot_docs.add_subparsers(
        dest="godot_docs_command", required=True
    )
    docs_build = godot_docs_commands.add_parser(
        "build", help="generate the local reference from the installed engine"
    )
    _add_common_options(docs_build)
    docs_build.set_defaults(action="godot_docs")
    docs_show = godot_docs_commands.add_parser(
        "show", help="show a Godot class or Class.member"
    )
    _add_common_options(docs_show)
    docs_show.add_argument("value")
    docs_show.set_defaults(action="godot_docs")
    docs_search = godot_docs_commands.add_parser(
        "search", help="search member names across the local reference"
    )
    _add_common_options(docs_search)
    docs_search.add_argument("value")
    docs_search.set_defaults(action="godot_docs")

    friction = commands.add_parser(
        "friction", help="report committed authoring churn at a slice boundary"
    )
    _add_common_options(friction)
    friction.add_argument(
        "--since", default="", help="Git ref to measure from (default: proposal baseline)"
    )
    friction.set_defaults(action="friction")

    plan = commands.add_parser("plan", help="regenerate the living plan")
    _add_common_options(plan)
    plan.set_defaults(action="plan")

    serve = commands.add_parser("serve", help="ensure the local board is running")
    _add_common_options(serve)
    serve.set_defaults(action="serve")

    retro = commands.add_parser("retro", help="retrospective status and explicit runs")
    _add_common_options(retro)
    retro_commands = retro.add_subparsers(dest="retro_command", required=True)

    retro_status = retro_commands.add_parser(
        "status", help="show whether deterministic retrospective evidence is due"
    )
    _add_common_options(retro_status)
    retro_status.set_defaults(action="retro_status")

    retro_run = retro_commands.add_parser(
        "run", help="explicitly run the configured retrospective provider"
    )
    _add_common_options(retro_run)
    retro_run.add_argument(
        "--confirm-spend",
        action="store_true",
        help="confirm that this explicit provider action may spend quota",
    )
    retro_run.set_defaults(action="retro_run")

    retro_publish = retro_commands.add_parser(
        "publish",
        help="validate findings and publish them to the decision board",
    )
    _add_common_options(retro_publish)
    retro_publish.add_argument(
        "report",
        nargs="?",
        help="docs/retro findings report (default: newest *-findings.md)",
    )
    retro_publish.set_defaults(action="retro_publish")

    release = commands.add_parser(
        "release", help="build or validate a distributable kit archive"
    )
    _add_common_options(release)
    release_commands = release.add_subparsers(dest="release_command", required=True)
    for name, help_text, archive_help in (
        ("build", "build and verify a release archive", "output archive path"),
        ("inspect", "safely inspect a release archive", "archive to inspect"),
        ("verify", "fully verify a release archive", "archive to verify"),
    ):
        release_command = release_commands.add_parser(name, help=help_text)
        _add_common_options(release_command)
        release_command.add_argument("archive", help=archive_help)
        release_command.set_defaults(action="release")
    return parser


def _resolve_project(raw: str | os.PathLike[str] | None) -> Path:
    candidate = Path(raw) if raw is not None else Path.cwd()
    try:
        project = candidate.expanduser().resolve()
    except OSError as exc:
        raise CliError(
            f"cannot resolve project directory {candidate}: {exc}",
            code=EXIT_REFUSED,
            status="project_unavailable",
        ) from exc
    if not project.is_dir():
        raise CliError(
            f"project directory does not exist: {project}",
            code=EXIT_REFUSED,
            status="project_unavailable",
        )
    try:
        kit_root, _marker = project_context.locate_kit_root(project)
    except project_context.ProjectContextError as exc:
        raise CliError(
            str(exc), code=EXIT_REFUSED, status="project_unavailable"
        ) from exc
    return kit_root


def _script(project: Path, *parts: str) -> Path:
    path = project.joinpath(*parts)
    if not path.is_file():
        raise CliError(
            f"required kit command is unavailable: {path}",
            code=EXIT_REFUSED,
            status="command_unavailable",
        )
    return path


def _verification_needs_engine(args: argparse.Namespace) -> bool:
    if bool(getattr(args, "static", False)):
        return False
    if bool(getattr(args, "strict", False)):
        return True
    requested = {
        stage
        for value in getattr(args, "stage", [])
        for stage in str(value).split(",")
        if stage
    }
    return not requested or bool(requested.intersection(_ENGINE_STAGES))


def _verification_environment(
    project: Path, args: argparse.Namespace
) -> tuple[dict[str, str] | None, engine_discovery.EngineSelection | None]:
    """Return a process-local exact-version selection for native verification."""
    if not _verification_needs_engine(args):
        return None, None
    selected = engine_discovery.select_godot(project)
    if selected.known_mismatch:
        raise CliError(
            f"selected Godot {selected.selected_version} at {selected.path}; "
            f"expected exactly {engine_discovery.EXPECTED_GODOT_VERSION}. "
            "Set GODOT_BIN to the supported Standard build or place the exact "
            "official binary beside it.",
            code=EXIT_REFUSED,
            status="engine_version_mismatch",
        )
    if selected.path is None:
        return None, selected
    return {"GODOT_BIN": str(selected.path)}, selected


def _looks_like_provider_action(command: Sequence[str]) -> bool:
    if not command:
        return False
    raw_names = {Path(str(part)).name.lower() for part in command}
    executable = Path(str(command[0])).name.lower()
    if {executable, Path(executable).stem}.intersection(_PROVIDER_EXECUTABLES):
        return True
    return "retro.py" in raw_names and "--sdk" in command


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    allow_provider: bool = False,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [str(part) for part in command]
    if _looks_like_provider_action(argv) and not allow_provider:
        raise CliError(
            "provider action refused: use `retro run --confirm-spend` explicitly",
            code=EXIT_REFUSED,
            status="confirmation_required",
        )
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            env=({**os.environ, **environment} if environment else None),
        )
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            f"command timed out after {timeout}s: {Path(argv[0]).name}",
            status="timed_out",
        ) from exc
    except OSError as exc:
        raise CliError(f"could not start command: {exc}") from exc


def _run_process_inherited(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a human-facing command with live output and shared cancellation.

    Verification can spend minutes inside native tools. Capturing its entire
    output made the public launcher look frozen and made an interrupted child
    difficult to distinguish from an orphan. Inheriting the console keeps the
    stage stream visible and lets one Ctrl-C reach the launcher, gate and native
    child in the same console group. JSON callers continue to use the captured
    process path above.
    """
    argv = [str(part) for part in command]
    if _looks_like_provider_action(argv):
        raise CliError(
            "provider action refused: use `retro run --confirm-spend` explicitly",
            code=EXIT_REFUSED,
            status="confirmation_required",
        )
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            shell=False,
            env=({**os.environ, **environment} if environment else None),
        )
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            f"command timed out after {timeout}s: {Path(argv[0]).name}",
            status="timed_out",
        ) from exc
    except OSError as exc:
        raise CliError(f"could not start command: {exc}") from exc


def _process_payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
    }


def _process_summary(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Return process metadata without duplicating a parsed command payload."""
    return {
        "exit_code": result.returncode,
        "stderr": result.stderr or "",
    }


def _output_tail(text: str, limit: int = 12) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def _doctor(project: Path, _args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    """Consume the authoritative read-only bootstrap probe.

    Bootstrap's default mode is the setup detector contract. Doctor never
    passes its mutating or opt-in capability flags, and invokes no second
    process, so this command cannot accidentally become a setup shortcut.
    """
    bootstrap = _script(project, "bootstrap.py")
    result = _run_process(
        [sys.executable, str(bootstrap), "--json"], cwd=project, timeout=300
    )
    try:
        state = json.loads(result.stdout or "")
    except ValueError:
        state = None
    valid = (
        isinstance(state, dict)
        and isinstance(state.get("complete"), bool)
        and isinstance(state.get("results"), list)
        and result.returncode in (0, 1)
        and (result.returncode == 0) == state.get("complete")
    )
    if not valid:
        output = (result.stdout or "") + (result.stderr or "")
        payload = {
            "ok": False,
            "command": "doctor",
            "status": "probe_failed",
            "project": str(project),
            "bootstrap": state,
            "check": {"available": (project / "check.py").is_file()},
            "process": _process_summary(result),
        }
        human = ["doctor: bootstrap probe failed"]
        human.extend("  " + line for line in _output_tail(output))
        return EXIT_FAILED, payload, human

    ready = bool(state["complete"])
    code = EXIT_OK if ready else EXIT_REFUSED
    status = "ready" if ready else "needs_setup"
    results = [item for item in state["results"] if isinstance(item, dict)]
    blocking = [
        str(item.get("name"))
        for item in results
        if item.get("state") != "OK" and not item.get("advisory")
    ]
    check_result = next(
        (item for item in results if item.get("name") in {"check", "verification"}),
        None,
    )
    payload = {
        "ok": ready,
        "command": "doctor",
        "status": status,
        "project": str(project),
        "bootstrap": state,
        "check": {
            "available": (project / "check.py").is_file(),
            "bootstrap_result": check_result,
        },
        "blocking": blocking,
        "process": _process_summary(result),
    }
    human = [f"doctor: {status}", f"  project: {project}"]
    if blocking:
        human.append("  blocking: " + ", ".join(blocking))
    else:
        human.append(f"  setup checks: {len(results)}")
    human.append("  bootstrap ran in read-only, offline detection mode")
    return code, payload, human


def _setup(project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    bootstrap = _script(project, "bootstrap.py")
    operation = args.setup_command
    flags = {
        "repair": ["--fix"],
        "editor": ["--configure-editor"],
        "repository": ["--init-git"],
        "import": ["--import-project"],
        "format": [],
        "profiles": ["--list-import-profiles"],
        "dependency": ["--download-dep", getattr(args, "value", "")],
        "audit-dependencies": ["--audit-dependencies"],
        "layout": [
            "--game-layout",
            "." if getattr(args, "value", "") == "root" else "src",
        ],
        "name": ["--project-name", getattr(args, "value", "")],
        "profile": ["--import-profile", getattr(args, "value", "")],
    }
    targets = {
        "repair": "repair",
        "editor": "editor-settings",
        "repository": "git-repo",
        "import": "import-cache",
        "dependency": getattr(args, "value", ""),
        "audit-dependencies": "dependency-audit",
        "layout": "game-layout",
        "name": "project-name",
    }
    if operation == "format":
        result = _run_process(
            [sys.executable, str(_script(project, "check.py")), "--fix-format"],
            cwd=project,
            timeout=300,
        )
    else:
        command = [sys.executable, str(bootstrap), *flags[operation]]
        if operation in targets:
            command.extend(["--operation-target", targets[operation]])
        result = _run_process(command, cwd=project, timeout=900)
    output = (result.stdout or "") + (result.stderr or "")
    ok = result.returncode == 0
    payload = {
        "ok": ok,
        "command": "setup",
        "operation": operation,
        "status": "completed" if ok else "incomplete_or_failed",
        "project": str(project),
        "process": _process_payload(result),
    }
    human = [f"setup {operation}: {'completed' if ok else 'incomplete or failed'}"]
    human.extend("  " + line for line in _output_tail(output, limit=12))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _integrity_accept(
        project: Path, _args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    check = _script(project, "check.py")
    result = _run_process(
        [sys.executable, str(check), "--accept-gate-changes"],
        cwd=project,
        timeout=120,
    )
    output = (result.stdout or "") + (result.stderr or "")
    ok = result.returncode == 0
    payload = {
        "ok": ok,
        "command": "integrity accept",
        "status": "accepted" if ok else "failed",
        "project": str(project),
        "process": _process_payload(result),
    }
    human = [f"integrity: {'accepted' if ok else 'failed'}"]
    human.extend("  " + line for line in _output_tail(output, limit=12))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _verify(project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    if args.strict:
        if args.stage or args.fast or args.static:
            raise CliError(
                "strict verification is always complete; --stage, --fast and --static are unsupported",
                code=EXIT_REFUSED,
                status="strict_scope_refused",
            )
        environment, engine_selection = _verification_environment(project, args)
        strict = _script(project, "tools", "strict_verify.py")
        result = _run_process(
            [sys.executable, str(strict), "--json"],
            cwd=project,
            timeout=7200,
            environment=environment,
        )
        try:
            report = json.loads(result.stdout or "")
        except ValueError:
            report = None
        valid = (
            isinstance(report, dict)
            and report.get("exit_code") in (EXIT_OK, EXIT_FAILED, EXIT_REFUSED)
            and result.returncode == report.get("exit_code")
        )
        if not valid:
            code, status = EXIT_FAILED, "strict_report_invalid"
        else:
            code = int(report["exit_code"])
            status = str(report.get("status") or "failed")
        payload = {
            "ok": code == EXIT_OK,
            "command": "verify",
            "status": status,
            "project": str(project),
            "strict": True,
            "report": report,
            "process": _process_payload(result),
            "engine": (
                {
                    "path": str(engine_selection.path),
                    "source": engine_selection.source,
                }
                if engine_selection and engine_selection.path else None
            ),
        }
        human = [f"verify strict: {status}"]
        if isinstance(report, dict):
            for stage in report.get("stages", []):
                if isinstance(stage, dict) and stage.get("status") != "passed":
                    human.append(
                        f"  {stage.get('name', 'unknown')}: "
                        f"{stage.get('status', 'unknown')} - {stage.get('reason', '')}"
                    )
        else:
            human.extend(
                "  " + line
                for line in _output_tail((result.stdout or "") + (result.stderr or ""))
            )
        return code, payload, human

    if args.static and (args.stage or args.fast):
        raise CliError(
            "--static cannot be combined with --stage or --fast",
            code=EXIT_REFUSED,
            status="static_scope_refused",
        )
    environment, engine_selection = _verification_environment(project, args)
    check = _script(project, "check.py")
    command = [sys.executable, str(check)]
    for stage in args.stage:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", stage):
            raise CliError(
                f"invalid gate stage name: {stage!r}",
                code=EXIT_REFUSED,
                status="invalid_stage",
            )
        command.extend(["--only", stage])
    if args.fast:
        command.append("--fast")
    if args.static:
        command.append("--static")
    streamed = not bool(getattr(args, "json_output", False))
    result = (
        _run_process_inherited(
            command, cwd=project, timeout=1800, environment=environment
        )
        if streamed
        else _run_process(
            command, cwd=project, timeout=1800, environment=environment
        )
    )
    output = (result.stdout or "") + (result.stderr or "")
    plain_output = _ANSI_ESCAPE.sub("", output)
    skips = re.findall(r"(?m)^\s*SKIP\s+(.+?)\s*$", plain_output)
    gate_passed = result.returncode == 0 and (
        streamed or "GATE PASSED" in plain_output
    )
    if not gate_passed:
        code, status = EXIT_FAILED, "failed"
    else:
        code, status = EXIT_OK, "passed"
    payload = {
        "ok": code == EXIT_OK,
        "command": "verify",
        "status": status,
        "project": str(project),
        "strict": False,
        "stages": list(args.stage),
        "fast": bool(args.fast),
        "static": bool(args.static),
        "skips": skips,
        "process": _process_payload(result),
        "engine": (
            {
                "path": str(engine_selection.path),
                "source": engine_selection.source,
            }
            if engine_selection and engine_selection.path else None
        ),
    }
    human = [f"verify: {status}"]
    if skips:
        human.append("  skipped: " + ", ".join(skips))
    if code == EXIT_FAILED and not streamed:
        human.extend("  " + line for line in _output_tail(output))
    return code, payload, human


def _self_test(
    project: Path, args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    """Run kit-owned regression tests behind an enforced no-engine boundary."""
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tools/tests",
    ]
    environment = {
        "KIT_ENGINE_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    streamed = not bool(getattr(args, "json_output", False))
    result = (
        _run_process_inherited(
            command,
            cwd=project,
            timeout=1800,
            environment=environment,
        )
        if streamed
        else _run_process(
            command,
            cwd=project,
            timeout=1800,
            environment=environment,
        )
    )
    ok = result.returncode == 0
    output = (result.stdout or "") + (result.stderr or "")
    payload = {
        "ok": ok,
        "command": "self-test",
        "status": "passed" if ok else "failed",
        "project": str(project),
        "engine": "disabled",
        "process": _process_payload(result),
    }
    human = [
        f"self-test: {'passed' if ok else 'failed'}",
        "  native engine: disabled",
    ]
    if not ok and not streamed:
        human.extend("  " + line for line in _output_tail(output))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _maintenance_process(
    project: Path,
    *,
    label: str,
    script_name: str,
    arguments: Sequence[str],
    timeout: int = 300,
) -> tuple[int, dict[str, Any], list[str]]:
    result = _run_process(
        [sys.executable, str(_script(project, script_name)), *arguments],
        cwd=project,
        timeout=timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    ok = result.returncode == 0
    payload = {
        "ok": ok,
        "command": label,
        "status": "completed" if ok else "failed",
        "project": str(project),
        "process": _process_payload(result),
    }
    human = [f"{label}: {'completed' if ok else 'failed'}"]
    human.extend("  " + line for line in _output_tail(output))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _architecture_update(
    project: Path, _args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    return _maintenance_process(
        project,
        label="architecture update",
        script_name="arch.py",
        arguments=("--write",),
    )


def _sanitize(
    project: Path, args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    arguments = ("--write",) if args.write else ()
    return _maintenance_process(
        project,
        label="sanitize",
        script_name="sanitise.py",
        arguments=arguments,
    )


def _schema_describe(
    project: Path, args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    result = _run_process(
        [
            sys.executable,
            str(_script(project, "tools", "schema.py")),
            "--describe",
            args.value,
        ],
        cwd=project,
        timeout=60,
    )
    ok = result.returncode == 0 and bool((result.stdout or "").strip())
    payload = {
        "ok": ok,
        "command": "schema describe",
        "status": "described" if ok else "failed",
        "project": str(project),
        "artefact": args.value,
        "description": result.stdout or "",
        "process": _process_payload(result),
    }
    human = [f"schema {args.value}: {'described' if ok else 'failed'}"]
    human.extend("  " + line for line in (result.stdout or result.stderr).splitlines())
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _godot_docs(
    project: Path, args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    operation = args.godot_docs_command
    arguments = {
        "build": ("--build",),
        "show": (getattr(args, "value", ""),),
        "search": ("--search", getattr(args, "value", "")),
    }[operation]
    return _maintenance_process(
        project,
        label=f"godot-docs {operation}",
        script_name="tools/gddoc.py",
        arguments=arguments,
        timeout=360 if operation == "build" else 60,
    )


def _friction(
    project: Path, args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    arguments: tuple[str, ...] = ("--since", args.since) if args.since else ()
    return _maintenance_process(
        project,
        label="friction",
        script_name="tools/friction.py",
        arguments=arguments,
        timeout=60,
    )


def _plan(project: Path, _args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    plan_tool = _script(project, "tools", "plan_html.py")
    retro_tool = _script(project, "tools", "retro_html.py")
    plan_file = project / "plan.html"
    retro_file = project / "retro.html"

    # These pages are two views of the same review workflow. Generate both in
    # one public operation and keep server startup in the explicit `serve`
    # command, so a deterministic render never launches a background process.
    jobs = (
        ("plan", [sys.executable, str(plan_tool)]),
        ("retro", [sys.executable, str(retro_tool), "--no-board"]),
    )
    processes: dict[str, dict[str, Any]] = {}
    output_parts: list[str] = []
    all_succeeded = True
    for name, command in jobs:
        result = _run_process(command, cwd=project, timeout=180)
        processes[name] = _process_payload(result)
        output_parts.extend((result.stdout or "", result.stderr or ""))
        all_succeeded = all_succeeded and result.returncode == 0

    output = "".join(output_parts)
    ok = all_succeeded and plan_file.is_file() and retro_file.is_file()
    payload = {
        "ok": ok,
        "command": "plan",
        "status": "regenerated" if ok else "failed",
        "project": str(project),
        "path": str(plan_file),
        "paths": {"plan": str(plan_file), "retro": str(retro_file)},
        "processes": processes,
    }
    human = [f"plan: {'regenerated' if ok else 'failed'}"]
    if ok:
        human.append(f"  {plan_file}")
        human.append(f"  {retro_file}")
    else:
        human.extend("  " + line for line in _output_tail(output))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _serve(project: Path, _args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    tool = _script(project, "tools", "board.py")
    result = _run_process(
        [sys.executable, str(tool), "--ensure"], cwd=project, timeout=60
    )
    output = (result.stdout or "") + (result.stderr or "")
    match = re.search(r"http://127\.0\.0\.1:\d+/", output)
    url = match.group(0) if match else ""
    ok = result.returncode == 0 and bool(url)
    payload = {
        "ok": ok,
        "command": "serve",
        "status": "running" if ok else "failed",
        "project": str(project),
        "url": url or None,
        "process": _process_payload(result),
    }
    human = [f"serve: {url}" if ok else "serve: failed"]
    if not ok:
        human.extend("  " + line for line in _output_tail(output))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _retro_status(project: Path, _args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    tool = _script(project, "tools", "retro_due.py")
    result = _run_process(
        [sys.executable, str(tool), "--json"], cwd=project, timeout=30
    )
    try:
        state = json.loads(result.stdout or "") if result.returncode == 0 else None
    except ValueError:
        state = None
    ok = isinstance(state, dict)
    payload = {
        "ok": ok,
        "command": "retro status",
        "status": "due" if ok and state.get("due") else "not_due" if ok else "failed",
        "project": str(project),
        "retro": state,
        "process": _process_payload(result),
    }
    if ok:
        human = [
            f"retro: {payload['status']} "
            f"({state.get('unarchived', 0)}/{state.get('threshold', 0)} notes)"
        ]
    else:
        output = (result.stdout or "") + (result.stderr or "")
        human = ["retro status: failed"] + ["  " + line for line in _output_tail(output)]
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _retro_run(project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    try:
        provider = providers.selection(project, "analyzer")
    except (providers.ProviderConfigError, ValueError) as exc:
        raise CliError(
            f"retrospective provider configuration is invalid: {exc}",
            code=EXIT_REFUSED,
            status="provider_invalid",
        ) from exc
    if provider.automatic and not args.confirm_spend:
        raise CliError(
            "retrospective provider run refused; repeat with --confirm-spend",
            code=EXIT_REFUSED,
            status="confirmation_required",
        )
    if provider.automatic:
        blockers = providers.preflight(provider)
        if blockers:
            raise CliError(
                "retrospective provider is unavailable: " + "; ".join(blockers),
                code=EXIT_REFUSED,
                status="provider_unavailable",
            )
    tool = _script(project, "tools", "retro.py")
    mode = "--sdk" if provider.kind == "copilot-sdk" else "--print"
    result = _run_process(
        [sys.executable, str(tool), mode, "--force"],
        cwd=project,
        timeout=3600,
        allow_provider=provider.automatic and bool(args.confirm_spend),
    )
    ok = result.returncode == 0
    successful_status = "completed" if provider.automatic else "evidence_prepared"
    payload = {
        "ok": ok,
        "command": "retro run",
        "status": successful_status if ok else "failed",
        "project": str(project),
        "confirmed": bool(args.confirm_spend),
        "provider": provider.status(),
        "process": _process_payload(result),
    }
    output = (result.stdout or "") + (result.stderr or "")
    human = [f"retro run: {successful_status if ok else 'failed'}"]
    human.extend("  " + line for line in _output_tail(output, limit=6))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _retro_publish(
        project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    retro_root = (project / "docs" / "retro").resolve()
    command = [sys.executable, str(_script(project, "tools", "retro_rank.py"))]
    report_path: Path | None = None
    if args.report:
        raw = Path(args.report)
        report_path = (raw if raw.is_absolute() else project / raw).resolve()
        if not report_path.is_relative_to(retro_root):
            raise CliError(
                "retrospective findings must be under docs/retro",
                code=EXIT_REFUSED,
                status="report_outside_retro",
            )
        if not report_path.name.endswith("-findings.md"):
            raise CliError(
                "retrospective report name must end with -findings.md",
                code=EXIT_REFUSED,
                status="invalid_report_name",
            )
        command.append(str(report_path))

    ranked = _run_process(command, cwd=project, timeout=180)
    rank_output = (ranked.stdout or "") + (ranked.stderr or "")
    if ranked.returncode != 0:
        payload = {
            "ok": False,
            "command": "retro publish",
            "status": "validation_failed",
            "project": str(project),
            "report": str(report_path) if report_path else None,
            "process": _process_payload(ranked),
        }
        human = ["retro publish: validation failed"]
        human.extend("  " + line for line in _output_tail(rank_output, limit=8))
        return EXIT_FAILED, payload, human

    plan_code, plan_payload, _plan_human = _plan(project, args)
    ok = plan_code == EXIT_OK
    payload = {
        "ok": ok,
        "command": "retro publish",
        "status": "published" if ok else "render_failed",
        "project": str(project),
        "report": str(report_path) if report_path else None,
        "ranking": _process_payload(ranked),
        "plan": plan_payload,
    }
    human = [f"retro publish: {'published' if ok else 'render failed'}"]
    human.extend("  " + line for line in _output_tail(rank_output, limit=5))
    if ok:
        human.append(f"  {project / 'retro.html'}")
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _release_path(project: Path) -> Path:
    return project / "tools" / "release.py"


def _release(project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    tool = _release_path(project)
    if not tool.is_file():
        raise CliError(
            "release tooling is not installed yet (expected tools/release.py)",
            code=EXIT_REFUSED,
            status="release_unavailable",
        )
    result = _run_process(
        [sys.executable, str(tool), args.release_command, args.archive],
        cwd=project,
        timeout=900,
    )
    ok = result.returncode == 0
    payload = {
        "ok": ok,
        "command": "release",
        "status": "completed" if ok else "failed",
        "project": str(project),
        "operation": args.release_command,
        "archive": args.archive,
        "process": _process_payload(result),
    }
    output = (result.stdout or "") + (result.stderr or "")
    human = [
        f"release {args.release_command}: {'completed' if ok else 'failed'}",
        f"  archive: {args.archive}",
    ]
    human.extend("  " + line for line in _output_tail(output, limit=6))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


_HANDLERS = {
    "doctor": _doctor,
    "setup": _setup,
    "integrity_accept": _integrity_accept,
    "verify": _verify,
    "self_test": _self_test,
    "architecture_update": _architecture_update,
    "sanitize": _sanitize,
    "schema_describe": _schema_describe,
    "godot_docs": _godot_docs,
    "friction": _friction,
    "plan": _plan,
    "serve": _serve,
    "retro_status": _retro_status,
    "retro_run": _retro_run,
    "retro_publish": _retro_publish,
    "release": _release,
}
_COMMAND_LABELS = {
    "doctor": "doctor",
    "setup": "setup",
    "integrity_accept": "integrity accept",
    "verify": "verify",
    "self_test": "self-test",
    "architecture_update": "architecture update",
    "sanitize": "sanitize",
    "schema_describe": "schema describe",
    "godot_docs": "godot-docs",
    "friction": "friction",
    "plan": "plan",
    "serve": "serve",
    "retro_status": "retro status",
    "retro_run": "retro run",
    "retro_publish": "retro publish",
    "release": "release",
}


def _emit(payload: dict[str, Any], human: list[str], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("\n".join(human))


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    wants_json = "--json" in raw_argv
    if sys.version_info < MIN_PYTHON:
        payload = {
            "ok": False,
            "command": None,
            "status": "python_unsupported",
            "error": (
                f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required; "
                f"found {sys.version_info.major}.{sys.version_info.minor}"
            ),
        }
        _emit(payload, [f"kit: {payload['error']}"], json_output=wants_json)
        return EXIT_REFUSED

    parser = build_parser()
    args = parser.parse_args(raw_argv)
    json_output = bool(getattr(args, "json_output", False))
    try:
        project = _resolve_project(getattr(args, "project", None))
        handler = _HANDLERS[args.action]
        code, payload, human = handler(project, args)
    except CliError as exc:
        code = exc.code
        action = getattr(args, "action", None)
        payload = {
            "ok": False,
            "command": _COMMAND_LABELS.get(action, action),
            "status": exc.status,
            "error": str(exc),
        }
        human = [f"kit: {exc}"]
    payload["exit_code"] = code
    _emit(payload, human, json_output=json_output)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nkit: interrupted", file=sys.stderr)
        raise SystemExit(130)
