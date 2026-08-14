---
name: godot-lifecycle-and-signals
description: Use when writing Godot node code that runs per frame, initialises, or communicates between nodes — _ready, _init, _process, _physics_process, _input, signals, connect, await, node references, get_node, timers, or async sequences. Also use when something works in one scene but not another, when a null node reference appears at startup, when a signal connection never fires, or when deciding whether logic belongs in a frame callback.
---

# Lifecycle and signals

## Frame callbacks are for presentation, not decisions

The most common structural mistake in Godot code is putting game logic in a
per-frame callback. It runs every frame, it cannot be unit tested, and it hides
ordering bugs.

Frame callbacks should read input, interpolate visuals, and call into logic
objects. The decisions themselves belong in plain classes that can be stepped
deliberately in a test.

Two distinct callbacks exist and confusing them causes jitter: one runs as often
as the frame rate allows and is for visuals and input; the other runs at a fixed
rate and is for physics and anything that must be deterministic. Simulation goes
in the fixed one, or better, in a logic object the fixed one calls.

If you find yourself writing state transitions, cooldown arithmetic or rule
evaluation inside a frame callback, move it.

## Initialisation order

Construction and tree-entry are different moments, and node references are not
available at the earlier one. Reaching for a child too early yields null, which
is the usual cause of a startup crash that only happens in one scene.

Order matters in both directions: children are notified before parents on entry.
Code that depends on a sibling or parent being fully set up cannot assume it is,
so prefer having the parent drive its children explicitly over having children
reach outward.

Anything cached from the tree should be resolved at tree-entry, not at
construction, and should tolerate absence rather than assuming presence.

## Signals

Signals are for a node telling anyone who cares that something happened. They
are not a general-purpose way to call a known object — if you know exactly who
should react, call it directly.

Use the modern object-based connection form. The legacy string-based form from
Godot 3 still parses in Godot 4 and **never fires**, producing no error at all.
It is the single most damaging idiom in this engine, and no type checker or
linter catches it. The gate greps for it explicitly.

Connections do not survive being packed into a scene unless made persistent, so
a connection created at runtime is a runtime-only connection.

## Await

Awaiting is fine, but two failure modes matter.

A signal that never fires produces a coroutine that never resumes. In a test
this manifests as a hang rather than a failure, because the test framework has
no per-test timeout. Prefer awaiting something guaranteed to occur, or add your
own timeout.

An object freed while a coroutine is suspended on it leaves the resumption
pointing at nothing. Check validity after any await that spans frames.

## Cleanup

Anything you connect, allocate or start should be undone when the node leaves
the tree. Connections to a longer-lived object are the common leak: the emitter
outlives the listener and keeps a dead reference.

## Checklist

1. Is there logic in a frame callback that could live in a plain class?
2. Is simulation in the fixed-rate callback rather than the variable one?
3. Are node references resolved at tree-entry, and is absence handled?
4. Are all connections the modern form, never the legacy string form?
5. Does every await have a guaranteed resumption or a timeout?
6. Is everything connected or started also torn down?

## Connecting in the editor or in code

Both are legitimate and practice is genuinely split, so this is a judgement
call rather than a rule. The useful discriminator:

**Connect in code** when the connection must always exist for the thing to
function. A button that does nothing without its handler, a timer whose timeout
drives state. Code connections are compiler-visible: a `Callable` breaks loudly
when the method is renamed, whereas an editor connection stores the method as a
name string and fails silently.

**Connect in the editor** when the connection is level design or optional - this
particular switch opens that particular door. A designer should be able to
rewire it without touching a script, and the wiring is data about this level
rather than logic about the class.

What matters more than the choice: a reusable sub-scene should not carry
editor connections to nodes outside itself, because they break on reuse.

## Direction of communication

Call downward, signal upward. A parent may call methods on its children,
because it knows they exist. A child must not reach up or sideways by node
path - it emits a signal and lets whoever cares connect.

For anything genuinely distant, use groups or a single event bus rather than
threading references through intermediate nodes. Each reference threaded through
a node that does not use it is a coupling you will have to unpick later.

## If the fork is the human's to make

The trade-offs above are yours to weigh, but some forks are not technical
choices at all - they turn on scope, audience, art direction or how much the
project intends to carry. If the right answer here depends on something like
that and nothing in `project.shape.json` decides it, raise it rather than
picking, and record the answer per
`.agents/skills/godot-project-decisions/SKILL.md`.
