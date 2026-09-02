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
CHECK_ROOT = "kit-change-checks"
REQUIRED_LAUNCHERS = (".agent-kit/launcher.py", "kit", "kit.cmd")
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
    _echo(outcome)
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
        if getattr(self_test, "returncode", None) != 0 or not self_test_receipt["ok"]:
            return _failure("Installed kit self-test failed.")

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
        if not static_ok:
            failed = _failure(
                "Installed static verification failed after applying the exact baseline.",
                generated_sha,
            )
            failed["existing_issues"] = scan_issues
            return failed
        issues = scan_issues
        project_ok = not issues
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
