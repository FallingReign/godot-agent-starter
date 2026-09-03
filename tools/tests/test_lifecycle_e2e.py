#!/usr/bin/env python3
"""Real offline install/use/restore proof for archive and extracted releases."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Iterator


TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import brownfield  # noqa: E402
import kit_change  # noqa: E402
import kit_change_controller as controller  # noqa: E402
import process_supervisor  # noqa: E402
import release  # noqa: E402


_CONTROLLER_RESULT_PREFIX = "__KIT_LIFECYCLE_CONTROLLER_RESULT__="
_WINDOWS_E2E_SCRATCH_CHARS = 96
_CONTROLLER_DRIVER = r"""
import json
import os
import subprocess
import sys
from pathlib import Path

release_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(release_root / "tools"))
import kit_change_controller as controller
import managed_launcher
import process_supervisor

request = json.loads(sys.argv[2])
runtime = Path(request["runtime"])
action = request["action"]
if action == "prepare":
    result = controller.prepare(
        runtime,
        Path(request["target"]),
        Path(request["release"]),
        request["mode"],
    )
elif action == "apply":
    def passed(_target, _session):
        return {
            "kit_ok": True,
            "project_ok": True,
            "existing_issues": [],
            "detail": "Injected lifecycle check passed without starting Godot.",
        }

    def public_static(checked_target, _session):
        if checked_target.resolve() != Path(request["target"]).resolve():
            raise AssertionError("lifecycle check received a different target")
        environment = process_supervisor.isolated_python_environment(
            {
                "KIT_ENGINE_DISABLED": "1",
                "KIT_LIFECYCLE_CHECK": "1",
                "KIT_NATIVE_RETRY_TOKEN": "",
                "KIT_PYTHON": process_supervisor.isolated_python_executable(
                    sys.executable
                ),
                "KIT_VERIFY_AUTH_KEY": "",
                "KIT_VERIFY_NONCE": "",
                "KIT_VERIFY_REPOSITORY_SHA256": "",
            }
        )
        environment.pop(managed_launcher.PROJECT_ROOT_ENV, None)
        environment.pop(managed_launcher.CORE_ROOT_ENV, None)
        commands = (("doctor", "--json"), ("verify", "--static", "--json"))
        for arguments in commands:
            if os.name == "nt":
                command = [
                    process_supervisor.windows_command_processor(),
                    "/d",
                    "/c",
                    str(checked_target / "kit.cmd"),
                    *arguments,
                ]
            else:
                command = [str(checked_target / "kit"), *arguments]
            completed = subprocess.run(
                command,
                cwd=checked_target,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
            try:
                payload = json.loads(completed.stdout.lstrip("\ufeff"))
            except json.JSONDecodeError:
                payload = None
            valid = False
            if isinstance(payload, dict) and payload.get("command") == arguments[0]:
                if arguments[0] == "doctor":
                    valid = (
                        completed.returncode == 0
                        and payload.get("ok") is True
                        and payload.get("status") == "ready"
                    ) or (
                        completed.returncode == 3
                        and payload.get("ok") is False
                        and payload.get("status") == "needs_setup"
                    )
                else:
                    valid = (
                        completed.returncode == 0
                        and payload.get("ok") is True
                        and payload.get("status") == "passed"
                    )
            if not valid:
                detail = " ".join(
                    (completed.stderr or completed.stdout or "invalid response").split()
                )[:500]
                return {
                    "kit_ok": False,
                    "project_ok": False,
                    "existing_issues": [],
                    "detail": (
                        f"Public {' '.join(arguments)} failed with "
                        f"exit {completed.returncode}: {detail}"
                    )[:1000],
                }
        return {
            "kit_ok": True,
            "project_ok": True,
            "existing_issues": [],
            "detail": "Public doctor response and static verification passed.",
        }

    hook = public_static if request.get("public_check") else passed
    result = controller.apply(
        runtime,
        request["session_id"],
        request["plan_sha256"],
        post_apply_check=None if request.get("real_check") else hook,
    )
elif action == "restore":
    result = controller.restore(
        runtime,
        request["session_id"],
        request["result_sha256"],
    )
else:
    raise RuntimeError(f"unsupported controller action: {action}")
print(
    "__KIT_LIFECYCLE_CONTROLLER_RESULT__="
    + json.dumps(result, ensure_ascii=True, sort_keys=True)
)
"""


def _scratch_identity(path: Path) -> tuple[object, ...]:
    info = path.lstat()
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or int(getattr(info, "st_file_attributes", 0)) & marker
        or path.resolve(strict=True) != path
    ):
        raise AssertionError(f"lifecycle scratch is redirected: {path}")
    return tuple(
        getattr(info, field, None)
        for field in ("st_dev", "st_ino", "st_mode", "st_file_attributes")
    )


def _remove_readonly(
    function: object,
    path: str,
    _error: tuple[type[BaseException], BaseException, object],
) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)  # type: ignore[operator]


@contextlib.contextmanager
def _short_scratch() -> Iterator[Path]:
    configured = os.environ.get("KIT_TEST_TMPDIR", "").strip()
    parent = Path(configured or tempfile.gettempdir()).resolve(strict=True)
    scratch = Path(tempfile.mkdtemp(prefix="e-", dir=parent)).resolve(strict=True)
    identity = _scratch_identity(scratch)
    try:
        if scratch.parent != parent or not scratch.name.startswith("e-"):
            raise AssertionError("lifecycle scratch escaped its private parent")
        if os.name == "nt" and len(str(scratch)) > _WINDOWS_E2E_SCRATCH_CHARS:
            raise AssertionError("lifecycle scratch path is too long for Windows")
        yield scratch
    finally:
        if (
            scratch.parent != parent
            or not scratch.name.startswith("e-")
            or _scratch_identity(scratch) != identity
        ):
            raise AssertionError("refusing to remove a changed lifecycle scratch")
        shutil.rmtree(scratch, onerror=_remove_readonly)
        if os.path.lexists(scratch):
            raise AssertionError("lifecycle scratch cleanup was incomplete")


def _write(root: Path, relative: str, content: bytes) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _run_git(root: Path, *arguments: str) -> str:
    null_hooks = "NUL" if os.name == "nt" else "/dev/null"
    executable = process_supervisor.resolve_ordinary_executable(
        "git", excluded_roots=(root, ROOT)
    )
    completed = subprocess.run(
        [
            executable,
            "-C",
            str(root),
            "-c",
            f"core.hooksPath={null_hooks}",
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AssertionError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def _clone_exact_commit(destination: Path, commit: str) -> None:
    executable = process_supervisor.resolve_ordinary_executable(
        "git", excluded_roots=(ROOT, destination.parent)
    )
    completed = subprocess.run(
        [
            executable,
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--quiet",
            "--no-checkout",
            "--no-hardlinks",
            str(ROOT),
            str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AssertionError(f"local Git clone failed: {detail}")
    _run_git(destination, "checkout", "--quiet", "--detach", commit)
    if _run_git(destination, "rev-parse", "HEAD") != commit:
        raise AssertionError("historic release checkout selected a different commit")


def _build_exact_legacy_release(source: Path, archive: Path) -> None:
    command = process_supervisor.isolated_python_script_command(
        Path(sys.executable).resolve(),
        source / "tools" / "release.py",
        source,
        "build",
        str(archive),
    )
    completed = subprocess.run(
        command,
        cwd=source,
        env=process_supervisor.isolated_python_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AssertionError(f"historic release build failed: {detail}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != release.LEGACY_0_2_0_ARCHIVE_SHA256:
        raise AssertionError(f"historic release identity changed: {digest}")


def _managed_block_sources(archive: Path) -> dict[str, bytes]:
    _report, members = release.read_verified_archive(archive)
    manifest = json.loads(members[release.INSTALL_MANIFEST_PATH].content.decode("utf-8"))
    return {
        str(item["path"]): members[str(item["source"])].content
        for item in manifest["managed_blocks"]
    }


def _make_clean_release_source(destination: Path) -> None:
    _version, _legal, files = release.collect_files(ROOT)
    destination.mkdir()
    for item in files:
        path = _write(destination, item.path, item.content)
        path.chmod(item.mode)
    _run_git(destination, "init", "--quiet")
    _run_git(destination, "config", "user.name", "Kit lifecycle test")
    _run_git(destination, "config", "user.email", "kit-lifecycle@example.invalid")
    _run_git(destination, "add", "--all")
    _run_git(
        destination,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )


def _set_release_version(root: Path, version: str) -> None:
    content = f"{version}\n".encode("utf-8")
    if (root / "VERSION").read_bytes() == content:
        return
    _write(root, "VERSION", content)
    _run_git(root, "add", "VERSION")
    _run_git(
        root,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        f"version {version}",
    )


def _change_release_b_provider_blocks(root: Path) -> None:
    paths = ("AGENTS.md", ".github/copilot-instructions.md")
    for relative in paths:
        content = root.joinpath(*relative.split("/")).read_bytes()
        first_line, separator, remainder = content.partition(b"\n")
        if not separator:
            raise AssertionError(f"provider block has no first line: {relative}")
        _write(
            root,
            relative,
            first_line
            + separator
            + b"<!-- Lifecycle release B provider contract. -->\n"
            + remainder,
        )
    _run_git(root, "add", *paths)
    _run_git(
        root,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "change provider instructions",
    )


def _seed_empty_baseline(target: Path) -> bytes:
    binding = brownfield.read_install_binding(target)
    content = brownfield.canonical_json(
        brownfield.build_baseline(
            target,
            [],
            installation_id=binding["installation_id"],
            release_sha256=binding["release_sha256"],
        )
    )
    _write(target, brownfield.BASELINE_RELATIVE, content)
    return content


def _extract_verified_release(
    archive: Path, destination: Path, *, verify_directory: bool = True
) -> None:
    _report, members = release.read_verified_archive(archive)
    destination.mkdir()
    for relative, member in sorted(members.items()):
        path = _write(destination, relative, member.content)
        if member.mode is not None:
            path.chmod(member.mode)
    if verify_directory:
        release.read_verified_directory(destination)


def _write_game_fixture(destination: Path) -> None:
    _write(
        destination,
        "src/project.godot",
        b'[application]\nconfig/name="Lifecycle fixture"\n',
    )
    _write(
        destination,
        "src/custom/avatar.gd",
        b"class_name LifecycleFixtureAvatar\nextends RefCounted\n",
    )


def _copy_project_fixture(destination: Path) -> None:
    _write_game_fixture(destination)
    _write(destination, "AGENTS.md", b"# Project guidance\nKeep the game readable.\n")
    _write(
        destination,
        ".github/copilot-instructions.md",
        b"# Project Copilot guidance\nPreserve the player experience.\n",
    )
    _write(destination, ".gitattributes", b"# Project attributes\n*.story text\n")
    _write(destination, ".gitignore", b"# Project ignores\n.godot/\n.kit/\n")
    _run_git(destination, "init", "--quiet")
    _run_git(destination, "config", "user.name", "Kit lifecycle test")
    _run_git(destination, "config", "user.email", "kit-lifecycle@example.invalid")
    _run_git(destination, "add", "--all")
    _run_git(
        destination,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "game",
    )


def _tree_snapshot(
    root: Path, *, exclude_top: frozenset[str] = frozenset()
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in exclude_top:
            continue
        name = relative.as_posix()
        if path.is_symlink():
            snapshot[name] = f"link:{os.readlink(path)}"
        elif path.is_dir():
            snapshot[name] = "directory"
        else:
            content = path.read_bytes()
            snapshot[name] = f"file:{len(content)}:{hashlib.sha256(content).hexdigest()}"
    return snapshot


def _guarded_snapshot(root: Path) -> dict[str, dict[str, str]]:
    return {
        relative: _tree_snapshot(root / relative)
        for relative in ("src", "docs/design", ".git")
    }


def _run_release_controller(
    release_root: Path, **request: object
) -> dict[str, object]:
    environment = process_supervisor.isolated_python_environment(
        {
            "KIT_ENGINE_DISABLED": "1",
            "KIT_NATIVE_RETRY_TOKEN": "",
            "KIT_VERIFY_AUTH_KEY": "",
            "KIT_VERIFY_NONCE": "",
            "KIT_VERIFY_REPOSITORY_SHA256": "",
        }
    )
    completed = subprocess.run(
        [
            str(Path(sys.executable).resolve()),
            "-B",
            "-I",
            "-S",
            "-c",
            _CONTROLLER_DRIVER,
            str(release_root),
            json.dumps(request, ensure_ascii=True, sort_keys=True),
        ],
        cwd=release_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800 if request.get("real_check") else 180,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AssertionError(
            f"release controller {request.get('action')} failed "
            f"({completed.returncode}): {detail}"
        )
    encoded_results = [
        line.removeprefix(_CONTROLLER_RESULT_PREFIX)
        for line in completed.stdout.splitlines()
        if line.startswith(_CONTROLLER_RESULT_PREFIX)
    ]
    if len(encoded_results) != 1:
        raise AssertionError(
            f"release controller {request.get('action')} returned "
            f"{len(encoded_results)} marked receipts: {completed.stdout[:500]}"
        )
    try:
        payload = json.loads(encoded_results[0])
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"release controller {request.get('action')} returned invalid JSON: "
            f"{completed.stdout[:500]}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        child_output = completed.stdout.split(_CONTROLLER_RESULT_PREFIX, 1)[0].strip()
        stdout_tail = child_output[-4000:]
        stderr_tail = completed.stderr.strip()[-4000:]
        raise AssertionError(
            f"release controller {request.get('action')} returned a failing receipt: "
            f"{payload}; child stdout tail: {stdout_tail}; "
            f"child stderr tail: {stderr_tail}"
        )
    return payload


class ManagedLifecycleEndToEndTest(unittest.TestCase):
    def test_short_scratch_is_bounded_and_removed(self) -> None:
        retained: Path | None = None
        with _short_scratch() as scratch:
            retained = scratch
            (scratch / "probe").write_text("ok\n", encoding="utf-8")
            nested_dispatch = (
                scratch
                / "project"
                / ".kit"
                / "runtime"
                / "dispatch"
                / "workspaces"
                / ("run-" + "a" * 32)
            )
            self.assertLessEqual(
                len(str(scratch)),
                _WINDOWS_E2E_SCRATCH_CHARS if os.name == "nt" else len(str(scratch)),
            )
            if os.name == "nt":
                self.assertLess(len(str(nested_dispatch)), 260)
        assert retained is not None
        self.assertFalse(os.path.lexists(retained))

    def test_managed_upgrade_restores_exact_prior_release(self) -> None:
        if not (ROOT / ".git").exists():
            self.skipTest("source Git authority is absent in an installed release")

        with _short_scratch() as scratch:
            source_a = scratch / "source-a"
            source_b = scratch / "source-b"
            archive_a = scratch / "kit-a.zip"
            archive_b = scratch / "kit-b.zip"
            extracted_a = scratch / "release-a"
            extracted_b = scratch / "release-b"
            target = scratch / "project"
            runtime_a = scratch / "controller-a"
            runtime_b = scratch / "controller-b"
            runtime_a.mkdir()
            runtime_b.mkdir()

            _make_clean_release_source(source_a)
            _set_release_version(source_a, "0.3.0")
            report_a = release.build_release(source_a, archive_a)
            _extract_verified_release(archive_a, extracted_a)
            managed_blocks_a = _managed_block_sources(archive_a)

            _make_clean_release_source(source_b)
            _set_release_version(source_b, "0.3.1")
            _change_release_b_provider_blocks(source_b)
            report_b = release.build_release(source_b, archive_b)
            _extract_verified_release(archive_b, extracted_b)
            managed_blocks_b = _managed_block_sources(archive_b)

            self.assertEqual("0.3.0", report_a["version"])
            self.assertEqual("0.3.1", report_b["version"])
            self.assertNotEqual(
                report_a["archive_sha256"], report_b["archive_sha256"]
            )

            target.mkdir()
            _copy_project_fixture(target)
            guarded = _guarded_snapshot(target)
            human_text = {
                path: target.joinpath(*path.split("/")).read_bytes()
                for path in (
                    "AGENTS.md",
                    ".github/copilot-instructions.md",
                    ".gitattributes",
                    ".gitignore",
                )
            }

            prepared_a = _run_release_controller(
                extracted_a,
                action="prepare",
                runtime=str(runtime_a),
                target=str(target),
                release=str(extracted_a),
                mode="install",
            )
            self.assertEqual("ready", prepared_a["kit_change"]["status"])
            applied_a = _run_release_controller(
                extracted_a,
                action="apply",
                runtime=str(runtime_a),
                session_id=prepared_a["session_id"],
                plan_sha256=prepared_a["kit_change"]["plan_sha256"],
            )
            self.assertEqual("complete", applied_a["kit_change"]["status"])
            self.assertEqual(guarded, _guarded_snapshot(target))
            self.assertIn(
                b'm0["custom"]',
                (target / "ARCHITECTURE.md").read_bytes(),
            )

            current_path = target / kit_change.CURRENT_STATE
            current_a_bytes = current_path.read_bytes()
            current_a = json.loads(current_a_bytes)
            self.assertEqual("0.3.0", current_a["active_release"]["kit_version"])
            self.assertEqual(
                report_a["archive_sha256"],
                current_a["active_release"]["archive_sha256"],
            )
            bridge_content_a: dict[str, bytes] = {}
            for path, before in human_text.items():
                content = target.joinpath(*path.split("/")).read_bytes()
                self.assertTrue(content.startswith(before), path)
                self.assertEqual(1, content.count(managed_blocks_a[path]), path)
                if path in {"AGENTS.md", ".github/copilot-instructions.md"}:
                    bridge_content_a[path] = content
            core_a = target.joinpath(
                *current_a["active_release"]["core_path"].split("/")
            )
            provider_core_a = {
                path: core_a.joinpath(*path.split("/")).read_bytes()
                for path in bridge_content_a
            }
            baseline_a_bytes = _seed_empty_baseline(target)
            public_a = _tree_snapshot(
                target,
                exclude_top=frozenset({".kit"}),
            )

            prepared_b = _run_release_controller(
                extracted_b,
                action="prepare",
                runtime=str(runtime_b),
                target=str(target),
                release=str(extracted_b),
                mode="upgrade",
            )
            self.assertEqual("ready", prepared_b["kit_change"]["status"])
            self.assertEqual("0.3.0", prepared_b["kit_change"]["current_version"])
            self.assertEqual("0.3.1", prepared_b["kit_change"]["incoming_version"])
            self.assertEqual(
                public_a,
                _tree_snapshot(target, exclude_top=frozenset({".kit"})),
            )

            applied_b = _run_release_controller(
                extracted_b,
                action="apply",
                runtime=str(runtime_b),
                session_id=prepared_b["session_id"],
                plan_sha256=prepared_b["kit_change"]["plan_sha256"],
                real_check=True,
            )
            self.assertIn(
                applied_b["kit_change"]["status"],
                {"complete", "adoption_required"},
            )
            self.assertEqual(
                "apply_time",
                applied_b["kit_change"]["check_evidence"]["state"],
            )
            self.assertTrue(
                applied_b["kit_change"]["check_evidence"]["checked_at"]
            )
            checked_b = controller.load(runtime_b, prepared_b["session_id"])
            self.assertTrue(checked_b["check"]["kit_ok"], checked_b["check"])
            self.assertEqual([], checked_b["check"]["existing_issues"])
            if applied_b["kit_change"]["status"] == "complete":
                self.assertTrue(
                    checked_b["check"]["project_ok"], checked_b["check"]
                )
                self.assertTrue(
                    checked_b["check"]["detail"].startswith(
                        "Offline kit and project checks passed"
                    ),
                    checked_b["check"]["detail"],
                )
            else:
                self.assertFalse(
                    checked_b["check"]["project_ok"], checked_b["check"]
                )
                prefix = "Kit works; these project checks are not ready: "
                check_detail = checked_b["check"]["detail"]
                self.assertTrue(check_detail.startswith(prefix), check_detail)
                self.assertTrue(check_detail.removeprefix(prefix).rstrip("."))
            current_b = json.loads(current_path.read_bytes())
            self.assertEqual("0.3.1", current_b["active_release"]["kit_version"])
            self.assertEqual(
                report_b["archive_sha256"],
                current_b["active_release"]["archive_sha256"],
            )
            self.assertEqual(
                report_a["archive_sha256"],
                current_b["previous_release"]["archive_sha256"],
            )
            core_b = target.joinpath(
                *current_b["active_release"]["core_path"].split("/")
            )
            self.assertEqual(guarded, _guarded_snapshot(target))
            baseline_b_bytes = target.joinpath(
                *brownfield.BASELINE_RELATIVE.split("/")
            ).read_bytes()
            self.assertNotEqual(baseline_a_bytes, baseline_b_bytes)
            self.assertEqual(
                hashlib.sha256(baseline_b_bytes).hexdigest(),
                checked_b["check"]["baseline_sha256"],
            )
            baseline_b = json.loads(baseline_b_bytes)
            self.assertEqual(
                report_b["archive_sha256"],
                baseline_b["binding"]["release_sha256"],
            )
            for path, before in human_text.items():
                content = target.joinpath(*path.split("/")).read_bytes()
                self.assertTrue(content.startswith(before), path)
                self.assertEqual(1, content.count(managed_blocks_b[path]), path)
                if path in bridge_content_a:
                    self.assertEqual(
                        managed_blocks_a[path], managed_blocks_b[path]
                    )
                    self.assertEqual(bridge_content_a[path], content, path)
                    active_content = core_b.joinpath(*path.split("/")).read_bytes()
                    self.assertNotEqual(provider_core_a[path], active_content, path)
                    self.assertIn(
                        b"Lifecycle release B provider contract.",
                        active_content,
                        path,
                    )

            restored = _run_release_controller(
                extracted_b,
                action="restore",
                runtime=str(runtime_b),
                session_id=prepared_b["session_id"],
                result_sha256=applied_b["kit_change"]["result_sha256"],
            )
            self.assertEqual("restored", restored["kit_change"]["status"])
            self.assertEqual(current_a_bytes, current_path.read_bytes())
            self.assertEqual(
                public_a,
                _tree_snapshot(target, exclude_top=frozenset({".kit"})),
            )
            self.assertEqual(guarded, _guarded_snapshot(target))

    def test_verified_archive_and_exact_extracted_release_install_use_and_restore(
        self,
    ) -> None:
        if not (ROOT / ".git").exists():
            self.skipTest("source Git authority is absent in an installed release")

        with _short_scratch() as scratch:
            source = scratch / "source"
            archive = scratch / "godot-agent-kit.zip"
            extracted = scratch / "extracted"
            _make_clean_release_source(source)
            release_report = release.build_release(source, archive)
            self.assertTrue(release_report["ok"])
            _extract_verified_release(archive, extracted)
            managed_blocks = _managed_block_sources(archive)

            for label, release_source in (("archive", archive), ("extracted", extracted)):
                with self.subTest(release_source=label):
                    target = scratch / f"target-{label}"
                    runtime = scratch / f"controller-{label}"
                    target.mkdir()
                    runtime.mkdir()
                    _copy_project_fixture(target)
                    original = _tree_snapshot(target)
                    guarded = _guarded_snapshot(target)
                    human_text = {
                        path: target.joinpath(*path.split("/")).read_bytes()
                        for path in (
                            "AGENTS.md",
                            ".github/copilot-instructions.md",
                        )
                    }
                    release_before = (
                        _tree_snapshot(extracted) if label == "extracted" else None
                    )
                    archive_before = archive.read_bytes() if label == "archive" else None

                    prepared = _run_release_controller(
                        extracted,
                        action="prepare",
                        runtime=str(runtime),
                        target=str(target),
                        release=str(release_source),
                        mode="install",
                    )
                    self.assertEqual("ready", prepared["kit_change"]["status"])
                    reviewed = controller.status(runtime, prepared["session_id"])
                    self.assertEqual(
                        prepared["kit_change"]["plan_sha256"],
                        reviewed["kit_change"]["plan_sha256"],
                    )
                    self.assertEqual(original, _tree_snapshot(target))
                    if release_before is not None:
                        self.assertEqual(release_before, _tree_snapshot(extracted))
                    if archive_before is not None:
                        self.assertEqual(archive_before, archive.read_bytes())

                    applied = _run_release_controller(
                        extracted,
                        action="apply",
                        runtime=str(runtime),
                        target=str(target),
                        session_id=prepared["session_id"],
                        plan_sha256=prepared["kit_change"]["plan_sha256"],
                        public_check=True,
                    )
                    self.assertEqual(
                        "complete", applied["kit_change"]["status"], applied
                    )
                    self.assertEqual(
                        "Public doctor response and static verification passed.",
                        controller.load(runtime, prepared["session_id"])["check"][
                            "detail"
                        ],
                    )
                    self.assertEqual(guarded, _guarded_snapshot(target))
                    self.assertIn(
                        b'm0["custom"]',
                        (target / "ARCHITECTURE.md").read_bytes(),
                    )
                    for path, before in human_text.items():
                        content = target.joinpath(*path.split("/")).read_bytes()
                        self.assertTrue(content.startswith(before), path)
                        self.assertEqual(1, content.count(managed_blocks[path]), path)

                    restored = _run_release_controller(
                        extracted,
                        action="restore",
                        runtime=str(runtime),
                        session_id=prepared["session_id"],
                        result_sha256=applied["kit_change"]["result_sha256"],
                    )
                    self.assertEqual(
                        "restored", restored["kit_change"]["status"], restored
                    )
                    self.assertFalse(os.path.lexists(target / ".agent-kit"))
                    self.assertEqual(
                        original,
                        _tree_snapshot(target, exclude_top=frozenset({".kit"})),
                    )
                    if release_before is not None:
                        self.assertEqual(release_before, _tree_snapshot(extracted))
                    if archive_before is not None:
                        self.assertEqual(archive_before, archive.read_bytes())

    def test_exact_legacy_0_2_0_migrates_checks_and_restores(self) -> None:
        self.assertTrue(
            (ROOT / ".git").exists(),
            "the source-only legacy lifecycle proof requires Git history",
        )

        self.assertTrue(release.LEGACY_0_2_0_SOURCE_COMMIT.startswith("17b1ecb"))
        self.assertTrue(release.LEGACY_0_2_0_ARCHIVE_SHA256.startswith("b5a2f7"))
        with _short_scratch() as scratch:
            historic_source = scratch / "historic-source"
            historic_archive = scratch / "godot-agent-kit-0.2.0.zip"
            current_source = scratch / "current-source"
            current_archive = scratch / "godot-agent-kit-current.zip"
            current_release = scratch / "current-release"
            target = scratch / "legacy-project"
            runtime = scratch / "controller"
            runtime.mkdir()

            _clone_exact_commit(
                historic_source,
                release.LEGACY_0_2_0_SOURCE_COMMIT,
            )
            _build_exact_legacy_release(historic_source, historic_archive)
            historic_report = release.verify_archive(historic_archive)
            self.assertEqual("0.2.0", historic_report["version"])
            self.assertEqual(
                release.LEGACY_0_2_0_SOURCE_COMMIT,
                historic_report["source"]["commit"],
            )

            _make_clean_release_source(current_source)
            release.build_release(current_source, current_archive)
            _extract_verified_release(current_archive, current_release)
            managed_blocks = _managed_block_sources(current_archive)
            # The exact 0.2.0 ZIP is pinned by its historic container digest.
            # Once extracted, it is a legacy project input rather than a
            # current release directory and must not claim current templates.
            _extract_verified_release(
                historic_archive, target, verify_directory=False
            )
            _write_game_fixture(target)
            _run_git(target, "init", "--quiet")
            _run_git(target, "config", "user.name", "Kit lifecycle test")
            _run_git(
                target,
                "config",
                "user.email",
                "kit-lifecycle@example.invalid",
            )
            _run_git(target, "add", "--all")
            _run_git(
                target,
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "-m",
                "legacy game",
            )

            old_manifest = target / kit_change.RELEASE_MANIFEST
            self.assertEqual(
                kit_change.LEGACY_0_2_0_RELEASE_MANIFEST_SHA256,
                hashlib.sha256(old_manifest.read_bytes()).hexdigest(),
            )
            self.assertTrue((target / "check.py").is_file())
            original = _tree_snapshot(target, exclude_top=frozenset({".kit"}))
            guarded = _guarded_snapshot(target)

            prepared = _run_release_controller(
                current_release,
                action="prepare",
                runtime=str(runtime),
                target=str(target),
                release=str(current_archive),
                mode="upgrade",
            )
            self.assertEqual("ready", prepared["kit_change"]["status"], prepared)
            self.assertEqual(
                "legacy",
                controller.load(runtime, prepared["session_id"])["preview"]["raw"]
                ["material"]["current"]["mode"],
            )
            applied = _run_release_controller(
                current_release,
                action="apply",
                runtime=str(runtime),
                session_id=prepared["session_id"],
                plan_sha256=prepared["kit_change"]["plan_sha256"],
                real_check=True,
            )

            self.assertIn(
                applied["kit_change"]["status"],
                {"complete", "adoption_required"},
                applied,
            )
            checked = controller.load(runtime, prepared["session_id"])
            self.assertTrue(checked["check"]["kit_ok"], checked["check"])
            self.assertEqual("apply_time", applied["kit_change"]["check_evidence"]["state"])
            self.assertFalse(old_manifest.exists())
            self.assertFalse((target / "check.py").exists())
            self.assertFalse((target / "kit.py").exists())
            for path in ("AGENTS.md", ".github/copilot-instructions.md"):
                content = target.joinpath(*path.split("/")).read_bytes()
                self.assertEqual(managed_blocks[path], content, path)
            self.assertEqual(guarded, _guarded_snapshot(target))

            restored = _run_release_controller(
                current_release,
                action="restore",
                runtime=str(runtime),
                session_id=prepared["session_id"],
                result_sha256=applied["kit_change"]["result_sha256"],
            )

            self.assertEqual("restored", restored["kit_change"]["status"], restored)
            self.assertEqual(
                original,
                _tree_snapshot(target, exclude_top=frozenset({".kit"})),
            )


if __name__ == "__main__":
    unittest.main()
