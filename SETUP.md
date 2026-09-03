# SETUP

**A safe, cross-platform runbook for a person or coding agent.**

Use the repository launcher as `.\kit.cmd` on Windows or `./kit` on macOS/Linux;
examples below shorten either form to `kit`. The launcher owns its internal
runtime, so the developer workflow is the same in PowerShell, `cmd`, Bash and
zsh and requires no shell-specific chaining.

Automation that must bind the launcher to one already-provisioned interpreter may
set process-local `KIT_PYTHON` to its absolute executable path. When present, both
launchers use only that file and fail closed if it is relative, missing or not a
file; they never fall back to another Python. It is runtime input, not kit
configuration. Ordinary developer setup should leave it unset.

Adding the kit to an existing project or upgrading an installed kit is a separate
reviewed lifecycle, not a setup repair or folder copy. Use `kit install TARGET`,
`kit upgrade TARGET`, and `kit recover SESSION_ID` as described in
[`docs/LIFECYCLE.md`](docs/LIFECYCLE.md).

The install target must already be a real Godot 4.7.2 GDScript project with a
regular `project.godot` at the project root or in one unique nested game folder.
The kit refuses an empty folder because it never invents or edits
`project.godot`. For a new game, create and close a blank Godot project first,
then install the kit.

The lifecycle safely reads the selected `project.godot`. An empty, comment-only
or normal section-first header may omit `config_version`; when present, it must
be one single-line integer assignment with value `5`. Ambiguous multiline headers are
refused. This checks the format declaration, not every setting. Full native
verification parses the file and authenticates Godot 4.7.2.

Run install, upgrade and recovery from the exact incoming extracted release, or
from a clean source checkout that produces that exact release. A different or
changed kit is refused before any project or review-session file is written.
Move to the incoming extracted release, or its matching clean source checkout,
and run the command again.

---

## 1. Detect

```text
kit doctor
kit doctor --json
```

`doctor` locates the kit through `.agent-kit.json`, loads `kit.config.json`, and
runs the setup detector in read-only, offline mode. It does not create a runtime
directory, install or download anything, edit the game or editor, initialise Git,
stage files, commit, import, format, call a model, or start the Godot executable.
It reports the discovered engine path only. Full verification and every
explicit non-gate native operation independently authenticate the engine's own
version output before doing project work.

`complete: true` means continue to verification. Otherwise, each result has
`name`, `state` (`OK`, `MISSING` or `MANUAL`), `detail` and `remedy`. A result is
evidence about current state, not permission for an automated repair.

## 2. Apply only local-safe repairs

```
kit setup repair
```

`setup repair` is intentionally narrow: it performs safe repository-local kit repairs
only. It does **not** use the network, write user/editor configuration, initialise
or mutate Git, change the game, import assets, or format GDScript. It never
creates or accepts `.gate.sha256`.

External or mutable operations each require their own flag. Run only the ones
the developer has consciously selected:

```text
kit setup dependency gdtoolkit # network and project-private tooling write
kit setup dependency gut       # network and game dependency write
kit setup dependency mermaid   # network and offline diagram-bundle write
kit setup audit-dependencies   # network; authenticate every direct lock source without publishing
kit setup editor               # user-owned Godot editor settings
kit setup repository           # initialise/version an unversioned copy
kit setup import               # Godot import/cache mutation
kit setup format               # rewrite game GDScript formatting
kit setup layout root          # select an existing root-level Godot project
kit setup layout src           # select an existing Godot project under src/
kit setup name "My Game"       # change the game project name
```

These commands make side effects visible in command history. They are never implied
by `doctor` or `setup repair`, and setup never stages or commits an existing repository's
work. Report changed paths; do not describe an attempted action as completed.
The layout choice updates only `kit.config.json`; it refuses to create, move or
rename a game and succeeds only where `project.godot` already exists.

## 3. Handle the remaining decisions and prerequisites

One at a time, wait for confirmation, then rerun `kit doctor` to verify
rather than assuming.

**`project-shape`** needs the human's three onboarding answers: the project name, a
one- or two-sentence pitch, and the `involvement` level (`hands-off`, `module`, `file`,
or `function`). Greet first, ask one question at a time, write the answers into
`project.shape.json`, then rerun `doctor`.

**`kit-context` reports the wrong game root** → select the existing layout with
`kit setup layout root` or `kit setup layout src`, then rerun `doctor`. Do not
move project files merely to satisfy the kit default.

**`godot`** is the only item that genuinely needs a person. Ask the user to download
**Godot 4.7.2, Standard** from <https://godotengine.org/download> — *not* the .NET
build. Then offer, and do the work yourself once they answer:

- *"Tell me where you saved it"* → you set `GODOT_BIN` and confirm it resolves.
  On Windows, prefer `Godot_v4.7.2-stable_win64_console.exe` for kit commands;
  the normal executable remains the interactive editor.
- *"Add it to PATH"* → give the exact command for their shell, then verify.

Do not lecture them about PATH. Ask where the file is and handle it.

`GODOT_BIN` is project input, not proof of compatibility. Diagnosis never starts
the executable. If its official-style filename names an older patch and the exact
supported binary is beside it or beside the project, the kit selects that exact
binary for its child process and reports the stale variable. If no exact replacement
exists, `doctor` blocks without launching the known-wrong executable. An
unversioned command is discovery input only. `kit setup import`, `kit godot-docs
build`, and `kit gdls start` first run one shell-free bounded version probe and
continue only when that exact resolved file reports Godot 4.7.2. A missing,
ambiguous, mismatched, crashed, or timed-out probe fails closed.

**`editor-settings` needs changes** → explain that this is a user-level edit,
ask them to close Godot, then run `kit setup editor` only
after approval. Godot rewrites that file on exit and can discard concurrent edits.

**`editor-settings` says "no editor settings file yet"** → ask them to open the project
in Godot once and close it. The file does not exist until first launch.

## 3b. Choose the import profile

**Before any asset lands in the project.** Godot writes every import parameter into each
`.import` sidecar individually rather than recording "uses default", so a later change
does not propagate. Retrofitting means deleting every `.import` file.

```
kit setup profiles
kit setup profile desktop-3d
```

Applying a profile mutates game configuration, so it is always explicit. Ask which
fits. If assets are already imported, the command warns and does nothing.

## 4. Verify

```text
kit verify
kit verify --static
kit self-test
```

Every required stage must pass. A normal developer run may report an explained
`SKIP`; production and release evidence must use `kit verify --strict`,
which refuses unsupported skips. `VERIFY.md` contains the deeper adversarial and
acceptance tests. `--static` is a process-light diagnostic for kit work and
never discovers or starts Godot. `self-test` runs the kit regression suite with
native engine execution forcibly disabled. Neither is full or release proof.

If integrity fails, stop. Only a human who has reviewed the exact protected-file
diff may run `kit integrity accept`. No setup, agent or
release command performs that action.

## 5. Hand back

Keep the handback short and truthful:

> Kit doctor is complete and verification passes. Optional actions performed:
> `<explicit actions>`. Godot `<version>` at `<path>`. What do you want to build?

---

## After setup

`AGENTS.md` is the contract. `docs/GATE.md` explains each stage and its remedy.
`docs/WORKFLOW.md` has the daily loop. `docs/RULES.md` holds the detailed rules.

## Reference: what bootstrap checks

| Check | Detector | Mutation, if selected |
| --- | --- | --- |
| `python` | Read-only | Install Python 3.10+ outside the kit |
| `git-repo` | Read-only | `kit setup repository`; never implicit and never mutates an existing repository |
| `uv` | Read-only optional presence probe | None; it is not required when the private locked tool is available |
| `gdtoolkit` | Offline exact-version advisory probe | `kit setup dependency gdtoolkit`; verifies the locked canonical PyPI wheel and installs only into private runtime |
| `godot` | Read-only | Install Godot outside the kit |
| `gut` / `mermaid` | Local-file and digest probe | `kit setup dependency gut` or `kit setup dependency mermaid`; each pinned third-party repository write is separate |
| dependency sources | None during ordinary diagnosis | `kit setup audit-dependencies`; downloads and authenticates all direct canonical artifacts without installing them |
| `warnings-block` | Read-only | Human-owned game configuration; detector only |
| `editor-settings` | Read-only | `kit setup editor`; user-level file with a backup |
| `import-cache` | Read-only | `kit setup import`; game cache mutation |

**Version pin.** Native verification requires the exact supported patch. To upgrade, change
`EXPECTED_GODOT_VERSION` in `tools/engine_discovery.py`, `EXPECTED_VERSION` in
`check.py`, and the version line in `AGENTS.md` together, then update all three
authenticated CI archives. A control-plane test rejects drift between both code
constants.

Dependency resolution is pinned by the kit rather than described as a floating
installer command. Verification may use an already available exact-version tool;
a missing gdtoolkit style adapter is advisory in ordinary setup and reports
`format`/`lint` as unsupported; strict proof rejects those skips. Only the explicit gdtoolkit
dependency operation creates a private environment. It verifies the direct wheel
against the lock, resolves binary-only transitive packages from canonical PyPI,
and never changes a global environment.

GUT is the selected third-party project-test adapter and Mermaid is an optional
third-party rendering adapter. Neither is released by the Godot project. A present
but changed adapter fails closed; an absent optional adapter is reported without an
implicit download.

## Troubleshooting

**`godot` not found after a PATH change** — the terminal must be restarted. Setting
`GODOT_BIN` avoids this.

**Editor settings patch keeps reverting** — Godot was open. Close it, rerun
`kit setup editor`, then verify with `kit doctor`.

**Dependency download fails** — keep the failed result. Check the network/proxy
and rerun the matching `kit setup dependency` operation; do not let a later verification silently count the
missing dependency as proof.

**`format` and `lint` SKIP** — no compatible local tool is available. Run the
explicit dependency action when network access is allowed, or provision the
pinned tool through the developer's normal environment manager. Strict
verification continues to fail until the dependency is genuinely available.

**Import fails on a fresh clone** — run `kit setup import`
and read the captured Godot output. The first import is slow and its exit code
alone is not trustworthy.
