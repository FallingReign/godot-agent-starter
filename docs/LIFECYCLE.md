# Install and upgrade the kit

The kit can be added to an existing Godot project and upgraded later without
copying a repository over that project.

## Quick read

1. Build or obtain one verified kit release.
2. Run `kit install TARGET` or `kit upgrade TARGET` from that release.
3. Open the exact local review link printed by the command.
4. Review the affected files, then choose **Apply**.
5. Keep the result, or choose **Restore** to put the prior project bytes back.

The review does not change game files, design, project instructions, Git, or the
active kit. It may retain the release and review session in private `.kit/runtime`
storage so an interrupted review can be recovered.

## Build a release

From a clean canonical kit checkout:

```text
kit release build PATH/agent-kit-0.3.0.zip
```

This uses a closed file list. It excludes the source game, current design,
proposal, retrospective state, private runtime, and other project-specific data.

## Install into an existing project

From a built or extracted release:

```text
kit install PATH/TO/PROJECT
```

From a source checkout, name the built release explicitly:

```text
kit install PATH/TO/PROJECT --release PATH/TO/agent-kit-0.3.0.zip
```

If the project contains more than one possible `project.godot`, the review asks
one D1 question. Choosing a folder creates a new exact review; it does not edit
the project.

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

## What Apply may change

- Versioned kit code is written under `.agent-kit/releases/`.
- The stable `kit` and `kit.cmd` launchers select the active verified core.
- One marked kit section is added to or updated in `AGENTS.md` and the Copilot
  bridge. Text outside marked sections stays project-owned.
- Missing generic starter files may be created. Existing create-only project
  choices are preserved.
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
| Ready | The exact change can be applied | Review and choose Apply |
| Needs a decision | One folder choice is unresolved | Answer D1 and review the new plan |
| Complete | The Apply-time check passed when Apply finished | Ask your agent to run `kit verify --static` for the current project |
| Adoption required | Project problems were recorded at Apply | Keep the kit and address the recorded problems separately |
| Restored | The prior project bytes were restored | Review or retry when ready |
| Recovery required | Work stopped between durable steps | Run the recovery command below |

The Apply-time check runs once when Apply finishes. Its signed result records
when it ran. Later project changes are not included. Ask your agent to run
`kit verify --static` when you need the current project state.

An existing project problem is not described as fixed. A new or worsened
problem fails the Apply-time check. A valid static project failure can therefore
produce **Adoption required** while the installed kit itself remains usable.

## Recover an interrupted change

Run recovery from the same incoming kit that created the review:

```text
kit recover FULL_SESSION_ID
```

The session ID is printed by `install` or `upgrade`. Recovery reads the durable
journal and continues or restores the exact known state; it does not guess which
write completed. A third version of any protected file stops automatic Restore
and leaves the conflict visible.

## Legacy 0.2.0 projects

The managed lifecycle recognizes the exact verified 0.2.0 release. Known legacy
kit files can be migrated into the managed layout. Modified legacy files are
preserved and block automatic replacement so the human can decide what to keep.

## Provider behavior

Codex and Copilot receive the same managed project contract. The project bridge
identifies the project root, active kit core, and configured game root. Agents
read game design from the project and kit workflow rules from the active core;
an upgrade never treats the old core as the controller for a newer review.
