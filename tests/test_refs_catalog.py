"""The shared catalog: which context repositories exist, for every agent.

What is pinned here: two independently configured consumers discover the
same sources from one catalog and read identical bytes at a pinned commit;
a newly registered source is found without touching either consumer;
archived sources resolve but are not listed; an outage keeps the last
catalog and every pinned reference already fetched; a catalog entry that
is wrong (duplicate id, bad status) is reported without hiding the others;
an empty repository and an unknown revision are said plainly; an explicit
local source wins over the catalog; the host file configures every
instance at once.
"""

import io
import shutil
import subprocess

import pytest

from agag import refs


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def new_repo(path, files):
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "dev@example.invalid", cwd=path)
    git("config", "user.name", "Developer", cwd=path)
    if files:
        commit(path, files, "first")
    return path


def commit(path, files, message):
    for name, data in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", message, cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


def catalog_text(*entries):
    lines = [f'schema = "{refs.CATALOG_SCHEMA}"', ""]
    for entry in entries:
        lines.append("[[source]]")
        lines.extend(f"{key} = {value!r}".replace("'", '"') for key, value in entry.items())
        lines.append("")
    return "\n".join(lines)


@pytest.fixture
def server(tmp_path):
    """A git host: `developer/<repo>` directories, one of them the catalog."""
    host = tmp_path / "githost"
    meadow = new_repo(host / "developer" / "meadow-refs", {
        "README.md": "# Meadow\n\nDusk scenes.\n",
        "images/dusk.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
    })
    cat = new_repo(host / "developer" / "context-catalog", {
        "catalog.toml": catalog_text({
            "id": "meadow-refs", "name": "Meadow references", "description": "Dusk scenes for the meadow",
            "repository": "developer/meadow-refs", "branch": "main", "status": "active",
        }),
    })
    return host, cat, meadow


def consumer(tmp_path, name, catalog_url=None):
    local = tmp_path / name / ".local"
    local.mkdir(parents=True)
    if catalog_url:
        (local / "refs.toml").write_text(f'[catalog]\nurl = "{catalog_url}"\n', encoding="utf-8")
    return local


def test_two_consumers_discover_and_read_identical_bytes(tmp_path, server):
    host, cat, meadow = server
    a = consumer(tmp_path, "front", cat)
    b = consumer(tmp_path, "forge", cat)
    names_a = [s.name for s in refs.load_sources(a)]
    names_b = [s.name for s in refs.load_sources(b)]
    assert names_a == names_b == ["meadow-refs"]
    source = refs.load_sources(a)[0]
    assert (source.title, source.about, source.branch, source.origin) == (
        "Meadow references", "Dusk scenes for the meadow", "main", "catalog")
    _, sha, _ = refs.sync("meadow-refs", base=a)
    one = refs.path_of(f"meadow-refs@{sha}:images/dusk.png", base=a).read_bytes()
    two = refs.path_of(f"meadow-refs@{sha[:7]}:images/dusk.png", base=b).read_bytes()
    assert one == two == (meadow / "images" / "dusk.png").read_bytes()


def test_a_newly_registered_source_is_found_without_touching_consumers(tmp_path, server):
    host, cat, _ = server
    local = consumer(tmp_path, "front", cat)
    assert [s.name for s in refs.load_sources(local)] == ["meadow-refs"]
    new_repo(host / "developer" / "pond-refs", {"README.md": "# Pond\n"})
    commit(cat, {"catalog.toml": (cat / "catalog.toml").read_text() + "\n" + catalog_text(
        {"id": "pond-refs", "name": "Pond", "description": "Still water", "repository": "developer/pond-refs"},
    ).split("\n", 2)[2]}, "register pond-refs")
    # Within the refresh interval the cached list is used…
    assert [s.name for s in refs.load_sources(local)] == ["meadow-refs"]
    # …but naming the new source reads the catalog again at once.
    assert "# Pond" in refs.show("pond-refs@latest:README.md", base=local)
    assert [s.name for s in refs.load_sources(local)] == ["meadow-refs", "pond-refs"]


def test_refresh_interval_rereads_the_catalog(tmp_path, server, monkeypatch):
    host, cat, _ = server
    local = consumer(tmp_path, "front", cat)
    refs.catalog(local)
    commit(cat, {"catalog.toml": catalog_text()}, "empty the catalog")
    assert refs.catalog(local).sources  # cached
    later = refs.catalog(local, now=10**10)
    assert later.state == refs.CURRENT and later.sources == []


def test_archived_resolves_but_is_not_listed(tmp_path, server):
    host, cat, _ = server
    local = consumer(tmp_path, "front", cat)
    _, sha, _ = refs.sync("meadow-refs", base=local)
    commit(cat, {"catalog.toml": catalog_text({
        "id": "meadow-refs", "name": "Meadow references", "repository": "developer/meadow-refs", "status": "archived",
    })}, "archive meadow")
    refs.catalog(local, refresh=True)
    text = refs.listing(base=local)
    assert "## meadow-refs" not in text and "1 archived source not shown" in text
    assert "## meadow-refs — Meadow references  [archived]" in refs.listing(base=local, include_archived=True)
    assert "Dusk scenes" in refs.show(f"meadow-refs@{sha[:7]}:README.md", base=local)


def test_an_outage_keeps_the_last_catalog_and_pinned_references(tmp_path, server):
    host, cat, _ = server
    local = consumer(tmp_path, "front", cat)
    _, sha, _ = refs.sync("meadow-refs", base=local)
    shutil.move(str(host), str(tmp_path / "gone"))
    found = refs.catalog(local, refresh=True)
    assert found.state == refs.LAST_KNOWN and found.error
    assert [s.name for s in found.sources] == ["meadow-refs"]
    assert "last-known" in refs.listing(base=local)
    assert "Dusk scenes" in refs.show(f"meadow-refs@{sha[:7]}:README.md", base=local)
    with pytest.raises(refs.RefsError, match=f"last fetched head is {sha[:7]}"):
        refs.sync("meadow-refs", base=local)


def test_never_read_catalog_is_unavailable(tmp_path):
    local = consumer(tmp_path, "front", str(tmp_path / "nowhere" / "developer" / "context-catalog"))
    found = refs.catalog(local)
    assert found.state == refs.UNAVAILABLE and found.sources == []
    with pytest.raises(refs.RefsError, match="catalog unavailable"):
        refs.show("meadow-refs@abc:README.md", base=local)


def test_bad_entries_are_reported_not_fatal(tmp_path, server):
    host, cat, _ = server
    commit(cat, {"catalog.toml": catalog_text(
        {"id": "meadow-refs", "repository": "developer/meadow-refs"},
        {"id": "meadow-refs", "repository": "developer/other"},
        {"id": "Bad Name", "repository": "developer/x"},
        {"id": "odd", "repository": "developer/x", "status": "hidden"},
        {"id": "nowhere"},
    )}, "messy")
    local = consumer(tmp_path, "front", cat)
    found = refs.catalog(local)
    assert [s.name for s in found.sources] == ["meadow-refs"]
    assert any("duplicate id 'meadow-refs'" in issue for issue in found.issues)
    assert any("'Bad Name' is not a source id" in issue for issue in found.issues)
    assert any("status 'hidden'" in issue for issue in found.issues)
    assert any("nowhere: needs a repository" in issue for issue in found.issues)


def test_an_unusable_newest_catalog_keeps_the_last_good_one(tmp_path, server):
    host, cat, _ = server
    local = consumer(tmp_path, "front", cat)
    refs.catalog(local)
    commit(cat, {"catalog.toml": "schema = 'something else'\n"}, "broken")
    found = refs.catalog(local, refresh=True)
    assert found.state == refs.LAST_KNOWN and "schema" in found.error
    assert [s.name for s in found.sources] == ["meadow-refs"]


def test_empty_repository_and_unknown_revision_are_plain(tmp_path, server):
    host, cat, _ = server
    new_repo(host / "developer" / "blank", {})
    commit(cat, {"catalog.toml": (cat / "catalog.toml").read_text() + "\n[[source]]\nid = \"blank\"\nrepository = \"developer/blank\"\n"}, "blank")
    local = consumer(tmp_path, "front", cat)
    with pytest.raises(refs.RefsError, match="blank is empty: nothing has been published"):
        refs.sync("blank", base=local)
    source = refs.source_named("blank", base=local)
    assert refs.remote_head(source) is None
    with pytest.raises(refs.RefsError, match="no such revision in meadow-refs"):
        refs.show("meadow-refs@" + "0" * 40, base=local)


def test_explicit_local_source_wins_and_host_file_configures_everyone(tmp_path, server, monkeypatch):
    host, cat, meadow = server
    hostcfg = tmp_path / "hostcfg.toml"
    hostcfg.write_text(f'[catalog]\nurl = "{cat}"\n', encoding="utf-8")
    monkeypatch.setenv(refs.HOST_CONFIG_VARIABLE, str(hostcfg))
    plain = tmp_path / "observer" / ".local"
    plain.mkdir(parents=True)
    assert [s.name for s in refs.load_sources(plain)] == ["meadow-refs"]
    other = new_repo(tmp_path / "elsewhere" / "meadow", {"README.md": "# a local override\n"})
    local = consumer(tmp_path, "front")
    (local / "refs.toml").write_text(f'[[source]]\nname = "meadow-refs"\nurl = "{other}"\nabout = "override"\n', encoding="utf-8")
    sources, found = refs.load_sources(local, with_catalog=True)
    assert [(s.name, s.origin) for s in sources] == [("meadow-refs", "local")]
    assert any("overrides the catalog entry" in issue for issue in found.issues)


def test_repository_urls_follow_the_catalog_host():
    url, web = refs.repository_url("http://git.example:3000/developer/context-catalog.git", "developer/meadow")
    assert (url, web) == ("http://git.example:3000/developer/meadow.git", "http://git.example:3000/developer/meadow")
    url, web = refs.repository_url("https://git.example/sub/developer/context-catalog", "developer/meadow.git")
    assert url == "https://git.example/sub/developer/meadow.git"


def test_tree_lists_every_file_with_text_flags(tmp_path, server):
    host, cat, _ = server
    local = consumer(tmp_path, "front", cat)
    _, sha, _ = refs.sync("meadow-refs", base=local)
    found = refs.tree(f"meadow-refs@{sha}", base=local)
    assert found["revision"] == sha
    assert [(f["path"], f["text"]) for f in found["files"]] == [("README.md", True), ("images/dusk.png", False)]


def test_catalog_command_says_where_and_how_fresh(tmp_path, server, monkeypatch):
    host, cat, _ = server
    local = consumer(tmp_path, "front", cat)
    monkeypatch.setenv(refs.HOME_VARIABLE, str(local))
    out = io.StringIO()
    assert refs.main(["catalog"], out=out) == 0
    assert out.getvalue().startswith("catalog: current at ")
    out = io.StringIO()
    assert refs.main(["list"], out=out) == 0
    assert "## meadow-refs — Meadow references" in out.getvalue() and "Dusk scenes for the meadow" in out.getvalue()
