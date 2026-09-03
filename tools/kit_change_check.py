#!/usr/bin/env python3
"""Offline proof for one newly installed managed kit.

The lifecycle controller owns rollback.  This module proves the selected core,
runs only engine-disabled checks, creates one exact brownfield baseline, and
returns enough evidence for the controller to decide whether to keep or restore
the install.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import brownfield
import kit_change
import managed_launcher
import process_supervisor
import project_context
import release


MAX_DETAIL_CHARS = 2048
MAX_SCAN_BYTES = brownfield.MAX_DOCUMENT_BYTES
MAX_RECEIPT_OUTPUT_CHARS = 2 * 1024 * 1024
CHECK_ROOT = "kit-change-checks"
REQUIRED_LAUNCHERS = (".agent-kit/launcher.py", "kit", "kit.cmd")
STATIC_STAGES = (
    "integrity",
    "skills",
    "schema",
    "shape",
    "design",
    "conformance",
    "format",
    "lint",
    "sanitise",
    "grep",
    "types",
    "arch",
    "tests",
    "assets",
)
_CHECK_FIELDS = {
    "kit_ok",
    "project_ok",
    "existing_issues",
    "detail",
    "baseline_sha256",
}
_PUBLIC_ISSUE_FIELDS = ("stage", "code", "path", "line", "message_sha256")


class KitChangeCheckError(RuntimeError):
    """The installed kit could not produce trustworthy offline proof."""


ProcessRunner = Callable[..., Any]
BaselineReady = Callable[[bytes], Mapping[str, Any]]


def _failpoint(_name: str) -> None:
    """Test-only crash boundary around the baseline handoff."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _clean_environment(
    installation: managed_launcher.Installation,
) -> dict[str, str]:
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON")
    }
    base.pop(managed_launcher.PROJECT_ROOT_ENV, None)
    base.pop(managed_launcher.CORE_ROOT_ENV, None)
    environment = managed_launcher.bound_environment(installation, base)
    environment.update({
        "KIT_ENGINE_DISABLED": "1",
        "KIT_LIFECYCLE_CHECK": "1",
        "KIT_NATIVE_RETRY_TOKEN": "",
        "KIT_PYTHON": str(Path(sys.executable).resolve()),
        "KIT_VERIFY_AUTH_KEY": "",
        "KIT_VERIFY_NONCE": "",
        "KIT_VERIFY_REPOSITORY_SHA256": "",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return environment


def _launcher_command(
    installation: managed_launcher.Installation,
    *arguments: str,
) -> list[str]:
    launcher = kit_change._target(  # noqa: SLF001 - authenticated active core
        installation.core_root, "tools/managed_launcher.py"
    )
    return process_supervisor.isolated_python_script_command(
        Path(sys.executable).resolve(),
        launcher,
        installation.core_root,
        *arguments,
    )


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in process_supervisor.isolated_python_environment().items()
        if not key.upper().startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return environment


def _git_command(executable: str, root: Path, *arguments: str) -> list[str]:
    return [
        executable,
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "diff.external=",
        "-c",
        "diff.trustExitCode=false",
        "-c",
        f"core.attributesFile={os.devnull}",
        "-C",
        str(root),
        *arguments,
    ]


def _initialise_self_test_repository(
    core: Path,
    *,
    excluded_roots: Sequence[Path],
    runner: ProcessRunner,
) -> None:
    environment = _git_environment()
    try:
        executable = process_supervisor.resolve_ordinary_executable(
            "git",
            environment=environment,
            excluded_roots=excluded_roots,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise KitChangeCheckError(
            "trusted Git is unavailable for the disposable self-test"
        ) from exc
    commands = (
        ("initialise", ("init", "--quiet")),
        ("stage", ("add", "--all", "--")),
        (
            "commit",
            (
                "-c",
                "user.name=Agent Kit Self-Test",
                "-c",
                "user.email=agent-kit-self-test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                "verified release fixture",
            ),
        ),
    )
    for label, arguments in commands:
        outcome = _run_process(
            _git_command(executable, core, *arguments),
            cwd=core,
            environment=environment,
            timeout=60,
            runner=runner,
        )
        if getattr(outcome, "returncode", None) != 0:
            raise KitChangeCheckError(
                f"disposable self-test Git {label} failed"
            )


def _self_test_command(
    installation: managed_launcher.Installation,
    scratch: Path,
    *,
    runner: ProcessRunner,
) -> tuple[list[str], dict[str, str]]:
    copied_core = scratch / "c"
    shutil.copytree(installation.core_root, copied_core)
    report, _members = release.read_verified_directory(copied_core)
    if str(report.get("archive_sha256") or "") != installation.release_sha256:
        raise KitChangeCheckError("self-test copy selected a different release")
    # Releases deliberately exclude game content.  The disposable placeholder
    # supplies only the one file a source-context regression test inspects; no
    # project or game bytes are copied into the self-test sandbox.
    (copied_core / "src").mkdir()
    (copied_core / "src" / "project.godot").write_bytes(b"[application]\n")
    _initialise_self_test_repository(
        copied_core,
        excluded_roots=(
            installation.project_root,
            installation.core_root,
            scratch,
        ),
        runner=runner,
    )
    environment = process_supervisor.isolated_python_environment()
    environment = {
        key: value
        for key, value in environment.items()
        if not key.upper().startswith("GIT_")
    }
    environment.pop(managed_launcher.PROJECT_ROOT_ENV, None)
    environment.pop(managed_launcher.CORE_ROOT_ENV, None)
    environment.pop("KIT_LIFECYCLE_CHECK", None)
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "KIT_ENGINE_DISABLED": "1",
        "KIT_NATIVE_RETRY_TOKEN": "",
        "KIT_TEST_TMPDIR": str(scratch / "t"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return (
        process_supervisor.isolated_python_script_command(
            Path(sys.executable).resolve(),
            copied_core / "kit.py",
            copied_core,
            "self-test",
            "--project",
            str(installation.project_root),
            "--json",
        ),
        environment,
    )


def _echo(outcome: Any) -> None:
    stdout = str(getattr(outcome, "stdout", "") or "")
    stderr = str(getattr(outcome, "stderr", "") or "")
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr:
        print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: int,
    runner: ProcessRunner,
) -> Any:
    outcome = runner(
        command,
        cwd=cwd,
        timeout=timeout,
        environment=dict(environment),
        capture_output=True,
        allow_child_breakaway=False,
    )
    if not bool(getattr(outcome, "termination_verified", False)):
        raise KitChangeCheckError("offline check containment was not verified")
    if bool(getattr(outcome, "timed_out", False)):
        raise KitChangeCheckError("offline check timed out")
    if bool(getattr(outcome, "cancelled", False)):
        raise KitChangeCheckError("offline check was cancelled")
    launch_error = str(getattr(outcome, "launch_error", "") or "")
    if launch_error:
        raise KitChangeCheckError(f"offline check could not start: {launch_error}")
    if getattr(outcome, "returncode", None) is None:
        raise KitChangeCheckError("offline check returned no exit code")
    return outcome


def _receipt(outcome: Any, command: str) -> dict[str, Any]:
    stdout = str(getattr(outcome, "stdout", "") or "")
    try:
        value = json.loads(stdout)
    except (UnicodeError, ValueError) as exc:
        raise KitChangeCheckError(f"{command} returned no readable receipt") from exc
    if not isinstance(value, dict) or value.get("command") != command:
        raise KitChangeCheckError(f"{command} returned the wrong receipt")
    exit_code = getattr(outcome, "returncode", None)
    if value.get("exit_code") != exit_code or not isinstance(value.get("ok"), bool):
        raise KitChangeCheckError(f"{command} receipt does not match its process")
    return value


def _receipt_project(receipt: Mapping[str, Any], expected: Path, label: str) -> None:
    raw = receipt.get("project")
    if not isinstance(raw, str) or not raw or len(raw) > 32_768:
        raise KitChangeCheckError(f"{label} receipt has no valid project")
    candidate = Path(raw)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise KitChangeCheckError(f"{label} receipt project is not canonical")
    try:
        resolved = candidate.resolve(strict=True)
        expected_resolved = expected.resolve(strict=True)
    except OSError as exc:
        raise KitChangeCheckError(f"{label} receipt project is unavailable") from exc
    if (
        os.path.normcase(os.path.abspath(raw)) != os.path.normcase(str(resolved))
        or os.path.normcase(str(resolved)) != os.path.normcase(str(expected_resolved))
    ):
        raise KitChangeCheckError(f"{label} receipt selected the wrong project")


def _nested_process_exit(receipt: Mapping[str, Any], label: str) -> int:
    process = receipt.get("process")
    if not isinstance(process, Mapping) or len(process) > 3:
        raise KitChangeCheckError(f"{label} receipt has malformed process evidence")
    exit_code = process.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise KitChangeCheckError(f"{label} receipt has malformed process evidence")
    for field in ("stdout", "stderr"):
        if field in process and (
            not isinstance(process[field], str)
            or len(process[field]) > MAX_RECEIPT_OUTPUT_CHARS
        ):
            raise KitChangeCheckError(f"{label} receipt has oversized process evidence")
    return exit_code


def _validate_self_test_receipt(
    receipt: Mapping[str, Any], installation: managed_launcher.Installation, outer_exit: int
) -> None:
    _receipt_project(receipt, installation.project_root, "self-test")
    if receipt.get("ok") is False and "error" in receipt:
        if set(receipt) != {
            "ok", "command", "status", "error", "project", "exit_code"
        }:
            raise KitChangeCheckError("self-test error receipt fields are malformed")
        status = receipt.get("status")
        error = receipt.get("error")
        if (
            not isinstance(outer_exit, int)
            or isinstance(outer_exit, bool)
            or outer_exit == 0
            or receipt.get("exit_code") != outer_exit
            or not isinstance(status, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", status) is None
            or not isinstance(error, str)
            or not 1 <= len(error) <= 2_000
            or any(ord(character) < 32 or ord(character) == 127 for character in error)
        ):
            raise KitChangeCheckError("self-test error receipt is malformed")
        return
    nested_exit = _nested_process_exit(receipt, "self-test")
    if receipt.get("engine") != "disabled":
        raise KitChangeCheckError("self-test receipt did not disable the native engine")
    tests = receipt.get("tests")
    if not isinstance(tests, Mapping) or set(tests) != {
        "ran", "failures", "errors", "skipped"
    }:
        raise KitChangeCheckError("self-test receipt has no valid test summary")
    test_counts: dict[str, int] = {}
    for field in ("ran", "failures", "errors", "skipped"):
        value = tests.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 1_000_000
        ):
            raise KitChangeCheckError("self-test receipt has no valid test summary")
        test_counts[field] = value
    if receipt.get("ok") is True:
        if (
            outer_exit != 0
            or receipt.get("exit_code") != 0
            or nested_exit != 0
            or receipt.get("status") != "passed"
            or test_counts["ran"] < 1
            or test_counts["failures"] != 0
            or test_counts["errors"] != 0
            or test_counts["skipped"] != 0
            or "failure" in receipt
        ):
            raise KitChangeCheckError("self-test success receipt is internally inconsistent")
    elif receipt.get("status") != "failed":
        raise KitChangeCheckError("self-test failure receipt is internally inconsistent")


def _bounded_gate_summary(receipt: Mapping[str, Any]) -> None:
    nonce = receipt.get("verification_nonce")
    summary = receipt.get("gate_summary")
    validated_skips = _verification_skips(receipt)
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise KitChangeCheckError("static verification receipt has no valid run identity")
    if not isinstance(summary, Mapping):
        raise KitChangeCheckError("static verification receipt has no gate summary")
    repository_sha = summary.get("repository_sha256")
    if (
        summary.get("schema") != 2
        or summary.get("run_id") != nonce
        or summary.get("failed") is not False
        or not isinstance(repository_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", repository_sha) is None
        or not isinstance(summary.get("auth_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(summary.get("auth_sha256"))) is None
    ):
        raise KitChangeCheckError("static verification gate summary is malformed")
    results = summary.get("results")
    diagnostics = summary.get("diagnostics")
    if (
        not isinstance(results, list)
        or len(results) > 200
        or any(not isinstance(item, str) or len(item) > 500 for item in results)
        or not isinstance(diagnostics, Mapping)
        or set(diagnostics) != {
            "native_crashes",
            "engine_refusals",
            "engine_start_failures",
            "timeouts",
        }
    ):
        raise KitChangeCheckError("static verification gate evidence is malformed")
    if len(results) != len(STATIC_STAGES):
        raise KitChangeCheckError("static verification did not report every static stage")
    seen: set[str] = set()
    reported_skips: list[str] = []
    for line in results:
        match = re.fullmatch(r"(PASS|FAIL|SKIP)\s{2}(.+)", line)
        if match is None:
            raise KitChangeCheckError("static verification stage evidence is malformed")
        state, detail = match.groups()
        stage = detail.split(maxsplit=1)[0]
        if stage not in STATIC_STAGES or stage in seen or state == "FAIL":
            raise KitChangeCheckError("static verification stage evidence is malformed")
        seen.add(stage)
        if state == "SKIP":
            reported_skips.append(detail)
    if seen != set(STATIC_STAGES):
        raise KitChangeCheckError("static verification did not report every static stage")
    if reported_skips != validated_skips:
        raise KitChangeCheckError("static verification skipped-check evidence disagrees")
    for entries in diagnostics.values():
        if (
            not isinstance(entries, list)
            or len(entries) > 20
            or any(not isinstance(entry, Mapping) for entry in entries)
        ):
            raise KitChangeCheckError("static verification gate evidence is malformed")
    for field in ("repository_start", "repository_end"):
        state = receipt.get(field)
        if (
            not isinstance(state, Mapping)
            or state.get("available") is not True
            or state.get("digest") != repository_sha
        ):
            raise KitChangeCheckError("static verification repository evidence is malformed")
    if receipt.get("repository_stable") is not True:
        raise KitChangeCheckError("static verification did not prove a stable repository")


def _validate_static_success(
    receipt: Mapping[str, Any], installation: managed_launcher.Installation, outer_exit: int
) -> None:
    _receipt_project(receipt, installation.project_root, "static verification")
    nested_exit = _nested_process_exit(receipt, "static verification")
    if (
        outer_exit != 0
        or receipt.get("exit_code") != 0
        or nested_exit != 0
        or receipt.get("ok") is not True
        or receipt.get("status") != "passed"
        or receipt.get("strict") is not False
        or receipt.get("static") is not True
        or receipt.get("stages") != []
        or receipt.get("fast") is not False
        or receipt.get("engine") is not None
    ):
        raise KitChangeCheckError("static verification receipt has the wrong scope")
    _bounded_gate_summary(receipt)


def _verification_skips(receipt: Mapping[str, Any]) -> list[str]:
    if "skips" not in receipt:
        raise KitChangeCheckError("verify receipt has no skipped-check list")
    raw = receipt["skips"]
    if not isinstance(raw, list) or len(raw) > 64:
        raise KitChangeCheckError("verify returned malformed skipped checks")
    skips: list[str] = []
    for item in raw:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 160
            or "\n" in item
            or "\r" in item
        ):
            raise KitChangeCheckError("verify returned malformed skipped checks")
        skips.append(" ".join(item.split()))
    return skips


def _skip_detail(skips: Sequence[str]) -> str:
    visible = list(skips[:8])
    detail = ", ".join(visible)
    if len(skips) > len(visible):
        detail += f", and {len(skips) - len(visible)} more"
    return detail


def _self_test_failure_detail(receipt: Mapping[str, Any]) -> str:
    """Render only bounded structured failure evidence, never raw test output."""
    if receipt.get("ok") is False and "error" in receipt:
        status = str(receipt["status"]).replace("_", " ")
        return f"Installed kit self-test could not run ({status}): {receipt['error']}"
    failure = receipt.get("failure")
    if not isinstance(failure, Mapping) or set(failure) != {
        "ran", "failures", "errors", "skipped", "test_ids", "skip_ids", "log"
    }:
        return "Installed kit self-test failed; no valid failure report was returned."
    counts: dict[str, int] = {}
    for field in ("ran", "failures", "errors", "skipped"):
        value = failure.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000_000:
            return "Installed kit self-test failed; its failure report was malformed."
        counts[field] = value
    groups: dict[str, list[str]] = {}
    for field in ("test_ids", "skip_ids"):
        raw = failure.get(field)
        if (
            not isinstance(raw, list)
            or len(raw) > 20
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", item) is None
                for item in raw
            )
        ):
            return "Installed kit self-test failed; its failure report was malformed."
        groups[field] = list(raw)
    parts = [
        f"{counts['ran']} ran",
        f"{counts['failures']} failed",
        f"{counts['errors']} errors",
        f"{counts['skipped']} skipped",
    ]
    if groups["test_ids"]:
        parts.append("tests: " + ", ".join(groups["test_ids"][:8]))
    if groups["skip_ids"]:
        parts.append("skipped tests: " + ", ".join(groups["skip_ids"][:8]))
    log = failure.get("log")
    if isinstance(log, Mapping) and log.get("available") is True:
        path = log.get("path")
        digest = log.get("sha256")
        size = log.get("bytes")
        truncated = log.get("truncated")
        if (
            isinstance(path, str)
            and len(path) <= 512
            and path.startswith(".kit/")
            and ".." not in Path(path).parts
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest)
            and isinstance(size, int)
            and not isinstance(size, bool)
            and 0 <= size <= 256 * 1024
            and isinstance(truncated, bool)
        ):
            parts.append(f"private log: {path} ({digest[:12]})")
    return "Installed kit self-test failed: " + "; ".join(parts) + "."


def _current_document(installation: managed_launcher.Installation) -> dict[str, Any]:
    if installation.current_path is None:
        raise KitChangeCheckError("managed current pointer is missing")
    try:
        content = kit_change._stable_bytes(  # noqa: SLF001 - shared path safety
            installation.current_path,
            limit=managed_launcher.MAX_CURRENT_BYTES,
        )
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, kit_change.KitChangeError) as exc:
        raise KitChangeCheckError(f"managed current pointer is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise KitChangeCheckError("managed current pointer is malformed")
    return value


def _validate_launchers(installation: managed_launcher.Installation) -> None:
    current = _current_document(installation)
    active = current.get("active_release")
    active_sha = str(active.get("archive_sha256") or "") if isinstance(active, Mapping) else ""
    if active_sha != installation.release_sha256:
        raise KitChangeCheckError("managed current pointer changed after core selection")
    surfaces = current.get("managed_surfaces")
    if not isinstance(surfaces, list):
        raise KitChangeCheckError("managed launcher records are missing")
    by_path = {
        str(item.get("path") or ""): item
        for item in surfaces
        if isinstance(item, Mapping)
    }
    for relative in REQUIRED_LAUNCHERS:
        record = by_path.get(relative)
        expected = str(record.get("applied_sha256") or "") if record else ""
        if not record or len(expected) != 64:
            raise KitChangeCheckError(f"managed launcher record is missing: {relative}")
        try:
            path = kit_change._target(  # noqa: SLF001 - shared path safety
                installation.project_root, relative
            )
            content = kit_change._stable_bytes(path)  # noqa: SLF001
        except kit_change.KitChangeError as exc:
            raise KitChangeCheckError(f"managed launcher is unsafe: {relative}: {exc.detail}") from exc
        if _sha256(content) != expected:
            raise KitChangeCheckError(f"managed launcher changed after apply: {relative}")


def _scan_document(content: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise KitChangeCheckError(f"brownfield scan has duplicate key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, ValueError) as exc:
        raise KitChangeCheckError(f"brownfield scan is unreadable: {exc}") from exc
    expected = {"schema", "kind", "complete", "issues", "errors"}
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema") != brownfield.SCHEMA
        or value.get("kind") != brownfield.SCAN_KIND
        or not isinstance(value.get("complete"), bool)
        or not isinstance(value.get("issues"), list)
        or not isinstance(value.get("errors"), list)
    ):
        raise KitChangeCheckError("brownfield scan fields are not exact")
    try:
        issues = brownfield.normalize_issues(value["issues"])
        canonical = brownfield.canonical_json(value)
    except ValueError as exc:
        raise KitChangeCheckError(f"brownfield scan is invalid: {exc}") from exc
    if issues != value["issues"] or canonical != content:
        raise KitChangeCheckError("brownfield scan is not canonical")
    if not value["complete"] or value["errors"]:
        raise KitChangeCheckError("brownfield scan is incomplete")
    return value


def _baseline_prior(session: Mapping[str, Any]) -> tuple[bool, str]:
    baseline = session.get("baseline")
    prior = baseline.get("prior") if isinstance(baseline, Mapping) else None
    if not isinstance(prior, Mapping):
        raise KitChangeCheckError("approved baseline snapshot is missing")
    exists = prior.get("exists")
    digest = prior.get("sha256")
    if not isinstance(exists, bool) or not isinstance(digest, str):
        raise KitChangeCheckError("approved baseline snapshot is malformed")
    if (exists and len(digest) != 64) or (not exists and digest):
        raise KitChangeCheckError("approved baseline identity is malformed")
    return exists, digest


def _prior_baseline_content(session: Mapping[str, Any]) -> bytes | None:
    exists, digest = _baseline_prior(session)
    if not exists:
        return None
    baseline = session.get("baseline")
    prior = baseline.get("prior") if isinstance(baseline, Mapping) else None
    encoded = prior.get("content_base64") if isinstance(prior, Mapping) else None
    if not isinstance(encoded, str):
        raise KitChangeCheckError("approved prior baseline bytes are missing")
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise KitChangeCheckError("approved prior baseline bytes are malformed") from exc
    if _sha256(content) != digest:
        raise KitChangeCheckError("approved prior baseline bytes do not match their identity")
    return content


def _prior_binding(content: bytes) -> dict[str, str]:
    try:
        value = json.loads(content.decode("utf-8"))
        binding = value.get("binding") if isinstance(value, Mapping) else None
        installation_id = str(binding.get("installation_id") or "") if isinstance(binding, Mapping) else ""
        release_sha256 = str(binding.get("release_sha256") or "") if isinstance(binding, Mapping) else ""
        brownfield.validate_baseline(
            content,
            installation_id=installation_id,
            release_sha256=release_sha256,
        )
    except (UnicodeError, ValueError) as exc:
        raise KitChangeCheckError(f"approved prior baseline is invalid: {exc}") from exc
    return {
        "installation_id": installation_id,
        "release_sha256": release_sha256,
    }


def _reviewed_prior_release(session: Mapping[str, Any]) -> str:
    preview = session.get("preview")
    raw = preview.get("raw") if isinstance(preview, Mapping) else None
    material = raw.get("material") if isinstance(raw, Mapping) else None
    current = material.get("current") if isinstance(material, Mapping) else None
    return str(current.get("active_archive_sha256") or "") if isinstance(current, Mapping) else ""


def _reviewed_current_mode(session: Mapping[str, Any]) -> str:
    preview = session.get("preview")
    raw = preview.get("raw") if isinstance(preview, Mapping) else None
    material = raw.get("material") if isinstance(raw, Mapping) else None
    current = material.get("current") if isinstance(material, Mapping) else None
    return str(current.get("mode") or "") if isinstance(current, Mapping) else ""


def _reviewed_changed_paths(session: Mapping[str, Any]) -> set[str]:
    preview = session.get("preview")
    raw = preview.get("raw") if isinstance(preview, Mapping) else None
    material = raw.get("material") if isinstance(raw, Mapping) else None
    changes = material.get("changes") if isinstance(material, Mapping) else None
    if not isinstance(changes, list) or len(changes) > 4096:
        raise KitChangeCheckError("approved change list is missing or malformed")
    paths: set[str] = set()
    for item in changes:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise KitChangeCheckError("approved change list is malformed")
        try:
            relative = kit_change._safe_relative(item["path"])  # noqa: SLF001
        except kit_change.KitChangeError as exc:
            raise KitChangeCheckError(
                f"approved change path is unsafe: {exc.detail}"
            ) from exc
        if relative in paths:
            raise KitChangeCheckError("approved change list contains a duplicate path")
        paths.add(relative)
    return paths


def _first_baseline(
    target: Path,
    issues: list[dict[str, object]],
    binding: Mapping[str, str],
) -> dict[str, object]:
    try:
        return brownfield.build_baseline(
            target,
            issues,
            installation_id=str(binding.get("installation_id") or ""),
            release_sha256=str(binding.get("release_sha256") or ""),
        )
    except ValueError as exc:
        raise KitChangeCheckError(f"brownfield baseline cannot be built: {exc}") from exc


def _public_issues(values: object) -> list[dict[str, object]]:
    if not isinstance(values, list):
        raise KitChangeCheckError("brownfield evaluation issues are malformed")
    public = [
        {field: item.get(field) for field in _PUBLIC_ISSUE_FIELDS}
        for item in values
        if isinstance(item, Mapping)
    ]
    if len(public) != len(values):
        raise KitChangeCheckError("brownfield evaluation issue is malformed")
    try:
        return brownfield.normalize_issues(public)
    except ValueError as exc:
        raise KitChangeCheckError(f"brownfield evaluation is invalid: {exc}") from exc


def _rebound_baseline(
    stored_issues: object,
    current_binding: Mapping[str, str],
) -> dict[str, object]:
    if not isinstance(stored_issues, list):
        raise KitChangeCheckError("brownfield evaluation existing gaps are malformed")
    document: dict[str, object] = {
        "schema": brownfield.SCHEMA,
        "kind": brownfield.KIND,
        "binding": {
            "installation_id": str(current_binding.get("installation_id") or ""),
            "release_sha256": str(current_binding.get("release_sha256") or ""),
        },
        "issues": stored_issues,
    }
    try:
        content = brownfield.canonical_json(document)
        return brownfield.validate_baseline(
            content,
            installation_id=str(current_binding.get("installation_id") or ""),
            release_sha256=str(current_binding.get("release_sha256") or ""),
        )
    except ValueError as exc:
        raise KitChangeCheckError(f"brownfield baseline cannot be rebound: {exc}") from exc


def _baseline_plan(
    target: Path,
    session: Mapping[str, Any],
    current_issues: list[dict[str, object]],
    current_binding: Mapping[str, str],
) -> tuple[dict[str, object], list[dict[str, object]], int]:
    changed_paths = _reviewed_changed_paths(session)
    changed_issues = [
        str(issue.get("path") or "")
        for issue in current_issues
        if str(issue.get("path") or "") in changed_paths
    ]
    if changed_issues:
        visible = ", ".join(sorted(set(changed_issues))[:8])
        raise KitChangeCheckError(
            "post-Apply verification reported a problem in a file changed by the kit: "
            + visible
        )
    request = session.get("request")
    mode = str(request.get("mode") or "") if isinstance(request, Mapping) else ""
    if mode == "install":
        return _first_baseline(target, current_issues, current_binding), [], 0
    if mode != "upgrade":
        raise KitChangeCheckError("kit change mode is missing or invalid")
    prior_content = _prior_baseline_content(session)
    if prior_content is None:
        if _reviewed_current_mode(session) != "legacy":
            raise KitChangeCheckError(
                "approved managed upgrade has no prior brownfield baseline"
            )
        return _first_baseline(target, current_issues, current_binding), [], 0
    prior_binding = _prior_binding(prior_content)
    if prior_binding["installation_id"] != current_binding.get("installation_id"):
        raise KitChangeCheckError("approved prior baseline belongs to another installation")
    reviewed_release = _reviewed_prior_release(session)
    if not reviewed_release or prior_binding["release_sha256"] != reviewed_release:
        raise KitChangeCheckError("approved prior baseline is not bound to the reviewed release")
    def evaluate() -> dict[str, object]:
        try:
            return brownfield.evaluate_baseline(
                target,
                prior_content,
                current_issues,
                installation_id=prior_binding["installation_id"],
                release_sha256=prior_binding["release_sha256"],
            )
        except ValueError as exc:
            raise KitChangeCheckError(
                f"approved prior baseline cannot be evaluated: {exc}"
            ) from exc

    evaluate()
    _failpoint("after-upgrade-evaluation")
    evaluation = evaluate()
    counts = evaluation.get("counts")
    resolved = counts.get("resolved") if isinstance(counts, Mapping) else None
    if not isinstance(resolved, int) or isinstance(resolved, bool) or resolved < 0:
        raise KitChangeCheckError("brownfield evaluation counts are malformed")
    return (
        _rebound_baseline(evaluation.get("existing_gaps"), current_binding),
        _public_issues(evaluation.get("failing_gaps")),
        resolved,
    )


def _write_baseline(
    target: Path,
    content: bytes,
    session: Mapping[str, Any],
) -> str:
    generated_sha = _sha256(content)
    prior_exists, prior_sha = _baseline_prior(session)
    baseline_state = session.get("baseline")
    expected = (
        baseline_state.get("generated")
        if isinstance(baseline_state, Mapping)
        else None
    )
    if isinstance(expected, Mapping):
        expected_sha = str(expected.get("sha256") or "")
        if expected_sha != generated_sha:
            raise KitChangeCheckError(
                "brownfield scan changed after its generated baseline was recorded"
            )
    try:
        current = brownfield.read_baseline_file(target)
    except (OSError, ValueError) as exc:
        raise KitChangeCheckError(f"existing baseline is unsafe: {exc}") from exc
    current_sha = _sha256(current) if current is not None else ""
    prior_matches = (current is not None) == prior_exists and current_sha == prior_sha
    generated_matches = current is not None and current_sha == generated_sha
    if not prior_matches and not generated_matches:
        raise KitChangeCheckError("brownfield baseline changed after the approved review")
    if not generated_matches:
        try:
            path = kit_change._target(target, brownfield.BASELINE_RELATIVE)  # noqa: SLF001
            kit_change._atomic_bytes(path, content, "0644")  # noqa: SLF001
        except (OSError, kit_change.KitChangeError) as exc:
            detail = exc.detail if isinstance(exc, kit_change.KitChangeError) else str(exc)
            raise KitChangeCheckError(f"brownfield baseline could not be written: {detail}") from exc
    try:
        written = brownfield.read_baseline_file(target)
    except (OSError, ValueError) as exc:
        raise KitChangeCheckError(f"written baseline is unsafe: {exc}") from exc
    if written is None or _sha256(written) != generated_sha:
        raise KitChangeCheckError("brownfield baseline changed while it was written")
    return generated_sha


def _scan_path(context: project_context.ProjectContext, session_id: str) -> Path:
    if len(session_id) != 64:
        raise KitChangeCheckError("kit change session identity is malformed")
    try:
        runtime_relative = context.runtime_root.relative_to(context.project_root).as_posix()
        directory = kit_change._ensure_directory(  # noqa: SLF001
            context.project_root,
            f"{runtime_relative}/{CHECK_ROOT}",
            [],
        )
    except (OSError, ValueError, kit_change.KitChangeError) as exc:
        detail = exc.detail if isinstance(exc, kit_change.KitChangeError) else str(exc)
        raise KitChangeCheckError(f"private check folder is unavailable: {detail}") from exc
    return directory / f"{session_id}.json"


def _failure(detail: str, baseline_sha256: str = "") -> dict[str, object]:
    return {
        "kit_ok": False,
        "project_ok": False,
        "existing_issues": [],
        "detail": " ".join(detail.split())[:MAX_DETAIL_CHARS],
        "baseline_sha256": baseline_sha256,
    }


def run(
    target_project: Path,
    session: Mapping[str, Any],
    *,
    runner: ProcessRunner = process_supervisor.run_supervised,
    baseline_ready: BaselineReady | None = None,
) -> Mapping[str, Any]:
    """Run the exact managed-core, self-test, scan, baseline and static proof."""
    generated_sha = ""
    try:
        installation = managed_launcher.resolve_installation(target_project)
        if installation.mode != "managed":
            raise KitChangeCheckError("the applied project did not select a managed kit")
        release_state = session.get("release")
        expected_release = (
            str(release_state.get("archive_sha256") or "")
            if isinstance(release_state, Mapping)
            else ""
        )
        if not expected_release or installation.release_sha256 != expected_release:
            raise KitChangeCheckError("the applied project selected a different release")
        _validate_launchers(installation)
        context = project_context.load_configured_context(installation.project_root)
        if context.install_mode != "managed" or context.core_root != installation.core_root:
            raise KitChangeCheckError("the configured project selects a different managed core")
        environment = _clean_environment(installation)
        with tempfile.TemporaryDirectory(prefix="ak-") as temporary:
            self_test_command, self_test_environment = _self_test_command(
                installation,
                Path(temporary).resolve(),
                runner=runner,
            )
            self_test = _run_process(
                self_test_command,
                cwd=Path(temporary).resolve(),
                environment=self_test_environment,
                timeout=1800,
                runner=runner,
            )
        self_test_receipt = _receipt(self_test, "self-test")
        _validate_self_test_receipt(
            self_test_receipt,
            installation,
            int(getattr(self_test, "returncode", -1)),
        )
        if getattr(self_test, "returncode", None) != 0 or not self_test_receipt["ok"]:
            return _failure(_self_test_failure_detail(self_test_receipt))

        scan_path = _scan_path(context, str(session.get("session_id") or ""))
        scan = _run_process(
            _launcher_command(
                installation,
                "__brownfield-scan",
                str(scan_path),
                "--project",
                str(installation.project_root),
                "--json",
            ),
            cwd=installation.project_root,
            environment=environment,
            timeout=900,
            runner=runner,
        )
        if getattr(scan, "returncode", None) != 0:
            return _failure("Existing-project scan could not complete.")
        try:
            scan_content = kit_change._stable_bytes(  # noqa: SLF001
                scan_path, limit=MAX_SCAN_BYTES
            )
        except kit_change.KitChangeError as exc:
            raise KitChangeCheckError(f"brownfield scan output is unsafe: {exc.detail}") from exc
        scan_document = _scan_document(scan_content)
        binding = brownfield.read_install_binding(installation.project_root)
        if binding["release_sha256"] != installation.release_sha256:
            raise KitChangeCheckError("managed current pointer changed during offline checks")
        scan_issues = list(scan_document["issues"])
        baseline, failing_issues, resolved_count = _baseline_plan(
            installation.project_root,
            session,
            scan_issues,
            binding,
        )
        baseline_content = brownfield.canonical_json(baseline)
        if baseline_ready is not None:
            session = baseline_ready(baseline_content)
        _failpoint("after-baseline-intent")
        generated_sha = _write_baseline(
            installation.project_root, baseline_content, session
        )
        _failpoint("after-baseline-write")

        static = _run_process(
            _launcher_command(
                installation,
                "verify",
                "--static",
                "--project",
                str(installation.project_root),
                "--json",
            ),
            cwd=installation.project_root,
            environment=environment,
            timeout=1800,
            runner=runner,
        )
        static_receipt = _receipt(static, "verify")
        static_ok = getattr(static, "returncode", None) == 0 and bool(
            static_receipt["ok"]
        )
        if static_ok:
            _validate_static_success(
                static_receipt,
                installation,
                int(getattr(static, "returncode", -1)),
            )
        if not static_ok:
            failed = _failure(
                "Installed static verification failed after applying the exact baseline.",
                generated_sha,
            )
            failed["existing_issues"] = scan_issues
            return failed
        issues = scan_issues
        skips = _verification_skips(static_receipt)
        project_ok = not issues and not skips
        if project_ok:
            detail = (
                f"Offline kit and project checks passed; {resolved_count} old gap(s) resolved."
                if resolved_count
                else "Offline kit and project checks passed."
            )
        elif failing_issues:
            detail = (
                f"Kit works; {len(failing_issues)} new or changed project gap(s) "
                "need adoption work and were not added to the baseline."
            )
        elif issues:
            detail = f"Kit works; {len(issues)} unchanged existing project gap(s) remain."
            if skips:
                detail = (
                    detail[:-1]
                    + f"; these project checks are not ready: {_skip_detail(skips)}."
                )
        elif skips:
            detail = (
                "Kit works; these project checks are not ready: "
                f"{_skip_detail(skips)}."
            )
        else:
            detail = "Kit works; project checks failed and need adoption work."
        result: dict[str, object] = {
            "kit_ok": True,
            "project_ok": project_ok,
            "existing_issues": issues,
            "detail": detail,
            "baseline_sha256": generated_sha,
        }
        if set(result) != _CHECK_FIELDS:
            raise AssertionError("internal check result fields drifted")
        return result
    except (OSError, ValueError, KitChangeCheckError) as exc:
        return _failure(f"Installed kit check failed: {exc}", generated_sha)
