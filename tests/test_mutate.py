import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from truecount_action import mutate

SAMPLE = textwrap.dedent(
    '''\
    import logging
    log = logging.getLogger(__name__)

    def check(a: int, b: int = 0) -> Literal[1]:
        """Docstring."""
        if a == b:
            log.info("equal")
            return True
        if not a:
            raise ValueError("no a")
        total = a + 1
        notify(total)
        return total > b and a < 10

    if __name__ == "__main__":
        check(1)
    '''
)


def ops(mutants, line=None):
    return [m.operator for m in mutants if line is None or m.line == line]


def test_parse_diff_reads_new_side_lines():
    diff = textwrap.dedent(
        """\
        diff --git a/app.py b/app.py
        --- a/app.py
        +++ b/app.py
        @@ -3,0 +4,2 @@ def f():
        +    x = 1
        +    y = 2
        @@ -10 +12 @@
        -old
        +new
        @@ -20,3 +23,0 @@
        -gone
        diff --git a/old.py b/old.py
        --- a/old.py
        +++ /dev/null
        @@ -1 +0,0 @@
        -x
        """
    )
    assert mutate.parse_diff(diff) == {"app.py": {4, 5, 12}}


def test_test_files_are_never_mutated():
    for path in ("tests/test_a.py", "pkg/tests/helpers.py", "test_x.py", "x_test.py", "conftest.py"):
        assert mutate.is_test_path(path)
    for path in ("app.py", "pkg/testing_utils.py", "contest.py"):
        assert not mutate.is_test_path(path)


def test_breaks_offered_on_each_line():
    mutants = mutate.generate("m.py", SAMPLE, set(range(1, 20)))
    assert ops(mutants, 4) == ["0 → 1"]  # the default value; the 1 inside the annotation is skipped
    assert ops(mutants, 6) == ["condition → False", "condition → True", "negate == → !="]
    assert ops(mutants, 8) == ["return None", "True → False"]
    assert ops(mutants, 9) == ["condition → False", "condition → True", "remove not"]
    assert ops(mutants, 10) == ["remove raise"]
    assert ops(mutants, 11) == ["+ → -", "1 → 2"]
    assert ops(mutants, 12) == ["remove call"]
    assert ops(mutants, 13) == [
        "return None", "and → or",
        "negate > → <=", "boundary > → >=", "negate < → >=", "boundary < → <=", "10 → 11",
    ]


def test_logging_docstrings_and_main_guard_are_left_alone():
    mutants = mutate.generate("m.py", SAMPLE, set(range(1, 20)))
    assert ops(mutants, 5) == []  # docstring
    assert ops(mutants, 7) == []  # log.info
    assert ops(mutants, 15) == [] and ops(mutants, 16) == []  # if __name__ == "__main__"


def test_only_changed_lines_are_broken():
    assert {m.line for m in mutate.generate("m.py", SAMPLE, {6, 13})} == {6, 13}


def test_each_built_mutant_changes_one_thing_and_compiles():
    mutants = mutate.generate("m.py", SAMPLE, {11})
    built = [mutate.build(m) for m in mutants]
    assert all(b is not None for b in built)
    assert "total = a - 1" in built[0]
    assert "total = a + 2" in built[1]


def test_select_spreads_the_budget_across_lines():
    mutants = mutate.generate("m.py", SAMPLE, {6, 9, 13})
    chosen = mutate.select(mutants, 4)
    assert sorted(m.line for m in chosen[:3]) == [6, 9, 13]  # one on each line before a second on any
    assert len(mutate.select(mutants, 1000)) == len(mutants)


def test_a_small_budget_reaches_across_the_whole_change():
    # A hundred changed lines in two files and a budget of four: the first four
    # lines in file order would leave b.py and most of a.py untouched.
    mutants = [mutate.Mutant(path, n, "op", "", "") for path in ("a.py", "b.py") for n in range(1, 51)]
    assert [(m.path, m.line) for m in mutate.select(mutants, 4)] == [
        ("a.py", 1), ("b.py", 15), ("a.py", 33), ("b.py", 47),  # positions 0, 64, 32 and 96 of 100
    ]


def test_test_runs_never_read_stale_bytecode(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            seen.update(kwargs["env"])
            self.pid = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(mutate.subprocess, "Popen", FakePopen)
    assert mutate.run_tests(["true"], tmp_path, 5, str(tmp_path / "cache"))[0] == "passed"
    assert seen["PYTHONDONTWRITEBYTECODE"] == "1"
    assert seen["PYTHONPYCACHEPREFIX"] == str(tmp_path / "cache")


def test_a_hung_test_run_is_a_timeout(tmp_path):
    status, _ = mutate.run_tests([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 0.5, str(tmp_path / "c"))
    assert status == "timeout"


# ── End to end, on a real repository with real tests ──────────────────────

BEFORE = "def clamp(n, low, high):\n    return n\n"
AFTER = textwrap.dedent(
    """\
    def clamp(n, low, high):
        if n < low:
            return low
        if n > high:
            return high
        return n

    def same(a, b):
        return a == b
    """
)
TESTS = textwrap.dedent(
    """\
    from calc import clamp, same

    def test_low():
        assert clamp(-5, 0, 10) == 0

    def test_mid():
        assert clamp(5, 0, 10) == 5

    def test_same():
        assert same(1, 1) is True
    """
)


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "calc.py").write_text(BEFORE)
    (tmp_path / "tests" / "test_calc.py").write_text(TESTS.replace(", same", "").split("def test_same")[0])
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "calc.py").write_text(AFTER)
    (tmp_path / "tests" / "test_calc.py").write_text(TESTS)
    git(tmp_path, "commit", "-q", "-am", "clamp")
    return tmp_path


def test_end_to_end_finds_the_untested_branch_and_restores_every_file(project):
    command = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
    report = mutate.run("HEAD~1", "HEAD", command, project, limit=100)
    status = {(m.line, m.operator): m.status for m in report.mutants}

    # Nothing calls clamp() above `high`, so breaking that branch goes
    # unnoticed, and so does removing its guard altogether.
    assert status[(5, "return None")] == "survived"
    assert status[(4, "condition → False")] == "survived"
    assert status[(4, "condition → True")] == "killed"
    assert status[(2, "negate < → >=")] == "killed"
    assert status[(3, "return None")] == "killed"
    # Same length as the original: only caught if no stale bytecode is reused.
    assert status[(9, "negate == → !=")] == "killed"
    # The tests file changed too, and was not mutated.
    assert all(m.path == "calc.py" for m in report.mutants)

    assert (project / "calc.py").read_text() == AFTER
    assert not (project / mutate.BACKUP_DIR).exists()
    git(project, "diff", "--exit-code")

    markdown = report.to_markdown()
    assert "The tests caught **8 of 12** deliberate breaks" in markdown
    # Sorted by line. The two boundary survivors are equivalent mutants: at
    # n == low, returning low and returning n are the same value. The report
    # says a survivor is a lead, not proof, for exactly this reason.
    assert markdown.split("**Not caught**")[1].split("Each of these")[0].strip().splitlines() == [
        "- `calc.py:2` `n < low` → `n <= low` (boundary < → <=)",
        "- `calc.py:4` `if n > high:` → `if False:` (condition → False)",
        "- `calc.py:4` `n > high` → `n >= high` (boundary > → >=)",
        "- `calc.py:5` `return high` → `return None` (return None)",
    ]


def test_failing_baseline_is_reported_not_mutated(project):
    # Every test fails: set aside, nothing is left to run, so nothing is checked.
    (project / "tests" / "test_calc.py").write_text("def test_broken():\n    assert False\n")
    git(project, "commit", "-q", "-am", "break")
    report = mutate.run("HEAD~2", "HEAD", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], project)
    assert report.error and report.mutants == [] and report.set_aside == []
    assert "Not run:" in report.to_markdown()


def test_tests_failing_before_any_break_are_set_aside_and_the_rest_checked(project, capsys):
    # Two tests stale with the calendar, beside the real ones. With -x each
    # baseline run stops at the first, so they are found one run at a time.
    stale = "\n\ndef test_stale_a():\n    assert 2025 > 2026\n\n\ndef test_stale_b():\n    assert False\n"
    (project / "tests" / "test_calc.py").write_text(TESTS + stale)
    git(project, "commit", "-q", "-am", "stale")
    command = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
    report = mutate.run("HEAD~2", "HEAD", command, project, limit=100)
    assert report.error is None and report.baseline_failures == []
    assert report.set_aside == ["tests/test_calc.py::test_stale_a", "tests/test_calc.py::test_stale_b"]
    assert report.command == command  # the report names the command as given
    status = {(m.line, m.operator): m.status for m in report.mutants}
    assert status[(2, "negate < → >=")] == "killed" and status[(5, "return None")] == "survived"
    assert "**Set aside**" in report.to_markdown() and "`tests/test_calc.py::test_stale_b`" in report.to_markdown()
    assert report.to_dict()["set_aside"] == report.set_aside
    assert "Set aside, failing before any break: tests/test_calc.py::test_stale_a" in capsys.readouterr().err
    assert (project / "calc.py").read_text() == AFTER


def test_a_collection_error_is_not_set_aside(project):
    (project / "tests" / "test_broken.py").write_text("def test_x(:\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "syntax error in a test file")
    report = mutate.run("HEAD~2", "HEAD", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], project)
    assert report.error and report.set_aside == [] and report.mutants == []


def test_failing_ids_are_read_only_when_every_failure_is_a_named_test():
    pytest_out = "x\nFAILED tests/t.py::test_a[a b] - assert 1 == 2\nERROR tests/t.py::test_b - fixture 'db' not found\n"
    assert mutate.failing_ids(pytest_out) == ["tests/t.py::test_a[a b]", "tests/t.py::test_b"]
    assert mutate.failing_ids("--- FAIL: TestBad (0.00s)\n    --- FAIL: TestBad/sub (0.00s)\n") == ["TestBad"]
    assert mutate.failing_ids("ERROR tests/t.py - SyntaxError\nFAILED tests/u.py::test_c\n") is None
    assert mutate.failing_ids("not ok 1 - discount\n") is None  # a runner that cannot leave a test out
    assert mutate.failing_ids("Required test coverage of 90% not reached.\n") is None


def test_set_aside_keeps_what_the_environment_already_sets(monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:randomly")
    monkeypatch.delenv("GOFLAGS", raising=False)
    assert mutate.set_aside_env(["tests/t.py::test_a[a b]", "TestBad", "TestOld"]) == [
        "PYTEST_ADDOPTS=-p no:randomly --deselect 'tests/t.py::test_a[a b]'",
        "GOFLAGS=-skip=^(TestBad|TestOld)$",
    ]


def test_cli_writes_json_and_markdown(project, monkeypatch, tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    monkeypatch.chdir(project)
    code = mutate.main(
        ["--base", "HEAD~1", "--max", "3", "--json", str(out / "r.json"), "--markdown", str(out / "r.md"),
         "--", sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
    )
    assert code == 0
    assert (out / "r.md").read_text().startswith("### Truecount mutation check")
    assert '"summary"' in (out / "r.json").read_text()


def test_the_next_run_repairs_a_file_a_killed_run_left_broken(project):
    # What a SIGKILL mid-break leaves behind: the broken file, and its backup.
    backup = project / mutate.BACKUP_DIR / "calc.py"
    backup.parent.mkdir()
    backup.write_text(AFTER)
    (project / "calc.py").write_text(AFTER.replace("n < low", "n >= low"))
    assert mutate.recover(project) == ["calc.py"]
    assert (project / "calc.py").read_text() == AFTER
    assert not backup.parent.exists()
    assert mutate.recover(project) == []


def test_run_recovers_before_it_reads_the_diff(project):
    backup = project / mutate.BACKUP_DIR / "calc.py"
    backup.parent.mkdir()
    backup.write_text(AFTER)
    (project / "calc.py").write_text("broken = True\n")
    report = mutate.run("HEAD~1", "HEAD", [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"], project, limit=1)
    assert (project / "calc.py").read_text() == AFTER
    assert report.mutants and report.mutants[0].status in ("killed", "survived")


def test_jobs_run_breaks_at_once_in_copies_and_agree_with_one_at_a_time(project):
    command = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
    serial = mutate.run("HEAD~1", "HEAD", command, project, limit=100)
    parallel = mutate.run("HEAD~1", "HEAD", command, project, limit=100, jobs=3)
    assert [(m.line, m.operator, m.status) for m in parallel.mutants] == \
        [(m.line, m.operator, m.status) for m in serial.mutants]
    assert (project / "calc.py").read_text() == AFTER
    git(project, "diff", "--exit-code")
    assert not (project / mutate.BACKUP_DIR).exists()


def test_a_copy_that_runs_the_original_code_falls_back_to_one_at_a_time(project, capsys):
    # What an editable install does: the copy's tests import the original checkout.
    (project / "tests" / "conftest.py").write_text(f"import sys\nsys.path.insert(0, {str(project)!r})\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "editable")
    command = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
    report = mutate.run("HEAD~2", "HEAD", command, project, limit=100, jobs=3)
    assert "--jobs: the tests still passed in a copy" in capsys.readouterr().err
    # Run in place instead, so the copy's blindness cannot make a break survive.
    assert {(m.line, m.operator): m.status for m in report.mutants}[(2, "negate < → >=")] == "killed"
    assert (project / "calc.py").read_text() == AFTER
