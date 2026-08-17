"""Unit tests for test/matrix.py — test matrix generation."""

import json
import subprocess
import sys
from pathlib import Path

import matrix
import pytest


@pytest.fixture
def roles_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scaffold a minimal roles/ tree and point matrix at it."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _make_role(root: Path, name: str, meta: dict | None = None) -> None:
    tasks = root / "roles" / name / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "main.yml").write_text("---\n")
    if meta is not None:
        meta_dir = root / "roles" / name / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        import yaml

        (meta_dir / "test.yml").write_text(yaml.dump(meta))


# ---------------------------------------------------------------------------
# list_testable_roles
# ---------------------------------------------------------------------------


class TestListTestableRoles:
    def test_discovers_roles_with_main_yml(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha")
        _make_role(roles_tree, "beta")
        (roles_tree / "roles" / "gamma").mkdir(parents=True)
        assert matrix.list_testable_roles() == ["alpha", "beta"]

    def test_empty_when_no_roles_dir(self, roles_tree: Path) -> None:
        assert matrix.list_testable_roles() == []

    def test_sorted_output(self, roles_tree: Path) -> None:
        for name in ["zeta", "alpha", "mu"]:
            _make_role(roles_tree, name)
        assert matrix.list_testable_roles() == ["alpha", "mu", "zeta"]


# ---------------------------------------------------------------------------
# machines_for / default_machine_for / release_ubuntu_for
# ---------------------------------------------------------------------------


class TestRoleMeta:
    def test_default_machine_falls_back_to_box(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "plain")
        assert matrix.default_machine_for("plain") == "box"

    def test_default_machine_reads_meta(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "fancy", {"machines": {"box_deps": None}})
        assert matrix.default_machine_for("fancy") == "box_deps"

    def test_machines_for_defaults(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "plain")
        assert matrix.machines_for("plain") == {"box": None}

    def test_machines_for_reads_dict(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "multi", {"machines": {"box": None, "minimal": None}})
        assert matrix.machines_for("multi") == {"box": None, "minimal": None}

    def test_release_ubuntu_empty_when_absent(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "plain")
        assert matrix.release_ubuntu_for("plain") == []

    def test_release_ubuntu_reads_list(self, roles_tree: Path) -> None:
        # Raw passthrough of whatever meta/test.yml declares, so the codenames
        # here are data rather than a claim about the current default.
        _make_role(roles_tree, "multi", {"ubuntu": ["noble", "resolute"]})
        assert matrix.release_ubuntu_for("multi") == ["noble", "resolute"]


# ---------------------------------------------------------------------------
# build_role_cells
# ---------------------------------------------------------------------------


class TestBuildRoleCells:
    def test_plain_role_one_cell(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "plain")
        cells = matrix.build_role_cells("plain")
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "plain")]

    def test_box_deps_role(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "svc", {"machines": {"box_deps": None}})
        cells = matrix.build_role_cells("svc")
        assert cells == [matrix.TestCell("box_deps", matrix.DEFAULT_UBUNTU, "svc")]

    def test_minimal_machine_gets_extra_cell(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "cleanup", {"machines": {"box": None, "minimal": None}})
        cells = matrix.build_role_cells("cleanup")
        assert matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "cleanup") in cells
        assert matrix.TestCell("minimal", matrix.DEFAULT_UBUNTU, "cleanup") in cells
        assert len(cells) == 2

    def test_release_cells_use_primary_machine(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "netdata", {"machines": {"box_deps": None}, "ubuntu": ["resolute"]})
        cells = matrix.build_role_cells("netdata")
        assert matrix.TestCell("box_deps", matrix.DEFAULT_UBUNTU, "netdata") in cells
        assert matrix.TestCell("box_deps", "resolute", "netdata") in cells
        assert len(cells) == 2

    def test_multi_machine_plus_release(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "podman", {"machines": {"box": None, "minimal": None}, "ubuntu": ["resolute"]})
        cells = matrix.build_role_cells("podman")
        expected = [
            matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "podman"),
            matrix.TestCell("minimal", matrix.DEFAULT_UBUNTU, "podman"),
            matrix.TestCell("box", "resolute", "podman"),
            matrix.TestCell("minimal", "resolute", "podman"),
        ]
        assert cells == expected


# ---------------------------------------------------------------------------
# build_test_matrix
# ---------------------------------------------------------------------------


class TestBuildTestMatrix:
    def test_deduplicates(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha")
        cells = matrix.build_test_matrix(["alpha", "alpha"])
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha")]

    def test_sorted_by_all_fields(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "beta")
        _make_role(roles_tree, "alpha", {"ubuntu": ["resolute"]})
        cells = matrix.build_test_matrix(["beta", "alpha"])
        assert cells == sorted(cells)

    def test_extra_cells_merged(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha")
        extra = [matrix.TestCell("box", "resolute", "alpha")]
        cells = matrix.build_test_matrix(["alpha"], extra_cells=extra)
        assert matrix.TestCell("box", "resolute", "alpha") in cells
        assert matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha") in cells

    def test_empty_roles_with_extra(self, roles_tree: Path) -> None:
        cells = matrix.build_test_matrix([], extra_cells=[matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "foo")])
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "foo")]


# ---------------------------------------------------------------------------
# skip:
# ---------------------------------------------------------------------------


class TestSkip:
    def test_skip_for_parses_machine_and_release(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "svc", {"skip": {"minimal": "why", "box_deps:resolute": "why"}})
        assert matrix.skip_for("svc") == {("minimal", matrix.DEFAULT_UBUNTU), ("box_deps", "resolute")}

    def test_skip_for_empty_when_absent(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "svc")
        assert matrix.skip_for("svc") == set()

    def test_build_role_cells_drops_skipped_noble_cell(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "svc", {"machines": {"box": None, "minimal": None}, "skip": {"minimal": "flaky"}})
        cells = matrix.build_role_cells("svc")
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "svc")]

    def test_build_role_cells_drops_skipped_release_cell_only(self, roles_tree: Path) -> None:
        _make_role(
            roles_tree,
            "svc",
            {"machines": {"box": None}, "ubuntu": ["resolute"], "skip": {"box:resolute": "flaky"}},
        )
        cells = matrix.build_role_cells("svc")
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "svc")]

    def test_bare_machine_skip_drops_only_that_machines_base_cell(self, roles_tree: Path) -> None:
        # The bare form is the only correct spelling for the default cell;
        # lint/test-meta.py rejects the `minimal:noble` spelling that reads as
        # an escalation skip but lands here identically.
        _make_role(roles_tree, "svc", {"machines": {"box": None, "minimal": None}, "skip": {"minimal": "flaky"}})
        assert matrix.build_role_cells("svc") == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "svc")]

    def test_listing_the_default_release_adds_no_duplicate_cell(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "svc", {"machines": {"box": None}, "ubuntu": [matrix.DEFAULT_UBUNTU]})
        assert matrix.build_role_cells("svc") == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "svc")]

    def test_build_test_matrix_drops_skipped_propagated_extra(self, roles_tree: Path) -> None:
        # A consumer's release cell pushed in via CI fan-out must still drop
        # if that consumer skips it.
        _make_role(roles_tree, "svc", {"machines": {"box": None}, "skip": {"box:resolute": "flaky"}})
        extra = [matrix.TestCell("box", "resolute", "svc")]
        cells = matrix.build_test_matrix(["svc"], extra_cells=extra)
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "svc")]


# ---------------------------------------------------------------------------
# CI spec conversion
# ---------------------------------------------------------------------------


class TestCiSpecs:
    def test_cell_to_ci_spec_default_ubuntu(self) -> None:
        assert matrix.cell_to_ci_spec(matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha")) == "alpha:box"

    def test_cell_to_ci_spec_non_default_ubuntu(self) -> None:
        assert matrix.cell_to_ci_spec(matrix.TestCell("box_deps", "resolute", "netdata")) == "netdata:box_deps:resolute"

    def test_cells_to_ci_specs_sorted_deduped(self) -> None:
        cells = [
            matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "beta"),
            matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha"),
            matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha"),
        ]
        assert matrix.cells_to_ci_specs(cells) == ["alpha:box", "beta:box"]

    def test_ci_spec_to_cell_two_parts(self) -> None:
        assert matrix.ci_spec_to_cell("alpha:box") == matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha")

    def test_ci_spec_to_cell_three_parts(self) -> None:
        assert matrix.ci_spec_to_cell("netdata:box_deps:resolute") == matrix.TestCell("box_deps", "resolute", "netdata")

    def test_ci_spec_to_cell_invalid(self) -> None:
        with pytest.raises(ValueError, match="Invalid CI spec"):
            matrix.ci_spec_to_cell("bad")

    def test_roundtrip(self) -> None:
        cell = matrix.TestCell("box_deps", matrix.DEFAULT_UBUNTU, "zfs")
        assert matrix.ci_spec_to_cell(matrix.cell_to_ci_spec(cell)) == cell


# ---------------------------------------------------------------------------
# _build_dispatch_matrix
# ---------------------------------------------------------------------------


class TestDispatchMatrix:
    def test_bare_role_expands(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha", {"machines": {"box": None, "minimal": None}, "ubuntu": ["resolute"]})
        cells = matrix._build_dispatch_matrix("alpha")
        assert matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha") in cells
        assert matrix.TestCell("minimal", matrix.DEFAULT_UBUNTU, "alpha") in cells

    def test_exact_spec_no_escalation(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha", {"machines": {"box": None, "minimal": None}, "ubuntu": ["resolute"]})
        cells = matrix._build_dispatch_matrix("alpha:box")
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha")]

    def test_unknown_role_exits(self, roles_tree: Path) -> None:
        with pytest.raises(SystemExit):
            matrix._build_dispatch_matrix("nonexistent")

    def test_comma_separated(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha")
        _make_role(roles_tree, "beta")
        cells = matrix._build_dispatch_matrix("alpha,beta")
        roles = {c.role for c in cells}
        assert roles == {"alpha", "beta"}

    def test_ignores_empty_tokens(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha")
        cells = matrix._build_dispatch_matrix("alpha,,")
        assert len(cells) == 1


# ---------------------------------------------------------------------------
# CLI (subprocess) — integration-level
# ---------------------------------------------------------------------------


class TestCli:
    def test_json_all(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha")
        _make_role(roles_tree, "beta", {"machines": {"box_deps": None}})
        result = subprocess.run(
            [sys.executable, "-m", "matrix", "--json", "--all"],
            capture_output=True,
            text=True,
            cwd=roles_tree,
            env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent / "test")},
            timeout=30,
        )
        assert result.returncode == 0
        specs = json.loads(result.stdout)
        assert "alpha:box" in specs
        assert "beta:box_deps" in specs

    def test_json_dispatch(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha")
        result = subprocess.run(
            [sys.executable, "-m", "matrix", "--json", "--dispatch", "alpha"],
            capture_output=True,
            text=True,
            cwd=roles_tree,
            env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent / "test")},
            timeout=30,
        )
        assert result.returncode == 0
        specs = json.loads(result.stdout)
        assert specs == ["alpha:box"]

    def test_json_empty(self, roles_tree: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "matrix", "--json"],
            capture_output=True,
            text=True,
            cwd=roles_tree,
            env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent / "test")},
            timeout=30,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == []

    def test_json_extra_with_roles(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "matrix",
                "--json",
                "--extra",
                "alpha:box:resolute",
                "--",
                "alpha",
            ],
            capture_output=True,
            text=True,
            cwd=roles_tree,
            env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent / "test")},
            timeout=30,
        )
        assert result.returncode == 0
        specs = json.loads(result.stdout)
        assert "alpha:box" in specs
        assert "alpha:box:resolute" in specs

    def test_human_readable(self, roles_tree: Path) -> None:
        _make_role(roles_tree, "alpha")
        result = subprocess.run(
            [sys.executable, "-m", "matrix"],
            capture_output=True,
            text=True,
            cwd=roles_tree,
            env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent / "test")},
            timeout=30,
        )
        assert result.returncode == 0
        assert "box\tnoble\talpha" in result.stdout

    def test_dispatch_mutual_exclusion(self, roles_tree: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "matrix", "--json", "--dispatch", "x", "--all"],
            capture_output=True,
            text=True,
            cwd=roles_tree,
            env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent / "test")},
            timeout=30,
        )
        assert result.returncode != 0


class TestOnDemandMachines:
    def test_drops_lab_and_pug_keeps_others(self) -> None:
        specs = ["zfs:box", "zfs:lab", "zfs:pug", "swap:lab:noble", "nginx:box"]
        kept, dropped = matrix.drop_on_demand_cells(specs)
        assert kept == ["zfs:box", "nginx:box"]
        assert dropped == ["zfs:lab", "zfs:pug", "swap:lab:noble"]

    def test_no_on_demand_cells_is_noop(self) -> None:
        specs = ["nginx:box", "zfs:box:noble"]
        kept, dropped = matrix.drop_on_demand_cells(specs)
        assert kept == specs
        assert dropped == []

    def test_on_demand_machines_are_lab_and_pug(self) -> None:
        assert frozenset({"lab", "pug"}) == matrix.ON_DEMAND_MACHINES
