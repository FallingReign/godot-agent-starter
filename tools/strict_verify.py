#!/usr/bin/env python3
"""Production-shaped, fail-closed verification with durable local evidence.

This is deliberately an orchestrator, not a setup tool.  It never installs,
downloads, formats, imports on its own, changes Git state, or calls a model.
The commands it verifies run one at a time so two verifier-owned Godot runs can
never overlap.  Complete stdout and stderr are retained beneath the configured
private runtime root.

Exit codes:
    0  every required proof passed
    1  a proof ran and failed, timed out, was malformed, or contained a skip
    3  a prerequisite or required release/legal decision is unavailable
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import runtime_paths  # noqa: E402

SCHEMA = 1
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 3
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
GATE_SKIP = re.compile(r"(?m)^\s*SKIP\s{1,}[^\r\n]+$")
UNITTEST_RAN = re.compile(r"(?m)^Ran\s+(\d+)\s+tests?\s+in\s+")
UNITTEST_SKIP = re.compile(
    r"(?im)(?:\bskipped\s*=\s*[1-9]\d*|\.\.\.\s+skipped\s+['\"]|^skipped\s+)"
)
BROWSER_SUMMARY = re.compile(r"(?m)^\s*(\d+)/(\d+) browser checks passed\s*$")
BROWSER_SKIP = re.compile(r"(?im)^.*browser (?:check )?skipped.*$")
LEGAL_FILES = (
    "LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md",
)
MAINTAINER_FIXTURE_PATH = Path("src/.kit-maintainer-fixture")
MAINTAINER_FIXTURE_CONTENT = "portable-agent-kit-maintainer-fixture-v1\n"
MAINTAINER_GATE_SKIPS = frozenset(
    {
        "SKIP shape (absent)",
        "SKIP design",
        "SKIP conformance",
    }
)


@dataclass(frozen=True)
class ProcessOutcome:
    """Complete observable result of one child process."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    launch_error: str = ""


Runner = Callable[[Sequence[str], Path, int, Mapping[str, str]], ProcessOutcome]


class StrictVerifyError(RuntimeError):
    """The verifier cannot establish its trusted local evidence boundary."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _default_runner(
    command: Sequence[str], cwd: Path, timeout: int, environment: Mapping[str, str]
) -> ProcessOutcome:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(part) for part in command],
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return ProcessOutcome(
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
            time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        return ProcessOutcome(
            None,
            _text(exc.stdout),
            _text(exc.stderr),
            time.monotonic() - started,
            timed_out=True,
        )
    except OSError as exc:
        return ProcessOutcome(
            None,
            "",
            "",
            time.monotonic() - started,
            launch_error=f"{type(exc).__name__}: {exc}",
        )


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_text(path: Path, content: str) -> None:
    _atomic_bytes(path, content.encode("utf-8"))


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_bytes(path, encoded)


@contextlib.contextmanager
def _release_workspace(run_directory: Path) -> Iterator[Path]:
    """Create bounded release scratch inside the private verification run."""
    workspace = run_directory / "release-workspace"
    workspace.mkdir(parents=False, exist_ok=False)
    try:
        yield workspace
    finally:
        if workspace.parent == run_directory and workspace.name == "release-workspace":
            if workspace.is_symlink():
                workspace.unlink(missing_ok=True)
            else:
                shutil.rmtree(workspace, ignore_errors=True)


def _relative(path: Path, root: Path) -> str:
    return path.resolve(strict=False).relative_to(root.resolve()).as_posix()


def _private_verification_root(root: Path) -> Path:
    try:
        configured = runtime_paths.resolve(root).runtime
    except (runtime_paths.RuntimeConfigError, OSError, ValueError) as exc:
        raise StrictVerifyError(f"cannot resolve private runtime root: {exc}") from exc
    configured.parent.mkdir(parents=True, exist_ok=True)
    configured.mkdir(parents=True, exist_ok=True)
    canonical_root = root.resolve()
    canonical_runtime = configured.resolve()
    if canonical_runtime == canonical_root or not canonical_runtime.is_relative_to(
        canonical_root
    ):
        raise StrictVerifyError("configured runtime root escapes the project")
    verification = canonical_runtime / "verification"
    verification.mkdir(parents=True, exist_ok=True)
    if verification.resolve().parent != canonical_runtime:
        raise StrictVerifyError("verification directory escapes the private runtime root")
    return verification


@contextlib.contextmanager
def _exclusive_run(lock_path: Path):
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StrictVerifyError(
            f"another strict verification may be running ({lock_path})"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as lock:
            json.dump({"pid": os.getpid(), "started_at": _utc_now()}, lock, sort_keys=True)
            lock.write("\n")
            lock.flush()
            os.fsync(lock.fileno())
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _json_object(output: str) -> dict[str, Any] | None:
    try:
        value = json.loads(output)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _classify_doctor(outcome: ProcessOutcome) -> tuple[str, str]:
    if outcome.timed_out:
        return "failed", "doctor timed out"
    if outcome.launch_error:
        return "blocked", f"doctor could not start: {outcome.launch_error}"
    payload = _json_object(outcome.stdout)
    if payload is None:
        return "failed", "doctor output is not one JSON object"
    blocking = payload.get("blocking")
    bootstrap = payload.get("bootstrap")
    complete = isinstance(bootstrap, dict) and bootstrap.get("complete") is True
    if (
        outcome.returncode != 0
        or payload.get("exit_code") != 0
        or payload.get("ok") is not True
        or payload.get("status") != "ready"
        or not complete
    ):
        detail = "; ".join(str(item) for item in blocking) if isinstance(blocking, list) else ""
        return "blocked", detail or "doctor reports an incomplete prerequisite"
    if not isinstance(blocking, list) or blocking:
        return "failed", "doctor readiness fields are malformed or contradictory"
    results = bootstrap.get("results")
    if not isinstance(results, list) or not results:
        return "failed", "doctor bootstrap results are malformed"
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not item.get("name")
        or not isinstance(item.get("state"), str)
        or not isinstance(item.get("advisory"), bool)
        for item in results
    ):
        return "failed", "doctor bootstrap result entries are malformed"
    unsupported = [
        str(item.get("name", "unnamed"))
        for item in results
        if not item.get("advisory", False) and item.get("state") != "OK"
    ]
    if unsupported:
        return "blocked", "missing required dependency: " + ", ".join(unsupported)
    return "passed", "read-only environment readiness is complete"


def _maintainer_fixture_present(root: Path) -> bool:
    """Recognise the release-excluded fixture used to verify kit source itself."""
    marker = root / MAINTAINER_FIXTURE_PATH
    try:
        if not marker.is_file() or marker.is_symlink():
            return False
        if not marker.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            return False
        return marker.read_text(encoding="utf-8") == MAINTAINER_FIXTURE_CONTENT
    except (OSError, UnicodeError, ValueError):
        return False


def _classify_gate(
    outcome: ProcessOutcome,
    allowed_skips: frozenset[str] = frozenset(),
) -> tuple[str, str]:
    if outcome.timed_out:
        return "failed", "verification gate timed out"
    if outcome.launch_error:
        return "blocked", f"verification gate could not start: {outcome.launch_error}"
    output = ANSI_ESCAPE.sub("", outcome.stdout + "\n" + outcome.stderr)
    skips = [re.sub(r"\s+", " ", line.strip()) for line in GATE_SKIP.findall(output)]
    unsupported = [line for line in skips if line not in allowed_skips]
    if unsupported:
        return "failed", "strict verification rejects gate SKIP: " + "; ".join(
            unsupported
        )
    if outcome.returncode != 0:
        return "failed", f"verification gate exited {outcome.returncode}"
    if "GATE PASSED" not in output or "GATE FAILED" in output:
        return "failed", "verification gate output is missing an unambiguous pass marker"
    if skips:
        return (
            "passed",
            "gate passed; canonical maintainer project stages are not applicable: "
            + "; ".join(skips),
        )
    return "passed", "gate passed with no skipped stage"


def _classify_unittest(outcome: ProcessOutcome) -> tuple[str, str]:
    if outcome.timed_out:
        return "failed", "unit-test discovery timed out"
    if outcome.launch_error:
        return "blocked", f"unit-test discovery could not start: {outcome.launch_error}"
    output = ANSI_ESCAPE.sub("", outcome.stdout + "\n" + outcome.stderr)
    if UNITTEST_SKIP.search(output):
        return "failed", "strict verification rejects skipped unit tests"
    matches = UNITTEST_RAN.findall(output)
    if len(matches) != 1 or int(matches[0]) < 1:
        return "failed", "unit-test output has no valid non-zero discovery summary"
    if outcome.returncode != 0 or re.search(r"(?m)^FAILED\s*\(", output):
        return "failed", f"unit tests exited {outcome.returncode}"
    if re.search(r"(?m)^OK(?:\s|$)", output) is None:
        return "failed", "unit-test output is missing its OK marker"
    return "passed", f"{matches[0]} unit tests passed without skips"


def _classify_browser(outcome: ProcessOutcome) -> tuple[str, str]:
    if outcome.timed_out:
        return "failed", "browser check timed out"
    if outcome.launch_error:
        return "blocked", f"browser check could not start: {outcome.launch_error}"
    output = ANSI_ESCAPE.sub("", outcome.stdout + "\n" + outcome.stderr)
    if BROWSER_SKIP.search(output):
        return "blocked", "a Chromium-based browser is required; browser check skipped"
    matches = BROWSER_SUMMARY.findall(output)
    if len(matches) != 1:
        return "failed", "browser output has no unique check summary"
    passed, total = (int(item) for item in matches[0])
    if outcome.returncode != 0 or total < 1 or passed != total or "  FAIL " in output:
        return "failed", f"browser checks passed {passed}/{total} (exit {outcome.returncode})"
    return "passed", f"{passed}/{total} real-browser checks passed"


def _classify_release(outcome: ProcessOutcome) -> tuple[str, str]:
    if outcome.timed_out:
        return "failed", "release command timed out"
    if outcome.launch_error:
        return "blocked", f"release command could not start: {outcome.launch_error}"
    payload = _json_object(outcome.stdout)
    if outcome.returncode != 0:
        return "failed", f"release command exited {outcome.returncode}"
    if payload is None or payload.get("ok") is not True:
        return "failed", "release command output is not a successful JSON report"
    return "passed", "release command returned a verified JSON report"


def _write_stage_logs(
    root: Path, run_directory: Path, name: str, stdout: str, stderr: str
) -> tuple[str, str]:
    stdout_path = run_directory / f"{name}.stdout.log"
    stderr_path = run_directory / f"{name}.stderr.log"
    _atomic_text(stdout_path, stdout)
    _atomic_text(stderr_path, stderr)
    return _relative(stdout_path, root), _relative(stderr_path, root)


def _run_stage(
    *,
    name: str,
    command: Sequence[str],
    timeout: int,
    classifier: Callable[[ProcessOutcome], tuple[str, str]],
    root: Path,
    run_directory: Path,
    runner: Runner,
    environment: Mapping[str, str],
) -> tuple[dict[str, Any], ProcessOutcome]:
    outcome = runner(command, root, timeout, environment)
    stdout_log, stderr_log = _write_stage_logs(
        root, run_directory, name, outcome.stdout, outcome.stderr
    )
    status, reason = classifier(outcome)
    stage = {
        "name": name,
        "status": status,
        "reason": reason,
        "command": [str(part) for part in command],
        "timeout_seconds": timeout,
        "duration_seconds": round(outcome.duration_seconds, 3),
        "exit_code": outcome.returncode,
        "timed_out": outcome.timed_out,
        "stdout_log": stdout_log,
        "stderr_log": stderr_log,
    }
    return stage, outcome


def _release_metadata(root: Path) -> list[str]:
    blockers: list[str] = []
    version = root / "VERSION"
    try:
        version_present = version.is_file() and bool(
            version.read_text(encoding="utf-8").strip()
        )
    except (OSError, UnicodeError):
        version_present = False
    if not version_present:
        blockers.append("required VERSION metadata is absent")
    legal = [name for name in LEGAL_FILES if (root / name).is_file()]
    if not legal:
        blockers.append("required approved LICENSE or COPYING metadata is absent")
    else:
        try:
            legal_valid = all(
                bool((root / name).read_text(encoding="utf-8").strip()) for name in legal
            )
        except (OSError, UnicodeError):
            legal_valid = False
        if not legal_valid:
            blockers.append("present legal metadata is empty or unreadable")
    if not (root / "tools" / "release.py").is_file():
        blockers.append("tools/release.py is unavailable")
    return blockers


def _overall_status(stages: Sequence[Mapping[str, Any]]) -> tuple[str, int]:
    statuses = {str(stage.get("status")) for stage in stages}
    if "failed" in statuses:
        return "failed", EXIT_FAILED
    if "blocked" in statuses or "not_run" in statuses:
        return "blocked", EXIT_BLOCKED
    if statuses == {"passed"}:
        return "passed", EXIT_OK
    return "failed", EXIT_FAILED


def run_strict(root: Path = ROOT, *, runner: Runner | None = None) -> tuple[dict[str, Any], int]:
    """Run every strict proof and atomically retain a machine-readable report."""
    root = root.resolve(strict=True)
    verification = _private_verification_root(root)
    report_path = verification / "strict-report.json"
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + f"-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    run_directory = verification / "runs" / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    test_scratch = run_directory / "test-scratch"
    test_scratch.mkdir()
    selected_runner = runner or _default_runner
    environment = dict(os.environ)
    environment.update(
        {
            "CI": environment.get("CI", "1"),
            "KIT_STRICT_VERIFY": "1",
            "KIT_TEST_TMPDIR": str(test_scratch),
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    started_at = _utc_now()
    stages: list[dict[str, Any]] = []
    abort_remaining = ""
    allowed_gate_skips = (
        MAINTAINER_GATE_SKIPS if _maintainer_fixture_present(root) else frozenset()
    )

    with _exclusive_run(verification / "strict.lock"):
        commands = (
            (
                "doctor",
                [sys.executable, str(root / "kit.py"), "doctor", "--json"],
                60,
                _classify_doctor,
            ),
            (
                "gate",
                [sys.executable, str(root / "check.py")],
                1800,
                lambda outcome: _classify_gate(outcome, allowed_gate_skips),
            ),
            (
                "unit-tests",
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tools/tests",
                    "-v",
                ],
                1200,
                _classify_unittest,
            ),
            (
                "browser-check",
                [sys.executable, str(root / "tools" / "tests" / "browser_check.py")],
                900,
                _classify_browser,
            ),
        )
        for name, command, timeout, classifier in commands:
            if abort_remaining:
                stages.append(
                    {
                        "name": name,
                        "status": "not_run",
                        "reason": abort_remaining,
                        "command": command,
                    }
                )
                continue
            stage, outcome = _run_stage(
                name=name,
                command=command,
                timeout=timeout,
                classifier=classifier,
                root=root,
                run_directory=run_directory,
                runner=selected_runner,
                environment=environment,
            )
            stages.append(stage)
            if outcome.timed_out:
                abort_remaining = (
                    "an earlier timeout may have left a child process; overlap refused"
                )
            if name == "doctor" and stage["status"] != "passed":
                abort_remaining = (
                    "doctor did not prove readiness; later commands were not started"
                )
                stage["reason"] += "; later commands were not started"

        metadata_blockers = _release_metadata(root)
        if abort_remaining:
            stages.append(
                {
                    "name": "release-reproducibility",
                    "status": "not_run",
                    "reason": abort_remaining,
                    "command": [],
                }
            )
        elif metadata_blockers:
            reason = "; ".join(metadata_blockers)
            stdout_log, stderr_log = _write_stage_logs(
                root, run_directory, "release-metadata", reason + "\n", ""
            )
            stages.append(
                {
                    "name": "release-metadata",
                    "status": "blocked",
                    "reason": reason,
                    "command": [],
                    "stdout_log": stdout_log,
                    "stderr_log": stderr_log,
                }
            )
        else:
            with _release_workspace(run_directory) as release_workspace:
                release_one = release_workspace / "kit-one.zip"
                release_two = release_workspace / "kit-two.zip"
                release_tool = root / "tools" / "release.py"
                release_commands = (
                    (
                        "release-build-1",
                        [sys.executable, str(release_tool), "build", str(release_one)],
                    ),
                    (
                        "release-build-2",
                        [sys.executable, str(release_tool), "build", str(release_two)],
                    ),
                    (
                        "release-verify-1",
                        [sys.executable, str(release_tool), "verify", str(release_one)],
                    ),
                    (
                        "release-verify-2",
                        [sys.executable, str(release_tool), "verify", str(release_two)],
                    ),
                    (
                        "release-smoke",
                        [
                            sys.executable,
                            str(release_tool),
                            "smoke",
                            str(release_one),
                            str(release_workspace / "smoke"),
                        ],
                    ),
                )
                for name, command in release_commands:
                    stage, outcome = _run_stage(
                        name=name,
                        command=command,
                        timeout=900,
                        classifier=_classify_release,
                        root=root,
                        run_directory=run_directory,
                        runner=selected_runner,
                        environment=environment,
                    )
                    stages.append(stage)
                    if outcome.timed_out:
                        abort_remaining = (
                            "a release timeout may have left a child process; overlap refused"
                        )
                        break
                if abort_remaining:
                    stages.append(
                        {
                            "name": "release-reproducibility",
                            "status": "not_run",
                            "reason": "release command timed out; byte comparison refused",
                            "command": [],
                        }
                    )
                elif not release_one.is_file() or not release_two.is_file():
                    stages.append(
                        {
                            "name": "release-reproducibility",
                            "status": "failed",
                            "reason": "release command did not produce both archives",
                            "command": [],
                        }
                    )
                else:
                    first = release_one.read_bytes()
                    second = release_two.read_bytes()
                    first_hash = hashlib.sha256(first).hexdigest()
                    second_hash = hashlib.sha256(second).hexdigest()
                    identical = first == second
                    detail = json.dumps(
                        {
                            "first_bytes": len(first),
                            "first_sha256": first_hash,
                            "second_bytes": len(second),
                            "second_sha256": second_hash,
                            "byte_identical": identical,
                        },
                        indent=2,
                        sort_keys=True,
                    ) + "\n"
                    stdout_log, stderr_log = _write_stage_logs(
                        root, run_directory, "release-reproducibility", detail, ""
                    )
                    stages.append(
                        {
                            "name": "release-reproducibility",
                            "status": "passed" if identical else "failed",
                            "reason": (
                                "two independent release builds are byte-identical"
                                if identical
                                else "two independent release builds differ byte-for-byte"
                            ),
                            "command": [],
                            "archive_sha256": first_hash if identical else None,
                            "stdout_log": stdout_log,
                            "stderr_log": stderr_log,
                        }
                    )

        status, exit_code = _overall_status(stages)
        report: dict[str, Any] = {
            "schema": SCHEMA,
            "command": "strict-verify",
            "ok": exit_code == EXIT_OK,
            "status": status,
            "exit_code": exit_code,
            "project": str(root),
            "started_at": started_at,
            "finished_at": _utc_now(),
            "run_id": run_id,
            "logs_directory": _relative(run_directory, root),
            "report_path": _relative(report_path, root),
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "python_executable": sys.executable,
            },
            "stages": stages,
        }
        _atomic_json(run_directory / "report.json", report)
        _atomic_json(report_path, report)
        return report, exit_code


def _blocked_without_report(root: Path, message: str) -> tuple[dict[str, Any], int]:
    payload = {
        "schema": SCHEMA,
        "command": "strict-verify",
        "ok": False,
        "status": "blocked",
        "exit_code": EXIT_BLOCKED,
        "project": str(root),
        "error": message,
    }
    return payload, EXIT_BLOCKED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true", dest="json_output")
    arguments = parser.parse_args(argv)
    try:
        report, exit_code = run_strict(arguments.root)
    except (OSError, StrictVerifyError) as exc:
        report, exit_code = _blocked_without_report(arguments.root, str(exc))
    if arguments.json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"strict verify: {report['status']}")
        if report.get("report_path"):
            print(f"report: {report['report_path']}")
        if report.get("error"):
            print(f"blocked: {report['error']}")
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("strict verify: interrupted", file=sys.stderr)
        raise SystemExit(130)
