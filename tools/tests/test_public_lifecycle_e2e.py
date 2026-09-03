#!/usr/bin/env python3
"""Cross-platform proof of the public managed-kit lifecycle.

This file is intentionally source-only.  It builds two authenticated releases,
then uses only their public launchers for install, review, apply, upgrade and
restore.  Godot is disabled for the entire proof.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


TESTS = Path(__file__).resolve().parent
TOOLS = TESTS.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TOOLS))

import kit_change  # noqa: E402
import design  # noqa: E402
import managed_launcher  # noqa: E402
import process_supervisor  # noqa: E402
import release  # noqa: E402
import test_lifecycle_e2e as lifecycle_support  # noqa: E402


DRIVER_RELATIVE = "tools/tests/test_public_lifecycle_e2e.py"
SHA256_LENGTH = 64


def _public_environment(state_root: Path) -> dict[str, str]:
    state_root.mkdir()
    home = state_root / "h"
    temporary = state_root / "t"
    home.mkdir()
    temporary.mkdir()
    environment = process_supervisor.isolated_python_environment()
    for name in (
        managed_launcher.PROJECT_ROOT_ENV,
        managed_launcher.CORE_ROOT_ENV,
        "AGENT_KIT_CONTROLLER_RUNTIME",
        "KIT_NATIVE_RETRY_TOKEN",
        "KIT_VERIFY_AUTH_KEY",
        "KIT_VERIFY_NONCE",
        "KIT_VERIFY_REPOSITORY_SHA256",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "CI": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "HOME": str(home),
            "KIT_ENGINE_DISABLED": "1",
            "KIT_NATIVE_RETRY_TOKEN": "",
            "KIT_PYTHON": str(Path(sys.executable).resolve()),
            "LOCALAPPDATA": str(state_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "XDG_STATE_HOME": str(state_root),
        }
    )
    return environment


def _launcher_command(release_root: Path, *arguments: str) -> list[str]:
    if os.name == "nt":
        return [
            process_supervisor.windows_command_processor(),
            "/d",
            "/c",
            str(release_root / "kit.cmd"),
            *arguments,
        ]
    return [str(release_root / "kit"), *arguments]


def _run_public(
    release_root: Path,
    environment: dict[str, str],
    *arguments: str,
    expected_codes: Iterable[int] = (0,),
    timeout: int = 1800,
) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        _launcher_command(release_root, *arguments),
        cwd=release_root,
        env=environment,
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
            f"stdout={completed.stdout[-1200:]!r}; "
            f"stderr={completed.stderr[-1200:]!r}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("exit_code") != completed.returncode:
        raise AssertionError(
            f"public {' '.join(arguments)} returned an inconsistent receipt: "
            f"code={completed.returncode}; payload={payload!r}"
        )
    allowed = set(expected_codes)
    if completed.returncode not in allowed:
        raise AssertionError(
            f"public {' '.join(arguments)} failed ({completed.returncode}): "
            f"{payload!r}; stderr={completed.stderr[-1200:]!r}"
        )
    return completed.returncode, payload


def _assert_sha256(value: object, label: str) -> str:
    rendered = str(value or "")
    if len(rendered) != SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in rendered
    ):
        raise AssertionError(f"{label} is not a full SHA-256: {rendered!r}")
    return rendered


def _assert_live_review(review_url: object, session_id: str) -> None:
    rendered = str(review_url or "")
    parsed = urllib.parse.urlsplit(rendered)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.path != "/kit-change.html"
        or urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        != {"session": [session_id]}
    ):
        raise AssertionError(f"review URL is not the exact local session: {rendered!r}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(rendered, timeout=10) as response:
        body = response.read().decode("utf-8")
        content_type = response.headers.get_content_type()
    if content_type != "text/html" or "kit-board-token" not in body:
        raise AssertionError("live review did not return the protected HTML cockpit")


def _stop_review(
    release_root: Path, environment: dict[str, str]
) -> None:
    _code, stopped = _run_public(
        release_root, environment, "serve", "stop", "--json", timeout=60
    )
    if stopped.get("ok") is not True:
        raise AssertionError(f"review server did not accept stop: {stopped!r}")
    deadline = time.monotonic() + 10
    while True:
        _code, status = _run_public(
            release_root, environment, "serve", "status", "--json", timeout=60
        )
        if status.get("status") in {"stopped", "already-stopped"}:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"review server did not stop: {status!r}")
        time.sleep(0.1)


def _add_design_fixture(target: Path) -> None:
    lifecycle_support._write(
        target,
        "docs/design/player-experience.md",
        (
            b"# Player experience\n\n"
            b"The player's existing experience must remain unchanged while the kit "
            b"is installed or upgraded.\n"
        ),
    )
    lifecycle_support._run_git(target, "add", "docs/design/player-experience.md")
    lifecycle_support._run_git(
        target,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "Add player experience design",
    )


def _require_ready_change(
    payload: dict[str, Any], *, command: str, session_id: str | None = None
) -> tuple[str, str]:
    if payload.get("ok") is not True or payload.get("command") != command:
        raise AssertionError(f"{command} returned an invalid receipt: {payload!r}")
    actual_session = _assert_sha256(payload.get("session_id"), "session id")
    if session_id is not None and actual_session != session_id:
        raise AssertionError(f"{command} selected a different session")
    state = payload.get("kit_change")
    if not isinstance(state, dict) or state.get("status") != "ready":
        raise AssertionError(f"{command} did not retain a ready plan: {payload!r}")
    plan_sha256 = _assert_sha256(state.get("plan_sha256"), "plan SHA-256")
    return actual_session, plan_sha256


class PublicLifecycleEndToEndTest(unittest.TestCase):
    def test_public_install_upgrade_and_restore_preserve_the_project(self) -> None:
        self.assertTrue(
            (ROOT / ".git").is_dir(),
            "the source-only public lifecycle proof requires source Git authority",
        )

        with lifecycle_support._short_scratch() as scratch:
            source_a = scratch / "sa"
            source_b = scratch / "sb"
            archive_a = scratch / "a.zip"
            archive_b = scratch / "b.zip"
            release_a = scratch / "ra"
            release_b = scratch / "rb"
            target = scratch / "p"
            target.mkdir()

            lifecycle_support._make_clean_release_source(source_a)
            lifecycle_support._set_release_version(source_a, "0.3.0")
            report_a = release.build_release(source_a, archive_a)
            lifecycle_support._extract_verified_release(archive_a, release_a)

            lifecycle_support._make_clean_release_source(source_b)
            lifecycle_support._set_release_version(source_b, "0.3.1")
            lifecycle_support._change_release_b_provider_blocks(source_b)
            report_b = release.build_release(source_b, archive_b)
            lifecycle_support._extract_verified_release(archive_b, release_b)

            digest_a = _assert_sha256(report_a.get("archive_sha256"), "release A")
            digest_b = _assert_sha256(report_b.get("archive_sha256"), "release B")
            self.assertNotEqual(digest_a, digest_b)
            self.assertEqual(
                digest_a, release.read_verified_directory(release_a)[0]["archive_sha256"]
            )
            self.assertEqual(
                digest_b, release.read_verified_directory(release_b)[0]["archive_sha256"]
            )

            lifecycle_support._copy_project_fixture(target)
            _add_design_fixture(target)
            original_project = lifecycle_support._tree_snapshot(target)
            original_guarded = lifecycle_support._guarded_snapshot(target)

            environment_a = _public_environment(scratch / "s1")
            environment_b = _public_environment(scratch / "s2")
            cleanup_errors: list[str] = []
            try:
                _code, installed = _run_public(
                    release_a, environment_a, "install", str(target), "--json"
                )
                session_a, plan_a = _require_ready_change(
                    installed, command="install"
                )
                self.assertEqual(False, installed.get("project_files_changed"))
                self.assertEqual(original_project, lifecycle_support._tree_snapshot(target))
                _assert_live_review(installed.get("review_url"), session_a)

                _code, listed_a = _run_public(
                    release_a, environment_a, "change", "list", "--json"
                )
                listed_ids = {
                    str(item.get("session_id") or "")
                    for item in listed_a.get("sessions", [])
                    if isinstance(item, dict)
                }
                self.assertIn(session_a, listed_ids)
                _code, status_a = _run_public(
                    release_a,
                    environment_a,
                    "change",
                    "status",
                    session_a,
                    "--json",
                )
                _require_ready_change(
                    status_a, command="change status", session_id=session_a
                )
                self.assertEqual(original_project, lifecycle_support._tree_snapshot(target))

                _code, applied_a = _run_public(
                    release_a,
                    environment_a,
                    "change",
                    "apply",
                    session_a,
                    "--plan-sha256",
                    plan_a,
                    "--json",
                )
                state_a = applied_a.get("kit_change")
                self.assertTrue(applied_a.get("ok"), applied_a)
                self.assertIn(
                    state_a.get("status") if isinstance(state_a, dict) else None,
                    {"complete", "adoption_required"},
                    applied_a,
                )
                self.assertEqual(original_guarded, lifecycle_support._guarded_snapshot(target))
                current = target.joinpath(*kit_change.CURRENT_STATE.split("/"))
                current_a = current.read_bytes()
                installed_a = lifecycle_support._tree_snapshot(
                    target, exclude_top=frozenset({".kit"})
                )
                selected_a = managed_launcher.resolve_installation(target)
                self.assertEqual(digest_a, selected_a.release_sha256)
                _stop_review(release_a, environment_a)

                before_upgrade = lifecycle_support._tree_snapshot(target)
                _code, upgraded = _run_public(
                    release_b, environment_b, "upgrade", str(target), "--json"
                )
                session_b, plan_b = _require_ready_change(
                    upgraded, command="upgrade"
                )
                self.assertEqual(False, upgraded.get("project_files_changed"))
                self.assertEqual(before_upgrade, lifecycle_support._tree_snapshot(target))
                _assert_live_review(upgraded.get("review_url"), session_b)

                _code, applied_b = _run_public(
                    release_b,
                    environment_b,
                    "change",
                    "apply",
                    session_b,
                    "--plan-sha256",
                    plan_b,
                    "--json",
                )
                state_b = applied_b.get("kit_change")
                self.assertTrue(applied_b.get("ok"), applied_b)
                self.assertIn(
                    state_b.get("status") if isinstance(state_b, dict) else None,
                    {"complete", "adoption_required"},
                    applied_b,
                )
                self.assertEqual(original_guarded, lifecycle_support._guarded_snapshot(target))
                selected_b = managed_launcher.resolve_installation(target)
                self.assertEqual(digest_b, selected_b.release_sha256)

                result_b = _assert_sha256(
                    state_b.get("result_sha256") if isinstance(state_b, dict) else None,
                    "upgrade result SHA-256",
                )
                _code, restored = _run_public(
                    release_b,
                    environment_b,
                    "change",
                    "restore",
                    session_b,
                    "--result-sha256",
                    result_b,
                    "--json",
                )
                self.assertTrue(restored.get("ok"), restored)
                restored_state = restored.get("kit_change")
                self.assertEqual(
                    "restored",
                    restored_state.get("status")
                    if isinstance(restored_state, dict)
                    else None,
                )
                self.assertEqual(current_a, current.read_bytes())
                self.assertEqual(
                    installed_a,
                    lifecycle_support._tree_snapshot(
                        target, exclude_top=frozenset({".kit"})
                    ),
                )
                self.assertEqual(original_guarded, lifecycle_support._guarded_snapshot(target))
                selected_restored = managed_launcher.resolve_installation(target)
                self.assertEqual(digest_a, selected_restored.release_sha256)
                _stop_review(release_b, environment_b)

                doctor_code, doctor = _run_public(
                    target,
                    environment_a,
                    "doctor",
                    "--json",
                    expected_codes=(0, 3),
                )
                self.assertEqual("doctor", doctor.get("command"))
                self.assertEqual(
                    ("ready", True) if doctor_code == 0 else ("needs_setup", False),
                    (doctor.get("status"), doctor.get("ok")),
                )
                static_code, static = _run_public(
                    target,
                    environment_a,
                    "verify",
                    "--static",
                    "--json",
                    expected_codes=(0, 1),
                )
                self.assertEqual("verify", static.get("command"))
                self.assertIs(static.get("static"), True)
                self.assertIsNone(static.get("engine"))
                self.assertEqual(
                    ("passed", True) if static_code == 0 else ("failed", False),
                    (static.get("status"), static.get("ok")),
                )
                self.assertIsInstance(static.get("gate_summary"), dict)

                after_static = lifecycle_support._guarded_snapshot(target)
                self.assertEqual(original_guarded["src"], after_static["src"])
                self.assertEqual(original_guarded[".git"], after_static[".git"])
                current_design = dict(after_static["docs/design"])
                index_record = current_design.pop("INDEX.md", None)
                self.assertEqual(
                    original_guarded["docs/design"],
                    current_design,
                    "normal static verification changed authored design authority",
                )
                index_path = target / "docs" / "design" / "INDEX.md"
                self.assertIsNotNone(index_record)
                index_stat = index_path.lstat()
                self.assertTrue(stat.S_ISREG(index_stat.st_mode))
                self.assertEqual(1, int(getattr(index_stat, "st_nlink", 1)))
                parsed_design = design.parse_design(
                    target / "docs" / "design" / "player-experience.md",
                    design_root=target / "docs" / "design",
                )
                expected_index = design.build_index([parsed_design], {}, [])
                self.assertEqual(expected_index.encode("utf-8"), index_path.read_bytes())

                self.assertEqual(
                    digest_a,
                    release.read_verified_directory(release_a)[0]["archive_sha256"],
                )
                self.assertEqual(
                    digest_b,
                    release.read_verified_directory(release_b)[0]["archive_sha256"],
                )
            finally:
                for release_root, environment in (
                    (release_a, environment_a),
                    (release_b, environment_b),
                ):
                    if not release_root.is_dir():
                        continue
                    try:
                        _stop_review(release_root, environment)
                    except (AssertionError, OSError, subprocess.SubprocessError) as exc:
                        cleanup_errors.append(f"{release_root.name}: {exc}")
            self.assertEqual([], cleanup_errors, "review cleanup failed")

    def test_public_lifecycle_proof_is_source_only(self) -> None:
        self.assertIn(DRIVER_RELATIVE, release.SOURCE_ONLY_VALIDATION_FILES)
        self.assertNotIn(DRIVER_RELATIVE, release.VALIDATION_FILES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
