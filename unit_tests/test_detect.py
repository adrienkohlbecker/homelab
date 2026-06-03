"""Unit tests for mise-tasks/ci/detect.py — CI change-detection pipeline.

Tests path classification regexes, file classification, matrix bucket
splitting, packer source/ubuntu matrices, release-cell propagation,
git helpers, GitHub API wrappers, green-base resolution, role dependency
map, and the full ``run`` orchestration command.
"""

import importlib
import io
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
        # mise.toml carries ZBM_VERSION but isn't a zbm path -> full-universe, not zbm.
        assert detect.classify_changed_files(["mise.toml"]).zbm_changed is False

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
            ("packer/hcloud_worker.pkr.hcl", {"worker"}),
            ("packer/scripts/provision_worker.sh", {"worker"}),
            ("mise-tasks/packer/worker.sh", {"worker"}),
            ("roles/github_runner/vars/main.yml", {"worker"}),
            ("packer/ubuntu_images.json", {"qemu", "worker"}),
            ("mise-tasks/packer/hetzner.sh", {"hetzner_upload"}),
            ("mise-tasks/packer/_hcloud_token.sh", {"hetzner_upload", "worker"}),
            ("mise-tasks/packer/hcloud-prune-snapshots.sh", {"hetzner_upload", "worker"}),
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
# split_matrix_buckets
# ---------------------------------------------------------------------------


class TestSplitMatrixBuckets:
    def test_jammy_box_cells(self) -> None:
        result = detect.split_matrix_buckets(["alpha:box", "beta:box_deps"])
        assert result.jammy == ["alpha:box", "beta:box_deps"]
        assert result.noble == []
        assert result.resolute == []
        assert result.minimal == []

    def test_noble_cells(self) -> None:
        result = detect.split_matrix_buckets(["netdata:box_deps:noble", "zfs:box:noble"])
        assert result.noble == ["netdata:box_deps:noble", "zfs:box:noble"]
        assert result.jammy == []

    def test_resolute_cells(self) -> None:
        result = detect.split_matrix_buckets(["podman:box:resolute", "netdata:box_deps:resolute"])
        assert result.resolute == ["netdata:box_deps:resolute", "podman:box:resolute"]

    def test_minimal_cells(self) -> None:
        result = detect.split_matrix_buckets(["cleanup:minimal"])
        assert result.minimal == ["cleanup:minimal"]
        assert result.jammy == []
        assert result.lab == []
        assert result.pug == []

    def test_lab_cells(self) -> None:
        result = detect.split_matrix_buckets(["zfs:lab", "zfs:lab:noble"])
        assert result.lab == ["zfs:lab", "zfs:lab:noble"]
        assert result.minimal == []

    def test_pug_cells(self) -> None:
        result = detect.split_matrix_buckets(["swap:pug"])
        assert result.pug == ["swap:pug"]
        assert result.minimal == []

    def test_mixed_matrix(self) -> None:
        specs = [
            "alpha:box",
            "beta:box_deps",
            "cleanup:minimal",
            "netdata:box_deps:noble",
            "podman:box:resolute",
            "zfs:lab",
            "swap:pug",
        ]
        result = detect.split_matrix_buckets(specs)
        assert result.jammy == ["alpha:box", "beta:box_deps"]
        assert result.noble == ["netdata:box_deps:noble"]
        assert result.resolute == ["podman:box:resolute"]
        assert result.minimal == ["cleanup:minimal"]
        assert result.lab == ["zfs:lab"]
        assert result.pug == ["swap:pug"]

    def test_empty_matrix(self) -> None:
        result = detect.split_matrix_buckets([])
        assert result == detect.MatrixBuckets([], [], [], [], [], [])

    def test_box_deps_jammy_no_release(self) -> None:
        result = detect.split_matrix_buckets(["redis:box_deps"])
        assert result.jammy == ["redis:box_deps"]

    def test_box_deps_noble(self) -> None:
        result = detect.split_matrix_buckets(["redis:box_deps:noble"])
        assert result.noble == ["redis:box_deps:noble"]

    def test_lab_pug_separate_buckets(self) -> None:
        result = detect.split_matrix_buckets(["somerole:lab", "anotherrole:pug"])
        assert result.lab == ["somerole:lab"]
        assert result.pug == ["anotherrole:pug"]
        assert result.minimal == []


# ---------------------------------------------------------------------------
# compute_packer_sources
# ---------------------------------------------------------------------------


class TestComputePackerSources:
    def test_default_full_set(self) -> None:
        result = detect.compute_packer_sources()
        assert result.all == ["box", "pug", "lab", "hetzner"]
        assert result.box == ["box"]
        assert result.lab == ["lab"]
        assert result.pug == ["pug"]
        assert result.hetzner == ["hetzner"]

    def test_empty_string(self) -> None:
        result = detect.compute_packer_sources("")
        assert result.all == ["box", "pug", "lab", "hetzner"]

    def test_explicit_sources(self) -> None:
        result = detect.compute_packer_sources("lab pug")
        assert result.all == ["lab", "pug"]
        assert result.box == []
        assert result.lab == ["lab"]
        assert result.pug == ["pug"]
        assert result.hetzner == []

    def test_box_only(self) -> None:
        result = detect.compute_packer_sources("box")
        assert result.all == ["box"]
        assert result.box == ["box"]
        assert result.lab == []
        assert result.pug == []
        assert result.hetzner == []

    def test_preserves_order(self) -> None:
        result = detect.compute_packer_sources("hetzner box lab")
        assert result.all == ["hetzner", "box", "lab"]
        assert result.box == ["box"]
        assert result.lab == ["lab"]
        assert result.hetzner == ["hetzner"]


# ---------------------------------------------------------------------------
# compute_packer_ubuntu
# ---------------------------------------------------------------------------


class TestComputePackerUbuntu:
    def test_default_releases(self) -> None:
        result = detect.compute_packer_ubuntu()
        assert result.box == ["jammy", "noble", "resolute"]
        assert result.lab == ["jammy", "noble", "resolute"]
        assert result.pug == ["jammy", "noble", "resolute"]
        assert result.hetzner == ["jammy"]

    def test_empty_string(self) -> None:
        result = detect.compute_packer_ubuntu("")
        assert result.box == ["jammy", "noble", "resolute"]
        assert result.hetzner == ["jammy"]

    def test_pinned_release(self) -> None:
        result = detect.compute_packer_ubuntu("noble")
        assert result.box == ["noble"]
        assert result.lab == ["noble"]
        assert result.pug == ["noble"]
        assert result.hetzner == ["noble"]

    def test_pinned_resolute(self) -> None:
        result = detect.compute_packer_ubuntu("resolute")
        assert result.box == ["resolute"]
        assert result.lab == ["resolute"]


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
# Regex parity with detect-roles.sh EREs
# ---------------------------------------------------------------------------


class TestRegexParityWithBash:
    """Parametrized parity checks for path classification regexes."""

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
            ".github/workflows/ci.yml",
            ".github/workflows/detect.yml",
            "mise-tasks/ci/detect.py",
            "mise-tasks/ci/detect-roles.sh",
            ".github/workflows/packer-build.yml",
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
# GitHub API
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal context-manager response for mocking urlopen."""

    def __init__(self, data: dict):
        self._data = json.dumps(data).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestGhApiGet:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect.urllib.request,
            "urlopen",
            lambda req, timeout=None: _FakeResponse({"ok": True}),
        )
        assert detect._gh_api_get("http://example.com", "tok") == {"ok": True}

    def test_retry_then_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}

        def mock_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise urllib.error.URLError("transient")
            return _FakeResponse({"recovered": True})

        monkeypatch.setattr(detect.urllib.request, "urlopen", mock_urlopen)
        monkeypatch.setattr(detect.time, "sleep", lambda _: None)
        result = detect._gh_api_get("http://example.com", "tok", retries=4)
        assert result == {"recovered": True}
        assert attempts["n"] == 3

    def test_all_retries_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect.urllib.request,
            "urlopen",
            lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("down")),
        )
        monkeypatch.setattr(detect.time, "sleep", lambda _: None)
        assert detect._gh_api_get("http://x", "tok", retries=2) is None

    def test_http_error_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}

        def mock_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.HTTPError("http://x", 500, "ISE", {}, None)
            return _FakeResponse({"ok": True})

        monkeypatch.setattr(detect.urllib.request, "urlopen", mock_urlopen)
        monkeypatch.setattr(detect.time, "sleep", lambda _: None)
        assert detect._gh_api_get("http://x", "tok", retries=2) == {"ok": True}


class TestIsAncestorOfHead:
    def test_ahead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gh_api_get", lambda url, token, **kw: {"status": "ahead"})
        assert detect.is_ancestor_of_head("abc", "def", repo="r", api_url="http://api", token="t")

    def test_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gh_api_get", lambda url, token, **kw: {"status": "identical"})
        assert detect.is_ancestor_of_head("abc", "abc", repo="r", api_url="http://api", token="t")

    def test_diverged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gh_api_get", lambda url, token, **kw: {"status": "diverged"})
        assert not detect.is_ancestor_of_head("abc", "def", repo="r", api_url="http://api", token="t")

    def test_behind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gh_api_get", lambda url, token, **kw: {"status": "behind"})
        assert not detect.is_ancestor_of_head("abc", "def", repo="r", api_url="http://api", token="t")

    def test_api_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gh_api_get", lambda url, token, **kw: None)
        assert not detect.is_ancestor_of_head("abc", "def", repo="r", api_url="http://api", token="t")

    def test_constructs_correct_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def mock_api(url, token, **kw):
            captured["url"] = url
            return {"status": "ahead"}

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        detect.is_ancestor_of_head(
            "base_sha",
            "head_sha",
            repo="owner/repo",
            api_url="https://api.github.com",
            token="t",
        )
        assert captured["url"] == "https://api.github.com/repos/owner/repo/compare/base_sha...head_sha"


class TestNightlyActuallyTested:
    def test_real_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "_gh_api_get",
            lambda url, token, **kw: {
                "jobs": [
                    {"name": "gate", "conclusion": "success"},
                    {"name": "test (foo:box)", "conclusion": "success"},
                ]
            },
        )
        assert detect.nightly_actually_tested(42, repo="r", api_url="http://api", token="t")

    def test_gate_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "_gh_api_get",
            lambda url, token, **kw: {"jobs": [{"name": "gate", "conclusion": "success"}]},
        )
        assert not detect.nightly_actually_tested(42, repo="r", api_url="http://api", token="t")

    def test_api_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gh_api_get", lambda url, token, **kw: None)
        assert not detect.nightly_actually_tested(42, repo="r", api_url="http://api", token="t")

    def test_empty_jobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gh_api_get", lambda url, token, **kw: {"jobs": []})
        assert not detect.nightly_actually_tested(42, repo="r", api_url="http://api", token="t")

    def test_failed_non_gate_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "_gh_api_get",
            lambda url, token, **kw: {
                "jobs": [
                    {"name": "gate", "conclusion": "success"},
                    {"name": "test (foo:box)", "conclusion": "failure"},
                ]
            },
        )
        assert not detect.nightly_actually_tested(42, repo="r", api_url="http://api", token="t")


class TestNewestGreenAncestor:
    @staticmethod
    def _kw(**overrides):
        defaults = dict(
            head_sha="HEAD",
            repo="r",
            api_url="http://api",
            token="t",
            log_fn=lambda m: None,
        )
        defaults.update(overrides)
        return defaults

    def test_finds_on_first_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "actions/runs?" in url:
                return {
                    "workflow_runs": [
                        {
                            "path": ".github/workflows/ci.yml",
                            "event": "push",
                            "head_sha": "abc123456789",
                            "created_at": "2026-01-01",
                            "id": 1,
                        }
                    ]
                }
            if "compare/" in url:
                return {"status": "ahead"}
            return None

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        assert detect.newest_green_ancestor("master", **self._kw()) == "abc123456789"

    def test_skips_non_ancestor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "actions/runs?" in url:
                if "&page=1" in url:
                    return {
                        "workflow_runs": [
                            {
                                "path": ".github/workflows/ci.yml",
                                "event": "push",
                                "head_sha": "old123",
                                "created_at": "2026-01-01",
                                "id": 1,
                            }
                        ]
                    }
                return {"workflow_runs": []}
            if "compare/" in url:
                return {"status": "diverged"}
            return None

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        logs = []
        assert detect.newest_green_ancestor("master", **self._kw(log_fn=logs.append)) is None
        assert any("not an ancestor" in msg for msg in logs)

    def test_skips_gate_only_schedule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "actions/runs?" in url:
                if "&page=1" in url:
                    return {
                        "workflow_runs": [
                            {
                                "path": ".github/workflows/ci.yml",
                                "event": "schedule",
                                "head_sha": "sched123",
                                "created_at": "2026-01-01",
                                "id": 42,
                            }
                        ]
                    }
                return {"workflow_runs": []}
            if "/jobs?" in url:
                return {"jobs": [{"name": "gate", "conclusion": "success"}]}
            return None

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        logs = []
        assert detect.newest_green_ancestor("master", **self._kw(log_fn=logs.append)) is None
        assert any("skipped its test matrix" in msg for msg in logs)

    def test_skips_gate_only_old_nightly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "actions/runs?" in url:
                if "&page=1" in url:
                    return {
                        "workflow_runs": [
                            {
                                "path": ".github/workflows/test-nightly.yml",
                                "event": "schedule",
                                "head_sha": "nightly123",
                                "created_at": "2026-01-01",
                                "id": 42,
                            }
                        ]
                    }
                return {"workflow_runs": []}
            if "/jobs?" in url:
                return {"jobs": [{"name": "gate", "conclusion": "success"}]}
            return None

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        logs = []
        assert detect.newest_green_ancestor("master", **self._kw(log_fn=logs.append)) is None
        assert any("skipped its test matrix" in msg for msg in logs)

    def test_accepts_schedule_that_tested(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "actions/runs?" in url:
                return {
                    "workflow_runs": [
                        {
                            "path": ".github/workflows/ci.yml",
                            "event": "schedule",
                            "head_sha": "sched_good",
                            "created_at": "2026-01-01",
                            "id": 42,
                        }
                    ]
                }
            if "/jobs?" in url:
                return {
                    "jobs": [
                        {"name": "gate", "conclusion": "success"},
                        {"name": "detect", "conclusion": "success"},
                        {"name": "test_jammy / test-role (foo:box)", "conclusion": "success"},
                    ]
                }
            if "compare/" in url:
                return {"status": "ahead"}
            return None

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        assert detect.newest_green_ancestor("master", **self._kw()) == "sched_good"

    def test_accepts_old_nightly_that_tested(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "actions/runs?" in url:
                return {
                    "workflow_runs": [
                        {
                            "path": ".github/workflows/test-nightly.yml",
                            "event": "schedule",
                            "head_sha": "nightly_good",
                            "created_at": "2026-01-01",
                            "id": 42,
                        }
                    ]
                }
            if "/jobs?" in url:
                return {
                    "jobs": [
                        {"name": "gate", "conclusion": "success"},
                        {"name": "test (foo:box)", "conclusion": "success"},
                    ]
                }
            if "compare/" in url:
                return {"status": "ahead"}
            return None

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        assert detect.newest_green_ancestor("master", **self._kw()) == "nightly_good"

    def test_pages_through_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = {"n": 0}

        def mock_api(url, token, **kw):
            if "actions/runs?" in url:
                page["n"] += 1
                if page["n"] == 1:
                    return {
                        "workflow_runs": [
                            {
                                "path": ".github/workflows/ci.yml",
                                "event": "push",
                                "head_sha": "diverged1",
                                "created_at": "2026-01-02",
                                "id": 1,
                            }
                        ]
                    }
                if page["n"] == 2:
                    return {
                        "workflow_runs": [
                            {
                                "path": ".github/workflows/ci.yml",
                                "event": "push",
                                "head_sha": "found_it",
                                "created_at": "2026-01-01",
                                "id": 2,
                            }
                        ]
                    }
                return {"workflow_runs": []}
            if "compare/" in url:
                return {"status": "ahead" if "found_it" in url else "diverged"}
            return None

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        assert detect.newest_green_ancestor("master", **self._kw()) == "found_it"

    def test_api_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gh_api_get", lambda url, token, **kw: None)
        logs = []
        assert detect.newest_green_ancestor("master", **self._kw(log_fn=logs.append)) is None
        assert any("failed" in msg for msg in logs)

    def test_empty_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detect, "_gh_api_get", lambda url, token, **kw: {"workflow_runs": []})
        assert detect.newest_green_ancestor("master", **self._kw()) is None

    def test_skips_dispatch_ci_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "actions/runs?" in url:
                if "&page=1" in url:
                    return {
                        "workflow_runs": [
                            {
                                "path": ".github/workflows/ci.yml",
                                "event": "workflow_dispatch",
                                "head_sha": "dispatch123",
                                "created_at": "2026-01-01",
                                "id": 1,
                            }
                        ]
                    }
                return {"workflow_runs": []}
            return None

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        assert detect.newest_green_ancestor("master", **self._kw()) is None

    def test_skips_runs_with_empty_sha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def mock_api(url, token, **kw):
            if "actions/runs?" in url:
                if "&page=1" in url:
                    return {
                        "workflow_runs": [
                            {
                                "path": ".github/workflows/ci.yml",
                                "event": "push",
                                "head_sha": "",
                                "created_at": "2026-01-01",
                                "id": 1,
                            }
                        ]
                    }
                return {"workflow_runs": []}
            return None

        monkeypatch.setattr(detect, "_gh_api_get", mock_api)
        assert detect.newest_green_ancestor("master", **self._kw()) is None


class TestResolveGreenBase:
    def test_no_token(self) -> None:
        logs = []
        result = detect.resolve_green_base(
            token="",
            repo="r",
            ref_name="master",
            head_sha="abc",
            log_fn=logs.append,
        )
        assert result is None
        assert any("no GITHUB_TOKEN" in msg for msg in logs)

    def test_found_on_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "newest_green_ancestor",
            lambda branch, **kw: "abc123" if branch == "feat" else None,
        )
        result = detect.resolve_green_base(
            token="t",
            repo="r",
            ref_name="feat",
            head_sha="head",
            log_fn=lambda m: None,
        )
        assert result == "abc123"

    def test_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            detect,
            "newest_green_ancestor",
            lambda branch, **kw: None if branch == "feat" else "default_green",
        )
        logs = []
        result = detect.resolve_green_base(
            token="t",
            repo="r",
            ref_name="feat",
            head_sha="head",
            default_branch="master",
            log_fn=logs.append,
        )
        assert result == "default_green"
        assert any("falling back" in msg for msg in logs)

    def test_no_fallback_when_on_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def mock_ancestor(branch, **kw):
            calls["n"] += 1
            return None

        monkeypatch.setattr(detect, "newest_green_ancestor", mock_ancestor)
        result = detect.resolve_green_base(
            token="t",
            repo="r",
            ref_name="master",
            head_sha="head",
            default_branch="master",
            log_fn=lambda m: None,
        )
        assert result is None
        assert calls["n"] == 1

    def test_missing_repo_returns_none(self) -> None:
        assert (
            detect.resolve_green_base(
                token="t",
                repo="",
                ref_name="master",
                head_sha="abc",
                log_fn=lambda m: None,
            )
            is None
        )

    def test_missing_ref_returns_none(self) -> None:
        assert (
            detect.resolve_green_base(
                token="t",
                repo="r",
                ref_name="",
                head_sha="abc",
                log_fn=lambda m: None,
            )
            is None
        )

    def test_missing_sha_returns_none(self) -> None:
        assert (
            detect.resolve_green_base(
                token="t",
                repo="r",
                ref_name="master",
                head_sha="",
                log_fn=lambda m: None,
            )
            is None
        )


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


# ---------------------------------------------------------------------------
# CLI subcommands (existing)
# ---------------------------------------------------------------------------


class TestCmdClassify:
    def test_role_detection(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO("roles/nginx/tasks/main.yml\nroles/podman/templates/foo.j2\n"),
        )
        rc = detect._cmd_classify([])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["direct_roles"] == ["nginx", "podman"]
        assert result["ci_image_changed"] is False

    def test_master_push_flag(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("Dockerfile\n"))
        rc = detect._cmd_classify(["--master-push"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["ci_image_changed"] is True


class TestCmdSplitBuckets:
    def test_basic_split(self, capsys: pytest.CaptureFixture) -> None:
        rc = detect._cmd_split_buckets([json.dumps(["alpha:box", "beta:minimal", "zfs:lab"])])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["jammy"] == ["alpha:box"]
        assert result["minimal"] == ["beta:minimal"]
        assert result["lab"] == ["zfs:lab"]

    def test_no_args_returns_error(self) -> None:
        rc = detect._cmd_split_buckets([])
        assert rc == 2


class TestCmdPackerSources:
    def test_default(self, capsys: pytest.CaptureFixture) -> None:
        rc = detect._cmd_packer_sources([])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["all"] == ["box", "pug", "lab", "hetzner"]
        assert result["lab"] == ["lab"]
        assert result["pug"] == ["pug"]

    def test_explicit(self, capsys: pytest.CaptureFixture) -> None:
        rc = detect._cmd_packer_sources(["lab pug"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["all"] == ["lab", "pug"]
        assert result["box"] == []
        assert result["lab"] == ["lab"]
        assert result["pug"] == ["pug"]


class TestCmdPackerUbuntu:
    def test_default(self, capsys: pytest.CaptureFixture) -> None:
        rc = detect._cmd_packer_ubuntu([])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["box"] == ["jammy", "noble", "resolute"]
        assert result["lab"] == ["jammy", "noble", "resolute"]
        assert result["pug"] == ["jammy", "noble", "resolute"]
        assert result["hetzner"] == ["jammy"]

    def test_pinned(self, capsys: pytest.CaptureFixture) -> None:
        rc = detect._cmd_packer_ubuntu(["noble"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["box"] == ["noble"]
        assert result["lab"] == ["noble"]


class TestCmdEmit:
    def test_basic_emit(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", '["alpha:box","beta:minimal"]'])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert json.loads(out["matrix_jammy"]) == ["alpha:box"]
        assert json.loads(out["matrix_minimal"]) == ["beta:minimal"]
        assert json.loads(out["matrix_noble"]) == []
        assert json.loads(out["matrix_resolute"]) == []
        assert json.loads(out["matrix_lab"]) == []
        assert json.loads(out["matrix_pug"]) == []
        assert out["packer_changed"] == "false"
        assert out["packer_worker_changed"] == "false"
        assert out["ci_image_changed"] == "false"
        assert out["site_test"] == "false"
        assert out["zbm_changed"] == "false"

    def test_packer_changed_flag(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--packer-changed", "true"])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert out["packer_changed"] == "true"
        assert out["packer_worker_changed"] == "false"
        assert out["ci_image_changed"] == "false"

    def test_packer_worker_changed_flag(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--packer-worker-changed", "true"])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert out["packer_worker_changed"] == "true"
        assert out["packer_changed"] == "false"

    def test_ci_image_changed_flag(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--ci-image-changed", "true"])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert out["ci_image_changed"] == "true"

    def test_site_test_flag(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--site-test", "true"])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert out["site_test"] == "true"

    def test_zbm_changed_flag(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--zbm-changed", "true"])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert out["zbm_changed"] == "true"

    def test_packer_sources(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--inputs-sources", "lab pug"])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert json.loads(out["packer_sources"]) == ["lab", "pug"]
        assert json.loads(out["packer_sources_box"]) == []
        assert json.loads(out["packer_sources_lab"]) == ["lab"]
        assert json.loads(out["packer_sources_pug"]) == ["pug"]

    def test_packer_ubuntu_default(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]"])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert json.loads(out["packer_ubuntu_box"]) == ["jammy", "noble", "resolute"]
        assert json.loads(out["packer_ubuntu_lab"]) == ["jammy", "noble", "resolute"]
        assert json.loads(out["packer_ubuntu_pug"]) == ["jammy", "noble", "resolute"]
        assert json.loads(out["packer_ubuntu_hetzner"]) == ["jammy"]

    def test_packer_ubuntu_pinned(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--inputs-ubuntu", "noble"])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert json.loads(out["packer_ubuntu_box"]) == ["noble"]
        assert json.loads(out["packer_ubuntu_lab"]) == ["noble"]

    def test_github_output_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        gh_out = tmp_path / "output"
        gh_out.touch()
        monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
        rc = detect._cmd_emit(["--matrix", '["alpha:box"]'])
        assert rc == 0
        assert capsys.readouterr().out == ""
        content = gh_out.read_text()
        kv = _parse_kv(content)
        assert json.loads(kv["matrix_jammy"]) == ["alpha:box"]
        assert json.loads(kv["packer_sources"]) == ["box", "pug", "lab", "hetzner"]
        assert json.loads(kv["packer_sources_lab"]) == ["lab"]

    def test_log_to_stderr(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        detect._cmd_emit(["--matrix", '["alpha:box"]'])
        err = capsys.readouterr().err
        assert "[detect-roles] result:" in err

    def test_mixed_buckets(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        matrix = json.dumps(
            [
                "alpha:box",
                "cleanup:minimal",
                "net:box_deps:noble",
                "apt:box:resolute",
                "zfs:lab",
                "swap:pug",
            ]
        )
        rc = detect._cmd_emit(["--matrix", matrix])
        assert rc == 0
        out = _parse_kv(capsys.readouterr().out)
        assert json.loads(out["matrix_jammy"]) == ["alpha:box"]
        assert json.loads(out["matrix_noble"]) == ["net:box_deps:noble"]
        assert json.loads(out["matrix_resolute"]) == ["apt:box:resolute"]
        assert json.loads(out["matrix_minimal"]) == ["cleanup:minimal"]
        assert json.loads(out["matrix_lab"]) == ["zfs:lab"]
        assert json.loads(out["matrix_pug"]) == ["swap:pug"]


# ---------------------------------------------------------------------------
# _cmd_run (full orchestration)
# ---------------------------------------------------------------------------


def _clean_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all CI-related env vars so tests start clean."""
    for var in [
        "GITHUB_OUTPUT",
        "GITHUB_EVENT_NAME",
        "GITHUB_SHA",
        "GITHUB_REF_NAME",
        "GITHUB_REF",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_API_URL",
        "CI_BASE_REF",
        "CI_DEFAULT_BRANCH",
        "INPUTS_ROLES",
        "INPUTS_SOURCES",
        "INPUTS_UBUNTU",
    ]:
        monkeypatch.delenv(var, raising=False)


def _parse_kv(text: str) -> dict[str, str]:
    result = {}
    for line in text.strip().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            result[k] = v
    return result


class TestCmdRun:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_ci_env(monkeypatch)
        monkeypatch.setenv("INPUTS_SOURCES", "")
        monkeypatch.setenv("INPUTS_UBUNTU", "")

    def test_all_mode(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr(detect, "_full_universe_matrix", lambda: '["alpha:box","beta:minimal"]')
        rc = detect._cmd_run(["--all"])
        assert rc == 0
        out = capsys.readouterr()
        assert "alpha:box" in out.out
        assert "mode: --all" in out.err

    def test_dispatch_roles(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
        monkeypatch.setenv("INPUTS_ROLES", "cleanup")
        monkeypatch.setattr(detect, "_build_dispatch_matrix", lambda inp: [])
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: ["cleanup:box", "cleanup:minimal"])
        rc = detect._cmd_run([])
        assert rc == 0
        assert "cleanup:box" in capsys.readouterr().out

    def test_dispatch_all_keyword(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
        monkeypatch.setenv("INPUTS_ROLES", "ALL")
        monkeypatch.setattr(detect, "_full_universe_matrix", lambda: '["big:box"]')
        rc = detect._cmd_run([])
        assert rc == 0
        out = capsys.readouterr()
        assert "big:box" in out.out
        assert "roles=ALL" in out.err

    def test_dispatch_passes_args(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
        monkeypatch.setenv("INPUTS_ROLES", "foo,bar:minimal")
        captured = {}
        monkeypatch.setattr(
            detect,
            "_build_dispatch_matrix",
            lambda inp: (captured.update(input=inp), [])[1],
        )
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: ["foo:box"])
        detect._cmd_run([])
        assert captured["input"] == "foo,bar:minimal"

    def test_packer_only_dispatch(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
        monkeypatch.setenv("INPUTS_ROLES", "")
        monkeypatch.setenv("INPUTS_SOURCES", "lab pug")
        rc = detect._cmd_run([])
        assert rc == 0
        kv = _parse_kv(capsys.readouterr().out)
        assert kv["packer_changed"] == "true"
        assert json.loads(kv["matrix"]) == []
        assert sorted(json.loads(kv["packer_sources"])) == ["lab", "pug"]

    def test_schedule_full_build(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
        monkeypatch.setattr(detect, "_full_universe_matrix", lambda: '["alpha:box","beta:minimal"]')
        rc = detect._cmd_run([])
        assert rc == 0
        out = capsys.readouterr()
        kv = _parse_kv(out.out)
        assert "alpha:box" in kv["matrix"]
        assert kv["packer_changed"] == "true"
        assert kv["packer_worker_changed"] == "true"
        assert kv["ci_image_changed"] == "true"
        assert kv["site_test"] == "true"
        assert "schedule" in out.err

    def test_local_preview(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc123")
        monkeypatch.setattr(
            detect,
            "git_diff_files",
            lambda base, head="HEAD": ["roles/zfs/tasks/main.yml"],
        )
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "def456")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: ["zfs"])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {})
        monkeypatch.setattr(detect, "release_ubuntu_for", lambda role: [])
        monkeypatch.setattr(detect, "build_test_matrix", lambda roles, extra=None: [])
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: ["zfs:box"])
        rc = detect._cmd_run([])
        assert rc == 0
        out = capsys.readouterr()
        kv = _parse_kv(out.out)
        assert "zfs:box" in kv["matrix"]
        assert kv["site_test"] == "false"
        assert "HEAD~1" in out.err

    def test_ci_base_ref_override(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_SHA", "head")
        monkeypatch.setenv("GITHUB_REF_NAME", "master")
        monkeypatch.setenv("GITHUB_REF", "refs/heads/master")
        monkeypatch.setenv("CI_BASE_REF", "HEAD~3")
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "resolved_sha")
        monkeypatch.setattr(detect, "git_diff_files", lambda base, head="HEAD": [])
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "short")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: [])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {})
        monkeypatch.setattr(detect, "build_test_matrix", lambda roles, extra=None: [])
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: [])
        rc = detect._cmd_run([])
        assert rc == 0
        assert "CI_BASE_REF override" in capsys.readouterr().err

    def test_push_with_green_base(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_SHA", "head_sha_123")
        monkeypatch.setenv("GITHUB_REF_NAME", "master")
        monkeypatch.setenv("GITHUB_REF", "refs/heads/master")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setattr(detect, "resolve_green_base", lambda **kw: "green_sha_abc")
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: ref)
        monkeypatch.setattr(
            detect,
            "git_diff_files",
            lambda base, head="HEAD": ["roles/nginx/tasks/main.yml"],
        )
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "abc123")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: ["nginx", "podman"])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {})
        monkeypatch.setattr(detect, "release_ubuntu_for", lambda role: [])
        monkeypatch.setattr(detect, "build_test_matrix", lambda roles, extra=None: [])
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: ["nginx:box"])
        rc = detect._cmd_run([])
        assert rc == 0
        assert "nginx:box" in capsys.readouterr().out

    def test_push_no_green_full_universe(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_SHA", "head")
        monkeypatch.setenv("GITHUB_REF_NAME", "master")
        monkeypatch.setenv("GITHUB_REF", "refs/heads/master")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setattr(detect, "resolve_green_base", lambda **kw: None)
        monkeypatch.setattr(detect, "_full_universe_matrix", lambda: '["big:box"]')
        rc = detect._cmd_run([])
        assert rc == 0
        kv = _parse_kv(capsys.readouterr().out)
        assert kv["site_test"] == "true"

    def test_push_green_needs_fetch(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_SHA", "head")
        monkeypatch.setenv("GITHUB_REF_NAME", "master")
        monkeypatch.setenv("GITHUB_REF", "refs/heads/master")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setattr(detect, "resolve_green_base", lambda **kw: "green_sha")
        rev_calls = {"n": 0}

        def mock_rev_parse(ref):
            if ref == "green_sha":
                rev_calls["n"] += 1
                return "green_sha" if rev_calls["n"] > 1 else None
            return ref

        monkeypatch.setattr(detect, "git_rev_parse", mock_rev_parse)
        monkeypatch.setattr(detect, "git_fetch_commit", lambda sha: True)
        monkeypatch.setattr(detect, "git_diff_files", lambda base, head="HEAD": [])
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "short")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: [])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {})
        monkeypatch.setattr(detect, "build_test_matrix", lambda roles, extra=None: [])
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: [])
        rc = detect._cmd_run([])
        assert rc == 0
        # assert "fetching the commit" in capsys.readouterr().err

    def test_full_universe_on_group_vars(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(
            detect,
            "git_diff_files",
            lambda base, head="HEAD": ["group_vars/all/main.yml"],
        )
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "short")
        monkeypatch.setattr(detect, "_full_universe_matrix", lambda: '["big:box"]')
        rc = detect._cmd_run([])
        assert rc == 0
        out = capsys.readouterr()
        assert "FULL universe" in out.err
        kv = _parse_kv(out.out)
        assert kv["site_test"] == "true"

    def test_packer_change_adds_packer_role(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(
            detect,
            "git_diff_files",
            lambda base, head="HEAD": ["packer/scripts/chroot.sh"],
        )
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "short")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: ["packer", "nginx"])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {})
        monkeypatch.setattr(detect, "release_ubuntu_for", lambda role: [])
        captured = {}
        monkeypatch.setattr(
            detect,
            "build_test_matrix",
            lambda roles, extra=None: (captured.update(roles=list(roles)), [])[1],
        )
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: ["packer:box"])
        rc = detect._cmd_run([])
        assert rc == 0
        assert "packer" in captured["roles"]
        kv = _parse_kv(capsys.readouterr().out)
        assert kv["packer_changed"] == "true"

    def test_role_deps_expansion(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(
            detect,
            "git_diff_files",
            lambda base, head="HEAD": ["roles/systemd_unit/tasks/main.yml"],
        )
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "short")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: ["systemd_unit", "nginx", "redis"])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {"systemd_unit": ["nginx", "redis"]})
        monkeypatch.setattr(detect, "release_ubuntu_for", lambda role: [])
        captured = {}
        monkeypatch.setattr(
            detect,
            "build_test_matrix",
            lambda roles, extra=None: (captured.update(roles=list(roles)), [])[1],
        )
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: ["systemd_unit:box"])
        rc = detect._cmd_run([])
        assert rc == 0
        assert "nginx" in captured["roles"]
        assert "redis" in captured["roles"]
        assert "systemd_unit" in captured["roles"]

    def test_release_cell_propagation(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(
            detect,
            "git_diff_files",
            lambda base, head="HEAD": ["roles/apt_source/tasks/main.yml"],
        )
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "short")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: ["apt_source", "nginx"])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {"apt_source": ["nginx"]})
        monkeypatch.setattr(
            detect,
            "release_ubuntu_for",
            lambda role: ["noble"] if role == "apt_source" else [],
        )
        monkeypatch.setattr(detect, "machines_for", lambda role: {"box": None})
        captured = {}
        monkeypatch.setattr(
            detect,
            "build_test_matrix",
            lambda roles, extra=None: (captured.update(roles=list(roles), extra=extra), [])[1],
        )
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: ["apt_source:box"])
        rc = detect._cmd_run([])
        assert rc == 0
        assert captured["extra"] is not None
        extra_specs = [f"{c.role}:{c.machine}:{c.ubuntu}" for c in captured["extra"]]
        assert "nginx:box:noble" in extra_specs

    def test_ci_image_on_master_push(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_SHA", "head")
        monkeypatch.setenv("GITHUB_REF_NAME", "master")
        monkeypatch.setenv("GITHUB_REF", "refs/heads/master")
        monkeypatch.setenv("CI_BASE_REF", "HEAD~1")
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "resolved")
        monkeypatch.setattr(
            detect,
            "git_diff_files",
            lambda base, head="HEAD": ["Dockerfile", "roles/nginx/tasks/main.yml"],
        )
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "short")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: ["nginx"])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {})
        monkeypatch.setattr(detect, "release_ubuntu_for", lambda role: [])
        monkeypatch.setattr(detect, "build_test_matrix", lambda roles, extra=None: [])
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: ["nginx:box"])
        rc = detect._cmd_run([])
        assert rc == 0
        kv = _parse_kv(capsys.readouterr().out)
        assert kv["ci_image_changed"] == "true"

    def test_empty_changeset(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(detect, "git_diff_files", lambda base, head="HEAD": [])
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "short")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: ["nginx"])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {})
        monkeypatch.setattr(detect, "build_test_matrix", lambda roles, extra=None: [])
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: [])
        rc = detect._cmd_run([])
        assert rc == 0
        assert "no role-relevant changes" in capsys.readouterr().err

    def test_role_not_in_universe_skipped(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr(detect, "git_rev_parse", lambda ref: "abc")
        monkeypatch.setattr(
            detect,
            "git_diff_files",
            lambda base, head="HEAD": ["roles/helper_only/tasks/setup.yml"],
        )
        monkeypatch.setattr(detect, "git_rev_parse_short", lambda ref: "short")
        monkeypatch.setattr(detect, "list_testable_roles", lambda: ["nginx"])
        monkeypatch.setattr(detect, "build_role_deps_map", lambda: {"helper_only": ["nginx"]})
        monkeypatch.setattr(detect, "release_ubuntu_for", lambda role: [])
        captured = {}
        monkeypatch.setattr(
            detect,
            "build_test_matrix",
            lambda roles, extra=None: (captured.update(roles=list(roles)), [])[1],
        )
        monkeypatch.setattr(detect, "cells_to_ci_specs", lambda cells: ["nginx:box"])
        rc = detect._cmd_run([])
        assert rc == 0
        assert "nginx" in captured["roles"]
        assert "helper_only" not in captured["roles"]


class TestMainEntrypoint:
    def test_no_args_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["detect.py"])
        assert detect.main() == 2

    def test_unknown_command_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["detect.py", "bogus"])
        assert detect.main() == 2

    def test_emit_via_main(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        monkeypatch.setattr("sys.argv", ["detect.py", "emit", "--matrix", "[]"])
        assert detect.main() == 0
        out = capsys.readouterr().out
        assert "matrix=" in out

    def test_run_in_commands(self) -> None:
        assert "run" in detect._COMMANDS
