"""Breaks inside SQL written as Python strings.

To `ast`, a query is text: `ORDER BY created_at DESC` and `day >= ?` offer the
Python engine nothing to break, so a change that lives in its SQL went
unchecked. On Truecount's own test-gap yardstick, five of the fifteen claims
judged by hand were about such strings.

Three breaks, each of which leaves the SQL valid, so a failing test has noticed
the behaviour and not a syntax error:

- `ASC` for `DESC`, and `DESC` for `ASC`
- an off-by-one boundary: `<` for `<=`, `>` for `>=`, and back
- one `AND` condition dropped, only in the plain shape
  `AND <column> <comparison> <value>` (or `IS [NOT] NULL`) and only where a
  clause boundary follows it, so what is left still parses

A string reads as SQL when it, or the literal it is implicitly concatenated
into, has an uppercase SQL keyword. Docstrings, bytes and f-strings are left
alone. Each break is a byte-range edit of the file, like the tree-sitter
engine's, and the edited file must still compile.
"""

from __future__ import annotations

import io
import re
import tokenize

KEYWORDS = re.compile(r"\b(?:SELECT|INSERT INTO|UPDATE|DELETE FROM|WHERE|JOIN|ORDER BY|GROUP BY|HAVING)\b")
ORDER = re.compile(r"\b(ASC|DESC)\b")
FLIPPED = {"ASC": "DESC", "DESC": "ASC"}
# Not part of <>, ->, ->>, <<, >>, =>, <=> or !=.
COMPARISON = re.compile(r"(?<![<>!=\-])(<=|>=|<|>)(?![<>=])")
BOUNDARY = {"<": "<=", "<=": "<", ">": ">=", ">=": ">"}
VALUE = r"(?:\?|%s|%\(\w+\)s|:\w+|'[^'\n]*'|-?\d+(?:\.\d+)?|[A-Za-z_][\w.]*)"
CONDITION = re.compile(
    r"[ \t]+AND[ \t]+[A-Za-z_][\w.]*[ \t]*"
    rf"(?:(?:<=|>=|<>|!=|=|<|>)[ \t]*{VALUE}|IS[ \t]+(?:NOT[ \t]+)?NULL)"
    r"(?=\s*(?:$|[),{]|\b(?:AND|OR|ORDER|GROUP|LIMIT|HAVING|THEN|UNION|RETURNING|WINDOW)\b))"
)
# A string between one of these and the end of the line is a statement on its own.
STATEMENT_START = {tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}


def edits(text: str) -> list[tuple[str, int, int, str]]:
    """(operator, start, end, replacement) for one string's contents."""
    out = []
    for m in ORDER.finditer(text):
        out.append((f"sql {m.group(1)} → {FLIPPED[m.group(1)]}", m.start(), m.end(), FLIPPED[m.group(1)]))
    for m in COMPARISON.finditer(text):
        out.append((f"sql boundary {m.group(1)} → {BOUNDARY[m.group(1)]}", m.start(), m.end(), BOUNDARY[m.group(1)]))
    for m in CONDITION.finditer(text):
        out.append(("sql drop AND condition", m.start(), m.end(), ""))
    return sorted(out, key=lambda e: e[1])


def _groups(source: str) -> list[list[tokenize.TokenInfo]]:
    """Plain string literals, implicitly concatenated ones grouped together,
    leaving out any that stands alone as a statement (a docstring)."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return []
    groups, current, opened_at = [], [], 0
    for i, tok in enumerate(tokens):
        if tok.type == tokenize.STRING:
            if not current:
                opened_at = i
            current.append(tok)
        elif tok.type in (tokenize.NL, tokenize.COMMENT) and current:
            continue
        elif current:
            before = tokens[opened_at - 1].type if opened_at else tokenize.NEWLINE
            alone = before in STATEMENT_START and tok.type in (tokenize.NEWLINE, tokenize.ENDMARKER)
            if not alone:
                groups.append(current)
            current = []
    return groups


def _split(token: str) -> tuple[str, str] | None:
    """(what precedes the contents, the contents), or None for bytes and f-strings."""
    prefix = re.match(r"[A-Za-z]*", token).group(0)
    if set(prefix.lower()) & {"b", "f"}:
        return None
    rest = token[len(prefix):]
    quote = rest[:3] if rest[:3] in ('"""', "'''") else rest[:1]
    return prefix + quote, rest[len(quote):-len(quote)]


def generate(path: str, source: str, lines: set[int], mutant_class) -> list:
    """Every SQL break on the given lines, as Mutant objects carrying their
    byte-range edit."""
    line_texts = io.StringIO(source).readlines()  # split as tokenize splits, on \n only
    starts = [0]
    for text in line_texts:
        starts.append(starts[-1] + len(text.encode()))
    mutants = []
    for group in _groups(source):
        parts = [(tok, _split(tok.string)) for tok in group]
        if not KEYWORDS.search("".join(split[1] for _, split in parts if split)):
            continue
        for tok, split in parts:
            if split is None:
                continue
            opening, contents = split
            row, col = tok.start
            first = line_texts[row - 1]
            contents_byte = starts[row - 1] + len(first[:col].encode()) + len(opening.encode())
            for operator, start, end, replacement in edits(contents):
                if contents[start:end].lstrip(" \t").startswith("AND"):
                    at = start + len(contents[start:end]) - len(contents[start:end].lstrip(" \t"))
                else:
                    at = start
                line = row + contents.count("\n", 0, at)
                if line not in lines:
                    continue
                # The line as written, and with the break: a break never spans a line.
                newline = contents.rfind("\n", 0, start)
                column = col + len(opening) + start if newline == -1 else start - newline - 1
                text = line_texts[line - 1].rstrip("\r\n")
                mutated = text[:column] + replacement + text[column + end - start:]
                mutants.append(mutant_class(
                    path, line, operator, text.strip(), mutated.strip() or "(nothing left on the line)", module=source,
                    start_byte=contents_byte + len(contents[:start].encode()),
                    end_byte=contents_byte + len(contents[:end].encode()), replacement=replacement,
                ))
    return mutants


def build(mutant) -> str | None:
    """The file with this one edit made, or None if it no longer compiles."""
    data = mutant.module.encode()
    edited = (data[:mutant.start_byte] + mutant.replacement.encode() + data[mutant.end_byte:]).decode()
    try:
        compile(edited, mutant.path, "exec")
    except (SyntaxError, ValueError):
        return None
    return edited
