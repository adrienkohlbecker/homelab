"""Unit tests for mise-tasks/ci/detect.py — CI change-detection logic.

Tests the pure Python equivalents of detect-roles.sh's data transforms:
path classification regexes, file classification, matrix bucket splitting,
packer source/ubuntu matrices, and release-cell propagation.
"""

import importlib
import io
import json
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent.parent / "mise-tasks" / "ci" / "detect.py"


def _load():
    spec = importlib.util.spec_from_file_location("detect", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


detect = _load()


# ---------------------------------------------------------------------------
# FULL_UNIVERSE_RE
# ---------------------------------------------------------------------------


class TestFullUniverseRe:
    def test_group_vars_yml(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("group_vars/all/main.yml")

    def test_group_vars_yaml(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("group_vars/all/service_ports.yaml")

    def test_group_vars_test(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("group_vars/test.yml")

    def test_host_vars_box(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("host_vars/box.yml")

    def test_host_vars_minimal(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("host_vars/minimal.yml")

    def test_host_vars_lab_excluded(self) -> None:
        assert not detect.FULL_UNIVERSE_RE.match("host_vars/lab.yml")

    def test_host_vars_pug_excluded(self) -> None:
        assert not detect.FULL_UNIVERSE_RE.match("host_vars/pug.yml")

    def test_test_module(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("test/machine.py")

    def test_test_subdir_excluded(self) -> None:
        assert not detect.FULL_UNIVERSE_RE.match("test/unit/test_matrix.py")

    def test_test_playbooks(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("test/playbooks/site.yml")

    def test_test_minimal_subdir(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("test/minimal/cloud-init.yml")

    def test_ansible_cfg(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("ansible.cfg")

    def test_vault_client(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("vault-client.sh")

    def test_mise_toml(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("mise.toml")

    def test_pyproject_toml(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("pyproject.toml")

    def test_uv_lock(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("uv.lock")

    def test_topology_yml(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("data/network_topology.yml")

    def test_topology_schema(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("data/network_topology.schema.json")

    def test_role_file_excluded(self) -> None:
        assert not detect.FULL_UNIVERSE_RE.match("roles/nginx/tasks/main.yml")

    def test_random_file_excluded(self) -> None:
        assert not detect.FULL_UNIVERSE_RE.match("README.md")

    def test_test_inventory(self) -> None:
        assert detect.FULL_UNIVERSE_RE.match("test/inventory.ini")

    def test_group_vars_nested_dir_excluded(self) -> None:
        assert not detect.FULL_UNIVERSE_RE.match("group_vars/all/sub/deep.yml")


# ---------------------------------------------------------------------------
# PACKER_PATHS_RE
# ---------------------------------------------------------------------------


class TestPackerPathsRe:
    def test_packer_file(self) -> None:
        assert detect.PACKER_PATHS_RE.match("packer/qemu.pkr.hcl")

    def test_packer_script(self) -> None:
        assert detect.PACKER_PATHS_RE.match("packer/scripts/chroot.sh")

    def test_mise_packer_task(self) -> None:
        assert detect.PACKER_PATHS_RE.match("mise-tasks/packer/build")

    def test_role_packer_excluded(self) -> None:
        assert not detect.PACKER_PATHS_RE.match("roles/packer/tasks/main.yml")

    def test_test_file_excluded(self) -> None:
        assert not detect.PACKER_PATHS_RE.match("test/machine.py")


# ---------------------------------------------------------------------------
# CI_IMAGE_INPUTS_RE
# ---------------------------------------------------------------------------


class TestCiImageInputsRe:
    def test_dockerfile(self) -> None:
        assert detect.CI_IMAGE_INPUTS_RE.match("Dockerfile")

    def test_mise_toml(self) -> None:
        assert detect.CI_IMAGE_INPUTS_RE.match("mise.toml")

    def test_pyproject_toml(self) -> None:
        assert detect.CI_IMAGE_INPUTS_RE.match("pyproject.toml")

    def test_uv_lock(self) -> None:
        assert detect.CI_IMAGE_INPUTS_RE.match("uv.lock")

    def test_packer_hcl(self) -> None:
        assert detect.CI_IMAGE_INPUTS_RE.match("packer/qemu.pkr.hcl")

    def test_packer_script_excluded(self) -> None:
        assert not detect.CI_IMAGE_INPUTS_RE.match("packer/scripts/chroot.sh")

    def test_ansible_cfg_excluded(self) -> None:
        assert not detect.CI_IMAGE_INPUTS_RE.match("ansible.cfg")


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
        result = detect.classify_changed_files([
            "roles/nginx/tasks/main.yml",
            "roles/podman/templates/foo.j2",
        ])
        assert result.direct_roles == ["nginx", "podman"]
        assert not result.packer_changed
        assert not result.ci_image_changed
        assert result.full_universe_paths == []

    def test_full_universe_trigger(self) -> None:
        result = detect.classify_changed_files(["group_vars/all/main.yml"])
        assert result.full_universe_paths == ["group_vars/all/main.yml"]

    def test_packer_changed(self) -> None:
        result = detect.classify_changed_files(["packer/qemu.pkr.hcl"])
        assert result.packer_changed is True

    def test_ci_image_on_master(self) -> None:
        result = detect.classify_changed_files(["Dockerfile"], is_master_push=True)
        assert result.ci_image_changed is True

    def test_ci_image_off_master(self) -> None:
        result = detect.classify_changed_files(["Dockerfile"], is_master_push=False)
        assert result.ci_image_changed is False

    def test_mixed_paths(self) -> None:
        paths = [
            "roles/nginx/tasks/main.yml",
            "packer/scripts/chroot.sh",
            "group_vars/all/main.yml",
            "Dockerfile",
        ]
        result = detect.classify_changed_files(paths, is_master_push=True)
        assert result.direct_roles == ["nginx"]
        assert result.packer_changed is True
        assert result.ci_image_changed is True
        assert result.full_universe_paths == ["group_vars/all/main.yml"]

    def test_empty_paths(self) -> None:
        result = detect.classify_changed_files([])
        assert result == detect.ChangeClassification([], [], False, False)

    def test_blank_lines_ignored(self) -> None:
        result = detect.classify_changed_files(["", "roles/nginx/tasks/main.yml", ""])
        assert result.direct_roles == ["nginx"]

    def test_deduplicates_roles(self) -> None:
        result = detect.classify_changed_files([
            "roles/nginx/tasks/main.yml",
            "roles/nginx/templates/site.conf.j2",
        ])
        assert result.direct_roles == ["nginx"]

    def test_multiple_full_universe_paths(self) -> None:
        result = detect.classify_changed_files([
            "mise.toml",
            "pyproject.toml",
            "uv.lock",
        ])
        assert result.full_universe_paths == ["mise.toml", "pyproject.toml", "uv.lock"]

    def test_packer_and_role_simultaneous(self) -> None:
        result = detect.classify_changed_files([
            "packer/scripts/chroot.sh",
            "roles/zfs/tasks/main.yml",
        ])
        assert result.packer_changed is True
        assert result.direct_roles == ["zfs"]


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
        result = detect.split_matrix_buckets(["cleanup:minimal", "somerole:lab", "anotherrole:pug"])
        assert result.minimal == ["anotherrole:pug", "cleanup:minimal", "somerole:lab"]
        assert result.jammy == []

    def test_mixed_matrix(self) -> None:
        specs = [
            "alpha:box",
            "beta:box_deps",
            "cleanup:minimal",
            "netdata:box_deps:noble",
            "podman:box:resolute",
        ]
        result = detect.split_matrix_buckets(specs)
        assert result.jammy == ["alpha:box", "beta:box_deps"]
        assert result.noble == ["netdata:box_deps:noble"]
        assert result.resolute == ["podman:box:resolute"]
        assert result.minimal == ["cleanup:minimal"]

    def test_empty_matrix(self) -> None:
        result = detect.split_matrix_buckets([])
        assert result == detect.MatrixBuckets([], [], [], [])

    def test_box_deps_jammy_no_release(self) -> None:
        result = detect.split_matrix_buckets(["redis:box_deps"])
        assert result.jammy == ["redis:box_deps"]

    def test_box_deps_noble(self) -> None:
        result = detect.split_matrix_buckets(["redis:box_deps:noble"])
        assert result.noble == ["redis:box_deps:noble"]

    def test_lab_pug_go_to_minimal(self) -> None:
        result = detect.split_matrix_buckets(["somerole:lab", "anotherrole:pug"])
        assert result.minimal == ["anotherrole:pug", "somerole:lab"]
        assert result.jammy == []


# ---------------------------------------------------------------------------
# compute_packer_sources
# ---------------------------------------------------------------------------


class TestComputePackerSources:
    def test_default_full_set(self) -> None:
        result = detect.compute_packer_sources()
        assert result.all == ["box", "pug", "lab", "hetzner"]
        assert result.box == ["box"]
        assert result.extra == ["pug", "lab", "hetzner"]

    def test_empty_string(self) -> None:
        result = detect.compute_packer_sources("")
        assert result.all == ["box", "pug", "lab", "hetzner"]

    def test_explicit_sources(self) -> None:
        result = detect.compute_packer_sources("lab pug")
        assert result.all == ["lab", "pug"]
        assert result.box == []
        assert result.extra == ["lab", "pug"]

    def test_box_only(self) -> None:
        result = detect.compute_packer_sources("box")
        assert result.all == ["box"]
        assert result.box == ["box"]
        assert result.extra == []

    def test_preserves_order(self) -> None:
        result = detect.compute_packer_sources("hetzner box lab")
        assert result.all == ["hetzner", "box", "lab"]
        assert result.box == ["box"]
        assert result.extra == ["hetzner", "lab"]


# ---------------------------------------------------------------------------
# compute_packer_ubuntu
# ---------------------------------------------------------------------------


class TestComputePackerUbuntu:
    def test_default_releases(self) -> None:
        result = detect.compute_packer_ubuntu()
        assert result.box == ["jammy", "noble", "resolute"]
        assert result.extra == ["jammy"]

    def test_empty_string(self) -> None:
        result = detect.compute_packer_ubuntu("")
        assert result.box == ["jammy", "noble", "resolute"]
        assert result.extra == ["jammy"]

    def test_pinned_release(self) -> None:
        result = detect.compute_packer_ubuntu("noble")
        assert result.box == ["noble"]
        assert result.extra == ["noble"]

    def test_pinned_resolute(self) -> None:
        result = detect.compute_packer_ubuntu("resolute")
        assert result.box == ["resolute"]
        assert result.extra == ["resolute"]


# ---------------------------------------------------------------------------
# propagate_release_cells
# ---------------------------------------------------------------------------


class TestPropagateReleaseCells:
    def test_basic_propagation(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["nginx", "podman"]},
            default_machines={"nginx": "box", "podman": "box_deps"},
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
            default_machines={"homepage": "box"},
            role_releases={},
            universe={"homepage"},
        )
        assert result == []

    def test_empty_releases_list(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["nginx"],
            consumers={"nginx": ["homepage"]},
            default_machines={"homepage": "box"},
            role_releases={"nginx": []},
            universe={"homepage"},
        )
        assert result == []

    def test_no_consumers(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={},
            default_machines={},
            role_releases={"apt_source": ["noble"]},
            universe=set(),
        )
        assert result == []

    def test_consumer_not_in_universe(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["helper_only"]},
            default_machines={"helper_only": "box"},
            role_releases={"apt_source": ["noble"]},
            universe=set(),
        )
        assert result == []

    def test_default_machine_fallback(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source"],
            consumers={"apt_source": ["newrole"]},
            default_machines={},
            role_releases={"apt_source": ["noble"]},
            universe={"newrole"},
        )
        assert result == ["newrole:box:noble"]

    def test_deduplicates_across_helpers(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["helper_a", "helper_b"],
            consumers={"helper_a": ["consumer"], "helper_b": ["consumer"]},
            default_machines={"consumer": "box"},
            role_releases={"helper_a": ["noble"], "helper_b": ["noble"]},
            universe={"consumer"},
        )
        assert result == ["consumer:box:noble"]

    def test_multiple_roles_multiple_releases(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=["apt_source", "podman"],
            consumers={"apt_source": ["nginx", "redis"], "podman": ["redis"]},
            default_machines={"nginx": "box", "redis": "box_deps"},
            role_releases={"apt_source": ["noble", "resolute"], "podman": ["noble"]},
            universe={"nginx", "redis"},
        )
        assert result == [
            "nginx:box:noble",
            "nginx:box:resolute",
            "redis:box_deps:noble",
            "redis:box_deps:resolute",
        ]

    def test_empty_inputs(self) -> None:
        result = detect.propagate_release_cells(
            direct_roles=[],
            consumers={},
            default_machines={},
            role_releases={},
            universe=set(),
        )
        assert result == []


# ---------------------------------------------------------------------------
# Regex parity with detect-roles.sh EREs
# ---------------------------------------------------------------------------


class TestRegexParityWithBash:
    """Verify the Python regexes match the same paths as the bash tests.

    The existing test_detect_roles.py extracts EREs from detect-roles.sh and
    tests them via subprocess grep.  If both test suites pass, the two
    implementations agree.
    """

    @pytest.mark.parametrize("path", [
        "group_vars/all/main.yml",
        "group_vars/all/service_ports.yaml",
        "group_vars/test.yml",
        "host_vars/box.yml",
        "host_vars/minimal.yml",
        "test/machine.py",
        "test/testall.py",
        "test/matrix.py",
        "test/inventory.ini",
        "test/playbooks/site.yml",
        "test/minimal/cloud-init.yml",
        "ansible.cfg",
        "vault-client.sh",
        "mise.toml",
        "pyproject.toml",
        "uv.lock",
        "data/network_topology.yml",
        "data/network_topology.schema.json",
    ])
    def test_full_universe_match(self, path: str) -> None:
        assert detect.FULL_UNIVERSE_RE.match(path), f"should match: {path}"

    @pytest.mark.parametrize("path", [
        "host_vars/lab.yml",
        "host_vars/pug.yml",
        "test/unit/test_matrix.py",
        "roles/nginx/tasks/main.yml",
        "roles/podman/templates/foo.j2",
        "README.md",
        "Dockerfile",
    ])
    def test_full_universe_reject(self, path: str) -> None:
        assert not detect.FULL_UNIVERSE_RE.match(path), f"should not match: {path}"

    @pytest.mark.parametrize("path", [
        "packer/qemu.pkr.hcl",
        "packer/scripts/chroot.sh",
        "mise-tasks/packer/build",
    ])
    def test_packer_match(self, path: str) -> None:
        assert detect.PACKER_PATHS_RE.match(path), f"should match: {path}"

    @pytest.mark.parametrize("path", [
        "roles/packer/tasks/main.yml",
        "test/machine.py",
    ])
    def test_packer_reject(self, path: str) -> None:
        assert not detect.PACKER_PATHS_RE.match(path), f"should not match: {path}"

    @pytest.mark.parametrize("path", [
        "Dockerfile",
        "mise.toml",
        "pyproject.toml",
        "uv.lock",
        "packer/qemu.pkr.hcl",
    ])
    def test_ci_image_match(self, path: str) -> None:
        assert detect.CI_IMAGE_INPUTS_RE.match(path), f"should match: {path}"

    @pytest.mark.parametrize("path", [
        "packer/scripts/chroot.sh",
        "ansible.cfg",
    ])
    def test_ci_image_reject(self, path: str) -> None:
        assert not detect.CI_IMAGE_INPUTS_RE.match(path), f"should not match: {path}"


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


class TestCmdClassify:
    def test_role_detection(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("roles/nginx/tasks/main.yml\nroles/podman/templates/foo.j2\n"))
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
        rc = detect._cmd_split_buckets([json.dumps(["alpha:box", "beta:minimal"])])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["jammy"] == ["alpha:box"]
        assert result["minimal"] == ["beta:minimal"]

    def test_no_args_returns_error(self) -> None:
        rc = detect._cmd_split_buckets([])
        assert rc == 2


class TestCmdPackerSources:
    def test_default(self, capsys: pytest.CaptureFixture) -> None:
        rc = detect._cmd_packer_sources([])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["all"] == ["box", "pug", "lab", "hetzner"]

    def test_explicit(self, capsys: pytest.CaptureFixture) -> None:
        rc = detect._cmd_packer_sources(["lab pug"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["all"] == ["lab", "pug"]
        assert result["box"] == []


class TestCmdPackerUbuntu:
    def test_default(self, capsys: pytest.CaptureFixture) -> None:
        rc = detect._cmd_packer_ubuntu([])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["box"] == ["jammy", "noble", "resolute"]
        assert result["extra"] == ["jammy"]

    def test_pinned(self, capsys: pytest.CaptureFixture) -> None:
        rc = detect._cmd_packer_ubuntu(["noble"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["box"] == ["noble"]
        assert result["extra"] == ["noble"]


class TestCmdEmit:
    @staticmethod
    def _parse_kv(text: str) -> dict[str, str]:
        result = {}
        for line in text.strip().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                result[k] = v
        return result

    def test_basic_emit(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", '["alpha:box","beta:minimal"]'])
        assert rc == 0
        out = self._parse_kv(capsys.readouterr().out)
        assert json.loads(out["matrix_jammy"]) == ["alpha:box"]
        assert json.loads(out["matrix_minimal"]) == ["beta:minimal"]
        assert json.loads(out["matrix_noble"]) == []
        assert json.loads(out["matrix_resolute"]) == []
        assert out["packer_changed"] == "false"
        assert out["ci_image_changed"] == "false"

    def test_packer_changed_flag(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--packer-changed", "true"])
        assert rc == 0
        out = self._parse_kv(capsys.readouterr().out)
        assert out["packer_changed"] == "true"
        assert out["ci_image_changed"] == "false"

    def test_ci_image_changed_flag(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--ci-image-changed", "true"])
        assert rc == 0
        out = self._parse_kv(capsys.readouterr().out)
        assert out["ci_image_changed"] == "true"

    def test_packer_sources(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--inputs-sources", "lab pug"])
        assert rc == 0
        out = self._parse_kv(capsys.readouterr().out)
        assert json.loads(out["packer_sources"]) == ["lab", "pug"]
        assert json.loads(out["packer_sources_box"]) == []
        assert json.loads(out["packer_sources_extra"]) == ["lab", "pug"]

    def test_packer_ubuntu_default(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]"])
        assert rc == 0
        out = self._parse_kv(capsys.readouterr().out)
        assert json.loads(out["packer_ubuntu_box"]) == ["jammy", "noble", "resolute"]
        assert json.loads(out["packer_ubuntu_extra"]) == ["jammy"]

    def test_packer_ubuntu_pinned(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        rc = detect._cmd_emit(["--matrix", "[]", "--inputs-ubuntu", "noble"])
        assert rc == 0
        out = self._parse_kv(capsys.readouterr().out)
        assert json.loads(out["packer_ubuntu_box"]) == ["noble"]
        assert json.loads(out["packer_ubuntu_extra"]) == ["noble"]

    def test_github_output_file(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        gh_out = tmp_path / "output"
        gh_out.touch()
        monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
        rc = detect._cmd_emit(["--matrix", '["alpha:box"]'])
        assert rc == 0
        assert capsys.readouterr().out == ""
        content = gh_out.read_text()
        kv = self._parse_kv(content)
        assert json.loads(kv["matrix_jammy"]) == ["alpha:box"]
        assert json.loads(kv["packer_sources"]) == ["box", "pug", "lab", "hetzner"]

    def test_log_to_stderr(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        detect._cmd_emit(["--matrix", '["alpha:box"]'])
        err = capsys.readouterr().err
        assert "[detect-roles] result:" in err

    def test_mixed_buckets(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        matrix = json.dumps([
            "alpha:box",
            "cleanup:minimal",
            "net:box_deps:noble",
            "apt:box:resolute",
        ])
        rc = detect._cmd_emit(["--matrix", matrix])
        assert rc == 0
        out = self._parse_kv(capsys.readouterr().out)
        assert json.loads(out["matrix_jammy"]) == ["alpha:box"]
        assert json.loads(out["matrix_noble"]) == ["net:box_deps:noble"]
        assert json.loads(out["matrix_resolute"]) == ["apt:box:resolute"]
        assert json.loads(out["matrix_minimal"]) == ["cleanup:minimal"]


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
