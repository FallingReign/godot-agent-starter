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
| `schema` | every key in `proposal.json` and `project.shape.json` is a declared field, so an invented one fails here instead of being silently discarded by every reader |
| `shape` | the decision log is well formed and free of placeholder answers |
| `design` | regenerates `docs/design/INDEX.md` and the tunable binding tables, then reports a declared tunable no code claims, a tunable claimed in code no section declares, a design-declared tunable bound to a `const`, and a section with no resolution line. Regenerates rather than failing on staleness: tuning values change constantly, and a gate that fails on every balance tweak trains people to stop regenerating |
| `conformance` | Code vs `proposal.json` when `status` is `approved`, including each file's declared `create`/`modify` against the tree at `baseline_sha`, at the depth `involvement` implies. A draft is awaiting review and is not diffed. Structure without `experience`, or a dangling `design_refs` entry, fails; absent design ancestry warns honestly |
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

**Symbol navigation.** For go-to-definition and find-references, `tools/gdls.py`
queries Godot's language server. Advisory, never a gate stage. `refs` works with no
server running via a text scan. Stop the language-server helper before running the
gate — a live Godot instance holds `.godot/`, which the gate writes to.

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
- Native launches are serialized. On Windows they run without a console window,
  inherit an error mode that suppresses modal Application Error / WER dialogs, and
  a timeout terminates only the process tree owned by that invocation.
- The native group stops before project work when the engine does not report the
  exact supported patch version.
- `--check-only` runs per file, not once for the project, because it reports only the
  first file's errors otherwise.

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
`arch.rules.json`. It reports rather than fails: at `hands-off` an undeclared
module is legitimate, and a declared-but-unbuilt module is normal mid-slice. The
value is the difference between approved intent and built reality in one place.
