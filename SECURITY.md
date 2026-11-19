# Security Policy

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Report them privately through GitHub's security advisory flow:

**[Report a vulnerability](https://github.com/bilbospocketses/docker-keeps/security/advisories/new)**

This opens a private channel with the maintainer. Nothing is disclosed publicly until a fix is
ready.

## What to Include

- A description of the issue and its impact
- Steps to reproduce: the repository state, the command line, and the output
- The affected version (tag or commit)

## Response Expectations

- **Acknowledgment:** within 72 hours
- **Triage and initial assessment:** within one week
- **Fix and disclosure timeline:** agreed with the reporter, depending on severity

## Supported Versions

Fixes land on `main` and ship in a new tag. Only the latest tag is supported; consumers
upgrade by bumping the commit they pin.

## Scope

In scope:

- anything that makes the checker run code, write files or change the state of the repository
  it checks (it only reads the tracked files of one commit through `git`);
- a crafted file, path or keep file that makes the checker hang, crash or exit 0 when it
  should report a finding or exit 2.

Out of scope: an image reference written in a shape the scanner does not read (open an issue
or a pull request instead; the README lists the shapes it reads), and issues that need write
access to the consumer's CI configuration.
