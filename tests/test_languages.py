"""Breaks for JavaScript, TypeScript, Go and Ruby through tree-sitter."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from truecount_action import languages, mutate

JS = """function price(qty, member) {
  if (qty > 10 && !member) {
    return qty * 2;
  }
  console.log("small order", qty > 1);
  return qty + 1;
}
const label = `total: ${1 + 1}`;
module.exports = { price };
"""


def offered(path, source, lines):
    return [(m.line, m.operator) for m in languages.generate(path, source, lines, mutate.Mutant)]


def test_javascript_offers_the_python_engines_breaks_on_changed_lines_only():
    got = offered("src/price.js", JS, {2, 3, 6})
    assert (2, "negate > → <=") in got and (2, "boundary > → >=") in got
    assert (2, "&& → ||") in got and (2, "remove not") in got
    assert (2, "condition → false") in got and (2, "condition → true") in got
    assert (3, "* → /") in got and (3, "2 → 3") in got
    assert (6, "+ → -") in got
    assert {line for line, _ in got} == {2, 3, 6}


def test_logging_template_strings_and_types_are_left_alone():
    assert offered("src/price.js", JS, {5, 8}) == []
    ts = "const f = (a: number, b: number): boolean => a <= b || false;\ntype N = 1 | 2;\n"
    got = offered("src/f.ts", ts, {1, 2})
    assert (1, "negate <= → >") in got and (1, "false → true") in got
    assert all(line == 1 for line, _ in got)  # the type alias on line 2 is not code
    assert not any(op.startswith(("1 →", "2 →")) for _, op in got)


def test_go_and_ruby_breaks():
    go = "package p\n\nfunc f(a, b int) bool {\n\tif a >= b && !ok() {\n\t\treturn a+1 > b\n\t}\n\treturn true\n}\n"
    got = offered("p/f.go", go, {4, 5, 7})
    assert (4, "negate >= → <") in got and (4, "condition → false") in got and (5, "+ → -") in got and (7, "true → false") in got
    # With an initializer, a constant condition would leave err unused, which does not compile.
    init = "package p\n\nfunc g() error {\n\tif err := f(); err != nil {\n\t\treturn err\n\t}\n\treturn nil\n}\n"
    got = offered("p/g.go", init, {4})
    assert (4, "negate condition") in got and not any(op.startswith("condition →") for _, op in got)
    rb = "def f(a, b)\n  return a + 1 if a == b and !c\n  a < b || false\nend\n"
    got = offered("lib/f.rb", rb, {2, 3})
    assert (2, "negate == → !=") in got and (2, "and → or") in got and (3, "|| → &&") in got


def test_a_break_is_a_byte_edit_and_one_that_stops_parsing_is_unbuildable():
    m = next(m for m in languages.generate("src/price.js", JS, {6}, mutate.Mutant) if m.operator == "+ → -")
    built = mutate.build(m)
    assert built == JS.replace("return qty + 1;", "return qty - 1;")
    m.replacement = "+ +("  # something that does not parse
    assert mutate.build(m) is None
    guard = next(m for m in languages.generate("src/price.js", JS, {2}, mutate.Mutant) if m.operator == "condition → false")
    assert mutate.build(guard) == JS.replace("if (qty > 10 && !member)", "if (false)")


def test_without_the_parser_the_files_are_skipped_and_python_still_runs(monkeypatch):
    def missing(language):
        raise languages.ParserMissing(f"breaking {language} needs tree-sitter, which is not installed here")

    monkeypatch.setattr(languages, "_parser", missing)
    with pytest.raises(languages.ParserMissing):
        languages.generate("src/price.js", JS, {2}, mutate.Mutant)


def test_test_files_in_every_language_are_not_broken():
    for path in ("web/a.test.ts", "web/a.spec.js", "web/__tests__/a.js", "p/a_test.go", "spec/a_spec.rb", "test/a_test.rb"):
        assert mutate.is_test_path(path), path
    assert not mutate.is_test_path("web/contest.js")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def repo_with(tmp_path: Path, files: dict[str, str], change: dict[str, str]) -> str:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    for name, text in change.items():
        (tmp_path / name).write_text(text)
    git(tmp_path, "commit", "-qam", "change")
    return base


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_end_to_end_on_a_javascript_project(tmp_path):
    lib = "function discount(qty) {\n  return qty > 10 ? 5 : 0;\n}\nmodule.exports = { discount };\n"
    changed = "function discount(qty) {\n  return qty >= 10 ? 5 : 0;\n}\nmodule.exports = { discount };\n"
    test = ("const { test } = require('node:test');\nconst assert = require('node:assert');\n"
            "const { discount } = require('../lib.js');\ntest('large', () => assert.equal(discount(50), 5));\n")
    base = repo_with(tmp_path, {"lib.js": lib, "test/discount.test.js": test}, {"lib.js": changed})
    report = mutate.run(base, "HEAD", ["node", "--test"], tmp_path, limit=10)
    statuses = {m.operator: m.status for m in report.mutants}
    assert statuses["negate >= → <"] == "killed"
    assert statuses["boundary >= → >"] == "survived"  # nothing tests exactly 10
    assert (tmp_path / "lib.js").read_text() == changed


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is not installed")
def test_end_to_end_on_a_ruby_project(tmp_path):
    lib = "def big?(n)\n  n > 100\nend\n"
    changed = "def big?(n)\n  n >= 100\nend\n"
    test = "require_relative '../lib'\nabort('big') unless big?(500)\nabort('small') if big?(1)\n"
    base = repo_with(tmp_path, {"lib.rb": lib, "test/big_test.rb": test}, {"lib.rb": changed})
    report = mutate.run(base, "HEAD", ["ruby", "test/big_test.rb"], tmp_path, limit=10)
    statuses = {m.operator: m.status for m in report.mutants}
    assert statuses["negate >= → <"] == "killed" and statuses["boundary >= → >"] == "survived"


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_build_failures_are_unbuildable_not_caught(tmp_path):
    files = {"go.mod": "module p\n\ngo 1.21\n", "p.go": "package p\n\nfunc Name(a string) string {\n\treturn a + \"!\"\n}\n",
             "p_test.go": "package p\n\nimport \"testing\"\n\nfunc TestName(t *testing.T) {\n\tif Name(\"a\") != \"a!\" {\n\t\tt.Fail()\n\t}\n}\n"}
    # The change and its test move together, so the baseline passes at the head.
    base = repo_with(tmp_path, files, {"p.go": "package p\n\nfunc Name(a string) string {\n\treturn a + \"?\"\n}\n",
                                       "p_test.go": files["p_test.go"].replace('"a!"', '"a?"')})
    report = mutate.run(base, "HEAD", ["go", "test", "./..."], tmp_path, limit=10)
    # string - string does not compile: unbuildable, never counted as caught
    assert {m.operator: m.status for m in report.mutants}["+ → -"] == "unbuildable"


def test_a_failing_baseline_names_the_failing_tests_and_logs_the_rest(tmp_path, capsys):
    lib = "def big(n):\n    return n > 100\n"
    test = "from lib import big\n\ndef test_big():\n    print('token=sekret')\n    assert big(5)\n"
    base = repo_with(tmp_path, {"lib.py": lib, "test_lib.py": test}, {"lib.py": lib.replace("100", "101")})
    report = mutate.run(base, "HEAD", ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_lib.py"], tmp_path)
    assert report.error and report.mutants == []
    assert report.baseline_failures == ["FAILED test_lib.py::test_big - assert False"]
    data = report.to_dict()
    assert "sekret" not in json.dumps(data)  # the report is posted unmasked: names only
    assert "sekret" in capsys.readouterr().err  # the job log, which GitHub masks, has the output
    assert "Failing before any break" in report.to_markdown()


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_a_go_test_failing_before_any_break_is_skipped_and_the_rest_checked(tmp_path):
    lib = "package p\n\nfunc Big(n int) bool {\n\treturn n > 100\n}\n"
    tests = ("package p\n\nimport \"testing\"\n\nfunc TestBig(t *testing.T) {\n\tif !Big(500) || Big(1) {\n\t\tt.Fail()\n\t}\n}\n\n"
             "func TestStale(t *testing.T) {\n\tt.Run(\"year\", func(t *testing.T) { t.Fail() })\n}\n")
    base = repo_with(tmp_path, {"go.mod": "module p\n\ngo 1.21\n", "p.go": lib, "p_test.go": tests},
                     {"p.go": lib.replace("n > 100", "n >= 100")})
    report = mutate.run(base, "HEAD", ["go", "test", "./..."], tmp_path, limit=10)
    assert report.error is None and report.set_aside == ["TestStale"]
    statuses = {m.operator: m.status for m in report.mutants}
    assert statuses["negate >= → <"] == "killed" and statuses["boundary >= → >"] == "survived"


@pytest.mark.parametrize("line", [
    "FAILED tests/test_a.py::test_x - AssertionError", "--- FAIL: TestName (0.00s)", "not ok 3 - discount for large orders",
    "  ✕ renders the cart (5 ms)", "rspec ./spec/cart_spec.rb:12 # Cart totals", "FAIL src/cart.test.ts",
])
def test_failing_test_names_are_recognised_for_the_common_runners(line):
    assert mutate.failing_tests(f"noise\n{line}\nmore noise") == [line.strip()]


def test_playwright_failures_are_named_from_its_summary_without_the_rule():
    out = ("Running 3 tests using 1 worker\n  ✓  1 [chromium] › tests/home.spec.ts:3:5 › loads (1.1s)\n"
           "  1) [chromium] › tests/cart.spec.ts:12:5 › adds an item ──────────────────────\n"
           "    Error: expect(received).toBe(expected)\n  1 failed\n")
    assert mutate.failing_tests(out) == ["1) [chromium] › tests/cart.spec.ts:12:5 › adds an item"]
