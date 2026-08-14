# WORKFLOW

Day-to-day loop. Assumes `SETUP.md` is complete.

## Who owns what

`AGENTS.md` is the single source of truth for ownership. Read the table there
rather than a second copy here -- an earlier version of this file carried its
own table, it drifted, and an agent read the stale one and reported the repo
as self-contradicting.

The one thing worth repeating, because it shapes every task: the gate gives a
fast, reliable "is this broken" signal and **no signal at all** on "is this
good". Design work around that.

## A turn

1. Commit first. `git add -A && git commit -m "wip: before agent turn"`.
2. Close editor tabs for any file the agent will touch, or use a worktree.
3. Prompt.
4. Agent edits, runs `python check.py`, pastes the result.
5. `git diff` and review.
6. Alt-tab to the editor. Scripts reload automatically. Press F5 and judge.
7. Commit or `git checkout -- .`.

**The agent is not done until it has pasted gate output.** Not "I ran the checks",
the actual output. Without that you are hand-reviewing generated code, which is the
thing the gate exists to avoid.

## Worktrees

Use a worktree for anything long-running or multi-file. Use tab-closing for quick
single-file edits, where a worktree is more ceremony than the change deserves.

```bash
git worktree add ../wt-agent -b agent/feature-x
cd ../wt-agent && python check.py
```

Two Godot processes must never share one project directory. They fight over `.godot/`,
which holds `uid_cache.bin`, `global_script_class_cache.cfg` and the import cache, with
no arbitration. There is no supported way to relocate `.godot`, so a separate directory
is the only isolation. Budget one reimport per worktree.

If a second Godot instance needs to run at the same time, give it explicit ports;
6005 and 6006 are fixed defaults your editor already holds:

```bash
godot --path . --lsp-port 6105 --dap-port 6106
```

Pull the work when you want to see it:

```bash
git -C /path/to/main merge agent/feature-x
git worktree remove ../wt-agent
```

**Never `git pull`, `git rebase` or `git checkout <branch>` while the editor is open.**
Branch switches are the classic trigger for the scene-overwrite bug and for reimport
storms.

## Prompt patterns

**Good.** Scoped to logic, verifiable headlessly:

> Add a stamina system in `src/scripts/logic/stamina.gd`. Plain `RefCounted`, no node
> dependency. Drains while sprinting, regenerates after 2s idle, clamps at 0 and max.
> GUT tests for drain, regen delay and both clamps. Run `python check.py` and paste output.

**Bad.** Unverifiable, and touches a file the agent does not own:

> Make the player movement feel better and add a sprint animation.

Split it: the agent writes the stamina rules and the movement maths, you tune the
numbers and wire the animation.

**When the gate fails**, paste the failing stage output back rather than describing it.
The error text is what the agent needs.

## Moving scripts

UIDs are engine-generated. When relocating a script, move both files in one commit:

```bash
git mv scripts/logic/old.gd scripts/logic/new.gd
git mv scripts/logic/old.gd.uid scripts/logic/new.gd.uid
```

Never hand-write a `uid://` value. Never copy a `.tscn` on disk to duplicate it: that
duplicates its UID and Godot may resolve references to the wrong file, with no automated
fix. Duplicate scenes inside the editor, or write a fresh file with no UID at all.

## When the agent needs a scene

It writes one. `docs/SCENES.md` has the rules; the short version is omit every optional
field, never invent a `uid://`, one root node. Two gate stages verify the result:
`sanitise` strips anything fabricated, `resources` makes the engine load and instantiate
it. Run `python check.py --only sanitise,resources` after any scene change.

Three things to expect. The editor will rewrite the file the first time it saves,
adding a header UID and stripping comments, which is normal. A passing `resources` stage
proves the scene loads, not that it looks right, so anything visual still needs your eye.
And for a genuinely complex scene, building the tree in code then using **Remote tab ->
Save Branch as Scene** is still the safest route, since the engine does the serialising.

The concurrency rule is unchanged and now carries all the weight: **close the editor, or
put the agent in a worktree.** Godot never reloads an open scene from disk and will
overwrite the file from its stale in-memory copy on the next save, with no warning and no
undo entry.

## Next additions, in order

1. **Game capture autoload.** ~20 lines, writes a PNG plus a structured state dump on a
   key press, so the agent can check its own visual work. Note `--headless` forces the
   dummy renderer and produces black or empty images, so captures need a real window.
   The state dump matters more than the screenshot: a JSON dump of every light's
   `visible`, `light_energy` and `layers` answers "why is it dark" exactly, with no
   vision tokens and no hallucination.
2. **Multi-process netcode harness.** Spawn host and two clients headlessly over
   loopback, run scripted inputs, assert identical end state. Catches desync and RPC
   authority bugs that are brutal to reproduce by hand.
3. **Editor selection plugin.** Dumps selected node plus properties to JSON so you can
   say "this asset is lacking X".
4. **Golden-frame diffing.** Only once scenes are stable enough to have baselines.
5. **C++ / GDExtension.** Only when the profiler names a specific hot path.

## When the architecture gate fails

Two different failures, two different fixes.

**"diagram is stale"** — you changed module dependencies. Run `python arch.py --write`
and commit `ARCHITECTURE.md` alongside the code change. This is routine and expected.

**"boundary violation"** — you introduced a dependency `arch.rules.json` does not
allow. This is usually real. The most common one is game logic reaching for a node
class or an autoload, which is exactly the thing that makes logic untestable.

Widening a rule is occasionally correct, but treat it as an architectural decision
rather than a fix. If an agent proposes it, ask what the alternative was.

## Adding a module

```
mkdir scripts/ai
# ... write code ...
python arch.py --write        # graph now shows scripts/ai
```

The module is drawn but unconstrained until you add it to `arch.rules.json`. Add it
once its role is clear:

```json
"scripts/ai": { "description": "behaviour trees", "may_depend_on": ["scripts/logic", "scripts/data"] }
```
