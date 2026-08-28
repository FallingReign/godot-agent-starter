# The gate

```text
kit verify
kit verify --static
kit verify --strict
kit self-test
```

Python 3.10 or newer powers the launcher's internal runtime. `kit` is the stable
public entry point: it resolves `.agent-kit.json`, reports one consistent result, and supports
`--project PATH` and `--json` on every platform. Normal verification permits only
the gate's explicitly explained skips; `--strict` refuses a green result whose
proof depends on an unavailable stage. `--static` runs every process-light stage
without discovering or launching Godot, so it is useful for kit work but is not
completion or release proof. Low-level stage diagnostics remain available through
`kit verify --stage NAME`; internal scripts are not part of the public workflow.

The canonical maintainer checkout is the sole exception to strict's blanket skip
rule. Its exact release-excluded marker at `src/.kit-maintainer-fixture` permits
only `shape (absent)`, `design`
and `conformance`, because the kit source intentionally has no game proposal or
design state. A wrong or absent marker, or any additional skip, still fails closed.
The marker never ships, so an extracted kit receives no exception.

`integrity` always runs first and stops immediately when protected-file review is
required. Every other static stage then completes before the native-engine
boundary. Godot is health-checked only at that boundary; after the first failed
native check, every later engine stage is skipped to prevent repeated crash
dialogs. Human-facing verification streams stage output live.

`kit self-test` is separate from the project gate. It exercises the kit control
plane and sets an enforced no-engine boundary inherited by its test processes.

Stages are grouped as follows:

| Stage | What it proves |
| --- | --- |
| `integrity` | The protected gate is unmodified. Changes to the configured game root's `project.godot` are noted, not failed |
| `skills` | `.agents/skills/*/SKILL.md` frontmatter is valid, so skills actually load |
| `schema` | every key in `proposal.json` and `project.shape.json` is declared; proposal status, design authority, canonical design digests and the reversibility envelope are internally consistent, so an invented or malformed field fails instead of being silently discarded |
| `shape` | the decision log is well formed and free of placeholder answers |
| `design` | parses orthogonal resolution, authority, authorship and confidence metadata; rejects malformed metadata and incomplete agent-authored disclosure; computes canonical SHA-256 with generated bindings excluded; regenerates `docs/design/INDEX.md` and tunable bindings; reports legacy sections that need authority migration and design/code tunable mismatches |
| `conformance` | Every authored game input vs `proposal.json` when `status` is `recorded` or `approved`, including custom/extensionless content, game-owned tools, project tests, scenes, resources, assets, GDScript and deletions. If `proposal.json` is absent, Git must prove there are no authored changes; otherwise implementation has no design authority and fails. The inverse scope excludes only known generated, private, engine-owned and third-party surfaces. Each `new`/`modify`/`delete` declaration is checked against an exact resolvable `baseline_sha` at the configured involvement depth. Hands-off declarations bind coarse file/directory `scope[]`; module declarations bind complete allowed outgoing dependencies and boundary data; function declarations bind class-qualified identity, contiguous annotations, a typed canonical signature and normalized semantic body change. Git, architecture, baseline, source parsing and untracked-file discovery fail closed. A draft authorizes no implementation. Recorded work is hands-off, reversible and never approval; agent-provisional recorded work requires exact `very-high` confidence. Approved work additionally requires the current `approval_sha256` and active append-only human confirmation entries matching `docs_at`, `design_sha256` and `proposal_sha256`. Full and strict verification reject declared work that is still unbuilt; static verification may display it as planning state. Missing ancestry, stale digests, active rejections, unproposed deviations and hard-to-undo work without approval all fail |
| `format` | Optional third-party gdtoolkit adapter: `gdformat --check` |
| `lint` | Optional third-party gdtoolkit adapter: `gdlint` |
| `sanitise` | Scenes carry no fabricated UIDs and are structurally sound |
| `import` | Assets import, class cache is warm |
| `typecheck` | Per-file `--check-only`, warnings as errors. Errors grouped, every `file:line` shown |
| `grep` | No banned Godot 3 idioms |
| `types` | No untyped external data reaching the interior layer |
| `tests` | No test-shaped file the runner would silently ignore |
| `arch` | Module graph current, no boundary violations |
| `assets` | Every asset has a committed `.import` sidecar |
| `resources` | Every `.tscn` and `.tres` loads AND instantiates in the engine |
| `gut` | Project unit tests through the selected third-party GUT adapter |
| `smoke` | The project boots headlessly |

## Low-level diagnostic flags

```
kit verify --stage typecheck          # repeatable
kit verify --static                   # every non-engine stage; diagnostic only
kit verify --fast                     # skip import when only .gd changed
kit setup format                      # scoped format fix
kit integrity accept                  # HUMAN ONLY, re-baselines .gate.sha256
```

Logs land in `.checklogs/<stage>.log`.

Neither `verify` mode installs dependencies, runs bootstrap repairs, edits user
configuration, imports on behalf of setup, calls a model, or accepts integrity
changes. Readiness is the separate read-only command `kit doctor`.

## Remediation per stage

**`integrity` fails** — **stop.** Do not modify the gate, do not run
`--accept-gate-changes`. Report which file drifted and hand back to the human. A gate
you can edit is not a gate. If `.gate.sha256` is missing or incomplete, restore the
shipped file from version control or the distribution; the gate never creates a new
trust root from the files it is meant to verify.

**`schema` fails** — run `kit schema describe proposal` or `kit schema describe
shape`, then correct only the named source field. Do not invent an alias or add a
gate exception. Approval fields (`approved_by`, `approved_on`,
`approval_sha256`) and authority events are cockpit-owned human decisions; never
hand-fill them to satisfy validation.

**`design` fails** — repair the exact named `docs/design/` section. Resolution,
authority, authorship and confidence are independent metadata. Agent-authored
design must remain provisional, use exact `very-high` confidence before any
reversible implementation, and contain the Quick Read, inference, assumptions,
veto scope and next go/no-go sections. A legacy or malformed section requires
explicit migration and cockpit re-review; age and prior prose are not authority.

**`conformance` fails** — use the reported file and reason. Invalid/missing Git
proof or an unresolved baseline is a stop, not permission to compare the whole
tree approximately. Authored changes with no `proposal.json` are implementation
without authority: retrieve or write the design and record the reversible plan
before continuing. A stale design or proposal digest requires re-reading and
re-review. An unproposed file, module, action or function is a real deviation:
record the revision, return the proposal to `draft`, and use `kit serve start`
for a new exact decision. Do not widen the proposal after the fact or reuse a
legacy approval. A deleted input must be declared `delete`; an unfamiliar
extension remains authored scope. At function involvement, the exact typed
signature and body delta must match, and a missing/unparseable baseline fails
closed. At module involvement, a dependency outside `may_depend_on` is an
unapproved boundary change.

**`format` fails** — `kit setup format`. Never bare `gdformat .`; it is
unscoped and reformats the configured game root's `addons/`, which is
third-party code.

**`lint` reports SKIP** — gdtoolkit is not resolvable, not a style failure. `typecheck`
is the correctness gate; `lint` is style only. gdtoolkit has also historically crashed
on newly added GDScript syntax, so a normal developer run reports that limitation
honestly. A strict/release proof rejects the unsupported skip rather than presenting
partial coverage as complete coverage. After explicit network approval, acquire the
locked canonical tool with `kit setup dependency gdtoolkit`; diagnosis and verification
never install it themselves.

**`sanitise` fails** — `kit sanitize --write` applies the mechanical fixes
(fabricated `uid=`, `unique_id`, `load_steps`). Structural errors — two roots, a root
carrying `parent=`, an unresolvable `parent=` path, duplicate sibling names — are **not**
auto-fixable and need a real correction. See `SCENES.md`.

**`typecheck` fails** — fix the code. Never lower a warning in the configured
game root's `project.godot`.

Output is grouped: one entry per distinct error message, a `[xN]` count, and every
affected `file:line` beneath it. 57 errors of 4 kinds prints 4 entries, not 57 lines.
Fix the first location, re-run, repeat — a falling count means you are converging.
Do not re-read whole files hunting for an error the gate has already located.

Strict mode rejects several forms an LLM produces by default, mostly untyped loop
iterators and unnarrowed `Variant` values. Worked examples taken from code in this
reviewed project-neutral examples: **`docs/GDSCRIPT.md`** (generated — regenerate with
the kit's internal documentation generator during maintenance).

**Symbol navigation.** The public helper queries Godot's language server and is
advisory, never a gate stage:

```text
kit gdls start
kit gdls status
kit gdls diagnose scripts/logic/example.gd
kit gdls symbols scripts/logic/example.gd
kit gdls refs Example
kit gdls stop
```

`refs` works with no server via a text scan. The helper accepts port 6105 only
when the exact retained receipt, native lock, PID and process identity all still
match; a foreign listener or reused PID is refused. `status` is read-only and
surfaces a retained engine which disappeared after startup as unresolved native
safety evidence. Stop the helper before running the gate — a live Godot instance
holds `.godot/`, which the gate writes to.

**`grep` fails** — replace the idiom. Full table in `RULES.md`. If a flagged line is
genuinely correct, append `# gate:allow` to it, use that sparingly, and mention it in
your summary.

**`shape` skips** when `project.shape.json` is absent or onboarding is incomplete.
It fails on malformed fields, invalid involvement, placeholder answers such as
`unknown`/`tbd`, dangling decision references, or stale answered questions. An
undecided choice is an absent entry, not a recorded non-answer.

**`tests` fails** when a `.gd` file looks like a test but does not match the runner's
configured dirs, prefix and suffix. Such a file is collected by nothing and reported by
nothing, so the suite passes having never run it — a false green, which is why it blocks.
Scripts attached to a `.tscn` are exempt automatically, since their scene runs them.
It also prints an advisory list of logic modules with no matching test file.

**Layer directories are configurable.** `types` reads `type_boundary.interior` and
`.boundary` from `arch.rules.json`, so reorganising into feature folders or adding a
tier does not require editing a hash-pinned gate file.

**`types` has two grades.** Loose containers in a file that also parses external data
**block** — that is the untyped-data leak, and it converges by containment. Loose
containers in a parse-free file are **advisory**: heterogeneous storage such as a
component registry is inherently loose in GDScript, and no boundary work removes it.
Confine those behind a typed facade instead.

**`types` fails** when the interior layer has a bare `Dictionary`, `Array` or `Variant` in
a signature, or calls `JSON.parse_string` / `FileAccess.open` / `ConfigFile` directly.
Do **not** fix this by adding casts at the use sites: GDScript has no safe cast from
`Variant`, so each cast reveals the next error and the loop never terminates. Move the
conversion into `<game_root>/scripts/data/`, return a typed object, and change
the logic signature to accept it. See `GDSCRIPT.md`. A deliberate exception
takes a trailing `# gate:allow`.

**`arch` fails** two distinct ways. A **stale graph** means dependencies changed without
regeneration: run `kit architecture update` and commit. A **boundary violation** means a
dependency `arch.rules.json` does not permit: fix the code, do not widen the rule
without asking.

**`assets` fails** — run `kit verify --stage import`, then commit the generated
`.import` sidecars. Missing `.uid` sidecars are advisory, not fatal; Godot assigns them.

**`resources` fails** — the engine could not load or instantiate a scene. The log names
the file. Usually a bad `ext_resource` path or a malformed node header.

**`gut` fails** — read the `gut.log` path printed by the gate. Zero collected
tests is a FAIL, not a pass.

GUT is a pinned third-party adapter, not a Godot release component. Godot's own
[`--test` / doctest infrastructure](https://docs.godotengine.org/en/4.7/engine_details/architecture/unit_testing.html)
tests the C++ engine and GDScript implementation, requires an engine built with
`tests=yes`, and explicitly is not a runner for user project scripts. Replacing GUT
means replacing this isolated adapter and its collection/evidence contract; it does
not mean compiling a custom engine for every game.

**`smoke` fails** — the project does not boot. Check the main scene and its script.

**A native crash, timeout or concurrent-launch refusal occurs** — stop every
native launch. The gate records a bounded diagnostic in the private verification
ledger; the cockpit and `kit retro status` surface the unresolved consequence.
Use `kit verify --static` and `kit self-test` for safe diagnostics, but neither
clears the warning. Ask the human before one bounded full retry. Only a successful
complete full or strict native verification resolves the native failure record.
The retry is digest-bound rather than a boolean: copy the exact SHA-256 shown by
the warning into `kit verify --confirm-native-retry <sha256>`. The launcher issues
a private one-run token; the native boundary atomically consumes it at the first
engine launch and binds it to that exact warning, verification run and Python
process. Other commands, processes and warning revisions remain refused. The
token is never an argument, report field or native-child environment variable.

## Engine quirks the script works around

Do not "fix" these; they are deliberate.

- `--headless --import --quit` can exit non-zero on a clean import, so its exit code is
  ignored and output is grepped instead.
- Other Godot commands can exit **zero while printing hard errors**, so output is
  grepped there too.
- **Stages after `typecheck` are skipped when parsing is broken.** Loading GDScript that
  does not parse tells you nothing new, and under `-d` Godot's debugger breaks, prompts
  for input, and spins — an uncapped run produced a 165 MB log in the field. stdin is
  closed on every invocation and logs are capped at 2 MB, keeping the tail.
- **`arch.rules.json` and the configured game root's `project.godot` are advisory,
  not pinned.** Adding a module is a real architecture change that needs a real
  edit, and Godot rewrites `project.godot`
  whenever files are added. Changes are surfaced as a note for review rather than failing.
- GUT exits zero when it collects no tests at all, so a missing summary is treated as a
  failure.
- Every invocation has a hard timeout; Godot can hang on import.
- Native launches are serialized by an OS-backed cross-process guard, including
  stale-lock recovery. An abandoned authentic engine lock is visible to read-only
  diagnosis; the next mutating command persists an unresolved warning before it
  removes the stale lock, and refuses that triggering launch. An unreadable or
  indeterminate owner is never recovered. Output is drained continuously into a fixed-size tail
  rather than accumulated in memory. On Windows launches run without a console
  window, inherit an error mode that suppresses modal Application Error / WER
  dialogs, and live inside a `KILL_ON_JOB_CLOSE` Job Object whose active-process
  count must reach zero. On POSIX the owned group has an independent parent
  lifeline: timeout/cancellation requests `SIGINT`, then a bounded hard fallback
  kills the group and verifies it after reaping the leader. Nested command
  supervisors cascade that lifeline, so terminating an outer strict verifier
  cannot silently leave its checker or bounded engine behind.
- Native children receive a documented allowlist of OS execution, user-data,
  temporary-directory, display and locale variables. Verification nonces, retry
  tokens, provider credentials and secret-like host variables are not inherited.
- A deliberately retained language-server process is authenticated by its PID,
  unguessable lock token, creation marker and executable identity. Startup first
  writes a recoverable `starting` journal, proves the exact live owner, binds the
  listener to that PID where the OS exposes it, completes a real LSP initialize,
  then atomically marks the journal `ready`. Any `BaseException` takes the exact
  cleanup path; an interrupted handoff remains visibly recoverable. Stop refuses
  PID reuse or unreadable identity and preserves the lock unless termination of
  the identity-bound root process is proven. Linux uses a pidfd and Windows uses
  an authenticated process handle; POSIX systems without an identity-bound
  signalling primitive refuse automated retained-process termination.
- Two commands intentionally retain authenticated children beyond their short
  launcher: `kit gdls start`, and cockpit `kit serve start`/`open`. Only those
  outer invocations permit Windows Job breakaway, and only the exact GDLS or
  board child requests `CREATE_BREAKAWAY_FROM_JOB`. Every other command remains
  non-breakaway and fully tree-contained.
- Windows access-violation status and signal-style native exits are classified as
  `native-crash`; timeout and concurrent-launch refusal remain distinct. A safe
  static follow-up cannot overwrite unresolved native evidence. Recovery uses an
  exact compare-and-swap snapshot, so a newer or unreadable warning cannot be
  cleared by an older passing verification.
- The native group stops before project work when the engine does not report the
  exact supported patch version.
- Explicit non-gate operations use the same rule: setup import, local API-doc
  generation and language-server startup first bind the resolved executable,
  its file identity and an exact engine-reported 4.7.2 version. Filename or PATH
  discovery alone is never execution authority.
- `--check-only` runs per file, not once for the project, because it reports only the
  first file's errors otherwise.

The boundary does not claim what the host cannot prove. Power loss and an
uncatchable whole-host termination leave only the durable lock/warning evidence
for the next run. Linux closes the child-spawn/lifeline gap with `PDEATHSIG`;
other POSIX kernels have a very small interval between process creation and the
independent guard becoming ready because the Python standard library exposes no
atomic parent-death primitive there. If guard startup fails, the launch is killed
and refused. Automated stop of a deliberately retained server is likewise
refused on a POSIX host without an identity-bound signalling primitive.

## Assets and import defaults

Drop source files in and run `kit verify --stage import`. Godot generates the
`.import` sidecar; commit it alongside the source. Never hand-edit one.

Import defaults live in the configured game root's `project.godot` under
`[importer_defaults]`, set once by `kit setup profile <name>`. **Do not change
them casually.** Godot
writes every parameter into each `.import` file individually rather than recording "uses
default", so changing a default does not propagate to anything already imported.
Adopting a new profile means deleting every `.import` file and reimporting.

If the import cache becomes inconsistent, deleting `.godot/` entirely and re-running the
import stage is the normal repair, not a sign something is broken.

## Scenes are agent-writable because of two stages

`sanitise` is a deterministic text pass with no third-party dependency. `resources` asks
the engine itself. Together they give scenes a hard pass/fail, which is the criterion
everything else in this kit uses to decide what an agent may own.

## Skill pointers

Several stages print a `skill:` line on failure, naming the on-demand knowledge
file in `.agents/skills/` that covers the failure. Load it rather than guessing
again — the pointer exists because reference docs sitting in `docs/` were
repeatedly not read while an agent iterated blindly on the same error.

`typecheck` routes conditionally: `Variant` and inference errors point at the
typed-data boundary skill, unknown-identifier errors point at API lookup.

## Looking up an API

```
kit godot-docs build                      # once, dumps the class reference
kit godot-docs show Color                 # class summary
kit godot-docs show Color.from_string     # exact signature
kit godot-docs search from_str            # find a member across all classes
```

Generated by the installed engine binary, so it describes exactly the engine that
will run this project. Never read the generated XML directly; it is roughly a
thousand files. The dump lives in `.godot_doc/` and is gitignored.

## The `skills` stage

A skill whose frontmatter `name` does not match its folder name is **silently
ignored** by the agent runtimes that read this directory. No error is raised; the
knowledge simply never loads. Because that failure is invisible, it is checked:
frontmatter present, `name` matching the folder, lowercase-hyphenated, and a
`description` present and within limits.

## involvement and undeclared modules

The `shape` stage validates `involvement` if present and fails on a value that is
not `hands-off`, `module`, `file` or `function`. A typo there would silently drop the human
to the least-oversight level, which is the worst possible default, so it is an
error rather than a fallback. The stage also prints the resulting obligation on
every run, because a rule the agent re-reads each run is more reliable than one it
read once at session start.

The `arch` stage reports modules that exist in code but are absent from
`arch.rules.json`. It reports rather than fails: at `hands-off` a module may be
legitimate when it is covered by a `recorded`, reversible proposal, and a
declared-but-unbuilt module is normal mid-slice. The value is the difference
between recorded or approved intent and built reality in one place.
