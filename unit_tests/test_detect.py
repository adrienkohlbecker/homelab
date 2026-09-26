"""Unit tests for mise-tasks/ci/detect.py — CI change-detection pipeline.

Tests path classification regexes, file classification, release-cell
propagation, git helpers, GitLab pipelines-API green-base resolution, role
dependency map, and the ``gitlab`` child-pipeline command.
"""

import email.message
import json
import subprocess
import urllib.error
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import load_repo_module

detect = load_repo_module("mise-tasks/ci/detect.py")


@pytest.fixture(autouse=True)
def _no_pipeline_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    # GitLab exports pipeline variables to every job, including unit_tests, so
    # a benchmark pipeline's ROLES / HOMELAB_CI_ARM would otherwise steer detect.
    for name in ("ROLES", "HOMELAB_CI_ARM"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# classify_changed_files
# ---------------------------------------------------------------------------


class TestClassifyChangedFiles:
    @pytest.mark.parametrize(
        ("path", "expected_roles"),
        [
            ("roles/nginx/tasks/main.yml", ["nginx"]),
            ("roles/podman/templates/foo.j2", ["podman"]),
            ("test/machine.py", []),
            ("roles/", []),
        ],
    )
    def test_role_paths(self, path: str, expected_roles: list[str]) -> None:
        assert detect.classify_changed_files([path]).direct_roles == expected_roles

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("group_vars/all/main.yml", True),
            ("group_vars/all/service_ports.yaml", True),
            ("group_vars/test.yml", True),
            ("test/machine.py", True),
            ("test/testall.py", True),
            ("test/matrix.py", True),
            ("test/inventory.ini", True),
            ("test/playbooks/site.yml", True),
            ("ansible.cfg", True),
            ("vault-client.sh", True),
            ("mise.toml", True),
            ("pyproject.toml", True),
            ("uv.lock", True),
            ("data/network_topology.yml", True),
            ("data/network_topology.schema.json", True),
            ("data/architectures.yml", True),
            ("mise-tasks/ci/detect.py", True),
            ("test/host_vars/lab.yml", True),
            ("test/host_vars/minimal.yml", False),
            ("group_vars/physical_lab.yml", False),
            ("group_vars/physical_pug.yml", False),
            ("group_vars/physical_fox.yml", False),
            ("group_vars/storage_lab.yml", True),
            ("group_vars/storage_pug.yml", False),
            ("site.yml", False),
            ("group_vars/all/sub/deep.yml", False),
            ("unit_tests/test_matrix.py", False),
            ("roles/nginx/tasks/main.yml", False),
            ("roles/podman/templates/foo.j2", False),
            ("README.md", False),
            ("Dockerfile", False),
        ],
    )
    def test_full_universe_paths(self, path: str, expected: bool) -> None:
        result = detect.classify_changed_files([path])
        assert bool(result.full_universe_paths) is expected

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("packer/qemu.pkr.hcl", True),
            ("packer/scripts/chroot.sh", True),
            ("mise-tasks/packer/build", True),
            ("roles/packer/tasks/main.yml", False),
            ("test/machine.py", False),
        ],
    )
    def test_packer_paths(self, path: str, expected: bool) -> None:
        assert detect.classify_changed_files([path]).packer_changed is expected

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("test/host_vars/minimal.yml", {"minimal"}),
            ("test/minimal/user-data", {"minimal"}),
            ("test/host_vars/lab.yml", set()),
            ("group_vars/physical_lab.yml", set()),
            ("group_vars/physical_pug.yml", set()),
            ("group_vars/physical_fox.yml", set()),
            ("group_vars/storage_lab.yml", set()),
            ("group_vars/storage_pug.yml", {"pug"}),
        ],
    )
    def test_machine_universe_paths(self, path: str, expected: set[str]) -> None:
        assert detect.classify_changed_files([path]).machine_universe == expected

    def test_mixed_paths(self) -> None:
        paths = [
            "roles/nginx/tasks/main.yml",
            "packer/scripts/chroot.sh",
            "group_vars/all/main.yml",
            "Dockerfile",
        ]
        result = detect.classify_changed_files(paths)
        assert result.direct_roles == ["nginx"]
        assert result.packer_changed
        assert result.full_universe_paths == ["group_vars/all/main.yml"]

    def test_empty_paths_and_blank_lines(self) -> None:
        assert detect.classify_changed_files([]) == detect.ChangeClassification([], [], False, set())
        assert detect.classify_changed_files(["", "roles/nginx/tasks/main.yml", ""]).direct_roles == ["nginx"]

    def test_deduplicates_roles(self) -> None:
        result = detect.classify_changed_files(
            [
                "roles/nginx/tasks/main.yml",
                "roles/nginx/templates/site.conf.j2",
            ]
        )
        assert result.direct_roles == ["nginx"]

    def test_aggregates_categories(self) -> None:
        result = detect.classify_changed_files(
            [
                "mise.toml",
                "pyproject.toml",
                "packer/scripts/chroot.sh",
                "roles/zfs/tasks/main.yml",
                "group_vars/storage_lab.yml",
                "group_vars/storage_pug.yml",
            ]
        )
        assert result.direct_roles == ["zfs"]
        assert result.full_universe_paths == ["mise.toml", "pyproject.toml", "group_vars/storage_lab.yml"]
        assert result.packer_changed
        assert result.machine_universe == {"pug"}


# ---------------------------------------------------------------------------
# propagate_release_cells
# ---------------------------------------------------------------------------


class TestPropagateReleaseCells:
    @staticmethod
    def _stub_configs(
        monkeypatch: pytest.MonkeyPatch,
        role_releases: dict[str, list[str]],
        role_machines: dict[str, list[str]],
    ) -> None:
        monkeypatch.setattr(
            detect,
            "load_role_test_config",
            lambda role: SimpleNamespace(
                ubuntu=role_releases.get(role, []),
                machines=role_machines.get(role, ["lab"]),
            ),
        )

    def test_basic_propagation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_configs(
            monkeypatch,
            {"apt_source": ["noble", "resolute"]},
            {"nginx": ["lab"], "podman": ["lab"]},
        )
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["nginx", "podman"]},
            universe={"nginx", "podman"},
        )
        assert result == [
            detect.TestCell("lab", "noble", "nginx"),
            detect.TestCell("lab", "noble", "podman"),
            detect.TestCell("lab", "resolute", "nginx"),
            detect.TestCell("lab", "resolute", "podman"),
        ]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"releases": {}},
            {"releases": {"apt_source": []}},
            {"consumers": {}},
            {"universe": set()},
            {"direct_roles": []},
        ],
        ids=["missing-releases", "empty-releases", "no-consumers", "outside-universe", "no-direct-roles"],
    )
    def test_missing_relationships_return_no_cells(self, monkeypatch: pytest.MonkeyPatch, overrides) -> None:
        arguments = {
            "direct_roles": ["apt_source"],
            "consumers": {"apt_source": ["nginx"]},
            "releases": {"apt_source": ["noble"]},
            "universe": {"nginx"},
        }
        arguments.update(overrides)
        self._stub_configs(monkeypatch, arguments.pop("releases"), {"nginx": ["lab"]})

        assert detect.propagate_release_cells(**arguments) == []

    def test_default_machine_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_configs(monkeypatch, {"apt_source": ["noble"]}, {})
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["newrole"]},
            universe={"newrole"},
        )
        assert result == [detect.TestCell("lab", "noble", "newrole")]

    def test_deduplicates_across_helpers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_configs(monkeypatch, {"helper_a": ["noble"], "helper_b": ["noble"]}, {"consumer": ["lab"]})
        result = detect.propagate_release_cells(
            direct_roles=["helper_a", "helper_b"],
            consumers={"helper_a": ["consumer"], "helper_b": ["consumer"]},
            universe={"consumer"},
        )
        assert result == [detect.TestCell("lab", "noble", "consumer")]

    def test_multiple_roles_multiple_releases(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_configs(
            monkeypatch,
            {"apt_source": ["noble", "resolute"], "podman": ["noble"]},
            {"nginx": ["lab"], "redis": ["lab"]},
        )
        result = detect.propagate_release_cells(
            direct_roles=["apt_source", "podman"],
            consumers={"apt_source": ["nginx", "redis"], "podman": ["redis"]},
            universe={"nginx", "redis"},
        )
        assert result == [
            detect.TestCell("lab", "noble", "nginx"),
            detect.TestCell("lab", "noble", "redis"),
            detect.TestCell("lab", "resolute", "nginx"),
            detect.TestCell("lab", "resolute", "redis"),
        ]

    def test_multi_machine_propagation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_configs(monkeypatch, {"apt_source": ["noble"]}, {"cleanup": ["lab", "minimal"]})
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["cleanup"]},
            universe={"cleanup"},
        )
        assert result == [
            detect.TestCell("lab", "noble", "cleanup"),
            detect.TestCell("minimal", "noble", "cleanup"),
        ]


# Git helpers
# ---------------------------------------------------------------------------


def _fake_git_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


class TestGitDiffFiles:
    def test_parses_filenames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("a.yml\nb.yml\n"))
        assert detect.git_diff_files("abc", "HEAD") == ["a.yml", "b.yml"]

    def test_empty_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result(""))
        assert detect.git_diff_files("abc") == []

    def test_strips_blank_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("a.yml\n\nb.yml\n\n"))
        assert detect.git_diff_files("abc") == ["a.yml", "b.yml"]


class TestGitRevParse:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("abc123\n"))
        assert detect.git_rev_parse("HEAD~1") == "abc123"

    def test_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=128))
        assert detect.git_rev_parse("bogus") is None


class TestGitRevParseShort:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("abc123\n"))
        assert detect.git_rev_parse_short("HEAD") == "abc123"

    def test_failure_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=128))
        assert detect.git_rev_parse_short("a" * 40) == "a" * 12


class TestGitTree:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("tree123\n"))
        assert detect.git_tree("abc") == "tree123"

    def test_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=128))
        assert detect.git_tree("bogus") is None


class TestGitFetchCommit:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result(""))
        assert detect.git_fetch_commit("abc") is True

    def test_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=1))
        assert detect.git_fetch_commit("abc") is False


class TestGitDeepenSince:
    def test_fetches_branch_with_shallow_since(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = {}

        def mock_git(*a, **kw):
            seen["args"] = a
            return _fake_git_result("")

        monkeypatch.setattr(detect, "_git", mock_git)
        assert detect.git_deepen_since("master", "2026-06-12") is True
        assert seen["args"] == ("fetch", "--no-tags", "--quiet", "--shallow-since=2026-06-12", "origin", "master")

    def test_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=128))
        assert detect.git_deepen_since("master", "2026-06-12") is False


class TestShallowSinceArg:
    def test_subtracts_a_day_margin(self) -> None:
        assert detect._shallow_since_arg("2026-06-13T07:43:18.832Z") == "2026-06-12"

    def test_no_fractional_no_zulu(self) -> None:
        assert detect._shallow_since_arg("2026-06-13T00:00:00+00:00") == "2026-06-12"

    def test_unparseable_returns_none(self) -> None:
        assert detect._shallow_since_arg("not-a-date") is None


# ---------------------------------------------------------------------------
# GitLab API — green-base resolution
# ---------------------------------------------------------------------------


class _FakeListResponse:
    """urlopen mock returning a JSON list (the pipelines endpoint shape)."""

    def __init__(self, data: list):
        self._data = json.dumps(data).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestGlApiGet:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect.urllib.request,
            "urlopen",
            lambda req, timeout=None: _FakeListResponse([{"sha": "abc"}]),
        )
        assert detect._gl_api_get("http://x/pipelines", "tok") == [{"sha": "abc"}]

    def test_sends_job_token_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = {}

        def mock_urlopen(req, timeout=None):
            seen["headers"] = dict(req.headers)
            return _FakeListResponse([])

        monkeypatch.setattr(detect.urllib.request, "urlopen", mock_urlopen)
        detect._gl_api_get("http://x", "jobtok", token_kind="job")
        # urllib capitalizes header names: JOB-TOKEN -> Job-token.
        assert seen["headers"].get("Job-token") == "jobtok"

    def test_sends_private_token_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = {}

        def mock_urlopen(req, timeout=None):
            seen["headers"] = dict(req.headers)
            return _FakeListResponse([])

        monkeypatch.setattr(detect.urllib.request, "urlopen", mock_urlopen)
        detect._gl_api_get("http://x", "pat", token_kind="private")
        assert seen["headers"].get("Private-token") == "pat"

    def test_auth_error_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}

        def mock_urlopen(req, timeout=None):
            attempts["n"] += 1
            raise urllib.error.HTTPError("http://x", 403, "Forbidden", email.message.Message(), None)

        monkeypatch.setattr(detect.urllib.request, "urlopen", mock_urlopen)
        monkeypatch.setattr(detect.time, "sleep", lambda _: None)
        assert detect._gl_api_get("http://x", "tok", retries=4) is None
        assert attempts["n"] == 1

    def test_transient_http_error_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}

        def mock_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise urllib.error.HTTPError("http://x", 502, "Bad Gateway", email.message.Message(), None)
            return _FakeListResponse([{"ok": 1}])

        monkeypatch.setattr(detect.urllib.request, "urlopen", mock_urlopen)
        monkeypatch.setattr(detect.time, "sleep", lambda _: None)
        assert detect._gl_api_get("http://x", "tok", retries=2) == [{"ok": 1}]

    def test_all_retries_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect.urllib.request,
            "urlopen",
            lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("down")),
        )
        monkeypatch.setattr(detect.time, "sleep", lambda _: None)
        assert detect._gl_api_get("http://x", "tok", retries=2) is None


class TestIsLocalAncestor:
    def test_ancestor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=0))
        assert detect.is_local_ancestor("abc", "HEAD") is True

    def test_not_ancestor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=1))
        assert detect.is_local_ancestor("abc", "HEAD") is False

    def test_fetches_when_missing_then_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = {"present": False}
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc" if state["present"] else None)

        def fake_fetch(sha):
            state["present"] = True
            return True

        monkeypatch.setattr(detect, "git_fetch_commit", fake_fetch)
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=0))
        assert detect.is_local_ancestor("abc", "HEAD") is True

    def test_unfetchable_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: None)
        monkeypatch.setattr(detect, "git_fetch_commit", lambda sha: False)
        called = {"git": False}

        def mock_git(*a, **kw):
            called["git"] = True
            return _fake_git_result("", returncode=0)

        monkeypatch.setattr(detect, "_git", mock_git)
        assert detect.is_local_ancestor("abc", "HEAD") is False
        # never reaches merge-base once the commit can't be made local
        assert called["git"] is False

    def test_deepens_branch_when_shallow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The regression: an old green base sits past the shallow boundary, so the
        # branch must be deepened to its date before merge-base can connect it.
        deepened = {}

        def fake_deepen(branch, since):
            deepened["branch"], deepened["since"] = branch, since
            return True

        monkeypatch.setattr(detect, "git_is_shallow", lambda: True)
        monkeypatch.setattr(detect, "git_deepen_since", fake_deepen)
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=0))
        assert detect.is_local_ancestor("abc", "HEAD", since="2026-06-13T07:43:18.832Z", branch="master") is True
        assert deepened == {"branch": "master", "since": "2026-06-12"}

    def test_no_deepen_on_full_clone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # --shallow-since would truncate a complete clone; never deepen one.
        monkeypatch.setattr(detect, "git_is_shallow", lambda: False)
        monkeypatch.setattr(
            detect,
            "git_deepen_since",
            lambda *a, **kw: pytest.fail("must not deepen a full clone"),
        )
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=0))
        assert detect.is_local_ancestor("abc", "HEAD", since="2026-06-13T00:00:00Z", branch="master") is True


class TestTreeEquivalentAncestor:
    def test_returns_newest_matching_ancestor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: ref)
        monkeypatch.setattr(detect, "git_tree", lambda ref: "green_tree")
        seen_args: list[str] = []

        def fake_git(*args, **kw):
            seen_args.extend(args)
            return _fake_git_result("newest other_tree\nrewritten green_tree\nolder green_tree\n")

        monkeypatch.setattr(detect, "_git", fake_git)
        result = detect.tree_equivalent_ancestor("old_green", "head", since="2026-01-05T00:00:00Z")
        assert result == "rewritten"
        # the scan is bounded to history newer than the pipeline (1-day margin)
        assert "--since=2026-01-04" in seen_args

    def test_none_when_trees_diverge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: ref)
        monkeypatch.setattr(detect, "git_tree", lambda ref: "green_tree")
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("head other_tree\n"))
        assert detect.tree_equivalent_ancestor("old_green", "head") is None

    def test_fetches_unfetched_candidate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A rewritten-away sha is absent from the local clone; the helper must
        # fetch it itself rather than rely on a prior caller having done so.
        fetched: list[str] = []
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: None)
        monkeypatch.setattr(detect, "git_fetch_commit", lambda sha: fetched.append(sha) or True)
        monkeypatch.setattr(detect, "git_tree", lambda ref: "green_tree")
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("rewritten green_tree\n"))
        assert detect.tree_equivalent_ancestor("old_green", "head") == "rewritten"
        assert fetched == ["old_green"]

    def test_none_when_candidate_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: None)
        monkeypatch.setattr(detect, "git_fetch_commit", lambda sha: False)
        monkeypatch.setattr(detect, "git_tree", lambda ref: None)
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: pytest.fail("git log must not run"))
        assert detect.tree_equivalent_ancestor("missing", "head") is None


class TestNewestGreenPipeline:
    @staticmethod
    def _kw(**overrides):
        defaults = dict(
            head_sha="head",
            project_api="http://api/projects/1",
            token="t",
            token_kind="job",
            log_fn=lambda m: None,
        )
        defaults.update(overrides)
        return defaults

    def test_finds_first_ancestor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "_gl_api_get",
            lambda url, token, **kw: [
                {"id": 1, "sha": "newsha", "source": "push", "created_at": "2026-01-02"},
            ],
        )
        monkeypatch.setattr(detect, "is_local_ancestor", lambda sha, head, **kw: True)
        assert detect.newest_green_pipeline("master", **self._kw())["sha"] == "newsha"

    def test_skips_non_base_sources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A web (manual ROLES dispatch) and a parent_pipeline (cell child) are
        # skipped; the push behind them is the real base.
        monkeypatch.setattr(
            detect,
            "_gl_api_get",
            lambda url, token, **kw: (
                [
                    {"id": 3, "sha": "websha", "source": "web", "created_at": "2026-01-03"},
                    {"id": 2, "sha": "childsha", "source": "parent_pipeline", "created_at": "2026-01-03"},
                    {"id": 1, "sha": "pushsha", "source": "push", "created_at": "2026-01-01"},
                ]
                if "&page=1" in url
                else []
            ),
        )
        seen = []

        def fake_anc(sha, head, **kw):
            seen.append(sha)
            return True

        monkeypatch.setattr(detect, "is_local_ancestor", fake_anc)
        assert detect.newest_green_pipeline("master", **self._kw())["sha"] == "pushsha"
        # ancestry is only ever checked for the push pipeline
        assert seen == ["pushsha"]

    def test_accepts_successful_no_cells_push(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A docs-only push can safely advance the base because its diff contained
        # no role-relevant changes.
        monkeypatch.setattr(
            detect,
            "_gl_api_get",
            lambda url, token, **kw: (
                [
                    {"id": 2, "sha": "nocells", "source": "push", "created_at": "2026-01-05"},
                    {"id": 1, "sha": "ranmatrix", "source": "push", "created_at": "2026-01-01"},
                ]
                if "&page=1" in url
                else []
            ),
        )
        seen = []

        def fake_anc(sha, head, **kw):
            seen.append(sha)
            return True

        monkeypatch.setattr(detect, "is_local_ancestor", fake_anc)
        result = detect.newest_green_pipeline("master", **self._kw())
        assert result["sha"] == "nocells"
        assert seen == ["nocells"]

    def test_skips_non_ancestor_then_paginates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "&page=1" in url:
                return [{"id": 2, "sha": "divsha", "source": "push", "created_at": "2026-01-05"}]
            if "&page=2" in url:
                return [{"id": 1, "sha": "oldgreen", "source": "push", "created_at": "2026-01-01"}]
            return []

        monkeypatch.setattr(detect, "_gl_api_get", mock_api)
        monkeypatch.setattr(detect, "is_local_ancestor", lambda sha, head, **kw: sha == "oldgreen")
        monkeypatch.setattr(detect, "tree_equivalent_ancestor", lambda sha, head, **kw: None)
        logs = []
        result = detect.newest_green_pipeline("master", **self._kw(log_fn=logs.append))
        assert result["sha"] == "oldgreen"
        assert any("not an ancestor" in m for m in logs)

    def test_maps_green_pipeline_to_tree_equivalent_ancestor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "_gl_api_get",
            lambda url, token, **kw: (
                [{"id": 7, "sha": "oldgreen", "source": "push", "created_at": "2026-01-05"}] if "&page=1" in url else []
            ),
        )
        monkeypatch.setattr(detect, "is_local_ancestor", lambda *a, **kw: False)
        monkeypatch.setattr(
            detect,
            "tree_equivalent_ancestor",
            lambda sha, head, **kw: "rewritten_green",
        )
        logs = []
        result = detect.newest_green_pipeline("master", **self._kw(log_fn=logs.append))
        assert result["id"] == 7
        assert result["sha"] == "rewritten_green"
        assert any("green tree-equivalent ancestor" in m for m in logs)

    def test_none_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gl_api_get", lambda url, token, **kw: [])
        assert detect.newest_green_pipeline("master", **self._kw()) is None

    def test_none_on_api_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gl_api_get", lambda url, token, **kw: None)
        logs = []
        assert detect.newest_green_pipeline("master", **self._kw(log_fn=logs.append)) is None
        assert any("query failed" in m for m in logs)


class TestResolveGreenBaseGitlab:
    @staticmethod
    def _kw(**overrides):
        defaults = dict(
            project_api="http://api/projects/1",
            token="t",
            token_kind="job",
            head_sha="head",
            log_fn=lambda m: None,
        )
        defaults.update(overrides)
        return defaults

    def test_found_on_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "newest_green_pipeline",
            lambda branch, **kw: "feat_green" if branch == "feat" else None,
        )
        assert detect.resolve_green_base_gitlab(branch="feat", **self._kw()) == "feat_green"

    def test_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "newest_green_pipeline",
            lambda branch, **kw: None if branch == "feat" else "default_green",
        )
        logs = []
        result = detect.resolve_green_base_gitlab(
            branch="feat", default_branch="master", **self._kw(log_fn=logs.append)
        )
        assert result == "default_green"
        assert any("falling back" in m for m in logs)

    def test_no_fallback_when_on_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def mock(branch, **kw):
            calls["n"] += 1
            return

        monkeypatch.setattr(detect, "newest_green_pipeline", mock)
        assert detect.resolve_green_base_gitlab(branch="master", default_branch="master", **self._kw()) is None
        assert calls["n"] == 1

    def test_missing_inputs_return_none(self) -> None:
        assert detect.resolve_green_base_gitlab(branch="", **self._kw()) is None
        assert detect.resolve_green_base_gitlab(branch="m", **self._kw(token="")) is None
        assert detect.resolve_green_base_gitlab(branch="m", **self._kw(project_api="")) is None
        assert detect.resolve_green_base_gitlab(branch="m", **self._kw(head_sha="")) is None


class TestGitlabApiCreds:
    def _env(self, monkeypatch, **kv):
        for k in ("CI_API_V4_URL", "CI_PROJECT_ID", "GITLAB_API_TOKEN", "CI_JOB_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        for k, v in kv.items():
            monkeypatch.setenv(k, v)

    def test_prefers_private_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(
            monkeypatch,
            CI_API_V4_URL="http://api",
            CI_PROJECT_ID="1",
            GITLAB_API_TOKEN="pat",
            CI_JOB_TOKEN="jobtok",
        )
        assert detect._gitlab_api_creds() == ("http://api/projects/1", "pat", "private")

    def test_falls_back_to_job_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch, CI_API_V4_URL="http://api", CI_PROJECT_ID="1", CI_JOB_TOKEN="jobtok")
        assert detect._gitlab_api_creds() == ("http://api/projects/1", "jobtok", "job")

    def test_none_when_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch, CI_API_V4_URL="http://api", CI_PROJECT_ID="1")
        assert detect._gitlab_api_creds() is None

    def test_none_when_no_api_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch, CI_PROJECT_ID="1", CI_JOB_TOKEN="jobtok")
        assert detect._gitlab_api_creds() is None


# ---------------------------------------------------------------------------
# Cell runtime ordering
# ---------------------------------------------------------------------------


class TestSortSpecsByRuntime:
    def test_longest_first(self) -> None:
        runtimes = {"a:lab": 100.0, "b:lab": 300.0, "c:lab": 200.0}
        assert detect.sort_specs_by_runtime(["a:lab", "b:lab", "c:lab"], runtimes) == [
            "b:lab",
            "c:lab",
            "a:lab",
        ]

    def test_unmeasured_sort_first(self) -> None:
        # A spec with no recorded runtime leads, so an unmeasured (possibly long)
        # cell is never left to start last.
        runtimes = {"measured:lab": 500.0}
        out = detect.sort_specs_by_runtime(["measured:lab", "new:lab"], runtimes)
        assert out == ["new:lab", "measured:lab"]

    def test_ties_break_by_name(self) -> None:
        runtimes = {"z:lab": 100.0, "a:lab": 100.0}
        assert detect.sort_specs_by_runtime(["z:lab", "a:lab"], runtimes) == ["a:lab", "z:lab"]

    def test_empty_runtimes_is_name_order(self) -> None:
        assert detect.sort_specs_by_runtime(["c:lab", "a:lab", "b:lab"], {}) == [
            "a:lab",
            "b:lab",
            "c:lab",
        ]


class TestGlApiGetAll:
    def test_paginates_until_short_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if url.endswith("page=1"):
                return [{"id": i} for i in range(100)]
            if url.endswith("page=2"):
                return [{"id": 100}]
            return []

        monkeypatch.setattr(detect, "_gl_api_get", mock_api)
        items = detect._gl_api_get_all("http://api/jobs", "t", "job")
        assert len(items) == 101

    def test_none_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gl_api_get", lambda url, token, **kw: None)
        assert detect._gl_api_get_all("http://api/jobs", "t", "job") is None


class TestCollectCellJobs:
    def test_reads_named_child_and_collapses_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_get_all(base_url, token, token_kind, **kw):
            if base_url.endswith("/pipelines/1/bridges"):
                return [
                    {"name": "unrelated", "downstream_pipeline": {"id": 99}},
                    {"name": "test_cells", "downstream_pipeline": {"id": 2}},
                ]
            if base_url.endswith("/pipelines/2/jobs"):
                # An earlier attempt and its retry; the retry (higher id) wins.
                return [
                    {"name": "nginx:lab", "id": 20, "duration": 111},
                    {"name": "nginx:lab", "id": 21, "duration": 222},
                ]
            return []

        monkeypatch.setattr(detect, "_gl_api_get_all", mock_get_all)
        jobs = detect._collect_cell_jobs("http://api", 1, "t", "job")
        assert jobs["nginx:lab"]["id"] == 21
        assert jobs["nginx:lab"]["duration"] == 222

    def test_empty_without_test_cells_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "_gl_api_get_all",
            lambda *a, **kw: [{"name": "unrelated", "downstream_pipeline": {"id": 99}}],
        )
        assert detect._collect_cell_jobs("http://api", 1, "t", "job") == {}


class TestCellRuntimes:
    def test_keeps_only_successful_cell_jobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gitlab_api_creds", lambda: ("http://api", "t", "job"))
        monkeypatch.setattr(detect, "_recent_pipeline_ids", lambda *a, **k: [42])
        monkeypatch.setattr(
            detect,
            "_collect_cell_jobs",
            lambda *a, **k: {
                "nginx:lab": {"id": 1, "duration": 200, "status": "success"},
                "zfs:lab": {"id": 2, "duration": 50, "status": "failed"},  # failed -- dropped
                "running:lab": {"id": 3, "duration": None, "status": "running"},  # no duration
            },
        )
        logs = []
        runtimes = detect._cell_runtimes("master", logs.append)
        assert runtimes == {"nginx:lab": 200}
        assert any("1 recent pipeline(s)" in m for m in logs)

    def test_takes_median_across_pipelines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A cell's runtime is the median of its successful samples; an outlier
        # run doesn't dominate. The union of cell names is covered.
        monkeypatch.setattr(detect, "_gitlab_api_creds", lambda: ("http://api", "t", "job"))
        monkeypatch.setattr(detect, "_recent_pipeline_ids", lambda *a, **k: [4, 3, 2])
        per_pipeline = {
            4: {"nginx:lab": {"id": 40, "duration": 100, "status": "success"}},
            3: {"nginx:lab": {"id": 30, "duration": 300, "status": "success"}},
            2: {
                "nginx:lab": {"id": 20, "duration": 1000, "status": "success"},  # outlier
                "zfs:lab": {"id": 21, "duration": 400, "status": "success"},
            },
        }
        monkeypatch.setattr(detect, "_collect_cell_jobs", lambda pa, pid, t, tk: per_pipeline[pid])
        runtimes = detect._cell_runtimes("master", lambda m: None)
        # median([100, 300, 1000]) == 300 -- the outlier doesn't pull it up.
        assert runtimes == {"nginx:lab": 300, "zfs:lab": 400}

    def test_failed_in_newest_falls_back_to_older(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A cell that failed in the newest run but passed in an older one still
        # gets a sample -- the whole point of harvesting per-job, not per-pipeline.
        monkeypatch.setattr(detect, "_gitlab_api_creds", lambda: ("http://api", "t", "job"))
        monkeypatch.setattr(detect, "_recent_pipeline_ids", lambda *a, **k: [3, 2])
        per_pipeline = {
            3: {"nginx:lab": {"id": 30, "duration": 5, "status": "failed"}},
            2: {"nginx:lab": {"id": 20, "duration": 300, "status": "success"}},
        }
        monkeypatch.setattr(detect, "_collect_cell_jobs", lambda pa, pid, t, tk: per_pipeline[pid])
        runtimes = detect._cell_runtimes("master", lambda m: None)
        assert runtimes == {"nginx:lab": 300}

    def test_no_creds_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gitlab_api_creds", lambda: None)
        assert detect._cell_runtimes("master", lambda m: None) == {}


class TestRecentPipelineIds:
    def test_filters_sources_and_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = {}

        def mock_get(url, token, **kw):
            seen["url"] = url
            return [
                {"id": 5, "source": "push"},
                {"id": 4, "source": "web"},  # manual dispatch -- excluded
                {"id": 3, "source": "schedule"},
                {"id": 2, "source": "parent_pipeline"},  # cell child -- excluded
                {"id": 1, "source": "push"},
            ]

        monkeypatch.setattr(detect, "_gl_api_get", mock_get)
        ids = detect._recent_pipeline_ids("master", "http://api", "t", "job", 2)
        # newest-first pushes, capped at the limit
        assert ids == [5, 1]
        # no status filter -- failed-overall pipelines must be sampled too
        assert "status=success" not in seen["url"]

    def test_empty_on_no_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gl_api_get", lambda url, token, **kw: None)
        assert detect._recent_pipeline_ids("master", "http://api", "t", "job", 10) == []


# ---------------------------------------------------------------------------
# Role dependency map
# ---------------------------------------------------------------------------


class TestWalkTasks:
    def test_finds_import_role(self) -> None:
        inv = defaultdict(set)
        detect._walk_tasks([{"import_role": {"name": "nginx"}}], "homepage", inv)
        assert "homepage" in inv["nginx"]

    def test_finds_include_role(self) -> None:
        inv = defaultdict(set)
        detect._walk_tasks([{"include_role": {"name": "podman"}}], "redis", inv)
        assert "redis" in inv["podman"]

    def test_walks_block(self) -> None:
        inv = defaultdict(set)
        detect._walk_tasks([{"block": [{"import_role": {"name": "systemd_unit"}}]}], "nginx", inv)
        assert "nginx" in inv["systemd_unit"]

    def test_walks_rescue(self) -> None:
        inv = defaultdict(set)
        detect._walk_tasks([{"rescue": [{"import_role": {"name": "helper"}}]}], "consumer", inv)
        assert "consumer" in inv["helper"]

    def test_walks_always(self) -> None:
        inv = defaultdict(set)
        detect._walk_tasks([{"always": [{"import_role": {"name": "cleanup"}}]}], "svc", inv)
        assert "svc" in inv["cleanup"]

    def test_skips_non_dict_tasks(self) -> None:
        inv = defaultdict(set)
        detect._walk_tasks(["string_task", 42, None], "role", inv)
        assert len(inv) == 0

    def test_skips_non_list(self) -> None:
        inv = defaultdict(set)
        detect._walk_tasks("not a list", "role", inv)
        assert len(inv) == 0

    def test_ignores_role_without_name_key(self) -> None:
        inv = defaultdict(set)
        detect._walk_tasks([{"import_role": {"tasks_from": "site"}}], "consumer", inv)
        assert len(inv) == 0


class TestBuildRoleDepsMap:
    def test_builds_map(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        consumer_dir = tmp_path / "roles" / "consumer" / "tasks"
        consumer_dir.mkdir(parents=True)
        (consumer_dir / "main.yml").write_text("- import_role:\n    name: helper\n")
        helper_dir = tmp_path / "roles" / "helper" / "tasks"
        helper_dir.mkdir(parents=True)
        (helper_dir / "main.yml").write_text("- debug:\n    msg: hello\n")
        result = detect.build_role_deps_map()
        assert result.get("helper") == ["consumer"]

    def test_empty_roles_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "roles").mkdir()
        assert detect.build_role_deps_map() == {}

    def test_rejects_parse_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        bad_dir = tmp_path / "roles" / "broken" / "tasks"
        bad_dir.mkdir(parents=True)
        (bad_dir / "main.yml").write_text(": : :\n  - [\n")
        with pytest.raises(detect.yaml.YAMLError):
            detect.build_role_deps_map()


class TestListTestableRoles:
    def test_finds_roles_with_main(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        for name in ["alpha", "beta"]:
            (tmp_path / "roles" / name / "tasks").mkdir(parents=True)
            (tmp_path / "roles" / name / "tasks" / "main.yml").touch()
        (tmp_path / "roles" / "helper_only" / "tasks").mkdir(parents=True)
        assert detect.list_testable_roles() == ["alpha", "beta"]

    def test_no_roles_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert detect.list_testable_roles() == []


def _render_child_doc(
    specs: list[str],
    site_test: bool,
    target: str = "aws_qemu",
) -> dict:
    """Render test_child.yml.j2 and parse it back to a dict for assertions."""
    return detect.yaml.safe_load(detect.render_child_pipeline(specs, site_test, target=target))


class TestGitlabChangeMatrix:
    @staticmethod
    def _empty_diff(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        seen: list[str] = []
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: ref)
        monkeypatch.setattr(detect, "git_diff_files", lambda base: seen.append(base) or [])
        monkeypatch.setattr(detect, "list_testable_roles", list)
        monkeypatch.setattr(detect, "build_role_deps_map", dict)
        return seen

    @pytest.mark.parametrize("branch", ["master", "feature"])
    def test_no_green_runs_full_universe(self, monkeypatch: pytest.MonkeyPatch, branch: str) -> None:
        monkeypatch.delenv("CI_BASE_REF", raising=False)
        monkeypatch.setenv("CI_COMMIT_BRANCH", branch)
        monkeypatch.setenv("CI_DEFAULT_BRANCH", "master")
        monkeypatch.setenv("CI_COMMIT_BEFORE_SHA", "red_previous_tip")
        monkeypatch.setattr(detect, "_full_universe_specs", lambda: ["full"])
        monkeypatch.setattr(detect, "git_diff_files", lambda base: pytest.fail("red tip must not be used"))
        logs = []
        assert detect._gitlab_change_matrix(None, logs.append) == (["full"], True)
        assert any("no green base" in line for line in logs)

    def test_explicit_base_still_wins_on_default_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_BASE_REF", "explicit")
        monkeypatch.setenv("CI_COMMIT_BRANCH", "master")
        monkeypatch.setenv("CI_DEFAULT_BRANCH", "master")
        monkeypatch.setenv("CI_COMMIT_BEFORE_SHA", "red_previous_tip")
        seen = self._empty_diff(monkeypatch)
        assert detect._gitlab_change_matrix(None, lambda _: None) == ([], False)
        assert seen == ["explicit"]

    @pytest.mark.parametrize("path", ["test/host_vars/lab.yml", "group_vars/storage_lab.yml"])
    def test_lab_site_inputs_trigger_site_jobs(self, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
        monkeypatch.setenv("CI_BASE_REF", "explicit")
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: ref)
        monkeypatch.setattr(detect, "git_diff_files", lambda base: [path])
        monkeypatch.setattr(detect, "_full_universe_specs", lambda: ["full"])
        assert detect._gitlab_change_matrix(None, lambda _: None) == (["full"], True)

    def test_site_playbook_change_triggers_only_site_jobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_BASE_REF", "explicit")
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: ref)
        monkeypatch.setattr(detect, "git_diff_files", lambda base: ["site.yml"])
        monkeypatch.setattr(detect, "list_testable_roles", list)
        monkeypatch.setattr(detect, "build_role_deps_map", dict)

        assert detect._gitlab_change_matrix(None, lambda _: None) == ([], True)


class TestRenderChildPipeline:
    def test_one_job_per_spec(self) -> None:
        doc = _render_child_doc(["nginx:lab", "podman:lab:resolute"], site_test=False)
        assert "tags" not in doc["default"]
        assert "image" not in doc["default"]
        assert doc["stages"] == ["test1", "test2"]
        # All cells run the qemu backend; the harness defaults to it, so there is
        # no HOMELAB_TEST_BACKEND variable.
        assert "HOMELAB_TEST_BACKEND" not in doc[".cell"]["variables"]
        assert doc[".cell"]["variables"]["HOMELAB_TEST_IN_AWS"] == "true"
        assert "HOMELAB_TEST_AWS_REGION" not in doc[".cell"]["variables"]
        assert doc[".arm_cell"]["tags"] == ["aws-shell-qemu-arm"]
        assert doc[".arm_cell"]["variables"]["ARCH"] == "aarch64"
        assert "HOMELAB_TEST_AWS_REGION" not in doc[".arm_cell"]["variables"]
        # No spot retry on the qemu targets.
        assert "retry" not in doc[".cell"]
        # nginx:lab defaults to Noble; podman:lab:resolute is explicit.
        assert doc["nginx:lab"]["variables"] == {"ROLE": "nginx", "VARIANT": "lab", "UBUNTU": "noble"}
        assert doc["podman:lab:resolute"]["variables"] == {
            "ROLE": "podman",
            "VARIANT": "lab",
            "UBUNTU": "resolute",
        }
        assert doc["nginx:lab"]["extends"] == ".cell"
        assert "_site_test:lab" not in doc
        assert "_site_check:lab" not in doc
        assert "no_cells" not in doc

    def test_cells_auto_run_by_default(self) -> None:
        doc = _render_child_doc(["nginx:lab"], site_test=True)
        assert "when" not in doc[".cell"]
        assert "allow_failure" not in doc[".cell"]

    def test_no_cells_placeholder_runs_on_hosted_runner(self) -> None:
        # A no-cell pipeline does not consume a persistent or autoscaled qemu
        # runner just to report there is nothing to test.
        doc = _render_child_doc([], site_test=False)
        assert doc["no_cells"]["tags"] == ["saas-linux-small-amd64"]
        assert doc["no_cells"]["image"] == "alpine:3.22"
        assert "tags" not in doc["default"]

    def test_site_test_job_added(self) -> None:
        doc = _render_child_doc(["nginx:lab"], site_test=True)
        assert "_site_test:lab" in doc
        assert doc["_site_test:lab"]["timeout"] == "60m"
        assert "_site_test:lab:resolute" in doc
        assert doc["_site_test:lab:resolute"]["timeout"] == "60m"
        resolute_script = "\n".join(doc["_site_test:lab:resolute"]["script"])
        assert 'site_test.py --ubuntu "$UBUNTU" --timeout 3000' in resolute_script
        assert "_site_check:lab" in doc
        assert doc["_site_check:lab"]["timeout"] == "35m"
        script = "\n".join(doc["_site_check:lab"]["script"])
        assert "site_test.py --check --timeout 1800" in script
        assert "timeout --kill-after=30s 1860" in script
        assert "no_cells" not in doc

    def test_cells_yield_to_the_site_converge(self) -> None:
        # Cells share hosts with the critical-path site converge, so they run
        # at lower CPU priority and a higher OOM score; the site jobs do not.
        doc = _render_child_doc(["nginx:lab"], site_test=True)
        cell_script = "\n".join(doc["nginx:lab"]["script"])
        assert "nice -n 10 choom -n 500 -- mise exec --" in cell_script
        for job in ("_site_test:lab", "_site_test:lab:resolute", "_site_check:lab"):
            script = "\n".join(doc[job]["script"])
            assert "nice" not in script
            assert "choom" not in script

    def test_site_test_seeded_first_in_leading_stage(self) -> None:
        # _site_test must be picked before the matrix: it lives in a dedicated
        # `site` stage declared ahead of the cell stages, so GitLab (which seeds
        # build ids stage-by-stage) gives it the lowest id and a runner claims
        # it first. needs:[] (from .cell) keeps it parallel, so the leading
        # stage never gates the cells.
        doc = _render_child_doc(["nginx:lab", "podman:lab:noble"], site_test=True)
        assert doc["stages"] == ["site", "test1", "test2"]
        assert doc["_site_test:lab"]["stage"] == "site"
        assert doc["_site_test:lab:resolute"]["stage"] == "site"
        assert doc["_site_check:lab"]["stage"] == "site"
        for job in ("_site_test:lab", "_site_check:lab"):
            assert doc[job]["variables"] == {"VARIANT": "lab", "UBUNTU": detect.DEFAULT_UBUNTU}
        assert doc["_site_test:lab:resolute"]["variables"] == {"VARIANT": "lab", "UBUNTU": "resolute"}
        assert doc[".cell"]["needs"] == []

    def test_site_test_only_stage(self) -> None:
        # site_test with no cells still seeds a single leading `site` stage.
        doc = _render_child_doc([], site_test=True)
        assert doc["stages"] == ["site"]
        assert doc["_site_test:lab"]["stage"] == "site"
        assert doc["_site_test:lab:resolute"]["stage"] == "site"
        assert doc["_site_check:lab"]["stage"] == "site"
        assert "no_cells" not in doc

    def test_empty_gets_noop_placeholder(self) -> None:
        doc = _render_child_doc([], site_test=False)
        assert "no_cells" in doc
        assert "_site_test:lab" not in doc
        assert "_site_test:lab:resolute" not in doc
        assert "_site_check:lab" not in doc
        # No cell jobs beyond the scaffolding + placeholder.
        jobs = [k for k in doc if k not in ("default", "stages", ".cell", ".arm_cell")]
        assert jobs == ["no_cells"]

    def test_arm_scaffold_uses_frankfurt_images_and_bounded_runtime(self) -> None:
        doc = _render_child_doc(["apt:lab"], site_test=False)
        scaffold = doc[".arm_cell"]
        before_script = "\n".join(scaffold["before_script"])

        assert scaffold["timeout"] == "45m"
        assert scaffold["needs"] == []
        assert "--region" not in before_script
        assert "--bucket" not in before_script
        assert 'if [ "$VARIANT" != "minimal" ]; then mise run ci:hydrate-qemu-images' in before_script
        assert "--architecture aarch64; fi" in before_script
        assert scaffold["after_script"] == ['rm -f "$CI_PROJECT_DIR/.aws_web_identity_token"']
        assert scaffold["artifacts"]["paths"] == ["test/out/"]

    def test_arm_metadata_preserves_change_selection_and_gates(self) -> None:
        doc = _render_child_doc(
            ["apt:lab:resolute", "boot:minimal", "fan2go:lab", "nginx:lab"],
            site_test=False,
        )

        assert "apt:lab:aarch64" in doc
        assert "boot:minimal:aarch64" not in doc
        assert "fan2go:lab:aarch64" not in doc
        assert "nginx:lab:aarch64" not in doc
        assert doc["stages"][-1] == "arm"
        assert "when" not in doc["apt:lab:aarch64"]
        assert "allow_failure" not in doc["apt:lab:aarch64"]

    def test_arm_minimal_uses_upstream_cloud_image(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        meta_dir = tmp_path / "roles" / "minimal_probe" / "meta"
        meta_dir.mkdir(parents=True)
        (meta_dir / "test.yml").write_text("machines:\n  lab:\n  minimal:\narm:\n  - minimal\n")

        doc = _render_child_doc(["minimal_probe:minimal"], site_test=False)

        assert "minimal_probe:minimal:aarch64" in doc
        assert doc["minimal_probe:minimal:aarch64"]["variables"]["VARIANT"] == "minimal"
        assert 'if [ "$VARIANT" != "minimal" ]; then mise run ci:hydrate-qemu-images' in "\n".join(
            doc[".arm_cell"]["before_script"]
        )

    def test_arm_release_entry_adds_that_releases_cell(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        meta_dir = tmp_path / "roles" / "release_probe" / "meta"
        meta_dir.mkdir(parents=True)
        (meta_dir / "test.yml").write_text("arm:\n  - lab\n  - lab:resolute\n")

        doc = _render_child_doc(["release_probe:lab"], site_test=False)

        assert doc["release_probe:lab:aarch64"]["variables"]["UBUNTU"] == "noble"
        assert doc["release_probe:lab:resolute:aarch64"]["variables"]["UBUNTU"] == "resolute"

    def test_full_universe_keeps_all_x86_and_arm_cells(self) -> None:
        specs = detect._full_universe_specs()
        doc = _render_child_doc(specs, site_test=True)
        x86_jobs = [name for name in specs if name in doc]
        arm_jobs = {
            f"{role}:{entry}:aarch64"
            for role in detect.list_testable_roles()
            for entry in detect.load_role_test_config(role).arm_machines
        }

        assert len(x86_jobs) == len(specs)
        assert arm_jobs
        assert {name for name in doc if name.endswith(":aarch64")} == arm_jobs

    def test_arm_lane_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOMELAB_CI_ARM", "false")
        doc = _render_child_doc(detect._full_universe_specs(), site_test=True)

        assert "arm" not in doc["stages"]
        assert not any(name.endswith(":aarch64") for name in doc)

    def test_lab_target_never_renders_arm_jobs(self) -> None:
        doc = _render_child_doc(["apt:lab"], site_test=False, target="lab")

        assert ".arm_cell" not in doc
        assert not any(name.endswith(":aarch64") for name in doc)

    def test_lab_target_uses_shell_qemu_runner(self) -> None:
        doc = _render_child_doc(["nginx:lab"], site_test=False, target="lab")
        assert "tags" not in doc["default"]
        assert "image" not in doc["default"]
        assert doc[".cell"]["tags"] == ["lab-shell-qemu"]
        # The harness defaults to the qemu backend, so no HOMELAB_TEST_BACKEND.
        assert "HOMELAB_TEST_BACKEND" not in doc[".cell"]["variables"]
        # lab's shell runner is on the operator LAN: qemu guest, not in AWS.
        assert doc[".cell"]["variables"]["HOMELAB_TEST_IN_AWS"] == "false"
        assert "HOMELAB_TEST_AWS_REGION" not in doc[".cell"]["variables"]
        assert ".arm_cell" not in doc
        # lab's shell runner is not the baked AMI -- no /opt/mise to point at.
        assert "MISE_DATA_DIR" not in doc[".cell"]["variables"]
        assert "image" not in doc[".cell"]
        # lab boots images from local disk: no OIDC, so id_tokens is dropped.
        assert "id_tokens" not in doc[".cell"]
        assert "retry" not in doc[".cell"]

        joined = "\n".join(doc[".cell"]["before_script"])
        assert "HOMELAB_VAULT_PASSWORD_TEST" in joined
        assert "CI_CELL_SSH_KEY" not in joined
        # lab boots images straight from /mnt/scratch/homelab_ci: no OIDC role
        # assumption, no sts call, no object store, and no hydration at all.
        assert detect.CELL_ROLE_ARN not in joined
        assert "sts get-caller-identity" not in joined
        assert "AWS_ROLE_ARN" not in joined
        assert "AWS_WEB_IDENTITY_TOKEN_FILE" not in joined
        assert "HOMELAB_CI_MINIO_ACCESS_KEY" not in joined
        assert "HOMELAB_CI_S3_ENDPOINT" not in joined
        assert "ci:hydrate-qemu-images" not in joined
        assert "--upstream-mirrors" not in "\n".join(doc["nginx:lab"]["script"])

    def test_aws_qemu_target_uses_shell_qemu_runner(self) -> None:
        doc = _render_child_doc(["nginx:lab"], site_test=True, target="aws_qemu")
        assert "tags" not in doc["default"]
        assert "image" not in doc["default"]
        assert doc[".cell"]["tags"] == ["aws-shell-qemu"]
        assert "HOMELAB_TEST_BACKEND" not in doc[".cell"]["variables"]
        # Qemu backend but an AWS host: the guest egresses through AWS, so it
        # must take the in-region-mirror / public-DNS path.
        assert doc[".cell"]["variables"]["HOMELAB_TEST_IN_AWS"] == "true"
        # This target's shell host is the baked qemu-host AMI, so the cell points
        # mise/uv at the /opt caches to skip the toolchain re-download.
        assert doc[".cell"]["variables"]["MISE_DATA_DIR"] == "/opt/mise"
        assert doc[".cell"]["variables"]["UV_CACHE_DIR"] == "/opt/uv-cache"
        assert doc[".cell"]["variables"]["UV_LINK_MODE"] == "copy"
        assert "image" not in doc[".cell"]
        assert doc[".cell"]["id_tokens"] == {"GITLAB_OIDC_TOKEN": {"aud": "sts.amazonaws.com"}}
        assert "retry" not in doc[".cell"]
        assert doc["_site_test:lab"]["extends"] == ".cell"
        assert "tags" not in doc["_site_test:lab"]
        assert doc["_site_test:lab:resolute"]["extends"] == ".cell"
        assert "tags" not in doc["_site_test:lab:resolute"]
        assert doc["_site_check:lab"]["extends"] == ".cell"
        assert "tags" not in doc["_site_check:lab"]

        joined = "\n".join(doc[".cell"]["before_script"])
        assert "HOMELAB_VAULT_PASSWORD_TEST" in joined
        assert "CI_CELL_SSH_KEY" not in joined
        assert "ssh-add" not in joined
        assert detect.CELL_ROLE_ARN in joined
        expected_oidc = (
            f'source mise-tasks/ci/aws-oidc.sh {detect.CELL_ROLE_ARN} "aws_qemu-cell-$CI_JOB_ID" --cache-dir'
        )
        assert expected_oidc in joined
        # aws_qemu reads from AWS S3 via OIDC -- never the lab MinIO mirror.
        assert "HOMELAB_CI_MINIO_ACCESS_KEY" not in joined
        assert "HOMELAB_CI_S3_ENDPOINT" not in joined
        assert 'if [ "$VARIANT" != "minimal" ]; then mise run ci:hydrate-qemu-images' in joined
        assert '"$VARIANT" --ubuntu "$UBUNTU"; fi' in joined
        assert "--upstream-mirrors" not in "\n".join(doc["nginx:lab"]["script"])
        assert "--upstream-mirrors" not in "\n".join(doc["_site_test:lab"]["script"])
        assert "--upstream-mirrors" not in "\n".join(doc["_site_test:lab:resolute"]["script"])


class TestEmitGitlab:
    @pytest.mark.parametrize("target", ["aws_qemu", "lab"])
    def test_writes_child_with_cells(self, tmp_path: Path, target: str) -> None:
        child = tmp_path / "child.yml"
        rc = detect._emit_gitlab(["zfs:lab", "zfs:pug"], False, str(child), {}, lambda *_: None, target=target)
        assert rc == 0
        loaded = detect.yaml.safe_load(child.read_text())
        assert "zfs:lab" in loaded
        assert "zfs:pug" in loaded
        assert "no_cells" not in loaded

    def test_orders_cells_longest_first(self, tmp_path: Path) -> None:
        # The emitted cell jobs follow the sorted order: unmeasured first, then
        # the rest longest-first. The child YAML preserves that job order.
        child = tmp_path / "child.yml"
        runtimes = {"a:lab": 100.0, "b:lab": 300.0}
        detect._emit_gitlab(["a:lab", "b:lab", "c:lab"], False, str(child), runtimes, lambda *_: None)
        text = child.read_text()
        order = [text.index(f'"{name}":') for name in ("c:lab", "b:lab", "a:lab")]
        assert order == sorted(order)

    def test_full_universe_includes_declared_lab_cells(self) -> None:
        expected = {
            "gitlab_runner:lab",
            "nginx:lab",
            "podman:lab",
            "swap:lab",
            "zfs:lab",
        }

        assert expected <= set(detect._full_universe_specs())


class TestCmdGitlab:
    @pytest.fixture(autouse=True)
    def _offline_full_universe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Keep these tests offline: a real CI environment exports the GitLab API
        # vars, which would otherwise drive a live green-pipeline lookup.
        monkeypatch.setattr(detect, "_gitlab_api_creds", lambda: None)
        monkeypatch.setattr(detect, "_full_universe_specs", lambda: ["nginx:lab"])

    @pytest.mark.parametrize(
        ("args", "pipeline_source", "roles"),
        [
            pytest.param(["--all"], None, None, id="all-flag"),
            pytest.param([], "web", "ALL", id="dispatch-all"),
        ],
    )
    def test_full_universe_triggers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        args: list[str],
        pipeline_source: str | None,
        roles: str | None,
    ) -> None:
        for name, value in {"CI_PIPELINE_SOURCE": pipeline_source, "ROLES": roles}.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        child = tmp_path / "child.yml"
        assert detect._cmd_gitlab([*args, "--child-path", str(child)]) == 0
        loaded = detect.yaml.safe_load(child.read_text())
        assert {"nginx:lab", "_site_test:lab"} <= loaded.keys()

    @pytest.mark.parametrize(
        ("roles", "expect_site", "expect_cells"),
        [
            pytest.param("SITE", True, False, id="site-only"),
            pytest.param("site", True, False, id="site-lowercase"),
            pytest.param("SITE,nginx", True, True, id="site-with-a-role"),
            pytest.param("nginx", False, True, id="role-only"),
        ],
    )
    def test_site_dispatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        roles: str,
        expect_site: bool,
        expect_cells: bool,
    ) -> None:
        """SITE dispatches the full-fleet converge without the whole universe.

        Without it the converge can only be reached through ROLES=ALL, which
        drags in every cell and costs a full matrix to exercise one play.
        """
        monkeypatch.setenv("CI_PIPELINE_SOURCE", "web")
        monkeypatch.setenv("ROLES", roles)
        child = tmp_path / "child.yml"
        assert detect._cmd_gitlab(["--child-path", str(child)]) == 0
        loaded = detect.yaml.safe_load(child.read_text())
        assert ("_site_test:lab" in loaded) is expect_site
        assert any(key == "nginx:lab" for key in loaded) is expect_cells

    @pytest.mark.parametrize(
        ("target", "runner_tag", "in_aws"),
        [
            pytest.param("lab", "lab-shell-qemu", "false", id="lab"),
            pytest.param("aws_qemu", "aws-shell-qemu", "true", id="aws-qemu"),
            pytest.param(None, "aws-shell-qemu", "true", id="default"),
        ],
    )
    def test_target_selection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        target: str | None,
        runner_tag: str,
        in_aws: str,
    ) -> None:
        if target is None:
            monkeypatch.delenv("HOMELAB_CI_TARGET", raising=False)
        else:
            monkeypatch.setenv("HOMELAB_CI_TARGET", target)
        child = tmp_path / "child.yml"
        assert detect._cmd_gitlab(["--all", "--child-path", str(child)]) == 0
        loaded = detect.yaml.safe_load(child.read_text())
        assert loaded[".cell"]["tags"] == [runner_tag]
        assert loaded[".cell"]["variables"]["HOMELAB_TEST_IN_AWS"] == in_aws

    def test_aws_target_automatically_selects_arm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_full_universe_specs", lambda: ["apt:lab"])
        child = tmp_path / "child.yml"

        assert detect._cmd_gitlab(["--all", "--child-path", str(child)]) == 0
        loaded = detect.yaml.safe_load(child.read_text())

        assert "apt:lab:aarch64" in loaded

    def test_main_renders_child(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        child = tmp_path / "child.yml"
        monkeypatch.setattr("sys.argv", ["detect.py", "--all", "--child-path", str(child)])
        assert detect.main() == 0
        assert "nginx:lab" in detect.yaml.safe_load(child.read_text())

    def test_unknown_command_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["detect.py", "bogus"])
        assert detect.main() == 2
