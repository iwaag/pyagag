"""`agrefs` — human-authored references, by name, at a pinned revision.

A *reference* is something a human made and published for agents to work
from: a story, an image, a template, a runnable example. It lives in a git
repository the human owns, and an agent reads it at one immutable revision
so that "the meadow composition" means the same bytes to Front, autolab,
forge and archsage, on this machine or another.

The identity of a reference is a string every agent can carry in a post,
a plan, a task or a report:

    <source>@<revision>[:<relative path>]      protoprey-refs@3f2a1b0:scenes/meadow/composition.png

**Which sources exist is read from a shared catalog** (`give_context_easier`
p1): one small git repository whose `catalog.toml` lists every context
repository the human has registered, so registering a new one reaches every
agent without editing anything per agent.

    schema = "agag.refs-catalog.v1"

    [[source]]
    id = "protoprey-refs"                      # the <source> in every reference; never renamed
    name = "ProtoPrey references"              # display name, freely edited
    description = "Stories, compositions and templates for ProtoPrey"
    repository = "developer/protoprey-refs"    # relative to the catalog's own git host
    branch = "main"
    status = "active"                          # "archived": still resolvable, not listed by default

`repository` is resolved against the URL the catalog itself was read from,
so two hosts that reach the same git server under different names both
work; `url` names an absolute repository instead.

Where the catalog is, is a host fact, configured once per host in
`~/.config/agag/refs.toml`:

    [catalog]
    url = "http://<git host>/developer/context-catalog.git"

An instance's own `refs.toml` (in the directory `AGREFS_HOME` names — an
agent's `.local/` — or the nearest `.local/refs.toml` above the working
directory) may name another `[catalog]` and may add explicit `[[source]]`
entries (`name`, `url`, `about`), which win over a catalog entry of the same
id. `AGREFS_CATALOG` overrides the catalog URL for one process.

The catalog is cached beside the snapshots (`refs/_catalog/`) and refreshed
when older than `refresh_seconds` (60), and at once when a name is not in
it — a newly registered source is found without anybody restarting
anything. When the git server cannot be reached the last catalog read is
used and every answer says it is *last-known*; with nothing read ever it is
*unavailable*. A pinned reference whose commit was fetched before keeps
resolving through an outage.

A snapshot is `git archive` of one commit, laid out under
`refs/<source>/<full sha>/` next to a mirror clone. Several revisions
coexist: a task that adopted one keeps it while a newer one is adopted
elsewhere, and nothing is ever rewritten in place. Content is fetched on
demand, one source at a time.

The verbs, all reached through the `agrefs` console script so a role whose
grant is `Bash(agrefs:*)` can get at every file on every harness:

    agrefs list [--all]                      every source: name, what it is for, what is cached
    agrefs sync <source>[@<rev>]             fetch, resolve the revision (default: latest), lay out its snapshot
    agrefs show <source>@<rev>[:<path>]      a text file, a directory listing, or what a binary file is
    agrefs path <source>@<rev>[:<path>]      the absolute path of the snapshot (or one file in it)
    agrefs search <source>@<rev> <terms…>    lines holding every term
    agrefs revision <source>                 the latest published revision (fetches)
    agrefs changes <source>@<old>..<new>     the files that differ between two revisions
    agrefs catalog                           where the catalog is, and whether this read is current

`show` never pretends to have read a binary: it says the kind, the size,
the pixel size for an image, and the path — and the path is what a harness's
own image reader takes. A revision must be named: `latest` is a name too,
and every answer prints what it resolved to, so a run records a commit, not
a moving target.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HOME_VARIABLE = "AGREFS_HOME"
CATALOG_VARIABLE = "AGREFS_CATALOG"
HOST_CONFIG_VARIABLE = "AGREFS_HOST_CONFIG"
CONFIG_NAME = "refs.toml"
CACHE_NAME = "refs"
CATALOG_CACHE = "_catalog"
CATALOG_FILE = "catalog.toml"
MIRROR_NAME = "mirror.git"
SCHEMA = "agag.refs.v1"
CATALOG_SCHEMA = "agag.refs-catalog.v1"
LATEST = "latest"
ACTIVE = "active"
ARCHIVED = "archived"
STATUSES = (ACTIVE, ARCHIVED)
DEFAULT_REFRESH_SECONDS = 60
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SEARCH_SUFFIXES = (".md", ".txt", ".py", ".toml", ".json", ".yaml", ".yml", ".gd", ".ink", ".tscn", ".cfg", ".csv", ".html", ".js", ".ts")
SEARCH_LIMIT = 200
SHOW_LIMIT = 60_000
GIT_TIMEOUT = 120

# Catalog read states.
CURRENT = "current"
LAST_KNOWN = "last-known"
UNAVAILABLE = "unavailable"
UNCONFIGURED = "unconfigured"

__all__ = [
    "ACTIVE",
    "ARCHIVED",
    "CATALOG_SCHEMA",
    "CATALOG_VARIABLE",
    "CONFIG_NAME",
    "Catalog",
    "HOME_VARIABLE",
    "HOST_CONFIG_VARIABLE",
    "LATEST",
    "Ref",
    "RefsError",
    "Source",
    "build_parser",
    "catalog",
    "changes",
    "describe",
    "home",
    "host_config_path",
    "listing",
    "load_sources",
    "main",
    "parse_catalog",
    "parse_ref",
    "path_of",
    "remote_head",
    "resolve",
    "search",
    "show",
    "source_named",
    "sync",
    "tree",
]


class RefsError(RuntimeError):
    """A name that resolves to nothing, or a repository git cannot reach."""


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    about: str
    cache: Path
    #: The display name; the id (`name`) is what a reference carries.
    title: str = ""
    branch: str = ""
    status: str = ACTIVE
    #: `catalog` or `local` (an explicit `[[source]]` in a `refs.toml`).
    origin: str = "local"
    #: A browser link to the repository, when the host is a web git server.
    web: str | None = None

    @property
    def mirror(self) -> Path:
        return self.cache / MIRROR_NAME

    def snapshot(self, sha: str) -> Path:
        return self.cache / sha

    @property
    def archived(self) -> bool:
        return self.status == ARCHIVED

    @property
    def head_ref(self) -> str:
        """What `latest` means for this source: its branch, or the remote default."""
        return f"refs/heads/{self.branch}" if self.branch else "HEAD"


@dataclass(frozen=True)
class Ref:
    """`<source>@<revision>[:<path>]`, as typed — the revision unresolved."""

    source: str
    revision: str | None
    path: str

    def __str__(self) -> str:
        head = f"{self.source}@{self.revision}" if self.revision else self.source
        return f"{head}:{self.path}" if self.path else head


@dataclass
class Catalog:
    """One read of the shared catalog, and how fresh it is.

    `state` is `current` (read from the git server within the refresh
    interval), `last-known` (the server could not be reached or its newest
    catalog does not parse; this is the last good one), `unavailable`
    (configured, never read) or `unconfigured`.
    """

    url: str | None
    state: str
    revision: str | None = None
    fetched_at: float | None = None
    error: str | None = None
    sources: list[Source] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def payload(self) -> dict:
        return {
            "url": self.url, "state": self.state, "revision": self.revision,
            "fetched_at": self.fetched_at, "error": self.error, "issues": list(self.issues),
        }

    def summary(self) -> str:
        if self.state == UNCONFIGURED:
            return "catalog: not configured (a [catalog] url in ~/.config/agag/refs.toml names it)"
        where = f" at {self.revision[:7]}" if self.revision else ""
        when = f", read {_age(self.fetched_at)}" if self.fetched_at else ""
        line = f"catalog: {self.state}{where}{when} — {self.url}"
        if self.error:
            line += f"\n  ({self.error})"
        return line


# --- where the config and the cache are --------------------------------------


def home(environ=None, cwd: Path | None = None) -> Path:
    """The directory holding `refs.toml` and `refs/`.

    `AGREFS_HOME` wins (a run is handed it by the listener); otherwise the
    nearest `.local/` above the working directory that holds a `refs.toml`,
    so a human standing in their own checkout gets their own configuration.
    Nothing found means the working directory's `.local/`; the host catalog
    still applies there.
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


def host_config_path(environ=None) -> Path:
    """`~/.config/agag/refs.toml` (or `$XDG_CONFIG_HOME/agag/refs.toml`), unless
    `AGREFS_HOST_CONFIG` names another file."""
    environ = os.environ if environ is None else environ
    value = str(environ.get(HOST_CONFIG_VARIABLE, "")).strip()
    if value:
        return Path(value).expanduser()
    base = str(environ.get("XDG_CONFIG_HOME", "")).strip()
    return (Path(base).expanduser() if base else Path.home() / ".config") / "agag" / CONFIG_NAME


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RefsError(f"{path}: {error}") from error


@dataclass(frozen=True)
class _Settings:
    catalog_url: str | None
    refresh_seconds: float
    local: list[dict]
    config: Path


def _settings(base: Path, environ=None) -> _Settings:
    environ = os.environ if environ is None else environ
    config = base / CONFIG_NAME
    instance = _read_toml(config)
    host = _read_toml(host_config_path(environ))
    url, refresh = None, DEFAULT_REFRESH_SECONDS
    for table in (host.get("catalog"), instance.get("catalog")):
        if isinstance(table, dict):
            url = str(table.get("url") or "").strip() or url
            if table.get("refresh_seconds") is not None:
                try:
                    refresh = float(table["refresh_seconds"])
                except (TypeError, ValueError) as error:
                    raise RefsError(f"[catalog] refresh_seconds must be a number: {error}") from error
    override = str(environ.get(CATALOG_VARIABLE, "")).strip()
    url = override or url
    return _Settings(url, refresh, list(instance.get("source", []) or []), config)


def _explicit_sources(settings: _Settings, base: Path) -> list[Source]:
    sources: list[Source] = []
    for entry in settings.local:
        name = str(entry.get("name", "")).strip()
        url = str(entry.get("url", "")).strip()
        if not name or not url:
            raise RefsError(f"{settings.config}: every [[source]] needs a name and a url")
        if not NAME_RE.match(name):
            raise RefsError(f"{settings.config}: {name!r} is not a source name ({NAME_RE.pattern})")
        about = str(entry.get("about", "")).strip()
        sources.append(Source(name, url, about, base / CACHE_NAME / name, title=str(entry.get("title", "")).strip(),
                              branch=str(entry.get("branch", "")).strip(), origin="local"))
    return sources


# --- the catalog ---------------------------------------------------------------


def _is_local(url: str) -> bool:
    return "://" not in url and not re.match(r"^[\w.-]+@[\w.-]+:", url)


def repository_url(catalog_url: str, repository: str) -> tuple[str, str | None]:
    """(git URL, web URL) of `owner/name` on the catalog's own host."""
    repository = repository.strip().strip("/")
    if repository.endswith(".git"):
        repository = repository[: -len(".git")]
    if _is_local(catalog_url):
        base = Path(catalog_url.removeprefix("file://")).expanduser()
        base = base.parent.parent
        plain, bare = base / repository, base / f"{repository}.git"
        return str(bare if bare.exists() and not plain.exists() else plain), None
    parts = urlsplit(catalog_url)
    segments = [one for one in parts.path.split("/") if one]
    prefix = "/".join(segments[:-2])
    path = "/" + "/".join(one for one in (prefix, repository) if one)
    web = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return web + ".git", web


def web_url(url: str) -> str | None:
    if _is_local(url) or not url.startswith(("http://", "https://")):
        return None
    return url[: -len(".git")] if url.endswith(".git") else url


def parse_catalog(text: str, catalog_url: str, cache_root: Path) -> tuple[list[Source], list[str]]:
    """The sources a `catalog.toml` names, and what was wrong with it.

    A malformed entry, an unknown status or a duplicate id is reported and
    skipped rather than failing the whole catalog — one bad registration must
    not hide every other source from every agent. An unreadable file or a
    wrong schema does raise: that catalog is not usable at all.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise RefsError(f"{CATALOG_FILE} is not valid TOML: {error}") from error
    if data.get("schema") != CATALOG_SCHEMA:
        raise RefsError(f"{CATALOG_FILE} declares schema {data.get('schema')!r}, not {CATALOG_SCHEMA!r}")
    sources: list[Source] = []
    issues: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(data.get("source", []) or [], 1):
        if not isinstance(entry, dict):
            issues.append(f"entry {index} is not a table")
            continue
        ident = str(entry.get("id", "")).strip()
        if not NAME_RE.match(ident):
            issues.append(f"entry {index}: {ident!r} is not a source id ({NAME_RE.pattern})")
            continue
        if ident in seen:
            issues.append(f"duplicate id {ident!r} (entry {index}); the first entry is used")
            continue
        status = str(entry.get("status", ACTIVE)).strip() or ACTIVE
        if status not in STATUSES:
            issues.append(f"{ident}: status {status!r} is not one of {', '.join(STATUSES)}")
            continue
        branch = str(entry.get("branch", "")).strip()
        if branch and not BRANCH_RE.match(branch):
            issues.append(f"{ident}: branch {branch!r} is not a branch name")
            continue
        repository = str(entry.get("repository", "")).strip()
        url = str(entry.get("url", "")).strip()
        if url:
            web = web_url(url)
        elif repository and all(part not in ("", ".", "..") for part in repository.strip("/").split("/")):
            url, web = repository_url(catalog_url, repository)
        else:
            issues.append(f"{ident}: needs a repository (owner/name) or a url")
            continue
        seen.add(ident)
        sources.append(Source(
            ident, url, str(entry.get("description", "")).strip(), cache_root / ident,
            title=str(entry.get("name", "")).strip(), branch=branch, status=status, origin="catalog", web=web,
        ))
    return sources, issues


@contextlib.contextmanager
def _locked(directory: Path):
    """One fetcher per cache directory: runs of one instance share it."""
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _state_file(root: Path) -> Path:
    return root / "state.json"


def _read_state(root: Path) -> dict:
    try:
        data = json.loads(_state_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(root: Path, state: dict) -> None:
    handle = tempfile.NamedTemporaryFile("w", dir=root, prefix=".state.", delete=False, encoding="utf-8")
    with handle:
        json.dump(state, handle, indent=2)
    os.replace(handle.name, _state_file(root))


def catalog(base: Path | None = None, *, refresh: bool | None = None, environ=None, now: float | None = None) -> Catalog:
    """Read the shared catalog, fetching it when due.

    `refresh=None` fetches when the cached read is older than the refresh
    interval, `True` always, `False` never (offline reads for a pinned
    reference). A failed fetch or an unusable newest catalog falls back to
    the last good one and says so.
    """
    base = home(environ) if base is None else base
    settings = _settings(base, environ)
    if not settings.catalog_url:
        return Catalog(None, UNCONFIGURED)
    url = settings.catalog_url
    root = base / CACHE_NAME / CATALOG_CACHE
    content_root = base / CACHE_NAME
    now = time.time() if now is None else now
    with _locked(root):
        state = _read_state(root)
        if state.get("url") != url:
            # Another catalog was configured: its cache is not this one's.
            if (root / MIRROR_NAME).exists():
                shutil.rmtree(root / MIRROR_NAME, ignore_errors=True)
            state = {"url": url}
        fetched_at = state.get("fetched_at")
        due = refresh is True or (refresh is None and (not fetched_at or now - float(fetched_at) >= settings.refresh_seconds))
        error = None
        if due:
            try:
                _mirror_fetch(url, root / MIRROR_NAME)
                head = _git("rev-parse", "--verify", "HEAD^{commit}", cwd=root / MIRROR_NAME).strip()
                try:
                    text = _git("show", f"{head}:{CATALOG_FILE}", cwd=root / MIRROR_NAME)
                except RefsError as missing:
                    raise RefsError(f"{CATALOG_FILE} is missing at {head[:7]}") from missing
                parse_catalog(text, url, content_root)
                state.update({"good": head, "fetched_at": now, "error": None})
            except RefsError as failure:
                error = str(failure)
                state.update({"error": error, "error_at": now})
            _write_state(root, state)
        good = state.get("good")
        if not good:
            return Catalog(url, UNAVAILABLE, error=error or state.get("error") or "never read")
        try:
            text = _git("show", f"{good}:{CATALOG_FILE}", cwd=root / MIRROR_NAME)
            sources, issues = parse_catalog(text, url, content_root)
        except RefsError as failure:
            return Catalog(url, UNAVAILABLE, error=str(failure))
        # The newest attempt decides the word: a failed one leaves the last
        # good read in use, and the answer says so.
        return Catalog(url, LAST_KNOWN if state.get("error") else CURRENT, revision=good,
                       fetched_at=state.get("fetched_at"), error=state.get("error"),
                       sources=sources, issues=issues)


def load_sources(base: Path | None = None, *, refresh: bool | None = None, environ=None,
                 with_catalog: bool = False):
    """Every source this instance can read: explicit ones, then the catalog's.

    An explicit `[[source]]` wins over a catalog entry with the same id (and
    the override is reported among the catalog's issues).
    """
    base = home(environ) if base is None else base
    settings = _settings(base, environ)
    explicit = _explicit_sources(settings, base)
    found = catalog(base, refresh=refresh, environ=environ)
    names = {source.name for source in explicit}
    merged = list(explicit)
    for source in found.sources:
        if source.name in names:
            found.issues.append(f"{source.name}: the explicit source in {settings.config} overrides the catalog entry")
            continue
        merged.append(source)
    return (merged, found) if with_catalog else merged


def source_named(name: str, sources: list[Source] | None = None, *, base: Path | None = None) -> Source:
    """The source with this id; a name not yet in the cached catalog reads it again first."""
    found = load_sources(base) if sources is None else sources
    for source in found:
        if source.name == name:
            return source
    if sources is None:
        fresh, state = load_sources(base, refresh=True, with_catalog=True)
        for source in fresh:
            if source.name == name:
                return source
        known = ", ".join(s.name for s in fresh if not s.archived) or "none"
        note = f"; catalog {state.state}" + (f" ({state.error})" if state.error else "") if state.url else ""
        raise RefsError(f"no source named {name!r} (sources: {known}{note})")
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
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(
            ["git", *arguments], cwd=str(cwd) if cwd else None, env=env,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RefsError(f"git {arguments[0]} failed: {error}") from error
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise RefsError(f"git {' '.join(arguments[:2])} failed: {detail[-1] if detail else done.returncode}")
    return done.stdout


def _mirror_fetch(url: str, mirror: Path) -> None:
    if (mirror / "HEAD").is_file():
        _git("remote", "set-url", "origin", url, cwd=mirror)
        _git("fetch", "--prune", "origin", cwd=mirror)
        return
    mirror.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".mirror-", dir=mirror.parent))
    try:
        _git("clone", "--mirror", url, str(staging / "m"))
        os.rename(staging / "m", mirror)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def fetch(source: Source) -> None:
    """Clone the mirror once, then keep every branch level with the remote."""
    with _locked(source.cache):
        _mirror_fetch(source.url, source.mirror)


def _has_commits(source: Source) -> bool:
    try:
        return bool(_git("for-each-ref", "--count=1", "--format=%(objectname)", "refs/", cwd=source.mirror).strip())
    except RefsError:
        return False


def _known(source: Source, revision: str) -> str | None:
    """The full sha `revision` names in the mirror, or None when unknown."""
    if not (source.mirror / "HEAD").is_file():
        return None
    try:
        return _git("rev-parse", "--verify", f"{revision}^{{commit}}", cwd=source.mirror).strip()
    except RefsError:
        return None


def remote_head(source: Source) -> str | None:
    """The published head of a source without fetching it (`git ls-remote`);
    None for an empty repository. Raises when the server cannot be reached."""
    out = _git("ls-remote", source.url, source.head_ref, timeout=30).strip()
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if SHA_RE.match(sha.strip()):
            return sha.strip()
    return None


def resolve(ref: Ref | str, *, sources: list[Source] | None = None, fetch_first: bool | None = None,
            base: Path | None = None) -> tuple[Source, str]:
    """(source, full sha) for a reference. `latest` fetches; a sha is looked
    up in the mirror and fetched for only if unknown there."""
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    source = source_named(ref.source, sources, base=base)
    if not ref.revision:
        raise RefsError(f"{ref}: name a revision — `{ref.source}@<sha>` or `{ref.source}@{LATEST}`")
    if ref.revision == LATEST or fetch_first:
        try:
            fetch(source)
        except RefsError as error:
            last = _known(source, source.head_ref)
            hint = f"; the last fetched head is {last[:7]} — `{source.name}@{last[:7]}` reads it" if last else ""
            raise RefsError(f"{source.name} cannot be reached ({error}){hint}") from error
        target = source.head_ref if ref.revision == LATEST else ref.revision
        sha = _known(source, target)
    else:
        sha = _known(source, ref.revision)
        if sha is None:
            try:
                fetch(source)
            except RefsError as error:
                raise RefsError(f"{ref}: not fetched on this host yet, and {source.name} cannot be reached ({error})") from error
            sha = _known(source, ref.revision)
    if sha is None:
        if not _has_commits(source):
            raise RefsError(f"{source.name} is empty: nothing has been published to it yet")
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


def sync(ref: Ref | str, *, sources: list[Source] | None = None, base: Path | None = None) -> tuple[Source, str, Path]:
    """Fetch, resolve and lay out one revision (`latest` by default)."""
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    if not ref.revision:
        ref = Ref(ref.source, LATEST, ref.path)
    source, sha = resolve(ref, sources=sources, fetch_first=True, base=base)
    return source, sha, _export(source, sha)


def snapshot(ref: Ref | str, *, sources: list[Source] | None = None, base: Path | None = None) -> tuple[Source, str, Path]:
    """The snapshot directory for a reference, laid out if it is not yet."""
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    source, sha = resolve(ref, sources=sources, base=base)
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


def is_text(path: Path) -> bool:
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


_is_text = is_text


def show(ref: Ref | str, *, sources: list[Source] | None = None, base: Path | None = None) -> str:
    """A text file, a directory's entries, or a binary file's description."""
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    source, sha, root = snapshot(ref, sources=sources, base=base)
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
    if not is_text(target):
        return f"{resolved}: {describe(target)}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > SHOW_LIMIT:
        text = text[:SHOW_LIMIT] + f"\n… truncated at {SHOW_LIMIT} characters; `agrefs path {resolved}` for the whole file"
    return f"{resolved} (revision {sha})\n{text}"


def path_of(ref: Ref | str, *, sources: list[Source] | None = None, base: Path | None = None) -> Path:
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    _, _, root = snapshot(ref, sources=sources, base=base)
    target = _inside(root, ref.path)
    if not target.exists():
        raise RefsError(f"{ref}: no such file")
    return target


def tree(ref: Ref | str, *, sources: list[Source] | None = None, base: Path | None = None) -> dict:
    """Every file of one revision (or under one path), for a browser: the
    resolved commit, and each file's path, size and whether it is text."""
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    source, sha, root = snapshot(ref, sources=sources, base=base)
    top = _inside(root, ref.path)
    if not top.exists():
        raise RefsError(f"{ref}: no such file in {source.name}@{sha[:7]}")
    base_dir = root.resolve()
    files = []
    for path in sorted(top.rglob("*") if top.is_dir() else [top]):
        if not path.is_file() or path.name == ".agrefs-revision":
            continue
        files.append({"path": path.relative_to(base_dir).as_posix(), "size": path.stat().st_size, "text": is_text(path)})
    return {"source": source.name, "revision": sha, "path": ref.path, "files": files}


# --- search, listing, changes --------------------------------------------------------


def search(ref: Ref | str, terms, *, sources: list[Source] | None = None, limit: int = SEARCH_LIMIT,
           base: Path | None = None) -> list[str]:
    """`<source>@<sha>:<path>:<line>: <text>` for lines holding every term."""
    wanted = [str(term).lower() for term in terms if str(term).strip()]
    if not wanted:
        return []
    ref = parse_ref(ref) if isinstance(ref, str) else ref
    source, sha, root = snapshot(ref, sources=sources, base=base)
    top = _inside(root, ref.path)
    files = [top] if top.is_file() else sorted(p for p in top.rglob("*") if p.is_file() and p.suffix in SEARCH_SUFFIXES)
    hits: list[str] = []
    for path in files:
        if not is_text(path):
            continue
        rel = path.relative_to(root.resolve()).as_posix()
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


def _age(stamp: float | None, now: float | None = None) -> str:
    if not stamp:
        return "never"
    seconds = max(0, int((time.time() if now is None else now) - float(stamp)))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def listing(sources: list[Source] | None = None, base: Path | None = None, *, include_archived: bool = False) -> str:
    """What `agrefs list` prints: the catalog's state, then every source —
    its id, display name, what it is for and what is cached — without
    fetching any content."""
    base = home() if base is None else base
    state = None
    if sources is None:
        sources, state = load_sources(base, with_catalog=True)
    shown = [s for s in sources if include_archived or not s.archived]
    hidden = len(sources) - len(shown)
    archived_note = (f"({hidden} archived source{'s' if hidden != 1 else ''} not shown: `agrefs list --all`; "
                     "their old references still resolve)")
    head = []
    if state is not None:
        head.append(state.summary())
        head.extend(f"  issue: {issue}" for issue in state.issues)
    if not shown and hidden:
        return "\n".join([*head, "no active reference sources", archived_note])
    if not shown:
        note = ("no reference sources are known here: the shared catalog lists none, "
                f"and {base / CONFIG_NAME} names none") if state is None or state.url else (
            f"no reference sources are configured here ({base / CONFIG_NAME} is missing and no catalog is configured); "
            "a [catalog] url in ~/.config/agag/refs.toml names the shared list")
        return "\n".join([*head, note]) if head else note
    blocks = ["\n".join(head)] if head else []
    for source in shown:
        title = f"## {source.name}" + (f" — {source.title}" if source.title and source.title != source.name else "")
        lines = [title + ("  [archived]" if source.archived else "")]
        if source.about:
            lines.append(source.about)
        head_sha = _known(source, source.head_ref)
        lines.append(f"latest fetched: {_commit_line(source, head_sha) if head_sha else '(never fetched here: `agrefs sync ' + source.name + '`)'}")
        cached = sorted(p for p in source.cache.iterdir() if SHA_RE.match(p.name)) if source.cache.is_dir() else []
        if cached:
            lines.append("snapshots on this host:")
            lines.extend(f"  {source.name}@{p.name[:7]}  {_commit_line(source, p.name)}" for p in cached)
        lines.append(
            f"read: `agrefs show {source.name}@<rev>:<path>` · file: `agrefs path {source.name}@<rev>:<path>` · "
            f"newest: `agrefs sync {source.name}`"
        )
        blocks.append("\n".join(lines))
    if hidden:
        blocks.append(archived_note)
    return "\n\n".join(blocks)


def changes(spec: str, *, sources: list[Source] | None = None, base: Path | None = None) -> str:
    """`<source>@<old>..<new>` → the files that differ, one per line, with status."""
    head, _, new = spec.partition("..")
    ref = parse_ref(head)
    if not ref.revision or not new.strip():
        raise RefsError(f"{spec!r}: write `<source>@<old>..<new>`")
    source, old_sha = resolve(ref, sources=sources, base=base)
    _, new_sha = resolve(Ref(ref.source, new.strip(), ""), sources=[source])
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
            "`agrefs list` shows every context the developer has published for agents —\n"
            "id, name and what it is for — from a shared catalog; read the ones that bear\n"
            "on your work. A reference is `<source>@<revision>[:<path>]`. The revision is\n"
            "a git commit (short form is fine) or the word `latest`; every answer prints the\n"
            "commit it resolved to, and that is what you record and pass on — never `latest`.\n"
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
    p = sub.add_parser("list", help="every source: id, name, what it is for, what is cached here")
    p.add_argument("--all", action="store_true", help="include archived sources")
    p.add_argument("--refresh", action="store_true", help="read the catalog again now")
    p.set_defaults(run=_run_list)
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
    p = sub.add_parser("catalog", help="where the shared catalog is and whether this read is current")
    p.add_argument("--refresh", action="store_true", help="read it again now")
    p.set_defaults(run=_run_catalog)
    return parser


def _run_list(args, out) -> int:
    if args.refresh:
        catalog(refresh=True)
    print(listing(include_archived=args.all), file=out)
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


def _run_catalog(args, out) -> int:
    found = catalog(refresh=True if args.refresh else None)
    print(found.summary(), file=out)
    for issue in found.issues:
        print(f"  issue: {issue}", file=out)
    if found.sources:
        active = sum(1 for s in found.sources if not s.archived)
        print(f"  {active} active, {len(found.sources) - active} archived", file=out)
    return 0 if found.state in (CURRENT, LAST_KNOWN, UNCONFIGURED) else 1


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
