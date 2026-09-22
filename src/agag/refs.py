"""`agrefs` — human-authored references, by name, at a pinned revision.

A *reference* is something a human made and published for agents to work
from: a story, an image, a template, a runnable example. It lives in a git
repository the human owns, and an agent reads it at one immutable revision
so that "the meadow composition" means the same bytes to Front, autolab,
forge and archsage, on this machine or another.

The identity of a reference is a string every agent can carry in a post,
a plan, a task or a report:

    <source>@<revision>[:<relative path>]      protoprey-refs@3f2a1b0:scenes/meadow/composition.png

`<source>` is a short name; what URL it stands for is a host fact and lives
in an ignored file, `refs.toml`, beside a cache directory `refs/`. Both sit
in the directory `AGREFS_HOME` names — an agent's `.local/` — or, with no
variable, the nearest `.local/refs.toml` above the working directory, so a
human can run the same command in their own clone.

    [[source]]
    name = "protoprey-refs"
    url = "http://<git host>/developer/protoprey-refs.git"
    about = "ProtoPrey references the Developer writes: stories, compositions, templates"

A snapshot is `git archive` of one commit, laid out under
`refs/<source>/<full sha>/` next to a mirror clone. Several revisions
coexist: a task that adopted one keeps it while a newer one is adopted
elsewhere, and nothing is ever rewritten in place. Image bytes are in the
snapshot, so no delivery link has to stay alive.

The verbs, all reached through the `agrefs` console script so a role whose
grant is `Bash(agrefs:*)` can get at every file on every harness:

    agrefs list                              every source, its cached revisions and its latest fetched head
    agrefs sync <source>[@<rev>]             fetch, resolve the revision (default: latest), lay out its snapshot
    agrefs show <source>@<rev>[:<path>]      a text file, a directory listing, or what a binary file is
    agrefs path <source>@<rev>[:<path>]      the absolute path of the snapshot (or one file in it)
    agrefs search <source>@<rev> <terms…>    lines holding every term
    agrefs revision <source>                 the latest published revision (fetches)
    agrefs changes <source>@<old>..<new>     the files that differ between two revisions

`show` never pretends to have read a binary: it says the kind, the size,
the pixel size for an image, and the path — and the path is what a harness's
own image reader takes. A revision must be named: `latest` is a name too,
and every answer prints what it resolved to, so a run records a commit, not
a moving target.
"""

from __future__ import annotations

import argparse
import io
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

HOME_VARIABLE = "AGREFS_HOME"
CONFIG_NAME = "refs.toml"
CACHE_NAME = "refs"
MIRROR_NAME = "mirror.git"
SCHEMA = "agag.refs.v1"
LATEST = "latest"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SEARCH_SUFFIXES = (".md", ".txt", ".py", ".toml", ".json", ".yaml", ".yml", ".gd", ".ink", ".tscn", ".cfg", ".csv", ".html", ".js", ".ts")
SEARCH_LIMIT = 200
SHOW_LIMIT = 60_000
GIT_TIMEOUT = 120

__all__ = [
    "CONFIG_NAME",
    "HOME_VARIABLE",
    "LATEST",
    "Ref",
    "RefsError",
    "Source",
    "build_parser",
    "changes",
    "describe",
    "home",
    "listing",
    "load_sources",
    "main",
    "parse_ref",
    "path_of",
    "resolve",
    "search",
    "show",
    "sync",
]


class RefsError(RuntimeError):
    """A name that resolves to nothing, or a repository git cannot reach."""


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    about: str
    cache: Path

    @property
    def mirror(self) -> Path:
        return self.cache / MIRROR_NAME

    def snapshot(self, sha: str) -> Path:
        return self.cache / sha


@dataclass(frozen=True)
class Ref:
    """`<source>@<revision>[:<path>]`, as typed — the revision unresolved."""

    source: str
    revision: str | None
    path: str

    def __str__(self) -> str:
        head = f"{self.source}@{self.revision}" if self.revision else self.source
        return f"{head}:{self.path}" if self.path else head


# --- where the config and the cache are --------------------------------------


def home(environ=None, cwd: Path | None = None) -> Path:
    """The directory holding `refs.toml` and `refs/`.

    `AGREFS_HOME` wins (a run is handed it by the listener); otherwise the
    nearest `.local/` above the working directory that holds a `refs.toml`,
    so a human standing in their own checkout gets their own configuration.
    Nothing found means the working directory's `.local/`, and `list` then
    says the file is missing rather than crashing.
    """
    environ = os.environ if environ is None else environ
    value = str(environ.get(HOME_VARIABLE, "")).strip()
    if value:
        return Path(value).expanduser()
    here = (Path.cwd() if cwd is None else cwd).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".local"
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    return here / ".local"


def load_sources(base: Path | None = None) -> list[Source]:
    """The sources `refs.toml` names, in file order; none when there is no file."""
    base = home() if base is None else base
    config = base / CONFIG_NAME
    if not config.is_file():
        return []
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RefsError(f"{config}: {error}") from error
    sources: list[Source] = []
    for entry in data.get("source", []) or []:
        name = str(entry.get("name", "")).strip()
        url = str(entry.get("url", "")).strip()
        if not name or not url:
            raise RefsError(f"{config}: every [[source]] needs a name and a url")
        if not NAME_RE.match(name):
            raise RefsError(f"{config}: {name!r} is not a source name ({NAME_RE.pattern})")
        sources.append(Source(name, url, str(entry.get("about", "")).strip(), base / CACHE_NAME / name))
    return sources


def source_named(name: str, sources: list[Source] | None = None) -> Source:
    found = load_sources() if sources is None else sources
    for source in found:
        if source.name == name:
            return source
    known = ", ".join(s.name for s in found) or f"none configured ({CONFIG_NAME} names them)"
    raise RefsError(f"no source named {name!r} (sources: {known})")


# --- the reference string ------------------------------------------------------


def parse_ref(text: str) -> Ref:
    """`<source>[@<rev>][:<path>]` → Ref. A path may not climb out."""
    text = str(text or "").strip()
    head, _, path = text.partition(":")
    source, _, revision = head.partition("@")
    source, revision, path = source.strip(), revision.strip(), path.strip().strip("/")
    if not source:
        raise RefsError(f"{text!r}: a reference starts with a source name")
    if path and any(part in ("..", "") for part in path.split("/")):
        raise RefsError(f"{text!r}: the path climbs out of the source")
    return Ref(source, revision or None, path)


# --- git ---------------------------------------------------------------------------


def _git(*arguments: str, cwd: Path | None = None, timeout: float = GIT_TIMEOUT) -> str:
    try:
        done = subprocess.run(
            ["git", *arguments], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RefsError(f"git {arguments[0]} failed: {error}") from error
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise RefsError(f"git {' '.join(arguments[:2])} failed: {detail[-1] if detail else done.returncode}")
    return done.stdout


def fetch(source: Source) -> None:
    """Clone the mirror once, then keep every branch level with the remote."""
    if (source.mirror / "HEAD").is_file():
        _git("fetch", "--prune", "origin", cwd=source.mirror)
        return
    source.cache.mkdir(parents=True, exist_ok=True)
    _git("clone", "--mirror", source.url, str(source.mirror))


def _known(source: Source, revision: str) -> str | None:
    """The full sha `revision` names in the mirror, or None when unknown."""
    if not (source.mirror / "HEAD").is_file():
        return None
    try:
        return _git("rev-parse", "--verify", f"{revision}^{{commit}}", cwd=source.mirror).strip()
    except RefsError:
        return None


def resolve(ref: Ref | str, *, sources: list[Source] | None = None, fetch_first: bool | None = None) -> tuple[Source, str]:
    """(source, full sha) for a reference. `latest` fetches; a sha is looked
    up in the mirror and fetched for only if unknown there."""
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    source = source_named(ref.source, sources)
    if not ref.revision:
        raise RefsError(f"{ref}: name a revision — `{ref.source}@<sha>` or `{ref.source}@{LATEST}`")
    if ref.revision == LATEST or fetch_first:
        fetch(source)
        target = "HEAD" if ref.revision == LATEST else ref.revision
        sha = _known(source, target)
    else:
        sha = _known(source, ref.revision)
        if sha is None:
            fetch(source)
            sha = _known(source, ref.revision)
    if sha is None:
        raise RefsError(f"{ref}: no such revision in {source.name}")
    return source, sha


def _export(source: Source, sha: str) -> Path:
    """Lay out `git archive <sha>` under the cache, once; return the directory."""
    target = source.snapshot(sha)
    if target.is_dir():
        return target
    try:
        done = subprocess.run(
            ["git", "archive", "--format=tar", sha], cwd=str(source.mirror),
            capture_output=True, timeout=GIT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RefsError(f"git archive failed: {error}") from error
    if done.returncode != 0:
        raise RefsError(f"git archive {sha[:7]} failed: {done.stderr.decode('utf-8', 'replace').strip()[:200]}")
    staging = Path(tempfile.mkdtemp(prefix=f".{sha[:7]}-", dir=source.cache))
    try:
        with tarfile.open(fileobj=io.BytesIO(done.stdout), mode="r:") as archive:
            archive.extractall(staging, filter="data")
        (staging / ".agrefs-revision").write_text(sha + "\n", encoding="utf-8")
        os.rename(staging, target)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        if not target.is_dir():
            raise
    return target


def sync(ref: Ref | str, *, sources: list[Source] | None = None) -> tuple[Source, str, Path]:
    """Fetch, resolve and lay out one revision (`latest` by default)."""
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    if not ref.revision:
        ref = Ref(ref.source, LATEST, ref.path)
    source, sha = resolve(ref, sources=sources, fetch_first=True)
    return source, sha, _export(source, sha)


def snapshot(ref: Ref | str, *, sources: list[Source] | None = None) -> tuple[Source, str, Path]:
    """The snapshot directory for a reference, laid out if it is not yet."""
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    source, sha = resolve(ref, sources=sources)
    return source, sha, _export(source, sha)


def _inside(root: Path, relative: str) -> Path:
    root = root.resolve()
    target = (root / relative).resolve() if relative else root
    if target != root and root not in target.parents:
        raise RefsError(f"{relative!r} reaches outside the snapshot")
    return target


# --- what one thing is -------------------------------------------------------------


def _image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size of a PNG, GIF or JPEG header, without any image library."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                return None
            marker, length = data[index + 1], struct.unpack(">H", data[index + 2:index + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return width, height
            index += 2 + length
    return None


def describe(path: Path) -> str:
    """`binary: image/png, 1920x1080, 2.1 MB, at /…` — what `show` says of a file it does not print."""
    size = path.stat().st_size
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as handle:
        head = handle.read(64 * 1024)
    pixels = _image_size(head)
    parts = [kind]
    if pixels:
        parts.append(f"{pixels[0]}x{pixels[1]}")
    parts.append(f"{size:,} bytes")
    return f"binary: {', '.join(parts)}, at {path}"


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def show(ref: Ref | str, *, sources: list[Source] | None = None) -> str:
    """A text file, a directory's entries, or a binary file's description."""
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    source, sha, root = snapshot(ref, sources=sources)
    target = _inside(root, ref.path)
    resolved = f"{source.name}@{sha[:7]}" + (f":{ref.path}" if ref.path else "")
    if not target.exists():
        raise RefsError(f"{ref}: no such file in {source.name}@{sha[:7]}")
    if target.is_dir():
        lines = [f"{resolved}/ (revision {sha})"]
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if child.name == ".agrefs-revision":
                continue
            lines.append(f"  {child.name}/" if child.is_dir() else f"  {child.name}  ({child.stat().st_size:,} bytes)")
        return "\n".join(lines)
    if not _is_text(target):
        return f"{resolved}: {describe(target)}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > SHOW_LIMIT:
        text = text[:SHOW_LIMIT] + f"\n… truncated at {SHOW_LIMIT} characters; `agrefs path {resolved}` for the whole file"
    return f"{resolved} (revision {sha})\n{text}"


def path_of(ref: Ref | str, *, sources: list[Source] | None = None) -> Path:
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    _, _, root = snapshot(ref, sources=sources)
    target = _inside(root, ref.path)
    if not target.exists():
        raise RefsError(f"{ref}: no such file")
    return target


# --- search, listing, changes --------------------------------------------------------


def search(ref: Ref | str, terms, *, sources: list[Source] | None = None, limit: int = SEARCH_LIMIT) -> list[str]:
    """`<source>@<sha>:<path>:<line>: <text>` for lines holding every term."""
    wanted = [str(term).lower() for term in terms if str(term).strip()]
    if not wanted:
        return []
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    source, sha, root = snapshot(ref, sources=sources)
    base = _inside(root, ref.path)
    files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in SEARCH_SUFFIXES)
    hits: list[str] = []
    for path in files:
        if not _is_text(path):
            continue
        rel = path.relative_to(root).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            lowered = line.lower()
            if all(term in lowered for term in wanted):
                hits.append(f"{source.name}@{sha[:7]}:{rel}:{number}: {line.strip()}")
                if len(hits) >= limit:
                    hits.append(f"… stopped at {limit} hits; add a term to narrow")
                    return hits
    return hits


def _commit_line(source: Source, sha: str) -> str:
    try:
        return _git("log", "-1", "--format=%h %cs %s", sha, cwd=source.mirror).strip()
    except RefsError:
        return sha[:7]


def listing(sources: list[Source] | None = None, base: Path | None = None) -> str:
    """What `agrefs list` prints: every source, whole, without fetching."""
    base = home() if base is None else base
    found = load_sources(base) if sources is None else sources
    if not found:
        return (
            f"no reference sources are configured here ({base / CONFIG_NAME} is missing); "
            "a [[source]] needs a name, a url and one line about it"
        )
    blocks = []
    for source in found:
        lines = [f"## {source.name}"]
        if source.about:
            lines.append(source.about)
        head = _known(source, "HEAD")
        lines.append(f"latest fetched: {_commit_line(source, head) if head else '(never fetched: `agrefs sync ' + source.name + '`)'}")
        cached = sorted(p for p in source.cache.iterdir() if SHA_RE.match(p.name)) if source.cache.is_dir() else []
        if cached:
            lines.append("snapshots on this host:")
            lines.extend(f"  {source.name}@{p.name[:7]}  {_commit_line(source, p.name)}" for p in cached)
        lines.append(
            f"read: `agrefs show {source.name}@<rev>:<path>` · file: `agrefs path {source.name}@<rev>:<path>` · "
            f"newest: `agrefs sync {source.name}`"
        )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def changes(spec: str, *, sources: list[Source] | None = None) -> str:
    """`<source>@<old>..<new>` → the files that differ, one per line, with status."""
    head, _, new = spec.partition("..")
    ref = parse_ref(head)
    if not ref.revision or not new.strip():
        raise RefsError(f"{spec!r}: write `<source>@<old>..<new>`")
    source, old_sha = resolve(ref, sources=sources)
    _, new_sha = resolve(Ref(ref.source, new.strip(), ""), sources=sources)
    diff = _git("diff", "--name-status", old_sha, new_sha, cwd=source.mirror).strip()
    log = _git("log", "--format=%h %cs %an: %s", f"{old_sha}..{new_sha}", cwd=source.mirror).strip()
    lines = [f"{source.name}@{old_sha[:7]}..{new_sha[:7]}"]
    lines.append(log if log else "(no commits between them)")
    lines.append(diff if diff else "(no file differs)")
    return "\n".join(lines)


# --- the command ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agrefs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Human-authored references, read by name at a pinned revision.\n\n"
            "A reference is `<source>@<revision>[:<path>]`. The revision is a git commit\n"
            "(short form is fine) or the word `latest`; every answer prints the commit it\n"
            "resolved to, and that is what you record and pass on — never `latest`.\n"
            "A source's files keep the human's folder structure; `show` prints text,\n"
            "lists a directory, and describes a binary (kind, pixel size, bytes, path)\n"
            "without pretending to have read it. To look at an image, read the file at\n"
            "`agrefs path …` with your own image-capable reader. Originals are read-only:\n"
            "derivatives go into your own workspace, and a departure from a reference is\n"
            "something to say, not to hide."
        ),
        epilog=(
            "examples\n"
            "  agrefs list\n"
            "  agrefs sync protoprey-refs                  newest published revision, laid out\n"
            "  agrefs show protoprey-refs@3f2a1b0           the tree at that revision\n"
            "  agrefs show protoprey-refs@3f2a1b0:README.md\n"
            "  agrefs path protoprey-refs@3f2a1b0:images/meadow.png\n"
            "  agrefs search protoprey-refs@3f2a1b0 meadow dusk\n"
            "  agrefs changes protoprey-refs@3f2a1b0..latest\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every source, its cached revisions, its latest fetched head").set_defaults(run=_run_list)
    p = sub.add_parser("sync", help="fetch and lay out a revision (default: latest)")
    p.add_argument("ref", help="<source>[@<rev>]")
    p.set_defaults(run=_run_sync)
    p = sub.add_parser("show", help="a text file, a directory listing, or what a binary is")
    p.add_argument("ref", help="<source>@<rev>[:<path>]")
    p.set_defaults(run=_run_show)
    p = sub.add_parser("path", help="the absolute path of a snapshot or one file in it")
    p.add_argument("ref", help="<source>@<rev>[:<path>]")
    p.set_defaults(run=_run_path)
    p = sub.add_parser("search", help="lines holding every term, under a path")
    p.add_argument("ref", help="<source>@<rev>[:<path>]")
    p.add_argument("terms", nargs="+")
    p.set_defaults(run=_run_search)
    p = sub.add_parser("revision", help="the latest published revision (fetches)")
    p.add_argument("source")
    p.set_defaults(run=_run_revision)
    p = sub.add_parser("changes", help="files that differ between two revisions")
    p.add_argument("spec", help="<source>@<old>..<new>")
    p.set_defaults(run=_run_changes)
    return parser


def _run_list(args, out) -> int:
    print(listing(), file=out)
    return 0


def _run_sync(args, out) -> int:
    source, sha, root = sync(args.ref)
    print(f"{source.name}@{sha[:7]}  ({sha})  {_commit_line(source, sha)}", file=out)
    print(f"snapshot: {root}", file=out)
    return 0


def _run_show(args, out) -> int:
    print(show(args.ref), file=out)
    return 0


def _run_path(args, out) -> int:
    print(path_of(args.ref), file=out)
    return 0


def _run_search(args, out) -> int:
    hits = search(args.ref, args.terms)
    print("\n".join(hits) if hits else "no line holds every term", file=out)
    return 0


def _run_revision(args, out) -> int:
    source, sha = resolve(Ref(args.source, LATEST, ""))
    print(f"{source.name}@{sha[:7]}  ({sha})  {_commit_line(source, sha)}", file=out)
    return 0


def _run_changes(args, out) -> int:
    print(changes(args.spec), file=out)
    return 0


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = build_parser().parse_args(argv)
    try:
        return args.run(args, out)
    except RefsError as error:
        print(f"agrefs: {error}", file=err)
        return 2


if __name__ == "__main__":
    sys.exit(main())
