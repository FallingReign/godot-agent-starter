# Pillars

_Resolution: settled_

Four things this game commits to. They are goals rather than rules: every domain
satisfies them differently, and each domain records how. A domain that cannot
satisfy one says so plainly rather than leaving it unaddressed.

A pillar is not a specification. If a statement admits only one implementation
it is a rule, and a rule belongs to the single domain that owns it, with every
other domain linking to that owner.

## Recognition before decoding

A player should understand what a thing is by looking at it, without having
learned an alphabet first. A tree reads as a tree. A chest reads as a chest.
Stylisation is welcome; a legend the player has to memorise is not.

This is the pillar most in tension with a small shape vocabulary, and the
tension is resolved per domain. [The cell grammar](world/cells.md) sets what is
available; [readability](ui/ui.md) sets how much contrast a thing may claim.

Violated when: a player has to be told what something is before they can
identify it, or two unrelated things are indistinguishable at normal viewing
distance.

## Legible at a glance

Whatever the player needs in the next second should be readable without
stopping to look. Threat, footing, reach, what is interactive, where they can
go. Detail that does not serve that is decoration and yields to it.

Violated when: a player has to stop moving to work out what is happening, or
information needed during combat is only available in a menu.

## Playable without perfect eyes

Nothing essential is communicated by colour alone. Shape, position, motion and
outline weight carry the same information, so the game survives a colour-blind
player, a small window, and a palette swap.

This constrains [rarity](loot/rarity.md) and [icons](ui/icons.md) most, since
both are traditionally colour-first.

Violated when: removing colour from a screenshot makes it unreadable, or a
state exists that only a hue distinguishes.

## Authorable by a player

Anything the game is built from, a player can build too. That is not a feature
bolted on at the end; it is a constraint on how content is represented from the
first commit. [Content authoring](ugc/authoring.md) and
[the content format](ugc/format.md) exist because of this pillar.

Violated when: a system can only be authored by someone with the source tree,
or content produced by a player is second class.

## Suggested reading order

Not a folder structure. Read in this order to understand the game from the
player's side:

1. [The cell grammar](world/cells.md)
2. [Moving through the world](movement/movement.md)
3. [What the world is made of](world/terrain.md)
4. [How the world is structured](world/structure.md)
5. [Interface](ui/ui.md)
6. [Loot](loot/loot.md)
7. [Playing together](multiplayer/multiplayer.md)
8. [Player-made content](ugc/ugc.md)
