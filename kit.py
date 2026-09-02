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
import contextlib
import hashlib
import hmac
import json
import os
import re
import runpy
import secrets
import subprocess
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

CORE_ROOT = Path(__file__).resolve().parent
TOOLS = CORE_ROOT / "tools"
sys.path.insert(0, str(TOOLS))
import project_context  # noqa: E402
import engine_discovery  # noqa: E402
import providers  # noqa: E402
import cockpit  # noqa: E402
import runtime_paths  # noqa: E402
import native_engine  # noqa: E402
import process_supervisor  # noqa: E402
import kit_change_controller  # noqa: E402
import release as release_tool  # noqa: E402

ACTIVE_INSTALLATION = project_context.resolve_active_installation(CORE_ROOT)
DEFAULT_PROJECT_ROOT = ACTIVE_INSTALLATION.project_root

MIN_PYTHON = (3, 10)
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 3

_PROVIDER_EXECUTABLES = {"claude", "codex", "copilot", "gemini"}
_ENGINE_STAGES = {"import", "typecheck", "resources", "gut", "smoke"}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VERIFY_NONCE = re.compile(r"^[0-9a-f]{32}$")
_VERIFY_SECRET = re.compile(r"^[0-9a-f]{64}$")


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
    verify.add_argument(
        "--confirm-native-retry",
        default="",
        metavar="WARNING_SHA256",
        help=(
            "authorize one full/strict recovery run for the exact unresolved "
            "native-warning digest"
        ),
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

    gdls = commands.add_parser(
        "gdls", help="manage the kit-owned Godot language-server helper"
    )
    _add_common_options(gdls)
    gdls_commands = gdls.add_subparsers(dest="gdls_command", required=True)
    for name, help_text in (
        ("start", "start the exact-version kit-owned language server"),
        ("status", "inspect ownership and native safety without starting Godot"),
        ("stop", "stop only the exactly owned language-server process"),
    ):
        command = gdls_commands.add_parser(name, help=help_text)
        _add_common_options(command)
        command.set_defaults(action="gdls")
    for name, help_text, value_name in (
        ("diagnose", "request fresh diagnostics for one GDScript file", "file"),
        ("symbols", "list document symbols for one GDScript file", "file"),
        ("refs", "find references or text matches for one symbol", "symbol"),
    ):
        command = gdls_commands.add_parser(name, help=help_text)
        _add_common_options(command)
        command.add_argument("value", metavar=value_name)
        command.set_defaults(action="gdls")

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
    plan.add_argument(
        "--snapshot",
        nargs="?",
        const="",
        default=None,
        metavar="NAME",
        help="also keep a fingerprinted review snapshot (optional label)",
    )
    plan.set_defaults(action="plan")

    serve = commands.add_parser("serve", help="manage the local review cockpit")
    _add_common_options(serve)
    serve.add_argument(
        "serve_command",
        nargs="?",
        choices=("start", "status", "open", "stop"),
        default="start",
        help="start/reuse, inspect, explicitly open, or stop the cockpit",
    )
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
    retro_run.add_argument(
        "--since",
        help="explicit ISO lower bound for repository session activity",
    )
    retro_run.add_argument(
        "--limit",
        type=int,
        help="explicitly select the most recent N matching sessions",
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

    for name, help_text in (
        ("install", "review installing this kit into an existing project"),
        ("upgrade", "review upgrading a project that already uses this kit"),
    ):
        lifecycle = commands.add_parser(name, help=help_text)
        _add_common_options(lifecycle)
        lifecycle.add_argument("target", help="project directory to install or upgrade")
        lifecycle.add_argument(
            "--release",
            dest="release_source",
            help="verified release archive or exact extracted release",
        )
        lifecycle.add_argument(
            "--game-root",
            help="project-relative folder containing project.godot",
        )
        lifecycle.set_defaults(action="kit_change_prepare", lifecycle_mode=name)

    recover = commands.add_parser(
        "recover", help="continue or restore one interrupted kit change"
    )
    _add_common_options(recover)
    recover.add_argument("session_id", help="full kit change session identity")
    recover.set_defaults(action="kit_change_recover")
    return parser


def _resolve_project(raw: str | os.PathLike[str] | None) -> Path:
    candidate = Path(raw) if raw is not None else DEFAULT_PROJECT_ROOT
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
    del project  # command code belongs to the selected immutable core
    path = CORE_ROOT.joinpath(*parts)
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


def _native_retry_environment(
    project: Path,
    args: argparse.Namespace,
    *,
    nonce: str,
    warning: native_engine.NativeWarningSnapshot,
) -> dict[str, str]:
    supplied = str(getattr(args, "confirm_native_retry", "") or "").strip().lower()
    needs_engine = _verification_needs_engine(args)
    if not needs_engine:
        if supplied:
            raise CliError(
                "--confirm-native-retry is valid only for a full or strict native recovery run",
                code=EXIT_REFUSED,
                status="native_retry_scope_refused",
            )
        return {}
    if warning.state == native_engine.WARNING_CLEAN:
        if supplied:
            raise CliError(
                "native retry confirmation is stale; there is no unresolved warning",
                code=EXIT_REFUSED,
                status="native_retry_stale",
            )
        return {}
    if warning.state != native_engine.WARNING_UNRESOLVED or not warning.identity:
        raise CliError(
            "native warning state cannot be authenticated; engine verification is refused",
            code=EXIT_REFUSED,
            status="native_warning_unreadable",
        )
    if supplied != warning.identity:
        raise CliError(
            "an unresolved native application failure requires one explicit bounded retry; "
            "review the warning and rerun full/strict verification with "
            f"--confirm-native-retry {warning.identity}",
            code=EXIT_REFUSED,
            status="native_retry_confirmation_required",
        )
    try:
        token = native_engine.authorize_recovery_retry(
            project,
            warning.identity,
            nonce,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CliError(
            f"native retry authorization could not be recorded: {exc}",
            code=EXIT_REFUSED,
            status="native_retry_authorization_failed",
        ) from exc
    return {"KIT_NATIVE_RETRY_TOKEN": token}


def _selected_engine_path(project: Path, *, operation: str) -> Path:
    """Resolve one public engine candidate without launching it.

    Exact authentication belongs to the delegated native operation.  This
    selector still rejects a filename-known mismatch before delegation; an
    unversioned candidate is passed only to a child which performs the bounded
    engine-reported version probe before its requested launch.
    """
    selected = engine_discovery.select_godot(project)
    if selected.known_mismatch:
        raise CliError(
            f"{operation} refused Godot {selected.selected_version} at {selected.path}; "
            f"expected exactly {engine_discovery.EXPECTED_GODOT_VERSION}.",
            code=EXIT_REFUSED,
            status="engine_version_mismatch",
        )
    if selected.path is None:
        raise CliError(
            f"{operation} needs Godot {engine_discovery.EXPECTED_GODOT_VERSION}; "
            "run kit doctor for the supported official-binary locations.",
            code=EXIT_REFUSED,
            status="engine_unavailable",
        )
    return selected.path


def _looks_like_provider_action(command: Sequence[str]) -> bool:
    if not command:
        return False
    raw_names = {Path(str(part)).name.lower() for part in command}
    executable = Path(str(command[0])).name.lower()
    if {executable, Path(executable).stem}.intersection(_PROVIDER_EXECUTABLES):
        return True
    return "retro.py" in raw_names and "--sdk" in command


def _isolated_python_command(script: Path, *arguments: object) -> list[str]:
    try:
        return process_supervisor.isolated_python_script_command(
            sys.executable, script, CORE_ROOT, *arguments
        )
    except (OSError, ValueError) as exc:
        raise CliError(
            f"internal kit command is unsafe: {exc}",
            code=EXIT_REFUSED,
            status="command_unsafe",
        ) from exc


def _isolated_unittest_command(*arguments: object) -> list[str]:
    try:
        return process_supervisor.isolated_python_module_command(
            sys.executable,
            "unittest",
            CORE_ROOT / "kit.py",
            CORE_ROOT,
            *arguments,
        )
    except (OSError, ValueError) as exc:
        raise CliError(
            f"internal kit tests are unsafe: {exc}",
            code=EXIT_REFUSED,
            status="command_unsafe",
        ) from exc


def _isolated_python_environment(
    overrides: Mapping[str, object] | None = None,
) -> dict[str, str]:
    return process_supervisor.isolated_python_environment(overrides)


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    allow_provider: bool = False,
    allow_child_breakaway: bool = False,
    environment: dict[str, str] | None = None,
    inherit_environment: bool = True,
) -> subprocess.CompletedProcess[str]:
    argv = [str(part) for part in command]
    if _looks_like_provider_action(argv) and not allow_provider:
        raise CliError(
            "provider action refused: use `retro run --confirm-spend` explicitly",
            code=EXIT_REFUSED,
            status="confirmation_required",
        )
    outcome = process_supervisor.run_supervised(
        argv,
        cwd=cwd,
        timeout=timeout,
        environment=(
            ({**os.environ, **environment} if inherit_environment else dict(environment))
            if environment is not None
            else None
        ),
        capture_output=True,
        allow_child_breakaway=allow_child_breakaway,
    )
    if not outcome.termination_verified:
        raise CliError(
            f"child-process termination could not be verified: {Path(argv[0]).name}",
            status="containment_unverified",
        )
    if outcome.timed_out:
        raise CliError(
            f"command timed out after {timeout}s: {Path(argv[0]).name}",
            status="timed_out",
        )
    if outcome.cancelled:
        raise CliError(
            f"command was cancelled: {Path(argv[0]).name}",
            status="cancelled",
        )
    if outcome.launch_error:
        raise CliError(f"could not start command: {outcome.launch_error}")
    if outcome.returncode is None:
        raise CliError(
            f"child-process containment could not be verified: {Path(argv[0]).name}",
            status="containment_unverified",
        )
    return subprocess.CompletedProcess(
        argv,
        outcome.returncode,
        outcome.stdout or "",
        outcome.stderr or "",
    )


def _run_process_inherited(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    environment: dict[str, str] | None = None,
    inherit_environment: bool = True,
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
    outcome = process_supervisor.run_supervised(
        argv,
        cwd=cwd,
        timeout=timeout,
        environment=(
            ({**os.environ, **environment} if inherit_environment else dict(environment))
            if environment is not None
            else None
        ),
        capture_output=False,
        allow_child_breakaway=False,
    )
    if not outcome.termination_verified:
        raise CliError(
            f"child-process termination could not be verified: {Path(argv[0]).name}",
            status="containment_unverified",
        )
    if outcome.timed_out:
        raise CliError(
            f"command timed out after {timeout}s: {Path(argv[0]).name}",
            status="timed_out",
        )
    if outcome.cancelled:
        raise CliError(
            f"command was cancelled: {Path(argv[0]).name}",
            status="cancelled",
        )
    if outcome.launch_error:
        raise CliError(f"could not start command: {outcome.launch_error}")
    if outcome.returncode is None:
        raise CliError(
            f"child-process containment could not be verified: {Path(argv[0]).name}",
            status="containment_unverified",
        )
    return subprocess.CompletedProcess(argv, outcome.returncode, None, None)


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


def _final_json_object(text: str) -> dict[str, Any] | None:
    """Parse one command receipt after any preceding human-readable log lines.

    Long-lived tools may explain stale-state recovery before emitting their
    single-line JSON receipt. Only the final non-empty line is authoritative;
    accepting an earlier object would let trailing failure text be ignored.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _gdls_read_only_status(project: Path) -> dict[str, Any] | None:
    """Inspect retained GDLS ownership without starting or stopping anything."""
    tool = CORE_ROOT / "tools" / "gdls.py"
    if not tool.is_file():
        return None
    result = _run_process(
        _isolated_python_command(tool, "status"),
        cwd=project,
        timeout=30,
        environment=_isolated_python_environment(),
        inherit_environment=False,
    )
    return _final_json_object(result.stdout or "")


def _doctor(project: Path, _args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    """Consume the authoritative read-only bootstrap probe.

    Bootstrap's default mode is the setup detector contract. Doctor never
    passes its mutating or opt-in capability flags. It also asks the GDLS
    helper for a read-only ownership status so a retained engine which vanished
    after startup is visible before another GDLS command. Neither probe starts,
    stops or authenticates Godot.
    """
    bootstrap = _script(project, "bootstrap.py")
    result = _run_process(
        _isolated_python_command(bootstrap, "--json"),
        cwd=project,
        timeout=300,
        environment=_isolated_python_environment(),
        inherit_environment=False,
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
    native_warning = cockpit.persisted_native_warning(project)
    warnings = [native_warning] if native_warning else []
    gdls_status = _gdls_read_only_status(project)
    gdls_warning = (
        gdls_status.get("warning")
        if isinstance(gdls_status, dict)
        and isinstance(gdls_status.get("warning"), dict)
        else None
    )
    if gdls_warning:
        code = str(gdls_warning.get("code") or "gdls-native-safety")
        if not any(str(item.get("code") or "") == code for item in warnings):
            warnings.append({
                "code": code,
                "recorded_at": None,
                "operation": "gdls status",
                "summary": str(gdls_warning.get("summary") or "GDLS native safety is unresolved"),
            })
    if not valid:
        output = (result.stdout or "") + (result.stderr or "")
        payload = {
            "ok": False,
            "command": "doctor",
            "status": "probe_failed",
            "project": str(project),
            "bootstrap": state,
            "check": {"available": (CORE_ROOT / "check.py").is_file()},
            "warnings": warnings,
            "gdls": gdls_status,
            "process": _process_summary(result),
        }
        human = ["doctor: bootstrap probe failed"]
        human.extend("  " + line for line in _output_tail(output))
        for warning in warnings:
            human.append(f"  warning [{warning['code']}]: {warning['summary']}")
            warning_identity = warning.get("warning_sha256")
            if isinstance(warning_identity, str) and _VERIFY_SECRET.fullmatch(
                warning_identity
            ):
                human.append(
                    "  bounded retry (after explicit approval): "
                    f"kit verify --confirm-native-retry {warning_identity}"
                )
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
            "available": (CORE_ROOT / "check.py").is_file(),
            "bootstrap_result": check_result,
        },
        "blocking": blocking,
        "warnings": warnings,
        "gdls": gdls_status,
        "process": _process_summary(result),
    }
    human = [f"doctor: {status}", f"  project: {project}"]
    if blocking:
        human.append("  blocking: " + ", ".join(blocking))
    else:
        human.append(f"  setup checks: {len(results)}")
    for warning in warnings:
        human.append(f"  warning [{warning['code']}]: {warning['summary']}")
        warning_identity = warning.get("warning_sha256")
        if isinstance(warning_identity, str) and _VERIFY_SECRET.fullmatch(
            warning_identity
        ):
            human.append(
                "  bounded retry (after explicit approval): "
                f"kit verify --confirm-native-retry {warning_identity}"
            )
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
            _isolated_python_command(_script(project, "check.py"), "--fix-format"),
            cwd=project,
            timeout=300,
            environment=_isolated_python_environment(),
            inherit_environment=False,
        )
    else:
        arguments = list(flags[operation])
        if operation == "import":
            selected_engine = _selected_engine_path(
                project, operation="setup import"
            )
            arguments.extend(["--engine-bin", str(selected_engine)])
        if operation in targets:
            arguments.extend(["--operation-target", targets[operation]])
        result = _run_process(
            _isolated_python_command(bootstrap, *arguments),
            cwd=project,
            timeout=900,
            environment=_isolated_python_environment(),
            inherit_environment=False,
        )
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
    try:
        context = project_context.load_active_context(CORE_ROOT)
    except project_context.ProjectContextError as exc:
        raise CliError(
            f"cannot validate the active kit before integrity acceptance: {exc}",
            code=EXIT_REFUSED,
            status="project_unavailable",
        ) from exc
    if context.project_root == project and context.install_mode == "managed":
        raise CliError(
            "managed kit code is verified by its release manifest; install a reviewed "
            "release instead of changing its integrity baseline",
            code=EXIT_REFUSED,
            status="managed_integrity_immutable",
        )
    check = _script(project, "check.py")
    result = _run_process(
        _isolated_python_command(check, "--accept-gate-changes"),
        cwd=project,
        timeout=120,
        environment=_isolated_python_environment(),
        inherit_environment=False,
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


def _gate_summary_path(project: Path, nonce: str | None = None) -> Path:
    suffix = f"-{nonce}" if nonce else ""
    try:
        runs = runtime_paths.resolve(project, create=False).verification_runs
    except runtime_paths.RuntimeConfigError as exc:
        raise CliError(
            f"cannot resolve private verification evidence: {exc}",
            code=EXIT_REFUSED,
            status="runtime_unavailable",
        ) from exc
    return runs / f"run-summary{suffix}.json"


def _prepare_gate_summary(project: Path, nonce: str) -> None:
    """A previous gate's crash evidence must never be attributed to this run."""
    if _VERIFY_NONCE.fullmatch(nonce) is None:
        raise CliError(
            "verification run identity is invalid",
            code=EXIT_REFUSED,
            status="diagnostics_identity_invalid",
        )
    try:
        _gate_summary_path(project, nonce).unlink(missing_ok=True)
    except OSError as exc:
        raise CliError(
            f"cannot clear stale gate diagnostics: {exc}",
            code=EXIT_REFUSED,
            status="diagnostics_locked",
        ) from exc


def _read_gate_summary(
    project: Path,
    nonce: str,
    auth_key: str,
    repository_sha256: str,
) -> dict[str, Any] | None:
    """Read only diagnostics cryptographically bound to this public run."""
    if (
        _VERIFY_NONCE.fullmatch(nonce) is None
        or _VERIFY_SECRET.fullmatch(auth_key) is None
        or _VERIFY_SECRET.fullmatch(repository_sha256) is None
    ):
        return None
    try:
        raw = json.loads(
            _gate_summary_path(project, nonce).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != 2
        or raw.get("run_id") != nonce
        or raw.get("repository_sha256") != repository_sha256
    ):
        return None
    supplied_auth = raw.get("auth_sha256")
    if not isinstance(supplied_auth, str) or _VERIFY_SECRET.fullmatch(supplied_auth) is None:
        return None
    authenticated = dict(raw)
    authenticated.pop("auth_sha256", None)
    canonical = json.dumps(
        authenticated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_auth = hmac.new(
        bytes.fromhex(auth_key), canonical, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(supplied_auth, expected_auth):
        return None
    failed = raw.get("failed")
    results = raw.get("results")
    diagnostics = raw.get("diagnostics")
    if (
        not isinstance(failed, bool)
        or not isinstance(results, list)
        or not isinstance(diagnostics, dict)
    ):
        return None
    fields = {
        "native_crashes": ("code", "executable", "exit_code", "windows_status"),
        "engine_refusals": ("code", "executable"),
        "engine_start_failures": ("code", "executable", "exit_code"),
        "timeouts": ("code", "executable", "timeout_seconds"),
    }
    safe_diagnostics: dict[str, list[dict[str, Any]]] = {}
    for kind, allowed in fields.items():
        entries = diagnostics.get(kind) if isinstance(diagnostics.get(kind), list) else []
        safe_diagnostics[kind] = [
            {key: entry.get(key) for key in allowed if key in entry}
            for entry in entries[:20]
            if isinstance(entry, dict)
        ]
    return {
        "schema": 2,
        "run_id": nonce,
        "repository_sha256": repository_sha256,
        "auth_sha256": supplied_auth,
        "failed": failed,
        "results": [str(line)[:500] for line in results[:200]],
        "diagnostics": safe_diagnostics,
    }


def _strict_report_is_valid(
    project: Path,
    report: object,
    process_returncode: int,
    gate_summary: dict[str, Any] | None,
) -> bool:
    """Validate the public strict contract, including whether its gate ran."""
    if not isinstance(report, dict):
        return False
    if (
        report.get("schema") != 1
        or report.get("command") != "strict-verify"
        or report.get("exit_code") not in (EXIT_OK, EXIT_FAILED, EXIT_REFUSED)
        or process_returncode != report.get("exit_code")
        or report.get("status") not in ("passed", "failed", "blocked")
        or report.get("ok") is not (report.get("exit_code") == EXIT_OK)
    ):
        return False
    try:
        if Path(str(report.get("project"))).resolve() != project.resolve():
            return False
    except (OSError, ValueError):
        return False
    stages = report.get("stages")
    if stages is None:
        return bool(
            report.get("status") == "blocked"
            and report.get("exit_code") == EXIT_REFUSED
            and isinstance(report.get("error"), str)
            and report.get("error")
            and gate_summary is None
        )
    if not isinstance(stages, list) or not stages:
        return False
    authority_receipt = report.get("authority_receipt")
    if (
        not isinstance(authority_receipt, dict)
        or authority_receipt.get("receipt_trust")
        not in (
            "local-audit-matched",
            "portable-policy",
            "invalid",
            "no-exact-authority-event",
        )
        or not isinstance(authority_receipt.get("receipt_reasons"), list)
    ):
        return False
    names: set[str] = set()
    statuses: list[str] = []
    gate_status: str | None = None
    for stage in stages:
        if not isinstance(stage, dict):
            return False
        name = stage.get("name")
        status = stage.get("status")
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or status not in ("passed", "failed", "blocked", "not_run")
        ):
            return False
        names.add(name)
        statuses.append(str(status))
        if name == "gate":
            gate_status = str(status)
    if "failed" in statuses:
        expected_status, expected_exit = "failed", EXIT_FAILED
    elif "blocked" in statuses or "not_run" in statuses:
        expected_status, expected_exit = "blocked", EXIT_REFUSED
    elif set(statuses) == {"passed"}:
        expected_status, expected_exit = "passed", EXIT_OK
    else:
        return False
    if (
        report.get("status") != expected_status
        or report.get("exit_code") != expected_exit
    ):
        return False
    if expected_status == "passed" and authority_receipt.get("receipt_trust") == "invalid":
        return False
    if gate_status in ("passed", "failed"):
        return gate_summary is not None
    if gate_status in ("blocked", "not_run"):
        return gate_summary is None
    return False


@contextlib.contextmanager
def _verification_run_lock(project: Path) -> Iterator[None]:
    """Refuse overlapping public verifies using an OS-owned file lock.

    The file is intentionally persistent; process exit releases the kernel
    lock, so stale-file deletion and its read/unlink race are unnecessary.
    """
    try:
        paths = runtime_paths.resolve(project, create=True)
    except (OSError, ValueError, runtime_paths.RuntimeConfigError) as exc:
        raise CliError(
            f"cannot establish verification lock: {exc}",
            code=EXIT_REFUSED,
            status="verification_lock_unavailable",
        ) from exc
    path = paths.verification_runs.parent / "public-verify.lock"
    try:
        with process_supervisor.exclusive_file_lock(
            path, label="public verification"
        ):
            yield
    except process_supervisor.ExclusiveLockUnavailable as exc:
        raise CliError(
            str(exc),
            code=EXIT_REFUSED,
            status="verification_in_progress",
        ) from exc
    except OSError as exc:
        raise CliError(
            f"cannot establish verification lock: {exc}",
            code=EXIT_REFUSED,
            status="verification_lock_unavailable",
        ) from exc


def _verify(project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    with _verification_run_lock(project):
        repository_start = cockpit.repository_fingerprint(project)
        repository_sha256 = repository_start.get("digest")
        if (
            not repository_start.get("available")
            or not isinstance(repository_sha256, str)
            or _VERIFY_SECRET.fullmatch(repository_sha256) is None
        ):
            raise CliError(
                "verification cannot bind evidence to the current repository state: "
                + str(repository_start.get("reason") or "fingerprint unavailable"),
                code=EXIT_REFUSED,
                status="repository_fingerprint_unavailable",
            )
        code, payload, human = _verify_once(
            project,
            args,
            repository_sha256=repository_sha256,
        )
        repository_end = cockpit.repository_fingerprint(project)
        repository_stable = bool(
            repository_end.get("available")
            and repository_end.get("digest") == repository_sha256
        )
        payload["repository_start"] = repository_start
        payload["repository_end"] = repository_end
        payload["repository_stable"] = repository_stable
        if code == EXIT_OK and not repository_stable:
            code = EXIT_FAILED
            payload["ok"] = False
            payload["status"] = "repository_changed_during_verification"
            human.append(
                "  evidence: repository content changed while verification was running"
            )
        payload["exit_code"] = code
        if os.environ.get("KIT_SELF_TEST") != "1":
            try:
                payload["verification_record"] = cockpit.record_verification(
                    project, payload
                )
            except (OSError, cockpit.CockpitError) as exc:
                payload["verification_record"] = {
                    "status": "insufficient",
                    "error": f"verification result could not be persisted: {exc}",
                }
                human.append(f"  evidence ledger: unavailable ({exc})")
                if code == EXIT_OK:
                    code = EXIT_FAILED
                    payload["ok"] = False
                    payload["status"] = "evidence_persistence_failed"
                    payload["exit_code"] = code
        return code, payload, human


def _verify_once(
    project: Path,
    args: argparse.Namespace,
    *,
    repository_sha256: str,
) -> tuple[int, dict[str, Any], list[str]]:
    nonce = uuid.uuid4().hex
    auth_key = secrets.token_hex(32)
    warning_start = native_engine.snapshot_native_warning(project)
    warning_snapshot = {
        "state": warning_start.state,
        "identity": warning_start.identity,
    }
    _prepare_gate_summary(project, nonce)
    if args.strict:
        if args.stage or args.fast or args.static:
            raise CliError(
                "strict verification is always complete; --stage, --fast and --static are unsupported",
                code=EXIT_REFUSED,
                status="strict_scope_refused",
            )
        environment, engine_selection = _verification_environment(project, args)
        retry_environment = _native_retry_environment(
            project,
            args,
            nonce=nonce,
            warning=warning_start,
        )
        environment = {
            **(environment or {}),
            **retry_environment,
            "KIT_VERIFY_NONCE": nonce,
            "KIT_VERIFY_AUTH_KEY": auth_key,
            "KIT_VERIFY_REPOSITORY_SHA256": repository_sha256,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        strict = _script(project, "tools", "strict_verify.py")
        result = _run_process(
            _isolated_python_command(strict, "--json"),
            cwd=project,
            timeout=10800,
            environment=_isolated_python_environment(environment),
            inherit_environment=False,
        )
        try:
            report = json.loads(result.stdout or "")
        except ValueError:
            report = None
        gate_summary = _read_gate_summary(
            project, nonce, auth_key, repository_sha256
        )
        valid = _strict_report_is_valid(
            project,
            report,
            result.returncode,
            gate_summary,
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
            "verification_nonce": nonce,
            "native_warning_start": warning_snapshot,
            "report": report,
            "process": _process_payload(result),
            "engine": (
                {
                    "path": str(engine_selection.path),
                    "source": engine_selection.source,
                }
                if engine_selection and engine_selection.path else None
            ),
            "gate_summary": gate_summary,
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
    for stage in args.stage:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", stage):
            raise CliError(
                f"invalid gate stage name: {stage!r}",
                code=EXIT_REFUSED,
                status="invalid_stage",
            )
    environment, engine_selection = _verification_environment(project, args)
    retry_environment = _native_retry_environment(
        project,
        args,
        nonce=nonce,
        warning=warning_start,
    )
    environment = {
        **(environment or {}),
        **retry_environment,
        "KIT_VERIFY_NONCE": nonce,
        "KIT_VERIFY_AUTH_KEY": auth_key,
        "KIT_VERIFY_REPOSITORY_SHA256": repository_sha256,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    check = _script(project, "check.py")
    command = _isolated_python_command(check)
    for stage in args.stage:
        command.extend(["--only", stage])
    if args.fast:
        command.append("--fast")
    if args.static:
        command.append("--static")
    streamed = not bool(getattr(args, "json_output", False))
    result = (
        _run_process_inherited(
            command,
            cwd=project,
            timeout=1800,
            environment=_isolated_python_environment(environment),
            inherit_environment=False,
        )
        if streamed
        else _run_process(
            command,
            cwd=project,
            timeout=1800,
            environment=_isolated_python_environment(environment),
            inherit_environment=False,
        )
    )
    output = (result.stdout or "") + (result.stderr or "")
    plain_output = _ANSI_ESCAPE.sub("", output)
    skips = re.findall(r"(?m)^\s*SKIP\s+(.+?)\s*$", plain_output)
    gate_summary = _read_gate_summary(
        project, nonce, auth_key, repository_sha256
    )
    gate_passed = result.returncode == 0 and gate_summary is not None and (
        streamed or "GATE PASSED" in plain_output
    ) and gate_summary.get("failed") is False
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
        "verification_nonce": nonce,
        "native_warning_start": warning_snapshot,
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
        "gate_summary": gate_summary,
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
    command = _isolated_unittest_command(
        "discover",
        "-s",
        str(CORE_ROOT / "tools" / "tests"),
    )
    environment = {
        "KIT_ENGINE_DISABLED": "1",
        "KIT_SELF_TEST": "1",
        # A strict verifier may itself carry a public gate nonce.  The
        # self-test's mocked/nested checks must never write that outer run's
        # evidence receipt.
        "KIT_VERIFY_NONCE": "",
        "KIT_VERIFY_AUTH_KEY": "",
        "KIT_VERIFY_REPOSITORY_SHA256": "",
        "KIT_NATIVE_RETRY_TOKEN": "",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    streamed = not bool(getattr(args, "json_output", False))
    result = (
        _run_process_inherited(
            command,
            cwd=CORE_ROOT,
            timeout=1800,
            environment=_isolated_python_environment(environment),
            inherit_environment=False,
        )
        if streamed
        else _run_process(
            command,
            cwd=CORE_ROOT,
            timeout=1800,
            environment=_isolated_python_environment(environment),
            inherit_environment=False,
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
        _isolated_python_command(_script(project, script_name), *arguments),
        cwd=project,
        timeout=timeout,
        environment=_isolated_python_environment(),
        inherit_environment=False,
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
        _isolated_python_command(
            _script(project, "tools", "schema.py"),
            "--describe",
            args.value,
        ),
        cwd=project,
        timeout=60,
        environment=_isolated_python_environment(),
        inherit_environment=False,
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
    if operation == "build":
        arguments = (
            "--build",
            "--engine",
            str(_selected_engine_path(project, operation="godot-docs build")),
        )
    elif operation == "show":
        arguments = (getattr(args, "value", ""),)
    else:
        arguments = ("--search", getattr(args, "value", ""))
    return _maintenance_process(
        project,
        label=f"godot-docs {operation}",
        script_name="tools/gddoc.py",
        arguments=arguments,
        timeout=360 if operation == "build" else 60,
    )


def _gdls(
    project: Path, args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    operation = args.gdls_command
    arguments: list[str] = []
    if operation == "start":
        arguments.extend([
            "--engine",
            str(_selected_engine_path(project, operation="gdls start")),
        ])
    arguments.append(operation)
    if operation in {"diagnose", "symbols", "refs"}:
        arguments.append(str(getattr(args, "value", "")))
    timeout = 120 if operation == "start" else 60
    result = _run_process(
        _isolated_python_command(
            _script(project, "tools", "gdls.py"), *arguments
        ),
        cwd=project,
        timeout=timeout,
        allow_child_breakaway=operation == "start",
        environment=_isolated_python_environment(),
        inherit_environment=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    try:
        parsed = json.loads(result.stdout or "")
    except ValueError:
        parsed = None
    receipt = (
        parsed
        if isinstance(parsed, dict)
        else _final_json_object(result.stdout or "")
    )
    ok = result.returncode == 0 and receipt is not None
    payload = {
        "ok": ok,
        "command": "gdls",
        "operation": operation,
        "status": (
            str(receipt.get("status") or "completed")
            if ok and receipt is not None
            else "failed"
        ),
        "project": str(project),
        "result": receipt,
        "process": _process_payload(result),
    }
    human = [f"gdls {operation}: {'completed' if ok else 'failed'}"]
    human.extend("  " + line for line in _output_tail(output, limit=8))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


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


def _plan(project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    plan_file = project / "plan.html"
    retro_file = project / "retro.html"
    snapshot_requested = getattr(args, "snapshot", None) is not None
    snapshot = ""
    if snapshot_requested:
        requested = str(getattr(args, "snapshot", "") or "").strip()
        snapshot = (
            cockpit.snapshot_label(requested)
            if requested
            else cockpit.snapshot_name(project)
        )
    result = cockpit.regenerate_views(project, snapshot=snapshot)
    processes = result.get("processes") if isinstance(result.get("processes"), dict) else {}
    output = "".join(
        str(process.get("stdout") or "") + str(process.get("stderr") or "")
        for process in processes.values()
        if isinstance(process, dict)
    )
    ok = bool(result.get("ok"))
    snapshot_file = project / "plan" / f"{snapshot}.html" if snapshot else None
    payload = {
        "ok": ok,
        "command": "plan",
        "status": "regenerated" if ok else "failed",
        "project": str(project),
        "path": str(plan_file),
        "paths": {"plan": str(plan_file), "retro": str(retro_file)},
        "processes": processes,
        "review_uri": plan_file.resolve().as_uri() if ok else None,
        "snapshot": str(snapshot_file) if snapshot_file and snapshot_file.is_file() else None,
    }
    human = [f"plan: {'regenerated' if ok else 'failed'}"]
    if ok:
        human.append(f"  {plan_file}")
        human.append(f"  {retro_file}")
        if snapshot_file and snapshot_file.is_file():
            human.append(f"  snapshot {snapshot_file}")
    else:
        human.extend("  " + line for line in _output_tail(output))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _serve(project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    operation = str(getattr(args, "serve_command", "start") or "start")
    regeneration: dict[str, Any] | None = None
    if operation in ("start", "open"):
        plan_args = argparse.Namespace(snapshot=None)
        plan_code, regeneration, plan_human = _plan(project, plan_args)
        if plan_code != EXIT_OK:
            payload = dict(regeneration)
            payload.update({"command": f"serve {operation}", "status": "render_failed"})
            return plan_code, payload, plan_human
    runtime = _source_controller_runtime(project)
    try:
        runtime.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliError(
            f"review storage is unavailable: {exc}",
            code=EXIT_REFUSED,
            status="runtime_unavailable",
        ) from exc
    tool, board_cwd, board_environment = _board_target(project, project, runtime)
    switch = {
        "start": "--ensure",
        "status": "--status",
        "open": "--open",
        "stop": "--stop",
    }[operation]
    result = _run_process(
        _isolated_python_command(tool, switch, "--json"),
        cwd=board_cwd,
        timeout=60,
        allow_child_breakaway=operation in ("start", "open"),
        environment=_isolated_python_environment(board_environment),
        inherit_environment=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    state = _final_json_object(result.stdout or "")
    url = str(state.get("url") or "") if isinstance(state, dict) else ""
    review_url = str(state.get("review_url") or "") if isinstance(state, dict) else ""
    ok = result.returncode == 0 and isinstance(state, dict) and bool(state.get("ok"))
    payload = {
        "ok": ok,
        "command": f"serve {operation}",
        "status": str(state.get("status") or "failed") if isinstance(state, dict) else "failed",
        "project": str(project),
        "url": url or None,
        "review_url": review_url or None,
        "board": state,
        "regeneration": regeneration,
        "process": _process_payload(result),
    }
    if ok and review_url:
        human = [f"serve: {payload['status']}", f"  {review_url}"]
    elif ok:
        human = [f"serve: {payload['status']}"]
    else:
        human = [f"serve {operation}: failed"]
    if not ok:
        human.extend("  " + line for line in _output_tail(output))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _retro_status(project: Path, _args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    tool = _script(project, "tools", "retro_due.py")
    result = _run_process(
        _isolated_python_command(tool, "--json"),
        cwd=project,
        timeout=30,
        environment=_isolated_python_environment(),
        inherit_environment=False,
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
        unarchived = state.get("unarchived", 0)
        threshold = state.get("threshold", 0)
        remaining = state.get("remaining", max(threshold - unarchived, 0))
        progress = state.get("progress")
        percentage = (
            f"; {round(progress * 100)}%"
            if isinstance(progress, (int, float)) and not isinstance(progress, bool)
            else ""
        )
        level = str(state.get("trigger_level") or "none")

        def _codes(key: str) -> list[str]:
            values = state.get(key)
            if not isinstance(values, list):
                return []
            return sorted({
                str(item.get("code"))
                for item in values
                if isinstance(item, dict) and item.get("code")
            })

        immediate = _codes("immediate_consequences")
        prompt = _codes("prompt_triggers")
        warnings = _codes("warnings")
        human = [
            f"retro: {payload['status']}",
            f"  progress: {unarchived}/{threshold} notes "
            f"({remaining} remaining{percentage})",
            f"  urgency: {level}",
        ]
        if immediate:
            human.append("  immediate codes: " + ", ".join(immediate))
        if prompt:
            human.append("  prompt codes: " + ", ".join(prompt))
        if warnings:
            human.append("  status warnings: " + ", ".join(warnings))
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
    if provider.automatic:
        blockers = providers.preflight(provider)
        if blockers:
            raise CliError(
                "retrospective provider is unavailable: " + "; ".join(blockers),
                code=EXIT_REFUSED,
                status="provider_unavailable",
            )
        if not args.confirm_spend:
            raise CliError(
                "retrospective provider run refused; repeat with --confirm-spend",
                code=EXIT_REFUSED,
                status="confirmation_required",
            )
    tool = _script(project, "tools", "retro.py")
    mode = "--sdk" if provider.kind == "copilot-sdk" else "--print"
    capture_window: list[str] = []
    if args.since:
        capture_window.extend(("--since", str(args.since)))
    if args.limit is not None:
        if args.limit < 1:
            raise CliError(
                "retrospective session --limit must be a positive integer",
                code=EXIT_REFUSED,
                status="invalid_capture_window",
            )
        capture_window.extend(("--limit", str(args.limit)))
    result = _run_process(
        _isolated_python_command(tool, mode, "--force", *capture_window),
        cwd=project,
        timeout=3600,
        allow_provider=provider.automatic and bool(args.confirm_spend),
        environment=_isolated_python_environment(),
        inherit_environment=False,
    )
    ok = result.returncode == 0
    successful_status = "completed" if provider.automatic else "evidence_prepared"
    payload = {
        "ok": ok,
        "command": "retro run",
        "status": successful_status if ok else "failed",
        "project": str(project),
        "confirmed": bool(args.confirm_spend),
        "capture_window": {
            "since": args.since or None,
            "limit": args.limit,
            "explicit": bool(args.since or args.limit is not None),
        },
        "provider": provider.status(),
        "process": _process_payload(result),
    }
    output = (result.stdout or "") + (result.stderr or "")
    human = [f"retro run: {successful_status if ok else 'failed'}"]
    human.extend("  " + line for line in _output_tail(output, limit=6))
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _resolve_retro_report(project: Path, raw_report: str | None) -> Path:
    """Resolve one exact findings file before ranking mutates its bytes."""
    retro_root = (project / "docs" / "retro").resolve()
    if raw_report:
        raw = Path(raw_report)
        report_path = (raw if raw.is_absolute() else project / raw).resolve()
    else:
        reports = sorted(retro_root.glob("*-findings.md"))
        if not reports:
            raise CliError(
                "no retrospective findings report exists under docs/retro",
                code=EXIT_REFUSED,
                status="report_unavailable",
            )
        report_path = reports[-1].resolve()
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
    if not report_path.is_file():
        raise CliError(
            "retrospective findings report does not exist",
            code=EXIT_REFUSED,
            status="report_unavailable",
        )
    return report_path


def _retro_publish(
        project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    report_path = _resolve_retro_report(project, args.report)
    command = _isolated_python_command(
        _script(project, "tools", "retro_rank.py"), report_path
    )
    # Resolve the exact report before ranking. The same path is rendered and
    # then bound back to its immutable evidence snapshot during completion.
    ranked = _run_process(
        command,
        cwd=project,
        timeout=180,
        environment=_isolated_python_environment(),
        inherit_environment=False,
    )
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
    if plan_code != EXIT_OK:
        payload = {
            "ok": False,
            "command": "retro publish",
            "status": "render_failed",
            "project": str(project),
            "report": str(report_path),
            "ranking": _process_payload(ranked),
            "plan": plan_payload,
        }
        human = ["retro publish: render failed"]
        human.extend("  " + line for line in _output_tail(rank_output, limit=5))
        return EXIT_FAILED, payload, human

    completion_tool = _script(project, "tools", "retro.py")
    completed = _run_process(
        _isolated_python_command(
            completion_tool,
            "--complete-published",
            str(report_path),
        ),
        cwd=project,
        timeout=180,
        environment=_isolated_python_environment(),
        inherit_environment=False,
    )
    completion_output = (completed.stdout or "") + (completed.stderr or "")
    ok = completed.returncode == 0
    payload = {
        "ok": ok,
        "command": "retro publish",
        "status": "published" if ok else "completion_failed",
        "project": str(project),
        "report": str(report_path),
        "ranking": _process_payload(ranked),
        "plan": plan_payload,
        "completion": _process_payload(completed),
    }
    human = [f"retro publish: {'published' if ok else 'completion failed'}"]
    human.extend("  " + line for line in _output_tail(rank_output, limit=5))
    if not ok:
        human.extend("  " + line for line in _output_tail(completion_output, limit=8))
    if ok:
        human.append(f"  {project / 'retro.html'}")
    return (EXIT_OK if ok else EXIT_FAILED), payload, human


def _release_path(project: Path) -> Path:
    del project
    return CORE_ROOT / "tools" / "release.py"


def _release(project: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    tool = _release_path(project)
    if not tool.is_file():
        raise CliError(
            "release tooling is not installed yet (expected tools/release.py)",
            code=EXIT_REFUSED,
            status="release_unavailable",
        )
    result = _run_process(
        _isolated_python_command(tool, args.release_command, args.archive),
        cwd=project,
        timeout=900,
        environment=_isolated_python_environment(),
        inherit_environment=False,
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


def _brownfield_scan(
    project: Path, args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    output = Path(str(args.output))
    if not output.is_absolute():
        raise CliError(
            "brownfield scan output must be an absolute path",
            code=EXIT_REFUSED,
            status="scan_output_invalid",
        )
    namespace = runpy.run_path(
        str(_script(project, "check.py")),
        run_name="_agent_kit_brownfield_scan",
    )
    scan = namespace.get("run_brownfield_scan")
    if not callable(scan):
        raise CliError(
            "brownfield scan is unavailable in this kit",
            code=EXIT_REFUSED,
            status="scan_unavailable",
        )
    result = int(scan(str(output)))
    ok = result == 0
    payload = {
        "ok": ok,
        "command": "brownfield-scan",
        "status": "completed" if ok else "failed",
        "project": str(project),
        "output": str(output),
    }
    return (EXIT_OK if ok else EXIT_FAILED), payload, [
        f"brownfield scan: {'completed' if ok else 'failed'}"
    ]


def _explicit_path(value: str, label: str) -> Path:
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else Path.cwd() / raw
    try:
        return candidate.absolute()
    except OSError as exc:
        raise CliError(
            f"cannot resolve {label} {candidate}: {exc}",
            code=EXIT_REFUSED,
            status=f"{label.replace(' ', '_')}_unavailable",
        ) from exc


def _verified_release_source(args: argparse.Namespace) -> Path:
    supplied = str(getattr(args, "release_source", "") or "").strip()
    if supplied:
        return _explicit_path(supplied, "release")
    try:
        release_tool.read_verified_directory(CORE_ROOT)
    except release_tool.ReleaseError as exc:
        raise CliError(
            "this is a source checkout, not a built release; build a release archive "
            "and pass it with --release",
            code=EXIT_REFUSED,
            status="release_required",
        ) from exc
    return CORE_ROOT


def _source_controller_runtime(project: Path) -> Path:
    if project == CORE_ROOT:
        try:
            report, _members = release_tool.read_verified_directory(CORE_ROOT)
        except release_tool.ReleaseError:
            pass
        else:
            identity = str(report.get("archive_sha256") or "")
            if _VERIFY_SECRET.fullmatch(identity) is None:
                raise CliError(
                    "the extracted release has no valid identity",
                    code=EXIT_REFUSED,
                    status="release_invalid",
                )
            runtime = _user_controller_root() / identity / "runtime"
            try:
                runtime.relative_to(CORE_ROOT)
            except ValueError:
                return runtime
            raise CliError(
                "user-local kit-change storage overlaps the extracted release",
                code=EXIT_REFUSED,
                status="runtime_unavailable",
            )
    try:
        return runtime_paths.resolve(project, create=True).runtime
    except (OSError, ValueError, runtime_paths.RuntimeConfigError) as exc:
        raise CliError(
            f"private kit-change storage is unavailable: {exc}",
            code=EXIT_REFUSED,
            status="runtime_unavailable",
        ) from exc


def _user_controller_root() -> Path:
    try:
        if os.name == "nt":
            configured = str(os.environ.get("LOCALAPPDATA") or "").strip()
            base = (
                Path(configured).expanduser()
                if configured
                else Path.home() / "AppData" / "Local"
            )
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            configured = str(os.environ.get("XDG_STATE_HOME") or "").strip()
            base = (
                Path(configured).expanduser()
                if configured
                else Path.home() / ".local" / "state"
            )
        if not base.is_absolute():
            raise RuntimeError("configured user-local directory is not absolute")
        absolute = base.absolute()
    except (OSError, RuntimeError) as exc:
        raise CliError(
            f"user-local kit-change storage is unavailable: {exc}",
            code=EXIT_REFUSED,
            status="runtime_unavailable",
        ) from exc
    if not absolute.is_absolute():
        raise CliError(
            "user-local kit-change storage must be an absolute path",
            code=EXIT_REFUSED,
            status="runtime_unavailable",
        )
    return absolute / "GodotAgentKit" / "controller"


def _kit_change_runtime(project: Path, target: Path, mode: str) -> Path:
    del target, mode
    return _source_controller_runtime(project)


def _board_target(
    project: Path,
    target: Path,
    runtime: Path,
) -> tuple[Path, Path, dict[str, str] | None]:
    del target
    # The review belongs to the initiating incoming kit. The target's active
    # core may be an older release which cannot understand this session.
    try:
        configured = runtime_paths.resolve(project, create=False).runtime
        environment = {
            runtime_paths.CONTROLLER_RUNTIME_ENV: (
                str(runtime) if runtime != configured else ""
            ),
        }
        selected = runtime_paths.resolve_controller(
            project,
            environment=environment,
        ).runtime
    except (OSError, ValueError, runtime_paths.RuntimeConfigError) as exc:
        raise CliError(
            f"configured review storage is unavailable: {exc}",
            code=EXIT_REFUSED,
            status="runtime_unavailable",
        ) from exc
    if selected != runtime:
        raise CliError(
            "controller and review storage do not match",
            code=EXIT_REFUSED,
            status="runtime_unavailable",
        )
    return _script(project, "tools", "board.py"), project, environment


def _exact_kit_change_url(value: object, session_id: str) -> str:
    rendered = str(value or "")
    try:
        parsed = urllib.parse.urlsplit(rendered)
        queries = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise CliError(
            "the review server returned an invalid URL",
            status="board_failed",
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/kit-change.html"
        or parsed.fragment
        or queries != {"session": [session_id]}
        or rendered
        != f"http://127.0.0.1:{port}/kit-change.html?session={session_id}"
    ):
        raise CliError(
            "the review server did not return the exact local kit-change page",
            status="board_failed",
        )
    return rendered


def _register_kit_change_board(
    project: Path,
    target: Path,
    runtime: Path,
    session_id: str,
) -> tuple[str, dict[str, Any], subprocess.CompletedProcess[str]]:
    tool, cwd, environment = _board_target(project, target, runtime)
    result = _run_process(
        _isolated_python_command(
            tool,
            "--ensure",
            "--kit-change-session",
            session_id,
            "--json",
        ),
        cwd=cwd,
        timeout=60,
        allow_child_breakaway=True,
        environment=_isolated_python_environment(environment),
        inherit_environment=False,
    )
    board = _final_json_object(result.stdout or "")
    if result.returncode != 0 or not isinstance(board, dict) or not board.get("ok"):
        output = (result.stdout or "") + (result.stderr or "")
        detail = "; ".join(_output_tail(output, limit=20)) or "review server failed"
        raise CliError(detail, status="board_failed")
    review_url = _exact_kit_change_url(board.get("review_url"), session_id)
    return review_url, board, result


def _kit_change_prepare(
    project: Path, args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    mode = str(args.lifecycle_mode)
    target = _explicit_path(str(args.target), "target project")
    release_source = _verified_release_source(args)
    runtime = _kit_change_runtime(project, target, mode)
    try:
        prepared = kit_change_controller.prepare(
            runtime,
            target,
            release_source,
            mode,
            game_root=getattr(args, "game_root", None),
        )
        session_id = prepared["session_id"]
        review_url, board, process = _register_kit_change_board(
            project, target, runtime, session_id
        )
        prepared = kit_change_controller.status(
            runtime, session_id, plan_url=review_url
        )
    except kit_change_controller.KitChangeControllerError as exc:
        raise CliError(
            exc.detail,
            code=EXIT_REFUSED,
            status=exc.code.replace("-", "_"),
        ) from exc
    state = prepared["kit_change"]
    status = "needs_decision" if state["status"] == "blocked" else "ready"
    try:
        runtime.relative_to(target)
        review_storage = "target_private_runtime_only"
    except ValueError:
        review_storage = "initiating_kit_private_runtime"
    payload = {
        "ok": True,
        "command": mode,
        "status": status,
        "target": str(target),
        "session_id": session_id,
        "review_url": review_url,
        "review_storage": review_storage,
        "project_files_changed": False,
        "kit_change": state,
        "board": board,
        "process": _process_summary(process),
    }
    human = [f"{mode}: {status.replace('_', ' ')}", f"  {review_url}"]
    return EXIT_OK, payload, human


def _kit_change_recover(
    project: Path, args: argparse.Namespace
) -> tuple[int, dict[str, Any], list[str]]:
    runtime = _source_controller_runtime(project)
    try:
        recovered = kit_change_controller.recover(runtime, str(args.session_id))
    except kit_change_controller.KitChangeControllerError as exc:
        raise CliError(
            exc.detail,
            code=EXIT_REFUSED,
            status=exc.code.replace("-", "_"),
        ) from exc
    state = recovered["kit_change"]
    ok = state["status"] in {
        "ready", "blocked", "complete", "adoption_required", "restored"
    }
    payload = {
        "ok": ok,
        "command": "recover",
        "status": state["status"],
        "session_id": recovered["session_id"],
        "kit_change": state,
    }
    human = [f"recover: {state['status'].replace('_', ' ')}"]
    if state.get("plan_url"):
        human.append(f"  {state['plan_url']}")
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
    "gdls": _gdls,
    "friction": _friction,
    "plan": _plan,
    "serve": _serve,
    "retro_status": _retro_status,
    "retro_run": _retro_run,
    "retro_publish": _retro_publish,
    "release": _release,
    "brownfield_scan": _brownfield_scan,
    "kit_change_prepare": _kit_change_prepare,
    "kit_change_recover": _kit_change_recover,
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
    "brownfield_scan": "brownfield scan",
    "kit_change_prepare": "kit change",
    "kit_change_recover": "recover",
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

    if raw_argv[:1] == ["__brownfield-scan"]:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("internal_command")
        parser.add_argument("output")
        _add_common_options(parser)
        args = parser.parse_args(raw_argv)
        args.action = "brownfield_scan"
    else:
        parser = build_parser()
        args = parser.parse_args(raw_argv)
    json_output = bool(getattr(args, "json_output", False))
    project: Path | None = None
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
