"""Breaks inside SQL written as Python strings."""

import subprocess
import sys
import textwrap

from truecount_action import mutate, sql

SAMPLE = textwrap.dedent(
    '''\
    """Reads SELECT x FROM t WHERE a < b ORDER BY x DESC, but is a docstring."""
    # a comment
    """WHERE a < b"""
    LATEST = ("SELECT id FROM views"
              " WHERE day >= ? AND day <= ?{site}"
              "  AND p.source = '' AND p.n > 0 THEN 1"
              " ORDER BY created_at DESC, id DESC LIMIT 1")
    MESSAGE = "a < b, in ORDER"
    FORMATTED = f"SELECT x WHERE a < {b}"
    RAW = b"SELECT x WHERE a < 1"
    LONG = """
        SELECT a
        FROM t
        WHERE a >= ?
          AND b = 1
        ORDER BY a ASC
    """
    ''')


def offered(source, lines=None):
    return [(m.line, m.operator) for m in sql.generate("q.py", source, lines or set(range(1, 40)), mutate.Mutant)]


def test_three_breaks_inside_sql_strings_on_changed_lines_only():
    assert offered(SAMPLE) == [
        (5, "sql boundary >= → >"), (5, "sql drop AND condition"), (5, "sql boundary <= → <"),
        (6, "sql drop AND condition"), (6, "sql drop AND condition"), (6, "sql boundary > → >="),
        (7, "sql DESC → ASC"), (7, "sql DESC → ASC"),
        (14, "sql boundary >= → >"), (15, "sql drop AND condition"), (16, "sql ASC → DESC"),
    ]
    # Docstrings, a message without an SQL keyword, f-strings and bytes are left alone.
    assert {line for line, _ in offered(SAMPLE)} & {1, 3, 8, 9, 10} == set()
    assert offered(SAMPLE, {7}) == [(7, "sql DESC → ASC"), (7, "sql DESC → ASC")]


def test_python_breaks_and_sql_breaks_come_from_one_generate():
    ops = [m.operator for m in mutate.generate("q.py", SAMPLE, {7})]
    assert ops == ["sql DESC → ASC", "sql DESC → ASC"]


def test_operators_that_only_look_like_comparisons_are_not_broken():
    for text in ("WHERE data->>'a' = ?", "WHERE a <> b", "WHERE a != b", "WHERE f(a => 1)",
                 "WHERE a << 2", "WHERE data -> 'k' = ?", "WHERE a <=> b"):
        assert not [e for e in sql.edits(text) if "boundary" in e[0]], text


def test_only_a_plain_condition_before_a_clause_boundary_is_dropped():
    def dropped(text):
        return [text[s:e] for op, s, e, _ in sql.edits(text) if op == "sql drop AND condition"]
    assert dropped("WHERE a = 1 AND b = ? ORDER BY a") == [" AND b = ?"]
    assert dropped("WHERE a = 1 AND b IS NOT NULL)") == [" AND b IS NOT NULL"]
    assert dropped("WHERE a = 1 AND p.c = 'x' AND d = :d") == [" AND p.c = 'x'", " AND d = :d"]
    assert dropped("WHERE a = 1 AND b = f(c)") == []  # the value is a call
    assert dropped("WHERE a = 1 AND b = c + 1") == []  # what is left would not parse
    assert dropped("WHERE a BETWEEN ? AND ? LIMIT 1") == []


def test_a_break_is_a_byte_edit_that_keeps_the_file_compiling():
    source = '# café\nQ = "é SELECT a FROM t WHERE a < 1 ORDER BY a DESC"  # naïve\n'
    built = [mutate.build(m) for m in sql.generate("q.py", source, {2}, mutate.Mutant)]
    assert built == [source.replace("a < 1", "a <= 1"), source.replace("a DESC", "a ASC")]
    m = sql.generate("q.py", source, {2}, mutate.Mutant)[0]
    m.replacement = '"'  # ends the string early: no longer compiles
    assert mutate.build(m) is None


def test_end_to_end_a_query_whose_order_no_test_checks(tmp_path):
    before = "def latest(conn):\n    return conn.execute('SELECT name FROM t LIMIT 1').fetchone()[0]\n"
    after = "def latest(conn):\n    return conn.execute('SELECT name FROM t ORDER BY at DESC LIMIT 1').fetchone()[0]\n"
    test = ("import sqlite3\nfrom store import latest\n\n"
            "def test_latest():\n    conn = sqlite3.connect(':memory:')\n"
            "    conn.execute('CREATE TABLE t (name TEXT, at INTEGER)')\n"
            "    conn.execute(\"INSERT INTO t VALUES ('only', 1)\")\n"
            "    assert latest(conn) == 'only'\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "store.py").write_text(before)
    (tmp_path / "tests" / "test_store.py").write_text(test)
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    run = lambda *a: subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *a],  # noqa: E731
                                    cwd=tmp_path, check=True, capture_output=True)
    run("init", "-q", "-b", "main"), run("add", "-A"), run("commit", "-q", "-m", "base")
    (tmp_path / "store.py").write_text(after)
    run("commit", "-q", "-am", "newest first")
    report = mutate.run("HEAD~1", "HEAD", [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"],
                        tmp_path, limit=10)
    # One row: newest first and oldest first are the same answer.
    assert {m.operator: m.status for m in report.mutants}["sql DESC → ASC"] == "survived"
    assert (tmp_path / "store.py").read_text() == after
