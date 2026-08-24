"""Minimal markdown -> HTML. Deterministic, stdlib only, no network.

Pre-rendered at generation time rather than fetched in the browser, because
file:// blocks fetch() in Chrome and Edge: a plan opened by double-clicking
cannot load a sibling .md file at all. Inlining is the only thing that works
for the way this page is actually opened.

Deliberately small. It handles what the kit's own documents use and nothing
more. A fenced mermaid block becomes <div class="mermaid"> so the same diagram
renderer that draws the architecture graph also draws diagrams inside docs.
"""
from __future__ import annotations

import html
import re
from typing import List

_INLINE = (
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)"), r"<em>\1</em>"),
)

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)")

# Anything that is not plainly a document reference is dropped. Repo docs are
# authored by an agent, so a javascript: or data: URL reaching the rendered
# page is a real vector rather than a theoretical one.
_SAFE_SCHEME = re.compile(r"^(?:https?:|mailto:|#|[./]|[A-Za-z0-9_][\w./-]*$)")


def _safe_href(url: str) -> str:
    url = url.strip()
    if ":" in url.split("/")[0] and not _SAFE_SCHEME.match(url):
        return ""
    if not _SAFE_SCHEME.match(url):
        return ""
    return url


def _links(text: str) -> str:
    def repl(m: re.Match) -> str:
        label, url = m.group(1), _safe_href(m.group(2))
        if not url:
            return label
        return f'<a href="{url}">{label}</a>'
    return _LINK.sub(repl, text)


def _inline(text: str) -> str:
    out = html.escape(text, quote=True)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return _links(out)


def render(md: str) -> str:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # fenced code, including mermaid
        m = re.match(r"^\s*```+\s*(\w*)\s*$", line)
        if m:
            lang = m.group(1).lower()
            i += 1
            body: List[str] = []
            while i < n and not re.match(r"^\s*```+\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            i += 1
            text = "\n".join(body)
            if lang == "mermaid":
                # Same container the architecture graph uses, so one renderer
                # covers both. Raw text: mermaid.js parses it, not the browser.
                out.append(f'<div class="mermaid">{html.escape(text)}</div>')
            else:
                cls = f' class="lang-{lang}"' if lang else ""
                out.append(f"<pre><code{cls}>{html.escape(text)}</code></pre>")
            continue

        # heading
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            i += 1
            continue

        # table: a header row followed by a separator row of dashes
        if (line.lstrip().startswith("|") and i + 1 < n
                and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1])):
            def cells(row: str) -> List[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            head = cells(line)
            i += 2
            rows: List[List[str]] = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>"
                       + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>")
            for r in rows:
                # pad or trim so a malformed row cannot break the table
                r = (r + [""] * len(head))[:len(head)]
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r)
                           + "</tr>")
            out.append("</tbody></table>")
            continue

        # horizontal rule
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # blockquote
        if line.lstrip().startswith(">"):
            body = []
            while i < n and lines[i].lstrip().startswith(">"):
                body.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(body))}</blockquote>")
            continue

        # list, nested by indent
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            items: List[tuple] = []
            ordered_at: dict = {}
            while i < n and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]):
                raw = lines[i]
                indent = len(raw) - len(raw.lstrip())
                mk = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", raw)
                depth = indent // 2
                ordered_at.setdefault(depth, bool(re.match(r"\d", mk.group(1))))
                parts = [mk.group(2).strip()]
                i += 1
                # A soft-wrapped continuation line belongs to this item, not a
                # new paragraph, so inline spans (e.g. **bold**) that wrap
                # across the line break still get their closing marker joined
                # before _inline() looks for a pair.
                while i < n and lines[i].strip() and not re.match(
                        r"^\s*([-*+]|\d+[.)])\s+", lines[i]) and not re.match(
                        r"^\s*(#{1,6}\s|```|\||>|(-{3,}|\*{3,}|_{3,})\s*$)",
                        lines[i]):
                    parts.append(lines[i].strip())
                    i += 1
                items.append((depth, " ".join(parts)))
            depth_now = -1
            stack: List[int] = []
            for depth, text in items:
                while stack and depth < stack[-1]:
                    out.append("</ol>" if ordered_at.get(stack[-1]) else "</ul>")
                    stack.pop()
                if not stack or depth > stack[-1]:
                    out.append("<ol>" if ordered_at.get(depth) else "<ul>")
                    stack.append(depth)
                out.append(f"<li>{_inline(text)}</li>")
            while stack:
                out.append("</ol>" if ordered_at.get(stack[-1]) else "</ul>")
                stack.pop()
            continue

        # blank
        if not line.strip():
            i += 1
            continue

        # paragraph, joined until a blank or a block starts
        body = []
        while i < n and lines[i].strip():
            if re.match(r"^\s*(#{1,6}\s|```|\||>|([-*+]|\d+[.)])\s|(-{3,}|\*{3,}|_{3,})\s*$)",
                        lines[i]):
                break
            body.append(lines[i].strip())
            i += 1
        if body:
            out.append(f"<p>{_inline(' '.join(body))}</p>")

    return "\n".join(out)
