#!/usr/bin/env python3
"""Real offline install/use/restore proof for archive and extracted releases."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Mapping


TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import kit_change_controller as controller  # noqa: E402
import kit_change  # noqa: E402
import managed_launcher  # noqa: E402
import process_supervisor  # noqa: E402
import release  # noqa: E402


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


def _extract_verified_release(archive: Path, destination: Path) -> None:
    _report, members = release.read_verified_archive(archive)
    destination.mkdir()
    for relative, member in sorted(members.items()):
        path = _write(destination, relative, member.content)
        if member.mode is not None:
            path.chmod(member.mode)
    release.read_verified_directory(destination)


def _copy_project_fixture(destination: Path) -> None:
    shutil.copytree(ROOT / "src", destination / "src")
    shutil.copytree(ROOT / "docs" / "design", destination / "docs" / "design")
    shutil.copy2(ROOT / "ARCHITECTURE.md", destination / "ARCHITECTURE.md")
    shutil.copy2(ROOT / "arch.rules.json", destination / "arch.rules.json")
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


def _run_launcher(target: Path, *arguments: str) -> dict[str, object]:
    environment = dict(os.environ)
    environment.pop(managed_launcher.PROJECT_ROOT_ENV, None)
    environment.pop(managed_launcher.CORE_ROOT_ENV, None)
    environment.update(
        {
            "KIT_ENGINE_DISABLED": "1",
            "KIT_NATIVE_RETRY_TOKEN": "",
            "KIT_PYTHON": str(Path(sys.executable).resolve()),
            "KIT_VERIFY_AUTH_KEY": "",
            "KIT_VERIFY_NONCE": "",
            "KIT_VERIFY_REPOSITORY_SHA256": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    completed = subprocess.run(
        _launcher_command(target, *arguments),
        cwd=target,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AssertionError(
            f"managed launcher {' '.join(arguments)} failed ({completed.returncode}): {detail}"
        )
    try:
        payload = json.loads(completed.stdout.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"managed launcher {' '.join(arguments)} returned invalid JSON: "
            f"{completed.stdout[:500]}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise AssertionError(
            f"managed launcher {' '.join(arguments)} returned a failing receipt: {payload}"
        )
    return payload


class ManagedLifecycleEndToEndTest(unittest.TestCase):
    def test_verified_archive_and_exact_extracted_release_install_use_and_restore(
        self,
    ) -> None:
        if not (ROOT / ".git").exists():
            self.skipTest("source Git authority is absent in an installed release")

        with tempfile.TemporaryDirectory(prefix="lifecycle-e2e-") as temporary:
            scratch = Path(temporary).resolve()
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

                    prepared = controller.prepare(
                        runtime,
                        target,
                        release_source,
                        "install",
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

                    receipts: list[dict[str, object]] = []

                    def check_applied(
                        checked_target: Path, _session: Mapping[str, object]
                    ) -> Mapping[str, object]:
                        try:
                            self.assertEqual(target, checked_target)
                            receipts.append(_run_launcher(target, "doctor", "--json"))
                            receipts.append(
                                _run_launcher(target, "verify", "--static", "--json")
                            )
                        except (
                            AssertionError,
                            OSError,
                            subprocess.SubprocessError,
                        ) as exc:
                            return {
                                "kit_ok": False,
                                "project_ok": False,
                                "existing_issues": [],
                                "detail": " ".join(str(exc).split())[:1000],
                            }
                        return {
                            "kit_ok": True,
                            "project_ok": True,
                            "existing_issues": [],
                            "detail": "Managed doctor and static verification passed.",
                        }

                    applied = controller.apply(
                        runtime,
                        prepared["session_id"],
                        prepared["kit_change"]["plan_sha256"],
                        post_apply_check=check_applied,
                    )
                    self.assertEqual(
                        "complete", applied["kit_change"]["status"], applied
                    )
                    self.assertEqual(
                        ["doctor", "verify"],
                        [item["command"] for item in receipts],
                    )
                    self.assertEqual(guarded, _guarded_snapshot(target))
                    for path, before in human_text.items():
                        content = target.joinpath(*path.split("/")).read_bytes()
                        self.assertTrue(content.startswith(before), path)
                        self.assertEqual(1, content.count(managed_blocks[path]), path)

                    restored = controller.restore(
                        runtime,
                        prepared["session_id"],
                        applied["kit_change"]["result_sha256"],
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
        with tempfile.TemporaryDirectory(prefix="legacy-lifecycle-e2e-") as temporary:
            scratch = Path(temporary).resolve()
            historic_source = scratch / "historic-source"
            historic_archive = scratch / "godot-agent-kit-0.2.0.zip"
            current_source = scratch / "current-source"
            current_archive = scratch / "godot-agent-kit-current.zip"
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

            current_commit = _run_git(ROOT, "rev-parse", "HEAD")
            _clone_exact_commit(current_source, current_commit)
            release.build_release(current_source, current_archive)
            managed_blocks = _managed_block_sources(current_archive)
            _extract_verified_release(historic_archive, target)
            shutil.copytree(ROOT / "src", target / "src")
            shutil.copytree(ROOT / "docs" / "design", target / "docs" / "design")
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

            prepared = controller.prepare(
                runtime,
                target,
                current_archive,
                "upgrade",
            )
            self.assertEqual("ready", prepared["kit_change"]["status"], prepared)
            self.assertEqual(
                "legacy",
                controller.load(runtime, prepared["session_id"])["preview"]["raw"]
                ["material"]["current"]["mode"],
            )
            applied = controller.apply(
                runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
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

            restored = controller.restore(
                runtime,
                prepared["session_id"],
                applied["kit_change"]["result_sha256"],
            )

            self.assertEqual("restored", restored["kit_change"]["status"], restored)
            self.assertEqual(
                original,
                _tree_snapshot(target, exclude_top=frozenset({".kit"})),
            )


if __name__ == "__main__":
    unittest.main()
