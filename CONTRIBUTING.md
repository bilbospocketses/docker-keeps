# Contributing

Thanks for helping. This is a small, solo-maintained project, so the process is short.

## Before you open a pull request

1. **Run the suite.** `python tests/run_tests.py` must end with `N/N checks passed` and exit 0.
   It needs `git` and Python 3.10 or later, and nothing else. A case that checks nothing, or
   raises, is a failure.
2. **Run the checker on itself.** `python docker_keeps.py check --repo . --ref HEAD` must print
   `docker-keeps: docker-keeps: 0 finding(s)` once your change is committed. Test data lives
   under `tests/`, which this repository's `docker-images.json` ignores.
3. **Write in American spelling.** CI runs the central gate
   ([bilbospocketses/american-spelling](https://github.com/bilbospocketses/american-spelling))
   on your pull request's added lines and commit messages.

## Changing what the scanner reads

- Add a case to `tests/run_tests.py` for every shape the scanner learns to read, and one for
  the nearest shape it must still leave alone (prose, a comment, a stage name).
- Keep the checker one file of standard-library Python, with no install step.
- Update the README's "What it scans" section in the same pull request.

## Pull requests

- Branch from `main`, keep each pull request to one concern, and add a line under
  `## [Unreleased]` in `CHANGELOG.md`.
- Commits must be signed; `main` only accepts signed commits through a pull request.
- Pull requests are squash-merged once CI is green.
