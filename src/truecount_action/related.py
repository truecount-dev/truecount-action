"""Which tests to run for one break: the test files named like the changed
file, so a break in billing.py runs test_billing.py and not the whole suite.

The same naming conventions as Truecount's test-gap review (truecount/testgaps.py
in the server repository), copied rather than shared. A break whose file has no
test named like it runs the whole suite, so no break goes unchecked. A survivor
of a related-only run may still be caught by a test elsewhere, so every break
records which way it ran, and the report says so.

The customer's own command decides how tests are named on its command line:
`{tests}` becomes the matching test files and `{packages}` the changed file's
directory, for runners such as `go test` that take packages.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|specs|__tests__)/|(^|/)test_[^/]+$|_test\.[^/.]+$|\.(test|spec)\.[^/.]+$|_spec\.rb$|Tests?\.(java|kt|cs)$"
)
CODE = {"py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "go", "rb", "java", "kt", "rs", "cs", "php", "swift", "c", "cc", "cpp", "h", "scala"}
MAX_RELATED = 10
PLACEHOLDERS = ("{tests}", "{packages}")


def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].split(".", 1)[0].lower()


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("/"), b.split("/")):
        if x != y:
            break
        n += 1
    return n


def tests_for(path: str, tree: list[str]) -> list[str]:
    """Test files named like `path`, nearest first: those that follow a naming
    convention (test_x, x_test, x.test, x.spec, x_spec, XTest), then those that
    only share the name as a word (test_x_cli). At most MAX_RELATED."""
    stem = _stem(path)
    if not stem or is_test(path) or path.rsplit(".", 1)[-1].lower() not in CODE:
        return []
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    exact_names = {f"test_{stem}", f"{stem}_test", f"{stem}.test", f"{stem}.spec", f"{stem}_spec", f"{stem}test", f"{stem}tests"}
    exact, loose = [], []
    for candidate in tree:
        if not is_test(candidate) or candidate.rsplit(".", 1)[-1].lower() not in CODE:
            continue
        name = candidate.rsplit("/", 1)[-1].lower().rsplit(".", 1)[0]
        rank = (-_common_prefix(folder, candidate.rsplit("/", 1)[0] if "/" in candidate else ""), len(candidate), candidate)
        if _stem(candidate) in exact_names or name in exact_names:
            exact.append(rank)
        elif stem in re.split(r"[_.\-]", name):
            loose.append(rank)
    return [c for *_, c in sorted(exact) + sorted(loose)][:MAX_RELATED]


def package_for(path: str) -> str:
    """The changed file's directory, as `go test` names a package."""
    return "./" + path.rsplit("/", 1)[0] if "/" in path else "."


def command_for(template: str, path: str, tree: list[str]) -> str | None:
    """The related command for a break in `path`, or None to run the whole suite."""
    if "{packages}" in template:
        return template.replace("{packages}", shlex.quote(package_for(path)))
    tests = tests_for(path, tree)
    if not tests:
        return None
    return template.replace("{tests}", " ".join(shlex.quote(t) for t in tests))


def tree(cwd: Path) -> list[str]:
    """Every tracked file in the checkout."""
    return subprocess.run(["git", "ls-files"], cwd=cwd, capture_output=True, text=True, check=True).stdout.splitlines()
