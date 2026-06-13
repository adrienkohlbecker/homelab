"""Unit tests for mise-tasks/ci/detect.py — CI change-detection pipeline.

Tests path classification regexes, file classification, release-cell
propagation, git helpers, GitLab pipelines-API green-base resolution, role
dependency map, and the ``gitlab`` child-pipeline command.
"""

import importlib
import json
import subprocess
import urllib.error
from collections import defaultdict
from pathlib import Path
import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "mise-tasks" / "ci" / "detect.py"


def _load():
    spec = importlib.util.spec_from_file_location("detect", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


detect = _load()


# ---------------------------------------------------------------------------
# FULL_UNIVERSE_RE
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ROLE_PATH_RE
# ---------------------------------------------------------------------------


class TestRolePathRe:
    def test_extracts_role_name(self) -> None:
        m = detect.ROLE_PATH_RE.match("roles/nginx/tasks/main.yml")
        assert m and m.group(1) == "nginx"

    def test_nested_path(self) -> None:
        m = detect.ROLE_PATH_RE.match("roles/podman/templates/foo.j2")
        assert m and m.group(1) == "podman"

    def test_non_role_excluded(self) -> None:
        assert not detect.ROLE_PATH_RE.match("test/machine.py")

    def test_bare_roles_dir_excluded(self) -> None:
        assert not detect.ROLE_PATH_RE.match("roles/")


# ---------------------------------------------------------------------------
# classify_changed_files
# ---------------------------------------------------------------------------


class TestClassifyChangedFiles:
    def test_role_detection(self) -> None:
        result = detect.classify_changed_files(
            [
                "roles/nginx/tasks/main.yml",
                "roles/podman/templates/foo.j2",
            ]
        )
        assert result.direct_roles == ["nginx", "podman"]
        assert not result.packer_sources_affected
        assert not result.ci_image_changed
        assert result.full_universe_paths == []

    def test_full_universe_trigger(self) -> None:
        result = detect.classify_changed_files(["group_vars/all/main.yml"])
        assert result.full_universe_paths == ["group_vars/all/main.yml"]

    def test_packer_changed(self) -> None:
        result = detect.classify_changed_files(["packer/qemu.pkr.hcl"])
        assert result.packer_sources_affected

    def test_ci_image_on_master(self) -> None:
        result = detect.classify_changed_files(["Dockerfile"], is_master_push=True)
        assert result.ci_image_changed is True

    def test_ci_image_off_master(self) -> None:
        result = detect.classify_changed_files(["Dockerfile"], is_master_push=False)
        assert result.ci_image_changed is False

    def test_zbm_changed(self) -> None:
        for path in ("zbm/config.yaml", "zbm/dracut.conf.d/recovery.conf", "mise-tasks/zbm/build.sh"):
            assert detect.classify_changed_files([path]).zbm_changed is True, path

    def test_zbm_unchanged(self) -> None:
        assert detect.classify_changed_files(["roles/nginx/tasks/main.yml"]).zbm_changed is False
        # mise.toml carries ZBM_VERSION — a version bump must trigger the zbm build.
        assert detect.classify_changed_files(["mise.toml"]).zbm_changed is True

    def test_mixed_paths(self) -> None:
        paths = [
            "roles/nginx/tasks/main.yml",
            "packer/scripts/chroot.sh",
            "group_vars/all/main.yml",
            "Dockerfile",
        ]
        result = detect.classify_changed_files(paths, is_master_push=True)
        assert result.direct_roles == ["nginx"]
        assert result.packer_sources_affected
        assert result.ci_image_changed is True
        assert result.full_universe_paths == ["group_vars/all/main.yml"]

    def test_empty_paths(self) -> None:
        result = detect.classify_changed_files([])
        assert result == detect.ChangeClassification([], [], set(), False, set(), False)

    def test_blank_lines_ignored(self) -> None:
        result = detect.classify_changed_files(["", "roles/nginx/tasks/main.yml", ""])
        assert result.direct_roles == ["nginx"]

    def test_deduplicates_roles(self) -> None:
        result = detect.classify_changed_files(
            [
                "roles/nginx/tasks/main.yml",
                "roles/nginx/templates/site.conf.j2",
            ]
        )
        assert result.direct_roles == ["nginx"]

    def test_multiple_full_universe_paths(self) -> None:
        result = detect.classify_changed_files(
            [
                "mise.toml",
                "pyproject.toml",
                "uv.lock",
            ]
        )
        assert result.full_universe_paths == ["mise.toml", "pyproject.toml", "uv.lock"]

    def test_packer_and_role_simultaneous(self) -> None:
        result = detect.classify_changed_files(
            [
                "packer/scripts/chroot.sh",
                "roles/zfs/tasks/main.yml",
            ]
        )
        assert result.packer_sources_affected
        assert result.direct_roles == ["zfs"]

    def test_machine_universe_box(self) -> None:
        result = detect.classify_changed_files(["host_vars/box.yml"])
        assert result.machine_universe == {"box"}
        assert not result.full_universe_paths

    def test_machine_universe_minimal(self) -> None:
        result = detect.classify_changed_files(["host_vars/minimal.yml"])
        assert result.machine_universe == {"minimal"}
        assert not result.full_universe_paths

    def test_machine_universe_lab(self) -> None:
        result = detect.classify_changed_files(["host_vars/lab-qemu.yml"])
        assert result.machine_universe == {"lab"}
        assert not result.full_universe_paths

    def test_machine_universe_lab_prod(self) -> None:
        result = detect.classify_changed_files(["host_vars/lab.yml"])
        assert result.machine_universe == {"lab"}
        assert not result.full_universe_paths

    def test_machine_universe_pug(self) -> None:
        result = detect.classify_changed_files(["host_vars/pug-qemu.yml"])
        assert result.machine_universe == {"pug"}
        assert not result.full_universe_paths

    def test_machine_universe_pug_prod(self) -> None:
        result = detect.classify_changed_files(["host_vars/pug.yml"])
        assert result.machine_universe == {"pug"}
        assert not result.full_universe_paths

    def test_machine_universe_minimal_fixture(self) -> None:
        result = detect.classify_changed_files(["test/minimal/cloud-init.yml"])
        assert result.machine_universe == {"minimal"}
        assert not result.full_universe_paths

    def test_machine_universe_multiple(self) -> None:
        result = detect.classify_changed_files(["host_vars/lab-qemu.yml", "host_vars/pug-qemu.yml"])
        assert result.machine_universe == {"lab", "pug"}


# ---------------------------------------------------------------------------
# _packer_sources_for
# ---------------------------------------------------------------------------


class TestPackerSourcesFor:
    @pytest.mark.parametrize(
        "path, expected",
        [
            ("packer/ubuntu_images.json", {"qemu"}),
            ("mise-tasks/packer/hetzner.sh", {"hetzner_upload"}),
            ("mise-tasks/packer/hetzner-bake.sh", {"hetzner_upload"}),
            ("mise-tasks/packer/_hetzner_rescue.sh", {"hetzner_upload"}),
            ("mise-tasks/packer/_hcloud_token.sh", {"hetzner_upload"}),
            ("mise-tasks/packer/hcloud-prune-snapshots.sh", {"hetzner_upload"}),
            ("packer/qemu.pkr.hcl", {"qemu"}),
            ("packer/scripts/chroot.sh", {"qemu"}),
            ("mise-tasks/packer/build", {"qemu"}),
            ("roles/nginx/tasks/main.yml", set()),
            ("README.md", set()),
        ],
    )
    def test_mapping(self, path: str, expected: set[str]) -> None:
        assert detect._packer_sources_for(path) == expected


# ---------------------------------------------------------------------------
# propagate_release_cells
# ---------------------------------------------------------------------------


class TestPropagateReleaseCells:
    def test_basic_propagation(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["nginx", "podman"]},
            role_machines={"nginx": ["box"], "podman": ["box_deps"]},
            role_releases={"apt_source": ["noble", "resolute"]},
            universe={"nginx", "podman"},
        )
        assert result == [
            "nginx:box:noble",
            "nginx:box:resolute",
            "podman:box_deps:noble",
            "podman:box_deps:resolute",
        ]

    def test_no_releases_for_role(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["nginx"],
            consumers={"nginx": ["homepage"]},
            role_machines={"homepage": ["box"]},
            role_releases={},
            universe={"homepage"},
        )
        assert result == []

    def test_empty_releases_list(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["nginx"],
            consumers={"nginx": ["homepage"]},
            role_machines={"homepage": ["box"]},
            role_releases={"nginx": []},
            universe={"homepage"},
        )
        assert result == []

    def test_no_consumers(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={},
            role_machines={},
            role_releases={"apt_source": ["noble"]},
            universe=set(),
        )
        assert result == []

    def test_consumer_not_in_universe(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["helper_only"]},
            role_machines={"helper_only": ["box"]},
            role_releases={"apt_source": ["noble"]},
            universe=set(),
        )
        assert result == []

    def test_default_machine_fallback(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["newrole"]},
            role_machines={},
            role_releases={"apt_source": ["noble"]},
            universe={"newrole"},
        )
        assert result == ["newrole:box:noble"]

    def test_deduplicates_across_helpers(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["helper_a", "helper_b"],
            consumers={"helper_a": ["consumer"], "helper_b": ["consumer"]},
            role_machines={"consumer": ["box"]},
            role_releases={"helper_a": ["noble"], "helper_b": ["noble"]},
            universe={"consumer"},
        )
        assert result == ["consumer:box:noble"]

    def test_multiple_roles_multiple_releases(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source", "podman"],
            consumers={"apt_source": ["nginx", "redis"], "podman": ["redis"]},
            role_machines={"nginx": ["box"], "redis": ["box_deps"]},
            role_releases={"apt_source": ["noble", "resolute"], "podman": ["noble"]},
            universe={"nginx", "redis"},
        )
        assert result == [
            "nginx:box:noble",
            "nginx:box:resolute",
            "redis:box_deps:noble",
            "redis:box_deps:resolute",
        ]

    def test_multi_machine_propagation(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["cleanup"]},
            role_machines={"cleanup": ["box", "minimal"]},
            role_releases={"apt_source": ["noble"]},
            universe={"cleanup"},
        )
        assert result == [
            "cleanup:box:noble",
            "cleanup:minimal:noble",
        ]

    def test_empty_inputs(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=[],
            consumers={},
            role_machines={},
            role_releases={},
            universe=set(),
        )
        assert result == []


# ---------------------------------------------------------------------------
# Path classification regexes
# ---------------------------------------------------------------------------


class TestPathClassificationRegexes:
    """Parametrized match/reject checks for the path classification regexes."""

    @pytest.mark.parametrize(
        "path",
        [
            "group_vars/all/main.yml",
            "group_vars/all/service_ports.yaml",
            "group_vars/test.yml",
            "test/machine.py",
            "test/testall.py",
            "test/matrix.py",
            "test/inventory.ini",
            "test/playbooks/site.yml",
            "ansible.cfg",
            "vault-client.sh",
            "mise.toml",
            "pyproject.toml",
            "uv.lock",
            "data/network_topology.yml",
            "data/network_topology.schema.json",
            "mise-tasks/ci/detect.py",
        ],
    )
    def test_full_universe_match(self, path: str) -> None:
        assert detect.FULL_UNIVERSE_RE.match(path), f"should match: {path}"

    @pytest.mark.parametrize(
        "path",
        [
            "host_vars/lab.yml",
            "host_vars/pug.yml",
            "host_vars/box.yml",
            "host_vars/minimal.yml",
            "host_vars/lab-qemu.yml",
            "test/minimal/cloud-init.yml",
            "site.yml",
            "group_vars/all/sub/deep.yml",
            "unit_tests/test_matrix.py",
            "roles/nginx/tasks/main.yml",
            "roles/podman/templates/foo.j2",
            "README.md",
            "Dockerfile",
        ],
    )
    def test_full_universe_reject(self, path: str) -> None:
        assert not detect.FULL_UNIVERSE_RE.match(path), f"should not match: {path}"

    @pytest.mark.parametrize(
        "path",
        [
            "packer/qemu.pkr.hcl",
            "packer/scripts/chroot.sh",
            "mise-tasks/packer/build",
        ],
    )
    def test_packer_match(self, path: str) -> None:
        assert detect.PACKER_PATHS_RE.match(path), f"should match: {path}"

    @pytest.mark.parametrize(
        "path",
        [
            "roles/packer/tasks/main.yml",
            "test/machine.py",
        ],
    )
    def test_packer_reject(self, path: str) -> None:
        assert not detect.PACKER_PATHS_RE.match(path), f"should not match: {path}"

    @pytest.mark.parametrize(
        "path",
        [
            "Dockerfile",
            "mise.toml",
            "pyproject.toml",
            "uv.lock",
            "packer/qemu.pkr.hcl",
        ],
    )
    def test_ci_image_match(self, path: str) -> None:
        assert detect.CI_IMAGE_INPUTS_RE.match(path), f"should match: {path}"

    @pytest.mark.parametrize(
        "path",
        [
            "packer/scripts/chroot.sh",
            "ansible.cfg",
        ],
    )
    def test_ci_image_reject(self, path: str) -> None:
        assert not detect.CI_IMAGE_INPUTS_RE.match(path), f"should not match: {path}"


# ---------------------------------------------------------------------------
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


class TestGitFetchCommit:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result(""))
        assert detect.git_fetch_commit("abc") is True

    def test_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_git", lambda *a, **kw: _fake_git_result("", returncode=1))
        assert detect.git_fetch_commit("abc") is False


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
            raise urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)

        monkeypatch.setattr(detect.urllib.request, "urlopen", mock_urlopen)
        monkeypatch.setattr(detect.time, "sleep", lambda _: None)
        assert detect._gl_api_get("http://x", "tok", retries=4) is None
        assert attempts["n"] == 1

    def test_transient_http_error_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}

        def mock_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, None)
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
                {"sha": "newsha", "source": "push", "created_at": "2026-01-02"},
            ],
        )
        monkeypatch.setattr(detect, "is_local_ancestor", lambda sha, head: True)
        assert detect.newest_green_pipeline("master", **self._kw()) == "newsha"

    def test_skips_non_base_sources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A web (manual ROLES dispatch) and a parent_pipeline (cell child) are
        # skipped; the push behind them is the real base.
        monkeypatch.setattr(
            detect,
            "_gl_api_get",
            lambda url, token, **kw: (
                [
                    {"sha": "websha", "source": "web", "created_at": "2026-01-03"},
                    {"sha": "childsha", "source": "parent_pipeline", "created_at": "2026-01-03"},
                    {"sha": "pushsha", "source": "push", "created_at": "2026-01-01"},
                ]
                if "&page=1" in url
                else []
            ),
        )
        seen = []

        def fake_anc(sha, head):
            seen.append(sha)
            return True

        monkeypatch.setattr(detect, "is_local_ancestor", fake_anc)
        assert detect.newest_green_pipeline("master", **self._kw()) == "pushsha"
        # ancestry is only ever checked for the push pipeline
        assert seen == ["pushsha"]

    def test_skips_non_ancestor_then_paginates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "&page=1" in url:
                return [{"sha": "divsha", "source": "push", "created_at": "2026-01-05"}]
            if "&page=2" in url:
                return [{"sha": "oldgreen", "source": "schedule", "created_at": "2026-01-01"}]
            return []

        monkeypatch.setattr(detect, "_gl_api_get", mock_api)
        monkeypatch.setattr(detect, "is_local_ancestor", lambda sha, head: sha == "oldgreen")
        logs = []
        result = detect.newest_green_pipeline("master", **self._kw(log_fn=logs.append))
        assert result == "oldgreen"
        assert any("not an ancestor" in m for m in logs)

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
            return None

        monkeypatch.setattr(detect, "newest_green_pipeline", mock)
        assert detect.resolve_green_base_gitlab(branch="master", default_branch="master", **self._kw()) is None
        assert calls["n"] == 1

    def test_missing_inputs_return_none(self) -> None:
        assert detect.resolve_green_base_gitlab(branch="", **self._kw()) is None
        assert detect.resolve_green_base_gitlab(branch="m", **self._kw(token="")) is None
        assert detect.resolve_green_base_gitlab(branch="m", **self._kw(project_api="")) is None
        assert detect.resolve_green_base_gitlab(branch="m", **self._kw(head_sha="")) is None


class TestGitlabGreenBaseEnv:
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
        seen = {}

        def mock_resolve(**kw):
            seen.update(kw)
            return "green"

        monkeypatch.setattr(detect, "resolve_green_base_gitlab", mock_resolve)
        assert detect._gitlab_green_base("master", "head", "master", lambda m: None) == "green"
        assert seen["token"] == "pat"
        assert seen["token_kind"] == "private"
        assert seen["project_api"] == "http://api/projects/1"

    def test_falls_back_to_job_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch, CI_API_V4_URL="http://api", CI_PROJECT_ID="1", CI_JOB_TOKEN="jobtok")
        seen = {}

        def mock_resolve(**kw):
            seen.update(kw)
            return "green"

        monkeypatch.setattr(detect, "resolve_green_base_gitlab", mock_resolve)
        assert detect._gitlab_green_base("master", "head", "master", lambda m: None) == "green"
        assert seen["token"] == "jobtok"
        assert seen["token_kind"] == "job"

    def test_none_when_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch, CI_API_V4_URL="http://api", CI_PROJECT_ID="1")
        logs = []
        assert detect._gitlab_green_base("master", "head", "master", logs.append) is None
        assert any("green base unavailable" in m for m in logs)

    def test_none_when_no_api_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch, CI_PROJECT_ID="1", CI_JOB_TOKEN="jobtok")
        assert detect._gitlab_green_base("master", "head", "master", lambda m: None) is None


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

    def test_handles_parse_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        bad_dir = tmp_path / "roles" / "broken" / "tasks"
        bad_dir.mkdir(parents=True)
        (bad_dir / "main.yml").write_text(": : :\n  - [\n")
        result = detect.build_role_deps_map()
        assert result == {}


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


def _render_child_doc(specs: list[str], site_test: bool) -> dict:
    """Render test_child.yml.j2 and parse it back to a dict for assertions."""
    return detect.yaml.safe_load(detect.render_child_pipeline(specs, site_test))


class TestRenderChildPipeline:
    def test_one_job_per_spec(self) -> None:
        doc = _render_child_doc(["nginx:box", "podman:box:noble"], site_test=False)
        assert doc["default"]["tags"] == ["fox-docker-aws"]
        assert doc["stages"] == ["test"]
        assert doc[".cell"]["variables"]["HOMELAB_TEST_BACKEND"] == "aws"
        assert doc[".cell"]["retry"]["exit_codes"] == [86]
        # nginx:box → defaults ubuntu jammy; podman:box:noble → explicit noble.
        assert doc["nginx:box"]["variables"] == {"ROLE": "nginx", "VARIANT": "box", "UBUNTU": "jammy"}
        assert doc["podman:box:noble"]["variables"] == {"ROLE": "podman", "VARIANT": "box", "UBUNTU": "noble"}
        assert doc["nginx:box"]["extends"] == ".cell"
        assert "_site_test:box" not in doc
        assert "no_cells" not in doc

    def test_site_test_job_added(self) -> None:
        doc = _render_child_doc(["nginx:box"], site_test=True)
        assert "_site_test:box" in doc
        assert doc["_site_test:box"]["timeout"] == "60m"
        assert "no_cells" not in doc

    def test_empty_gets_noop_placeholder(self) -> None:
        doc = _render_child_doc([], site_test=False)
        assert "no_cells" in doc
        assert "_site_test:box" not in doc
        # No cell jobs beyond the scaffolding + placeholder.
        jobs = [k for k in doc if k not in ("default", "stages", ".cell")]
        assert jobs == ["no_cells"]

    def test_cell_role_arn_in_before_script(self) -> None:
        doc = _render_child_doc(["nginx:box"], site_test=False)
        joined = "\n".join(doc[".cell"]["before_script"])
        assert detect.CELL_ROLE_ARN in joined
        assert "ssh-add" in joined

    def test_before_script_printf_normalises_key(self) -> None:
        # The CR-strip + trailing-newline normalisation must survive the YAML
        # round-trip exactly (the literal \n is for printf, not a YAML newline).
        doc = _render_child_doc(["nginx:box"], site_test=False)
        assert 'printf \'%s\\n\' "$(tr -d \'\\r\' < "$CI_CELL_SSH_KEY")" > "$cell_key"' in doc[".cell"]["before_script"]


class TestEmitGitlab:
    def test_writes_child_with_cells(self, tmp_path: Path) -> None:
        child = tmp_path / "child.yml"
        rc = detect._emit_gitlab(json.dumps(["nginx:box"]), False, str(child), lambda *_: None)
        assert rc == 0
        loaded = detect.yaml.safe_load(child.read_text())
        assert "nginx:box" in loaded
        assert "no_cells" not in loaded

    def test_site_test_only(self, tmp_path: Path) -> None:
        child = tmp_path / "child.yml"
        detect._emit_gitlab(json.dumps([]), True, str(child), lambda *_: None)
        assert "_site_test:box" in detect.yaml.safe_load(child.read_text())

    def test_empty_writes_valid_noop_pipeline(self, tmp_path: Path) -> None:
        child = tmp_path / "child.yml"
        detect._emit_gitlab(json.dumps([]), False, str(child), lambda *_: None)
        # Always a valid pipeline with at least one job, so the trigger never
        # fails on an empty child.
        loaded = detect.yaml.safe_load(child.read_text())
        assert "no_cells" in loaded


class TestCmdGitlab:
    def test_all_flag_full_universe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_full_universe_matrix", lambda: json.dumps(["nginx:box"]))
        child = tmp_path / "child.yml"
        rc = detect._cmd_gitlab(["--all", "--child-path", str(child)])
        assert rc == 0
        assert "_site_test:box" in detect.yaml.safe_load(child.read_text())

    def test_schedule_full_universe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_PIPELINE_SOURCE", "schedule")
        monkeypatch.delenv("ROLES", raising=False)
        monkeypatch.setattr(detect, "_full_universe_matrix", lambda: json.dumps(["nginx:box"]))
        child = tmp_path / "child.yml"
        rc = detect._cmd_gitlab(["--child-path", str(child)])
        assert rc == 0
        loaded = detect.yaml.safe_load(child.read_text())
        assert "nginx:box" in loaded
        assert "_site_test:box" in loaded

    def test_dispatch_roles_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_PIPELINE_SOURCE", "web")
        monkeypatch.setenv("ROLES", "ALL")
        monkeypatch.setattr(detect, "_full_universe_matrix", lambda: json.dumps(["nginx:box"]))
        child = tmp_path / "child.yml"
        assert detect._cmd_gitlab(["--child-path", str(child)]) == 0
        assert "nginx:box" in detect.yaml.safe_load(child.read_text())

    def test_gitlab_in_commands(self) -> None:
        assert "gitlab" in detect._COMMANDS


class TestMainEntrypoint:
    def test_no_args_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["detect.py"])
        assert detect.main() == 2

    def test_unknown_command_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["detect.py", "bogus"])
        assert detect.main() == 2

    def test_gitlab_is_only_command(self) -> None:
        assert set(detect._COMMANDS) == {"gitlab"}
