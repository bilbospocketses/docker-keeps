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

Standard library only.
"""
import fnmatch
import json
import re
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
