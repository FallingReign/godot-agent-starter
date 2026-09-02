#!/usr/bin/env python3
"""Focused tests for the stable flat/managed kit launcher."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Iterator
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import managed_launcher as launcher  # noqa: E402


@contextlib.contextmanager
def _scratch() -> Iterator[Path]:
    configured = os.environ.get("KIT_TEST_TMPDIR")
    candidates = ([Path(configured)] if configured else []) + [
        TOOLS.parent / ".checklogs" / "tests",
        Path(tempfile.gettempdir()),
        Path("/tmp"),
    ]
    base = None
    for candidate in candidates:
        probe = candidate / f"launcher-probe-{uuid.uuid4().hex}"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe.mkdir()
            (probe / "write-check").write_text("ok", encoding="utf-8")
            shutil.rmtree(probe)
            base = candidate.resolve()
            break
        except OSError:
            shutil.rmtree(probe, ignore_errors=True)
    if base is None:
        raise RuntimeError("no writable managed-launcher test directory")
    directory = base / f"managed-launcher-{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.parent == base:
            shutil.rmtree(directory, ignore_errors=True)


def _canonical(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _marker(root: Path) -> None:
    (root / launcher.MARKER_NAME).write_bytes(_canonical({
        "kind": launcher.MARKER_KIND,
        "schema": launcher.MARKER_SCHEMA,
    }))


def _flat(root: Path) -> None:
    _marker(root)
    (root / "kit.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")


def _managed(
    root: Path, *, extra_files: dict[str, bytes] | None = None
) -> tuple[Path, Path, dict]:
    _marker(root)
    release_sha = hashlib.sha256(b"archive bytes").hexdigest()
    source_commit = "b" * 40
    core = root / launcher.MANAGED_DIRECTORY / launcher.RELEASES_DIRECTORY / release_sha
    core.mkdir(parents=True)
    install_manifest_content = b'{"schema":1}\n'
    contents = {
        launcher.INSTALL_MANIFEST_NAME: install_manifest_content,
        "LICENSE": b"MIT\n",
        "kit.py": b"#!/usr/bin/env python3\n",
    }
    contents.update(extra_files or {})
    entries = []
    for relative, content in sorted(contents.items()):
        target = core.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if relative.endswith(".py") else 0o644)
        entries.append({
            "path": relative,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "mode": "0755" if relative.endswith(".py") else "0644",
        })
    manifest = {
        "schema": launcher.MANIFEST_SCHEMA,
        "version": "0.3.0",
        "source": {"commit": source_commit, "dirty": False},
        "authority_evidence": {
            "identity_model": "portable-policy-audit",
            "project_receipt_trust": "not-applicable-no-project-state",
            "receipt_trust": "portable-policy",
        },
        "license_files": ["LICENSE"],
        "normalization": {
            "line_endings": "lf",
            "regular_mode": "0644",
            "executable_mode": "0755",
            "timestamps": "fixed",
        },
        "files": entries,
    }
    manifest_content = _canonical(manifest)
    manifest_path = core / launcher.MANIFEST_NAME
    manifest_path.write_bytes(manifest_content)
    manifest_path.chmod(0o644)
    current = {
        "schema": launcher.CURRENT_SCHEMA,
        "kind": launcher.CURRENT_KIND,
        "installation_id": "2" * 32,
        "install_schema": 1,
        "layout_schema": 1,
        "config_schema": 1,
        "active_release": {
            "kit_version": "0.3.0",
            "archive_sha256": release_sha,
            "release_manifest_sha256": hashlib.sha256(manifest_content).hexdigest(),
            "install_manifest_sha256": hashlib.sha256(
                install_manifest_content
            ).hexdigest(),
            "source_commit": source_commit,
            "core_path": (
                f"{launcher.MANAGED_DIRECTORY}/{launcher.RELEASES_DIRECTORY}/{release_sha}"
            ),
        },
        "previous_release": None,
        "managed_surfaces": [],
        "applied_migrations": [],
    }
    current_path = root / launcher.MANAGED_DIRECTORY / launcher.CURRENT_NAME
    current_path.write_bytes(_canonical(current))
    return core, current_path, current


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class InstallationSelection(unittest.TestCase):
    def test_flat_source_uses_marked_project_as_core(self) -> None:
        with _scratch() as root:
            _flat(root)
            nested = root / "nested" / "path"
            nested.mkdir(parents=True)

            selected = launcher.resolve_installation(nested)

            self.assertEqual("flat", selected.mode)
            self.assertEqual(root, selected.project_root)
            self.assertEqual(root, selected.core_root)
            self.assertEqual(root, selected.kit_root)
            self.assertEqual(root, selected.code_root)
            self.assertIsNone(selected.release_sha256)

    def test_managed_release_is_fully_checked_without_writing(self) -> None:
        with _scratch() as root:
            core, current_path, current = _managed(
                root, extra_files={"tools/helper.py": b"VALUE = 1\n"}
            )
            before = _tree_bytes(root)

            selected = launcher.resolve_installation(root)

            self.assertEqual(before, _tree_bytes(root))
            self.assertEqual("managed", selected.mode)
            self.assertEqual(root, selected.project_root)
            self.assertEqual(core, selected.core_root)
            self.assertEqual(current_path, selected.current_path)
            self.assertEqual(
                current["active_release"]["archive_sha256"], selected.release_sha256
            )
            self.assertEqual(
                current["active_release"]["source_commit"], selected.source_commit
            )

    def test_managed_release_refuses_unlisted_core_content(self) -> None:
        with _scratch() as root:
            core, _current_path, _current = _managed(root)
            (core / "json.py").write_text("raise RuntimeError('shadowed')\n", encoding="utf-8")

            with self.assertRaisesRegex(launcher.LauncherError, "unlisted file json.py"):
                launcher.resolve_installation(root)

        with _scratch() as root:
            core, _current_path, _current = _managed(root)
            (core / "empty-extra-directory").mkdir()

            with self.assertRaisesRegex(
                launcher.LauncherError, "unlisted directory empty-extra-directory"
            ):
                launcher.resolve_installation(root)

    def test_managed_release_refuses_a_hardlinked_member(self) -> None:
        with _scratch() as root:
            core, _current_path, _current = _managed(root)
            member = core / "kit.py"
            source = root / "hardlink-source.py"
            source.write_bytes(member.read_bytes())
            member.unlink()
            try:
                os.link(source, member)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable on this host: {exc}")

            with self.assertRaisesRegex(launcher.LauncherError, "hard link"):
                launcher.resolve_installation(root)

    def test_partial_managed_directory_never_falls_back_to_flat(self) -> None:
        with _scratch() as root:
            _flat(root)
            (root / launcher.MANAGED_DIRECTORY).mkdir()

            with self.assertRaisesRegex(launcher.LauncherError, "current pointer.*missing"):
                launcher.resolve_installation(root)

    def test_current_pointer_requires_exact_canonical_fields(self) -> None:
        with _scratch() as root:
            _core, current_path, current = _managed(root)
            cases = [
                {**current, "schema": 2},
                {
                    **current,
                    "active_release": {
                        **current["active_release"],
                        "archive_sha256": current["active_release"][
                            "archive_sha256"
                        ].upper(),
                    },
                },
                {**current, "extra": True},
            ]
            for value in cases:
                with self.subTest(value=value):
                    current_path.write_bytes(_canonical(value))
                    with self.assertRaises(launcher.LauncherError):
                        launcher.resolve_installation(root)

            current_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(launcher.LauncherError, "canonically encoded"):
                launcher.resolve_installation(root)

    def test_current_pointer_must_match_manifest_and_release(self) -> None:
        with _scratch() as root:
            core, current_path, current = _managed(root)
            current["active_release"]["release_manifest_sha256"] = "0" * 64
            current_path.write_bytes(_canonical(current))
            with self.assertRaisesRegex(launcher.LauncherError, "manifest SHA-256"):
                launcher.resolve_installation(root)

            current["active_release"]["release_manifest_sha256"] = hashlib.sha256(
                (core / launcher.MANIFEST_NAME).read_bytes()
            ).hexdigest()
            current["active_release"]["archive_sha256"] = "1" * 64
            current["active_release"]["core_path"] = (
                f"{launcher.MANAGED_DIRECTORY}/{launcher.RELEASES_DIRECTORY}/{'1' * 64}"
            )
            current_path.write_bytes(_canonical(current))
            with self.assertRaisesRegex(launcher.LauncherError, "selected release.*missing"):
                launcher.resolve_installation(root)

            self.assertTrue(core.is_dir())

    def test_rich_current_state_validates_install_manifest_and_sorted_surfaces(self) -> None:
        with _scratch() as root:
            _core, current_path, current = _managed(root)
            current["active_release"]["install_manifest_sha256"] = "0" * 64
            current_path.write_bytes(_canonical(current))
            with self.assertRaisesRegex(launcher.LauncherError, "install manifest SHA-256"):
                launcher.resolve_installation(root)

        with _scratch() as root:
            _core, current_path, current = _managed(root)
            current["managed_surfaces"] = [
                {
                    "id": "z-surface",
                    "path": "AGENTS.md",
                    "strategy": "managed-block",
                    "base_sha256": "1" * 64,
                    "applied_sha256": "2" * 64,
                },
                {
                    "id": "a-surface",
                    "path": "CLAUDE.md",
                    "strategy": "managed-block",
                    "base_sha256": "3" * 64,
                    "applied_sha256": "4" * 64,
                },
            ]
            current_path.write_bytes(_canonical(current))
            with self.assertRaisesRegex(launcher.LauncherError, "sorted unique ids"):
                launcher.resolve_installation(root)

    def test_changed_core_member_is_refused(self) -> None:
        with _scratch() as root:
            core, _current_path, _current = _managed(root)
            (core / "kit.py").write_text("changed\n", encoding="utf-8")

            with self.assertRaisesRegex(launcher.LauncherError, "size does not match"):
                launcher.resolve_installation(root)

    def test_manifest_case_collision_is_refused_before_member_lookup(self) -> None:
        with _scratch() as root:
            core, current_path, current = _managed(root)
            manifest_path = core / launcher.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            content = b"same\n"
            for relative in ("A.txt", "a.txt"):
                manifest["files"].append({
                    "path": relative,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "mode": "0644",
                })
            manifest["files"] = sorted(manifest["files"], key=lambda item: item["path"])
            manifest_content = _canonical(manifest)
            manifest_path.write_bytes(manifest_content)
            current["active_release"]["release_manifest_sha256"] = hashlib.sha256(
                manifest_content
            ).hexdigest()
            current_path.write_bytes(_canonical(current))

            with self.assertRaisesRegex(launcher.LauncherError, "case collision"):
                launcher.resolve_installation(root)

    def test_marker_and_managed_names_require_portable_casing(self) -> None:
        with _scratch() as root:
            (root / ".AGENT-KIT.JSON").write_bytes(_canonical({
                "kind": launcher.MARKER_KIND,
                "schema": launcher.MARKER_SCHEMA,
            }))
            with self.assertRaisesRegex(launcher.LauncherError, "unsafe casing"):
                launcher.resolve_installation(root)

    def test_project_path_with_a_redirected_component_is_refused(self) -> None:
        with _scratch() as root:
            _flat(root)
            other = root / "other"
            other.mkdir()
            original = Path.resolve

            def fake_resolve(path: Path, *args, **kwargs):
                if path == root:
                    return other
                return original(path, *args, **kwargs)

            with mock.patch.object(
                Path, "resolve", autospec=True, side_effect=fake_resolve
            ):
                with self.assertRaisesRegex(launcher.LauncherError, "redirected components"):
                    launcher.resolve_installation(root)

        with _scratch() as root:
            _flat(root)
            (root / ".Agent-Kit").mkdir()
            with self.assertRaisesRegex(launcher.LauncherError, "unsafe casing"):
                launcher.resolve_installation(root)

    def test_reparse_managed_directory_is_refused(self) -> None:
        with _scratch() as root:
            _managed(root)
            managed = root / launcher.MANAGED_DIRECTORY
            original = Path.lstat

            def fake_lstat(path: Path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == managed:
                    redirected = mock.Mock(wraps=info)
                    redirected.st_mode = info.st_mode
                    redirected.st_file_attributes = 0x0400
                    return redirected
                return info

            with mock.patch.object(Path, "lstat", autospec=True, side_effect=fake_lstat):
                with self.assertRaisesRegex(launcher.LauncherError, "unredirected"):
                    launcher.resolve_installation(root)

    def test_reparse_release_member_is_refused(self) -> None:
        with _scratch() as root:
            core, _current_path, _current = _managed(root)
            entrypoint = core / "kit.py"
            original = Path.lstat

            def fake_lstat(path: Path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == entrypoint:
                    redirected = mock.Mock(wraps=info)
                    redirected.st_mode = info.st_mode
                    redirected.st_file_attributes = 0x0400
                    return redirected
                return info

            with mock.patch.object(Path, "lstat", autospec=True, side_effect=fake_lstat):
                with self.assertRaisesRegex(
                    launcher.LauncherError, "redirected|regular file"
                ):
                    launcher.resolve_installation(root)


class ProcessBoundary(unittest.TestCase):
    def test_consumer_resolves_managed_core_only_through_both_bindings(self) -> None:
        with _scratch() as root:
            core, _current_path, _current = _managed(root)
            environment = {
                launcher.PROJECT_ROOT_ENV: str(root),
                launcher.CORE_ROOT_ENV: str(core),
            }

            selected = launcher.resolve_bound_installation(core, environment)

            self.assertEqual(root, selected.project_root)
            self.assertEqual(core, selected.core_root)

            with self.assertRaisesRegex(launcher.LauncherError, "supplied together"):
                launcher.resolve_bound_installation(
                    core, {launcher.PROJECT_ROOT_ENV: str(root)}
                )
            with self.assertRaisesRegex(launcher.LauncherError, "running core"):
                launcher.resolve_bound_installation(root, environment)

    def test_consumer_accepts_unbound_flat_core_but_not_unbound_release(self) -> None:
        with _scratch() as root:
            _flat(root)
            selected = launcher.resolve_bound_installation(root, {})
            self.assertEqual("flat", selected.mode)

        with _scratch() as root:
            core, _current_path, _current = _managed(root)
            with self.assertRaisesRegex(launcher.LauncherError, "requires validated"):
                launcher.resolve_bound_installation(core, {})

    def test_launch_binds_roots_and_does_not_write_before_process_start(self) -> None:
        with _scratch() as root:
            core, _current_path, _current = _managed(root)
            before = _tree_bytes(root)
            observed = {}

            def run(command, **kwargs):
                observed["command"] = command
                observed.update(kwargs)
                self.assertEqual(before, _tree_bytes(root))
                return subprocess.CompletedProcess(command, 17)

            environment = {
                key: value for key, value in os.environ.items()
                if key not in (launcher.PROJECT_ROOT_ENV, launcher.CORE_ROOT_ENV)
            }
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                launcher.subprocess, "run", side_effect=run
            ):
                result = launcher.launch([
                    "doctor", "--project", str(root), "--json"
                ])

            self.assertEqual(17, result)
            self.assertEqual(sys.executable, observed["command"][0])
            self.assertEqual(str(core / "kit.py"), observed["command"][1])
            self.assertEqual(root, observed["cwd"])
            self.assertEqual(str(root), observed["env"][launcher.PROJECT_ROOT_ENV])
            self.assertEqual(str(core), observed["env"][launcher.CORE_ROOT_ENV])
            self.assertEqual("1", observed["env"]["PYTHONDONTWRITEBYTECODE"])
            self.assertFalse(observed["check"])

    def test_existing_environment_binding_cannot_select_another_project(self) -> None:
        with _scratch() as root:
            _flat(root)
            other = root / "other"
            other.mkdir()
            selected = launcher.resolve_installation(root)

            with self.assertRaisesRegex(launcher.LauncherError, "does not match"):
                launcher.bound_environment(
                    selected,
                    {
                        launcher.PROJECT_ROOT_ENV: str(other),
                        launcher.CORE_ROOT_ENV: str(root),
                    },
                )
            with self.assertRaisesRegex(launcher.LauncherError, "supplied together"):
                launcher.bound_environment(
                    selected, {launcher.PROJECT_ROOT_ENV: str(root)}
                )

    def test_project_option_is_unambiguous_and_works_after_command(self) -> None:
        self.assertEqual("C:/game", launcher._project_argument([
            "doctor", "--project=C:/game", "--json"
        ]))
        with self.assertRaisesRegex(launcher.LauncherError, "only once"):
            launcher._project_argument([
                "--project", "one", "doctor", "--project", "two"
            ])
        with self.assertRaisesRegex(launcher.LauncherError, "requires"):
            launcher._project_argument(["doctor", "--project"])
        with self.assertRaisesRegex(launcher.LauncherError, "in full"):
            launcher._project_argument(["doctor", "--pro", "C:/game"])

    def test_release_copy_cannot_be_used_as_the_stable_launcher(self) -> None:
        release_sha = "c" * 64
        fake = (
            Path("C:/project/.agent-kit/releases")
            / release_sha
            / "tools"
            / "managed_launcher.py"
        )
        with mock.patch.object(launcher, "__file__", str(fake)):
            with self.assertRaisesRegex(launcher.LauncherError, "cannot be launched directly"):
                launcher._default_project()

    def test_main_reports_refusal_without_starting_a_process(self) -> None:
        with _scratch() as root:
            _flat(root)
            (root / launcher.MANAGED_DIRECTORY).mkdir()
            errors = io.StringIO()
            with mock.patch.object(launcher.subprocess, "run") as run, \
                    contextlib.redirect_stderr(errors):
                code = launcher.main(["--project", str(root), "doctor"])
            self.assertEqual(3, code)
            self.assertIn("current pointer is missing", errors.getvalue())
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
