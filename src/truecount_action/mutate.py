"""Diff-scoped mutation testing for Python, with no model in the loop.

For each line a change touched, make one small deliberate break, run the
tests, and record whether they noticed. A break the tests miss has
"survived": it marks behaviour no test pins down, a line that could be wrong
while every test still passes.

A survivor is a lead, not proof. Some breaks change nothing observable (an
`<` that may as well be `<=` because both branches return the same value).
The report says so rather than calling every survivor a bug.

Usage:
  python -m truecount_action.mutate --base origin/main -- python -m pytest -x -q

It runs the code under test, so it belongs in the customer's runner. Never
run it on Truecount's own hardware against a customer's repository.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DEFAULT_MAX = 40
# Each original is copied here before its file is broken, and removed once the
# file is restored. A run killed mid-break (a CI timeout, a SIGKILL) cannot
# run its own clean-up, so the next run restores whatever it finds here first.
BACKUP_DIR = ".truecount-mutate-backup"
LOGGERS = {"log", "logger", "logging", "LOG", "LOGGER"}

NEGATE = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE, ast.GtE: ast.Lt,
    ast.Gt: ast.LtE, ast.LtE: ast.Gt,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
# Off-by-one at a boundary: the break a test at exactly the edge would catch.
BOUNDARY = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
ARITHMETIC = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}
SYMBOL = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
    ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in",
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.And: "and", ast.Or: "or",
}


# ── Which lines changed ───────────────────────────────────────────────────


def parse_diff(text: str) -> dict[str, set[int]]:
    """New-side line numbers per file, from `git diff --unified=0`."""
    files: dict[str, set[int]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = None if path == "/dev/null" else path.removeprefix("b/")
            if current is not None:
                files.setdefault(current, set())
        elif current is not None and (match := HUNK.match(line)):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            files[current].update(range(start, start + count))
    return {path: lines for path, lines in files.items() if lines}


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        any(part in ("tests", "test") for part in p.parts[:-1])
        or p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or p.name == "conftest.py"
    )


def changed_python_lines(base: str, head: str, cwd: Path) -> dict[str, set[int]]:
    diff = subprocess.run(
        ["git", "diff", "--unified=0", "--no-color", "--no-ext-diff", f"{base}...{head}", "--", "*.py"],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout
    return {
        path: lines
        for path, lines in parse_diff(diff).items()
        if not is_test_path(path) and (cwd / path).is_file()
    }


# ── The breaks ────────────────────────────────────────────────────────────


@dataclass
class Mutant:
    path: str
    line: int
    operator: str
    original: str
    mutated: str
    node_index: int = field(default=-1, repr=False)  # position in ast.walk order
    module: str = field(default="", repr=False)  # the unmutated module source
    status: str = "pending"  # killed / survived / timeout / unbuildable
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "path": self.path, "line": self.line, "operator": self.operator,
            "original": self.original, "mutated": self.mutated,
            "status": self.status, "seconds": round(self.seconds, 2),
        }


class _Replace(ast.NodeTransformer):
    def __init__(self, target: ast.AST, replacement: ast.AST):
        self.target, self.replacement = target, replacement

    def visit(self, node):
        if node is self.target:
            return self.replacement
        return super().visit(node)


def _skipped_nodes(tree: ast.AST) -> set[int]:
    """Nodes never mutated: annotations, f-string parts, `if __name__ == ...`."""
    skip: set[int] = set()

    def all_ids(node):
        return {id(n) for n in ast.walk(node)}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns:
                skip |= all_ids(node.returns)
            for arg in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs, node.args.vararg, node.args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    skip |= all_ids(arg.annotation)
        elif isinstance(node, ast.AnnAssign):
            skip |= all_ids(node.annotation)
        elif isinstance(node, ast.JoinedStr):
            skip |= all_ids(node)
        elif (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ):
            skip |= all_ids(node)
    return skip


def _is_log_call(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    func = node.value.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    root = func
    while isinstance(root, ast.Attribute):
        root = root.value
    return isinstance(root, ast.Name) and root.id in LOGGERS


def _breaks(node: ast.AST) -> list[tuple[str, ast.AST]]:
    """(operator, replacement node) pairs for one node."""
    out: list[tuple[str, ast.AST]] = []
    if isinstance(node, ast.Compare):
        for i, op in enumerate(node.ops):
            for table, kind in ((NEGATE, "negate"), (BOUNDARY, "boundary")):
                if type(op) in table:
                    new = copy.copy(node)
                    new.ops = [*node.ops[:i], table[type(op)](), *node.ops[i + 1:]]
                    out.append((f"{kind} {SYMBOL[type(op)]} → {SYMBOL[table[type(op)]]}", new))
    elif isinstance(node, ast.BoolOp):
        flipped = ast.Or if isinstance(node.op, ast.And) else ast.And
        out.append((f"{SYMBOL[type(node.op)]} → {SYMBOL[flipped]}", ast.BoolOp(op=flipped(), values=node.values)))
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        out.append(("remove not", node.operand))
    elif isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in ARITHMETIC:
        new = copy.copy(node)
        new.op = ARITHMETIC[type(node.op)]()
        out.append((f"{SYMBOL[type(node.op)]} → {SYMBOL[type(new.op)]}", new))
    elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
        out.append((f"{node.value} → {not node.value}", ast.Constant(not node.value)))
    elif isinstance(node, ast.Constant) and type(node.value) is int:
        out.append((f"{node.value} → {node.value + 1}", ast.Constant(node.value + 1)))
    elif isinstance(node, (ast.If, ast.While)):
        new = copy.copy(node)
        new.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        out.append(("negate condition", new))
    elif isinstance(node, ast.Return) and node.value is not None and not (
        isinstance(node.value, ast.Constant) and node.value.value is None
    ):
        out.append(("return None", ast.Return(value=ast.Constant(None))))
    elif isinstance(node, ast.Raise):
        out.append(("remove raise", ast.Pass()))
    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and not _is_log_call(node):
        out.append(("remove call", ast.Pass()))
    return out


def _describe(node: ast.AST) -> str:
    if isinstance(node, (ast.If, ast.While)):
        return f"{'if' if isinstance(node, ast.If) else 'while'} {ast.unparse(node.test)}:"
    text = ast.unparse(node)
    return text.splitlines()[0] if text else type(node).__name__


def generate(path: str, source: str, lines: set[int]) -> list[Mutant]:
    """Every break this module offers on the given lines, described but not
    yet built: building means copying the whole tree, so only the selected
    few are built (see `build`)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    skip = _skipped_nodes(tree)
    mutants = []
    for index, node in enumerate(ast.walk(tree)):
        if id(node) in skip or getattr(node, "lineno", None) not in lines:
            continue
        for operator, replacement in _breaks(node):
            mutants.append(
                Mutant(path, node.lineno, operator, _describe(node), _describe(replacement), index, source)
            )
    return mutants


def build(mutant: Mutant) -> str | None:
    """The whole module with this one break applied, or None if it will not compile."""
    tree = ast.parse(mutant.module)
    target = list(ast.walk(tree))[mutant.node_index]
    replacement = dict(_breaks(target))[mutant.operator]
    mutated = ast.fix_missing_locations(_Replace(target, replacement).visit(tree))
    try:
        source = ast.unparse(mutated)
        compile(source, mutant.path, "exec")
    except (SyntaxError, ValueError, RecursionError):
        return None
    return source


def select(mutants: list[Mutant], limit: int) -> list[Mutant]:
    """Spread the budget: one break per changed line before a second on any."""
    by_line: dict[tuple[str, int], list[Mutant]] = {}
    for m in sorted(mutants, key=lambda m: (m.path, m.line)):
        by_line.setdefault((m.path, m.line), []).append(m)
    queues = list(by_line.values())
    chosen: list[Mutant] = []
    depth = 0
    while len(chosen) < limit and any(depth < len(q) for q in queues):
        for queue in queues:
            if depth < len(queue) and len(chosen) < limit:
                chosen.append(queue[depth])
        depth += 1
    return chosen


# ── Running the tests ─────────────────────────────────────────────────────


def run_tests(command: list[str], cwd: Path, timeout: float | None, cache_dir: str) -> tuple[str, float]:
    """'passed', 'failed' or 'timeout', and how long it took.

    Bytecode caching is redirected to a fresh directory per run. A mutant the
    same size as the original (`==` to `!=`), written within the same second,
    would otherwise load the ORIGINAL's cached bytecode and survive falsely.
    """
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPYCACHEPREFIX=cache_dir)
    start = time.monotonic()
    proc = subprocess.Popen(
        command, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole group: test runners fork workers
        proc.wait()
        return "timeout", time.monotonic() - start
    return ("passed" if code == 0 else "failed"), time.monotonic() - start


@dataclass
class Report:
    base: str
    head: str
    command: list[str]
    changed_lines: int
    candidates: int
    mutants: list[Mutant]
    baseline_seconds: float | None = None
    error: str | None = None

    def count(self, status: str) -> int:
        return sum(1 for m in self.mutants if m.status == status)

    def to_dict(self) -> dict:
        return {
            "base": self.base, "head": self.head, "command": self.command,
            "changed_lines": self.changed_lines, "candidates": self.candidates,
            "baseline_seconds": None if self.baseline_seconds is None else round(self.baseline_seconds, 2),
            "error": self.error,
            "summary": {s: self.count(s) for s in ("killed", "survived", "timeout")},
            "mutants": [m.to_dict() for m in self.mutants],
        }

    def to_markdown(self) -> str:
        if self.error:
            return f"### Truecount mutation check\n\nNot run: {self.error}\n"
        ran = len(self.mutants) - self.count("unbuildable")
        if ran == 0:
            return "### Truecount mutation check\n\nNothing to check: no changed Python lines outside tests offer a break.\n"
        caught = self.count("killed") + self.count("timeout")
        lines = [
            "### Truecount mutation check",
            "",
            f"The tests caught **{caught} of {ran}** deliberate breaks in the changed lines."
            f" Possible breaks: {self.candidates}; run: {ran}.",
        ]
        survivors = sorted((m for m in self.mutants if m.status == "survived"), key=lambda m: (m.path, m.line))
        if survivors:
            lines += ["", "**Not caught**", ""]
            lines += [
                f"- `{m.path}:{m.line}` {_code(m.original)} → {_code(m.mutated)} ({m.operator})"
                for m in survivors
            ]
            lines += [
                "",
                "Each of these is a change the tests would let through. Some change nothing observable;"
                " the rest name behaviour no test pins down.",
            ]
        return "\n".join(lines) + "\n"


def _code(text: str) -> str:
    text = text.replace("\n", " ")
    fence = "``" if "`" in text else "`"
    pad = " " if fence == "``" else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def recover(cwd: Path) -> list[str]:
    """Put back every file an interrupted run left broken. Returns their paths."""
    root = cwd / BACKUP_DIR
    if not root.is_dir():
        return []
    restored = []
    for backup in sorted(root.rglob("*")):
        if backup.is_file():
            relative = backup.relative_to(root)
            (cwd / relative).write_bytes(backup.read_bytes())
            restored.append(relative.as_posix())
    shutil.rmtree(root)
    return restored


def run(base: str, head: str, command: list[str], cwd: Path, limit: int = DEFAULT_MAX, progress=None) -> Report:
    restored = recover(cwd)
    if restored:
        print(f"restored files an interrupted run left broken: {', '.join(restored)}", file=sys.stderr)
    changed = changed_python_lines(base, head, cwd)
    candidates: list[Mutant] = []
    for path, lines in sorted(changed.items()):
        candidates += generate(path, (cwd / path).read_text(), lines)
    report = Report(base, head, command, sum(map(len, changed.values())), len(candidates), select(candidates, limit))
    if not report.mutants:
        return report
    with tempfile.TemporaryDirectory() as tmp:
        status, report.baseline_seconds = run_tests(command, cwd, None, f"{tmp}/baseline")
        if status != "passed":
            report.error = "the tests fail before anything is broken, so a break cannot be told from what is already failing"
            report.mutants = []
            return report
        timeout = max(10.0, report.baseline_seconds * 3 + 5)
        for i, mutant in enumerate(report.mutants):
            source = build(mutant)
            if source is None:
                mutant.status = "unbuildable"
                continue
            target = cwd / mutant.path
            original = target.read_bytes()
            backup = cwd / BACKUP_DIR / mutant.path
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(original)
            try:
                target.write_text(source)
                status, mutant.seconds = run_tests(command, cwd, timeout, f"{tmp}/{i}")
            finally:
                target.write_bytes(original)
                backup.unlink()
            mutant.status = {"passed": "survived", "failed": "killed", "timeout": "timeout"}[status]
            if progress:
                progress(i + 1, len(report.mutants), mutant)
    shutil.rmtree(cwd / BACKUP_DIR, ignore_errors=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m truecount_action.mutate", description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="commit or ref the change is measured against")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX, help="most breaks to run")
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--markdown", type=Path, help="write the summary here")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the test command")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the test command after --, e.g. -- python -m pytest -x -q")

    def progress(done, total, mutant):
        print(f"[{done}/{total}] {mutant.status:8} {mutant.path}:{mutant.line} {mutant.operator}", file=sys.stderr)

    report = run(args.base, args.head, command, Path.cwd(), args.max, progress)
    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), indent=2))
    markdown = report.to_markdown()
    if args.markdown:
        args.markdown.write_text(markdown)
    print(markdown)
    return 2 if report.error else 0


if __name__ == "__main__":
    sys.exit(main())
