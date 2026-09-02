from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tools import providers


class ProviderTests(unittest.TestCase):
    ROOT = Path("C:/project")

    def select(self, role: str, providers_value: object) -> providers.ProviderSpec:
        with mock.patch(
            "tools.providers.runtime_paths.load_config",
            return_value={"schema": 1, "providers": providers_value},
        ):
            return providers.selection(self.ROOT, role)

    def test_manual_is_the_safe_default(self) -> None:
        spec = self.select("worker", {})
        self.assertEqual(spec.kind, "manual")
        self.assertFalse(spec.automatic)
        self.assertTrue(providers.preflight(spec))

    def test_registry_rejects_arbitrary_command_and_unknown_keys(self) -> None:
        with self.assertRaises(providers.ProviderConfigError):
            self.select("worker", {"worker": {"kind": "command", "argv": ["bad"]}})

    def test_role_restricts_adapter_kind(self) -> None:
        with self.assertRaises(providers.ProviderConfigError):
            self.select("analyzer", {"analyzer": {"kind": "copilot-cli"}})

    def test_compatibility_aliases_normalize_to_closed_official_kinds(self) -> None:
        self.assertEqual(
            self.select("worker", {"worker": {"kind": "codex"}}).kind,
            "codex-cli",
        )
        self.assertEqual(
            self.select("worker", {
                "worker": {"kind": "github-copilot-cli", "persona": "kit-builder"}
            }).kind,
            "copilot-cli",
        )

    def test_provider_timeout_is_bounded_and_part_of_the_bound_record(self) -> None:
        spec = self.select(
            "worker", {"worker": {"kind": "manual", "timeout_minutes": 45}}
        )
        self.assertEqual(45, spec.timeout_minutes)
        self.assertEqual(45, spec.status()["timeout_minutes"])
        with self.assertRaises(providers.ProviderConfigError):
            self.select(
                "worker", {"worker": {"kind": "manual", "timeout_minutes": 0}}
            )

    def test_copilot_worker_fails_closed_without_host_read_boundary(
        self,
    ) -> None:
        spec = self.select(
            "worker",
            {"worker": {"kind": "copilot-cli", "model": "small", "persona": "kit-builder"}},
        )
        self.assertEqual(
            providers.preflight(spec), [providers.COPILOT_READ_BOUNDARY_BLOCKER]
        )
        with self.assertRaisesRegex(
            providers.ProviderConfigError, "host-enforced repository-scoped"
        ):
            providers.worker_command(spec)

    def test_provider_identifiers_cannot_smuggle_shell_syntax(self) -> None:
        for field, value in (("model", "small&whoami"), ("persona", "kit|builder")):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    providers.ProviderConfigError, "shell-inert"
                ):
                    self.select(
                        "worker", {"worker": {"kind": "copilot-cli", field: value}}
                    )

    def test_codex_rejects_unsupported_persona_instead_of_ignoring_it(self) -> None:
        with self.assertRaisesRegex(providers.ProviderConfigError, "do not support persona"):
            self.select("worker", {
                "worker": {"kind": "codex-cli", "persona": "kit-builder"}
            })

    def test_bound_provider_records_are_revalidated(self) -> None:
        original = providers.ProviderSpec(
            "worker", "copilot-cli", model="small", persona="kit-builder"
        )
        self.assertEqual(providers.from_record("worker", original.status()), original)
        with self.assertRaises(providers.ProviderConfigError):
            providers.from_record("worker", {
                "role": "analyzer", "kind": "copilot-cli", "automatic": True,
            })

    def test_blocked_automatic_preflight_never_probes_or_starts_host_tools(self) -> None:
        cases = (
            (
                providers.ProviderSpec("analyzer", "copilot-sdk"),
                providers.COPILOT_READ_BOUNDARY_BLOCKER,
            ),
            (
                providers.ProviderSpec(
                    "worker", "copilot-cli", persona="kit-builder"
                ),
                providers.COPILOT_READ_BOUNDARY_BLOCKER,
            ),
            (
                providers.ProviderSpec("analyzer", "codex-cli"),
                providers.CODEX_READ_BOUNDARY_BLOCKER,
            ),
            (
                providers.ProviderSpec("worker", "codex-cli"),
                providers.CODEX_READ_BOUNDARY_BLOCKER,
            ),
        )
        unexpected_probe = AssertionError("blocked preflight probed a host tool")
        with mock.patch.object(
            providers.shutil, "which", side_effect=unexpected_probe
        ), mock.patch.object(
            providers.subprocess, "run", side_effect=unexpected_probe
        ), mock.patch.object(
            providers.subprocess, "Popen", side_effect=unexpected_probe
        ), mock.patch.object(
            providers, "_copilot_package_root", side_effect=unexpected_probe
        ), mock.patch.object(
            providers, "_codex_package_root", side_effect=unexpected_probe
        ), mock.patch.object(
            providers, "copilot_sdk_path", side_effect=unexpected_probe
        ):
            for spec, blocker in cases:
                with self.subTest(role=spec.role, kind=spec.kind):
                    self.assertEqual(providers.preflight(spec), [blocker])

    def test_resume_hint_rejects_shell_metacharacters(self) -> None:
        spec = providers.ProviderSpec("worker", "copilot-cli", persona="kit-builder")
        self.assertEqual(providers.resume_command(spec, "good-session_123"),
                         "copilot --agent kit-builder --resume=good-session_123")
        self.assertEqual(providers.resume_command(spec, "x&whoami"), "")

    def test_codex_analyzer_is_recognized_but_execution_is_explicitly_unavailable(
            self) -> None:
        spec = self.select("analyzer", {"analyzer": {"kind": "openai-codex"}})
        self.assertEqual(spec.kind, "codex-cli")
        self.assertEqual(providers.preflight(spec), [providers.CODEX_READ_BOUNDARY_BLOCKER])

    @mock.patch("tools.providers._codex_launcher", return_value=["node", "codex.js"])
    def test_codex_worker_uses_exact_sandboxed_stdin_argv(
            self, _launcher: mock.Mock) -> None:
        spec = self.select("worker", {
            "worker": {"kind": "codex-cli", "model": "gpt-5.6-sol"}
        })
        self.assertEqual(providers.preflight(spec), [providers.CODEX_READ_BOUNDARY_BLOCKER])
        with self.assertRaisesRegex(providers.ProviderConfigError,
                                    "does not enforce repository-scoped reads"):
            providers.worker_command(spec)
        command = providers._codex_worker_argv(spec)

        self.assertEqual(command, [
            "node", "codex.js",
            "--ask-for-approval", "never",
            "--sandbox", "workspace-write",
            "--cd", ".",
            "--model", "gpt-5.6-sol",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--json",
            "-",
        ])
        joined = " ".join(command)
        self.assertNotIn("dangerously-bypass", joined)
        self.assertNotIn("yolo", joined)
        self.assertNotIn("search", joined)
        self.assertNotIn("login", joined)
        self.assertNotIn("install", joined)

    def test_windows_npm_shim_uses_node_entry_without_cmd(self) -> None:
        root = Path("C:/npm/node_modules/@github/copilot")
        entry = root / "npm-loader.js"

        def which(name: str) -> str | None:
            return "C:/node/node.exe" if name in ("node.exe", "node") else None

        with mock.patch("tools.providers._is_windows", return_value=True), \
                mock.patch("tools.providers._copilot_executable",
                           return_value="C:/npm/copilot.cmd"), \
                mock.patch("tools.providers._copilot_package_root", return_value=root), \
                mock.patch("tools.providers._copilot_bin_entry", return_value=entry), \
                mock.patch("tools.providers.shutil.which", side_effect=which):
            launcher = providers._copilot_launcher()
        self.assertEqual(["C:/node/node.exe", str(entry)], launcher)

    def test_codex_launcher_uses_only_authenticated_package_entry(self) -> None:
        root = Path("C:/npm/node_modules/@openai/codex")
        entry = root / "bin" / "codex.js"

        def which(name: str) -> str | None:
            return "C:/node/node.exe" if name in ("node.exe", "node") else None

        with mock.patch("tools.providers._codex_executable",
                        return_value="C:/npm/codex.cmd"), \
                mock.patch("tools.providers._codex_package_root", return_value=root), \
                mock.patch("tools.providers._codex_bin_entry", return_value=entry), \
                mock.patch("tools.providers.shutil.which", side_effect=which):
            launcher = providers._codex_launcher()
        self.assertEqual(["C:/node/node.exe", str(entry)], launcher)

    def test_sdk_path_resolves_the_export_beside_a_windows_npm_shim(self) -> None:
        root = Path("C:/npm")
        package_root = root / "node_modules" / "@github" / "copilot"
        expected = package_root / "sdk" / "index.js"
        with mock.patch("tools.providers._copilot_package_root", return_value=package_root), \
                mock.patch.object(Path, "resolve", autospec=True) as resolve, \
                mock.patch.object(Path, "is_file", autospec=True, return_value=True):
            resolve.side_effect = lambda path, strict=False: path
            actual = providers.copilot_sdk_path()
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
