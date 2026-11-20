# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0]

### Added

- `docker_keeps.py`, one stdlib-only Python 3 file that reads a repository's keep file,
  `docker-images.json` (schema 1: `pin` and `build` entries, each with a purpose, and
  `ignore` path globs), and a machine-tooling Markdown table of images kept for a machine
  rather than for one repository.
- A scanner for the image references a repository's tracked files name: Dockerfile `FROM`,
  compose and config `image` keys, `docker pull`, `run`, `create` and `build -t` commands,
  any digest-pinned reference, variables with exactly one literal, and `docker-keep:` markers.
  `FROM scratch` and build-stage names are no image.
- `check`: the findings of one repository at its main or master branch, or at `--ref`:
  `no-keep-file`, `undeclared`, `unresolved`, `lapsed`, `malformed` and `unreadable`,
  printed one per line, then a count. Exit 0 with no finding, 1 with findings, 2 when it
  cannot run.
- Matching of box images to keep entries (`claims`, `claim_of`, `kept_by`), with the
  precedence machine tooling, pin, build, then list order, and a `+N` count of other sources.
- CI: the suite on Ubuntu and Windows, the central American spelling gate, and the checker
  on this repository itself, aggregated as `build-and-test`.
