"""Bug reproduction in the customer's runner: one issue in, at most one draft
pull request out.

  python repro_run.py --test-command "python -m pytest -q {tests}" [--issue N] [--api URL]

1. The issue comes from the event payload, or `--issue N` on workflow_dispatch,
   read with the job's token.
2. A run token: the job's OIDC token, exchanged at Truecount's /api/v1/token.
   A refusal ends the step without failing it and says why. The reasons are:
   whoever opened the issue has no write access, the plan, or the month's runs
   are used up.
3. The loop (repro.reproduce) talks to /api/v1/chat/completions, where the
   server picks the model. A call that fails is retried once.
4. If the test fails because of the bug, the test file is sent to
   /api/v1/pulls and Truecount opens it as a draft pull request. Otherwise the
   job summary says why, and nothing is posted.

The test is code a model wrote, steered by an issue's text, so it runs
without the job's credentials in its environment: the OIDC request token, the
Actions runtime token and the GitHub token are removed first.

Standard library only, like the rest of the Action.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from . import repro
except ImportError:  # run as a script from the Action
    import repro  # type: ignore[no-redef]

AGENT = "bug_repro"
AUDIENCE = "https://truecount.dev"
EXCHANGE_SECONDS = 60
CALL_SECONDS = 150  # longer than Cloudflare's 100-second cut, so the cut is what ends a stuck call
OUTPUT_IN_BODY = 4000
SECRET_ENV = ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_RUNTIME_TOKEN",
              "ACTIONS_RUNTIME_URL", "ACTIONS_RESULTS_URL", "ACTIONS_CACHE_URL", "GITHUB_TOKEN", "GH_TOKEN")


class Stop(Exception):
    """Ends the step and says why. `failed` decides the exit code."""

    def __init__(self, message: str, failed: bool = False):
        super().__init__(message)
        self.failed = failed


def request(url: str, body: dict | None = None, token: str = "", timeout: int = 30) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "truecount-action")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except ValueError:
            return err.code, {"error": f"HTTP {err.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        # Status 0: no answer at all. Callers say so; none of them crash on it.
        reason = getattr(err, "reason", err)
        return 0, {"error": f"no answer from {urllib.parse.urlsplit(url).netloc}: {reason}"}


def read_issue(env: dict, issue_arg: str) -> dict:
    """The issue's number, title and body."""
    event = {}
    if env.get("GITHUB_EVENT_PATH") and Path(env["GITHUB_EVENT_PATH"]).is_file():
        event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
    issue = event.get("issue")
    if issue_arg and (not issue or str(issue.get("number")) != issue_arg):
        api = env.get("GITHUB_API_URL", "https://api.github.com")
        status, issue = request(f"{api}/repos/{env.get('GITHUB_REPOSITORY', '')}/issues/{int(issue_arg)}",
                                token=env.get("TC_GITHUB_TOKEN", ""))
        if status != 200:
            raise Stop(f"could not read issue #{issue_arg}: {issue.get('message') or status}", failed=True)
    if not issue:
        raise Stop("no issue to reproduce: run this on `issues` events, or pass `issue` on workflow_dispatch", failed=True)
    if "pull_request" in issue:  # the issues API lists pull requests too
        raise Stop(f"#{issue['number']} is a pull request, not an issue")
    return {"number": int(issue["number"]), "title": str(issue.get("title") or ""), "body": str(issue.get("body") or "")}


def oidc_token(env: dict) -> str:
    url, secret = env.get("ACTIONS_ID_TOKEN_REQUEST_URL"), env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not secret:
        raise Stop("no OIDC token: the workflow needs `permissions: id-token: write`", failed=True)
    status, data = request(f"{url}&audience={urllib.parse.quote(AUDIENCE, safe='')}", token=secret)
    if status != 200 or not data.get("value"):
        raise Stop(f"GitHub did not issue an OIDC token ({status})", failed=True)
    return data["value"]


def exchange(api: str, oidc: str) -> str:
    # Longer than other calls: a server process's first exchange fetches GitHub's signing keys.
    status, data = request(f"{api}/api/v1/token", {"agent": AGENT}, token=oidc, timeout=EXCHANGE_SECONDS)
    if status == 200 and data.get("token"):
        return data["token"]
    reason = data.get("error") or data.get("reason") or f"HTTP {status}"
    if status == 0:
        raise Stop(f"Truecount did not answer: {reason}", failed=True)
    # Refused by rule (who asked, the plan, the month's runs): not a failure of the job.
    raise Stop(f"Truecount did not start this run: {reason}", failed=status >= 500 or status in (400, 401))


def model(api: str, token: str, sleep=time.sleep):
    """The chat callable for repro.reproduce: the server's proxy, retried once."""

    def chat(body: dict) -> dict:
        last = ""
        for attempt in (1, 2):
            try:
                status, data = request(f"{api}/api/v1/chat/completions", body, token=token, timeout=CALL_SECONDS)
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                status, data = 0, {"error": str(err)}
            if status == 200 and data.get("choices"):
                return data
            last = data.get("error") or f"HTTP {status}"
            if 400 <= status < 500 and status != 408:
                break  # the budget, the prompt size, the token: asking again changes nothing
            if attempt == 1:
                sleep(5)
        raise Stop(f"the model did not answer: {last}", failed=True)

    return chat


def clean_env(env: dict) -> dict:
    """The job's environment without its credentials or this step's inputs."""
    return {k: v for k, v in env.items() if k not in SECRET_ENV and not k.startswith(("TC_", "INPUT_"))}


def reproduced(result) -> bool:
    """The agent says so, and the last run of the file it wrote failed."""
    return bool(result.reproduced and result.test_path and result.last_exit not in (None, 0))


def pull_request(issue: dict, result, test_command: str, base_sha: str) -> dict:
    title = f"Reproduce #{issue['number']}: {issue['title']}"
    if len(title) > 200:
        title = title[:199] + "…"
    output = result.last_output[-OUTPUT_IN_BODY:].strip()
    body = "\n".join([
        f"Reproduces #{issue['number']}. {result.summary}".strip(),
        "",
        f"This adds one test, `{result.test_path}`, that fails until the bug is fixed. It is a draft so it is not "
        "merged before the fix; commit the fix to this branch, or take the test into yours.",
        "",
        f"`{test_command.replace('{tests}', result.test_path)}` fails now with:",
        "",
        "````",
        output,
        "````",
        "",
        "Written by Truecount's bug reproduction agent from the issue's title and body and the code.",
    ])
    return {"base_sha": base_sha, "title": title, "body": body,
            "files": [{"path": result.test_path, "content": result.test}]}


def summary_lines(issue: dict, result, url: str = "") -> list[str]:
    lines = [f"## Truecount bug reproduction: #{issue['number']}", ""]
    if url:
        lines += [f"Reproduced: {result.summary}", "", f"Draft pull request: {url}"]
    else:
        why = result.summary or {"out_of_calls": "it ran out of tool calls", "no_answer": "it gave no answer"}.get(
            result.stopped, "it did not reproduce the bug")
        if result.reproduced and not reproduced(result):
            why = "it said so, but the last run of its test did not fail"
        lines += [f"Not reproduced: {why}"]
    lines += ["", f"{result.calls} tool calls, {result.runs} test runs."]
    if result.test_path:
        lines += ["", f"`{result.test_path}`:", "", "````python", result.test, "````"]
    return lines


def main(argv: list[str] | None = None, env: dict | None = None, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--test-command", required=True, help="runs one test file; {tests} is where its path goes")
    parser.add_argument("--issue", default="", help="the issue number, on workflow_dispatch")
    parser.add_argument("--api", default="https://api.truecount.dev")
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    if "{tests}" not in args.test_command:
        print("test-command needs {tests} where the test file's path goes", file=out)
        return 1
    root = Path(env.get("GITHUB_WORKSPACE") or ".")
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    try:
        issue = read_issue(env, args.issue)
        token = exchange(args.api.rstrip("/"), oidc_token(env))
        workspace = repro.Checkout(root, args.test_command, env=clean_env(env))
        result = repro.reproduce(model(args.api.rstrip("/"), token), workspace, issue["title"], issue["body"])
        url = ""
        if reproduced(result):
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
            status, data = request(f"{args.api.rstrip('/')}/api/v1/pulls",
                                   pull_request(issue, result, args.test_command, base), token=token, timeout=60)
            if status != 201:
                problems = "; ".join(data.get("problems") or []) or data.get("error") or f"HTTP {status}"
                raise Stop(f"reproduced, but Truecount did not open the pull request: {problems}", failed=True)
            url = data["pull_request"]
        lines = summary_lines(issue, result, url)
    except Stop as stop:
        print(str(stop), file=out)
        if summary_path:
            with open(summary_path, "a") as f:
                f.write(f"## Truecount bug reproduction\n\n{stop}\n")
        return 1 if stop.failed else 0
    print("\n".join(lines), file=out)
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
