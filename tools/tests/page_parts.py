#!/usr/bin/env python3
"""Read the ordering out of a generated (or browser-rendered) retro page.

Both the byte tests and `browser_check.py` need to answer the same question --
"which cards, in what order, are inside #list-toaction?" -- and a second
implementation of that walk would be a second thing to keep right. One walk,
imported by both, so what the unit test asserts and what the browser test
asserts are the same measurement.
"""
from __future__ import annotations

import re

CARD_RE = re.compile(r'<div class="finding[^"]*"([^>]*)>')
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def section(html: str, list_id: str) -> str:
    """The inner HTML of one list container, by walking div depth.

    A regex cannot balance tags and a card contains divs, so this counts
    openings and closings rather than pretending the first </div> ends it.
    """
    m = re.search(r'<div id="%s"[^>]*>' % re.escape(list_id), html)
    if not m:
        return ""
    i, depth = m.end(), 1
    while i < len(html):
        opened = html.find("<div", i)
        closed = html.find("</div>", i)
        if closed < 0:
            return html[m.end():]
        if 0 <= opened < closed:
            depth += 1
            i = opened + 4
            continue
        depth -= 1
        if depth == 0:
            return html[m.end():closed]
        i = closed + 6
    return html[m.end():]


def cards(html: str, list_id: str) -> list[dict[str, str]]:
    """Every finding card in one list, in document order, as its data-* attrs."""
    out: list[dict[str, str]] = []
    for m in CARD_RE.finditer(section(html, list_id)):
        out.append(dict(ATTR_RE.findall(m.group(1))))
    return out


RANK_RE = re.compile(r'<span class="rank">([^<]*)</span>')


def ranks(html: str, list_id: str) -> list[str]:
    """The rank label rendered on each card of one list, in document order."""
    part = section(html, list_id)
    return [m.group(1).strip() for m in RANK_RE.finditer(part)]
