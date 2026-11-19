#!/usr/bin/env python3
"""Tests for docker_keeps.py, the shared Docker image keep checker.

WHAT THIS PINS:

  * parse_ref: the whole text must be one image reference -- a lowercase
    repository, an optional Docker tag and an optional sha256 digest of 64 hex;
    a Docker Hub host and its `library/` are stripped, every other host is kept
    as written; prose, variables, URLs and short digests are not refs;
  * load_keep_file: schema 1, an `images` list of pin and build entries, each
    with a ref, a kind and a non-empty purpose; `*` is for the machine-tooling
    table only; an all-zero placeholder digest is allowed;
  * load_tooling_table: the rows between the two markers, under an
    `| Image | Purpose |` header;
  * claims / claim_of / kept_by: one check per matching rule, the precedence
    (machine tooling, then pin, then build, then list order) and the `+N` count
    of other sources;
  * covers: when a keep entry declares a ref the code names.

Every check counts; a case that checks nothing, or raises, is a failure.

Stdlib only, Python 3. ASCII only.

Run:  python3 tests/run_tests.py
Exit: 0 all passed, 1 any failure.
"""
import inspect
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True   # no __pycache__ in the work tree for a commit to pick up

from docker_keeps import (  # noqa: E402
    MACHINE_TOOLING, TABLE_END, TABLE_START, Entry, KeepFileError, Ref,
    claim_of, claims, covers, kept_by, load_keep_file, load_tooling_table,
    parse_ref)

RESULTS = []  # (case, line, ok)
_CURRENT = ["?"]


def check(condition, label=""):
    line = inspect.currentframe().f_back.f_lineno
    RESULTS.append((_CURRENT[0], line, bool(condition), label))


def raises(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return True
    return False


def read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def keep_with(*pins, build=None):
    """A keep file holding one pin entry per ref, plus one (ref, dockerfile) build."""
    images = [{"ref": r, "kind": "pin", "purpose": "p"} for r in pins]
    if build:
        images.append({"ref": build[0], "kind": "build", "purpose": "p", "dockerfile": build[1]})
    return json.dumps({"schema": 1, "images": images})


def _entry(text, kind, source):
    ref = parse_ref(text)
    assert ref is not None, "not ref-shaped: %r" % text
    return Entry(ref=ref, raw=text, kind=kind, purpose="p", source=source)


def pin_from(source, text):
    return _entry(text, "pin", source)


def build_from(source, text):
    return _entry(text, "build", source)


def pin(text):
    return pin_from("alpha", text)


def build(text):
    return build_from("alpha", text)


def tooling(text):
    return _entry(text, "tooling", MACHINE_TOOLING)


# ---------------------------------------------------------------- cases

def case_parse_ref():
    D = "sha256:" + "d" * 64
    check(parse_ref("alpine:3.20@" + D) == Ref("alpine", "3.20", D))
    check(parse_ref("ubuntu@" + D) == Ref("ubuntu", None, D))
    check(parse_ref("acme/probe") == Ref("acme/probe", None, None))
    check(parse_ref("ghcr.io/cross-rs/*:0.2.5") == Ref("ghcr.io/cross-rs/*", "0.2.5", None))
    check(parse_ref("docker.io/library/alpine:3.20").repo == "alpine")      # normalize_repo
    check(parse_ref("mirror.gcr.io/library/fedora:44").repo == "mirror.gcr.io/library/fedora")  # other hosts kept
    # An all-digit tag is a tag; a port counts only when a path follows it.
    check(parse_ref("fedora:44") == Ref("fedora", "44", None))
    check(parse_ref("node:22") == Ref("node", "22", None))
    check(parse_ref("acme/probe:2") == Ref("acme/probe", "2", None))
    check(parse_ref("localhost:5000/foo:1") == Ref("localhost:5000/foo", "1", None))
    check(parse_ref("registry.example.com:443/team/app:2") == Ref("registry.example.com:443/team/app", "2", None))
    check(parse_ref("my--image:1") == Ref("my--image", "1", None))          # repeated dashes join a component
    check(parse_ref("foo/bar:5000/baz:1") is None)                          # a port only on the first component
    for bad in ("...", "CREATES", "local://x:y", "$IMAGE", "${PROXY_IMAGE:?}", "", "alpine@sha256:abc",
                "alpine:3.20\n"):   # the WHOLE text: a trailing newline is not part of a ref
        check(parse_ref(bad) is None, repr(bad))


def case_load_keep_file():
    kf = load_keep_file(read("keep-ok.json"), "alpha")   # 1 pin with digest, 1 tag-only pin, 1 build, ignore ["tests/**"]
    check([e.kind for e in kf.entries] == ["pin", "pin", "build"] and kf.ignore == ["tests/**"])
    check(kf.entries[2].dockerfile == "images/probe/Dockerfile" and kf.entries[0].source == "alpha")
    for bad in ("not json", '{"schema": 2, "images": []}', '{"schema": 1}', '{"schema":1,"images":[{"ref":"x"}]}',
                '{"schema":1,"images":[{"ref":"x","kind":"pull","purpose":"p"}]}',
                '{"schema":1,"images":[{"ref":"x","kind":"pin","purpose":""}]}',
                '{"schema":1,"images":[{"ref":"...","kind":"pin","purpose":"p"}]}',
                '{"schema":1,"images":[{"ref":"acme/*:1","kind":"pin","purpose":"p"}]}',    # `*` is tooling-only
                '{"schema":1,"images":[{"ref":"alpine:*","kind":"pin","purpose":"p"}]}',
                '{"schema":1,"images":[{"ref":"acme/*","kind":"build","purpose":"p"}]}',
                '{"schema":1,"images":[{"ref":"acme/probe@sha256:' + "e" * 64 + '","kind":"build","purpose":"p"}]}',
                '{"schema":1,"images":[],"ignore":"tests/**"}',
                '{"schema":1,"images":[],"ignore":[""]}',
                '{"schema":1,"images":[{"ref":"acme/probe","kind":"build","purpose":"p","dockerfile":""}]}',
                '{"schema":1,"images":[{"ref":"acme/probe","kind":"build","purpose":"p","dockerfile":7}]}',
                '{"schema":true,"images":[]}'):
        check(raises(KeepFileError, load_keep_file, bad, "alpha"), bad)
    zero = load_keep_file(keep_with("alpine@sha256:" + "0" * 64), "alpha")   # placeholder digest allowed (4.1)
    check(zero.entries[0].ref.digest == "sha256:" + "0" * 64)


def case_load_tooling_table():
    rows = load_tooling_table(read("tooling.md"))   # prose, start marker, | Image | Purpose |, 2 rows, end marker, prose
    check([e.raw for e in rows] == ["ghcr.io/cross-rs/*:0.2.5", "tool-x:5.1.0"])
    check(all(e.kind == "tooling" and e.source == MACHINE_TOOLING for e in rows))
    check(raises(KeepFileError, load_tooling_table, "no markers here"))
    check(raises(KeepFileError, load_tooling_table, TABLE_START + "\n| Image | Purpose |\n|---|---|\n| ... | x |\n" + TABLE_END))


def case_claims():   # design 4.3, one row per rule
    D = "sha256:" + "a" * 64
    im = {"refs": ["alpine:3.20"], "repo": "", "digests": ["alpine@" + D]}
    check(claims(pin("alpine:3.20@" + D), im))                 # digest in RepoDigests
    check(claims(pin("alpine@" + D), im))                      # digest only
    check(not claims(pin("alpine:3.20@sha256:" + "b" * 64), im))  # same tag, other digest: NOT claimed
    check(claims(pin("alpine:3.20"), im))                      # tag-only entry, image carries the tag
    check(not claims(pin("alpine:3.21"), im))
    check(claims(build("acme/probe"), {"refs": ["acme/probe:latest", "acme/probe:v2"], "repo": "", "digests": []}))
    check(not claims(build("acme/probe:v2"), {"refs": ["acme/probe:latest"], "repo": "", "digests": []}))
    check(claims(build("acme/probe"), {"refs": [], "repo": "acme/probe", "digests": []}))  # digest-only row
    check(claims(tooling("ghcr.io/cross-rs/*:0.2.5"), {"refs": ["ghcr.io/cross-rs/x86_64-unknown-linux-gnu:0.2.5"], "repo": "", "digests": []}))
    check(not claims(tooling("ghcr.io/cross-rs/*:0.2.5"), {"refs": ["ghcr.io/cross-rs/x86_64-unknown-linux-gnu:main"], "repo": "", "digests": []}))
    check(not claims(pin("alpine@sha256:" + "0" * 64), {"refs": [], "repo": "alpine", "digests": []}))  # placeholder matches nothing


def case_precedence_and_plus_n():
    im = {"refs": ["alpine:3.20"], "repo": "", "digests": []}
    entries = [build_from("beta", "alpine"), pin_from("alpha", "alpine:3.20"), pin_from("gamma", "alpine:3.20"), tooling("alpine:*")]
    check(claim_of(im, entries).kind == "tooling")
    check(kept_by(im, entries) == "machine tooling +3")   # +N = the OTHER sources (distinct repos) claiming it
    check(kept_by(im, entries[:3]) == "alpha pin +2")      # pin beats build; first pin in list order
    check(kept_by(im, [build_from("beta", "alpine")]) == "beta build")
    check(kept_by(im, []) == "-")
    D = "sha256:" + "a" * 64
    by_digest = {"refs": [], "repo": "alpine", "digests": ["alpine@" + D]}
    check(kept_by(by_digest, [pin_from("alpha", "alpine:3.20@" + D), pin_from("beta", "alpine@" + D)]) == "alpha pin +1")
    two_tags = {"refs": ["alpine:3.20", "alpine:latest"], "repo": "", "digests": []}
    check(kept_by(two_tags, [pin_from("alpha", "alpine:3.20"), pin_from("beta", "alpine")]) == "alpha pin +1")
    rows = [tooling("alpine:*"), tooling("alp*:3.20")]                  # both rows claim it; one source
    check(all(claims(e, im) for e in rows) and kept_by(im, rows) == "machine tooling")


def case_covers():   # a code ref is declared by an entry (Task 2's undeclared / lapsed)
    D = "sha256:" + "c" * 64
    check(covers(pin("alpine:3.20@" + D), parse_ref("alpine:3.20@" + D)))
    check(covers(pin("ubuntu:26.04@" + D), parse_ref("ubuntu@" + D)))      # same repo and digest, tag ignored
    check(not covers(pin("alpine:3.20@" + D), parse_ref("alpine:3.20")))   # code drops the digest: undeclared
    check(covers(pin("koalaman/shellcheck:stable"), parse_ref("koalaman/shellcheck:stable")))
    check(covers(pin("alpine:latest"), parse_ref("alpine")))               # bare repo means :latest (Q5)
    check(covers(build("acme/probe"), parse_ref("acme/probe:v3")))
    check(covers(tooling("ghcr.io/cross-rs/*:0.2.5"), parse_ref("ghcr.io/cross-rs/aarch64-unknown-linux-gnu:0.2.5")))


# ---------------------------------------------------------------- runner

def main():
    cases = [(n, f) for n, f in globals().items() if n.startswith("case_") and callable(f)]
    for name, fn in cases:
        _CURRENT[0] = name
        before = len(RESULTS)
        try:
            fn()
        except Exception as exc:   # a crashing case is a failed row, never a pass
            RESULTS.append((name, 0, False, "raised %s: %s" % (type(exc).__name__, exc)))
        mine = RESULTS[before:]
        if not mine:
            RESULTS.append((name, 0, False, "checked nothing"))
            mine = RESULTS[before:]
        bad = [r for r in mine if not r[2]]
        print("%s %s (%d/%d)" % ("PASS" if not bad else "FAIL", name, len(mine) - len(bad), len(mine)))
        for _case, line, _ok, label in bad:
            print("      line %d: %s" % (line, label or "check failed"))
    failed = [r for r in RESULTS if not r[2]]
    print("docker-keeps: %d/%d checks passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
