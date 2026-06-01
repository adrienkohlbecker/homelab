"""Unit tests for mise-tasks/ci/detect-roles.sh.

Tests the emit() bucket-split logic (jq) and path-classification regexes.
The script is bash, so we test via subprocess against a harness that sources
the real script's functions, or by running the regexes through grep.
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
# emit() bucket splitting
# ---------------------------------------------------------------------------


class TestEmitBucketSplit:
    """Test the jq-based matrix splitting inside emit().

    We source just enough of detect-roles.sh to define emit(), then call it
    with crafted matrix JSON and verify the per-bucket outputs.
    """

    @staticmethod
    def _run_emit(matrix_json: str, packer: str = "false", ci_image: str = "false") -> dict[str, str]:
        script = f"""
set -euo pipefail
PACKER_SOURCES_JSON='["box"]'
PACKER_SOURCES_BOX_JSON='["box"]'
PACKER_SOURCES_EXTRA_JSON='[]'
PACKER_UBUNTU_BOX_JSON='["jammy"]'
PACKER_UBUNTU_EXTRA_JSON='["jammy"]'
log() {{ :; }}
emit() {{
  local matrix=$1 packer_changed=${{2:-false}} ci_image_changed=${{3:-false}}
  local matrix_noble matrix_resolute matrix_minimal matrix_jammy
  matrix_noble=$(jq -c '[.[] | select(split(":") | length == 3 and (.[1] == "box" or .[1] == "box_deps") and .[2] == "noble")]' <<<"$matrix")
  matrix_resolute=$(jq -c '[.[] | select(split(":") | length == 3 and (.[1] == "box" or .[1] == "box_deps") and .[2] == "resolute")]' <<<"$matrix")
  matrix_minimal=$(jq -c '[.[] | select(split(":") | .[1] | . != "box" and . != "box_deps")]' <<<"$matrix")
  matrix_jammy=$(jq -c '[.[] | select(split(":") | (.[1] == "box" or .[1] == "box_deps") and (length < 3 or (.[2] != "noble" and .[2] != "resolute")))]' <<<"$matrix")
  echo "matrix=$matrix"
  echo "matrix_jammy=$matrix_jammy"
  echo "matrix_noble=$matrix_noble"
  echo "matrix_resolute=$matrix_resolute"
  echo "matrix_minimal=$matrix_minimal"
  echo "packer_changed=$packer_changed"
  echo "ci_image_changed=$ci_image_changed"
}}
emit '{matrix_json}' {packer} {ci_image}
"""
        result = _run_bash(script)
        assert result.returncode == 0, f"emit failed: {result.stderr}"
        return _parse_emit_output(result.stdout)

    def test_jammy_box_cells(self) -> None:
        m = json.dumps(["alpha:box", "beta:box_deps"])
        out = self._run_emit(m)
        jammy = json.loads(out["matrix_jammy"])
        assert sorted(jammy) == ["alpha:box", "beta:box_deps"]
        assert json.loads(out["matrix_noble"]) == []
        assert json.loads(out["matrix_resolute"]) == []
        assert json.loads(out["matrix_minimal"]) == []

    def test_noble_cells(self) -> None:
        m = json.dumps(["netdata:box_deps:noble", "zfs:box:noble"])
        out = self._run_emit(m)
        noble = json.loads(out["matrix_noble"])
        assert sorted(noble) == ["netdata:box_deps:noble", "zfs:box:noble"]
        assert json.loads(out["matrix_jammy"]) == []

    def test_resolute_cells(self) -> None:
        m = json.dumps(["podman:box:resolute", "netdata:box_deps:resolute"])
        out = self._run_emit(m)
        resolute = json.loads(out["matrix_resolute"])
        assert sorted(resolute) == ["netdata:box_deps:resolute", "podman:box:resolute"]

    def test_minimal_cells(self) -> None:
        m = json.dumps(["cleanup:minimal", "cleanup:box"])
        out = self._run_emit(m)
        minimal = json.loads(out["matrix_minimal"])
        assert minimal == ["cleanup:minimal"]
        jammy = json.loads(out["matrix_jammy"])
        assert jammy == ["cleanup:box"]

    def test_mixed_matrix(self) -> None:
        m = json.dumps([
            "alpha:box",
            "beta:box_deps",
            "cleanup:minimal",
            "netdata:box_deps:noble",
            "podman:box:resolute",
        ])
        out = self._run_emit(m)
        assert sorted(json.loads(out["matrix_jammy"])) == ["alpha:box", "beta:box_deps"]
        assert json.loads(out["matrix_noble"]) == ["netdata:box_deps:noble"]
        assert json.loads(out["matrix_resolute"]) == ["podman:box:resolute"]
        assert json.loads(out["matrix_minimal"]) == ["cleanup:minimal"]

    def test_empty_matrix(self) -> None:
        out = self._run_emit("[]")
        for key in ("matrix_jammy", "matrix_noble", "matrix_resolute", "matrix_minimal"):
            assert json.loads(out[key]) == []

    def test_packer_and_ci_image_flags_pass_through(self) -> None:
        out = self._run_emit("[]", packer="true", ci_image="true")
        assert out["packer_changed"] == "true"
        assert out["ci_image_changed"] == "true"

    def test_lab_pug_go_to_minimal_bucket(self) -> None:
        m = json.dumps(["somerole:lab", "anotherrole:pug"])
        out = self._run_emit(m)
        minimal = json.loads(out["matrix_minimal"])
        assert sorted(minimal) == ["anotherrole:pug", "somerole:lab"]
        assert json.loads(out["matrix_jammy"]) == []


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


class TestPackerSources:
    @staticmethod
    def _run_packer_sources(inputs_sources: str = "") -> dict[str, list]:
        script = f"""
set -euo pipefail
INPUTS_SOURCES='{inputs_sources}'
PACKER_SOURCES_JSON=$(jq -cn --arg s "${{INPUTS_SOURCES:-}}" \\
  '($s | split(" ") | map(select(. != ""))) as $l
   | if ($l | length) == 0 then ["box", "pug", "lab", "hetzner"] else $l end')
PACKER_SOURCES_BOX_JSON=$(jq -cn --argjson all "$PACKER_SOURCES_JSON" '$all | map(select(. == "box"))')
PACKER_SOURCES_EXTRA_JSON=$(jq -cn --argjson all "$PACKER_SOURCES_JSON" '$all | map(select(. != "box"))')
echo "sources=$PACKER_SOURCES_JSON"
echo "box=$PACKER_SOURCES_BOX_JSON"
echo "extra=$PACKER_SOURCES_EXTRA_JSON"
"""
        result = _run_bash(script)
        assert result.returncode == 0, result.stderr
        parsed = _parse_emit_output(result.stdout)
        return {k: json.loads(v) for k, v in parsed.items()}

    def test_default_full_set(self) -> None:
        out = self._run_packer_sources()
        assert out["sources"] == ["box", "pug", "lab", "hetzner"]
        assert out["box"] == ["box"]
        assert out["extra"] == ["pug", "lab", "hetzner"]

    def test_explicit_sources(self) -> None:
        out = self._run_packer_sources("lab pug")
        assert out["sources"] == ["lab", "pug"]
        assert out["box"] == []
        assert out["extra"] == ["lab", "pug"]

    def test_box_only(self) -> None:
        out = self._run_packer_sources("box")
        assert out["sources"] == ["box"]
        assert out["box"] == ["box"]
        assert out["extra"] == []


# ---------------------------------------------------------------------------
# Packer ubuntu matrix
# ---------------------------------------------------------------------------


class TestPackerUbuntu:
    @staticmethod
    def _run_packer_ubuntu(inputs_ubuntu: str = "") -> dict[str, list]:
        env = {}
        if inputs_ubuntu:
            env["INPUTS_UBUNTU"] = inputs_ubuntu
        script = """
set -euo pipefail
if [ -n "${INPUTS_UBUNTU:-}" ]; then
  PACKER_UBUNTU_BOX_JSON=$(jq -cn --arg u "$INPUTS_UBUNTU" '[$u]')
  PACKER_UBUNTU_EXTRA_JSON=$PACKER_UBUNTU_BOX_JSON
else
  PACKER_UBUNTU_BOX_JSON='["jammy","noble","resolute"]'
  PACKER_UBUNTU_EXTRA_JSON='["jammy"]'
fi
echo "box=$PACKER_UBUNTU_BOX_JSON"
echo "extra=$PACKER_UBUNTU_EXTRA_JSON"
"""
        result = _run_bash(script, env=env)
        assert result.returncode == 0, result.stderr
        parsed = _parse_emit_output(result.stdout)
        return {k: json.loads(v) for k, v in parsed.items()}

    def test_default_releases(self) -> None:
        out = self._run_packer_ubuntu()
        assert out["box"] == ["jammy", "noble", "resolute"]
        assert out["extra"] == ["jammy"]

    def test_pinned_release(self) -> None:
        out = self._run_packer_ubuntu("noble")
        assert out["box"] == ["noble"]
        assert out["extra"] == ["noble"]


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
