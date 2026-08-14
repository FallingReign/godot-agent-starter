---
name: godot-headless-verification
description: Use when verifying Godot work without a human present — running the engine from the command line, writing or running unit tests, checking whether code compiles, interpreting engine exit codes, or deciding whether a change can be proven correct. Also use when tempted to claim a game runs or looks correct, when a headless run appears to hang, or when a test suite produces no output.
---

# Verifying work headlessly

## The core distinction

Everything you do falls into one of two categories, and conflating them is the
most damaging thing you can do to a human's trust.

**Provable:** it parses, it type-checks, it loads, it instantiates, an assertion
holds, the project boots. These you verify, and you paste the evidence.

**Not provable without eyes:** whether it looks right, feels right, reads
clearly, is well composed, is responsive, performs acceptably on a device.
These you never claim. State plainly that they need human confirmation.

A headless run cannot render. So you can never conclude from a successful
headless launch that a game "is running" in any sense a human would recognise,
and you must not report it that way.

## Exit codes are not trustworthy

The engine has documented cases of exiting non-zero on a completely successful
operation, and of exiting zero while printing a hard error. Reading only the exit
code will produce both false failures and false successes.

**Always scan the output text as well.** If a project's checks already do this,
use them rather than invoking the engine directly. If you must invoke it
directly, grep for error markers and apply a hard timeout, because the engine can
hang rather than fail.

## Design for verifiability

The single decision that determines how much you can prove is where logic lives.
Logic in plain classes with no scene dependency can be constructed, stepped and
asserted on completely headlessly. Logic inside node callbacks cannot be verified
at all.

So the proportion of behaviour you can prove is a design outcome, not a tooling
outcome. Push logic across that line deliberately.

## Tests

Test behaviour, not your own implementation. A test that asserts the same
constant the implementation contains proves nothing.

Cover the boundaries specifically: malformed input, unknown fields, version
mismatch, empty collections, out-of-range values. Those are where real defects
live, and they are cheap to assert.

Two hazards in headless test runs. A test awaiting a signal that never fires
hangs rather than fails, because there is typically no per-test timeout — prefer
awaiting something guaranteed, or add a timeout. And a run that collects zero
tests can report success, so confirm from the output that tests actually ran
rather than trusting the process status.

## A hang is usually not slowness

Unit test suites of this kind finish in well under a second. If a run appears to
hang, the likely cause is the engine sitting at an interactive prompt, or output
being buffered so nothing appears until the process exits. Check the log for a
repeated message; a runaway loop looks nothing like slow work.

## Reporting

Every completion report should contain: the verification command, its result, and
an explicit list of anything a human still needs to look at. If that list is
empty for visual work, you have made a mistake.
