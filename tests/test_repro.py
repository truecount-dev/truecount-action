"""The bug-repro agent: one new test file, written and run until it fails for the reported bug."""

import json
import subprocess
import sys

import pytest

from truecount_action import repro

BUGGY = "def add(a, b):\n    return a - b\n"
REPRO = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "calc.py").write_text(BUGGY)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_existing.py").write_text("def test_nothing():\n    pass\n")
    (tmp_path / "conftest.py").write_text("import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    return repro.Checkout(tmp_path, f"{sys.executable} -m pytest -q -p no:cacheprovider {{tests}}")


def call(name, n=[0], **args):  # noqa: B006 - a counter for unique ids
    n[0] += 1
    return {"id": f"c{n[0]}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Model:
    """Replies in order; records each request."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request):
        self.requests.append(json.loads(json.dumps(request)))
        reply = self.replies.pop(0) if self.replies else {"content": "", "tool_calls": [call("search", text="add")]}
        return {"choices": [{"message": {"role": "assistant", **reply}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def tools(*calls):
    return {"content": None, "tool_calls": list(calls)}


def test_it_writes_runs_and_reports_a_test_that_fails_for_the_bug(checkout):
    model = Model(
        tools(call("search", text="def add"), call("read_file", path="calc.py")),
        tools(call("write_test", path="tests/test_add_issue.py", content=REPRO)),
        tools(call("run_test")),
        {"content": '{"reproduced": true, "test_path": "tests/test_add_issue.py", "summary": "add subtracts"}'},
    )
    result = repro.reproduce(model, checkout, "add() is wrong", "add(2, 2) gives 0")
    assert (result.stopped, result.reproduced, result.test_path, result.calls, result.runs) == \
        ("answered", True, "tests/test_add_issue.py", 4, 1)
    assert result.last_exit == 1 and "assert 0 == 4" in result.last_output
    assert (checkout.root / "tests/test_add_issue.py").read_text() == REPRO
    tool_results = [m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool"]
    assert tool_results[0].startswith("calc.py:1:") and tool_results[1] == "1: def add(a, b):\n2:     return a - b"
    # The issue is in the user's message, never the system prompt.
    system, user = model.requests[0]["messages"][:2]
    assert "add(2, 2) gives 0" in user["content"] and "add(2, 2)" not in system["content"]
    assert "tests/" in user["content"]  # the top level is given, saving a call
    assert len(result.usage) == 4


@pytest.mark.parametrize("path, why", [
    ("/tmp/test_x.py", "relative"),
    ("tests/../../test_x.py", "relative"),
    ("calc_check.py", "does not look like a test file"),
    ("tests/test_existing.py", "must be a new file"),
])
def test_it_writes_only_one_new_test_file_inside_the_project(checkout, path, why):
    model = Model(tools(call("write_test", path=path, content=REPRO)), {"content": '{"reproduced": false}'})
    result = repro.reproduce(model, checkout, "t", "b")
    assert result.test_path == ""
    refusal = [m for m in model.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert refusal.startswith("refused") and why in refusal


def test_a_second_file_is_refused_and_the_first_can_be_rewritten(checkout):
    model = Model(tools(call("write_test", path="tests/test_a.py", content="x = 1\n"),
                        call("write_test", path="tests/test_b.py", content=REPRO),
                        call("write_test", path="tests/test_a.py", content=REPRO)),
                  {"content": '{"reproduced": false, "summary": "s"}'})
    result = repro.reproduce(model, checkout, "t", "b")
    results = [m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool"]
    assert results[1].startswith("refused: you already wrote tests/test_a.py")
    assert (result.test_path, result.test) == ("tests/test_a.py", REPRO)
    assert not (checkout.root / "tests/test_b.py").exists()


def test_runs_and_calls_are_capped_and_then_it_is_asked_for_the_answer(checkout):
    model = Model(tools(call("write_test", path="tests/test_a.py", content=REPRO)),
                  *[tools(call("run_test")) for _ in range(3)])
    result = repro.reproduce(model, checkout, "t", "b", max_calls=5, max_runs=2)
    assert (result.calls, result.runs, result.stopped) == (5, 2, "out_of_calls")
    results = [m["content"] for m in model.requests[-1]["messages"] if m["role"] == "tool"]
    assert "no more runs: you have used all 2" in results
    assert "tools" in model.requests[3] and all("tools" not in r for r in model.requests[5:])
    assert len(model.requests) == 7  # five with tools, then asked twice for the answer


def test_the_agent_claims_nothing_without_a_test(checkout):
    model = Model({"content": 'Here: {"reproduced": true, "test_path": "", "summary": "it is broken"}'})
    result = repro.reproduce(model, checkout, "t", "b")
    assert (result.stopped, result.reproduced, result.summary) == ("answered", False, "it is broken")
    assert repro.reproduce(Model({"content": "no json"}), checkout, "t", "b").stopped == "no_answer"


def test_cache_hints_mark_the_system_prompt_and_the_newest_message(checkout):
    model = Model(tools(call("search", text="add")), {"content": '{"reproduced": false}'})
    repro.reproduce(model, checkout, "t", "b", cache_hints=True)
    second = model.requests[1]["messages"]
    assert second[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert second[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(second[1]["content"], str)
    assert isinstance(repro.reproduce(Model({"content": "{}"}), checkout, "t", "b").usage, list)
