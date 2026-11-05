"""The steps around each Truecount agent in the customer's runner, for the
reusable workflow (.github/workflows/agents.yml).

  runner.py plan             which agents this event runs, with their definitions, as a job output
  runner.py token AGENT      a run token for one agent, masked, as a step output
  runner.py standard         the review's guidelines, written under .truecount/standard/, and the prompt naming them
  runner.py post FILE        the agent's output file, posted by Truecount as the App

The agents themselves run with Claude Code (anthropics/claude-code-action),
exactly as the fleet runs them. Truecount holds their definitions, so the plan
step asks for them, and its model proxy is where Claude Code's requests go,
with the run token as the key. The runner never holds a token that can write
to the repository: posting is Truecount's.

Standard library only, like the rest of the Action.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    from . import repro_run
except ImportError:  # run as a script from the workflow
    import repro_run  # type: ignore[no-redef]


def output(name: str, value: str, env: dict) -> None:
    path = env.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            delimiter = "TRUECOUNT_EOF"
            f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def plan(api: str, env: dict, out) -> int:
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text()) if env.get("GITHUB_EVENT_PATH") else {}
    pr = event.get("pull_request") or {}
    body = {"action": event.get("action"), "pull_request": {
        "number": pr.get("number"), "draft": bool(pr.get("draft")),
        "base": {"ref": (pr.get("base") or {}).get("ref"), "sha": (pr.get("base") or {}).get("sha")}},
        "features": ["standard"]}  # this runner fills {standard} itself
    try:
        oidc = repro_run.oidc_token(env)
    except repro_run.Stop as stop:
        print(str(stop), file=out)
        return 1
    status, data = repro_run.request(f"{api}/api/v1/plan", body, token=oidc, timeout=60)
    if status != 200:
        reason = data.get("error") or data.get("reason") or f"HTTP {status}"
        print(f"Truecount planned nothing: {reason}", file=out)
        output("agents", "[]", env)
        # Not installed, the plan, the closed beta: not a failure of the customer's job.
        return 1 if status == 0 or status >= 500 else 0
    agents = data.get("agents") or []
    names = ", ".join(a["name"] for a in agents) or "none"
    print(f"Truecount agents for this event: {names}" + (f" ({data['reason']})" if data.get("reason") else ""), file=out)
    output("agents", json.dumps(agents), env)
    return 0


def token(api: str, agent: str, env: dict, out) -> int:
    try:
        oidc = repro_run.oidc_token(env)
    except repro_run.Stop as stop:
        print(str(stop), file=out)
        return 1
    status, data = repro_run.request(f"{api}/api/v1/token", {"agent": agent}, token=oidc, timeout=repro_run.EXCHANGE_SECONDS)
    if status != 200 or not data.get("token"):
        print(f"Truecount did not start {agent}: {data.get('error') or data.get('reason') or f'HTTP {status}'}", file=out)
        return 1
    print(f"::add-mask::{data['token']}", file=out)  # the run token is the model key for this run: never in a log
    output("token", data["token"], env)
    return 0


STANDARD_DIR = ".truecount/standard/"


def standard(api: str, env: dict, out) -> int:
    """Write what the review is held to, as Truecount's server read it from the
    pull request's base commit, and output the agent's prompt with the paragraph
    that names those files. The checkout holds the pull request's own version of
    every file, so its guidelines are not the ones that count."""
    status, data = repro_run.request(f"{api}/api/v1/standard", {}, token=env.get("TC_TOKEN", ""), timeout=60)
    if status != 200 or not isinstance(data.get("section"), str):
        print(f"::error::Truecount did not send the review's guidelines: {data.get('error') or f'HTTP {status}'}", file=out)
        return 1
    workspace = Path(env.get("GITHUB_WORKSPACE") or ".").resolve()
    for f in data.get("files") or []:
        target = (workspace / f["path"]).resolve()
        if not f["path"].startswith(STANDARD_DIR) or not target.is_relative_to(workspace / STANDARD_DIR):
            print(f"::error::Truecount sent a file outside {STANDARD_DIR}: {f['path']}", file=out)
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["text"])
        print(f"Wrote {f['path']}", file=out)
    output("prompt", env.get("TC_PROMPT", "").replace("{standard}", data["section"]), env)
    return 0


def post(api: str, file: str, env: dict, out) -> int:
    path = Path(env.get("GITHUB_WORKSPACE") or ".") / file
    if not path.is_file() or not path.read_text().strip():
        # The fleet's rule: a review that was never posted is worth nothing, and must not look like a clean run.
        print(f"::error::the agent wrote no {file}, so there is nothing to post", file=out)
        return 1
    status, data = repro_run.request(f"{api}/api/v1/comment", {"body": path.read_text()}, token=env.get("TC_TOKEN", ""),
                                     timeout=60)
    if status != 200:
        print(f"::error::Truecount did not post {file}: {data.get('error') or data.get('problems') or f'HTTP {status}'}",
              file=out)
        return 1
    print(f"Posted: {data.get('comment')}", file=out)
    return 0


def main(argv: list[str] | None = None, env: dict | None = None, out=sys.stdout) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = dict(os.environ if env is None else env)
    api = (env.get("TC_API") or "https://api.truecount.dev").rstrip("/")
    if argv[:1] == ["plan"]:
        return plan(api, env, out)
    if argv[:1] == ["token"] and len(argv) == 2:
        return token(api, argv[1], env, out)
    if argv == ["standard"]:
        return standard(api, env, out)
    if argv[:1] == ["post"] and len(argv) == 2:
        return post(api, argv[1], env, out)
    print("usage: runner.py plan | token AGENT | standard | post FILE", file=out)
    return 2


if __name__ == "__main__":
    sys.exit(main())
