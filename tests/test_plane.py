"""The shared Plane client, with `_request_json` as the seam."""

import urllib.parse

import pytest

from agag import plane

CONFIG = plane.PlaneConfig("http://plane.invalid", "key", "ws")
PROJECT = {"id": "p-1", "name": "Demo Project", "identifier": "DP"}
STATES = [
    {"id": "s-backlog", "name": "Backlog", "group": "backlog"},
    {"id": "s-ready", "name": "Ready", "group": "unstarted"},
    {"id": "s-progress", "name": "In Progress", "group": "started"},
    {"id": "s-done", "name": "Done", "group": "completed"},
]


class Plane:
    def __init__(self, projects=(PROJECT,), issues=None, pages=None):
        self.projects = list(projects)
        self.issues = dict(issues or {})
        self.pages = pages
        self.calls = []

    def __call__(self, method, url, *, headers, body=None, timeout=30):
        self.calls.append((method, url, body))
        if method == "GET" and "/projects/?" in url:
            return 200, {"results": self.projects}
        if method == "POST" and url.endswith("/projects/"):
            created = {"id": f"p-{len(self.projects) + 1}", **body}
            self.projects.append(created)
            return 201, created
        if method == "GET" and "/states/" in url:
            return 200, {"results": STATES}
        if method == "GET" and "/labels/" in url:
            return 200, {"results": [{"id": "l-auto", "name": "AUTO"}]}
        if method == "GET" and "external_id=" in url:
            key = urllib.parse.unquote(url.split("external_id=", 1)[1].split("&", 1)[0])
            source = url.split("external_source=", 1)[1].split("&", 1)[0]
            found = self.issues.get((source, key))
            return (200, found) if found else (404, {"detail": "not found"})
        if method == "GET" and "/issues/?" in url and self.pages is not None:
            cursor = url.split("cursor=", 1)[1] if "cursor=" in url else ""
            return 200, self.pages[cursor]
        if method == "POST" and url.endswith("/issues/"):
            issue = {"id": f"i-{len(self.issues) + 1}", "sequence_id": 7, **body}
            self.issues[(body["external_source"], body["external_id"])] = issue
            return 201, issue
        if method == "PATCH":
            return 200, {"id": "patched", **(body or {})}
        raise AssertionError(f"unexpected call: {method} {url}")

    def bodies(self, method, suffix):
        return [b for m, url, b in self.calls if m == method and url.endswith(suffix)]


@pytest.fixture
def fake(monkeypatch):
    instance = Plane()
    monkeypatch.setattr(plane, "_request_json", instance)
    return instance


# --- the external key ------------------------------------------------------


def test_external_source_namespaces_two_agents_in_one_workspace(fake):
    plane.ensure_issue(
        CONFIG, "p-1", name="A", description="", state="s-ready",
        external_source="agforge", external_id="ch/topic",
    )
    # Same external_id, different source: a separate issue, not a duplicate.
    _, created = plane.ensure_issue(
        CONFIG, "p-1", name="B", description="", state="s-ready",
        external_source="agautolab", external_id="ch/topic",
    )
    assert created is True
    assert len(fake.bodies("POST", "/issues/")) == 2


def test_the_same_key_is_never_created_twice(fake):
    args = dict(name="A", description="", state="s-ready",
                external_source="agforge", external_id="ch/topic")
    first, created_first = plane.ensure_issue(CONFIG, "p-1", **args)
    second, created_second = plane.ensure_issue(CONFIG, "p-1", **args)
    assert (created_first, created_second) == (True, False)
    assert first["id"] == second["id"]
    assert len(fake.bodies("POST", "/issues/")) == 1


def test_an_unknown_key_answers_404_not_an_empty_list(fake):
    assert plane.find_issue_by_external(CONFIG, "p-1", "agforge", "nothing") is None


# --- labels are opt-in -----------------------------------------------------


def test_no_labels_are_attached_unless_asked(fake):
    plane.ensure_issue(
        CONFIG, "p-1", name="A", description="", state="s-ready",
        external_source="agforge", external_id="k1",
    )
    assert "labels" not in fake.bodies("POST", "/issues/")[0]


def test_labels_are_attached_when_asked(fake):
    plane.ensure_issue(
        CONFIG, "p-1", name="A", description="", state="s-ready",
        external_source="agautolab", external_id="k1", labels=["l-auto"],
    )
    assert fake.bodies("POST", "/issues/")[0]["labels"] == ["l-auto"]


def test_a_parent_is_attached_only_when_given(fake):
    plane.ensure_issue(
        CONFIG, "p-1", name="A", description="", state="s-ready",
        external_source="agautolab", external_id="k1", parent="i-parent",
    )
    assert fake.bodies("POST", "/issues/")[0]["parent"] == "i-parent"


def test_an_empty_title_is_refused(fake):
    with pytest.raises(plane.PlaneError):
        plane.ensure_issue(
            CONFIG, "p-1", name="  ", description="", state="s-ready",
            external_source="agforge", external_id="k1",
        )


# --- projects --------------------------------------------------------------


def test_find_project_matches_on_a_normalized_name(fake):
    assert plane.find_project(CONFIG, "demo-project")["id"] == "p-1"
    assert plane.find_project(CONFIG, "Demo  Project")["id"] == "p-1"
    assert plane.find_project(CONFIG, "absent") is None


def test_create_project_skips_a_taken_identifier(fake):
    plane.create_project(CONFIG, "Demo Portal", "a description")
    body = fake.bodies("POST", "/projects/")[0]
    # "DP" is taken by Demo Project, so the next free form is used.
    assert body["identifier"] == "DP2"
    assert body["description"] == "a description"


def test_the_project_description_is_the_callers(fake):
    """What a marker in a description means is the calling agent's policy."""
    plane.create_project(CONFIG, "Freeforge")
    assert fake.bodies("POST", "/projects/")[0]["description"] == ""


# --- states ----------------------------------------------------------------


def test_starting_state_prefers_ready(fake):
    assert plane.starting_state_id(CONFIG, "p-1") == "s-ready"


def test_state_lookup_by_group(fake):
    assert plane.state_id_for_group(CONFIG, "p-1", "completed") == "s-done"
    with pytest.raises(plane.PlaneError):
        plane.state_id_for_group(CONFIG, "p-1", "cancelled")


def test_starting_state_falls_back_to_backlog(monkeypatch):
    monkeypatch.setattr(
        plane, "_request_json",
        lambda *a, **k: (200, {"results": [{"id": "s-b", "name": "Backlog", "group": "backlog"}]}),
    )
    assert plane.starting_state_id(CONFIG, "p-1") == "s-b"


# --- pagination ------------------------------------------------------------


def test_issue_listing_follows_the_cursor(monkeypatch):
    fake = Plane(pages={
        "": {"results": [{"id": "a"}], "next_page_results": True, "next_cursor": "c2"},
        "c2": {"results": [{"id": "b"}], "next_page_results": False},
    })
    monkeypatch.setattr(plane, "_request_json", fake)
    assert [row["id"] for row in plane.list_issues(CONFIG, "p-1")] == ["a", "b"]


# --- documents -------------------------------------------------------------


def test_split_document_prefers_the_first_heading():
    assert plane.split_document("intro?\n# Real Title\n\nBody.") == (
        "intro?", "# Real Title\n\nBody."
    )
    assert plane.split_document("# Title\n\nBody.\n") == ("Title", "Body.")
    with pytest.raises(plane.PlaneError):
        plane.split_document("   \n\n")


def test_a_document_survives_the_round_trip():
    title, description = plane.split_document("# Bird\n\nDraw it.\nTwice.\n")
    restored = plane.compose_document(title, plane.description_html(description))
    assert restored == "# Bird\n\nDraw it.\nTwice.\n"


def test_html_to_text_does_not_trust_description_stripped():
    assert plane.html_to_text("<p>a &amp; b<br>c</p>") == "a & b\nc"
    assert plane.html_to_text(None) == ""


def test_issue_label_falls_back_to_the_raw_id():
    assert plane.issue_label(PROJECT, {"sequence_id": 4}) == "DP-4"
    assert plane.issue_label({}, {"id": "i-9"}) == "i-9"


# --- credentials -----------------------------------------------------------


def test_credentials_report_what_is_missing(tmp_path):
    path = tmp_path / "plane.env"
    path.write_text("PLANE_URL=http://x/\n")
    with pytest.raises(plane.PlaneError) as caught:
        plane.load_plane_config(path)
    assert "PLANE_API_KEY" in str(caught.value)
    assert "PLANE_WORKSPACE_SLUG" in str(caught.value)


def test_credentials_are_read_without_sourcing_shell(tmp_path):
    path = tmp_path / "plane.env"
    path.write_text(
        "# a comment\nPLANE_URL=http://x/\nPLANE_API_KEY=k\n"
        "PLANE_WORKSPACE_SLUG=ws\nexport NOT_A_PAIR\n"
    )
    config = plane.load_plane_config(path)
    assert (config.url, config.api_key, config.workspace) == ("http://x", "k", "ws")


def test_a_missing_credentials_file_is_reported(tmp_path):
    with pytest.raises(plane.PlaneError):
        plane.load_plane_config(tmp_path / "absent.env")
