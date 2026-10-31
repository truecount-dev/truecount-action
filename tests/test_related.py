"""Each break runs only the tests named like its file, unless asked for all."""

import json
import subprocess
import sys

import pytest

from truecount_action import mutate, related

TREE = ["app/billing.py", "app/billing_cli.py", "app/README.md", "tests/test_billing.py", "tests/unit/billing_test.py",
        "tests/test_billing_export.py", "tests/test_other.py", "web/cart.js", "web/cart.test.js", "pkg/tax/rate.go",
        "pkg/tax/rate_test.go"]


def test_tests_named_like_the_file_nearest_and_conventional_first():
    assert related.tests_for("app/billing.py", TREE) == [
        "tests/test_billing.py", "tests/unit/billing_test.py", "tests/test_billing_export.py"]
    assert related.tests_for("web/cart.js", TREE) == ["web/cart.test.js"]
    assert related.tests_for("app/README.md", TREE) == []  # not code
    assert related.tests_for("tests/test_billing.py", TREE) == []  # a test file is not broken anyway


def test_the_command_names_the_tests_or_the_package_or_gives_up():
    assert related.command_for("python -m pytest -x -q {tests}", "web/cart.js", TREE) == \
        "python -m pytest -x -q web/cart.test.js"
    assert related.command_for("go test -failfast {packages}", "pkg/tax/rate.go", TREE) == "go test -failfast ./pkg/tax"
    assert related.command_for("go test {packages}", "main.go", TREE) == "go test ."
    assert related.command_for("npx jest {tests}", "app/billing_cli.py", ["app/billing_cli.py"]) is None
    assert related.command_for("pytest {tests}", "a b.py", ["tests/test_a b.py"]) == "pytest 'tests/test_a b.py'"


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True)


CALC = "def clamp(n, low, high):\n    if n < low:\n        return low\n    if n > high:\n        return high\n    return n\n"
SHOUT = "def shout(s):\n    return s.upper() + '!'\n"


@pytest.fixture
def project(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (tmp_path / "calc.py").write_text("def clamp(n, low, high):\n    return n\n")
    (tmp_path / "shout.py").write_text("def shout(s):\n    return s\n")
    (tmp_path / "tests" / "test_calc.py").write_text("from calc import clamp\n\ndef test_low():\n    assert clamp(-5, 0, 10) == 0\n")
    # Catches clamp() above `high`, but is not named like calc.py.
    (tmp_path / "tests" / "test_limits.py").write_text("from calc import clamp\n\ndef test_high():\n    assert clamp(50, 0, 10) == 10\n")
    git(tmp_path, "init", "-q", "-b", "main"), git(tmp_path, "add", "-A"), git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "calc.py").write_text(CALC)
    (tmp_path / "shout.py").write_text(SHOUT)  # no test is named like it
    git(tmp_path, "commit", "-q", "-am", "clamp and shout")
    return tmp_path


COMMAND = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
RELATED = f"{sys.executable} -m pytest -x -q -p no:cacheprovider {{tests}}"


def test_related_runs_miss_a_test_elsewhere_and_say_so_and_all_tests_catches_it(project):
    report = mutate.run("HEAD~1", "HEAD", COMMAND, project, limit=100, related_command=RELATED)
    status = {(m.path, m.line, m.operator): (m.status, m.scope) for m in report.mutants}
    # Only test_calc.py ran for calc.py, and it never goes above `high`.
    assert status[("calc.py", 5, "return None")] == ("survived", "related")
    # shout.py has no test named like it, so its breaks ran the whole suite.
    assert {s for (p, *_), (_, s) in status.items() if p == "shout.py"} == {"full"}
    markdown = report.to_markdown()
    assert "ran only the tests named like the file they broke" in markdown
    assert "(return None) — related tests only" in markdown and "`truecount: all tests`" in markdown
    data = report.to_dict()
    assert data["related_command"] == RELATED and {m["scope"] for m in data["mutants"]} == {"related", "full"}

    everything = mutate.run("HEAD~1", "HEAD", COMMAND, project, limit=100, related_command=RELATED, all_tests=True)
    status = {(m.path, m.line, m.operator): (m.status, m.scope) for m in everything.mutants}
    assert status[("calc.py", 5, "return None")] == ("killed", "full")  # test_limits.py catches it
    assert everything.to_dict()["related_command"] == "" and "related tests only" not in everything.to_markdown()
    assert (project / "calc.py").read_text() == CALC


def test_related_needs_a_placeholder(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    with pytest.raises(SystemExit):
        mutate.main(["--base", "HEAD~1", "--related", "pytest -x", "--", *COMMAND])
    assert "{tests} or {packages}" in capsys.readouterr().err


def test_the_cli_passes_related_and_all_tests_through(project, monkeypatch, tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "r.json"
    monkeypatch.chdir(project)
    assert mutate.main(["--base", "HEAD~1", "--related", RELATED, "--json", str(out), "--", *COMMAND]) == 0
    assert "related" in {m["scope"] for m in json.loads(out.read_text())["mutants"]}
    assert mutate.main(["--base", "HEAD~1", "--related", RELATED, "--all-tests", "--json", str(out), "--", *COMMAND]) == 0
    assert {m["scope"] for m in json.loads(out.read_text())["mutants"]} == {"full"}
