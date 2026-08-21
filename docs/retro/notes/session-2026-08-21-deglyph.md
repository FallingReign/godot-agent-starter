# Deglyph session testimony: scale experiment and plan workflow

## What was proposed

The session began with a request to move beyond a one-screen world, compare
fullscreen cell sizes in a throwaway experiment, and later reimplement the
chosen size without temporary debug controls. The working comparison eventually
used stable scale IDs and a visible numeric size.

## What changed

The world was expanded, the renderer was optimized, and an optional F3
performance panel was added. A temporary F4 scale probe was implemented,
narrowed to 6, 8, 10, 12, 15, and 20 units, and then the human selected 15
units using the concept image.

The first experiment checkpoint was committed, followed by a narrowed-probe
commit. At the human's request, both commits were literally reverted to the
pre-experiment commit. The new primitives, larger map, world panning,
performance panel, experiment decisions, and committed slice notes were
therefore removed together rather than selectively.

## Human feedback

The human reported that moving between screens felt very low performance:

> This currently seems very very low performance. It was kind of laggy as I was
> transitioning around the screen.

They requested an optional performance readout:

> Could you put like a FPS in the top corner of the screen that I can turn on
> and off with a debug key? So that I can monitor the FPS as I'm playing myself.
> And Perhaps any other consumption like GPU if that's even related.

They reported launch and session-resume friction, asking for a plan control
surface that could launch the local game and make the contributing agent
session easy to reopen, potentially through the SDK:

> It's actually a bit cumbersome for me to launch the game. And it burns tokens
> for me to say run the game.

> I don't see any thing in the HTML file that shows me how to like launch the
> agent session that did the work.

They reported that the generated plan was difficult to review:

> the plan isnt coherent, im not sure this format is working. there is a lot of
> information and the mermaid diagram is often misleading.

> it needs more free form text too - I think as this plan could benefit from a
> "here is how the experiment will work and our plan after you choose your
> preference"

They asked for a rollback point and an on-screen preference reference:

> make sure you are printing an ID on the screen as a toggle so I can reference
> my preference.

After selecting 15 units, they reported that the approval view still did not
provide a usable review surface:

> the plan just shows unproposed for everything and is all red, with some green.
> the plan has never acrually successfully captured anything for me to approve,
> so far it has just been noise.

They then clarified that the rollback should include all work in the
experiment commits, and later asked whether the new primitives and retro
feedback had also been removed:

> please roll back to the commit before the experiment (if you havent) and
> update the plan accordingly

> did you also roll back lal the new primitives and retro feedback too? was it
> not a selective rollback?

The current request is to reconstruct all of this testimony so it is not lost,
then move forward with reimplementing the selected size.
