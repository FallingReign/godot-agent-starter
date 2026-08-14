---
name: godot-multiplayer-authority
description: Use when working on Godot networked or multiplayer code — RPCs, peer-to-peer or client-server sessions, replication, spawning networked objects, or handling player input across a network. Covers who owns state, validating remote messages, desync causes, and testing netcode headlessly. Skip entirely in single-player projects. Delete this skill if the project will never have multiplayer.
---

# Multiplayer authority

Delete this skill if this project is single-player.

## First check whether this applies at all

Check `project.shape.json` for a decision on networking. If a decision says
single-player, this skill does not apply and network scaffolding is wasted
work. If there is no decision, ask before writing anything that assumes a
topology, and record the answer per
`.agents/skills/godot-project-decisions/SKILL.md`.

The rest of this skill assumes the answer is `coop-p2p` or
`authoritative-server`.

## Decide authority before writing any netcode

Every piece of state has exactly one owner, and that decision constrains all the
code that follows. Changing it later is close to a rewrite.

Write down, before implementing anything: who owns world state, who owns each
player's own state, and what happens when they disagree. If that cannot be
answered in a sentence per item, the design is not ready.

The usual shape for small cooperative sessions is one peer authoritative over
world simulation, with each player authoritative over their own input only —
never over outcomes. Fully symmetric authority requires deterministic simulation
and rollback, which is a substantially larger commitment.

## Never trust a remote message

Any remotely callable function that accepts calls from arbitrary peers must
validate the sender's identity and the message's legitimacy. "Is this peer
allowed to do this to this object right now" is a check, not an assumption.

Declare the calling mode explicitly on every remotely callable function. A
declaration without an explicit mode is an unreviewable security decision, and
this project's checks reject it.

## Desync causes, in order of frequency

**Divergent configuration.** Two peers running different tuning values produce
different outcomes from identical input, and it surfaces much later as
inexplicable behaviour. Exchange a configuration fingerprint at connection time
and refuse a mismatch. This is cheap and eliminates an entire class of confusing
bugs.

**Simulating from presentation state.** Anything derived from frame timing,
interpolation or rendering will differ between machines. Simulation must run off
fixed-step logic only.

**Unseeded or independently advanced randomness.** Shared randomness needs a
shared seed and identical advancement, or it belongs on the authority alone with
results replicated.

**Iteration order.** Ordering that varies between machines produces different
results from the same data. Sort explicitly where order affects outcome.

## Structure for testability

Keep simulation in plain classes that can be stepped without a scene. Then a
network test is: construct several instances, feed a scripted sequence of inputs,
step them, and assert their states match.

That test runs headlessly, catches desync deterministically, and is the only
practical way to find these bugs — reproducing them by hand is brutal.

Separately, run several headless instances over a loopback connection to exercise
the real transport, connection handling and message routing.

## What you cannot verify

Latency, jitter, packet loss and whether the result feels responsive. Those need
real network conditions and human judgement. Never report netcode as validated on
the basis of loopback tests alone.

## Checklist

1. Is authority written down per piece of state?
2. Does every remotely callable function declare its mode explicitly?
3. Is every peer-callable function validating the sender?
4. Is configuration fingerprinted at connection time?
5. Is simulation steppable without a scene, and tested for state agreement?
6. Have I stated that real-latency behaviour is unverified?
