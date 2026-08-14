# Retrospectives

A retro asks whether the kit did its job: guide the agent so you intervened as
little as possible. Every correction you had to make is a candidate kit defect.

## Running one

```
python tools/retro.py --print     # evidence pack + prompt, no model call
python tools/retro.py --sdk       # hand the pack to copilot, resume the thread
python tools/retro.py --why       # is a retro warranted right now?
```

`--print` has no dependencies and works with any agent. `--sdk` needs node 22
or newer and the `copilot` CLI on PATH, and exists for one reason: it persists
a thread, so a retro can be reopened and probed further within the same slice
rather than starting cold. If preflight fails it says why and the pack is still
written.

The gate prints a one-line nudge when a retro is warranted. It never calls a
model, so it cannot spend your allowance.

## What is in this folder

| File | Committed | What it is |
| --- | --- | --- |
| `YYYY-MM-DD-slice.md` | yes | findings and proposed goals, the durable record |
| `evidence.json` | no | regenerated on demand |
| `prompt.md` | no | regenerated on demand |
| `thread.json` | no | SDK thread id for the current slice |
| `.ran` | no | per-slice lock, stops a retro firing twice |

## Triggers

An automatic retro fires at most once per slice, and only on: a slice
completing with an approved proposal and a gate run, one stage failing three
or more times in a session, a command repeated four or more times, a file
rewritten four or more times, or three or more human messages hinting at
friction.

The lock is not optional. A trigger that fires twice spends twice.

## What the evidence pack cannot see

It reads committed history, logged turns and gate logs. It does not see
attempts inside a single turn. Six tries at one command in one turn leave no
artefact, so the friction that cost the most can be invisible.

That gap is yours to close. A one-line note while it is annoying you is worth
more than a reconstruction afterwards.

## Findings are proposals

Nothing in a retro report is applied automatically, and an agent must not
change a rule, skill or gate file because of its own retro. A retro that can
act on its own conclusions accretes plausible rules nobody agreed to, and the
kit gets worse without anyone noticing.

Promote what you agree with. Delete the rest.
