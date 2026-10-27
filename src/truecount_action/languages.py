"""Breaks for languages other than Python, through tree-sitter.

Python keeps its own engine in mutate.py, on the standard library's `ast`.
For JavaScript, TypeScript, Go and Ruby this module parses the file with
tree-sitter and offers the same kinds of break on the changed lines: a
flipped comparison, an off-by-one boundary, `&&` for `||`, swapped
arithmetic, a removed `!`, a flipped boolean, an integer one higher, and a
negated `if`. Each break is a byte-range edit of the original text, so the
rest of the file stays exactly as written.

tree-sitter is the one thing the check ever installs, and only when a change
touches one of these languages: the Action puts pinned, hash-checked wheels in
a separate environment under the runner's temp folder, never in yours. Without
it, these files are listed as skipped and Python is checked as before.

A break can stop a file compiling, which a test run would report as a failure
and the check would wrongly count as caught. So every built break is parsed
again and a syntax error marks it unbuildable, and Go runs `go build ./...`
before its tests for the type errors a parser cannot see.
"""

from __future__ import annotations

import importlib

EXTENSIONS = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".rb": "ruby",
}
GRAMMARS = {
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
}
NEGATE = {"==": "!=", "!=": "==", "===": "!==", "!==": "===", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}
BOUNDARY = {"<": "<=", "<=": "<", ">": ">=", ">=": ">"}
ARITHMETIC = {"+": "-", "-": "+", "*": "/", "/": "*"}
LOGICAL = {"&&": "||", "||": "&&", "and": "or", "or": "and"}
BINARY = {"binary_expression", "binary"}
UNARY = {"unary_expression", "unary"}
INTEGER = {"number", "int_literal", "integer"}
# Never broken: types, imports, and the expressions inside template strings
# (as the Python engine leaves f-strings alone).
SKIP_INSIDE = {
    "type_annotation", "type_alias_declaration", "interface_declaration", "literal_type", "predefined_type",
    "type_arguments", "type_parameters", "template_string", "template_substitution", "string", "interpolation",
    "import_statement", "import_declaration", "type_declaration", "type_spec",
}
# Statements that only log are never broken, nor is anything inside them.
LOGGERS = ("console.", "logger.", "log.", "fmt.Print")


class ParserMissing(RuntimeError):
    pass


def language_for(path: str) -> str | None:
    dot = path.rfind(".")
    return EXTENSIONS.get(path[dot:]) if dot != -1 else None


def _parser(language: str):
    try:
        import tree_sitter
        module, attribute = GRAMMARS[language]
        grammar = getattr(importlib.import_module(module), attribute)()
    except ImportError as err:
        raise ParserMissing(f"breaking {language} needs tree-sitter, which is not installed here") from err
    return tree_sitter.Parser(tree_sitter.Language(grammar))


def _inside_skipped(node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in SKIP_INSIDE:
            return True
        parent = parent.parent
    return False


def _edits(node, source: bytes) -> list[tuple[str, int, int, bytes, str, str]]:
    """(operator, start, end, replacement, original, mutated) for one node."""
    text = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
    out = []
    if node.type in BINARY:
        op = node.child_by_field_name("operator")
        if op is not None:
            symbol = op.text.decode()
            for table, kind in ((NEGATE, "negate"), (BOUNDARY, "boundary"), (ARITHMETIC, None), (LOGICAL, None)):
                if symbol in table:
                    new = table[symbol]
                    mutated = (source[node.start_byte:op.start_byte] + new.encode() + source[op.end_byte:node.end_byte]).decode()
                    label = f"{kind} {symbol} → {new}" if kind else f"{symbol} → {new}"
                    out.append((label, op.start_byte, op.end_byte, new.encode(), text, mutated))
    elif node.type in UNARY:
        op = node.child_by_field_name("operator")
        operand = node.child_by_field_name("argument") or node.child_by_field_name("operand")
        if op is not None and op.text in (b"!", b"not") and operand is not None:
            inner = source[operand.start_byte:operand.end_byte]
            out.append(("remove not", node.start_byte, node.end_byte, inner, text, inner.decode()))
    elif node.type in ("true", "false"):
        flipped = b"false" if node.type == "true" else b"true"
        out.append((f"{node.type} → {flipped.decode()}", node.start_byte, node.end_byte, flipped, text, flipped.decode()))
    elif node.type in INTEGER and node.is_named and text.isdigit():
        bumped = str(int(text) + 1).encode()
        out.append((f"{text} → {bumped.decode()}", node.start_byte, node.end_byte, bumped, text, bumped.decode()))
    elif node.type in ("if_statement", "if", "if_modifier", "unless"):
        condition = node.child_by_field_name("condition")
        if condition is not None:
            inner = source[condition.start_byte:condition.end_byte]
            if condition.type == "parenthesized_expression":  # JavaScript keeps its parentheses
                negated = b"(!" + inner + b")"
            else:
                negated = b"!(" + inner + b")"
            line = text.splitlines()[0] if text else node.type
            out.append(("negate condition", condition.start_byte, condition.end_byte, negated, line,
                        line.replace(inner.decode(), negated.decode(), 1)))
    return out


def generate(path: str, source: str, lines: set[int], mutant_class) -> list:
    """Every break offered on the given lines, as Mutant objects carrying
    their byte-range edit. Raises ParserMissing without tree-sitter."""
    language = language_for(path)
    if language is None:
        return []
    data = source.encode()
    tree = _parser(language).parse(data)
    if tree.root_node.has_error:
        return []  # a file the parser cannot read would yield breaks at the wrong places
    mutants = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "expression_statement" and node.text.decode().startswith(LOGGERS):
            continue
        stack.extend(reversed(node.children))
        line = node.start_point[0] + 1
        if line not in lines or not node.is_named or _inside_skipped(node):
            continue
        for operator, start, end, replacement, original, mutated in _edits(node, data):
            mutants.append(mutant_class(path, line, operator, original.splitlines()[0] if original else node.type,
                                        mutated.splitlines()[0] if mutated else "", module=source,
                                        start_byte=start, end_byte=end, replacement=replacement.decode()))
    return mutants


def build(mutant) -> str | None:
    """The file with this one edit made, or None if it no longer parses."""
    data = mutant.module.encode()
    edited = data[:mutant.start_byte] + mutant.replacement.encode() + data[mutant.end_byte:]
    tree = _parser(language_for(mutant.path)).parse(edited)
    return None if tree.root_node.has_error else edited.decode()


def build_check(path: str) -> list[str] | None:
    """A command that must succeed before a break's tests count: Go only,
    where a type error is invisible to the parser."""
    return ["go", "build", "./..."] if language_for(path) == "go" else None
