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

    @mock.patch("tools.providers._copilot_launcher", return_value=["copilot"])
    def test_worker_command_is_argv_not_shell(self, _launcher: mock.Mock) -> None:
        spec = self.select(
            "worker",
            {"worker": {"kind": "copilot-cli", "model": "small", "persona": "kit-builder"}},
        )
        command = providers.worker_command(spec)
        self.assertEqual(command[:3], ["copilot", "--agent", "kit-builder"])
        self.assertNotIn("-p", command)
        self.assertFalse(any("prompt" in argument for argument in command))
        permission = next(arg for arg in command if arg.startswith("--allow-tool="))
        self.assertEqual("--allow-tool=read,write", permission)
        self.assertIn(
            "--available-tools=view,grep,glob,edit,create,apply_patch", command
        )
        self.assertIn("--deny-tool=shell,url,memory,write(.git)", command)
        self.assertIn("--disallow-temp-dir", command)
        self.assertIn("--no-ask-user", command)
        self.assertIn("--no-auto-update", command)
        self.assertIn("--no-remote", command)
        self.assertIn("--no-remote-export", command)
        self.assertIn("--disable-builtin-mcps", command)
        self.assertNotIn("--allow-all-tools", command)
        self.assertNotIn("--allow-all-paths", command)
        self.assertEqual(command[-2:], ["--model", "small"])

    def test_provider_identifiers_cannot_smuggle_shell_syntax(self) -> None:
        for field, value in (("model", "small&whoami"), ("persona", "kit|builder")):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    providers.ProviderConfigError, "shell-inert"
                ):
                    self.select(
                        "worker", {"worker": {"kind": "copilot-cli", field: value}}
                    )

    def test_bound_provider_records_are_revalidated(self) -> None:
        original = providers.ProviderSpec(
            "worker", "copilot-cli", model="small", persona="kit-builder"
        )
        self.assertEqual(providers.from_record("worker", original.status()), original)
        with self.assertRaises(providers.ProviderConfigError):
            providers.from_record("worker", {
                "role": "analyzer", "kind": "copilot-cli", "automatic": True,
            })

    def test_analyzer_preflight_is_read_only_and_specific(self) -> None:
        spec = providers.ProviderSpec("analyzer", "copilot-sdk")
        with mock.patch("tools.providers._node_major", return_value=20), mock.patch(
            "tools.providers.copilot_sdk_path", return_value=None
        ):
            problems = providers.preflight(spec)
        self.assertEqual(len(problems), 2)
        self.assertIn("needs >= 24", problems[0])

    def test_windows_npm_shim_uses_node_entry_without_cmd(self) -> None:
        root = Path("C:/npm/node_modules/@github/copilot")
        entry = root / "npm-loader.js"

        def which(name: str) -> str | None:
            return "C:/node/node.exe" if name in ("node.exe", "node") else None

        with mock.patch("tools.providers.os.name", "nt"), \
                mock.patch("tools.providers._copilot_executable",
                           return_value="C:/npm/copilot.cmd"), \
                mock.patch("tools.providers._copilot_package_root", return_value=root), \
                mock.patch("tools.providers._copilot_bin_entry", return_value=entry), \
                mock.patch("tools.providers.shutil.which", side_effect=which):
            launcher = providers._copilot_launcher()
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
