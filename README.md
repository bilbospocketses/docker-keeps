# docker-keeps

[![CI](https://github.com/bilbospocketses/docker-keeps/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/bilbospocketses/docker-keeps/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/bilbospocketses/docker-keeps/badge)](https://scorecard.dev/viewer/?uri=github.com/bilbospocketses/docker-keeps)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A checker that makes a repository declare the Docker images its code names, and say why it
keeps each one.

- **One stdlib-only Python 3 file** (3.10 or later), no install step. It needs `git` on the
  `PATH`.
- **It reads one commit, never the work tree.** By default that is the repository's `main` or
  `master` branch; in CI, `--ref HEAD` reads the commit under test.
- **Every image has a purpose.** A repository lists its images in `docker-images.json` at its
  root. An image its code names that the file does not declare is a finding, and so is a
  declared image nothing names any more.

## The keep file

`docker-images.json`, at the repository's root:

```json
{
  "schema": 1,
  "images": [
    {"ref": "alpine:3.20@sha256:<64 hex>", "kind": "pin", "purpose": "base of the app image"},
    {"ref": "redis:7", "kind": "pin", "purpose": "cache in the integration tests"},
    {"ref": "acme/app", "kind": "build", "purpose": "the image this repository ships",
     "dockerfile": "images/app/Dockerfile"}
  ],
  "ignore": ["tests/fixtures/**"]
}
```

- **`schema`** is the integer `1`. Any other value is a `malformed` finding.
- **`images`** is a list (it may be empty). Each entry has:
  - `ref`: an image reference (see [References](#references)). `*` is refused here: it belongs
    to the machine-tooling table only.
  - `kind`: `pin`, an image pulled as written, or `build`, an image this repository builds.
    A `build` ref names a repository or `repository:tag`, never a digest.
  - `purpose`: why the repository keeps it. It must not be empty.
  - `dockerfile` (optional): the path, from the repository's root, of the Dockerfile that
    builds it. A `build` entry whose `dockerfile` is a tracked file is never `lapsed`.
- **`ignore`** (optional) is a list of path globs. A file whose path matches one is not
  scanned. See [Ignore globs](#ignore-globs) for exactly how they match.
- A digest of 64 zeros (`sha256:000...0`) is allowed as a placeholder. It claims no image on
  a machine; like any entry, it is `lapsed` until the code names that exact ref.

### The machine-tooling table

Images kept for a machine itself, not for any one repository, are rows of a Markdown table
between two marker lines, in any Markdown file passed with `--tooling-file`:

```markdown
<!-- docker-images:start -->
| Image | Purpose |
|---|---|
| `ghcr.io/acme/tool:*` | every tag of the release tool |
| tool-x:5.1.0 | a converter |
<!-- docker-images:end -->
```

The header must be `| Image | Purpose |`, and every row needs a purpose. A ref may be written
in backticks. Only here, `*` may stand for any text in the repository or the tag
(`ghcr.io/acme/*:2`, `node:*`). A ref the table declares never needs a keep file entry.

## References

A ref follows the Docker reference grammar: a repository, an optional `:tag` and an optional
`@sha256:<64 hex>` digest. The whole text must be one reference.

- The repository is lowercase `/`-separated components. Each is alphanumeric runs joined by
  `.`, `_`, `__` or one or more `-`.
- A `:port` belongs only to the first component, and only when a `/` follows it, so
  `localhost:5000/app:1` is repository `localhost:5000/app`, tag `1`, while `node:22` is
  repository `node`, tag `22`.
- A tag is up to 128 characters: a letter, digit or `_`, then letters, digits, `.`, `_` and
  `-`.
- A digest is `sha256:` and 64 lowercase hex digits; a shorter one makes the text no ref.
- `docker.io/` and `index.docker.io/` are stripped, and then `library/`, so `alpine`,
  `library/alpine` and `docker.io/library/alpine` are one repository. Every other host is
  kept as written: `mirror.gcr.io/library/alpine` is a different repository.
- A ref with neither tag nor digest means the tag `latest`.

## Matching

### Code against declarations (what `check` uses)

An entry declares a ref the code names when:

| Entry | Declares |
|---|---|
| has a digest | the same repository and digest; the tags are not compared |
| `pin` without a digest | the same repository and tag |
| `build` without a tag | every tag of its repository |
| `build` with a tag | that tag of its repository |
| table row | the same repository and tag, where `*` matches any text |

An entry without a digest never declares a ref that pins one: a pull by digest leaves the
image untagged, so the tag would not keep it. A ref that pins a digest the entry lacks, or
drops a digest the entry has, is `undeclared`.

### Images on a box against declarations (the module API)

`claims(entry, image)`, `claim_of(image, entries)` and `kept_by(image, entries)` match an
image on a machine, given as `{"refs": [RepoTags...], "repo": "<repository>", "digests":
[RepoDigests...]}`, to the entries that keep it:

- an entry with a digest claims an image whose RepoDigests hold the same repository and
  digest (the tag is ignored; the zero placeholder claims nothing);
- a `pin` without a digest, or a table row, claims an image that carries its tag;
- a `build` without a tag claims every tag of its repository, and an untagged image whose
  repository is its own; a `build` with a tag claims an image that carries that tag.

When several entries claim one image, the one shown is, in order: a machine-tooling row,
then a `pin`, then a `build`, then the first in list order. `kept_by` returns
`machine tooling`, `<source> pin`, `<source> build`, or `-` for none, followed by ` +N` when
N other sources (other repositories, or the table) also claim it.

## What it scans

`check` reads the files tracked at the commit, never the work tree. It leaves out:

- binary files (any NUL byte) and files that are not UTF-8;
- every file under a directory named `docs` or `.superpowers`, at any depth;
- every `*.md` file (any case), every file whose name starts with `CHANGELOG`, and the root
  `docker-images.json` itself (one in a subdirectory is scanned like any JSON file);
- every file an `ignore` glob matches.

In the files it reads, a reference is any of these:

| Where | What |
|---|---|
| `Dockerfile`, `Dockerfile.*`, `*.Dockerfile`, `*.dockerfile` | the image of each `FROM` line (any case; `--platform=...` and other options skipped) |
| `*compose*.yml`, `*compose*.yaml` | the value of each `image:` key |
| any other `*.yml` / `*.yaml` | the value of any key ending in `image` (any case): `image:`, `baseImage:` |
| `*.psd1` | a `...Image = '<value>'` line |
| `*.json` | the string value of any `"...image"` or `"...Image"` key |
| any file | the image of `docker pull`, `docker image pull`, `docker run` and `docker create`, and each `-t` / `--tag` of `docker build` and `docker buildx build` |
| any file | any `<ref>@sha256:<64 hex>`, anywhere on a line |

- **`FROM scratch` is no image.** Docker reserves `scratch` for "no base image", in any case.
  A build stage's name (`FROM base`, after `FROM alpine:3.20 AS base`) is no image either.
- **Commands.** The image of `run`, `create` and `pull` is the first word after the options,
  where an option that takes a value (`-v`, `--name`, `-e`, `--network`, ...) has its value
  skipped. A line continued with ` \` or a PowerShell backtick is followed. An unquoted `;`,
  `|` or `&`, an unclosed quote or a `)` that closes nothing ends the command. A plain word
  before `docker` makes it prose (`a docker run of tests`), except a word a command may
  follow: `sudo`, `exec`, `RUN`, `time`, `nohup`, `command`, `then`, `do`, `else`, `if`,
  `elif`, `while`, `until`, `xargs`, `watch`, `env`, an assignment or a variable.
- **Comments.** In a `#` or `//` comment, a command counts only when it opens the comment
  (`# docker run --rm alpine:3.20 sh`, `# $ docker pull redis:7`) or a backtick code span.
  There it is never continued onto the next line, a variable gives no hit, and `...` ends it.
- **Variables.** `$NAME`, `${NAME}` and PowerShell's `$script:NAME` (and `global:`, `local:`,
  `private:`, `env:`) resolve when the same file assigns exactly one distinct ref-shaped
  literal to that name (`NAME=alpine:3.20`, `$NAME = 'alpine:3.20'`, `NAME: alpine:3.20` in
  YAML, or a quoted value after `name:` elsewhere). `${NAME:-default}` takes its default. A
  name with no such literal, or with two, leaves the hit `unresolved`: it is never guessed.
- A line that names one ref twice is one hit.

### Markers

A marker says what a line names: `docker-keep:` and a value to the end of the line. It counts
anywhere on the line, not only in a comment, though a comment is the usual place for it:

```sh
docker build -t "$tag" .        # docker-keep: acme/app
docker run --rm redis:7 true    # docker-keep: ignore
```

- `ignore` drops every hit of the line.
- Exactly one ref replaces the line's hits with that ref, and names it even on a line with
  no hit; that ref still needs a keep-file entry or a table row.
- Any other value (two refs, prose, nothing) leaves one `unresolved` hit.
- Only the **last** marker on a line counts, so a string that mentions the syntax before a
  real marker does not. A marker glued to a word before it (`my-docker-keep:`) is no marker.
- A command continued over several lines takes a marker on any of its lines.

### Ignore globs

Each `ignore` glob is matched with Python's `fnmatch.fnmatchcase` against the file's whole
path from the repository's root, written with `/`:

- matching is case-sensitive, and anchored at both ends of the path;
- `*` matches any text, **including `/`**, so `tests/*` ignores everything under `tests/` at
  any depth, and `*.sh` ignores every shell script in the repository;
- `?` matches any one character, `/` included, and `[...]` one character of a set;
- `**` is two `*` and means the same as one: there is no special "any directories" form;
- so `**/fixtures/**` needs some text and a `/` before `fixtures/`, and misses a
  `fixtures/` directory at the root; add `fixtures/**` for that one.

## Findings

| Kind | When |
|---|---|
| `no-keep-file` | there is no keep file and the code names an image the table does not declare (one finding, naming the first) |
| `undeclared` | the code names a ref that neither the keep file nor the table declares |
| `unresolved` | the code names an image the scanner cannot resolve: a variable without one literal, or a marker that is neither `ignore` nor one ref |
| `lapsed` | a keep file entry that nothing in the code names (a `build` entry whose `dockerfile` is tracked is never lapsed) |
| `malformed` | the keep file cannot be read as written; it is the only finding |
| `unreadable` | there is no `main` or `master` branch, or `git` cannot read the commit |

## The command line

```text
python3 docker_keeps.py check --repo <dir> [--ref <ref>] [--name <name>] [--tooling-file <md>]...
python3 docker_keeps.py --version
```

- `--repo`: the top directory of a work tree, or a bare repository. A directory inside a
  work tree is refused.
- `--ref`: the commit to read. Without it, the repository's default branch: the branch
  `origin/HEAD` names when that is `main` or `master`, else `main`, else `master`, from any
  remote (origin first) or as a local branch.
- `--name`: the name findings carry. The default is the directory's name.
- `--tooling-file`: a Markdown file holding a machine-tooling table; repeat it to add more.

It prints one line per finding, then a count:

```text
docker-keeps: app: undeclared node:24 (Dockerfile:1)
docker-keeps: app: lapsed redis:7 (docker-images.json)
docker-keeps: app: 2 finding(s)
```

| Exit | Meaning |
|---|---|
| 0 | no finding |
| 1 | one or more findings |
| 2 | it cannot run: a bad argument, `--repo` is not a git repository (or not its top), or a `--tooling-file` cannot be read or holds no valid table |

## Run it pinned by commit SHA

Check out this repository at a full commit SHA, never a branch or a tag (a tag can be moved),
and run it on the commit under test:

```yaml
  docker-keeps:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Check out docker-keeps
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: bilbospocketses/docker-keeps
          ref: <the full commit SHA of a release> # v1.0.0
          path: .docker-keeps
          persist-credentials: false

      - name: Check the images this repository keeps
        run: python .docker-keeps/docker_keeps.py check --repo . --ref HEAD
```

The checker's own checkout is not tracked by your repository, so it is never scanned.

## License

[MIT](LICENSE)
