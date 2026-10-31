# Truecount Action

The parts of [Truecount](https://truecount.dev) that run in **your** GitHub
Actions runner. Your code only executes here, never on Truecount's servers.

Today that is the mutation check.

## Mutation check

For each line a pull request changes, the check makes one small deliberate
break: a flipped comparison, an off-by-one boundary, an `if` whose condition
never holds, a removed `raise`, a `return None`. In SQL written as Python
strings it swaps `ASC` and `DESC`, moves a boundary, or drops one `AND`
condition. It runs your tests once per break and reports the ones they missed.

```
The tests caught 62 of 80 deliberate breaks in the changed lines.

Not caught
- truecount/admin.py:54       not rows → rows                 (remove not)
- truecount/retention.py:75   conn.execute('ROLLBACK') → pass (remove call)
```

A break the tests miss marks behaviour no test pins down. Some breaks change
nothing observable, so read a survivor as a lead, not a verdict.

It reads **Python, JavaScript, TypeScript, Go and Ruby**. No model is
involved. Python needs only the standard library. For the other languages the
Action installs a parser, tree-sitter, from pinned and hash-checked wheels into
a separate environment in the runner's temp folder, and only when the change
touches one of them, so nothing is added to your environment. A break that
stops the code compiling is reported as unbuildable, never as caught: every
break is parsed again, and Go runs `go build ./...` before its tests. If the
parser cannot install, those files are listed as skipped and the rest is
checked as before.

If some tests fail before anything is broken (stale with the calendar, say),
the check sets them aside and checks the rest: pytest leaves them out through
`PYTEST_ADDOPTS`, and `go test` skips them through `GOFLAGS`, however your
test command is written. The report lists them under **Set aside**. A break
that only they could catch reads as not caught.

It stops instead when:
- another runner is failing
- a failure is not a named test (a collection error, a coverage threshold)
- more than ten tests fail

It stops because it cannot tell a break from what was already failing. The
report names the failing tests. The end of their output goes to the job log,
where GitHub masks your secrets, and never into the report, which is posted
where nothing is masked.

### Use it

Save this as **`.github/workflows/truecount-mutation.yml`**. The Truecount App
looks for exactly that file name, to post the result on the pull request.

```yaml
name: Truecount mutation check
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review, labeled]
permissions:
  contents: read
jobs:
  mutation:
    # Not on drafts. Of all the labels, only `truecount: all tests` starts a run.
    if: >-
      github.event.pull_request.draft == false &&
      (github.event.action != 'labeled' || github.event.label.name == 'truecount: all tests')
    # A new push cancels the run it replaces, so you pay for the latest only.
    concurrency:
      group: truecount-mutation-${{ github.event.pull_request.number }}
      cancel-in-progress: true
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0          # the check diffs against the base commit
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt    # however your project installs
      - uses: truecount-dev/truecount-action@v0
        with:
          test-command: python -m pytest -x -q
          related-test-command: python -m pytest -x -q {tests}
          # Quoted: the label's ": " would otherwise end the YAML value early.
          all-tests: "${{ contains(github.event.pull_request.labels.*.name, 'truecount: all tests') }}"
```

It runs when a pull request is opened, pushed to, reopened or marked ready for
review, and never on a draft. Each break runs only the tests named like the file
it broke (`billing.py` → `test_billing.py`, `billing.test.js`,
`billing_test.go`'s package), and the whole suite when no test is named like it.
The baseline, run once before any break, is always the whole suite.

A test elsewhere might catch a break its related tests miss, so the report marks
those survivors **related tests only**. To run every test for every break, add
the label `truecount: all tests` to the pull request. It starts a run at once,
and every push runs in full while the label stays on. Create the label once:

```bash
gh label create "truecount: all tests" --color 5319e7 --description "The mutation check runs the whole test suite for every break, not only the related tests"
```

`related-test-command` is your own test command with `{tests}` where the test
files go, or `{packages}` for runners that take packages:

| Runner | `related-test-command` |
|---|---|
| pytest | `python -m pytest -x -q {tests}` |
| Jest | `npx jest --bail {tests}` |
| Vitest | `npx vitest run --bail 1 {tests}` |
| node --test | `node --test {tests}` |
| RSpec | `bundle exec rspec --fail-fast {tests}` |
| go test | `go test -failfast {packages}` |

Leave it out and every break runs `test-command`, the whole suite.

The summary appears on the workflow run's page. With the Truecount App
installed, it is also posted on the pull request, which works for pull
requests from forks too: the workflow needs no secrets and no token.

| Input | Default | |
|---|---|---|
| `test-command` | required | Exits non-zero when a test fails. `-x` makes each run stop at the first failure, which makes the whole check much faster. |
| `base` | the pull request's base commit | What the change is measured against |
| `related-test-command` | blank | Each break runs only the tests named like its file: `{tests}` or `{packages}` marks where they go |
| `all-tests` | `false` | `true` runs the whole suite for every break; set from the label above |
| `max-mutants` | `40` | Each break runs the test command once |
| `jobs` | `1` | Breaks to run at once, each in its own full copy of the checkout |
| `python` | `python3` | Any 3.10+ interpreter |

### Browser tests (Playwright, Selenium)

Any test command works, browser suites included, with two things to know.

**The app under test must be started from the checkout on every run.** Each
break edits a source file, and only an app started after the edit contains it.
Playwright's `webServer` does this, starting the app for each `npx playwright
test`, as long as `reuseExistingServer` is off. A Selenium suite needs a fixture
that starts the app the same way. A suite that tests a server already running,
or a bundle built before the check, never sees a break. So before any break,
the check breaks every changed file at once and runs the tests: if they still
pass, it stops and says so rather than report every break as not caught.

**Browser suites are slow, and each break runs the tests again.** End-to-end
specs are named after features (`checkout.spec.ts`), not source files
(`cart.ts`), so `related-test-command` seldom narrows them and most breaks run
the whole suite. Point `test-command` at your unit tests and keep the browser
suite in its own workflow, or narrow it with Playwright's `--project` or
`--grep`, and keep `jobs` at 1 if `webServer` uses a fixed port.

The check breaks Python, JavaScript, TypeScript, Go and Ruby, so a Selenium
suite in Java has nothing here to break.

### Run it on your own machine

The check runs your tests once per break, on GitHub's hosted runners unless you
say otherwise. On a public repository those minutes are free; on a private one
they count against your plan, and the hosted machines are small. You can run it
on a machine of your own instead, as a self-hosted runner:

1. Use a virtual machine or container that does nothing else: not a laptop, and
   nothing that holds production credentials or can reach production systems.
   Pull requests run their own code on it.
2. In the repository's settings, open **Actions → Runners → New self-hosted
   runner**, follow GitHub's commands, and add `--ephemeral` to `config.sh` so
   every job starts from a clean runner. Give it a label, such as `truecount`.
3. In the workflow, replace `runs-on: ubuntu-latest` with
   `runs-on: [self-hosted, truecount]`, and set `jobs` to about half the
   machine's cores.

**Private repositories only.** On a public repository anyone can open a pull
request from a fork, and its code would run on your machine. GitHub warns
against self-hosted runners on public repositories for this reason. Their
hosted runners cost nothing there anyway.

### What it does to your checkout

It edits one file at a time, runs the tests, and restores the file. Each
original is backed up to `.truecount-mutate-backup/` first, so if a run is
killed mid-break, the next run puts the file back before doing anything else.
It refuses to run if your tests already fail without any break and the failing tests cannot be set aside,
or if they still pass with every changed file broken, which means they never run the changed code.

When the change is larger than `max-mutants`, the breaks are spread evenly
from its first changed line to its last.

With `jobs` above 1, it copies the whole checkout once per job, into the
runner's temp folder, and breaks the copies instead. Your checkout is not
edited at all. Each copy is checked first: every changed file is broken
beyond parsing and the tests run once. If they still pass, the copy is running
your original code (an editable install points there), so the breaks run one
at a time in your checkout instead, and the job log says so. Leave `jobs` at 1
if your tests share a fixed port, a fixed temp path or an outside database: two
copies running at once would collide.

## Bug reproduction (not released)

`src/truecount_action/repro.py` turns an issue into one new test file that
fails because of the reported bug and will pass once it is fixed. It reads
the code, writes the test, runs it (at most 6 runs in 20 tool calls) and
stops when the failure is the bug, not an import error or a typo. It asserts
the correct behaviour, never the buggy one, so the test cannot lock the bug
in.

It is not wired to a workflow trigger yet. Truecount scores it against real
issues from public projects first, running this same loop in a container:
does its test fail before the maintainers' fix and pass after it?

## License

MIT
