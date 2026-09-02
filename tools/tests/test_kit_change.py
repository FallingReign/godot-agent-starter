#!/usr/bin/env python3
"""Focused safety tests for transactional kit install and upgrade."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import kit_change  # noqa: E402


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

    def test_one_nested_godot_project_selects_its_parent(self) -> None:
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

    def test_hardlinked_managed_target_blocks_preview(self) -> None:
        agents = _write(self.root, "AGENTS.md", b"# Human project rules\n")
        alias = self.root / "agents-hardlink-source.md"
        try:
            os.link(agents, alias)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable on this host: {exc}")

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
        source = self.root / "core-hardlink-source.py"
        source.write_bytes(member.read_bytes())
        member.unlink()
        try:
            os.link(source, member)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable on this host: {exc}")

        with self.assertRaises(kit_change.KitChangeError) as raised:
            kit_change.preview(self.root, self.archive)

        self.assertEqual("installed-kit-untrusted", raised.exception.code)

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
        with mock.patch.object(kit_change.os, "name", "nt"), mock.patch.object(
            kit_change.os, "replace", side_effect=[sharing, None]
        ) as replace, mock.patch.object(kit_change.time, "sleep") as sleep:
            kit_change._replace_file(Path("temporary"), Path("destination"))

        self.assertEqual(replace.call_count, 2)
        sleep.assert_called_once_with(0.02)


if __name__ == "__main__":
    unittest.main()
