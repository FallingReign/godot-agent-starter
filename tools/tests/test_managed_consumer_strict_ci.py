#!/usr/bin/env python3
"""Prove one installed managed kit through the real public strict path.

This driver is source-only because it builds the release under test from the
source checkout.  The workflow runs it on Windows, macOS and Linux after the
source checkout itself has passed strict verification.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from typing import Any


TESTS = Path(__file__).resolve().parent
TOOLS = TESTS.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TOOLS))

import design  # noqa: E402
import kit_change  # noqa: E402
import managed_launcher  # noqa: E402
import process_supervisor  # noqa: E402
import release  # noqa: E402
import test_lifecycle_e2e as lifecycle_support  # noqa: E402


DRIVER_RELATIVE = "tools/tests/test_managed_consumer_strict_ci.py"
MAINTAINER_MARKER = "src/.kit-maintainer-fixture"
COMPLETE_APPLY_STATUS = "complete"
ADOPTION_APPLY_STATUS = "adoption_required"
DESIGN_RELATIVE = "docs/design/experience/action-confirmation.md"
FAILURE_LOG_TAIL_BYTES = 12_000
FAILURE_EVIDENCE_CHARS = 24_000

DESIGN_TEXT = """# Action confirmation

_Resolution: settled_
_Authority: agent-provisional_
_Authored by: agent_
_Confidence: very-high_

## Quick read
- **Player does:** Repeats one simple action.
- **Player experiences:** A short confirmation showing the current action count.
- **Successful outcome:** Each action raises the count once and the confirmation matches it.

## Why this inference
One action, one count, and one matching confirmation is the smallest complete
experience that can be reversed without affecting saved progress or content.

## Assumptions
- The action has no lasting cost or authored content tied to its count.
- Plain confirmation text is enough for this bounded experience.

## Veto and go/no-go
- **Veto scope:** Remove the confirmation while leaving the action count unchanged.
- **Next go/no-go:** Before saved progress or authored content depends on this feedback.
"""


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write(root: Path, relative: str, content: bytes) -> Path:
    target = root.joinpath(*PurePosixPath(relative).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _is_reparse(info: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


def _copy_tracked_engine_fixture(target: Path) -> None:
    listing = lifecycle_support._run_git(ROOT, "ls-files", "-z", "--", "src")
    relative_paths = [item for item in listing.split("\0") if item]
    required = {
        MAINTAINER_MARKER,
        "src/project.godot",
        "src/.gutconfig.json",
        "src/scripts/logic/kit_verification_probe.gd",
        "src/tests/unit/test_kit_verification_probe.gd",
        "src/tests/smoke_test.tscn",
        "src/tests/fixtures/boot_target.tscn",
    }
    missing = sorted(required - set(relative_paths))
    if missing:
        raise AssertionError(f"canonical engine fixture is incomplete: {missing}")

    folded: set[str] = set()
    for relative in relative_paths:
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or pure.as_posix() != relative
            or not relative.startswith("src/")
        ):
            raise AssertionError(f"tracked fixture path is unsafe: {relative!r}")
        key = relative.casefold()
        if key in folded:
            raise AssertionError(f"tracked fixture path collides by case: {relative}")
        folded.add(key)
        if relative == MAINTAINER_MARKER:
            continue

        source = ROOT.joinpath(*pure.parts)
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(
            info.st_mode
        ):
            raise AssertionError(f"tracked fixture member is redirected: {relative}")
        destination = target.joinpath(*pure.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(stat.S_IMODE(info.st_mode))

    if os.path.lexists(target.joinpath(*PurePosixPath(MAINTAINER_MARKER).parts)):
        raise AssertionError("managed consumer inherited the maintainer fixture marker")


def _initialise_project(target: Path) -> str:
    _copy_tracked_engine_fixture(target)
    _write(target, DESIGN_RELATIVE, DESIGN_TEXT.encode("utf-8"))
    _write(
        target,
        "project.shape.json",
        _json_bytes(
            {
                "decisions": [],
                "direction": [],
                "involvement": "hands-off",
                "name": "Managed Consumer CI",
                "pitch": (
                    "A generic project that proves a released kit can be installed "
                    "and verified without its source-only maintainer exception."
                ),
                "questions": [],
            }
        ),
    )
    lifecycle_support._run_git(target, "init", "--quiet")
    lifecycle_support._run_git(target, "config", "user.name", "Kit CI")
    lifecycle_support._run_git(
        target, "config", "user.email", "kit-ci@example.invalid"
    )
    lifecycle_support._run_git(target, "config", "core.autocrlf", "false")
    lifecycle_support._run_git(target, "config", "core.safecrlf", "true")
    lifecycle_support._run_git(target, "config", "gc.auto", "0")
    lifecycle_support._run_git(target, "config", "maintenance.auto", "false")
    lifecycle_support._run_git(target, "add", "--all")
    lifecycle_support._run_git(
        target,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "Create generic project",
    )
    return lifecycle_support._run_git(target, "rev-parse", "HEAD")


def _record_design_backed_change(target: Path, baseline_sha: str) -> None:
    logic = target / "src" / "scripts" / "logic" / "kit_verification_probe.gd"
    logic.write_text(
        logic.read_text(encoding="utf-8")
        + "\n\nfunc confirmation() -> String:\n"
        + '\treturn "Action %d confirmed" % _observations\n',
        encoding="utf-8",
        newline="\n",
    )
    test = target / "src" / "tests" / "unit" / "test_kit_verification_probe.gd"
    test.write_text(
        test.read_text(encoding="utf-8")
        + "\n\nfunc test_probe_confirms_the_current_count() -> void:\n"
        + "\tvar probe: KitVerificationProbe = KitVerificationProbe.new()\n"
        + '\tassert_eq(probe.confirmation(), "Action 0 confirmed")\n'
        + "\tprobe.observe()\n"
        + '\tassert_eq(probe.confirmation(), "Action 1 confirmed")\n',
        encoding="utf-8",
        newline="\n",
    )
    design_digest = design.design_sha256(DESIGN_TEXT)
    proposal = {
        "baseline_sha": baseline_sha,
        "design_authority": {
            "authority": "agent-provisional",
            "authored_by": "agent",
            "confidence": "very-high",
        },
        "design_refs": [
            {
                "section": DESIGN_RELATIVE,
                "sha256": design_digest,
                "why": "Defines the exact action feedback this change must provide.",
            }
        ],
        "experience": {
            "feels_like": "Immediate and unambiguous progress.",
            "not_this": "A silent action or a count that lags behind the action.",
            "player_does": "Repeats one simple action.",
        },
        "mockup": {
            "not_possible": (
                "This bounded outcome is one changing text value, so its exact "
                "words are clearer than a static picture."
            )
        },
        "reversibility": {
            "hard_to_undo": (
                "Saved progress or authored content depending on the confirmation."
            ),
            "next_go_no_go": (
                "Before saved progress or authored content depends on this feedback."
            ),
            "state": "reversible",
            "veto_scope": (
                "Remove the confirmation and its test; the existing count remains."
            ),
        },
        "scope": [
            {
                "action": "modify",
                "kind": "file",
                "path": "scripts/logic/kit_verification_probe.gd",
                "why": "Extend the existing action count instead of creating a new module.",
            },
            {
                "action": "modify",
                "kind": "file",
                "path": "tests/unit/test_kit_verification_probe.gd",
                "why": "Prove the existing count and its new confirmation stay aligned.",
            },
        ],
        "slice": "managed-consumer-action-confirmation",
        "status": "recorded",
    }
    _write(target, "proposal.json", _json_bytes(proposal))


def _launcher_command(target: Path, *arguments: str) -> list[str]:
    if os.name == "nt":
        return [
            process_supervisor.windows_command_processor(),
            "/d",
            "/c",
            str(target / "kit.cmd"),
            *arguments,
        ]
    return [str(target / "kit"), *arguments]


def _public_environment() -> dict[str, str]:
    environment = process_supervisor.isolated_python_environment()
    for name in (
        managed_launcher.PROJECT_ROOT_ENV,
        managed_launcher.CORE_ROOT_ENV,
        "KIT_ENGINE_DISABLED",
        "KIT_NATIVE_RETRY_TOKEN",
        "KIT_VERIFY_AUTH_KEY",
        "KIT_VERIFY_NONCE",
        "KIT_VERIFY_REPOSITORY_SHA256",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "CI": "1",
            "KIT_PYTHON": str(Path(sys.executable).resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _file_identity(info: os.stat_result) -> tuple[object, ...]:
    return tuple(
        getattr(info, field, None)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_nlink",
        )
    )


def _failure_log_tail(target: Path, relative: object) -> str:
    if not isinstance(relative, str):
        return ""
    pure = PurePosixPath(relative)
    expected_prefix = PurePosixPath(".kit/runtime/verification/runs")
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or pure.as_posix() != relative
        or not pure.is_relative_to(expected_prefix)
        or pure.suffix != ".log"
    ):
        return ""
    path = target.joinpath(*pure.parts)
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or int(getattr(before, "st_nlink", 1)) != 1
        ):
            return ""
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(before):
                return ""
            offset = max(0, int(opened.st_size) - FAILURE_LOG_TAIL_BYTES)
            os.lseek(descriptor, offset, os.SEEK_SET)
            content = os.read(descriptor, FAILURE_LOG_TAIL_BYTES)
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = path.lstat()
    except OSError:
        return ""
    if (
        _file_identity(before) != _file_identity(after_handle)
        or _file_identity(after_handle) != _file_identity(after)
        or _is_reparse(after)
    ):
        return ""
    return content.decode("utf-8", errors="replace")


def _failed_stage_evidence(target: Path, payload: object) -> str:
    report = payload.get("report") if isinstance(payload, dict) else None
    stages = report.get("stages") if isinstance(report, dict) else None
    if not isinstance(stages, list):
        return ""
    evidence: list[str] = []
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("status") == "passed":
            continue
        evidence.append(
            f"stage={stage.get('name')!r} status={stage.get('status')!r} "
            f"reason={stage.get('reason')!r}"
        )
        for field in ("stdout_log", "stderr_log"):
            tail = _failure_log_tail(target, stage.get(field))
            if tail:
                evidence.append(f"{field} tail:\n{tail}")
    return "\n".join(evidence)[:FAILURE_EVIDENCE_CHARS]


def _run_public(target: Path, *arguments: str, timeout: int = 900) -> dict[str, Any]:
    completed = subprocess.run(
        _launcher_command(target, *arguments),
        cwd=target,
        env=_public_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"public {' '.join(arguments)} returned invalid JSON; "
            f"stdout={completed.stdout[-1000:]!r}; stderr={completed.stderr[-1000:]!r}"
        ) from exc
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
    ):
        evidence = _failed_stage_evidence(target, payload)
        summary = (
            {
                key: payload.get(key)
                for key in ("ok", "status", "exit_code", "error", "detail")
                if key in payload
            }
            if isinstance(payload, dict)
            else {"payload_type": type(payload).__name__}
        )
        raise AssertionError(
            f"public {' '.join(arguments)} failed ({completed.returncode}): "
            f"summary={summary!r}; stderr={completed.stderr[-1000:]!r}"
            + (f"; failing stage evidence:\n{evidence}" if evidence else "")
        )
    return payload


def _tree_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise AssertionError(f"authenticated core contains a redirect: {relative}")
        if stat.S_ISDIR(info.st_mode):
            snapshot[relative] = "directory"
        elif stat.S_ISREG(info.st_mode):
            content = path.read_bytes()
            snapshot[relative] = (
                f"file:{len(content)}:{hashlib.sha256(content).hexdigest()}"
            )
        else:
            raise AssertionError(f"authenticated core contains an unsafe entry: {relative}")
    return snapshot


def _require_clean(target: Path, label: str) -> None:
    status = lifecycle_support._run_git(
        target, "status", "--porcelain=v1", "--untracked-files=all"
    )
    if status:
        raise AssertionError(f"{label} left the managed project dirty: {status}")


def _require_apply_result(
    runtime: Path, prepared: dict[str, Any], applied: dict[str, Any]
) -> str:
    status = applied.get("kit_change", {}).get("status")
    evidence = applied["kit_change"].get("check_evidence")
    if not isinstance(evidence, dict) or evidence.get("state") != "apply_time":
        raise AssertionError(f"Apply did not retain its real check evidence: {evidence!r}")
    checked = lifecycle_support.controller.load(runtime, prepared["session_id"])
    result = checked.get("check")
    if (
        not isinstance(result, dict)
        or result.get("kit_ok") is not True
        or result.get("existing_issues") != []
    ):
        raise AssertionError(f"Apply result is not a clean project check: {result!r}")
    project_ok = result.get("project_ok")
    expected_status = (
        COMPLETE_APPLY_STATUS if project_ok is True else ADOPTION_APPLY_STATUS
    )
    detail = str(result.get("detail") or "").casefold()
    expected_detail = "checks passed" if project_ok is True else "not ready"
    if (
        (project_ok is not True and project_ok is not False)
        or status != expected_status
        or expected_detail not in detail
    ):
        raise AssertionError(
            "Apply status does not match its explicit project-check result: "
            f"status={status!r}; check={result!r}"
        )
    return expected_status


def _require_strict_result(payload: dict[str, Any], release_sha256: str) -> list[str]:
    if payload.get("strict") is not True or payload.get("status") != "passed":
        raise AssertionError(f"public strict receipt is inconsistent: {payload!r}")
    report = payload.get("report")
    if (
        not isinstance(report, dict)
        or report.get("ok") is not True
        or report.get("status") != "passed"
        or report.get("exit_code") != 0
    ):
        raise AssertionError(f"strict report did not pass: {report!r}")
    stages = report.get("stages")
    if not isinstance(stages, list) or not stages:
        raise AssertionError("strict report contains no stages")
    if any(
        not isinstance(stage, dict) or stage.get("status") != "passed"
        for stage in stages
    ):
        raise AssertionError(f"strict report contains a non-passing stage: {stages!r}")
    names = [str(stage.get("name") or "") for stage in stages]
    if len(names) != len(set(names)):
        raise AssertionError(f"strict stage names are not unique: {names!r}")
    required = {"release-materialize-1", "release-materialize-2"}
    if not required.issubset(names) or any(
        name.startswith("release-build") for name in names
    ):
        raise AssertionError(f"managed strict used the wrong release path: {names!r}")
    reproducibility = next(
        stage for stage in stages if stage.get("name") == "release-reproducibility"
    )
    if reproducibility.get("archive_sha256") != release_sha256:
        raise AssertionError(
            "managed release materialization does not match the installed release"
        )
    gate = payload.get("gate_summary")
    results = gate.get("results") if isinstance(gate, dict) else None
    if (
        not isinstance(gate, dict)
        or gate.get("failed") is not False
        or not isinstance(results, list)
    ):
        raise AssertionError(f"strict gate evidence is incomplete: {gate!r}")
    skips = [
        line
        for line in results
        if isinstance(line, str) and re.match(r"^\s*SKIP(?:\s|$)", line)
    ]
    if skips:
        raise AssertionError(f"managed consumer strict contains skipped checks: {skips!r}")
    return names


def run_managed_consumer_proof() -> dict[str, object]:
    if not (ROOT / ".git").is_dir():
        raise AssertionError("managed consumer proof requires the source Git checkout")
    source_status = lifecycle_support._run_git(
        ROOT, "status", "--porcelain=v1", "--untracked-files=all"
    )
    if source_status:
        raise AssertionError(
            "managed consumer proof requires the already-verified clean source checkout"
        )

    with tempfile.TemporaryDirectory(prefix="managed-consumer-strict-") as temporary:
        scratch = Path(temporary).resolve()
        archive = scratch / "godot-agent-kit.zip"
        extracted = scratch / "release"
        target = scratch / "generic-project"
        runtime = scratch / "controller"
        target.mkdir()
        runtime.mkdir()

        release_report = release.build_release(ROOT, archive)
        verified_report = release.verify_archive(archive)
        release_sha256 = str(release_report.get("archive_sha256") or "")
        if (
            release_report.get("ok") is not True
            or verified_report.get("archive_sha256") != release_sha256
            or release_report.get("source", {}).get("commit")
            != lifecycle_support._run_git(ROOT, "rev-parse", "HEAD")
        ):
            raise AssertionError("current source did not build one verified release")
        lifecycle_support._extract_verified_release(archive, extracted)

        baseline_sha = _initialise_project(target)
        _record_design_backed_change(target, baseline_sha)

        prepared = lifecycle_support._run_release_controller(
            extracted,
            action="prepare",
            runtime=str(runtime),
            target=str(target),
            release=str(extracted),
            mode="install",
        )
        if prepared.get("kit_change", {}).get("status") != "ready":
            raise AssertionError(f"managed install did not prepare: {prepared!r}")
        applied = lifecycle_support._run_release_controller(
            extracted,
            action="apply",
            runtime=str(runtime),
            session_id=prepared["session_id"],
            plan_sha256=prepared["kit_change"]["plan_sha256"],
            real_check=True,
        )
        apply_status = _require_apply_result(runtime, prepared, applied)

        _run_public(target, "setup", "dependency", "gdtoolkit", "--json", timeout=1800)
        _run_public(target, "setup", "import", "--json", timeout=1800)
        lifecycle_support._run_git(target, "add", "--all")
        lifecycle_support._run_git(
            target,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "Install and configure managed kit",
        )
        _require_clean(target, "managed setup commit")

        installation = managed_launcher.resolve_installation(target)
        if (
            installation.mode != "managed"
            or installation.release_sha256 != release_sha256
            or installation.core_root.parent.parent != target / ".agent-kit"
        ):
            raise AssertionError(f"managed release selection is wrong: {installation!r}")
        if os.path.lexists(target.joinpath(*PurePosixPath(MAINTAINER_MARKER).parts)):
            raise AssertionError("managed consumer gained the maintainer fixture marker")
        current_before = (target / kit_change.CURRENT_STATE).read_bytes()
        active_before = _tree_snapshot(installation.core_root)
        active_report, _members = release.read_verified_directory(
            installation.core_root
        )
        if active_report.get("archive_sha256") != release_sha256:
            raise AssertionError("active core identity differs from the built release")

        strict = _run_public(target, "verify", "--strict", "--json", timeout=10800)
        stage_names = _require_strict_result(strict, release_sha256)

        selected_after = managed_launcher.resolve_installation(target)
        after_report, _members = release.read_verified_directory(
            selected_after.core_root
        )
        if (
            selected_after.release_sha256 != release_sha256
            or after_report.get("archive_sha256") != release_sha256
            or (target / kit_change.CURRENT_STATE).read_bytes() != current_before
            or _tree_snapshot(selected_after.core_root) != active_before
        ):
            raise AssertionError("strict verification changed the active release or its bytes")
        doctor = _run_public(target, "doctor", "--json")
        if doctor.get("status") != "ready":
            raise AssertionError(f"public launcher stopped working after strict: {doctor!r}")
        _require_clean(target, "managed strict verification")

        return {
            "apply_status": apply_status,
            "ok": True,
            "platform": sys.platform,
            "release_sha256": release_sha256,
            "strict_stage_count": len(stage_names),
        }


class ManagedConsumerStrictCiContractTests(unittest.TestCase):
    def test_failure_evidence_keeps_the_bounded_unit_test_tail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="managed-consumer-evidence-") as raw:
            target = Path(raw)
            relative = (
                ".kit/runtime/verification/runs/fixture/unit-tests.stderr.log"
            )
            log = target.joinpath(*PurePosixPath(relative).parts)
            log.parent.mkdir(parents=True)
            log.write_text(
                "discarded-prefix\n" + ("x" * FAILURE_LOG_TAIL_BYTES) + "\nFAIL marker\n",
                encoding="utf-8",
                newline="\n",
            )
            payload = {
                "report": {
                    "stages": [
                        {
                            "name": "unit-tests",
                            "status": "failed",
                            "reason": "unit tests exited 1",
                            "stderr_log": relative,
                        }
                    ]
                }
            }

            evidence = _failed_stage_evidence(target, payload)

        self.assertIn("stage='unit-tests' status='failed'", evidence)
        self.assertIn("FAIL marker", evidence)
        self.assertNotIn("discarded-prefix", evidence)
        self.assertLessEqual(len(evidence), FAILURE_EVIDENCE_CHARS)

    def test_driver_is_source_only_and_runs_after_source_strict(self) -> None:
        self.assertIn(DRIVER_RELATIVE, release.SOURCE_ONLY_VALIDATION_FILES)
        self.assertNotIn(DRIVER_RELATIVE, release.VALIDATION_FILES)
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        step = "Prove a managed consumer through strict verification"
        self.assertIn(step, workflow)
        self.assertIn(
            "hashFiles('tools/tests/test_managed_consumer_strict_ci.py') != ''",
            workflow,
        )
        self.assertIn(
            'runpy.run_path("tools/tests/test_managed_consumer_strict_ci.py"',
            workflow,
        )
        self.assertGreater(
            workflow.index(step),
            workflow.index("Run strict production verification through the public kit launcher"),
        )
        self.assertGreater(
            workflow.index(step),
            workflow.index("Run strict production verification through the Windows launcher"),
        )
        self.assertLess(workflow.index(step), workflow.index("Print retained strict evidence"))


if __name__ == "__main__":
    print(json.dumps(run_managed_consumer_proof(), sort_keys=True))
