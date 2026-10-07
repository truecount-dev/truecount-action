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
    assert ops(mutants, 6) == ["negate condition", "negate == → !="]
    assert ops(mutants, 8) == ["return None", "True → False"]
    assert ops(mutants, 9) == ["negate condition", "remove not"]
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
    assert [(m.line, m.operator) for m in chosen] == [
        (6, "negate condition"), (9, "negate condition"), (13, "return None"), (6, "negate == → !="),
    ]
    assert len(mutate.select(mutants, 1000)) == len(mutants)


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

    # Nothing calls clamp() above `high`, so breaking that branch goes unnoticed.
    assert status[(5, "return None")] == "survived"
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
    assert "The tests caught **7 of 10** deliberate breaks" in markdown
    # Sorted by line. The two boundary survivors are equivalent mutants: at
    # n == low, returning low and returning n are the same value. The report
    # says a survivor is a lead, not proof, for exactly this reason.
    assert markdown.split("**Not caught**")[1].split("Each of these")[0].strip().splitlines() == [
        "- `calc.py:2` `n < low` → `n <= low` (boundary < → <=)",
        "- `calc.py:4` `n > high` → `n >= high` (boundary > → >=)",
        "- `calc.py:5` `return high` → `return None` (return None)",
    ]


def test_failing_baseline_is_reported_not_mutated(project):
    (project / "tests" / "test_calc.py").write_text("def test_broken():\n    assert False\n")
    git(project, "commit", "-q", "-am", "break")
    report = mutate.run("HEAD~2", "HEAD", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], project)
    assert report.error and report.mutants == []
    assert "Not run:" in report.to_markdown()


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
