#!/usr/bin/env python3
"""Focused safety tests for transactional kit install and upgrade."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import kit_change  # noqa: E402
import process_supervisor  # noqa: E402


BEGIN = "<!-- BEGIN GODOT AGENT KIT -->"
END = "<!-- END GODOT AGENT KIT -->"
MARKER_BYTES = b'{"kind":"portable-agent-kit-root","schema":1}\n'


class SimulatedCrash(BaseException):
    """A process-ending failure, deliberately outside ``Exception``."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write(root: Path, relative: str, content: bytes) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _release_fixture(
    *,
    version: str = "0.3.0",
    source_commit: str = "b" * 40,
    launcher: bytes = b"@echo off\necho managed kit\n",
    block_body: str = "Use the active managed kit release.",
    legacy_launcher: bytes | None = None,
    legacy_marker: bytes | None = None,
    legacy_agents: bytes | None = None,
    owned_path: str = "kit.cmd",
    config_target_schema: int = 1,
    config_supported_schemas: list[int] | None = None,
    retired: dict[str, bytes] | None = None,
) -> tuple[dict[str, object], dict[str, SimpleNamespace]]:
    block = f"{BEGIN}\n{block_body}\n{END}\n".encode("utf-8")
    config_defaults: dict[str, object] = {
        "schema": config_target_schema,
        "game_root": "src",
        "runtime_root": ".kit/runtime",
        "note_threshold": 10,
        "providers": {
            "analyzer": {"kind": "manual", "timeout_minutes": 30},
            "worker": {"kind": "manual", "timeout_minutes": 30},
        },
        "dispatch_policy": {"owned": [], "forbidden": []},
    }
    if config_supported_schemas is None:
        config_supported_schemas = [config_target_schema]
    if retired is None:
        retired = {}
    manifest: dict[str, object] = {
        "schema": 1,
        "kind": "agent-kit-install-manifest",
        "kit_version": version,
        "install_schema": 1,
        "layout_schema": 1,
        "config_schema": 1,
        "supported_legacy_versions": ["0.2.0"],
        "supported_install_schemas": [1],
        "core_layout": "versioned-by-archive-sha256",
        "owned_files": [
            {
                "id": "launcher",
                "path": owned_path,
                "source": "install/kit.cmd",
                "mode": "0644",
                "strategy": "replace",
                "legacy_sha256": (
                    _sha256(legacy_launcher) if legacy_launcher is not None else None
                ),
            },
            {
                "id": "marker",
                "path": ".agent-kit.json",
                "source": ".agent-kit.json",
                "mode": "0644",
                "strategy": "replace",
                "legacy_sha256": (
                    _sha256(legacy_marker) if legacy_marker is not None else None
                ),
            },
            {
                "id": "architecture",
                "path": "ARCHITECTURE.md",
                "source": "install/ARCHITECTURE.md",
                "mode": "0644",
                "strategy": "create-only",
                "legacy_sha256": None,
            },
            {
                "id": "architecture-rules",
                "path": "arch.rules.json",
                "source": "install/arch.rules.json",
                "mode": "0644",
                "strategy": "create-only",
                "legacy_sha256": None,
            },
            {
                "id": "gdscript-style",
                "path": ".gdlintrc",
                "source": "install/.gdlintrc",
                "mode": "0644",
                "strategy": "create-only",
                "legacy_sha256": None,
            },
            {
                "id": "project-config",
                "path": "kit.config.json",
                "source": "install/kit.config.default.json",
                "mode": "0644",
                "strategy": "schema-json",
                "legacy_sha256": None,
                "schema_key": "schema",
                "target_schema": config_target_schema,
                "supported_schemas": config_supported_schemas,
                "defaults": config_defaults,
                "allowed_keys": sorted(config_defaults),
            },
        ],
        "managed_blocks": [
            {
                "id": "policy",
                "path": "AGENTS.md",
                "source": "install/agents.block.md",
                "mode": "0644",
                "legacy_file_sha256": (
                    _sha256(legacy_agents) if legacy_agents is not None else None
                ),
                "begin": BEGIN,
                "end": END,
            }
        ],
        "legacy_retired_files": [
            {"path": path, "sha256": _sha256(content)}
            for path, content in sorted(retired.items())
        ],
    }
    members = {
        ".agent-kit.json": SimpleNamespace(content=MARKER_BYTES, mode=0o644),
        "INSTALL-MANIFEST.json": SimpleNamespace(content=_canonical(manifest), mode=0o644),
        "LICENSE": SimpleNamespace(content=b"MIT fixture license\n", mode=0o644),
        "install/agents.block.md": SimpleNamespace(content=block, mode=0o644),
        "install/.gdlintrc": SimpleNamespace(content=b"max-line-length=100\n", mode=0o644),
        "install/ARCHITECTURE.md": SimpleNamespace(
            content=b"# Project architecture\n", mode=0o644
        ),
        "install/arch.rules.json": SimpleNamespace(content=b"{}\n", mode=0o644),
        "install/kit.config.default.json": SimpleNamespace(
            content=_canonical(config_defaults), mode=0o644
        ),
        "install/kit.cmd": SimpleNamespace(content=launcher, mode=0o644),
        "kit.py": SimpleNamespace(content=b"# managed core\n", mode=0o755),
    }
    release_manifest = {
        "schema": 2,
        "version": version,
        "source": {"commit": source_commit, "dirty": False},
        "authority_evidence": {
            "receipt_trust": "portable-policy",
            "identity_model": "portable-policy-audit",
            "project_receipt_trust": "not-applicable-no-project-state",
        },
        "license_files": ["LICENSE"],
        "normalization": {
            "line_endings": "lf",
            "regular_mode": "0644",
            "executable_mode": "0755",
            "timestamps": "fixed",
        },
        "files": [
            {
                "path": name,
                "bytes": len(member.content),
                "sha256": _sha256(member.content),
                "mode": format(member.mode, "04o"),
            }
            for name, member in sorted(members.items())
        ],
    }
    members["RELEASE-MANIFEST.json"] = SimpleNamespace(
        content=_canonical(release_manifest), mode=0o644
    )
    archive_sha256 = kit_change.release._canonical_release_sha256(members)
    report: dict[str, object] = {
        "ok": True,
        "version": version,
        "archive_sha256": archive_sha256,
        "source": {"commit": source_commit, "dirty": False},
        "authority_evidence": {"receipt_trust": "portable-policy"},
    }
    return report, members


class KitChangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kit-change-test-")
        self.root = Path(self.temporary.name).resolve()
        _write(self.root, "src/project.godot", b"[application]\n")
        self.archive = self.root / "release.zip"
        self.fixture = _release_fixture()
        self.reader = mock.patch.object(
            kit_change, "_read_verified_archive", return_value=self.fixture
        )
        self.reader.start()

    def tearDown(self) -> None:
        self.reader.stop()
        self.temporary.cleanup()

    def _switch_release(
        self, fixture: tuple[dict[str, object], dict[str, SimpleNamespace]]
    ) -> None:
        self.reader.stop()
        self.fixture = fixture
        self.reader = mock.patch.object(
            kit_change, "_read_verified_archive", return_value=self.fixture
        )
        self.reader.start()

    def _preview_and_apply(self) -> dict[str, object]:
        decision = kit_change.preview(self.root, self.archive)
        self.assertTrue(decision["approval"]["approvable"])
        return kit_change.apply(
            self.root, self.archive, str(decision["approval"]["sha256"])
        )

    def _transaction_paths(self, result: dict[str, object]) -> tuple[Path, Path]:
        transaction = str(result["transaction_id"])
        directory = (
            self.root
            / ".kit"
            / "runtime"
            / "upgrade"
            / "transactions"
            / transaction
        )
        return directory / "journal.json", directory / "backup.json"

    def _materialize_core(
        self,
        fixture: tuple[dict[str, object], dict[str, SimpleNamespace]],
    ) -> Path:
        core = (
            self.root
            / ".agent-kit"
            / "releases"
            / str(fixture[0]["archive_sha256"])
        )
        for relative, member in fixture[1].items():
            path = _write(
                self.root,
                f"{core.relative_to(self.root).as_posix()}/{relative}",
                member.content,
            )
            path.chmod(member.mode)
        return core

    def test_preview_is_read_only_and_stable(self) -> None:
        original = b"# Human project rules\nKeep this paragraph.\n"
        _write(self.root, "AGENTS.md", original)
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))

        first = kit_change.preview(self.root, self.archive)
        second = kit_change.preview(self.root, self.archive)

        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(first, second)
        self.assertTrue(first["approval"]["approvable"])
        self.assertEqual(first["material"]["operation"], "install")
        self.assertFalse((self.root / ".agent-kit").exists())
        self.assertFalse((self.root / ".kit").exists())
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), original)

    def test_new_architecture_document_uses_the_target_project_graph(self) -> None:
        _write(
            self.root,
            "src/custom/avatar.gd",
            b"extends RefCounted\nclass_name Avatar\n",
        )

        plan = kit_change._build_plan(self.root, self.archive)

        rendered = plan.replacements["ARCHITECTURE.md"]
        self.assertIsInstance(rendered, bytes)
        assert isinstance(rendered, bytes)
        self.assertIn(b'm0["custom"]', rendered)
        self.assertNotEqual(
            self.fixture[1]["install/ARCHITECTURE.md"].content,
            rendered,
        )
        change = next(
            item
            for item in plan.preview["material"]["changes"]
            if item["path"] == "ARCHITECTURE.md"
        )
        self.assertEqual(_sha256(rendered), change["after"]["sha256"])

        self._preview_and_apply()

        self.assertEqual(rendered, (self.root / "ARCHITECTURE.md").read_bytes())

    def test_target_graph_change_after_preview_invalidates_approval(self) -> None:
        decision = kit_change.preview(self.root, self.archive)
        _write(
            self.root,
            "src/custom/avatar.gd",
            b"extends RefCounted\nclass_name Avatar\n",
        )

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.apply(
                self.root,
                self.archive,
                str(decision["approval"]["sha256"]),
            )

        self.assertEqual("approval-mismatch", raised.exception.code)
        self.assertFalse((self.root / "ARCHITECTURE.md").exists())
        self.assertFalse((self.root / ".agent-kit" / "current.json").exists())

    def test_generated_architecture_over_the_file_limit_blocks_preview(self) -> None:
        oversized = b"x" * (kit_change.MAX_MANAGED_FILE_BYTES + 1)

        with mock.patch.object(
            kit_change,
            "_architecture_document_bytes",
            return_value=oversized,
        ):
            decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            ("managed-file-too-large", "ARCHITECTURE.md"),
            [
                (item["code"], item["path"])
                for item in decision["material"]["blockers"]
            ],
        )

    def test_linked_architecture_source_is_a_clear_lifecycle_blocker(self) -> None:
        source = _write(
            self.root,
            "src/custom/shared-source.gd",
            b"extends RefCounted\n",
        )
        source_path = os.path.normcase(os.path.abspath(source))
        real_lstat = Path.lstat

        def simulated_hardlink(path: Path) -> os.stat_result:
            info = real_lstat(path)
            if os.path.normcase(os.path.abspath(path)) != source_path:
                return info
            values = list(info)
            values[stat.ST_NLINK] = 2
            return os.stat_result(values)

        with mock.patch.object(Path, "lstat", simulated_hardlink):
            decision = kit_change.preview(self.root, self.archive)

        blocker = next(
            item
            for item in decision["material"]["blockers"]
            if item["code"] == "architecture-generation-failed"
        )
        self.assertFalse(decision["approval"]["approvable"])
        self.assertEqual("ARCHITECTURE.md", blocker["path"])
        self.assertIn("architecture scan blocked", blocker["detail"])
        self.assertIn("hard-linked file is not allowed", blocker["detail"])

    def test_apply_rechecks_the_full_preview_digest(self) -> None:
        original = b"# Human project rules\n"
        agents = _write(self.root, "AGENTS.md", original)
        decision = kit_change.preview(self.root, self.archive)
        changed = original + b"A decision made after Preview.\n"
        agents.write_bytes(changed)

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.apply(
                self.root, self.archive, str(decision["approval"]["sha256"])
            )

        self.assertEqual(raised.exception.code, "approval-mismatch")
        self.assertEqual(agents.read_bytes(), changed)
        self.assertFalse((self.root / "kit.cmd").exists())
        self.assertFalse((self.root / ".agent-kit" / "current.json").exists())

    def test_apply_stages_core_and_preserves_shared_text(self) -> None:
        original = b"# Human project rules\nKeep this paragraph.\n"
        _write(self.root, "AGENTS.md", original)

        result = self._preview_and_apply()

        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.root / "kit.cmd").read_bytes(), self.fixture[1]["install/kit.cmd"].content)
        agents = (self.root / "AGENTS.md").read_bytes()
        self.assertTrue(agents.startswith(original))
        self.assertIn(self.fixture[1]["install/agents.block.md"].content, agents)
        state = json.loads((self.root / ".agent-kit" / "current.json").read_text("utf-8"))
        archive_sha256 = str(self.fixture[0]["archive_sha256"])
        self.assertEqual(state["active_release"]["archive_sha256"], archive_sha256)
        self.assertEqual(
            {surface["strategy"] for surface in state["managed_surfaces"]},
            {"create-only", "managed-block", "replace", "schema-json"},
        )
        core = self.root / ".agent-kit" / "releases" / archive_sha256
        self.assertTrue(core.is_dir())
        for relative, member in self.fixture[1].items():
            self.assertEqual(core.joinpath(*relative.split("/")).read_bytes(), member.content)
        tx = str(result["transaction_id"])
        backup = json.loads(
            (self.root / ".kit" / "runtime" / "upgrade" / "transactions" / tx / "backup.json").read_text("utf-8")
        )
        self.assertEqual(backup["kind"], "agent-kit-change-backup")

    def test_create_only_files_preserve_existing_project_choices(self) -> None:
        existing = {
            ".gdlintrc": b"project-style=true\n",
            "ARCHITECTURE.md": b"# Existing architecture\n",
            "arch.rules.json": b'{"project":"rules"}\n',
        }
        for path, content in existing.items():
            _write(self.root, path, content)
        _write(self.root, "AGENTS.md", b"# Human project rules\n")

        self._preview_and_apply()

        for path, content in existing.items():
            self.assertEqual((self.root / path).read_bytes(), content)

    def test_schema_json_migrates_and_preserves_supported_values(self) -> None:
        fixture = _release_fixture(config_supported_schemas=[0, 1])
        self._switch_release(fixture)
        existing = {
            "schema": 0,
            "game_root": ".",
            "runtime_root": ".kit/custom-runtime",
            "note_threshold": 25,
            "providers": {
                "analyzer": {"timeout_minutes": 45, "studio_provider": "local"}
            },
            "studio_extension": {"enabled": True},
        }
        _write(self.root, "project.godot", b"[application]\n")
        _write(self.root, "kit.config.json", _canonical(existing))
        _write(self.root, "AGENTS.md", b"# Human project rules\n")

        self._preview_and_apply()

        config = json.loads((self.root / "kit.config.json").read_text("utf-8"))
        self.assertEqual(config["schema"], 1)
        self.assertEqual(config["game_root"], ".")
        self.assertEqual(config["runtime_root"], ".kit/custom-runtime")
        self.assertEqual(config["note_threshold"], 25)
        self.assertEqual(config["providers"]["analyzer"]["timeout_minutes"], 45)
        self.assertEqual(config["providers"]["analyzer"]["studio_provider"], "local")
        self.assertEqual(config["studio_extension"], {"enabled": True})
        self.assertEqual(config["providers"]["worker"]["kind"], "manual")
        self.assertIn("dispatch_policy", config)

    def test_root_godot_project_selects_root_game_root(self) -> None:
        _write(self.root, "project.godot", b"[application]\n")

        decision = kit_change.preview(self.root, self.archive)

        self.assertEqual(
            decision["material"]["game_root"],
            {"value": ".", "source": "root-project"},
        )
        kit_change.apply(
            self.root, self.archive, str(decision["approval"]["sha256"])
        )
        config = json.loads((self.root / "kit.config.json").read_text("utf-8"))
        self.assertEqual(config["game_root"], ".")
        self.assertEqual((self.root / "project.godot").read_bytes(), b"[application]\n")

    def test_empty_project_config_is_allowed_and_bound_into_the_preview(self) -> None:
        project = _write(self.root, "src/project.godot", b"")

        decision = kit_change.preview(self.root, self.archive)

        self.assertTrue(decision["approval"]["approvable"])
        self.assertEqual(
            decision["material"]["godot_project"],
            {
                "path": "src/project.godot",
                "sha256": _sha256(b""),
                "config_version": None,
            },
        )
        self.assertEqual(project.read_bytes(), b"")

    def test_current_project_config_version_is_allowed(self) -> None:
        content = b"; Engine configuration file.\nconfig_version=5\n\n[application]\n"
        _write(self.root, "src/project.godot", content)

        decision = kit_change.preview(self.root, self.archive)

        self.assertTrue(decision["approval"]["approvable"])
        self.assertEqual(
            decision["material"]["godot_project"],
            {
                "path": "src/project.godot",
                "sha256": _sha256(content),
                "config_version": 5,
            },
        )

    def test_current_project_config_version_allows_leading_zeroes(self) -> None:
        content = b"config_version=0005\n\n[application]\n"
        _write(self.root, "src/project.godot", content)

        decision = kit_change.preview(self.root, self.archive)

        self.assertTrue(decision["approval"]["approvable"])
        self.assertEqual(
            decision["material"]["godot_project"]["config_version"],
            5,
        )

    def test_only_top_level_project_config_version_controls_the_guard(self) -> None:
        content = (
            b"config_version/custom=4\n"
            b'[application]\nconfig_version="game setting, not format"\n'
        )
        _write(self.root, "src/project.godot", content)

        decision = kit_change.preview(self.root, self.archive)

        self.assertTrue(decision["approval"]["approvable"])
        self.assertIsNone(
            decision["material"]["godot_project"]["config_version"]
        )

    def test_invalid_project_config_versions_have_specific_blockers(self) -> None:
        cases = (
            (b"config_version=4\n", "game-project-format-too-old"),
            (b"config_version=6\n", "game-project-format-too-new"),
            (b"config_version=99999999999\n", "game-project-format-too-new"),
            (b'config_version="5"\n', "game-project-config-version-malformed"),
            (
                b"config_version=5\nconfig_version=5\n",
                "game-project-config-version-duplicate",
            ),
            (b"\xffconfig_version=5\n", "game-project-config-version-malformed"),
        )
        for content, expected_code in cases:
            with self.subTest(expected_code=expected_code, content=content):
                _write(self.root, "src/project.godot", content)

                decision = kit_change.preview(self.root, self.archive)

                self.assertFalse(decision["approval"]["approvable"])
                blocker = next(
                    item
                    for item in decision["material"]["blockers"]
                    if item["code"] == expected_code
                )
                self.assertEqual(blocker["path"], "src/project.godot")

    def test_ambiguous_project_headers_are_blocked(self) -> None:
        cases = (
            b"config_version\n=\n4\n",
            b"config_ version=5\n",
            b'"config_version"=4\n',
            b"custom_setting=[\n1,\n2,\n]\n[application]\n",
            b"config_version=5\nunterminated=\"value\n",
        )
        for content in cases:
            with self.subTest(content=content):
                _write(self.root, "src/project.godot", content)

                decision = kit_change.preview(self.root, self.archive)

                self.assertFalse(decision["approval"]["approvable"])
                blocker = next(
                    item
                    for item in decision["material"]["blockers"]
                    if item["code"] == "game-project-config-version-ambiguous"
                )
                self.assertEqual(blocker["path"], "src/project.godot")

    def test_project_missing_at_format_read_is_still_blocked(self) -> None:
        (self.root / "src" / "project.godot").unlink()

        _evidence, blockers = kit_change._game_project_format(self.root, "src")

        self.assertEqual([item["code"] for item in blockers], ["game-project-missing"])

    def test_project_config_stable_read_failures_have_specific_blockers(self) -> None:
        project = self.root / "src" / "project.godot"
        stable_bytes = kit_change._stable_bytes
        cases = (
            ("unsafe-hardlink", "game-project-file-unsafe"),
            ("file-unreadable", "game-project-file-unreadable"),
            ("file-too-large", "game-project-file-too-large"),
            ("file-changed", "game-project-file-changed"),
        )
        for source_code, expected_code in cases:
            with self.subTest(source_code=source_code):

                def guarded_read(
                    path: Path,
                    *,
                    limit: int = kit_change.MAX_MANAGED_FILE_BYTES,
                ) -> bytes:
                    if path == project:
                        raise kit_change.KitChangeError(source_code, "simulated refusal")
                    return stable_bytes(path, limit=limit)

                with mock.patch.object(
                    kit_change,
                    "_stable_bytes",
                    side_effect=guarded_read,
                ):
                    decision = kit_change.preview(self.root, self.archive)

                self.assertFalse(decision["approval"]["approvable"])
                blocker = next(
                    item
                    for item in decision["material"]["blockers"]
                    if item["code"] == expected_code
                )
                self.assertEqual(blocker["path"], "src/project.godot")

    def test_project_config_change_after_preview_invalidates_approval(self) -> None:
        project = _write(self.root, "src/project.godot", b"config_version=5\n")
        decision = kit_change.preview(self.root, self.archive)
        project.write_bytes(b"config_version=5\n; changed after Preview\n")

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.apply(
                self.root,
                self.archive,
                str(decision["approval"]["sha256"]),
            )

        self.assertEqual(raised.exception.code, "approval-mismatch")
        self.assertFalse((self.root / ".agent-kit" / "current.json").exists())

    def test_project_config_change_during_apply_rebuild_blocks_before_writes(self) -> None:
        project = _write(self.root, "src/project.godot", b"config_version=5\n")
        decision = kit_change.preview(self.root, self.archive)
        original = kit_change._csharp_project_blockers

        def mutate_after_format_read(root: Path, game_root: str) -> list[dict]:
            blockers = original(root, game_root)
            project.write_bytes(b"config_version=4\n")
            return blockers

        with mock.patch.object(
            kit_change,
            "_csharp_project_blockers",
            side_effect=mutate_after_format_read,
        ):
            with self.assertRaises(kit_change.KitChangeError) as raised:
                kit_change.apply(
                    self.root,
                    self.archive,
                    str(decision["approval"]["sha256"]),
                )

        self.assertEqual(raised.exception.code, "approval-mismatch")
        self.assertFalse((self.root / ".agent-kit" / "current.json").exists())
        self.assertFalse((self.root / ".agent-kit" / "transactions").exists())

    def test_project_config_change_after_core_staging_rolls_back_kit_writes(self) -> None:
        project = _write(self.root, "src/project.godot", b"config_version=5\n")
        decision = kit_change.preview(self.root, self.archive)
        original = kit_change._stage_core

        def mutate_after_stage(plan: kit_change.ChangePlan, transaction_id: str) -> None:
            original(plan, transaction_id)
            project.write_bytes(b"config_version=4\n")

        with mock.patch.object(
            kit_change,
            "_stage_core",
            side_effect=mutate_after_stage,
        ):
            with self.assertRaises(kit_change.KitChangeError) as raised:
                kit_change.apply(
                    self.root,
                    self.archive,
                    str(decision["approval"]["sha256"]),
                )

        self.assertEqual(raised.exception.code, "approval-mismatch")
        self.assertEqual(project.read_bytes(), b"config_version=4\n")
        self.assertFalse((self.root / ".agent-kit" / "current.json").exists())
        self.assertFalse((self.root / "kit.cmd").exists())
        journals = list((self.root / ".agent-kit" / "transactions").glob("*.json"))
        self.assertEqual(journals, [])

    def test_one_nested_godot_project_selects_its_parent(self) -> None:
        (self.root / "src" / "project.godot").unlink()
        _write(self.root, "game/project.godot", b"[application]\n")

        decision = kit_change.preview(self.root, self.archive)

        self.assertEqual(
            decision["material"]["game_root"],
            {"value": "game", "source": "unique-project"},
        )
        kit_change.apply(
            self.root, self.archive, str(decision["approval"]["sha256"])
        )
        config = json.loads((self.root / "kit.config.json").read_text("utf-8"))
        self.assertEqual(config["game_root"], "game")

    def test_multiple_godot_projects_need_an_explicit_game_root(self) -> None:
        (self.root / "src" / "project.godot").unlink()
        _write(self.root, "first/project.godot", b"[application]\n")
        _write(self.root, "second/project.godot", b"[application]\n")

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertEqual(decision["material"]["game_root"]["value"], None)
        self.assertIn(
            "game-root-ambiguous",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )
        explicit = kit_change.preview(self.root, self.archive, game_root="second")
        self.assertTrue(explicit["approval"]["approvable"])
        self.assertEqual(
            explicit["material"]["game_root"],
            {"value": "second", "source": "explicit"},
        )

    def test_explicit_game_root_is_bound_into_the_preview_digest(self) -> None:
        _write(self.root, "game-a/project.godot", b"[application]\n")
        _write(self.root, "game-b/project.godot", b"[application]\n")
        decision = kit_change.preview(self.root, self.archive, game_root="game-a")

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.apply(
                self.root,
                self.archive,
                str(decision["approval"]["sha256"]),
                game_root="game-b",
            )

        self.assertEqual(raised.exception.code, "approval-mismatch")
        self.assertFalse((self.root / "kit.config.json").exists())

    def test_missing_godot_project_blocks_install(self) -> None:
        (self.root / "src" / "project.godot").unlink()

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertEqual(
            decision["material"]["game_root"],
            {"value": None, "source": "unresolved"},
        )
        blocker = next(
            item
            for item in decision["material"]["blockers"]
            if item["code"] == "game-project-missing"
        )
        self.assertIsNone(blocker["path"])
        self.assertIn("Godot 4.7.2 GDScript project", blocker["detail"])

    def test_explicit_game_root_cannot_bypass_a_missing_project(self) -> None:
        _write(self.root, "game/README.md", b"# Not a Godot project\n")

        decision = kit_change.preview(self.root, self.archive, game_root="game")

        self.assertFalse(decision["approval"]["approvable"])
        self.assertEqual(
            decision["material"]["game_root"],
            {"value": "game", "source": "explicit"},
        )
        self.assertIn(
            ("game-project-missing", "game/project.godot"),
            {
                (blocker["code"], blocker["path"])
                for blocker in decision["material"]["blockers"]
            },
        )

    def test_configured_game_root_cannot_bypass_a_missing_project(self) -> None:
        _write(
            self.root,
            "kit.config.json",
            _canonical({"schema": 1, "game_root": "game"}),
        )

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertEqual(
            decision["material"]["game_root"],
            {"value": "game", "source": "existing-config"},
        )
        self.assertIn(
            ("game-project-missing", "game/project.godot"),
            {
                (blocker["code"], blocker["path"])
                for blocker in decision["material"]["blockers"]
            },
        )

    def test_upgrade_blocks_when_the_configured_game_project_was_removed(self) -> None:
        self._preview_and_apply()
        self._switch_release(
            _release_fixture(version="0.3.1", source_commit="c" * 40)
        )
        (self.root / "src" / "project.godot").unlink()

        decision = kit_change.preview(self.root, self.archive)

        self.assertEqual(decision["material"]["operation"], "upgrade")
        self.assertFalse(decision["approval"]["approvable"])
        self.assertEqual(
            decision["material"]["game_root"],
            {"value": "src", "source": "existing-config"},
        )
        self.assertIn(
            ("game-project-missing", "src/project.godot"),
            {
                (blocker["code"], blocker["path"])
                for blocker in decision["material"]["blockers"]
            },
        )

    def test_selected_game_root_requires_exact_project_filename_case(self) -> None:
        _write(self.root, "game/Project.godot", b"[application]\n")

        explicit = kit_change.preview(self.root, self.archive, game_root="game")
        _write(
            self.root,
            "kit.config.json",
            _canonical({"schema": 1, "game_root": "game"}),
        )
        configured = kit_change.preview(self.root, self.archive)

        for decision in (explicit, configured):
            self.assertFalse(decision["approval"]["approvable"])
            self.assertIn(
                "path-case-collision",
                {
                    blocker["code"]
                    for blocker in decision["material"]["blockers"]
                },
            )

    def test_schema_json_blocks_an_unsupported_schema_without_editing(self) -> None:
        existing = _canonical({"schema": 99, "game_root": "src"})
        _write(self.root, "kit.config.json", existing)

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            "schema-json-schema-unsupported",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )
        self.assertEqual((self.root / "kit.config.json").read_bytes(), existing)

    def test_schema_json_blocks_a_case_colliding_unknown_key(self) -> None:
        existing = _canonical(
            {
                "schema": 1,
                "GAME_ROOT": ".",
                "runtime_root": ".kit/runtime",
            }
        )
        _write(self.root, "kit.config.json", existing)

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            "schema-json-key-unsafe",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )
        self.assertEqual((self.root / "kit.config.json").read_bytes(), existing)

    def test_root_codex_override_blocks_install_without_editing(self) -> None:
        override = _write(
            self.root,
            "AGENTS.override.md",
            b"# Human override\n",
        )

        decision = kit_change.preview(self.root, self.archive)

        blocker = next(
            item
            for item in decision["material"]["blockers"]
            if item["code"] == "instruction-override-conflict"
        )
        self.assertEqual("AGENTS.override.md", blocker["path"])
        self.assertFalse(decision["approval"]["approvable"])
        self.assertEqual(b"# Human override\n", override.read_bytes())
        self.assertFalse((self.root / "kit.cmd").exists())

    def test_unsafe_codex_override_casing_or_redirect_blocks_install(self) -> None:
        wrong_case = _write(
            self.root,
            "AGENTS.OVERRIDE.md",
            b"# Human override\n",
        )
        decision = kit_change.preview(self.root, self.archive)
        self.assertIn(
            "instruction-override-conflict",
            {item["code"] for item in decision["material"]["blockers"]},
        )
        wrong_case.unlink()

        redirected = self.root / "AGENTS.override.md"
        redirected.write_bytes(b"# Redirected override\n")
        original = kit_change._is_reparse

        def report_redirect(path: Path) -> bool:
            return path == redirected or original(path)

        with mock.patch.object(
            kit_change,
            "_is_reparse",
            side_effect=report_redirect,
        ):
            decision = kit_change.preview(self.root, self.archive)
        self.assertIn(
            "instruction-override-conflict",
            {item["code"] for item in decision["material"]["blockers"]},
        )

    def test_resulting_codex_instructions_over_32_kib_block_install(self) -> None:
        original = b"x" * kit_change.MAX_CODEX_INSTRUCTIONS_BYTES
        agents = _write(self.root, "AGENTS.md", original)

        decision = kit_change.preview(self.root, self.archive)

        blocker = next(
            item
            for item in decision["material"]["blockers"]
            if item["code"] == "instruction-file-too-large"
        )
        self.assertEqual("AGENTS.md", blocker["path"])
        self.assertIn("32768", blocker["detail"])
        self.assertFalse(decision["approval"]["approvable"])
        self.assertEqual(original, agents.read_bytes())

    def test_csharp_marker_in_project_or_selected_game_root_blocks_install(self) -> None:
        root_marker = _write(self.root, "Fixture.csproj", b"<Project />\n")
        root_decision = kit_change.preview(self.root, self.archive)
        root_blocker = next(
            item
            for item in root_decision["material"]["blockers"]
            if item["code"] == "unsupported-csharp-project"
        )
        self.assertEqual("Fixture.csproj", root_blocker["path"])
        root_marker.unlink()

        (self.root / "src" / "Nested.CSPROJ").write_bytes(b"<Project />\n")
        nested_decision = kit_change.preview(self.root, self.archive)
        nested_blocker = next(
            item
            for item in nested_decision["material"]["blockers"]
            if item["code"] == "unsupported-csharp-project"
        )
        self.assertEqual("src/Nested.CSPROJ", nested_blocker["path"])
        self.assertFalse(nested_decision["approval"]["approvable"])
        self.assertFalse((self.root / "kit.cmd").exists())

    def test_rollback_restores_exact_bytes_and_absence(self) -> None:
        original = b"# Human project rules\r\nKeep CRLF exactly.\r\n"
        _write(self.root, "AGENTS.md", original)
        result = self._preview_and_apply()

        rolled_back = kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual(rolled_back["status"], "rolled_back")
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), original)
        self.assertFalse((self.root / "kit.cmd").exists())
        self.assertFalse((self.root / ".agent-kit.json").exists())
        self.assertFalse((self.root / ".agent-kit" / "current.json").exists())
        self.assertFalse((self.root / ".agent-kit").exists())

        next_preview = kit_change.preview(self.root, self.archive)
        self.assertTrue(next_preview["approval"]["approvable"])
        reapplied = kit_change.apply(
            self.root, self.archive, str(next_preview["approval"]["sha256"])
        )
        self.assertEqual("applied", reapplied["status"])

    def test_forged_transaction_core_path_cannot_delete_a_project_directory(self) -> None:
        result = self._preview_and_apply()
        protected = _write(self.root, "human-release/keep.txt", b"keep\n")
        journal_path, _backup_path = self._transaction_paths(result)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["core"]["path"] = "human-release"
        journal_path.write_bytes(_canonical(journal))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual("journal-invalid", raised.exception.code)
        self.assertEqual(b"keep\n", protected.read_bytes())

    def test_forged_activation_phase_cannot_restore_project_files(self) -> None:
        _write(self.root, "AGENTS.md", b"# Human policy\n")
        result = self._preview_and_apply()
        applied = (self.root / "AGENTS.md").read_bytes()
        journal_path, _backup_path = self._transaction_paths(result)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        project_entry = next(
            entry for entry in journal["entries"] if entry["path"] == "AGENTS.md"
        )
        project_entry["phase"] = "activation"
        journal_path.write_bytes(_canonical(journal))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual("journal-invalid", raised.exception.code)
        self.assertEqual(applied, (self.root / "AGENTS.md").read_bytes())
        self.assertTrue((self.root / ".agent-kit" / "current.json").is_file())

    def test_forged_backup_blob_relationship_cannot_overwrite_a_project_file(self) -> None:
        _write(self.root, "AGENTS.md", b"# Human policy\n")
        result = self._preview_and_apply()
        applied = (self.root / "AGENTS.md").read_bytes()
        journal_path, _backup_path = self._transaction_paths(result)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        project_entry = next(
            entry for entry in journal["entries"] if entry["path"] == "AGENTS.md"
        )
        project_entry["backup_blob"] = "blobs/" + "f" * 64
        journal_path.write_bytes(_canonical(journal))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual("journal-invalid", raised.exception.code)
        self.assertEqual(applied, (self.root / "AGENTS.md").read_bytes())

    def test_forged_created_directory_cannot_remove_a_human_directory(self) -> None:
        result = self._preview_and_apply()
        human_directory = self.root / "human-empty"
        human_directory.mkdir()
        applied = (self.root / "kit.cmd").read_bytes()
        journal_path, backup_path = self._transaction_paths(result)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        backup["created_directories"].append("human-empty")
        backup["created_directories"].sort(
            key=lambda candidate: (len(PurePosixPath(candidate).parts), candidate)
        )
        backup_content = _canonical(backup)
        backup_path.write_bytes(backup_content)
        journal["backup_manifest_sha256"] = _sha256(backup_content)
        journal_path.write_bytes(_canonical(journal))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual("backup-invalid", raised.exception.code)
        self.assertTrue(human_directory.is_dir())
        self.assertEqual(applied, (self.root / "kit.cmd").read_bytes())

    def test_backup_preflight_failure_creates_no_transaction_directory(self) -> None:
        plan = kit_change._build_plan(self.root, self.archive)
        _write(self.root, "AGENTS.md", b"changed after preview\n")
        transactions = self.root.joinpath(
            *kit_change.TRANSACTIONS_ROOT.split("/")
        )

        for transaction_id in ("1" * 32, "2" * 32):
            with self.assertRaises(kit_change.KitChangeError) as raised:
                kit_change._prepare_backup(plan, transaction_id)
            self.assertEqual("target-changed", raised.exception.code)

        self.assertFalse(transactions.exists())

    def test_failed_backup_writes_do_not_accumulate_transactions(self) -> None:
        _write(self.root, "AGENTS.md", b"# Human policy\n")
        plan = kit_change._build_plan(self.root, self.archive)
        transactions = self.root.joinpath(
            *kit_change.TRANSACTIONS_ROOT.split("/")
        )

        with mock.patch.object(
            kit_change,
            "_durable_json",
            side_effect=OSError("simulated durable write failure"),
        ):
            for transaction_id in ("3" * 32, "4" * 32):
                with self.assertRaises(OSError):
                    kit_change._prepare_backup(plan, transaction_id)

        self.assertTrue(transactions.is_dir())
        self.assertEqual([], list(transactions.iterdir()))

    def test_failed_journal_write_does_not_leave_a_journal_less_transaction(self) -> None:
        _write(self.root, "AGENTS.md", b"# Human policy\n")
        decision = kit_change.preview(self.root, self.archive)
        transactions = self.root.joinpath(
            *kit_change.TRANSACTIONS_ROOT.split("/")
        )
        durable_json = kit_change._durable_json

        def fail_journal(path: Path, value: object) -> None:
            if path.name == "journal.json":
                raise OSError("simulated journal write failure")
            durable_json(path, value)

        with mock.patch.object(
            kit_change,
            "_durable_json",
            side_effect=fail_journal,
        ), self.assertRaises(OSError):
            kit_change.apply(
                self.root,
                self.archive,
                str(decision["approval"]["sha256"]),
            )

        self.assertTrue(transactions.is_dir())
        self.assertEqual([], list(transactions.iterdir()))
        self.assertFalse((self.root / "kit.cmd").exists())

    def test_journal_less_private_backup_is_recovered_before_the_next_apply(self) -> None:
        decision = kit_change.preview(self.root, self.archive)
        transaction_id = "5" * 32
        orphan = self.root.joinpath(
            *kit_change.TRANSACTIONS_ROOT.split("/"), transaction_id
        )
        blobs = orphan / "blobs"
        blobs.mkdir(parents=True)
        content = b"interrupted private backup\n"
        (blobs / _sha256(content)).write_bytes(content)
        (orphan / "backup.json").write_bytes(b"{}\n")

        result = kit_change.apply(
            self.root,
            self.archive,
            str(decision["approval"]["sha256"]),
        )

        self.assertEqual("applied", result["status"])
        self.assertFalse(orphan.exists())

    def test_unsafe_journal_less_transaction_is_preserved_and_blocks_apply(self) -> None:
        decision = kit_change.preview(self.root, self.archive)
        transaction_id = "6" * 32
        orphan = self.root.joinpath(
            *kit_change.TRANSACTIONS_ROOT.split("/"), transaction_id
        )
        orphan.mkdir(parents=True)
        unexpected = orphan / "unexpected.txt"
        unexpected.write_bytes(b"do not delete\n")

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.apply(
                self.root,
                self.archive,
                str(decision["approval"]["sha256"]),
            )

        self.assertEqual("transaction-incomplete", raised.exception.code)
        self.assertEqual(b"do not delete\n", unexpected.read_bytes())
        self.assertFalse((self.root / "kit.cmd").exists())

    def test_crash_during_project_writes_resumes_by_rolling_back(self) -> None:
        original = b"# Human project rules\n"
        _write(self.root, "AGENTS.md", original)
        decision = kit_change.preview(self.root, self.archive)

        def crash(name: str) -> None:
            if name == "after-entry:AGENTS.md":
                raise SimulatedCrash(name)

        with mock.patch.object(kit_change, "_failpoint", side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                kit_change.apply(
                    self.root, self.archive, str(decision["approval"]["sha256"])
                )

        recovered = kit_change.resume(self.root)

        self.assertEqual(recovered["status"], "rolled_back")
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), original)
        self.assertFalse((self.root / "kit.cmd").exists())
        self.assertFalse((self.root / ".agent-kit.json").exists())
        self.assertFalse((self.root / ".agent-kit" / "current.json").exists())
        self.assertFalse((self.root / ".agent-kit").exists())

    def test_crash_after_activation_resumes_forward(self) -> None:
        _write(self.root, "AGENTS.md", b"# Human project rules\n")
        decision = kit_change.preview(self.root, self.archive)

        def crash(name: str) -> None:
            if name == "after-activated":
                raise SimulatedCrash(name)

        with mock.patch.object(kit_change, "_failpoint", side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                kit_change.apply(
                    self.root, self.archive, str(decision["approval"]["sha256"])
                )

        recovered = kit_change.resume(self.root)

        self.assertEqual(recovered["status"], "applied")
        self.assertTrue((self.root / ".agent-kit" / "current.json").is_file())
        self.assertTrue((self.root / "kit.cmd").is_file())

    def test_ordinary_failure_rolls_back_before_returning(self) -> None:
        original = b"# Human project rules\n"
        _write(self.root, "AGENTS.md", original)
        decision = kit_change.preview(self.root, self.archive)

        def fail(name: str) -> None:
            if name == "after-entry:AGENTS.md":
                raise RuntimeError("injected write failure")

        with mock.patch.object(kit_change, "_failpoint", side_effect=fail):
            with self.assertRaises(kit_change.KitChangeError) as raised:
                kit_change.apply(
                    self.root, self.archive, str(decision["approval"]["sha256"])
                )

        self.assertEqual(raised.exception.code, "apply-failed")
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), original)
        self.assertFalse((self.root / "kit.cmd").exists())
        self.assertFalse((self.root / ".agent-kit" / "current.json").exists())
        self.assertFalse((self.root / ".agent-kit").exists())

    def test_rollback_refuses_a_modified_new_core_before_restoring_project_files(self) -> None:
        original = b"# Human project rules\n"
        _write(self.root, "AGENTS.md", original)
        result = self._preview_and_apply()
        installed_agents = (self.root / "AGENTS.md").read_bytes()
        core_file = (
            self.root
            / ".agent-kit"
            / "releases"
            / str(self.fixture[0]["archive_sha256"])
            / "kit.py"
        )
        core_file.write_bytes(b"tampered core\n")

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual("rollback-conflict", raised.exception.code)
        self.assertEqual(installed_agents, (self.root / "AGENTS.md").read_bytes())
        self.assertTrue((self.root / ".agent-kit" / "current.json").is_file())

    def test_rollback_refuses_a_third_version(self) -> None:
        original = b"# Human project rules\n"
        _write(self.root, "AGENTS.md", original)
        result = self._preview_and_apply()
        applied = (self.root / "AGENTS.md").read_bytes()
        third_version = b"A file changed independently after Apply.\n"
        (self.root / "AGENTS.md").write_bytes(third_version)

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual(raised.exception.code, "rollback-conflict")
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), third_version)
        journal = json.loads(
            next((self.root / ".kit" / "runtime" / "upgrade" / "transactions").glob("*/journal.json")).read_text("utf-8")
        )
        self.assertEqual(journal["state"], "blocked")
        with self.assertRaises(kit_change.KitChangeError) as unnamed:
            kit_change.rollback(self.root)
        self.assertEqual("transaction-blocked", unnamed.exception.code)

        (self.root / "AGENTS.md").write_bytes(applied)
        recovered = kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual("rolled_back", recovered["status"])
        self.assertEqual(original, (self.root / "AGENTS.md").read_bytes())
        self.assertFalse((self.root / ".agent-kit").exists())

    def test_rollback_retries_a_one_shot_permission_error(self) -> None:
        original = b"# Human project rules\n"
        agents = _write(self.root, "AGENTS.md", original)
        result = self._preview_and_apply()
        applied = agents.read_bytes()
        original_atomic = kit_change._atomic_bytes
        failed = False

        def flaky_atomic(path: Path, content: bytes, mode: str) -> None:
            nonlocal failed
            if path == agents and content == original and not failed:
                failed = True
                raise PermissionError("file is temporarily locked")
            original_atomic(path, content, mode)

        with mock.patch.object(
            kit_change, "_atomic_bytes", side_effect=flaky_atomic
        ):
            with self.assertRaises(kit_change.KitChangeError) as raised:
                kit_change.rollback(self.root, str(result["transaction_id"]))

            self.assertEqual("rollback-failed", raised.exception.code)
            self.assertEqual(applied, agents.read_bytes())
            recovered = kit_change.rollback(
                self.root, str(result["transaction_id"])
            )

        self.assertEqual("rolled_back", recovered["status"])
        self.assertEqual(original, agents.read_bytes())
        self.assertFalse((self.root / ".agent-kit").exists())

    def test_interrupted_created_core_removal_resumes_from_exact_subset(self) -> None:
        original = b"# Human project rules\n"
        _write(self.root, "AGENTS.md", original)
        result = self._preview_and_apply()
        removed: list[str] = []

        def crash(name: str) -> None:
            if name.startswith("after-core-member:"):
                removed.append(name.removeprefix("after-core-member:"))
                raise SimulatedCrash(name)

        with mock.patch.object(kit_change, "_failpoint", side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual(1, len(removed))
        core = (
            self.root
            / ".agent-kit"
            / "releases"
            / str(self.fixture[0]["archive_sha256"])
        )
        self.assertTrue(core.is_dir())
        self.assertFalse(core.joinpath(*removed[0].split("/")).exists())

        recovered = kit_change.resume(self.root)

        self.assertEqual("rolled_back", recovered["status"])
        self.assertEqual(original, (self.root / "AGENTS.md").read_bytes())
        self.assertFalse((self.root / "kit.cmd").exists())
        self.assertFalse((self.root / ".agent-kit.json").exists())
        self.assertFalse((self.root / ".agent-kit").exists())

    def test_case_collision_blocks_preview(self) -> None:
        _write(self.root, "agents.md", b"different spelling by case\n")

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            "path-case-collision",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )

    def test_redirected_managed_target_blocks_preview(self) -> None:
        _write(self.root, "AGENTS.md", b"# Human project rules\n")
        original = kit_change._is_reparse

        def redirected(path: Path) -> bool:
            if path.name == "AGENTS.md":
                return True
            return original(path)

        with mock.patch.object(kit_change, "_is_reparse", side_effect=redirected):
            decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            "redirected-path",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )

    def test_dangling_junction_blocks_preview(self) -> None:
        if os.name != "nt":
            return
        destination = self.root / "junction-destination"
        junction = self.root / "AGENTS.md"
        destination.mkdir()
        completed = subprocess.run(
            [
                process_supervisor.windows_command_processor(),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(junction),
                str(destination),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            original = kit_change._is_reparse

            def simulated_dangling_junction(path: Path) -> bool:
                return path == junction or original(path)

            with mock.patch.object(
                kit_change,
                "_is_reparse",
                side_effect=simulated_dangling_junction,
            ):
                decision = kit_change.preview(self.root, self.archive)

            self.assertFalse(decision["approval"]["approvable"])
            self.assertIn(
                "redirected-path",
                {
                    blocker["code"]
                    for blocker in decision["material"]["blockers"]
                },
            )
            return
        destination.rmdir()
        try:
            self.assertFalse(junction.exists())
            self.assertTrue(kit_change._is_reparse(junction))

            decision = kit_change.preview(self.root, self.archive)

            self.assertFalse(decision["approval"]["approvable"])
            self.assertIn(
                "redirected-path",
                {
                    blocker["code"]
                    for blocker in decision["material"]["blockers"]
                },
            )
        finally:
            if kit_change._is_reparse(junction):
                junction.rmdir()

    def test_hardlinked_managed_target_blocks_preview(self) -> None:
        agents = _write(self.root, "AGENTS.md", b"# Human project rules\n")
        path_type = type(agents)
        original_lstat = path_type.lstat

        def report_hardlink(path: Path):
            info = original_lstat(path)
            if path == agents:
                linked = mock.Mock(wraps=info)
                linked.st_mode = info.st_mode
                linked.st_file_attributes = getattr(info, "st_file_attributes", 0)
                linked.st_nlink = 2
                return linked
            return info

        with mock.patch.object(
            path_type, "lstat", autospec=True, side_effect=report_hardlink
        ):
            decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            "unsafe-hardlink",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )

    def test_hardlinked_managed_core_blocks_reuse(self) -> None:
        result = self._preview_and_apply()
        self.assertEqual(result["status"], "applied")
        core = (
            self.root
            / ".agent-kit"
            / "releases"
            / str(self.fixture[0]["archive_sha256"])
        )
        member = core / "kit.py"
        path_type = type(member)
        original_lstat = path_type.lstat

        def report_hardlink(path: Path):
            info = original_lstat(path)
            if path == member:
                linked = mock.Mock(wraps=info)
                linked.st_mode = info.st_mode
                linked.st_file_attributes = getattr(info, "st_file_attributes", 0)
                linked.st_nlink = 2
                return linked
            return info

        with mock.patch.object(
            path_type, "lstat", autospec=True, side_effect=report_hardlink
        ):
            with self.assertRaises(kit_change.KitChangeError) as raised:
                kit_change.preview(self.root, self.archive)

        self.assertEqual("installed-kit-untrusted", raised.exception.code)

    def test_incoming_core_with_extra_empty_directory_cannot_be_reused(self) -> None:
        self._preview_and_apply()
        incoming = _release_fixture(
            version="0.4.0",
            source_commit="c" * 40,
            launcher=b"@echo off\necho managed kit 0.4\n",
        )
        core = self._materialize_core(incoming)
        (core / "extra-empty").mkdir()
        self._switch_release(incoming)

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            "managed-core-modified",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )

    def test_incoming_core_with_case_colliding_directory_cannot_be_reused(self) -> None:
        if os.path.normcase("install") == os.path.normcase("INSTALL"):
            return
        self._preview_and_apply()
        incoming = _release_fixture(
            version="0.4.0",
            source_commit="c" * 40,
            launcher=b"@echo off\necho managed kit 0.4\n",
        )
        core = self._materialize_core(incoming)
        (core / "INSTALL").mkdir()
        self._switch_release(incoming)

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            "managed-core-modified",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )

    def test_forged_ownership_hash_cannot_authorize_overwriting_human_work(self) -> None:
        self._preview_and_apply()
        human = b"@echo off\necho human launcher\n"
        (self.root / "kit.cmd").write_bytes(human)
        current_path = self.root / ".agent-kit" / "current.json"
        current = json.loads(current_path.read_text(encoding="utf-8"))
        launcher = next(
            item for item in current["managed_surfaces"] if item["id"] == "launcher"
        )
        launcher["applied_sha256"] = _sha256(human)
        current_path.write_bytes(_canonical(current))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual("installed-kit-untrusted", raised.exception.code)
        self.assertEqual(human, (self.root / "kit.cmd").read_bytes())

    def test_noncanonical_install_record_blocks_upgrade(self) -> None:
        self._preview_and_apply()
        current_path = self.root / ".agent-kit" / "current.json"
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual("installed-kit-untrusted", raised.exception.code)

    def test_core_identity_mismatch_blocks_upgrade(self) -> None:
        self._preview_and_apply()
        current_path = self.root / ".agent-kit" / "current.json"
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current["active_release"]["install_manifest_sha256"] = "f" * 64
        current_path.write_bytes(_canonical(current))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual("installed-kit-untrusted", raised.exception.code)

    def test_core_change_after_launcher_authentication_blocks_upgrade(self) -> None:
        self._preview_and_apply()
        original = kit_change.managed_launcher.resolve_installation

        def change_after_authentication(project: Path):
            installation = original(project)
            (installation.core_root / "kit.py").write_bytes(b"changed after authentication\n")
            return installation

        with mock.patch.object(
            kit_change.managed_launcher,
            "resolve_installation",
            side_effect=change_after_authentication,
        ):
            with self.assertRaises(kit_change.KitChangeError) as raised:
                kit_change.preview(self.root, self.archive)

        self.assertEqual("installed-kit-untrusted", raised.exception.code)

    def test_exact_transaction_can_be_inspected_after_apply(self) -> None:
        decision = kit_change.preview(self.root, self.archive)
        result = kit_change.apply(
            self.root, self.archive, str(decision["approval"]["sha256"])
        )

        inspected = kit_change.inspect_transaction(
            self.root,
            preview_sha256=str(decision["approval"]["sha256"]),
            target_scope_sha256=str(decision["material"]["target_scope_sha256"]),
        )

        self.assertEqual("applied", inspected["status"])
        self.assertEqual(result["transaction_id"], inspected["transaction_id"])
        self.assertEqual(decision["approval"]["sha256"], inspected["preview_sha256"])

    def test_transaction_inspection_refuses_missing_or_ambiguous_matches(self) -> None:
        decision = kit_change.preview(self.root, self.archive)
        with self.assertRaises(kit_change.KitChangeError) as missing:
            kit_change.inspect_transaction(
                self.root,
                preview_sha256=str(decision["approval"]["sha256"]),
                target_scope_sha256=str(decision["material"]["target_scope_sha256"]),
            )
        self.assertEqual("transaction-missing", missing.exception.code)

        kit_change.apply(
            self.root, self.archive, str(decision["approval"]["sha256"])
        )
        transactions = self.root / ".kit" / "runtime" / "upgrade" / "transactions"
        original = next(transactions.glob("*/journal.json"))
        duplicate_id = "f" * 32
        duplicate = transactions / duplicate_id / "journal.json"
        duplicate.parent.mkdir()
        value = json.loads(original.read_text(encoding="utf-8"))
        value["transaction_id"] = duplicate_id
        duplicate.write_bytes(_canonical(value))

        with self.assertRaises(kit_change.KitChangeError) as ambiguous:
            kit_change.inspect_transaction(
                self.root,
                preview_sha256=str(decision["approval"]["sha256"]),
                target_scope_sha256=str(decision["material"]["target_scope_sha256"]),
            )
        self.assertEqual("transaction-ambiguous", ambiguous.exception.code)

    def test_traversal_in_install_manifest_is_rejected(self) -> None:
        report, members = _release_fixture(owned_path="../outside.cmd")
        self._switch_release((report, members))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual(raised.exception.code, "unsafe-path")
        self.assertFalse((self.root.parent / "outside.cmd").exists())

    def test_legacy_flat_0_2_0_is_detected_and_migrated(self) -> None:
        legacy_launcher = b"@echo off\r\necho legacy kit\r\n"
        legacy_agents = b"# Legacy kit policy\r\n"
        fixture = _release_fixture(
            legacy_launcher=legacy_launcher,
            legacy_marker=MARKER_BYTES,
            legacy_agents=legacy_agents,
        )
        self._switch_release(fixture)
        _write(self.root, ".agent-kit.json", MARKER_BYTES)
        _write(self.root, "VERSION", b"0.2.0\n")
        _write(self.root, "kit.cmd", legacy_launcher)
        _write(self.root, "AGENTS.md", legacy_agents)

        decision = kit_change.preview(self.root, self.archive)

        self.assertTrue(decision["approval"]["approvable"])
        self.assertEqual(decision["material"]["current"]["mode"], "legacy")
        self.assertEqual(decision["material"]["current"]["kit_version"], "0.2.0")
        self.assertEqual(len(decision["material"]["migrations"]), 1)
        result = kit_change.apply(
            self.root, self.archive, str(decision["approval"]["sha256"])
        )
        self.assertTrue((self.root / ".agent-kit" / "current.json").is_file())

        kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual((self.root / "kit.cmd").read_bytes(), legacy_launcher)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), legacy_agents)
        self.assertEqual((self.root / ".agent-kit.json").read_bytes(), MARKER_BYTES)
        self.assertEqual((self.root / "VERSION").read_bytes(), b"0.2.0\n")
        self.assertFalse((self.root / ".agent-kit" / "current.json").exists())

    def test_legacy_core_file_is_retired_and_rollback_restores_it(self) -> None:
        legacy_core = b"# verified flat core file\n"
        fixture = _release_fixture(
            legacy_launcher=b"@echo off\r\nlegacy\r\n",
            legacy_marker=MARKER_BYTES,
            legacy_agents=b"# Legacy kit policy\n",
            retired={"check.py": legacy_core},
        )
        self._switch_release(fixture)
        _write(self.root, ".agent-kit.json", MARKER_BYTES)
        _write(self.root, "VERSION", b"0.2.0\n")
        _write(self.root, "kit.cmd", b"@echo off\r\nlegacy\r\n")
        _write(self.root, "AGENTS.md", b"# Legacy kit policy\n")
        _write(self.root, "check.py", legacy_core)

        decision = kit_change.preview(self.root, self.archive)
        deletion = next(
            change
            for change in decision["material"]["changes"]
            if change["path"] == "check.py"
        )
        self.assertEqual(deletion["action"], "delete")
        result = kit_change.apply(
            self.root, self.archive, str(decision["approval"]["sha256"])
        )
        self.assertFalse((self.root / "check.py").exists())

        kit_change.rollback(self.root, str(result["transaction_id"]))

        self.assertEqual((self.root / "check.py").read_bytes(), legacy_core)

    def test_exact_legacy_release_manifest_is_an_internal_retirement(self) -> None:
        report, members = _release_fixture()

        _manifest, _surfaces, retired = kit_change._install_manifest(report, members)

        release_manifest = next(
            item for item in retired if item.path == "RELEASE-MANIFEST.json"
        )
        self.assertEqual(
            "6d9629f973aeed32ee022b117146f1ee81ba3b58c6a9ff7b14afdcd0daeaca3c",
            release_manifest.sha256,
        )

    def test_release_cannot_authorize_a_general_manifest_retirement(self) -> None:
        report, members = _release_fixture()
        manifest = json.loads(members["INSTALL-MANIFEST.json"].content.decode("utf-8"))
        manifest["legacy_retired_files"].append({
            "path": "RELEASE-MANIFEST.json",
            "sha256": kit_change.LEGACY_0_2_0_RELEASE_MANIFEST_SHA256,
        })
        manifest["legacy_retired_files"].sort(key=lambda item: item["path"])
        members["INSTALL-MANIFEST.json"] = SimpleNamespace(
            content=_canonical(manifest), mode=0o644
        )
        self._switch_release((report, members))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual("install-manifest-invalid", raised.exception.code)

    def test_modified_legacy_core_file_is_preserved_and_blocks_retirement(self) -> None:
        expected = b"# expected flat core\n"
        modified = b"# project-modified flat core\n"
        fixture = _release_fixture(
            legacy_launcher=b"@echo off\r\nlegacy\r\n",
            legacy_marker=MARKER_BYTES,
            legacy_agents=b"# Legacy kit policy\n",
            retired={"check.py": expected},
        )
        self._switch_release(fixture)
        _write(self.root, ".agent-kit.json", MARKER_BYTES)
        _write(self.root, "VERSION", b"0.2.0\n")
        _write(self.root, "kit.cmd", b"@echo off\r\nlegacy\r\n")
        _write(self.root, "AGENTS.md", b"# Legacy kit policy\n")
        _write(self.root, "check.py", modified)

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            "legacy-retired-file-modified",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )
        self.assertEqual((self.root / "check.py").read_bytes(), modified)

    def test_project_readme_cannot_be_a_legacy_retirement_target(self) -> None:
        fixture = _release_fixture(retired={"README.md": b"# project readme\n"})
        self._switch_release(fixture)

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual(raised.exception.code, "install-manifest-invalid")

    def test_project_engine_settings_cannot_be_replaceable_owned_files(self) -> None:
        report, members = _release_fixture()
        manifest = json.loads(members["INSTALL-MANIFEST.json"].content.decode("utf-8"))
        manifest["owned_files"].append(
            {
                "id": "engine-settings",
                "path": "project.godot",
                "source": "install/project.godot",
                "mode": "0644",
                "strategy": "replace",
                "legacy_sha256": None,
            }
        )
        members["install/project.godot"] = SimpleNamespace(
            content=b"[application]\n", mode=0o644
        )
        members["INSTALL-MANIFEST.json"] = SimpleNamespace(
            content=_canonical(manifest), mode=0o644
        )
        self._switch_release((report, members))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual(raised.exception.code, "install-manifest-invalid")
        self.assertFalse((self.root / "project.godot").exists())

    def test_only_the_stable_launcher_may_be_an_agent_kit_surface(self) -> None:
        report, members = _release_fixture()
        manifest = json.loads(members["INSTALL-MANIFEST.json"].content.decode("utf-8"))
        launcher = b"#!/usr/bin/env python3\nprint('managed launcher')\n"
        manifest["owned_files"].append(
            {
                "id": "stable-launcher",
                "path": ".agent-kit/launcher.py",
                "source": "tools/managed_launcher.py",
                "mode": "0755",
                "strategy": "replace",
                "legacy_sha256": None,
            }
        )
        members["tools/managed_launcher.py"] = SimpleNamespace(
            content=launcher, mode=0o755
        )
        members["INSTALL-MANIFEST.json"] = SimpleNamespace(
            content=_canonical(manifest), mode=0o644
        )
        self._switch_release((report, members))

        decision = kit_change.preview(self.root, self.archive)
        self.assertTrue(decision["approval"]["approvable"])
        kit_change.apply(
            self.root, self.archive, str(decision["approval"]["sha256"])
        )
        self.assertEqual(
            (self.root / ".agent-kit" / "launcher.py").read_bytes(), launcher
        )

        manifest["owned_files"].append(
            {
                "id": "unsafe-private-file",
                "path": ".agent-kit/other.py",
                "source": "tools/managed_launcher.py",
                "mode": "0755",
                "strategy": "replace",
                "legacy_sha256": None,
            }
        )
        members["INSTALL-MANIFEST.json"] = SimpleNamespace(
            content=_canonical(manifest), mode=0o644
        )
        self._switch_release((report, members))
        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)
        self.assertEqual(raised.exception.code, "install-manifest-invalid")

    def test_control_manifests_cannot_be_project_surface_sources(self) -> None:
        report, members = _release_fixture()
        manifest = json.loads(members["INSTALL-MANIFEST.json"].content.decode("utf-8"))
        manifest["owned_files"].append(
            {
                "id": "control-copy",
                "path": "control-copy.json",
                "source": "RELEASE-MANIFEST.json",
                "mode": "0644",
                "strategy": "replace",
                "legacy_sha256": None,
            }
        )
        members["INSTALL-MANIFEST.json"] = SimpleNamespace(
            content=_canonical(manifest), mode=0o644
        )
        self._switch_release((report, members))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual(raised.exception.code, "install-manifest-invalid")

    def test_parent_and_child_destinations_are_rejected(self) -> None:
        report, members = _release_fixture()
        manifest = json.loads(members["INSTALL-MANIFEST.json"].content.decode("utf-8"))
        for identifier, path in (("parent", "collision"), ("child", "collision/file")):
            source = f"install/{identifier}.txt"
            manifest["owned_files"].append(
                {
                    "id": identifier,
                    "path": path,
                    "source": source,
                    "mode": "0644",
                    "strategy": "replace",
                    "legacy_sha256": None,
                }
            )
            members[source] = SimpleNamespace(content=identifier.encode("ascii"), mode=0o644)
        members["INSTALL-MANIFEST.json"] = SimpleNamespace(
            content=_canonical(manifest), mode=0o644
        )
        self._switch_release((report, members))

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual(raised.exception.code, "install-manifest-invalid")

    def test_modified_legacy_owned_file_blocks_migration(self) -> None:
        expected_launcher = b"@echo off\r\necho expected legacy\r\n"
        fixture = _release_fixture(
            legacy_launcher=expected_launcher,
            legacy_marker=MARKER_BYTES,
            legacy_agents=b"# Legacy kit policy\n",
        )
        self._switch_release(fixture)
        _write(self.root, ".agent-kit.json", MARKER_BYTES)
        _write(self.root, "VERSION", b"0.2.0\n")
        _write(self.root, "kit.cmd", b"locally changed launcher\n")
        _write(self.root, "AGENTS.md", b"# Legacy kit policy\n")

        decision = kit_change.preview(self.root, self.archive)

        self.assertFalse(decision["approval"]["approvable"])
        self.assertIn(
            "owned-file-modified",
            {blocker["code"] for blocker in decision["material"]["blockers"]},
        )

    def test_upgrade_changes_only_the_managed_block(self) -> None:
        original = b"# Human project rules\n"
        _write(self.root, "AGENTS.md", original)
        first_block = self.fixture[1]["install/agents.block.md"].content
        first_identity = str(self.fixture[0]["archive_sha256"])
        self._preview_and_apply()
        current = (self.root / "AGENTS.md").read_bytes()
        outside_change = b"A human rule added between releases.\n"
        (self.root / "AGENTS.md").write_bytes(outside_change + current)
        second = _release_fixture(
            version="0.4.0",
            source_commit="d" * 40,
            launcher=b"@echo off\necho managed kit 0.4\n",
            block_body="Use the active managed kit release, version two.",
        )
        self._switch_release(second)

        decision = kit_change.preview(self.root, self.archive)
        self.assertTrue(decision["approval"]["approvable"])
        self.assertEqual(decision["material"]["operation"], "upgrade")
        result = kit_change.apply(
            self.root, self.archive, str(decision["approval"]["sha256"])
        )

        self.assertEqual(result["status"], "applied")
        agents = (self.root / "AGENTS.md").read_bytes()
        self.assertTrue(agents.startswith(outside_change + original))
        self.assertIn(second[1]["install/agents.block.md"].content, agents)
        self.assertNotIn(first_block, agents)
        state = json.loads((self.root / ".agent-kit" / "current.json").read_text("utf-8"))
        self.assertEqual(
            state["active_release"]["archive_sha256"],
            second[0]["archive_sha256"],
        )
        self.assertEqual(
            state["previous_release"]["archive_sha256"], first_identity
        )

    def test_windows_replace_retries_only_a_sharing_violation(self) -> None:
        sharing = PermissionError("file is in use")
        sharing.winerror = 32
        windows_os = mock.Mock()
        windows_os.name = "nt"
        windows_os.replace.side_effect = [sharing, None]
        with mock.patch.object(kit_change, "os", windows_os), mock.patch.object(
            kit_change.time, "sleep"
        ) as sleep:
            kit_change._replace_file(Path("temporary"), Path("destination"))

        self.assertEqual(windows_os.replace.call_count, 2)
        sleep.assert_called_once_with(0.02)


if __name__ == "__main__":
    unittest.main()
