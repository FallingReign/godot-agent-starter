# SETUP

**A runbook for the coding agent, not a checklist for the human.**

If you are a person: open a terminal here, start your coding agent, say *"let's begin"*.
Stop reading.

---

## 1. Detect

```
python bootstrap.py --json
```

`complete: true` → skip to step 4. Otherwise continue. Each result has `name`, `state`
(`OK` / `MISSING` / `MANUAL`), `detail` and `remedy`. `MISSING` means the script can fix
it; `MANUAL` means a human must act.

## 2. Fix everything automatable

```
python bootstrap.py --fix
```

Initialises Git with a baseline commit, installs `uv`, downloads GUT into `src/addons/gut`,
normalises GDScript formatting with the local gdtoolkit, generates `.gate.sha256`,
patches Godot editor settings, and runs the first import. Idempotent.

Report what changed in one or two sentences. Do not paste raw output.

## 3. Handle the `MANUAL` items

One at a time, wait for confirmation, then re-run `--json` to verify rather than assuming.

**`godot`** is the only item that genuinely needs a person. Ask the user to download
**Godot 4.7.x, Standard** from <https://godotengine.org/download> — *not* the .NET
build. Then offer, and do the work yourself once they answer:

- *"Tell me where you saved it"* → you set `GODOT_BIN` and confirm it resolves.
- *"Add it to PATH"* → give the exact command for their shell, then verify.

Do not lecture them about PATH. Ask where the file is and handle it.

**`editor-settings` says "Godot is running"** → ask them to close it, re-run `--fix`.
Godot rewrites that file on exit and would discard the patch.

**`editor-settings` says "no editor settings file yet"** → ask them to open the project
in Godot once and close it. The file does not exist until first launch.

## 3b. Choose the import profile

**Before any asset lands in the project.** Godot writes every import parameter into each
`.import` sidecar individually rather than recording "uses default", so a later change
does not propagate. Retrofitting means deleting every `.import` file.

```
python bootstrap.py --list-import-profiles
python bootstrap.py --import-profile desktop-3d
```

Ask which fits. If assets are already imported, the command warns and does nothing.

## 4. Verify

```
python check.py
```

Every stage must PASS or SKIP. Then offer the user `VERIFY.md` (~15 min), which
deliberately breaks each gate to prove it fires. A silently disabled gate is worse than
no gate. Work through it if they want it and report each result.

## 5. Hand back

Commit, then keep it short:

> Setup is done: repository initialised, GUT installed, editor settings patched, gate
> passing. Godot 4.7.1 at `<path>`. What do you want to build first?

---

## After setup

`AGENTS.md` is the contract. `docs/GATE.md` explains each stage and its remedy.
`docs/WORKFLOW.md` has the daily loop. `docs/RULES.md` holds the detailed rules.

## Reference: what bootstrap checks

| Check | Automatable | Notes |
| --- | --- | --- |
| `python` | No | 3.8+. Almost always present |
| `git-repo` | Yes | `git init` plus baseline commit. Sets a local identity if none exists |
| `uv` | Yes | Advisory. OK if `gdlint` already resolves without it |
| `gdtoolkit` | Yes | Via `uvx --from gdtoolkit==4.*`, nothing installed permanently |
| `godot` | **No** | Requires a download |
| `gut` | Yes | From the GitHub release into `src/addons/gut` |
| `warnings-block` | No | `src/project.godot` is human-owned; reports rather than edits |
| `editor-settings` | Yes | Patches the `.tres` with a timestamped backup. Refuses while Godot runs |
| `import-cache` | Yes | Headless import. Exit code deliberately ignored |

**Version pin.** A mismatch is a warning, not a failure. To upgrade, change
`EXPECTED_GODOT` in `bootstrap.py`, `EXPECTED_VERSION` in `check.py`, and the version
line in `AGENTS.md` together.

**Why `uvx` over `pip install`.** No virtualenv, no PATH surprises when pip's Scripts
directory is missing on Windows, and the major version pinned at point of use.
`check.py` falls back to a PATH binary or `python -m gdtoolkit.*`.

## Troubleshooting

**`godot` not found after a PATH change** — the terminal must be restarted. Setting
`GODOT_BIN` avoids this.

**Editor settings patch keeps reverting** — Godot was open. Close it, re-run `--fix`.

**GUT download fails** — install from the editor AssetLib instead, into
`res://addons/gut/`.

**`format` and `lint` always SKIP** — no `uv` and no `gdlint`. Either
`pip install --user "gdtoolkit==4.*"` or install `uv`. `GDTOOLKIT_OFFLINE=1` skips the
network probe on an air-gapped machine.

**Import fails on a fresh clone** — run `godot --headless --path . --import --quit` once
manually and read the output. The first import is slow and its exit code is unreliable.
