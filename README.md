# Truecount Action

The parts of [Truecount](https://truecount.dev) that run in **your** GitHub
Actions runner. Your code only executes here, never on Truecount's servers.

Today that is the mutation check.

## Mutation check

For each line a pull request changes, the check makes one small deliberate
break: a flipped comparison, an off-by-one boundary, a removed `raise`, a
`return None`. It runs your tests once per break and reports the ones they
missed.

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
on: pull_request
permissions:
  contents: read
jobs:
  mutation:
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
```

The summary appears on the workflow run's page. With the Truecount App
installed, it is also posted on the pull request, which works for pull
requests from forks too: the workflow needs no secrets and no token.

| Input | Default | |
|---|---|---|
| `test-command` | required | Exits non-zero when a test fails. `-x` makes each run stop at the first failure, which makes the whole check much faster. |
| `base` | the pull request's base commit | What the change is measured against |
| `max-mutants` | `40` | Each break runs the test command once |
| `jobs` | `1` | Breaks to run at once, each in its own full copy of the checkout |
| `python` | `python3` | Any 3.10+ interpreter |

### What it does to your checkout

It edits one file at a time, runs the tests, and restores the file. Each
original is backed up to `.truecount-mutate-backup/` first, so if a run is
killed mid-break, the next run puts the file back before doing anything else.
It refuses to run if your tests already fail without any break and the failing tests cannot be set aside.

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

## License

MIT
