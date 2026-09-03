# Install and upgrade the kit

The kit can be added to an existing Godot project and upgraded later without
copying a repository over that project.

## Quick read

1. Start with a real Godot 4.7.2 GDScript project and close Godot.
2. Build or obtain one verified kit release.
3. Run `kit install TARGET` or `kit upgrade TARGET` from that release.
4. Open the exact local review link printed by the command.
5. If D1 appears, choose the game folder and select **Review this folder**.
6. Review the affected files, then choose **Add kit** or **Upgrade kit**.
7. Keep the result, or choose **Restore** to put the prior project bytes back.
8. Start a fresh agent chat and confirm it loaded the new kit rules.

The review does not change game files, design, project instructions, Git, or the
active kit. It may retain the release and review session in private controller
storage so an interrupted review can be recovered.

The local review server listens only on `127.0.0.1`. Closing the browser does
not stop it. From the same incoming kit, run `kit serve stop` when the review is
finished.

## Project requirement

The target must already contain one regular `project.godot` at the project root,
or one unique `project.godot` in a nested game folder. An empty folder is not a
project and is refused. The kit never invents or edits `project.godot`.

Preview also stops before writing when the project has a root
`AGENTS.override.md`, when the resulting root `AGENTS.md` would exceed 32 KiB,
or when a `.csproj` is present at the project root or selected game root. The
override would hide the kit rules from Codex, an oversized instructions file may
be read only in part, and C# projects are not supported by this release. Remove
or rename the override, shorten the instructions, or use a supported GDScript
project, then Preview again.

`TARGET` always means the game project. Do not use `--project` for the target.
The global form `kit --project PATH COMMAND` selects a different kit root and,
when needed, must appear before the lifecycle command.

For a new game:

1. Create a blank Godot 4.7.2 project using GDScript.
2. Close Godot.
3. Install the kit into that project.

## Build a release

From a clean canonical kit checkout:

```text
kit release build PATH/agent-kit-0.3.0.zip
```

This uses a closed file list. It excludes the source game, current design,
proposal, retrospective state, private runtime, and other project-specific data.

## Install into an existing project

From the incoming extracted release:

```text
kit install PATH/TO/PROJECT
```

From a source checkout, name the built release explicitly:

```text
kit install PATH/TO/PROJECT --release PATH/TO/agent-kit-0.3.0.zip
```

If the project contains more than one possible `project.godot`, the review asks
one D1 question. Choosing a folder creates a new exact review; it does not edit
the project. The button says **Review this folder** because this step cannot
apply the kit.

The kit running the command must be the exact incoming extracted release, or a
clean source checkout that produces that exact release. A different version or
a changed source checkout is refused before any project or review-session file
is written. Run the command from the incoming extracted release, or from its
matching clean source checkout.

## Upgrade an installed kit

Use the incoming release, not the project's older active kit core:

```text
kit upgrade PATH/TO/PROJECT
```

Or, from a source checkout:

```text
kit upgrade PATH/TO/PROJECT --release PATH/TO/agent-kit-0.3.0.zip
```

Upgrade authenticates the old active core and its ownership record before it
trusts any file as kit-owned. A human-edited file or managed section is never
silently reclaimed by changing the install record.

The project's older active kit cannot control an upgrade to a newer release.
Run the upgrade from the incoming extracted release, or from the clean source
checkout that produces it.

## Public review commands

Browser buttons and agent actions use the same exact session. Run these commands
from the incoming kit that created the review:

```text
kit change list
kit change status FULL_SESSION_ID
kit change apply FULL_SESSION_ID --plan-sha256 FULL_REVIEW_SHA256
kit change restore FULL_SESSION_ID --result-sha256 FULL_RESULT_SHA256
```

`list` finds retained full session IDs. `status` reopens the exact local review.
`apply` accepts only the full fingerprint from that review. `restore` accepts
only the full result fingerprint produced after Apply. Codex and Copilot
must use these public commands when browser actions are unavailable; internal
Python files are not a public interface.

## What the Apply stage may change

- Versioned kit code is written under `.agent-kit/releases/`.
- The stable `kit` and `kit.cmd` launchers select the active verified core.
- One marked kit section is added to or updated in `AGENTS.md` and the Copilot
  bridge. Text outside marked sections stays project-owned.
- Missing generic starter files may be created. A missing `ARCHITECTURE.md` is
  rendered from the destination's real module graph and shown in the exact
  review. Existing create-only project choices are preserved.
- `project.godot`, game source, design, proposal, retrospective state, Git state,
  export settings, imports, assets, and add-ons are not rewritten by the
  lifecycle.

The active release pointer changes last. Exact prior bytes are stored before the
first project-facing write.

## Safety boundary

The lifecycle refuses existing redirected paths, links, altered kit files and
case collisions. It rechecks exact bytes before each change and before Restore.
Review sessions and journals use checksums to detect accidental damage; they do
not prove who wrote a file.

Another process running as the same operating-system user can still rewrite
files during an already-running command. That is outside this kit's security
boundary. Do not run two kit changes or another file-writing agent against the
same project at once. The lifecycle lock prevents two cooperating kit changes;
it cannot contain unrelated software that ignores the lock.

## Storage and cleanup

Version 0.3.0 never automatically removes managed releases, review sessions,
transaction journals, or Restore backups. A successful upgrade remains
restorable, and an older release may still be part of that Restore chain. Do
not manually delete `.agent-kit/releases`, `.kit/runtime/upgrade`, or the
controller storage. A future separately reviewed cleanup command can remove
only records that have first been explicitly made non-restorable.

## Result states

| State | Meaning | Next action |
| --- | --- | --- |
| Ready | The exact change can be applied | Review and choose Add kit or Upgrade kit |
| Needs a decision | One folder choice is unresolved | Answer D1 and review the new plan |
| Complete | The Apply-time check passed when Apply finished | Ask your agent to run `kit verify --static` for the current project |
| Adoption required | Project problems were recorded at Apply | Keep the kit and address the recorded problems separately |
| Restored | The prior project bytes were restored | Review or retry when ready |
| Recovery required | Work stopped between durable steps | Run the recovery command below |

The Apply-time check runs once when Apply finishes. Its stored result records
when it ran. Later project changes are not included. Ask your agent to run
`kit verify --static` when you need the current project state.

### Existing-problem baseline

At Apply, the kit records existing problems only when each problem is tied to
an exact file. The baseline covers:

- formatting;
- lint;
- scene file hygiene;
- banned old Godot patterns;
- typed data boundaries;
- architecture graph freshness and architecture boundaries;
- test files the runner would miss;
- asset files missing `.import` sidecars.

An unchanged recorded problem stays visible and produces **Adoption required**;
it is never described as fixed. A new problem, or a recorded problem that
remains in a changed file, fails the Apply-time check. The installed kit remains
usable while unchanged problems are addressed separately.

An absent `.gutconfig.json` is not an existing file problem, so Apply does not
invent a test layout or add one to the baseline. Apply also does not download or
execute a project-owned style tool when no trusted external `gdformat` or
`gdlint` is available. These checks are named as **Adoption required**, and
strict verification remains incomplete until they are ready. A present but
invalid test config is recorded against the exact file.

## Recover an interrupted change

Run recovery from the same incoming kit that created the review:

```text
kit recover FULL_SESSION_ID
```

The labelled session ID is printed by `install` or `upgrade`. Recovery reads the
durable journal and continues or restores the exact known state; it does not guess
which write completed. It does not run a new project check. When the resulting state
is finished or still needs recovery, it starts or reuses the local review page and
prints a fresh exact link. If recovery still cannot finish, the command prints the
specific problem. A third version of any protected file stops automatic Restore and
leaves the conflict visible.

## Legacy 0.2.0 projects

The managed lifecycle recognizes the exact verified 0.2.0 release. Known legacy
kit files can be migrated into the managed layout. Modified legacy files are
preserved and block automatic replacement so the human can decide what to keep.

## Provider behavior

Codex and Copilot receive the same managed project contract. The project bridge
identifies the project root, active kit core, and configured game root. Agents
read game design from the project and kit workflow rules from the active core;
an upgrade never treats the old core as the controller for a newer review.

After Apply, start a fresh Codex or Copilot chat before game work. The chat that
performed the change may still hold the old kit rules. In Codex, check the active
instruction sources. In Copilot, run `/instructions` and check References. Stop
if the new managed project instructions are not shown.
