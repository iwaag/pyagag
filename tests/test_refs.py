"""`agrefs`: a human's repository, read by name at a pinned revision.

What is pinned here: a reference string names a commit and a path and
nothing else; a snapshot is the exact tree of that commit, immutable, and
two revisions coexist; `show` prints text, lists directories and describes
binaries without pretending to read them; nothing resolves outside the
snapshot; a missing configuration is a sentence, not a traceback; and the
listener hands a run the home the CLI reads.
"""

import io
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from agag import agent, refs

PNG = (
    b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 320, 200, 8, 2, 0, 0, 0)
    + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 320, 200, 8, 2, 0, 0, 0)))
)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def human_repo(tmp_path):
    """The human's repository with two published revisions."""
    repo = tmp_path / "human" / "protoprey-refs"
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "dev@example.invalid", cwd=repo)
    git("config", "user.name", "Developer", cwd=repo)
    (repo / "README.md").write_text("# refs\n\nStories and images for the meadow scene.\n", encoding="utf-8")
    (repo / "stories").mkdir()
    (repo / "stories" / "meadow.md").write_text("Dusk over the meadow.\nThe grass is taller than you.\n", encoding="utf-8")
    (repo / "images").mkdir()
    (repo / "images" / "meadow.png").write_bytes(PNG)
    (repo / "drafts").mkdir()
    (repo / "drafts" / "unpublished.md").write_text("not yet\n", encoding="utf-8")
    (repo / ".gitignore").write_text("drafts/\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "first references", cwd=repo)
    first = git("rev-parse", "HEAD", cwd=repo)
    (repo / "stories" / "meadow.md").write_text("Dusk over the meadow.\nThe grass is a forest.\n", encoding="utf-8")
    (repo / "stories" / "pond.md").write_text("Still water.\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "the grass is a forest", cwd=repo)
    second = git("rev-parse", "HEAD", cwd=repo)
    return repo, first, second


@pytest.fixture
def consumer(tmp_path, human_repo, monkeypatch):
    """An agent's `.local/` naming the human repository."""
    repo, _, _ = human_repo
    local = tmp_path / "agent" / ".local"
    local.mkdir(parents=True)
    (local / "refs.toml").write_text(
        f'schema = "{refs.SCHEMA}"\n\n[[source]]\nname = "protoprey-refs"\nurl = "{repo}"\n'
        'about = "the Developer\'s ProtoPrey references"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(refs.HOME_VARIABLE, str(local))
    return local


def test_reference_string_is_source_revision_path():
    ref = refs.parse_ref("protoprey-refs@3f2a1b0:scenes/meadow/composition.png")
    assert (ref.source, ref.revision, ref.path) == ("protoprey-refs", "3f2a1b0", "scenes/meadow/composition.png")
    assert str(ref) == "protoprey-refs@3f2a1b0:scenes/meadow/composition.png"
    assert refs.parse_ref("protoprey-refs").revision is None
    with pytest.raises(refs.RefsError, match="climbs out"):
        refs.parse_ref("protoprey-refs@abc:../secrets")


def test_sync_lays_out_the_exact_tree_and_keeps_drafts_out(consumer, human_repo):
    _, first, second = human_repo
    source, sha, root = refs.sync("protoprey-refs")
    assert sha == second
    assert root == consumer / "refs" / "protoprey-refs" / second
    assert (root / "stories" / "pond.md").is_file()
    assert (root / "images" / "meadow.png").read_bytes() == PNG
    assert not (root / "drafts").exists()
    assert not (root / ".git").exists()
    assert (root / ".agrefs-revision").read_text().strip() == second


def test_two_revisions_coexist_and_a_short_sha_resolves(consumer, human_repo):
    _, first, second = human_repo
    refs.sync("protoprey-refs")
    _, sha, old = refs.sync(f"protoprey-refs@{first[:7]}")
    assert sha == first
    assert old != refs.path_of(f"protoprey-refs@{second}")
    assert "taller than you" in refs.show(f"protoprey-refs@{first[:7]}:stories/meadow.md")
    assert "a forest" in refs.show(f"protoprey-refs@{second[:7]}:stories/meadow.md")
    assert not (old / "stories" / "pond.md").exists()


def test_a_revision_must_be_named(consumer):
    with pytest.raises(refs.RefsError, match="name a revision"):
        refs.show("protoprey-refs:README.md")


def test_show_lists_directories_and_describes_binaries(consumer, human_repo):
    _, _, second = human_repo
    tree = refs.show(f"protoprey-refs@{second[:7]}")
    assert tree.splitlines()[0] == f"protoprey-refs@{second[:7]}/ (revision {second})"
    assert "  stories/" in tree and "  README.md" in tree and ".agrefs-revision" not in tree
    image = refs.show(f"protoprey-refs@{second[:7]}:images/meadow.png")
    assert image.startswith(f"protoprey-refs@{second[:7]}:images/meadow.png: binary: image/png, 320x200, ")
    assert image.endswith(str(refs.path_of(f"protoprey-refs@{second[:7]}:images/meadow.png")))


def test_nothing_resolves_outside_the_snapshot(consumer, human_repo):
    _, _, second = human_repo
    with pytest.raises(refs.RefsError):
        refs.path_of(f"protoprey-refs@{second[:7]}:stories/../../secrets")
    with pytest.raises(refs.RefsError, match="no such file"):
        refs.path_of(f"protoprey-refs@{second[:7]}:stories/absent.md")


def test_search_and_changes(consumer, human_repo):
    _, first, second = human_repo
    hits = refs.search(f"protoprey-refs@{second[:7]}", ["grass", "forest"])
    assert hits == [f"protoprey-refs@{second[:7]}:stories/meadow.md:2: The grass is a forest."]
    report = refs.changes(f"protoprey-refs@{first[:7]}..{second[:7]}")
    assert report.splitlines()[0] == f"protoprey-refs@{first[:7]}..{second[:7]}"
    assert "the grass is a forest" in report
    assert "M\tstories/meadow.md" in report and "A\tstories/pond.md" in report


def test_listing_names_snapshots_and_the_latest_head(consumer, human_repo):
    _, first, second = human_repo
    assert "never fetched" in refs.listing()
    refs.sync(f"protoprey-refs@{first[:7]}")
    text = refs.listing()
    assert "## protoprey-refs" in text
    assert f"protoprey-refs@{first[:7]}" in text
    assert "latest fetched:" in text and second[:7] in text


def test_no_configuration_is_a_sentence(tmp_path, monkeypatch):
    monkeypatch.setenv(refs.HOME_VARIABLE, str(tmp_path / "nowhere"))
    out, err = io.StringIO(), io.StringIO()
    assert refs.main(["list"], out=out, err=err) == 0
    assert "no reference sources are configured" in out.getvalue()
    assert refs.main(["show", "ghost@abc:x"], out=out, err=err) == 2
    assert "no source named 'ghost'" in err.getvalue()


def test_home_is_the_environment_or_the_nearest_local(tmp_path, monkeypatch):
    monkeypatch.delenv(refs.HOME_VARIABLE, raising=False)
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "refs.toml").write_text("", encoding="utf-8")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert refs.home(cwd=deep) == tmp_path / ".local"
    assert refs.home({refs.HOME_VARIABLE: "/elsewhere"}, cwd=deep) == Path("/elsewhere")


def test_a_run_is_handed_its_refs_home(tmp_path):
    spec = agent.AgentSpec(agent="agforge", root=tmp_path)
    environment = agent.chat_environment(spec, bin_dir=tmp_path)
    assert environment[refs.HOME_VARIABLE] == str(tmp_path / ".local")


def test_cli_round_trip(consumer, human_repo):
    _, _, second = human_repo
    out = io.StringIO()
    assert refs.main(["sync", "protoprey-refs"], out=out) == 0
    assert out.getvalue().startswith(f"protoprey-refs@{second[:7]}  ({second})")
    out = io.StringIO()
    assert refs.main(["revision", "protoprey-refs"], out=out) == 0
    assert second in out.getvalue()
