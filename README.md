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

Python only for now. No model is involved, and the check uses only the
standard library, so it adds nothing to your environment.

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
| `python` | `python3` | Any 3.10+ interpreter |

### What it does to your checkout

It edits one file at a time, runs the tests, and restores the file. Each
original is backed up to `.truecount-mutate-backup/` first, so if a run is
killed mid-break, the next run puts the file back before doing anything else.
It refuses to run if your tests already fail without any break.

## License

MIT
