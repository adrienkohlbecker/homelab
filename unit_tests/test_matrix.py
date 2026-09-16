"""Unit tests for test/matrix.py — test matrix generation."""

from pathlib import Path

import machine
import matrix
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_META_ROLES = [path.parent.parent.name for path in sorted((_REPO_ROOT / "roles").glob("*/meta/test.yml"))]


@pytest.fixture(autouse=True)
def isolated_roles_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def _make_role(name: str, meta: dict | None = None) -> None:
    tasks = Path("roles") / name / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "main.yml").write_text("---\n")
    if meta is not None:
        meta_dir = Path("roles") / name / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        import yaml

        (meta_dir / "test.yml").write_text(yaml.dump(meta))


# ---------------------------------------------------------------------------
# list_testable_roles
# ---------------------------------------------------------------------------


class TestListTestableRoles:
    def test_discovers_roles_with_main_yml(self) -> None:
        _make_role("alpha")
        _make_role("beta")
        Path("roles/gamma").mkdir(parents=True)
        assert matrix.list_testable_roles() == ["alpha", "beta"]

    def test_empty_when_no_roles_dir(self) -> None:
        assert matrix.list_testable_roles() == []

    def test_sorted_output(self) -> None:
        for name in ["zeta", "alpha", "mu"]:
            _make_role(name)
        assert matrix.list_testable_roles() == ["alpha", "mu", "zeta"]


# ---------------------------------------------------------------------------
# RoleTestConfig access
# ---------------------------------------------------------------------------


class TestRoleMeta:
    @pytest.mark.parametrize("role", _REPOSITORY_META_ROLES)
    def test_repository_metadata_is_valid(self, role: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(_REPO_ROOT)
        matrix.load_role_test_config(role, tuple(sorted(machine.MACHINE_CHOICES)))

    def test_default_machine_falls_back_to_box(self) -> None:
        _make_role("plain")
        assert next(iter(matrix.load_role_test_config("plain").machines)) == "box"

    def test_default_machine_reads_meta(self) -> None:
        _make_role("fancy", {"machines": {"box_deps": None}})
        assert next(iter(matrix.load_role_test_config("fancy").machines)) == "box_deps"

    def test_machine_payload_is_rejected(self) -> None:
        _make_role("configured", {"machines": {"box": {"memory_mb": 8192}}})
        with pytest.raises(matrix.RoleTestConfigError, match=r"machines\.box must be empty"):
            matrix.load_role_test_config("configured")

    def test_base_prerequisites_defaults_to_true(self) -> None:
        _make_role("plain")
        assert matrix.load_role_test_config("plain").base_prerequisites is True

    def test_base_prerequisites_reads_false(self) -> None:
        _make_role("foundation", {"base_prerequisites": False})
        assert matrix.load_role_test_config("foundation").base_prerequisites is False

    def test_base_prerequisites_must_be_boolean(self) -> None:
        _make_role("foundation", {"base_prerequisites": "pristine"})
        with pytest.raises(matrix.RoleTestConfigError, match="base_prerequisites must be a boolean"):
            matrix.load_role_test_config("foundation")


# ---------------------------------------------------------------------------
# build_role_cells
# ---------------------------------------------------------------------------


class TestBuildRoleCells:
    def test_plain_role_one_cell(self) -> None:
        _make_role("plain")
        cells = matrix.build_role_cells("plain")
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "plain")]

    def test_box_deps_role(self) -> None:
        _make_role("svc", {"machines": {"box_deps": None}})
        cells = matrix.build_role_cells("svc")
        assert cells == [matrix.TestCell("box_deps", matrix.DEFAULT_UBUNTU, "svc")]

    def test_multi_machine_plus_release(self) -> None:
        _make_role("podman", {"machines": {"box": None, "minimal": None}, "ubuntu": ["resolute"]})
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
    def test_deduplicates(self) -> None:
        _make_role("alpha")
        cells = matrix.build_test_matrix(["alpha", "alpha"])
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha")]

    def test_sorted_by_all_fields(self) -> None:
        _make_role("beta")
        _make_role("alpha", {"ubuntu": ["resolute"]})
        cells = matrix.build_test_matrix(["beta", "alpha"])
        assert cells == sorted(cells)

    def test_extra_cells_merged(self) -> None:
        _make_role("alpha")
        extra = [matrix.TestCell("box", "resolute", "alpha")]
        cells = matrix.build_test_matrix(["alpha"], extra_cells=extra)
        assert matrix.TestCell("box", "resolute", "alpha") in cells
        assert matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha") in cells

    def test_empty_roles_with_extra(self) -> None:
        cells = matrix.build_test_matrix([], extra_cells=[matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "foo")])
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "foo")]


# ---------------------------------------------------------------------------
# skip:
# ---------------------------------------------------------------------------


class TestSkip:
    def test_config_normalizes_machine_and_release_skips(self) -> None:
        _make_role("svc", {"skip": {"minimal": "why", "box_deps:resolute": "why"}})
        assert matrix.load_role_test_config("svc").skip == {
            ("minimal", matrix.DEFAULT_UBUNTU),
            ("box_deps", "resolute"),
        }

    def test_config_skip_empty_when_absent(self) -> None:
        _make_role("svc")
        assert matrix.load_role_test_config("svc").skip == frozenset()

    def test_build_role_cells_drops_skipped_release_cell_only(self) -> None:
        _make_role(
            "svc",
            {"machines": {"box": None}, "ubuntu": ["resolute"], "skip": {"box:resolute": "flaky"}},
        )
        cells = matrix.build_role_cells("svc")
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "svc")]

    def test_bare_machine_skip_drops_only_that_machines_base_cell(self) -> None:
        # The bare form is the only correct spelling for the default cell.
        _make_role("svc", {"machines": {"box": None, "minimal": None}, "skip": {"minimal": "flaky"}})
        assert matrix.build_role_cells("svc") == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "svc")]

    def test_explicit_default_release_skip_is_rejected(self) -> None:
        _make_role(
            "svc",
            {"machines": {"box": None, "minimal": None}, "skip": {"minimal:noble": "flaky"}},
        )
        with pytest.raises(matrix.RoleTestConfigError, match="cancels the base cell"):
            matrix.load_role_test_config("svc")

    def test_listing_the_default_release_is_rejected(self) -> None:
        _make_role("svc", {"machines": {"box": None}, "ubuntu": [matrix.DEFAULT_UBUNTU]})
        with pytest.raises(matrix.RoleTestConfigError, match="the default release"):
            matrix.build_role_cells("svc")

    def test_build_test_matrix_drops_skipped_propagated_extra(self) -> None:
        # A consumer's release cell pushed in via CI fan-out must still drop
        # if that consumer skips it.
        _make_role("svc", {"machines": {"box": None}, "skip": {"box:resolute": "flaky"}})
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
# build_dispatch_matrix
# ---------------------------------------------------------------------------


class TestDispatchMatrix:
    def test_bare_role_expands(self) -> None:
        _make_role("alpha", {"machines": {"box": None, "minimal": None}, "ubuntu": ["resolute"]})
        cells = matrix.build_dispatch_matrix("alpha")
        assert matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha") in cells
        assert matrix.TestCell("minimal", matrix.DEFAULT_UBUNTU, "alpha") in cells

    def test_exact_spec_no_escalation(self) -> None:
        _make_role("alpha", {"machines": {"box": None, "minimal": None}, "ubuntu": ["resolute"]})
        cells = matrix.build_dispatch_matrix("alpha:box")
        assert cells == [matrix.TestCell("box", matrix.DEFAULT_UBUNTU, "alpha")]

    def test_unknown_role_exits(self) -> None:
        with pytest.raises(SystemExit):
            matrix.build_dispatch_matrix("nonexistent")

    def test_comma_separated(self) -> None:
        _make_role("alpha")
        _make_role("beta")
        cells = matrix.build_dispatch_matrix("alpha,beta")
        roles = {c.role for c in cells}
        assert roles == {"alpha", "beta"}

    def test_ignores_empty_tokens(self) -> None:
        _make_role("alpha")
        cells = matrix.build_dispatch_matrix("alpha,,")
        assert len(cells) == 1
