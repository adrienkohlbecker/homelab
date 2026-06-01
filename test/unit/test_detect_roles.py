"""End-to-end tests for mise-tasks/ci/detect-roles.sh.

Validates that the thin bash wrapper correctly delegates to detect.py's
``run`` command. Path classification regexes and data transforms are tested
directly in test_detect.py; these tests exercise the full pipeline via
subprocess.
"""

import json
import os
import subprocess
from pathlib import Path

DETECT_ROLES_SH = (
    Path(__file__).resolve().parent.parent.parent
    / "mise-tasks"
    / "ci"
    / "detect-roles.sh"
)
REPO_ROOT = DETECT_ROLES_SH.parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_bash(
    script: str, *, env: dict | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess:
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
