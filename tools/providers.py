#!/usr/bin/env python3
"""Closed provider adapters for optional model-backed kit operations.

The kit's deterministic capture, planning, verification, and release paths do
not need a model provider. A provider is consulted only after an explicit
human action that spends quota (retrospective analysis or worker dispatch).
The registry is closed so a repository configuration cannot smuggle an
arbitrary shell command into an approval click.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_paths

ROLE_KINDS = {
    "analyzer": frozenset({"manual", "copilot-sdk", "codex-cli"}),
    "worker": frozenset({"manual", "copilot-cli", "codex-cli"}),
}
KIND_ALIASES = {
    "codex": "codex-cli",
    "openai-codex": "codex-cli",
    "github-copilot-cli": "copilot-cli",
    "github-copilot-sdk": "copilot-sdk",
}
NODE_MIN = 24
CODEX_READ_BOUNDARY_BLOCKER = (
    "Codex CLI execution is unavailable because its sandbox does not enforce "
    "repository-scoped reads"
)
COPILOT_READ_BOUNDARY_BLOCKER = (
    "Automatic Copilot execution is unavailable because the official adapter "
    "does not establish a host-enforced repository-scoped read/write boundary"
)
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}\Z")
_PERSONA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ProviderConfigError(ValueError):
    """Provider configuration is malformed or unsafe."""


@dataclass(frozen=True)
class ProviderSpec:
    role: str
    kind: str
    model: str = ""
    persona: str = ""
    timeout_minutes: int = 30

    @property
    def automatic(self) -> bool:
        return self.kind != "manual"

    def status(self) -> dict[str, Any]:
        return {
            "automatic": self.automatic,
            "kind": self.kind,
            "model": self.model,
            "persona": self.persona,
            "role": self.role,
            "timeout_minutes": self.timeout_minutes,
        }


def _text(value: object, field: str, limit: int = 128) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProviderConfigError(f"provider {field} must be a string")
    text = value.strip()
    if len(text) > limit or any(ord(char) < 32 for char in text):
        raise ProviderConfigError(f"provider {field} must be at most {limit} printable characters")
    return text


def _parse_spec(role: str, value: object) -> ProviderSpec:
    if role not in ROLE_KINDS:
        raise ProviderConfigError(f"unknown provider role: {role!r}")
    if not isinstance(value, dict):
        raise ProviderConfigError(f"providers.{role} must be an object")
    unknown = set(value) - {"kind", "model", "persona", "timeout_minutes"}
    if unknown:
        raise ProviderConfigError(
            f"providers.{role} has unsupported key(s): {', '.join(sorted(unknown))}"
        )
    kind = _text(value.get("kind") or "manual", "kind", 32)
    kind = KIND_ALIASES.get(kind, kind)
    if kind not in ROLE_KINDS[role]:
        raise ProviderConfigError(
            f"providers.{role}.kind must be one of {', '.join(sorted(ROLE_KINDS[role]))}"
        )
    model = _text(value.get("model"), "model")
    persona = _text(value.get("persona"), "persona")
    if model and _MODEL_ID.fullmatch(model) is None:
        raise ProviderConfigError(
            "provider model must be a shell-inert model identifier"
        )
    if persona and _PERSONA_ID.fullmatch(persona) is None:
        raise ProviderConfigError(
            "provider persona must be a shell-inert agent identifier"
        )
    timeout_minutes = value.get("timeout_minutes", 30)
    if (not isinstance(timeout_minutes, int) or isinstance(timeout_minutes, bool)
            or not 1 <= timeout_minutes <= 240):
        raise ProviderConfigError(
            f"providers.{role}.timeout_minutes must be an integer from 1 to 240"
        )
    if role == "worker" and kind == "copilot-cli" and not persona:
        persona = "kit-builder"
    if kind == "codex-cli" and persona:
        raise ProviderConfigError("providers using codex-cli do not support persona")
    if role == "analyzer" and persona:
        raise ProviderConfigError("providers.analyzer.persona is not supported")
    return ProviderSpec(
        role=role, kind=kind, model=model, persona=persona,
        timeout_minutes=timeout_minutes,
    )


def selection(root: Path, role: str) -> ProviderSpec:
    if role not in ROLE_KINDS:
        raise ProviderConfigError(f"unknown provider role: {role!r}")
    config = runtime_paths.load_config(root)
    configured = config.get("providers")
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise ProviderConfigError("providers must be an object")
    return _parse_spec(role, configured.get(role, {"kind": "manual"}))


def from_record(role: str, value: object) -> ProviderSpec:
    """Restore a provider bound to a queued run without trusting runtime JSON."""
    if not isinstance(value, dict):
        raise ProviderConfigError("queued run has no valid provider record")
    recorded_role = value.get("role")
    if recorded_role not in (None, role):
        raise ProviderConfigError(
            f"queued provider role {recorded_role!r} does not match {role!r}"
        )
    automatic = value.get("automatic")
    spec = _parse_spec(role, {
        "kind": value.get("kind"),
        "model": value.get("model"),
        "persona": value.get("persona"),
        "timeout_minutes": value.get("timeout_minutes", 30),
    })
    if automatic is not None and bool(automatic) != spec.automatic:
        raise ProviderConfigError("queued provider automatic flag is inconsistent")
    return spec


def _copilot_executable() -> str | None:
    names = ("copilot.exe", "copilot.cmd", "copilot")
    return next((found for name in names if (found := shutil.which(name))), None)


def _codex_executable() -> str | None:
    names = ("codex.exe", "codex.cmd", "codex")
    return next((found for name in names if (found := shutil.which(name))), None)


def _copilot_package_root(executable: str | None = None) -> Path | None:
    """Resolve and authenticate the installed official npm package root."""
    executable = executable or _copilot_executable()
    if not executable:
        return None
    try:
        shim = Path(executable).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    candidates = (
        shim.parent,
        shim.parent / "node_modules" / "@github" / "copilot",
        shim.parent.parent / "lib" / "node_modules" / "@github" / "copilot",
    )
    for candidate in candidates:
        package = candidate / "package.json"
        try:
            root = candidate.resolve(strict=True)
            value = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, RuntimeError, UnicodeError, ValueError):
            continue
        if (isinstance(value, dict) and value.get("name") == "@github/copilot"
                and root.is_dir()):
            return root
    return None


def _copilot_bin_entry(package_root: Path) -> Path | None:
    """Return the package-declared CLI entry without trusting a shell shim."""
    try:
        value = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        configured = value.get("bin") if isinstance(value, dict) else None
        relative = configured.get("copilot") if isinstance(configured, dict) else configured
        if not isinstance(relative, str) or not relative.strip():
            return None
        raw = Path(relative)
        if raw.is_absolute() or ".." in raw.parts:
            return None
        entry = (package_root / raw).resolve(strict=True)
        entry.relative_to(package_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None
    return entry if entry.is_file() else None


def _copilot_launcher() -> list[str] | None:
    """Return a shell-free argv prefix for the installed Copilot CLI.

    Windows npm installs expose ``copilot.cmd``. Passing a multiline human
    prompt through that batch file would reintroduce command-shell parsing.
    Resolve the package-declared entry and invoke it with Node directly instead;
    fail closed if that cannot be established.
    """
    executable = _copilot_executable()
    if not executable:
        return None
    suffix = Path(executable).suffix.lower()
    if suffix == ".exe":
        return [executable]
    if os.name != "nt":
        return [executable]
    package_root = _copilot_package_root(executable)
    entry = _copilot_bin_entry(package_root) if package_root is not None else None
    node = shutil.which("node.exe") or shutil.which("node")
    if node and entry is not None:
        return [node, str(entry)]
    return None


def _codex_package_root(executable: str | None = None) -> Path | None:
    """Resolve the installed official @openai/codex npm package root."""
    executable = executable or _codex_executable()
    if not executable:
        return None
    try:
        shim = Path(executable).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    candidates = (
        shim.parent,
        shim.parent / "node_modules" / "@openai" / "codex",
        shim.parent.parent / "lib" / "node_modules" / "@openai" / "codex",
    )
    for candidate in candidates:
        try:
            root = candidate.resolve(strict=True)
            value = json.loads((candidate / "package.json").read_text(encoding="utf-8"))
        except (OSError, RuntimeError, UnicodeError, ValueError):
            continue
        if (isinstance(value, dict) and value.get("name") == "@openai/codex"
                and root.is_dir()):
            return root
    return None


def _codex_bin_entry(package_root: Path) -> Path | None:
    """Return Codex's authenticated package-declared CLI entry."""
    try:
        value = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        configured = value.get("bin") if isinstance(value, dict) else None
        relative = configured.get("codex") if isinstance(configured, dict) else configured
        if not isinstance(relative, str) or not relative.strip():
            return None
        raw = Path(relative)
        if raw.is_absolute() or ".." in raw.parts:
            return None
        entry = (package_root / raw).resolve(strict=True)
        entry.relative_to(package_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None
    return entry if entry.is_file() else None


def _codex_launcher() -> list[str] | None:
    """Return a shell-free launcher authenticated as official @openai/codex."""
    executable = _codex_executable()
    package_root = _codex_package_root(executable)
    entry = _codex_bin_entry(package_root) if package_root is not None else None
    node = shutil.which("node.exe") or shutil.which("node")
    if node and entry is not None:
        return [node, str(entry)]
    return None


def copilot_sdk_path() -> Path | None:
    """Resolve the SDK export shipped with the installed Copilot CLI.

    A CLI shim on PATH is not evidence that Node can import the SDK: global npm
    packages are not searched by ESM resolution, and the supported export is
    ``@github/copilot/sdk`` rather than the package root. Return the concrete
    entry point that a bridge can import by file URL.
    """
    package_root = _copilot_package_root()
    if package_root is None:
        return None
    try:
        candidate = (package_root / "sdk" / "index.js").resolve(strict=True)
        candidate.relative_to(package_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _node_major() -> int | None:
    executable = shutil.which("node")
    if not executable:
        return None
    try:
        output = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"v(\d+)", output)
    return int(match.group(1)) if match else None


def preflight(spec: ProviderSpec) -> list[str]:
    """Return blockers without installing, authenticating, or spending quota."""
    if spec.kind == "manual":
        return [
            f"{spec.role} provider is manual; configure an explicit adapter in "
            f"{runtime_paths.CONFIG_NAME} before using automatic {spec.role} actions"
        ]
    if spec.kind == "copilot-cli":
        problems = [] if _copilot_launcher() else [
            "Copilot CLI has no safe shell-free launcher on PATH"
        ]
        problems.append(COPILOT_READ_BOUNDARY_BLOCKER)
        return problems
    if spec.kind == "copilot-sdk":
        problems: list[str] = []
        major = _node_major()
        if major is None:
            problems.append(f"Node.js >= {NODE_MIN} is not available on PATH")
        elif major < NODE_MIN:
            problems.append(f"Node.js {major} is installed; Copilot SDK needs >= {NODE_MIN}")
        if copilot_sdk_path() is None:
            problems.append("The @github/copilot/sdk entry point is not available from the installed CLI")
        problems.append(COPILOT_READ_BOUNDARY_BLOCKER)
        return problems
    if spec.kind == "codex-cli":
        problems = [] if _codex_launcher() else [
            "Official @openai/codex has no authenticated shell-free launcher on PATH"
        ]
        problems.append(CODEX_READ_BOUNDARY_BLOCKER)
        return problems
    return [f"unsupported provider kind: {spec.kind}"]


def worker_command(spec: ProviderSpec) -> list[str]:
    """Build a project-scoped, edit-only non-interactive worker command.

    An allowed shell command is not a security boundary: Git aliases/hooks and
    mutable repository launchers can execute arbitrary programs. The provider
    therefore receives only file discovery/read/edit tools. Shell, URL, memory,
    MCP, sub-agent and temporary-directory access are unavailable; the trusted
    host validates scope, runs verification, creates the commit and integrates
    it after the provider exits. The approved prompt is supplied on stdin from
    the sealed prompt file, never as a command-line argument.
    """
    if spec.role != "worker" or spec.kind not in ("copilot-cli", "codex-cli"):
        raise ProviderConfigError(f"{spec.kind} cannot launch an automatic worker")
    if spec.kind == "codex-cli":
        raise ProviderConfigError(CODEX_READ_BOUNDARY_BLOCKER)
    raise ProviderConfigError(COPILOT_READ_BOUNDARY_BLOCKER)


def _codex_worker_argv(spec: ProviderSpec) -> list[str]:
    """Proposed bounded argv, retained as a tested contract but not executable.

    The flags close writes, approvals, persistence, search and prompt argv. They
    do not close host-wide reads, so :func:`worker_command` fails closed until
    the dispatcher can supply a real repository-scoped read boundary.
    """
    if spec.role != "worker" or spec.kind != "codex-cli" or spec.persona:
        raise ProviderConfigError("invalid codex-cli worker specification")
    launcher = _codex_launcher()
    if not launcher:
        raise ProviderConfigError(
            "Official @openai/codex has no authenticated shell-free launcher on PATH"
        )
    command = [
        *launcher,
        "--ask-for-approval", "never",
        "--sandbox", "workspace-write",
        "--cd", ".",
    ]
    if spec.model:
        command.extend(["--model", spec.model])
    command.extend([
        "exec", "--ephemeral", "--ignore-user-config", "--json", "-",
    ])
    return command


def resume_command(spec: ProviderSpec, session_id: str) -> str:
    """Human-readable resume hint for known adapters; never executed."""
    clean = _text(session_id, "session_id", 256)
    if clean and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}", clean) is None:
        return ""
    if spec.kind == "copilot-cli" and clean:
        return f"copilot --agent {spec.persona or 'kit-builder'} --resume={clean}"
    return ""
