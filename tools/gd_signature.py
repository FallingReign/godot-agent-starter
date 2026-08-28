#!/usr/bin/env python3
"""Parse typed GDScript function declarations without Godot or third parties.

The proposal, architecture view and conformance gate need one definition of a
function signature.  A regular expression is not enough: default expressions
may contain nested calls, collections, strings, comments and closing
parentheses of their own.  This module uses a deliberately small lexer and
balanced-delimiter scanner so those consumers can share a fail-closed result.

Only named functions are returned from source files.  Anonymous ``func(...)``
lambdas remain part of their containing function's body digest.  Top-level
function names are their portable identity. Methods in named inner classes use
their lexical class path (for example ``Menu.Button.press``), because the same
method name is legal in two different inner classes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple


class SignatureError(ValueError):
    """A function declaration cannot be represented by the kit contract."""


@dataclass(frozen=True)
class ParsedSignature:
    """Canonical, proposal-safe identity for one function signature."""

    name: str
    canonical: str
    is_static: bool


@dataclass(frozen=True)
class SourceFunction:
    """One named source declaration and its normalized implementation proof.

    Offsets refer to the LF-normalized source passed to
    :func:`parse_source_functions`. ``body_end`` is the first token outside the
    indentation block, or the source length for the final function.
    """

    name: str
    identity: str
    scope: Tuple[str, ...]
    signature: str
    body_sha256: str
    annotations: Tuple[str, ...]
    is_abstract: bool
    line: int
    start: int
    body_start: int
    body_end: int


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    start: int
    end: int
    line: int
    column: int


@dataclass(frozen=True)
class _ClassScope:
    name: str
    declaration_index: int
    body_index: int
    end_index: int
    indent: int


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(
    r"(?:0[xX][0-9A-Fa-f_]+|0[bB][01_]+|"
    r"(?:[0-9][0-9_]*)(?:\.[0-9][0-9_]*)?(?:[eE][+-]?[0-9][0-9_]*)?)"
)
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {close: opening for opening, close in _OPEN.items()}
_OPERATORS = tuple(sorted({
    "**=", "<<=", ">>=", "...", "->", "==", "!=", "<=", ">=", "&&",
    "||", "**", "<<", ">>", "+=", "-=", "*=", "/=", "%=", "&=", "|=",
    "^=", ":=", "..",
}, key=len, reverse=True))
_SPACED_OPERATORS = {
    "=", "+", "-", "*", "/", "%", "**", "==", "!=", "<", ">", "<=",
    ">=", "&&", "||", "&", "|", "^", "<<", ">>", "+=", "-=", "*=",
    "/=", "%=", "&=", "|=", "^=", ":=", "..", "?",
}


def _normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _advance(segment: str, line: int, column: int) -> Tuple[int, int]:
    newlines = segment.count("\n")
    if not newlines:
        return line, column + len(segment)
    return line + newlines, len(segment.rsplit("\n", 1)[1])


def _string_end(text: str, start: int) -> int:
    quote = text[start]
    delimiter = quote * 3 if text.startswith(quote * 3, start) else quote
    triple = len(delimiter) == 3
    index = start + len(delimiter)
    while index < len(text):
        if text.startswith(delimiter, index):
            return index + len(delimiter)
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "\n" and not triple:
            raise SignatureError("unterminated string literal")
        index += 1
    raise SignatureError("unterminated string literal")


def _tokenize(text: str) -> List[_Token]:
    tokens: List[_Token] = []
    index = 0
    line = 1
    column = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            segment = text[index:end]
            line, column = _advance(segment, line, column)
            index = end
            continue
        if char == "#":
            end = text.find("\n", index)
            if end < 0:
                end = len(text)
            segment = text[index:end]
            line, column = _advance(segment, line, column)
            index = end
            continue

        start = index
        start_line = line
        start_column = column
        if char in ("\"", "'"):
            end = _string_end(text, start)
            kind = "string"
        else:
            identifier = _IDENTIFIER.match(text, start)
            number = _NUMBER.match(text, start)
            if identifier is not None:
                end = identifier.end()
                kind = "identifier"
            elif number is not None:
                end = number.end()
                kind = "number"
            else:
                operator = next(
                    (candidate for candidate in _OPERATORS
                     if text.startswith(candidate, start)),
                    "",
                )
                end = start + (len(operator) if operator else 1)
                kind = "symbol"

        value = text[start:end]
        tokens.append(_Token(kind, value, start, end, start_line, start_column))
        line, column = _advance(value, line, column)
        index = end
    return tokens


def _expanded_line_indent(text: str, line: int) -> int:
    """Return a stable indentation width, treating one tab as four spaces."""
    lines = text.split("\n")
    if line < 1 or line > len(lines):
        raise SignatureError("internal parser error: token line is outside source")
    width = 0
    for char in lines[line - 1]:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4
        else:
            break
    return width


def _balanced(tokens: Sequence[_Token], where: str) -> None:
    stack: List[str] = []
    for token in tokens:
        if token.value in _OPEN:
            stack.append(token.value)
        elif token.value in _CLOSE:
            if not stack or stack[-1] != _CLOSE[token.value]:
                raise SignatureError(f"{where}: unmatched {token.value!r}")
            stack.pop()
    if stack:
        raise SignatureError(f"{where}: unclosed {stack[-1]!r}")


def _matching_token(tokens: Sequence[_Token], opening_index: int) -> int:
    opening = tokens[opening_index].value
    if opening not in _OPEN:
        raise SignatureError("internal parser error: expected an opening delimiter")
    stack: List[str] = []
    for index in range(opening_index, len(tokens)):
        value = tokens[index].value
        if value in _OPEN:
            stack.append(value)
        elif value in _CLOSE:
            if not stack or stack[-1] != _CLOSE[value]:
                raise SignatureError(f"function signature: unmatched {value!r}")
            stack.pop()
            if not stack:
                return index
    raise SignatureError(f"function signature: unclosed {opening!r}")


def _parse_type(tokens: Sequence[_Token], start: int = 0) -> Tuple[str, int]:
    if start >= len(tokens) or tokens[start].kind != "identifier":
        raise SignatureError("type must start with an identifier")
    out = tokens[start].value
    index = start + 1
    while index < len(tokens) and tokens[index].value == ".":
        if index + 1 >= len(tokens) or tokens[index + 1].kind != "identifier":
            raise SignatureError("qualified type has no name after '.'")
        out += "." + tokens[index + 1].value
        index += 2
    if index < len(tokens) and tokens[index].value == "[":
        index += 1
        arguments: List[str] = []
        while True:
            argument, index = _parse_type(tokens, index)
            arguments.append(argument)
            if index >= len(tokens):
                raise SignatureError("generic type has no closing ']'")
            if tokens[index].value == "]":
                index += 1
                break
            if tokens[index].value != ",":
                raise SignatureError("generic type arguments must be comma-separated")
            index += 1
        out += "[" + ", ".join(arguments) + "]"
    return out, index


def _canonical_type(tokens: Sequence[_Token], where: str) -> str:
    if not tokens:
        raise SignatureError(f"{where}: type is empty")
    canonical, end = _parse_type(tokens)
    if end != len(tokens):
        raise SignatureError(
            f"{where}: unexpected token {tokens[end].value!r} in type"
        )
    return canonical


def _render_expression(tokens: Sequence[_Token]) -> str:
    _balanced(tokens, "default expression")
    out = ""
    previous = ""
    for token in tokens:
        value = token.value
        if value in (")", "]", "}"):
            out = out.rstrip() + value
        elif value == ",":
            out = out.rstrip() + ", "
        elif value == ":":
            out = out.rstrip() + ": "
        elif value == ".":
            out = out.rstrip() + "."
        elif value in ("(", "["):
            separator = (
                " "
                if previous in ({":", ","} | _SPACED_OPERATORS)
                else ""
            )
            out = out.rstrip() + separator + value
        elif value == "{":
            if out and not out.endswith((" ", "(", "[", "{")):
                out += " "
            out += value
        elif value == ";":
            out = out.rstrip() + "; "
        elif value in _SPACED_OPERATORS:
            out = out.rstrip() + f" {value} "
        else:
            needs_space = bool(out) and not out.endswith((" ", "(", "[", "{", "."))
            if needs_space and previous not in _SPACED_OPERATORS:
                out += " "
            out += value
        previous = value
    return out.strip()


def _split_parameters(tokens: Sequence[_Token]) -> List[Sequence[_Token]]:
    if not tokens:
        return []
    parts: List[Sequence[_Token]] = []
    stack: List[str] = []
    start = 0
    for index, token in enumerate(tokens):
        value = token.value
        if value in _OPEN:
            stack.append(value)
        elif value in _CLOSE:
            if not stack or stack[-1] != _CLOSE[value]:
                raise SignatureError(f"parameter list: unmatched {value!r}")
            stack.pop()
        elif value == "," and not stack:
            if index == start:
                raise SignatureError("parameter list contains an empty parameter")
            parts.append(tokens[start:index])
            start = index + 1
    if stack:
        raise SignatureError(f"parameter list: unclosed {stack[-1]!r}")
    if start < len(tokens):
        parts.append(tokens[start:])
    elif not parts:
        raise SignatureError("parameter list contains an empty parameter")
    return parts


def _canonical_parameter(tokens: Sequence[_Token]) -> Tuple[str, str]:
    stack: List[str] = []
    colon = -1
    equals = -1
    for index, token in enumerate(tokens):
        value = token.value
        if value in _OPEN:
            stack.append(value)
        elif value in _CLOSE:
            if not stack or stack[-1] != _CLOSE[value]:
                raise SignatureError(f"parameter: unmatched {value!r}")
            stack.pop()
        elif not stack and value == ":" and equals < 0:
            if colon >= 0:
                raise SignatureError("parameter contains more than one type separator")
            colon = index
        elif not stack and value == "=":
            if equals >= 0:
                raise SignatureError("parameter contains more than one default separator")
            equals = index
    if stack:
        raise SignatureError(f"parameter: unclosed {stack[-1]!r}")
    if colon != 1 or tokens[0].kind != "identifier":
        raise SignatureError("every parameter must be '<name>: <type>'")
    if equals >= 0 and equals < colon:
        raise SignatureError("a parameter default must follow its type")

    type_end = equals if equals >= 0 else len(tokens)
    type_name = _canonical_type(tokens[colon + 1:type_end], "parameter")
    canonical = f"{tokens[0].value}: {type_name}"
    if equals >= 0:
        default_tokens = tokens[equals + 1:]
        if not default_tokens:
            raise SignatureError("parameter default is empty")
        canonical += " = " + _render_expression(default_tokens)
    return tokens[0].value, canonical


def parse_proposal_signature(text: str) -> ParsedSignature:
    """Parse one exact, typed proposal signature.

    Accepted syntax is ``[static ]func name(parameters) -> ReturnType``.
    Decorators, a source declaration's trailing colon and any function body are
    intentionally outside the proposal contract.
    """
    if not isinstance(text, str):
        raise SignatureError("function signature must be a string")
    normalized = _normalise_newlines(text)
    tokens = _tokenize(normalized)
    if not tokens:
        raise SignatureError("function signature is empty")

    index = 0
    is_static = tokens[index].value == "static"
    if is_static:
        index += 1
    if index >= len(tokens) or tokens[index].value != "func":
        raise SignatureError("signature must start with '[static ]func'")
    index += 1
    if index >= len(tokens) or tokens[index].kind != "identifier":
        raise SignatureError("named function identifier is missing")
    name = tokens[index].value
    index += 1
    if index >= len(tokens) or tokens[index].value != "(":
        raise SignatureError("function parameter list is missing")
    closing = _matching_token(tokens, index)
    parameter_tokens = tokens[index + 1:closing]
    index = closing + 1
    if index >= len(tokens) or tokens[index].value != "->":
        raise SignatureError("every function signature requires an explicit return type")
    return_tokens = tokens[index + 1:]
    return_type = _canonical_type(return_tokens, "return")

    parameter_names = set()
    parameters: List[str] = []
    for part in _split_parameters(parameter_tokens):
        parameter_name, canonical = _canonical_parameter(part)
        if parameter_name in parameter_names:
            raise SignatureError(f"duplicate parameter name {parameter_name!r}")
        parameter_names.add(parameter_name)
        parameters.append(canonical)

    prefix = "static " if is_static else ""
    canonical = f"{prefix}func {name}({', '.join(parameters)}) -> {return_type}"
    return ParsedSignature(name=name, canonical=canonical, is_static=is_static)


def _annotation_end(tokens: Sequence[_Token], start: int) -> int:
    if (
        start >= len(tokens)
        or tokens[start].value != "@"
        or start + 1 >= len(tokens)
        or tokens[start + 1].kind != "identifier"
    ):
        raise SignatureError("annotation must be '@<name>' with optional arguments")
    end = start + 2
    if end < len(tokens) and tokens[end].value == "(":
        end = _matching_token(tokens, end) + 1
    return end


def _annotation_gap_is_contiguous(text: str, left: int, right: int) -> bool:
    gap = text[left:right]
    without_comments = re.sub(r"#[^\n]*", "", gap)
    return not without_comments.strip() and without_comments.count("\n") <= 1


def _render_annotation(tokens: Sequence[_Token]) -> str:
    rendered = "@" + tokens[1].value
    if len(tokens) > 2:
        rendered += _render_expression(tokens[2:])
    return rendered


def _contiguous_annotations(
    tokens: Sequence[_Token],
    text: str,
    declaration_index: int,
) -> Tuple[Tuple[Tuple[_Token, ...], ...], int, int]:
    """Return decorators attached to a declaration and its structural indent."""
    cursor = declaration_index
    structural_indent = tokens[declaration_index].column
    found_reversed: List[Tuple[_Token, ...]] = []
    while cursor > 0:
        found: Tuple[int, int] | None = None
        for candidate in range(cursor - 1, -1, -1):
            if tokens[candidate].value != "@":
                continue
            try:
                end = _annotation_end(tokens, candidate)
            except SignatureError:
                continue
            if end != cursor:
                continue
            annotation = tokens[candidate]
            following = tokens[cursor]
            same_line = annotation.line == following.line
            if same_line:
                if annotation.column > structural_indent:
                    continue
            elif annotation.column != structural_indent:
                continue
            if not _annotation_gap_is_contiguous(
                text,
                tokens[end - 1].end,
                following.start,
            ):
                continue
            found = (candidate, end)
            break
        if found is None:
            break
        candidate, end = found
        found_reversed.append(tuple(tokens[candidate:end]))
        structural_indent = tokens[candidate].column
        cursor = candidate
    return tuple(reversed(found_reversed)), cursor, structural_indent


def _function_header_end(
    tokens: Sequence[_Token],
    func_index: int,
    *,
    is_abstract: bool,
    declaration_indent: int,
) -> Tuple[int | None, int]:
    """Return ``(colon, end)``; bodyless abstract methods have no colon."""
    name_index = func_index + 1
    if name_index >= len(tokens) or tokens[name_index].kind != "identifier":
        raise SignatureError(
            f"line {tokens[func_index].line}: named function identifier is missing"
        )
    opening_index = name_index + 1
    if opening_index >= len(tokens) or tokens[opening_index].value != "(":
        raise SignatureError(
            f"line {tokens[func_index].line}: function parameter list is missing"
        )
    closing_index = _matching_token(tokens, opening_index)
    arrow_index = closing_index + 1
    if arrow_index >= len(tokens) or tokens[arrow_index].value != "->":
        raise SignatureError(
            f"line {tokens[func_index].line}: function return type is missing"
        )

    try:
        _, return_end = _parse_type(tokens, arrow_index + 1)
    except SignatureError as exc:
        raise SignatureError(f"line {tokens[func_index].line}: {exc}") from exc

    colon_index = (
        return_end
        if return_end < len(tokens) and tokens[return_end].value == ":"
        else None
    )
    if colon_index is not None:
        if is_abstract:
            raise SignatureError(
                f"line {tokens[func_index].line}: @abstract function must be "
                "bodyless and have no trailing ':'"
            )
        return colon_index, return_end

    if not is_abstract:
        raise SignatureError(
            f"line {tokens[func_index].line}: function declaration has no trailing ':'"
        )

    last_header_token = tokens[return_end - 1]
    if return_end < len(tokens):
        following = tokens[return_end]
        if (
            following.line == last_header_token.line
            or following.column > declaration_indent
        ):
            raise SignatureError(
                f"line {tokens[func_index].line}: @abstract function has "
                f"unexpected token {following.value!r} after its return type"
            )
    return None, return_end


def _body_end(tokens: Sequence[_Token], colon_index: int, indent: int) -> int:
    stack: List[str] = []
    colon = tokens[colon_index]
    for index in range(colon_index + 1, len(tokens)):
        token = tokens[index]
        if token.line > colon.line and token.column <= indent and not stack:
            return index
        value = token.value
        if value in _OPEN:
            stack.append(value)
        elif value in _CLOSE:
            if not stack or stack[-1] != _CLOSE[value]:
                raise SignatureError(
                    f"line {colon.line}: unmatched {value!r} in function body"
                )
            stack.pop()
    if stack:
        raise SignatureError(
            f"line {colon.line}: unclosed {stack[-1]!r} in function body"
        )
    return len(tokens)


def _class_scopes(tokens: Sequence[_Token]) -> Tuple[_ClassScope, ...]:
    scopes: List[_ClassScope] = []
    for index, token in enumerate(tokens):
        if token.value != "class":
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].kind != "identifier":
            raise SignatureError(
                f"line {token.line}: named inner class identifier is missing"
            )
        stack: List[str] = []
        colon_index: int | None = None
        for candidate in range(index + 2, len(tokens)):
            current = tokens[candidate]
            if current.line > token.line and not stack:
                break
            if current.value in _OPEN:
                stack.append(current.value)
            elif current.value in _CLOSE:
                if not stack or stack[-1] != _CLOSE[current.value]:
                    raise SignatureError(
                        f"line {token.line}: unmatched {current.value!r} "
                        "in inner class declaration"
                    )
                stack.pop()
            elif current.value == ":" and not stack:
                colon_index = candidate
                break
        if colon_index is None:
            raise SignatureError(
                f"line {token.line}: inner class declaration has no trailing ':'"
            )
        scopes.append(
            _ClassScope(
                name=tokens[index + 1].value,
                declaration_index=index,
                body_index=colon_index + 1,
                end_index=_body_end(tokens, colon_index, token.column),
                indent=token.column,
            )
        )
    return tuple(scopes)


def _scope_for_function(
    scopes: Sequence[_ClassScope],
    func_index: int,
) -> Tuple[str, ...]:
    containing = [
        scope
        for scope in scopes
        if scope.body_index <= func_index < scope.end_index
    ]
    containing.sort(key=lambda scope: scope.declaration_index)
    return tuple(scope.name for scope in containing)


def _body_digest(
    tokens: Sequence[_Token],
    *,
    text: str,
    declaration_line: int,
    annotations: Sequence[Sequence[_Token]],
    is_abstract: bool,
) -> str:
    material: List[str] = ["gdscript-function-body-v2"]
    for annotation in annotations:
        material.append("annotation")
        material.extend(
            f"{token.kind}:{len(token.value)}:{token.value}"
            for token in annotation
        )
    if is_abstract:
        material.append("abstract-no-body")
    else:
        stack: List[str] = []
        previous_line = -1
        indent_cache: Dict[int, int] = {}
        base_width = _expanded_line_indent(text, declaration_line)
        for token in tokens:
            if token.line != previous_line and not stack:
                if token.line == declaration_line:
                    material.append("indent:inline")
                else:
                    if token.line not in indent_cache:
                        indent_cache[token.line] = _expanded_line_indent(
                            text, token.line
                        )
                    relative = max(0, indent_cache[token.line] - base_width)
                    material.append(f"indent:{relative}")
            material.append(f"{token.kind}:{len(token.value)}:{token.value}")
            if token.value in _OPEN:
                stack.append(token.value)
            elif token.value in _CLOSE and stack:
                stack.pop()
            previous_line = token.line
    encoded = "\n".join(material).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_source_functions(text: str) -> Dict[str, SourceFunction]:
    """Return named functions from one GDScript source file.

    The parser raises :class:`SignatureError` when a named declaration is
    malformed or when one lexical identity appears twice. That is deliberate:
    silently omitting a function would turn an incomplete architecture view
    into a false conformance pass.
    """
    if not isinstance(text, str):
        raise SignatureError("GDScript source must be a string")
    normalized = _normalise_newlines(text)
    tokens = _tokenize(normalized)
    scopes = _class_scopes(tokens)
    functions: Dict[str, SourceFunction] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.value != "func":
            index += 1
            continue
        if index + 1 < len(tokens) and tokens[index + 1].value == "(":
            # Anonymous lambdas have no proposal identity. If they are inside a
            # named function, their tokens are already covered by its body hash.
            index += 1
            continue

        declaration_index = (
            index - 1
            if index > 0 and tokens[index - 1].value == "static"
            else index
        )
        annotation_tokens, start_index, structural_indent = _contiguous_annotations(
            tokens,
            normalized,
            declaration_index,
        )
        annotation_names = tuple(
            annotation[1].value for annotation in annotation_tokens
        )
        is_abstract = "abstract" in annotation_names
        colon_index, header_end = _function_header_end(
            tokens,
            index,
            is_abstract=is_abstract,
            declaration_indent=structural_indent,
        )
        declaration_token = tokens[declaration_index]
        header_stop = (
            tokens[colon_index].start
            if colon_index is not None
            else tokens[header_end - 1].end
        )
        header = normalized[declaration_token.start:header_stop]
        try:
            parsed = parse_proposal_signature(header)
        except SignatureError as exc:
            raise SignatureError(f"line {token.line}: {exc}") from exc
        scope = _scope_for_function(scopes, index)
        identity = ".".join((*scope, parsed.name))
        if identity in functions:
            first = functions[identity]
            raise SignatureError(
                f"duplicate function identity {identity!r} at lines "
                f"{first.line} and {token.line}"
            )

        if colon_index is None:
            end_index = header_end
            body_tokens: Sequence[_Token] = ()
            body_start = tokens[header_end - 1].end
            body_end = body_start
        else:
            end_index = _body_end(tokens, colon_index, structural_indent)
            body_tokens = tokens[colon_index + 1:end_index]
            if not body_tokens:
                raise SignatureError(f"line {token.line}: function body is empty")
            body_start = tokens[colon_index].end
            body_end = (
                tokens[end_index].start
                if end_index < len(tokens)
                else len(normalized)
            )
        functions[identity] = SourceFunction(
            name=parsed.name,
            identity=identity,
            scope=scope,
            signature=parsed.canonical,
            body_sha256=_body_digest(
                body_tokens,
                text=normalized,
                declaration_line=tokens[start_index].line,
                annotations=annotation_tokens,
                is_abstract=is_abstract,
            ),
            annotations=tuple(
                _render_annotation(annotation) for annotation in annotation_tokens
            ),
            is_abstract=is_abstract,
            line=token.line,
            start=tokens[start_index].start,
            body_start=body_start,
            body_end=body_end,
        )
        index = end_index
    return functions
