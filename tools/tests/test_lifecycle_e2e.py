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
import managed_launcher  # noqa: E402
import release  # noqa: E402


def _write(root: Path, relative: str, content: bytes) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _run_git(root: Path, *arguments: str) -> None:
    null_hooks = "NUL" if os.name == "nt" else "/dev/null"
    completed = subprocess.run(
        ["git", "-C", str(root), "-c", f"core.hooksPath={null_hooks}", *arguments],
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
            os.environ.get("COMSPEC", "cmd.exe"),
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

        scratch_parent = ROOT / ".kit" / "runtime" / "tests"
        scratch_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="lifecycle-e2e-", dir=scratch_parent
        ) as temporary:
            scratch = Path(temporary).resolve()
            source = scratch / "source"
            archive = scratch / "godot-agent-kit.zip"
            extracted = scratch / "extracted"
            _make_clean_release_source(source)
            release_report = release.build_release(source, archive)
            self.assertTrue(release_report["ok"])
            _extract_verified_release(archive, extracted)

            for label, release_source in (("archive", archive), ("extracted", extracted)):
                with self.subTest(release_source=label):
                    target = scratch / f"target-{label}"
                    runtime = scratch / f"controller-{label}"
                    target.mkdir()
                    runtime.mkdir()
                    _copy_project_fixture(target)
                    original = _tree_snapshot(target)
                    guarded = _guarded_snapshot(target)
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


if __name__ == "__main__":
    unittest.main()
