"""yamlsubset — the part of YAML an overlay needs, read with the standard library alone.

WHY NOT PYYAML
    An overlay (`overlays.py`) changes the words of every transcription, so the way it
    parses must not depend on which optional packages a machine happens to have: PyYAML
    present on one machine and absent on another would give one recording two glossaries
    and two cache keys. stt has no required runtime dependency (AGENTS.md), so the reader is
    this module, and it reads a documented subset — the same answer everywhere, and an error
    that names the line for anything outside it. The decision is recorded in
    ``docs/specs/dictionaries.md`` section 7.

THE SUBSET
    Read: ``#`` comments; block mappings indented with spaces; plain, single-quoted and
    double-quoted scalars on one line; decimal integers; flow lists ``[a, "b"]`` on one
    line; block lists ``- a`` of scalars, indented under their key or level with it; one
    leading ``---``. Every key is text, even ``42:``.

    Refused, with the line number: anchors, aliases, tags, directives, a second document,
    block scalars (``|``, ``>``), flow mappings, a mapping inside a list, multi-line scalars,
    tabs anywhere, and a key repeated in one mapping (a standard reader keeps the last one
    without a word, so an edit made to the first would silently do nothing).

    Plain ``yes``/``no``/``on``/``off`` stay text, as they are in YAML 1.2. Plain ``null``,
    ``~``, ``true`` and ``false`` are refused rather than guessed at: a full YAML reader
    turns them into something that is not text, and a note that silently became "null" or
    vanished is worse than one line saying "quote it".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._errors import UsageError

# A document nested deeper than this is not an overlay, and the reader recurses per level.
MAX_DEPTH = 8

Scalar = str | int

_INTEGER = re.compile(r"[-+]?[0-9]+")
_RESERVED = frozenset(
    {"null", "Null", "NULL", "~", "true", "True", "TRUE", "false", "False", "FALSE"}
)
# Characters that start something outside the subset when they begin a plain value.
_INDICATORS = frozenset("&*!|>{}%@`")
_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
WHY = (
    "an overlay is read as a fixed subset of YAML, the same way on every machine "
    "(docs/specs/dictionaries.md section 7)"
)


class Block(dict[str, "Scalar | list[Scalar] | Block | None"]):
    """A mapping, remembering the line each key was written on, so a schema error can too."""

    def __init__(self, line: int) -> None:
        super().__init__()
        self.line = line
        self.lines: dict[str, int] = {}


@dataclass(frozen=True, slots=True)
class _Line:
    number: int
    indent: int
    text: str  # without the indentation; comments are removed while the line is lexed


def parse(text: str, origin: str) -> Scalar | list[Scalar] | Block | None:
    """The document in ``text``; ``origin`` names it in every error. Empty is ``None``."""
    return _Reader(_lines(text, origin), origin).document()


def _lines(text: str, origin: str) -> list[_Line]:
    lines: list[_Line] = []
    seen_content = False
    for number, raw in enumerate(text.removeprefix("\ufeff").splitlines(), start=1):
        if "\t" in raw:
            raise _error(origin, number, "has a tab", "indent with spaces")
        stripped = raw.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(stripped)
        body = stripped.rstrip(" ")
        if body.startswith("%"):
            raise _error(origin, number, "has a directive", "delete the line")
        if indent == 0 and body.startswith(("---", "...")) and body[3:4] in ("", " "):
            if body.startswith("---") and body[3:].strip(" ")[:1] in ("", "#") and not seen_content:
                seen_content = True  # one leading document marker, as editors write it
                continue
            raise _error(origin, number, "starts another document", "one document per file")
        seen_content = True
        lines.append(_Line(number, indent, body))
    return lines


class _Reader:
    def __init__(self, lines: list[_Line], origin: str) -> None:
        self.lines = lines
        self.origin = origin
        self.at = 0

    def document(self) -> Scalar | list[Scalar] | Block | None:
        if not self.lines:
            return None
        first = self.lines[0]
        value: Scalar | list[Scalar] | Block
        if not _is_item(first.text) and _split_key(first, self.origin) is None:
            value = self._inline(first.text, first.number)
            self.at = 1
        else:
            value = self._block(first.indent, depth=1)
        if self.at < len(self.lines):
            extra = self.lines[self.at]
            raise self._fail(extra.number, "does not line up with anything above it", "re-indent")
        return value

    def _block(self, indent: int, depth: int) -> list[Scalar] | Block:
        if depth > MAX_DEPTH:
            line = self.lines[self.at]
            raise self._fail(line.number, f"is nested deeper than {MAX_DEPTH} levels", "flatten")
        if _is_item(self.lines[self.at].text):
            return self._sequence(indent)
        return self._mapping(indent, depth)

    def _mapping(self, indent: int, depth: int) -> Block:
        block = Block(self.lines[self.at].number)
        while self.at < len(self.lines) and self.lines[self.at].indent == indent:
            line = self.lines[self.at]
            if _is_item(line.text):
                raise self._fail(
                    line.number, "is a list item where a key was expected", "re-indent"
                )
            split = _split_key(line, self.origin)
            if split is None:
                raise self._fail(line.number, "is not `key: value`", "write `key: value`")
            key, rest = split
            if key in block:
                first = block.lines[key]
                raise self._fail(
                    line.number, f"repeats the key {key!r} from line {first}", "keep one of them"
                )
            self.at += 1
            block[key] = self._value_of(line, rest, depth)
            block.lines[key] = line.number
        self._no_deeper_line(indent)
        return block

    def _value_of(self, line: _Line, rest: str, depth: int) -> Scalar | list[Scalar] | Block | None:
        """What follows ``key:``: on the same line, in an indented block, or a list at its level."""
        if rest:
            value = self._inline(rest, line.number)
            self._no_deeper_line(line.indent)
            return value
        if self.at >= len(self.lines):
            return None
        following = self.lines[self.at]
        if following.indent > line.indent:
            return self._block(following.indent, depth + 1)
        if following.indent == line.indent and _is_item(following.text):
            return self._sequence(line.indent)
        return None

    def _sequence(self, indent: int) -> list[Scalar]:
        items: list[Scalar] = []
        while self.at < len(self.lines):
            line = self.lines[self.at]
            if line.indent != indent or not _is_item(line.text):
                break
            rest = line.text[1:].lstrip(" ")
            if not rest or rest.startswith("#"):
                raise self._fail(line.number, "is an empty list item", "write the value after `- `")
            if _is_item(rest):
                raise self._fail(line.number, "nests a list in a list", "one value per item")
            value = self._inline(rest, line.number)
            if isinstance(value, list):
                raise self._fail(line.number, "nests a list in a list", "one value per item")
            items.append(value)
            self.at += 1
            self._no_deeper_line(indent)
        return items

    def _no_deeper_line(self, indent: int) -> None:
        if self.at < len(self.lines) and self.lines[self.at].indent > indent:
            line = self.lines[self.at]
            raise self._fail(
                line.number,
                "is indented deeper than the lines above it allow",
                "line up keys that belong together; a value fits on one line",
            )

    def _inline(self, text: str, number: int) -> Scalar | list[Scalar]:
        """A scalar or a flow list, and nothing but a comment after it."""
        value: Scalar | list[Scalar]
        if text.startswith("["):
            value, rest = self._flow_list(text, number)
        else:
            value, rest = self._scalar(text, number, flow=False)
        self._only_comment(rest, number)
        return value

    def _flow_list(self, text: str, number: int) -> tuple[list[Scalar], str]:
        items: list[Scalar] = []
        rest = text[1:].lstrip(" ")
        while True:
            if rest.startswith("]"):
                return items, rest[1:]
            if not rest or rest.startswith("#"):
                raise self._fail(number, "has a list that does not close", "close it with `]`")
            if rest[0] in ",[":
                what = "an empty list item" if rest[0] == "," else "a list in a list"
                raise self._fail(number, f"has {what}", 'write ["a", "b"]')
            item, rest = self._scalar(rest, number, flow=True)
            items.append(item)
            rest = rest.lstrip(" ")
            if rest.startswith(","):
                rest = rest[1:].lstrip(" ")
            elif not rest.startswith("]"):
                raise self._fail(number, "has a list that does not close", "close it with `]`")

    def _scalar(self, text: str, number: int, *, flow: bool) -> tuple[Scalar, str]:
        """One scalar at the start of ``text``, and what is left of the line after it."""
        if text[0] == '"':
            return _double_quoted(text, number, self.origin)
        if text[0] == "'":
            return _single_quoted(text, number, self.origin)
        found = re.search(r" #|,|\]" if flow else r" #", text)
        end = found.start() if found else len(text)
        plain = text[:end].rstrip(" ")
        self._plain_is_inside_the_subset(plain, number)
        if flow and any(bracket in plain for bracket in "[]{}"):
            raise self._fail(number, "has a bracket inside a list item", "quote the item")
        if _INTEGER.fullmatch(plain):
            try:
                return int(plain), text[end:]
            except ValueError:  # past Python's digit limit for int(): 4300 by default
                raise self._fail(
                    number, "has a number too long to read", "quote it if it is text"
                ) from None
        return plain, text[end:]

    def _plain_is_inside_the_subset(self, plain: str, number: int) -> None:
        if not plain:
            raise self._fail(number, "has an empty value", 'write the value, or "" for none')
        if plain[0] == "{":
            raise self._fail(
                number, "has a flow mapping `{...}`", "write each key on its own line, indented"
            )
        if plain == "-" or plain.startswith("- "):
            raise self._fail(
                number, "has a list item after a key", "put each `- item` on its own line"
            )
        if plain[0] in _INDICATORS or plain[:2] in ("? ", ": ") or plain in ("?", ":"):
            raise self._fail(
                number,
                f"has {plain[:1]!r}, which starts YAML this reader does not support",
                "quote the value if it is text",
            )
        if ": " in plain or plain.endswith(":"):
            raise self._fail(
                number,
                "has a colon and a space inside a plain value (or a mapping inside a list)",
                "quote the value",
            )
        if plain in _RESERVED:
            raise self._fail(
                number, f"has the bare word {plain!r}", f'quote it ("{plain}") if it is text'
            )

    def _only_comment(self, rest: str, number: int) -> None:
        """Nothing may follow a value but a comment, and YAML wants a space before its ``#``."""
        tail = rest.lstrip(" ")
        if tail and not (tail.startswith("#") and tail != rest):
            raise self._fail(number, f"has {tail!r} after its value", "one value per line")

    def _fail(self, number: int, what: str, how: str) -> UsageError:
        return _error(self.origin, number, what, how)


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _split_key(line: _Line, origin: str) -> tuple[str, str] | None:
    """``key`` and the rest of the line after ``key:``, or None when it is not a key line."""
    text = line.text
    if text[0] in "\"'":
        key, rest = (_double_quoted if text[0] == '"' else _single_quoted)(
            text, line.number, origin
        )
        rest = rest.lstrip(" ")
        if not rest.startswith(":") or rest[1:2] not in ("", " "):
            return None
        rest = rest[1:].lstrip(" ")
        return str(key), "" if rest.startswith("#") else rest
    found = re.search(r":( |$)", text)
    if found is None:
        return None
    key = text[: found.start()].rstrip(" ")
    if " #" in key or not key:
        return None
    if key[0] in _INDICATORS or key[0] in "[]?#":
        raise _error(origin, line.number, f"has the key {key!r}", "quote the key")
    rest = text[found.end() :].lstrip(" ")
    return key, "" if rest.startswith("#") else rest


def _single_quoted(text: str, number: int, origin: str) -> tuple[str, str]:
    out: list[str] = []
    position = 1
    while position < len(text):
        char = text[position]
        if char == "'":
            if text[position + 1 : position + 2] == "'":
                out.append("'")
                position += 2
                continue
            return "".join(out), text[position + 1 :]
        out.append(char)
        position += 1
    raise _error(origin, number, "has a quoted value that does not close", "close it on this line")


def _double_quoted(text: str, number: int, origin: str) -> tuple[str, str]:
    out: list[str] = []
    position = 1
    while position < len(text):
        char = text[position]
        if char == '"':
            return "".join(out), text[position + 1 :]
        if char != "\\":
            out.append(char)
            position += 1
            continue
        code = text[position + 1 : position + 2]
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            position += 2
            continue
        width = _HEX_ESCAPES.get(code)
        digits = text[position + 2 : position + 2 + width] if width else ""
        if not width or len(digits) != width or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
            raise _error(
                origin, number, f"has the escape \\{code}", 'use \\" \\\\ \\n \\t or \\uXXXX'
            )
        point = int(digits, 16)
        if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
            raise _error(origin, number, f"has the escape \\{code}{digits}", "no such character")
        out.append(chr(point))
        position += 2 + width
    raise _error(origin, number, "has a quoted value that does not close", "close it on this line")


def _error(origin: str, number: int, what: str, how: str) -> UsageError:
    return UsageError(what=f"{origin} line {number} {what}", why=WHY, how=how)
