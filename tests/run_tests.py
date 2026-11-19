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
  * covers: when a keep entry declares a ref the code names;
  * scan_text: the references a file names -- Dockerfile FROM, compose and
    config keys, docker commands (with their option values skipped and their
    line continuations followed), any digest-pinned ref, variables resolved
    from one literal, markers, and comment prose that is not a command;
  * is_scanned: which paths are read, and the keep file's ignore globs;
  * default_ref / read_tree: the branch a repository is checked at, and its
    tracked text files there (never the work tree);
  * check_tree / check_repo: the findings, their kinds and their order.

Every check counts; a case that checks nothing, or raises, is a failure.
Temporary git repositories live under the system temp dir and are removed at
exit; git runs there with no global or system config.

Stdlib only, Python 3. ASCII only.

Run:  python3 tests/run_tests.py
Exit: 0 all passed, 1 any failure.
"""
import atexit
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True   # no __pycache__ in the work tree for a commit to pick up

# git in the temp repositories, the module's own calls included, sees no
# global or system config: no signing, no hooks, no machine settings.
os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                  GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                  GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

from docker_keeps import (  # noqa: E402
    FINDING_KINDS, MACHINE_TOOLING, TABLE_END, TABLE_START, Entry, KeepFileError, Ref,
    check_repo, check_tree, claim_of, claims, covers, default_ref, is_scanned, kept_by,
    load_keep_file, load_tooling_table, parse_ref, read_tree, scan_text)

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


def fmt(ref):
    """A Ref written out, or None for an unresolved hit."""
    if ref is None:
        return None
    return ref.repo + (":" + ref.tag if ref.tag is not None else "") + ("@" + ref.digest if ref.digest else "")


def refs(hits):
    return [fmt(h.ref) for h in hits]


def unresolved(hits):
    return sum(1 for h in hits if h.ref is None)


def kinds(findings):
    return [f.kind for f in findings]


_TEMP = []
atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in _TEMP])


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


def write(repo, path, data):
    full = os.path.join(repo, *path.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def git_repo(files, name="alpha"):
    """A temp git repository holding `files` ({path: str or bytes}) in one commit."""
    top = tempfile.mkdtemp(prefix="docker-keeps-")
    _TEMP.append(top)
    repo = os.path.join(top, name)
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "local-work")   # a local branch named neither main nor master
    for path, data in files.items():
        write(repo, path, data)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--allow-empty", "-m", "fixture")
    return repo


def repo_with(origin_head=None, remote="origin", branches=None, files=None, local=()):
    """A temp repository whose remote-tracking refs `remote/<branch>` all point at its commit.

    `origin_head` makes `remote/HEAD` point at that branch; `branches` defaults to it alone;
    `local` adds local branches at the same commit.
    """
    repo = git_repo(files or {"README.txt": "fixture\n"})
    git(repo, "remote", "add", remote, "https://example.invalid/%s.git" % remote)
    for branch in (branches if branches is not None else [origin_head] if origin_head else []):
        git(repo, "update-ref", "refs/remotes/%s/%s" % (remote, branch), "HEAD")
    if origin_head:
        git(repo, "symbolic-ref", "refs/remotes/%s/HEAD" % remote, "refs/remotes/%s/%s" % (remote, origin_head))
    for branch in local:
        git(repo, "branch", branch)
    return repo


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


def case_dockerfile_from_and_arg():
    hits = scan_text("images/x/Dockerfile", "ARG NODE_IMAGE=node:24@sha256:" + "e"*64 + "\nFROM ${NODE_IMAGE} AS build\nFROM build AS runtime\nFROM --platform=linux/amd64 alpine:3.20\n")
    check(refs(hits) == ["node:24@sha256:" + "e"*64] * 2 + ["alpine:3.20"])   # line 1 (digest), line 2 (ARG resolved); stage `build` is no image
    check([(h.line, h.how) for h in hits] == [(1, "digest"), (2, "from"), (4, "from")])
    check(scan_text("images/x/notes.txt", "FROM alpine:3.20\n") == [])   # FROM is read in Dockerfiles only
    for name in ("Dockerfile", "Dockerfile.probe", "probe.Dockerfile", "probe.dockerfile"):
        check(refs(scan_text("images/" + name, "FROM alpine:3.20\n")) == ["alpine:3.20"], name)


def case_compose_image_only_in_compose_files():
    check(refs(scan_text("docker-compose.yml", "    image: ${WSSW_IMAGE:-ws-scrcpy-web:local}\n")) == ["ws-scrcpy-web:local"])
    check(unresolved(scan_text("compose/x.proxy.yml", "  image: ${PROXY_IMAGE:?}\n")) == 1)
    check(scan_text("src/FileIconUtils.ts", "    image: '#4ade80',\n") == [])
    check([h.how for h in scan_text("compose.yaml", "  image: redis:7\n")] == ["compose"])


def case_docker_commands():
    check(refs(scan_text("s.sh", "docker run --rm -v \"$v:/b:ro\" -e A=1 --name x alpine:3.20 sh -c 'ls'\n")) == ["alpine:3.20"])
    check(refs(scan_text("s.ps1", "docker run --rm @ownerArgs `\n    -v \"${b}:/b\" busybox:1.36 sh\n")) == ["busybox:1.36"])  # PowerShell continuation
    check(refs(scan_text("s.sh", "docker pull ghcr.io/x/y:1 && docker create node:24\n")) == ["ghcr.io/x/y:1", "node:24"])
    check(refs(scan_text("b.yml", "  run: docker buildx build --platform linux/amd64 --load -t ws-scrcpy-web:ci .\n")) == ["ws-scrcpy-web:ci"])
    check(unresolved(scan_text("b.ps1", "docker build -t $prof.Tag --progress=plain .\n")) == 1)
    hits = scan_text("s.sh", "docker run --rm \\\n  --network host \\\n  alpine:3.20 true\n")    # bash continuation
    check([(h.line, h.how, fmt(h.ref)) for h in hits] == [(3, "run", "alpine:3.20")])
    check([h.how for h in scan_text("s.sh", "docker image pull alpine:3.20\n")] == ["pull"])
    check([h.how for h in scan_text("s.sh", "docker build --tag=acme/probe:2 .\n")] == ["build-tag"])
    check(scan_text("s.sh", "docker build .\n") == [])                 # a build with no tag names no image
    check(scan_text("s.sh", "docker-compose run web\n") == [])         # not a docker command


def case_variable_with_one_literal_resolves():
    t = "$ALPINE = 'alpine:3.20@sha256:" + "d"*64 + "'\ndocker run --rm $ALPINE sh\n"
    check(refs(scan_text("x.ps1", t)) == ["alpine:3.20@sha256:" + "d"*64] * 2)   # the digest hit and the run hit
    two = "$I = 'a:1'\n$I = 'b:2'\ndocker run $I\n"
    check(unresolved(scan_text("y.ps1", two)) == 1)            # two literals: unresolved, never guessed
    check(refs(scan_text("z.sh", "IMAGE=alpine:3.20\ndocker run \"${IMAGE}\" true\n")) == ["alpine:3.20"])
    check(refs(scan_text("z.ps1", "$script:IMG = \"redis:7\"\ndocker run $script:IMG\n")) == ["redis:7"])
    check(scan_text("z.sh", "docker run -it\n") == [])             # neither ref-shaped nor a variable: no hit


def case_keys_in_psd1_json_yml():
    check(refs(scan_text("config/linux.psd1", "        Image             = 'acme/runner'\n")) == ["acme/runner"])
    check(refs(scan_text("base/redroid.lock", '    "qemu": "qemux/qemu@sha256:' + "f"*64 + '",\n')) == ["qemux/qemu@sha256:" + "f"*64])  # digest anywhere
    check(refs(scan_text(".github/workflows/b.yml", "      image: mirror.gcr.io/library/debian:trixie\n")) == ["mirror.gcr.io/library/debian:trixie"])  # Q4
    check(refs(scan_text("cfg/x.json", '{"baseImage": "alpine:3.20", "name": "redis:7"}\n')) == ["alpine:3.20"])
    hits = scan_text("cfg/y.json", '{"image": "alpine@sha256:' + "a"*64 + '"}\n')
    check(len(hits) == 1 and hits[0].how == "key")             # one line naming a ref twice is one hit


def case_markers():
    check(scan_text("t.ps1", "docker build -t $t .  # docker-keep: acme/feed\n")[0].ref == parse_ref("acme/feed"))
    check(scan_text("t.ps1", "docker run redis:7  # docker-keep: ignore\n") == [])
    hits = scan_text("t.ps1", "docker build -t $t .  # docker-keep: acme/feed\n")
    check([(h.line, h.how, fmt(h.ref)) for h in hits] == [(1, "marker", "acme/feed")])
    # A marker is `ignore` or exactly one ref; anything else makes the line's hit unresolved (F12).
    for bad in ("docker run $X  # docker-keep: some image\n", "docker run $X  # docker-keep: a:1 b:2\n",
                "docker run alpine:3.20  # docker-keep: Not-A-Ref\n", "docker run alpine:3.20  # docker-keep:\n"):
        hits = scan_text("t.ps1", bad)
        check(len(hits) == 1 and hits[0].ref is None and hits[0].how == "marker", bad)
    # A continued command takes a marker on any of its lines: a comment cannot follow a continuation.
    cont = "docker run --rm \\\n  $X \\\n  sh  # docker-keep: alpine:3.20\n"
    check([(h.line, fmt(h.ref)) for h in scan_text("t.sh", cont)] == [(3, "alpine:3.20")])
    check(scan_text("t.sh", "docker run --rm \\\n  $X \\\n  sh  # docker-keep: ignore\n") == [])


def case_comment_prose_is_not_a_ref():
    for line in ("# exec'd verbatim, so `docker run ... sh` still works\n",
                 "# Gate on the process actually serving, never on `docker run` exiting 0 --\n",
                 "#   Fail closed. `docker run -v <name>:/p` CREATES a named volume on demand, so\n",
                 "# Seconds after `docker run` before the first key.\n"):
        check(scan_text("lib/x.psm1", line) == [])
    check(scan_text("lib/x.psm1", "# docker run $IMAGE sh\n") == [])            # a variable in prose is no hit
    check(refs(scan_text("lib/x.psm1", "# docker run --rm alpine:3.20 sh\n")) == ["alpine:3.20"])  # fully written
    check(refs(scan_text("lib/x.js", "// docker pull redis:7\n")) == ["redis:7"])


# ---- calibration fixes: each false hit the real repositories showed, by shape

def case_comment_command_opens_the_comment_or_a_code_span():   # R1
    check(scan_text("lib/x.psm1", "# guest container (docker run -d creates it even when the start then fails), the\n") == [])
    check(scan_text("lib/x.psm1", "    # the docker run line (87)\n") == [])
    check(refs(scan_text("lib/x.psm1", "#   docker run --rm alpine:3.20 sh\n")) == ["alpine:3.20"])
    check(refs(scan_text("lib/x.psm1", "# $ docker run --rm alpine:3.20 sh\n")) == ["alpine:3.20"])
    check(refs(scan_text("lib/x.psm1", "# measured: `docker run alpine printf x` read back as\n")) == ["alpine"])


def case_prose_before_docker_is_not_a_command():   # R6
    for line in ('    "container": "a docker run of tests or checks in a container. BIG."\n',
                 "    It 'builds its docker run through Get-RunArgs and never hand-rolls one' {\n",
                 "    It 'gates on the process serving, not on docker run exiting 0' {\n",
                 "    $x | Should -Be $a0 -Because 'the filter is the only difference in what docker run is given'\n",
                 "Describe 'Every run-scoped docker create carries the owner label' {\n"):
        check(scan_text("t.ps1", line) == [], line)
    for line in ("sudo docker run --rm alpine:3.20 true\n", "RUN docker pull alpine:3.20\n",
                 "if docker run --rm alpine:3.20 true; then\n", "FOO=1 docker run --rm alpine:3.20 true\n",
                 "$out = docker run --rm alpine:3.20 true\n", "subprocess.run('docker run --rm alpine:3.20 true')\n",
                 "  - run: docker run --rm alpine:3.20 true\n", "$o = & docker run --rm alpine:3.20 true\n",
                 "$o = Invoke-Native { docker run --rm alpine:3.20 true }\n",
                 "docker exec $c docker run --rm alpine:3.20 true\n"):           # after a variable
        check(refs(scan_text("s.sh", line)) == ["alpine:3.20"], line)
    held = '  "content": "function Build {\\n    npm run build *>> $log\\n    docker build -t acme/app:local .\\n}"\n'
    check(refs(scan_text("cases/x.json", held)) == ["acme/app:local"])              # after an escaped line break


def case_spaced_bare_assignment_is_no_literal():   # R2
    check(unresolved(scan_text("x.ps1", "$image = if ($x) { 'a:1' } else { 'b:2' }\ndocker run $image\n")) == 1)
    check(refs(scan_text("x.sh", "IMAGE=alpine:3.20\ndocker run $IMAGE\n")) == ["alpine:3.20"])
    check(refs(scan_text("x.ps1", "$IMAGE = 'alpine:3.20'\ndocker run $IMAGE\n")) == ["alpine:3.20"])
    check(refs(scan_text("x.yml", "env:\n  IMG: alpine:3.20\nsteps:\n  - run: docker run $IMG\n")) == ["alpine:3.20"])
    ts = "function pull(ref: string) {\n  run(`docker pull ${ref}`);\n}\n"             # a type annotation is no literal
    check([(h.ref, h.text) for h in scan_text("s.ts", ts)] == [(None, "${ref}")])
    check(refs(scan_text("s.ts", "const IMG = { ref: 'alpine:3.20' };\nrun(`docker pull ${ref}`);\n")) == ["alpine:3.20"])


def case_more_option_values():   # R3
    check(refs(scan_text("s.sh", "docker run --rm --cpuset-cpus 0-2 -m 2g img sh\n")) == ["img"])
    check(refs(scan_text("s.sh", "docker run -dp 8080:80 img sh\n")) == ["img"])
    check(refs(scan_text("s.sh", "docker run -itd img sh\n")) == ["img"])           # a cluster ending in a flag
    check(unresolved(scan_text("s.ps1", "docker run -d --stop-timeout 120 $X | Out-Null\n")) == 1)


def case_escapes():   # R4
    line = '    "command": "docker run --rm -v \\"${vol}:/b\\" alpine sh -c \'test -d /b\'"\n'
    check(refs(scan_text("cases/x.json", line)) == ["alpine"])                      # \" in a JSON string is a quote
    check(scan_text("t.ps1", "$pull | Should -Not -Match '(?m)^docker pull \\$ref'\n") == [])   # \$ is no variable


def case_where_a_command_ends():   # R5
    check(scan_text("t.ps1", "if ($lines[$k] -match '^\\s*docker run --rm ') { $k }\n") == [])   # a quote never closed
    check(scan_text("m.py", "the seccomp profile off (docker run --security-opt seccomp=unconfined), as\n") == [])
    hits = scan_text("s.ts", "reject(new Error(`\\`docker pull ${ref}\\` exited ${code}`));\n")
    check([(h.ref, h.text) for h in hits] == [(None, "${ref}")])                      # an escaped backtick ends it
    check(refs(scan_text("s.sh", "x=$(docker run --rm alpine:3.20 cat /etc/os-release)\n")) == ["alpine:3.20"])
    check(unresolved(scan_text("s.ps1", "docker run --rm $($cfg.Image.Split('@')[0]) sh\n")) == 1)


def case_binary_and_stage_names_are_not_images():
    repo = git_repo({"bin/tool": b"\x7fELF\x00\x01FROM alpine", "Dockerfile": "FROM a:1 AS s\nFROM s\n",
                     "latin1.txt": b"caf\xe9\n"})
    tree = read_tree(repo, "HEAD")
    check("bin/tool" not in tree)
    check("latin1.txt" not in tree)                                # invalid UTF-8 is left out
    check(tree == {"Dockerfile": "FROM a:1 AS s\nFROM s\n"})
    check(refs(scan_text("Dockerfile", "FROM a:1 AS s\nFROM s\n")) == ["a:1"])


def case_is_scanned():
    check(not is_scanned("docs/x.ps1", []) and not is_scanned("README.md", []) and not is_scanned("CHANGELOG.md", []))
    check(not is_scanned(".superpowers/a.py", []) and not is_scanned("docker-images.json", []))
    check(not is_scanned("tests/fixtures/a.py", ["tests/fixtures/**"]) and is_scanned("tests/a.py", ["tests/fixtures/**"]))
    check(is_scanned("tests/Suite.Tests.ps1", []))          # tests ARE scanned (4.11)
    check(not is_scanned("src/docs/x.sh", []) and not is_scanned("CHANGELOG", []))
    check(is_scanned("sub/docker-images.json", []))           # only the root keep file is the keep file


def case_check_tree_findings():
    T = [tooling("ghcr.io/cross-rs/*:0.2.5")]
    check(kinds(check_tree("alpha", {"Dockerfile": "FROM alpine:3.20\n"}, T)) == ["no-keep-file"])
    check(check_tree("alpha", {"a.py": "print(1)\n"}, T) == [])  # names no image: no keep file needed
    files = {"Dockerfile": "FROM alpine:3.20\n", "s.sh": "docker pull ghcr.io/cross-rs/x:0.2.5\ndocker run $X\n",
             "docker-images.json": keep_with("alpine:3.20", "redis:7")}
    check(kinds(check_tree("alpha", files, T)) == ["unresolved", "lapsed"])   # cross-rs satisfied by the table; redis lapsed
    found = check_tree("alpha", files, T)
    check([(f.repo, f.detail, f.path, f.line) for f in found] == [("alpha", "$X", "s.sh", 2), ("alpha", "redis:7", "docker-images.json", None)])
    check(kinds(check_tree("alpha", {"Dockerfile": "FROM b:1\n", "docker-images.json": "{"}, T)) == ["malformed"])
    files2 = {"Dockerfile": "FROM node:24\n", "docker-images.json": keep_with()}
    check([(f.kind, f.path, f.line) for f in check_tree("alpha", files2, T)] == [("undeclared", "Dockerfile", 1)])
    check(check_tree("alpha", files2, T)[0].detail == "node:24")    # the ref only, never the kind (F6)
    built = {"images/p/Dockerfile": "FROM alpine:3.20\n", "docker-images.json": keep_with("alpine:3.20", build=("p/probe", "images/p/Dockerfile"))}
    check(check_tree("alpha", built, T) == [])                     # a build entry whose Dockerfile exists is not lapsed
    del built["images/p/Dockerfile"]; built["x.sh"] = "docker pull alpine:3.20\n"
    check(kinds(check_tree("alpha", built, T)) == ["lapsed"])     # Dockerfile gone, nothing names p/probe
    ignored = {"tests/fixtures/Dockerfile": "FROM node:24\n",
               "docker-images.json": json.dumps({"schema": 1, "images": [], "ignore": ["tests/fixtures/**"]})}
    check(check_tree("alpha", ignored, T) == [])                   # the keep file's ignore globs are honored
    check(all(f.kind in FINDING_KINDS for f in check_tree("alpha", files, T)))


def case_no_keep_file_tooling_only():   # F14
    T = [tooling("ghcr.io/cross-rs/*:0.2.5")]
    check(check_tree("alpha", {"s.sh": "docker pull ghcr.io/cross-rs/x:0.2.5\n"}, T) == [])   # tooling covers every ref
    mixed = {"Dockerfile": "FROM alpine:3.20\nFROM node:24\n", "s.sh": "docker run $X\n"}
    found = check_tree("alpha", mixed, T)
    check(kinds(found) == ["no-keep-file", "unresolved"])          # ONE no-keep-file, never undeclared
    check((found[0].detail, found[0].path, found[0].line) == ("alpine:3.20", "Dockerfile", 1))
    check(kinds(check_tree("alpha", {"s.sh": "docker pull ghcr.io/cross-rs/x:0.2.5\ndocker run $X\n"}, T)) == ["no-keep-file", "unresolved"])


def case_default_ref_fallbacks():
    check(default_ref(repo_with(origin_head="main")) == "refs/remotes/origin/main")
    check(default_ref(repo_with(remote="personal", branches=["main"])) == "refs/remotes/personal/main")   # remote not named origin
    check(default_ref(repo_with(origin_head=None, branches=["main"])) == "refs/remotes/origin/main")     # no origin/HEAD
    check(default_ref(repo_with(origin_head="feature-x", branches=["feature-x", "main"])) == "refs/remotes/origin/main")  # origin/HEAD on a feature branch
    check(default_ref(repo_with(origin_head=None, branches=["master"])) == "refs/remotes/origin/master")
    check(default_ref(repo_with(origin_head=None, branches=["dev"])) is None)
    check(default_ref(repo_with(origin_head="master", branches=["master", "main"])) == "refs/remotes/origin/master")
    check(default_ref(repo_with(origin_head=None, branches=["dev"], local=["main"])) == "refs/heads/main")
    check(default_ref(repo_with(origin_head=None, branches=["master"], local=["main"])) == "refs/heads/main")  # main before master
    check(default_ref(tempfile.gettempdir()) is None)               # not a repository


def case_check_repo_reads_the_default_branch_not_the_worktree():
    repo = repo_with(origin_head="main", files={"Dockerfile": "FROM alpine:3.20\n", "docker-images.json": keep_with("alpine:3.20")})
    git(repo, "checkout", "-q", "-b", "feature")                  # a feature branch checked out (F18)
    write(repo, "Dockerfile", "FROM node:24\n")
    git(repo, "commit", "-q", "-am", "feature")                   # committed on the feature branch
    write(repo, "Dockerfile", "FROM redis:7\n")                   # and an uncommitted edit on top
    check(check_repo(repo, tooling=[]) == [])
    found = check_repo(repo, ref="HEAD", tooling=[])               # the feature commit itself does differ
    check([(f.repo, f.kind, f.detail) for f in found] == [("alpha", "undeclared", "node:24"), ("alpha", "lapsed", "alpine:3.20")])
    check([(f.kind, f.repo) for f in check_repo(repo, ref="HEAD", tooling=[], name="beta")][:1] == [("undeclared", "beta")])
    none = check_repo(repo_with(origin_head=None, branches=["dev"]), tooling=[])
    check(kinds(none) == ["unreadable"] and none[0].repo == "alpha")
    check(kinds(check_repo(repo, ref="refs/heads/no-such-branch", tooling=[])) == ["unreadable"])


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
