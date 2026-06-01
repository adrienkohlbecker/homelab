"""Unit tests for mise-tasks/ci/detect-roles.sh.

Tests path-classification regexes (via grep against bash EREs), packer
source/ubuntu matrix computation, and end-to-end mode dispatch.
Bucket splitting and packer matrix are now handled by detect.py and
tested in test_detect.py.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

DETECT_ROLES_SH = Path(__file__).resolve().parent.parent.parent / "mise-tasks" / "ci" / "detect-roles.sh"
REPO_ROOT = DETECT_ROLES_SH.parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_bash(script: str, *, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
        env=merged_env,
    )


def _parse_emit_output(stdout: str) -> dict[str, str]:
    result = {}
    for line in stdout.strip().splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Path classification regexes
# ---------------------------------------------------------------------------


class TestPathRegexes:
    """Verify the ERE patterns from detect-roles.sh match expected paths."""

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        result = _run_bash(f"echo {shlex.quote(path)} | grep -qE {shlex.quote(pattern)}")
        return result.returncode == 0

    @pytest.fixture(autouse=True)
    def _build_regexes(self) -> None:
        # Reconstruct the regexes from the script source to stay in sync.
        script_text = DETECT_ROLES_SH.read_text()

        # Extract FULL_UNIVERSE_PATTERNS
        fu_patterns = []
        in_fu = False
        for line in script_text.splitlines():
            if "FULL_UNIVERSE_PATTERNS=(" in line:
                in_fu = True
                continue
            if in_fu:
                if line.strip() == ")":
                    break
                pat = line.strip().split("#")[0].strip().strip("'\"")
                if pat:
                    fu_patterns.append(pat)

        self.full_universe_re = "^(" + "|".join(fu_patterns) + ")$"

        # Extract PACKER_PATH_PATTERNS
        pp_patterns = []
        in_pp = False
        for line in script_text.splitlines():
            if "PACKER_PATH_PATTERNS=(" in line:
                in_pp = True
                continue
            if in_pp:
                if line.strip() == ")":
                    break
                pat = line.strip().split("#")[0].strip().strip("'\"")
                if pat:
                    pp_patterns.append(pat)

        self.packer_paths_re = "^(" + "|".join(pp_patterns) + ")"

        # Extract CI_IMAGE_INPUT_PATTERNS
        ci_patterns = []
        in_ci = False
        for line in script_text.splitlines():
            if "CI_IMAGE_INPUT_PATTERNS=(" in line:
                in_ci = True
                continue
            if in_ci:
                if line.strip() == ")":
                    break
                pat = line.strip().split("#")[0].strip().strip("'\"")
                if pat:
                    ci_patterns.append(pat)

        self.ci_image_re = "^(" + "|".join(ci_patterns) + ")$"

    # -- Full universe --

    def test_full_universe_group_vars(self) -> None:
        assert self._matches(self.full_universe_re, "group_vars/all/main.yml")
        assert self._matches(self.full_universe_re, "group_vars/all/service_ports.yaml")
        assert self._matches(self.full_universe_re, "group_vars/test.yml")

    def test_full_universe_host_vars(self) -> None:
        assert self._matches(self.full_universe_re, "host_vars/box.yml")
        assert self._matches(self.full_universe_re, "host_vars/minimal.yml")
        assert not self._matches(self.full_universe_re, "host_vars/lab.yml")
        assert not self._matches(self.full_universe_re, "host_vars/pug.yml")

    def test_full_universe_test_harness(self) -> None:
        assert self._matches(self.full_universe_re, "test/machine.py")
        assert self._matches(self.full_universe_re, "test/testall.py")
        assert self._matches(self.full_universe_re, "test/matrix.py")
        assert not self._matches(self.full_universe_re, "test/unit/test_matrix.py")

    def test_full_universe_test_subdirs(self) -> None:
        assert self._matches(self.full_universe_re, "test/playbooks/site.yml")
        assert self._matches(self.full_universe_re, "test/minimal/cloud-init.yml")

    def test_full_universe_config_files(self) -> None:
        assert self._matches(self.full_universe_re, "ansible.cfg")
        assert self._matches(self.full_universe_re, "vault-client.sh")
        assert self._matches(self.full_universe_re, "mise.toml")
        assert self._matches(self.full_universe_re, "pyproject.toml")

    def test_full_universe_topology(self) -> None:
        assert self._matches(self.full_universe_re, "data/network_topology.yml")
        assert self._matches(self.full_universe_re, "data/network_topology.schema.json")

    def test_full_universe_rejects_role_files(self) -> None:
        assert not self._matches(self.full_universe_re, "roles/nginx/tasks/main.yml")
        assert not self._matches(self.full_universe_re, "roles/podman/templates/foo.j2")

    # -- Packer paths --

    def test_packer_paths(self) -> None:
        assert self._matches(self.packer_paths_re, "packer/qemu.pkr.hcl")
        assert self._matches(self.packer_paths_re, "packer/scripts/chroot.sh")
        assert self._matches(self.packer_paths_re, "mise-tasks/packer/build")

    def test_packer_paths_rejects(self) -> None:
        assert not self._matches(self.packer_paths_re, "roles/packer/tasks/main.yml")
        assert not self._matches(self.packer_paths_re, "test/machine.py")

    # -- CI image inputs --

    def test_ci_image_inputs(self) -> None:
        assert self._matches(self.ci_image_re, "Dockerfile")
        assert self._matches(self.ci_image_re, "mise.toml")
        assert self._matches(self.ci_image_re, "pyproject.toml")
        assert self._matches(self.ci_image_re, "uv.lock")
        assert self._matches(self.ci_image_re, "packer/qemu.pkr.hcl")

    def test_ci_image_rejects(self) -> None:
        assert not self._matches(self.ci_image_re, "packer/scripts/chroot.sh")
        assert not self._matches(self.ci_image_re, "ansible.cfg")


# ---------------------------------------------------------------------------
# Packer source matrix
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# --all and --dispatch modes (end-to-end against real repo)
# ---------------------------------------------------------------------------


class TestModesEndToEnd:
    """Run detect-roles.sh modes that don't need git diff or GitHub API."""

    def test_all_mode(self) -> None:
        result = _run_bash(
            f"bash {DETECT_ROLES_SH} --all 2>/dev/null",
            env={
                **os.environ,
                "INPUTS_SOURCES": "",
                "INPUTS_UBUNTU": "",
            },
        )
        assert result.returncode == 0
        out = _parse_emit_output(result.stdout)
        matrix = json.loads(out["matrix"])
        assert len(matrix) > 50

    def test_dispatch_mode(self) -> None:
        result = _run_bash(
            f"bash {DETECT_ROLES_SH} 2>/dev/null",
            env={
                **os.environ,
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "INPUTS_ROLES": "cleanup",
                "INPUTS_SOURCES": "",
                "INPUTS_UBUNTU": "",
            },
        )
        assert result.returncode == 0
        out = _parse_emit_output(result.stdout)
        matrix = json.loads(out["matrix"])
        assert "cleanup:box" in matrix
        assert "cleanup:minimal" in matrix

    def test_dispatch_exact_spec(self) -> None:
        result = _run_bash(
            f"bash {DETECT_ROLES_SH} 2>/dev/null",
            env={
                **os.environ,
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "INPUTS_ROLES": "cleanup:box",
                "INPUTS_SOURCES": "",
                "INPUTS_UBUNTU": "",
            },
        )
        assert result.returncode == 0
        out = _parse_emit_output(result.stdout)
        matrix = json.loads(out["matrix"])
        assert matrix == ["cleanup:box"]

    def test_dispatch_all_keyword(self) -> None:
        result = _run_bash(
            f"bash {DETECT_ROLES_SH} 2>/dev/null",
            env={
                **os.environ,
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "INPUTS_ROLES": "ALL",
                "INPUTS_SOURCES": "",
                "INPUTS_UBUNTU": "",
            },
        )
        assert result.returncode == 0
        out = _parse_emit_output(result.stdout)
        matrix = json.loads(out["matrix"])
        assert len(matrix) > 50

    def test_packer_only_dispatch(self) -> None:
        result = _run_bash(
            f"bash {DETECT_ROLES_SH} 2>/dev/null",
            env={
                **os.environ,
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "INPUTS_ROLES": "",
                "INPUTS_SOURCES": "lab pug",
                "INPUTS_UBUNTU": "",
            },
        )
        assert result.returncode == 0
        out = _parse_emit_output(result.stdout)
        assert json.loads(out["matrix"]) == []
        assert out["packer_changed"] == "true"
        sources = json.loads(out["packer_sources"])
        assert sorted(sources) == ["lab", "pug"]
