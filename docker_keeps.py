#!/usr/bin/env python3
"""docker-keeps: which Docker images a repository keeps, and why.

A repository declares the images its code names in a keep file,
`docker-images.json` at its root:

    {"schema": 1,
     "images": [{"ref": "alpine:3.20@sha256:<64 hex>", "kind": "pin", "purpose": "..."},
                {"ref": "acme/probe", "kind": "build", "purpose": "...",
                 "dockerfile": "images/probe/Dockerfile"}],
     "ignore": ["<path glob>", ...]}

Images kept for the machine itself, not for any one repository, are rows of a
Markdown table between TABLE_START and TABLE_END, columns `Image | Purpose`;
those rows may hold `*` in the repository or the tag.

This module parses both, and matches entries to the images on a box (claims,
claim_of, kept_by) and to the references code names (covers).

It also finds the references a repository's code names (scan_text), reading
the tracked files of its default branch, never the work tree (default_ref,
read_tree), and checks them against its keep file (check_tree, check_repo).
A MARKER comment (the marker, a colon, then its value to the end of the line)
either drops the hits of its line (value `ignore`) or replaces them with the
one ref it names; any other value leaves one unresolved hit.

As a program it checks one repository:

    python3 docker_keeps.py check --repo <dir> [--ref <ref>] [--name <name>] [--tooling-file <md>]...

and prints one line per finding (format_finding), then a count line. Exit 0:
no finding; 1: findings; 2: it cannot run (bad arguments, `--repo` is not a
git repository, a tooling file cannot be read).

Standard library only.
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

__version__ = "1.0.0"
SCHEMA = 1
KEEP_FILE = "docker-images.json"
TABLE_START = "<!-- docker-images:start -->"
TABLE_END = "<!-- docker-images:end -->"
MACHINE_TOOLING = "machine tooling"

KINDS = ("tooling", "pin", "build")
KEEP_FILE_KINDS = ("pin", "build")
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64

# A lowercase repository, a tag and a sha256 digest. Each `/` component is
# alphanumeric runs joined by `.`, `_`, `__` or one or more `-`. A `:port`
# belongs only to the first (domain) component, and only when a `/` follows it,
# so `fedora:44` is repository `fedora`, tag `44`. `*` is allowed in the
# repository and the tag for machine-tooling rows; load_keep_file refuses it.
_COMPONENT = r"[a-z0-9*]+(?:(?:[_.]|__|-+)[a-z0-9*]+)*"
# Docker reference grammar (distribution/reference)
REF_RE = re.compile(
    r"^(?P<repo>" + _COMPONENT + r"(?::\d+(?=/))?(?:/" + _COMPONENT + r")*)"
    r"(?::(?P<tag>[A-Za-z0-9_*][A-Za-z0-9._*-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?$")

_HUB_HOSTS = ("docker.io/", "index.docker.io/")


@dataclass(frozen=True)
class Ref:
    repo: str
    tag: str | None
    digest: str | None


@dataclass(frozen=True)
class Entry:
    ref: Ref
    raw: str
    kind: str        # "tooling", "pin" or "build"
    purpose: str
    source: str      # the repository's name, or MACHINE_TOOLING
    dockerfile: str | None = None


class KeepFileError(ValueError):
    """A keep file or machine-tooling table that cannot be read as written."""


@dataclass
class KeepFile:
    entries: list[Entry]
    ignore: list[str]


# ---------------------------------------------------------------- references

def normalize_repo(repo: str) -> str:
    """Strip a Docker Hub host, then `library/` after it or at the start."""
    for host in _HUB_HOSTS:
        if repo.startswith(host):
            repo = repo[len(host):]
            break
    if repo.startswith("library/"):
        repo = repo[len("library/"):]
    return repo


def parse_ref(text: str) -> Ref | None:
    """The image reference `text` is, or None when it is not ref-shaped."""
    m = REF_RE.fullmatch(text)
    if not m:
        return None
    return Ref(normalize_repo(m.group("repo")), m.group("tag"), m.group("digest"))


def _tag(ref: Ref) -> str | None:
    """The tag a ref names; a ref with neither tag nor digest means `latest`."""
    if ref.tag is not None:
        return ref.tag
    return None if ref.digest else "latest"


# ---------------------------------------------------------------- keep sources

def _bad(source, msg):
    return KeepFileError("%s: %s" % (source, msg))


def load_keep_file(text: str, source: str) -> KeepFile:
    """Read a keep file's text; `source` names the repository it came from."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _bad(source, "%s is not JSON: %s" % (KEEP_FILE, exc)) from None
    if not isinstance(data, dict):
        raise _bad(source, "%s is not a JSON object" % KEEP_FILE)
    schema = data.get("schema")
    if type(schema) is not int or schema != SCHEMA:
        raise _bad(source, "schema must be %d, found %r" % (SCHEMA, schema))
    images = data.get("images")
    if not isinstance(images, list):
        raise _bad(source, "`images` must be a list")
    ignore = data.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(g, str) and g for g in ignore):
        raise _bad(source, "`ignore` must be a list of path globs")
    entries = []
    for i, item in enumerate(images):
        where = "images[%d]" % i
        if not isinstance(item, dict):
            raise _bad(source, "%s is not an object" % where)
        raw = item.get("ref")
        kind = item.get("kind")
        purpose = item.get("purpose")
        dockerfile = item.get("dockerfile")
        if not isinstance(raw, str):
            raise _bad(source, "%s has no ref" % where)
        ref = parse_ref(raw)
        if ref is None:
            raise _bad(source, "%s ref %r is not an image reference" % (where, raw))
        if "*" in raw:
            raise _bad(source, "%s ref %r: `*` belongs to the machine-tooling table only" % (where, raw))
        if kind not in KEEP_FILE_KINDS:
            raise _bad(source, "%s kind must be pin or build, found %r" % (where, kind))
        if kind == "build" and ref.digest:
            raise _bad(source, "%s build ref %r names a digest; a build names repo or repo:tag" % (where, raw))
        if not isinstance(purpose, str) or not purpose.strip():
            raise _bad(source, "%s has no purpose" % where)
        if dockerfile is not None and (not isinstance(dockerfile, str) or not dockerfile):
            raise _bad(source, "%s dockerfile must be a path" % where)
        entries.append(Entry(ref=ref, raw=raw, kind=kind, purpose=purpose.strip(),
                             source=source, dockerfile=dockerfile))
    return KeepFile(entries=entries, ignore=list(ignore))


def _cells(line):
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")) or len(s) < 2:
        return None
    return [c.strip() for c in s[1:-1].split("|")]


def load_tooling_table(markdown: str) -> list[Entry]:
    """The machine-tooling rows between TABLE_START and TABLE_END."""
    lines = markdown.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == TABLE_START]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == TABLE_END]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise KeepFileError("%s: expected one %s and one %s after it"
                            % (MACHINE_TOOLING, TABLE_START, TABLE_END))
    body = [ln for ln in lines[starts[0] + 1:ends[0]] if ln.strip()]
    if len(body) < 2:
        raise KeepFileError("%s: no `| Image | Purpose |` table between the markers" % MACHINE_TOOLING)
    header, rule = _cells(body[0]), _cells(body[1])
    if header is None or [c.lower() for c in header] != ["image", "purpose"]:
        raise KeepFileError("%s: the table header must be `| Image | Purpose |`" % MACHINE_TOOLING)
    if rule is None or len(rule) != 2 or not all(re.fullmatch(r":?-{3,}:?", c) for c in rule):
        raise KeepFileError("%s: the table header has no `|---|---|` line" % MACHINE_TOOLING)
    entries = []
    for ln in body[2:]:
        cells = _cells(ln)
        if cells is None or len(cells) != 2:
            raise KeepFileError("%s: not a two-column table row: %r" % (MACHINE_TOOLING, ln))
        raw, purpose = cells
        if len(raw) >= 2 and raw.startswith("`") and raw.endswith("`"):
            raw = raw[1:-1].strip()
        ref = parse_ref(raw)
        if ref is None:
            raise KeepFileError("%s: %r is not an image reference" % (MACHINE_TOOLING, raw))
        if not purpose:
            raise KeepFileError("%s: %r has no purpose" % (MACHINE_TOOLING, raw))
        entries.append(Entry(ref=ref, raw=raw, kind="tooling", purpose=purpose, source=MACHINE_TOOLING))
    return entries


# ---------------------------------------------------------------- matching

def _repo_matches(entry: Entry, repo: str) -> bool:
    if entry.kind == "tooling":
        return fnmatch.fnmatchcase(repo, entry.ref.repo)
    return repo == entry.ref.repo


def _tag_matches(entry: Entry, tag: str | None) -> bool:
    want = _tag(entry.ref)
    if tag is None or want is None:
        return False
    if entry.kind == "tooling":
        return fnmatch.fnmatchcase(tag, want)
    return tag == want


def _parsed(texts):
    return [r for r in (parse_ref(t) for t in texts or []) if r is not None]


def claims(entry: Entry, image: dict) -> bool:
    """Does `entry` claim `image`, a box image as {"refs", "repo", "digests"}?"""
    ref = entry.ref
    if ref.digest:
        if ref.digest == PLACEHOLDER_DIGEST:
            return False
        return any(d.digest == ref.digest and _repo_matches(entry, d.repo)
                   for d in _parsed(image.get("digests")))
    tagged = _parsed(image.get("refs"))
    if entry.kind == "build":
        if ref.tag is None:
            repos = [r.repo for r in tagged]
            if image.get("repo"):
                repos.append(normalize_repo(image["repo"]))
            return any(_repo_matches(entry, r) for r in repos)
        return any(_repo_matches(entry, r.repo) and r.tag == ref.tag for r in tagged)
    return any(_repo_matches(entry, r.repo) and _tag_matches(entry, r.tag) for r in tagged)


_PRECEDENCE = {"tooling": 0, "pin": 1, "build": 2}


def _claimers(image, entries):
    found = [(i, e) for i, e in enumerate(entries) if claims(e, image)]
    found.sort(key=lambda pair: (_PRECEDENCE.get(pair[1].kind, len(_PRECEDENCE)), pair[0]))
    return [e for _i, e in found]


def claim_of(image: dict, entries: list[Entry]) -> Entry | None:
    """The entry that shows for `image`: machine tooling, then pin, then build, then list order."""
    found = _claimers(image, entries)
    return found[0] if found else None


def kept_by(image: dict, entries: list[Entry]) -> str:
    """`machine tooling`, `<source> pin`, `<source> build` or `-`, plus ` +N` other sources."""
    found = _claimers(image, entries)
    if not found:
        return "-"
    first = found[0]
    label = MACHINE_TOOLING if first.kind == "tooling" else "%s %s" % (first.source, first.kind)
    others = {e.source for e in found} - {first.source}
    return label + (" +%d" % len(others) if others else "")


def covers(entry: Entry, ref: Ref) -> bool:
    """Does `entry` declare `ref`, an image reference the code names?

    A digest entry declares a ref with the same repository and digest (the tag is
    ignored). An entry without a digest never declares a ref that pins one: a pull
    by digest leaves the image untagged, so the tag would not keep it. A build with
    no tag declares every tag of its repository.
    """
    if entry.ref.digest:
        return ref.digest == entry.ref.digest and _repo_matches(entry, ref.repo)
    if ref.digest or not _repo_matches(entry, ref.repo):
        return False
    if entry.kind == "build" and entry.ref.tag is None:
        return True
    if entry.kind == "build":
        return _tag(ref) == entry.ref.tag
    return _tag_matches(entry, _tag(ref))


def _ref_text(ref: Ref) -> str:
    return ref.repo + (":" + ref.tag if ref.tag is not None else "") + ("@" + ref.digest if ref.digest else "")


# ---------------------------------------------------------------- scanning

HOWS = ("from", "compose", "pull", "run", "create", "build-tag", "digest", "key", "marker")


@dataclass(frozen=True)
class Hit:
    path: str
    line: int          # 1-based
    ref: Ref | None    # None: the code names an image the scanner cannot resolve
    text: str          # the token as written, or the marker
    how: str           # one of HOWS


_DOCKERFILE_GLOBS = ("Dockerfile", "Dockerfile.*", "*.Dockerfile", "*.dockerfile")
_COMPOSE_GLOB = "*compose*.y*ml"

# Options of run, create, pull and build that take the next token as their value.
_VALUE_OPTIONS = frozenset((
    "-v --volume -e --env --env-file -p --publish --name -w --workdir -u --user --network --network-alias"
    " --label -l --entrypoint --device --cap-add --cap-drop --restart --mount --platform --security-opt"
    " --memory -m --cpus --hostname --add-host --tmpfs --ulimit --shm-size --progress -f --file"
    " --build-arg --target --cache-from --stop-timeout --cpuset-cpus").split())
# A short-flag cluster (`-dp`) takes a value when its last letter does.
_VALUE_LETTERS = "vepwulmf"
# A plain word before `docker` makes it prose ("a docker run of tests"), unless
# the word is one a command may follow.
_COMMAND_PREFIXES = frozenset(
    "sudo exec RUN time nohup command then do else if elif while until xargs watch env".split())

_DOCKER_CMD_RE = re.compile(
    r"(?<![\w.-])docker(?:\.exe)?\s+"
    r"(?:image\s+(?P<ipull>pull)|(?P<cmd>pull|run|create)|(?:buildx\s+)?(?P<build>build))(?![\w-])")
_COMMENT_START_RE = re.compile(r"(?:^|(?<=\s))(?:#|//)")
_FROM_RE = re.compile(r"^\s*FROM\s+(.*)$", re.IGNORECASE)
_PSD1_KEY_RE = re.compile(r"^\s*\w*Image\s*=\s*'([^']*)'")
_JSON_KEY_RE = re.compile(r'"\w*[Ii]mage"\s*:\s*"([^"]*)"')
_YML_KEY_RE = re.compile(r"^\s*-?\s*\w*image\s*:\s*(.*)$", re.IGNORECASE)
_COMPOSE_KEY_RE = re.compile(r"^\s*-?\s*image\s*:\s*(.*)$")
_DIGEST_RE = re.compile(r"(?<![A-Za-z0-9._/:@-])([A-Za-z0-9._/:-]+@sha256:[0-9a-f]{64})(?![0-9A-Za-z])")
MARKER = "docker-keep"   # this source never writes it with its colon, so it never marks itself


def _marker_value(line):
    """The value of the LAST marker on `line`, or None.

    A string that names the marker syntax before a real marker does not count.
    A backward scan, not a `.*`-led regex: that one is quadratic per line, and a
    minified file has lines of megabytes.
    """
    key = MARKER + ":"
    i = line.rfind(key)
    while i >= 0:
        if i == 0 or not (line[i - 1].isalnum() or line[i - 1] in "_-"):
            return line[i + len(key):]
        i = line.rfind(key, 0, i)
    return None
# `V = 'lit'`, `$V = "lit"`, `V=lit` (bare only with no space: `$x = if ...` is no literal) and `V: lit`.
_ASSIGN_RE = re.compile(
    r"(?<![\w$.-])\$?(?:(?:script|global|local|private|env):)?(?P<name>\w+)"
    r"""(?:\s*=(?!=)\s*(?P<eq>"[^"]*"|'[^']*')"""
    r"""|=(?P<bare>[^\s"',;)\]}=][^\s"',;)\]}]*)"""
    r"""|\s*:(?!:)\s*(?P<colon>"[^"]*"|'[^']*'|[^\s"',;)\]}]+))""")
_VAR_RE = re.compile(
    r"\$\{(?P<bname>\w+)(?:(?P<dflt>:-)(?P<default>[^}]*)|[^}]*)\}"
    r"|\$(?:(?:script|global|local|private|env):)?(?P<name>\w+)")


def _basename_matches(path, globs):
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(base, g) for g in globs)


def _assignments(lines, yaml):
    """name -> {Ref: literal} for every ref-shaped literal the file assigns to that name.

    `V: lit` takes a bare literal in YAML only; elsewhere (`ref: string`, a
    TypeScript annotation) the colon form needs a quoted one.
    """
    found = {}
    for line in lines:
        for m in _ASSIGN_RE.finditer(line):
            value = next(v for v in (m.group("eq"), m.group("bare"), m.group("colon")) if v is not None)
            quoted = value[:1] in ("'", '"') and len(value) >= 2
            if m.group("colon") is not None and not (quoted or yaml):
                continue
            if quoted:
                value = value[1:-1]
            ref = parse_ref(value)
            if ref is not None:
                found.setdefault(m.group("name"), {}).setdefault(ref, value)
    return found


def _resolve(token, assigns, allow_vars=True):
    """("ref", Ref), ("unresolved", None) or (None, None) when the token names no image."""
    if "$" not in token.replace("\\$", ""):   # `\$` is an escaped dollar, not a variable
        ref = parse_ref(token)
        return ("ref", ref) if ref is not None else (None, None)
    if not allow_vars:
        return (None, None)
    failed = []

    def substitute(m):
        if m.group("dflt"):
            return m.group("default")
        literals = assigns.get(m.group("bname") or m.group("name"), {})
        if len(literals) != 1:
            failed.append(m.group(0))
            return m.group(0)
        return next(iter(literals.values()))

    text = _VAR_RE.sub(substitute, token)
    ref = None if failed or "$" in text else parse_ref(text)
    return ("ref", ref) if ref is not None else ("unresolved", None)


def _tokens(text):
    """(token, offset) pairs of a command text, quotes removed.

    A quote opens a quoted part at the start of a token or after `=`; anywhere
    else it ends the token (the end of a string the command sits in). A quote
    that is never closed, a `)` that closes nothing, or an unquoted shell
    operator character (`;`, `|`, `&`) ends the command. Inside
    the token's own parentheses (`$(...)`) quotes are kept as written.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        start, buf, depth = i, [], 0
        while i < n and (depth or not text[i].isspace()):
            c = text[i]
            if c in "\"'":
                close = text.find(c, i + 1)
                if depth:
                    close = n - 1 if close < 0 else close
                    buf.append(text[i:close + 1])
                    i = close + 1
                    continue
                if i == start or text[i - 1] == "=":
                    if close < 0:
                        return out
                    buf.append(text[i + 1:close])
                    i = close + 1
                    continue
                i += 1
                break
            if c in ";|&" and not depth:          # a shell operator ends the command, as `)` does
                if buf:
                    out.append(("".join(buf), start))
                return out
            if c == "(":
                depth += 1
            elif c == ")":
                if not depth:
                    if buf:
                        out.append(("".join(buf), start))
                    return out
                depth -= 1
            buf.append(c)
            i += 1
        out.append(("".join(buf), start))
    return out


def _command_images(kind, tokens, in_comment):
    """The (token, offset) pairs a docker command names as its image or tags."""
    found = []
    i = 0
    while i < len(tokens):
        tok, off = tokens[i]
        if in_comment and "..." in tok:
            break
        if kind == "build":
            if tok in ("-t", "--tag"):
                if i + 1 < len(tokens) and not (in_comment and "..." in tokens[i + 1][0]):
                    found.append(tokens[i + 1])
                i += 2
                continue
            for prefix in ("--tag=", "-t="):
                if tok.startswith(prefix):
                    found.append((tok[len(prefix):], off + len(prefix)))
            i += 1
            continue
        if tok in _VALUE_OPTIONS or (re.fullmatch(r"-[A-Za-z]{2,}", tok) and tok[-1] in _VALUE_LETTERS):
            i += 2
            continue
        if tok.startswith(("-", "@")):
            i += 1
            continue
        found.append((tok, off))
        break
    return found


def _command_span(lines, i, start, in_comment):
    """The text of the command starting at lines[i][start:], and (offset, line index) piece starts.

    Code follows ` \\` and PowerShell backtick continuations; a comment never
    does. A backtick left in the text ends it, and so does an escaped one
    (`\\``, a JavaScript template). An escaped quote (`\\"` in a JSON string)
    is a quote.
    """
    text, pieces, j = "", [], i
    body = lines[i][start:]
    while True:
        stripped = body.rstrip()
        more = (not in_comment and j + 1 < len(lines)
                and (stripped.endswith("`") or re.search(r"\s\\$", stripped) is not None))
        pieces.append((len(text), j))
        piece = stripped[:-1] if more else body
        text += piece.replace('\\"', '"').replace("\\'", "'") + " "
        if not more:
            break
        j += 1
        body = lines[j]
    tick = text.find("`")
    if tick > 0 and text[tick - 1] == "\\":
        tick -= 1
    return (text if tick < 0 else text[:tick]), pieces


def _is_command(line, start, comment):
    """Does the `docker` at `line[start:]` begin a command, not a mention in prose?

    In a comment it must open the comment text or a backtick code span; in code
    the word before it, if any, must be one a command may follow: a command
    prefix, an assignment, a variable (`docker exec $c docker run`), or an
    escaped line break (a script held in a JSON string).
    """
    if comment is not None and comment.start() < start:
        if start > 0 and line[start - 1] == "`":
            return True
        return re.fullmatch(r"(?:#+|//+)\s*(?:\$\s+)?", line[comment.start():start]) is not None
    before = line[:start].rstrip()
    if not before or not before[-1].isalnum() or re.search(r"\\[nr]$", before):
        return True
    word = re.sub(r"^(?:\$\(|[\"'(\[{])+", "", before.split()[-1])   # `"sudo`, `(sudo`, `$(sudo`
    return word in _COMMAND_PREFIXES or "=" in word or word.startswith("$")


def _line_of(pieces, offset):
    line = pieces[0][1]
    for start, j in pieces:
        if start <= offset:
            line = j
    return line


def scan_text(path: str, text: str) -> list[Hit]:
    """The image references the file at `path` (POSIX, repo-relative) names."""
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    base = path.rsplit("/", 1)[-1]
    assigns = _assignments(lines, base.endswith((".yml", ".yaml")))
    is_dockerfile = _basename_matches(path, _DOCKERFILE_GLOBS)
    is_compose = fnmatch.fnmatchcase(base, _COMPOSE_GLOB)
    is_yml = base.endswith((".yml", ".yaml")) and not is_compose
    hits = []   # (Hit, set of line indexes a marker on which applies to it)

    def add(i, token, how, group=None, allow_vars=True):
        state, ref = _resolve(token, assigns, allow_vars)
        if state is not None:
            hits.append((Hit(path, i + 1, ref, token, how), group or {i}))

    stages = set()
    for i, line in enumerate(lines):
        if is_dockerfile:
            m = _FROM_RE.match(line)
            words = [w for w in (m.group(1).split() if m else []) if not w.startswith("--")]
            if words:
                # A stage name is no image, nor is `scratch`: Docker reserves it for "no base image".
                if words[0].lower() not in stages and words[0].lower() != "scratch":
                    add(i, words[0], "from")
                if len(words) >= 3 and words[1].lower() == "as":
                    stages.add(words[2].lower())
        if is_compose or is_yml:
            m = (_COMPOSE_KEY_RE if is_compose else _YML_KEY_RE).match(line)
            if m:
                value = m.group(1).strip()
                if value[:1] in ("'", '"'):
                    close = value.find(value[0], 1)
                    value = value[1:close] if close > 0 else value[1:]
                else:
                    value = re.split(r"\s#", value, maxsplit=1)[0].strip()
                if value:
                    add(i, value, "compose" if is_compose else "key")
        if base.endswith(".psd1"):
            m = _PSD1_KEY_RE.match(line)
            if m:
                add(i, m.group(1), "key")
        if base.endswith(".json"):
            for m in _JSON_KEY_RE.finditer(line):
                add(i, m.group(1), "key")
        comment = _COMMENT_START_RE.search(line)
        for m in _DOCKER_CMD_RE.finditer(line):
            if not _is_command(line, m.start(), comment):
                continue
            in_comment = comment is not None and comment.start() < m.start()
            kind = "build" if m.group("build") else "pull" if m.group("ipull") else m.group("cmd")
            span, pieces = _command_span(lines, i, m.end(), in_comment)
            group = {j for _s, j in pieces}
            for token, offset in _command_images(kind, _tokens(span), in_comment):
                add(_line_of(pieces, offset), token, "build-tag" if kind == "build" else kind,
                    group, allow_vars=not in_comment)
        for m in _DIGEST_RE.finditer(line):
            add(i, m.group(1), "digest")

    for i, line in enumerate(lines):
        value = _marker_value(line)
        if value is None:
            continue
        value = value.strip()
        hits = [(h, g) for h, g in hits if i not in g]
        if value == "ignore":
            continue
        hits.append((Hit(path, i + 1, parse_ref(value), "%s: %s" % (MARKER, value), "marker"), {i}))

    seen, out = set(), []
    for hit, _group in sorted(hits, key=lambda pair: pair[0].line):
        if (hit.line, hit.ref) not in seen:
            seen.add((hit.line, hit.ref))
            out.append(hit)
    return out


def is_scanned(path: str, ignore: list[str]) -> bool:
    """Is the file at `path` (POSIX, repo-relative) read for image references?"""
    parts = path.split("/")
    base = parts[-1]
    if "docs" in parts[:-1] or ".superpowers" in parts[:-1]:
        return False
    if base.lower().endswith(".md") or base.startswith("CHANGELOG") or path == KEEP_FILE:
        return False
    return not any(fnmatch.fnmatchcase(path, glob) for glob in ignore)


# ---------------------------------------------------------------- repositories

def _git(git_dir, *args, data=None):
    return subprocess.run(["git", "-C", git_dir, *args], input=data, capture_output=True)


def _commit_exists(git_dir, ref):
    return _git(git_dir, "rev-parse", "--verify", "-q", ref + "^{commit}").returncode == 0


def default_ref(git_dir: str) -> str | None:
    """The ref a repository is checked at: its main or master branch, or None.

    None also when `git_dir` is no repository. Raises OSError when git cannot
    run at all (not installed, not on PATH), so that cause is never reported
    as a missing branch.
    """
    head = _git(git_dir, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    target = head.stdout.decode("utf-8", "replace").strip() if head.returncode == 0 else ""
    if target.removeprefix("refs/remotes/origin/") in ("main", "master") and _commit_exists(git_dir, target):
        return target
    listed = _git(git_dir, "remote")
    remotes = listed.stdout.decode("utf-8", "replace").split() if listed.returncode == 0 else []
    remotes = ["origin"] + [r for r in remotes if r != "origin"]
    for branch in ("main", "master"):
        for ref in ["refs/remotes/%s/%s" % (r, branch) for r in remotes] + ["refs/heads/" + branch]:
            if _commit_exists(git_dir, ref):
                return ref
    return None


def read_tree(git_dir: str, ref: str) -> dict[str, str]:
    """{path: text} of the files tracked at `ref`; binary or non-UTF-8 files are left out.

    Raises OSError when git cannot read the tree.
    """
    listed = _git(git_dir, "ls-tree", "-r", "-z", "--name-only", ref)
    if listed.returncode != 0:
        raise OSError("git ls-tree %s: %s" % (ref, listed.stderr.decode("utf-8", "replace").strip()))
    names = [n.decode("utf-8", "surrogateescape") for n in listed.stdout.split(b"\0") if n]
    names = [n for n in names if "\n" not in n]   # cat-file --batch reads one object name a line
    if not names:
        return {}
    batch = "".join("%s:%s\n" % (ref, n) for n in names).encode("utf-8", "surrogateescape")
    cat = _git(git_dir, "cat-file", "--batch", data=batch)
    if cat.returncode != 0:
        raise OSError("git cat-file: %s" % cat.stderr.decode("utf-8", "replace").strip())
    out, pos, files = cat.stdout, 0, {}
    for name in names:
        end = out.find(b"\n", pos)
        header, pos = out[pos:end], end + 1
        if header.endswith((b" missing", b" ambiguous")):
            continue
        fields = header.split(b" ")
        if end < 0 or len(fields) != 3 or not fields[2].isdigit():
            raise OSError("git cat-file: unexpected output %r" % header[:80])
        kind, size = fields[1], int(fields[2])
        body = out[pos:pos + size]
        pos += size + 1
        if kind != b"blob" or b"\0" in body:
            continue
        try:
            files[name] = body.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return files


# ---------------------------------------------------------------- findings

FINDING_KINDS = ("no-keep-file", "undeclared", "unresolved", "lapsed", "malformed", "unreadable")


@dataclass(frozen=True)
class Finding:
    repo: str
    kind: str              # one of FINDING_KINDS
    detail: str            # the ref or the cause, never the kind
    path: str | None = None
    line: int | None = None


def check_tree(name: str, files: dict[str, str], tooling: list[Entry]) -> list[Finding]:
    """The findings for repository `name` whose tracked files are `files`."""
    keep = None
    if KEEP_FILE in files:
        try:
            keep = load_keep_file(files[KEEP_FILE], name)
        except KeepFileError as exc:
            cause = str(exc).removeprefix(name + ": ")
            return [Finding(name, "malformed", cause, KEEP_FILE)]
    ignore = keep.ignore if keep else []
    hits = [h for p in sorted(files) if is_scanned(p, ignore) for h in scan_text(p, files[p])]
    declared = list(tooling) + (keep.entries if keep else [])

    def covered(hit, entries):
        return hit.ref is not None and any(covers(e, hit.ref) for e in entries)

    findings = []
    if keep is None:
        loose = [h for h in hits if not covered(h, declared)]
        if loose:
            first = loose[0]
            detail = _ref_text(first.ref) if first.ref is not None else first.text
            findings.append(Finding(name, "no-keep-file", detail, first.path, first.line))
    for hit in hits:
        if hit.ref is None:
            findings.append(Finding(name, "unresolved", hit.text, hit.path, hit.line))
        elif keep is not None and not covered(hit, declared):
            findings.append(Finding(name, "undeclared", _ref_text(hit.ref), hit.path, hit.line))
    for entry in keep.entries if keep else []:
        if entry.kind == "build" and entry.dockerfile in files:
            continue
        if not any(h.ref is not None and covers(entry, h.ref) for h in hits):
            findings.append(Finding(name, "lapsed", entry.raw, KEEP_FILE))
    return findings


def check_repo(git_dir: str, ref: str | None = None, tooling: list[Entry] = (),
               name: str | None = None) -> list[Finding]:
    """The findings for the repository at `git_dir`, read at `ref` (default: default_ref)."""
    name = name or os.path.basename(os.path.abspath(git_dir))
    try:
        ref = ref or default_ref(git_dir)
        if ref is None:
            return [Finding(name, "unreadable", "no main or master branch")]
        files = read_tree(git_dir, ref)
    except OSError as exc:   # git cannot run, or cannot read the tree: the real cause
        return [Finding(name, "unreadable", str(exc))]
    return check_tree(name, files, list(tooling))


def format_finding(f: Finding, name: str) -> str:
    """`docker-keeps: <name>: <kind> <detail>`, then ` (<path>:<line>)`, ` (<path>)` or nothing."""
    where = ""
    if f.path is not None:
        where = " (%s%s)" % (f.path, "" if f.line is None else ":%d" % f.line)
    return "docker-keeps: %s: %s %s%s" % (name, f.kind, f.detail, where)


# ---------------------------------------------------------------- command line

def _repository_error(path):
    """Why `path` is not a bare repository or the top of a work tree, or None when it is one.

    A directory inside a work tree is refused: git would read only its part of
    the tree. Raises OSError when git cannot run.
    """
    if not os.path.isdir(path):
        return "%s is not a directory" % path
    bare = _git(path, "rev-parse", "--is-bare-repository")
    cause = bare.stderr.decode("utf-8", "replace").strip()
    if bare.returncode != 0:
        return "%s is not a git repository%s" % (path, ": " + cause if cause else "")
    if bare.stdout.strip() == b"true":
        return None
    top = _git(path, "rev-parse", "--show-toplevel")
    root = top.stdout.decode("utf-8", "surrogateescape").strip()
    try:
        if top.returncode == 0 and os.path.samefile(root, path):
            return None
    except OSError:
        pass
    return "%s is not a git repository's top directory (that is %s)" % (path, root or "unknown")


def _parser():
    parser = argparse.ArgumentParser(
        prog="docker-keeps", description="Check that a repository declares the Docker images its code names.")
    parser.add_argument("--version", action="version", version="docker-keeps " + __version__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="check")
    check = commands.add_parser("check", help="check one repository against its keep file")
    check.add_argument("--repo", required=True, help="the repository: the top of a work tree, or a bare repository")
    check.add_argument("--ref", help="the commit to read (default: its main or master branch)")
    check.add_argument("--name", help="the name findings carry (default: the directory's name)")
    check.add_argument("--tooling-file", action="append", default=[], dest="tooling_files", metavar="MD",
                       help="a Markdown file whose machine-tooling table also declares images (repeatable)")
    return parser


def main(argv: list[str]) -> int:
    """The `check` command: 0 no finding, 1 findings, 2 cannot run."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:          # argparse: --help and --version exit 0, a bad argument 2
        return exc.code if isinstance(exc.code, int) else 2
    name = args.name or os.path.basename(os.path.abspath(args.repo))
    tooling = []
    try:
        for path in args.tooling_files:
            try:
                with open(path, encoding="utf-8") as fh:
                    tooling += load_tooling_table(fh.read())
            except (KeepFileError, UnicodeDecodeError) as exc:
                raise OSError("%s: %s" % (path, exc)) from None
        error = _repository_error(args.repo)
    except OSError as exc:
        error = str(exc)
    if error is not None:
        print("docker-keeps: cannot run: %s" % error, file=sys.stderr)
        return 2
    findings = check_repo(args.repo, args.ref, tooling, name)
    for f in findings:
        print(format_finding(f, name))
    print("docker-keeps: %s: %d finding(s)" % (name, len(findings)))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
