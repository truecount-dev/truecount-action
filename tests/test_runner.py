"""The steps around each agent in the reusable workflow: plan, token, post."""

import io
import json

from truecount_action import repro_run, runner

API = "https://api.example.test"
OIDC = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example.test/?x=1", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req"}


def env(tmp_path, **extra):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "opened", "pull_request": {
        "number": 7, "draft": False, "title": "secret title", "base": {"ref": "main", "sha": "b" * 40}}}))
    return {"GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(tmp_path / "out"), "GITHUB_WORKSPACE": str(tmp_path),
            "TC_API": API, **OIDC, **extra}


def outputs(tmp_path) -> str:
    return (tmp_path / "out").read_text() if (tmp_path / "out").exists() else ""


def fake(answers, seen):
    def request(url, body=None, token="", timeout=30):
        seen.append((url, body, token))
        if url.startswith("https://oidc.example.test"):
            return 200, {"value": "oidc-jwt"}
        return answers[url]
    return request


def test_the_plan_sends_only_what_it_needs_and_outputs_the_agents(tmp_path, monkeypatch):
    seen = []
    agents = [{"name": "review", "prompt": "Review #7", "claude_args": "--max-turns 60", "output": "review.md"}]
    monkeypatch.setattr(repro_run, "request", fake({f"{API}/api/v1/plan": (200, {"agents": agents})}, seen))
    out = io.StringIO()
    assert runner.main(["plan"], env=env(tmp_path), out=out) == 0
    url, body, token = seen[-1]
    assert token == "oidc-jwt" and body == {"action": "opened", "pull_request": {
        "number": 7, "draft": False, "base": {"ref": "main", "sha": "b" * 40}}}  # no title or body leaves the runner
    assert json.dumps(agents) in outputs(tmp_path) and "agents for this event: review" in out.getvalue()


def test_a_refused_plan_runs_nothing_and_fails_only_when_truecount_is_down(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(repro_run, "request", fake({f"{API}/api/v1/plan": (403, {"error": "closed beta"})}, seen))
    assert runner.main(["plan"], env=env(tmp_path), out=io.StringIO()) == 0 and "agents<<" in outputs(tmp_path)
    monkeypatch.setattr(repro_run, "request", fake({f"{API}/api/v1/plan": (0, {"error": "no answer"})}, seen))
    assert runner.main(["plan"], env=env(tmp_path), out=io.StringIO()) == 1


def test_the_run_token_is_masked_before_it_is_an_output(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(repro_run, "request", fake({f"{API}/api/v1/token": (200, {"token": "run-tok"})}, seen))
    out = io.StringIO()
    assert runner.main(["token", "review"], env=env(tmp_path), out=out) == 0
    assert out.getvalue().startswith("::add-mask::run-tok") and "run-tok" in outputs(tmp_path)
    assert seen[-1][1] == {"agent": "review"}


def test_post_sends_the_file_and_fails_loudly_when_the_agent_wrote_nothing(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(repro_run, "request", fake({f"{API}/api/v1/comment": (200, {"comment": "https://c"})}, seen))
    out = io.StringIO()
    assert runner.main(["post", "review.md"], env=env(tmp_path, TC_TOKEN="run-tok"), out=out) == 1
    assert "wrote no review.md" in out.getvalue() and not seen
    (tmp_path / "review.md").write_text("Findings: none.\n\n**What I checked** ...")
    assert runner.main(["post", "review.md"], env=env(tmp_path, TC_TOKEN="run-tok"), out=io.StringIO()) == 0
    assert seen[-1] == (f"{API}/api/v1/comment", {"body": "Findings: none.\n\n**What I checked** ..."}, "run-tok")


def test_a_failed_agent_says_why_without_its_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(repro_run, "request", fake({}, []))
    (tmp_path / "claude-execution-output.json").write_text(json.dumps([
        {"type": "assistant", "message": {"content": "secret code it read"}},
        {"type": "result", "is_error": True, "result": "API Error: 400 unknown field context_management"}]))
    out = io.StringIO()
    assert runner.main(["post", "review.md"], env=env(tmp_path, RUNNER_TEMP=str(tmp_path)), out=out) == 1
    assert "It stopped with: API Error: 400 unknown field context_management" in out.getvalue()
    assert "secret code" not in out.getvalue()
