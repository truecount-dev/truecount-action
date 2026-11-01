"""Bug reproduction in the runner: issue in, run token, the loop, at most one draft pull request out."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from truecount_action import repro, repro_run

API = "https://api.example.test"
SECRETS = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example.test/?x=1", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req-secret",
           "ACTIONS_RUNTIME_TOKEN": "rt-secret", "GITHUB_TOKEN": "gh-secret", "TC_GITHUB_TOKEN": "gh-secret"}
# The model's test checks its own environment: it fails for "the bug" only if no credential reached it.
TEST = ('import os\n\n\ndef test_bug():\n'
        '    assert not [k for k in ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_RUNTIME_TOKEN", "GITHUB_TOKEN",\n'
        '                            "TC_GITHUB_TOKEN") if k in os.environ], "a credential leaked"\n'
        '    assert 1 + 1 == 3, "the bug"\n')


def call(name, n=[0], **args):  # noqa: B006 - a counter for unique ids
    n[0] += 1
    return {"id": f"c{n[0]}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(message):
    return {"choices": [{"message": {"role": "assistant", **message}}], "usage": {}}


@pytest.fixture
def job(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_existing.py").write_text("def test_ok():\n    pass\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "opened", "issue": {"number": 12, "title": "Adding is wrong", "body": "1 + 1 is 2"}}))
    env = {"PATH": "/usr/bin:/bin", "GITHUB_WORKSPACE": str(root), "GITHUB_EVENT_PATH": str(event),
           "GITHUB_REPOSITORY": "acme/widgets", "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"), **SECRETS}
    return root, env, tmp_path


class Server:
    """GitHub's OIDC endpoint and Truecount's API, as `repro_run.request` sees them."""

    def __init__(self, model_replies, token_status=200, pulls_status=201):
        self.model_replies, self.token_status, self.pulls_status = list(model_replies), token_status, pulls_status
        self.calls = []

    def __call__(self, url, body=None, token="", timeout=30):
        self.calls.append((url, body, token))
        if url.startswith("https://oidc.example.test"):
            assert token == "req-secret" and "audience=https%3A%2F%2Ftruecount.dev" in url
            return 200, {"value": "oidc-jwt"}
        if url == f"{API}/api/v1/token":
            assert token == "oidc-jwt" and body == {"agent": "bug_repro"}
            return (200, {"token": "run-token"}) if self.token_status == 200 else \
                (self.token_status, {"error": "stranger has no write access, so bug_repro does not start unasked"})
        if url == f"{API}/api/v1/chat/completions":
            assert token == "run-token"
            return 200, reply(self.model_replies.pop(0))
        if url == f"{API}/api/v1/pulls":
            return (201, {"pull_request": "https://github.com/acme/widgets/pull/13"}) if self.pulls_status == 201 else \
                (self.pulls_status, {"problems": ["src/app.py: outside what bug_repro may change"]})
        raise AssertionError(url)


def steps(answer='{"reproduced": true, "test_path": "tests/test_issue_12.py", "summary": "1 + 1 is not 3"}'):
    return [{"content": None, "tool_calls": [call("write_test", path="tests/test_issue_12.py", content=TEST)]},
            {"content": None, "tool_calls": [call("run_test")]},
            {"content": answer}]


def run(job, server, monkeypatch, *args):
    root, env, _ = job
    monkeypatch.setattr(repro_run, "request", server)
    out = io.StringIO()
    code = repro_run.main(["--test-command", f"{sys.executable} -m pytest -q -p no:cacheprovider {{tests}}", "--api", API, *args],
                          env=env, out=out)
    return code, out.getvalue()


def test_a_reproduced_bug_becomes_a_draft_pull_request_of_the_test(job, monkeypatch):
    server = Server(steps())
    code, out = run(job, server, monkeypatch)
    assert code == 0 and "Draft pull request: https://github.com/acme/widgets/pull/13" in out
    url, body, token = next(c for c in server.calls if c[0].endswith("/pulls"))
    assert token == "run-token" and body["files"] == [{"path": "tests/test_issue_12.py", "content": TEST}]
    assert body["title"] == "Reproduce #12: Adding is wrong" and len(body["base_sha"]) == 40
    assert body["body"].startswith("Reproduces #12. 1 + 1 is not 3") and "Fixes" not in body["body"]
    # pytest echoes the source, so match the error it raised: no credential reached the test
    assert "AssertionError: the bug" in body["body"] and "AssertionError: a credential leaked" not in body["body"]
    summary = Path(job[1]["GITHUB_STEP_SUMMARY"]).read_text()
    assert "Reproduced" in summary and TEST in summary


def test_not_reproduced_posts_nothing_and_says_why(job, monkeypatch):
    server = Server(steps('{"reproduced": false, "test_path": "tests/test_issue_12.py", "summary": "1 + 1 is 2 here"}'))
    code, out = run(job, server, monkeypatch)
    assert code == 0 and "Not reproduced: 1 + 1 is 2 here" in out
    assert not any(c[0].endswith("/pulls") for c in server.calls)


def test_a_claim_without_a_failing_last_run_is_not_reproduced(job, monkeypatch):
    rewritten = steps()[:2] + [{"content": None, "tool_calls": [call("write_test", path="tests/test_issue_12.py", content="def test_x():\n    pass\n")]},
                               steps()[2]]
    code, out = run(job, Server(rewritten), monkeypatch)
    assert code == 0 and "it said so, but the last run of its test did not fail" in out


def test_a_refusal_ends_the_step_without_failing_it(job, monkeypatch):
    code, out = run(job, Server([], token_status=403), monkeypatch)
    assert code == 0 and "Truecount did not start this run: stranger has no write access" in out
    code, out = run(job, Server([], token_status=500), monkeypatch)
    assert code == 1


def test_a_rejected_pull_request_fails_the_step_and_says_why(job, monkeypatch):
    code, out = run(job, Server(steps(), pulls_status=422), monkeypatch)
    assert code == 1 and "outside what bug_repro may change" in out


def test_without_id_token_permission_it_says_what_to_add(job, monkeypatch):
    root, env, _ = job
    env.pop("ACTIONS_ID_TOKEN_REQUEST_URL")
    code, out = run(job, Server([]), monkeypatch)
    assert code == 1 and "permissions: id-token: write" in out


def test_on_dispatch_the_issue_is_read_by_number(job, monkeypatch):
    seen = []

    def fake(url, body=None, token="", timeout=30):
        seen.append((url, token))
        return 200, {"number": 7, "title": "T", "body": "B"}

    monkeypatch.setattr(repro_run, "request", fake)
    env = {**job[1], "GITHUB_EVENT_PATH": ""}
    assert repro_run.read_issue(env, "7") == {"number": 7, "title": "T", "body": "B"}
    assert seen == [("https://api.github.com/repos/acme/widgets/issues/7", "gh-secret")]
    monkeypatch.setattr(repro_run, "request", lambda *a, **k: (200, {"number": 8, "pull_request": {}, "title": "", "body": ""}))
    with pytest.raises(repro_run.Stop, match="is a pull request"):
        repro_run.read_issue(env, "8")


def test_a_failed_model_call_is_retried_once_and_a_refusal_is_not(monkeypatch):
    answers = [(502, {"error": "the model gateway did not answer"}), (200, reply({"content": "{}"}))]
    monkeypatch.setattr(repro_run, "request", lambda *a, **k: answers.pop(0))
    assert repro_run.model(API, "t", sleep=lambda s: None)({"messages": []})["choices"]
    answers = [(429, {"error": "this run has used its tokens"}), (200, reply({"content": "{}"}))]
    with pytest.raises(repro_run.Stop, match="used its tokens"):
        repro_run.model(API, "t", sleep=lambda s: None)({"messages": []})
    assert len(answers) == 1  # not asked again


def test_credentials_and_inputs_never_reach_the_test():
    cleaned = repro_run.clean_env({"PATH": "/bin", "HOME": "/h", "INPUT_X": "1", **SECRETS})
    assert cleaned == {"PATH": "/bin", "HOME": "/h"}


def test_it_runs_as_a_script_beside_its_modules():
    script = Path(repro_run.__file__)
    p = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
    assert p.returncode == 0 and "--test-command" in p.stdout


def test_a_rewrite_clears_the_last_run(tmp_path):
    result = repro.Result(test_path="tests/test_a.py", last_exit=1, last_output="1 failed")
    ws = repro.Checkout(tmp_path)
    assert repro._tool(ws, result, "write_test", {"path": "tests/test_a.py", "content": "x"}, 6) == "wrote tests/test_a.py"
    assert (result.last_exit, result.last_output) == (None, "")
