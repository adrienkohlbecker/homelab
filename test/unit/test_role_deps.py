"""Unit tests for mise-tasks/ci/role-deps.py — inverse-dependency lookup."""

import importlib
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# role-deps.py lives outside the pythonpath; import it by path.
_ROLE_DEPS_PATH = Path(__file__).resolve().parent.parent.parent / "mise-tasks" / "ci" / "role-deps.py"


def _load_role_deps():
    spec = importlib.util.spec_from_file_location("role_deps", _ROLE_DEPS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


role_deps = _load_role_deps()


# ---------------------------------------------------------------------------
# walk()
# ---------------------------------------------------------------------------


class TestWalk:
    def test_import_role_detected(self) -> None:
        tasks = [{"import_role": {"name": "nginx"}}]
        inv: dict[str, set[str]] = defaultdict(set)
        role_deps.walk(tasks, "pihole", inv)
        assert inv == {"nginx": {"pihole"}}

    def test_include_role_detected(self) -> None:
        tasks = [{"include_role": {"name": "systemd_unit"}}]
        inv: dict[str, set[str]] = defaultdict(set)
        role_deps.walk(tasks, "netdata", inv)
        assert inv == {"systemd_unit": {"netdata"}}

    def test_block_nesting(self) -> None:
        tasks = [
            {
                "block": [{"import_role": {"name": "nginx"}}],
                "rescue": [{"import_role": {"name": "service_user"}}],
                "always": [{"include_role": {"name": "systemd_unit"}}],
            }
        ]
        inv: dict[str, set[str]] = defaultdict(set)
        role_deps.walk(tasks, "homepage", inv)
        assert inv["nginx"] == {"homepage"}
        assert inv["service_user"] == {"homepage"}
        assert inv["systemd_unit"] == {"homepage"}

    def test_deeply_nested_block(self) -> None:
        tasks = [{"block": [{"block": [{"import_role": {"name": "deep"}}]}]}]
        inv: dict[str, set[str]] = defaultdict(set)
        role_deps.walk(tasks, "outer", inv)
        assert inv == {"deep": {"outer"}}

    def test_non_dict_tasks_skipped(self) -> None:
        tasks = ["just a string", 42, None, {"import_role": {"name": "real"}}]
        inv: dict[str, set[str]] = defaultdict(set)
        role_deps.walk(tasks, "consumer", inv)
        assert inv == {"real": {"consumer"}}

    def test_non_list_input_skipped(self) -> None:
        inv: dict[str, set[str]] = defaultdict(set)
        role_deps.walk("not a list", "x", inv)
        role_deps.walk(None, "x", inv)
        role_deps.walk(42, "x", inv)
        assert inv == {}

    def test_import_role_without_name_skipped(self) -> None:
        tasks = [{"import_role": {"tasks_from": "install"}}]
        inv: dict[str, set[str]] = defaultdict(set)
        role_deps.walk(tasks, "consumer", inv)
        assert inv == {}

    def test_multiple_consumers(self) -> None:
        inv: dict[str, set[str]] = defaultdict(set)
        role_deps.walk([{"import_role": {"name": "nginx"}}], "pihole", inv)
        role_deps.walk([{"import_role": {"name": "nginx"}}], "netdata", inv)
        role_deps.walk([{"import_role": {"name": "nginx"}}], "homepage", inv)
        assert inv["nginx"] == {"pihole", "netdata", "homepage"}

    def test_role_importing_multiple_helpers(self) -> None:
        tasks = [
            {"import_role": {"name": "service_user"}},
            {"import_role": {"name": "systemd_unit"}},
            {"import_role": {"name": "nginx"}},
        ]
        inv: dict[str, set[str]] = defaultdict(set)
        role_deps.walk(tasks, "speedtest", inv)
        assert "speedtest" in inv["service_user"]
        assert "speedtest" in inv["systemd_unit"]
        assert "speedtest" in inv["nginx"]


# ---------------------------------------------------------------------------
# CLI integration (against a scaffolded roles tree)
# ---------------------------------------------------------------------------


class TestCli:
    def test_finds_consumers(self, tmp_path: Path) -> None:
        roles = tmp_path / "roles"
        for name, content in [
            ("nginx", "---\n"),
            ("pihole", "- import_role:\n    name: nginx\n"),
            ("netdata", "- import_role:\n    name: nginx\n"),
            ("standalone", "- command: echo hi\n"),
        ]:
            task_dir = roles / name / "tasks"
            task_dir.mkdir(parents=True)
            (task_dir / "main.yml").write_text(content)

        result = subprocess.run(
            [sys.executable, str(_ROLE_DEPS_PATH), "nginx"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert result.returncode == 0
        consumers = result.stdout.strip().splitlines()
        assert sorted(consumers) == ["netdata", "pihole"]

    def test_no_consumers_empty_output(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "roles" / "leaf" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "main.yml").write_text("- command: echo hi\n")

        result = subprocess.run(
            [sys.executable, str(_ROLE_DEPS_PATH), "leaf"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_usage_on_missing_arg(self) -> None:
        result = subprocess.run(
            [sys.executable, str(_ROLE_DEPS_PATH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2

    def test_block_nesting_via_cli(self, tmp_path: Path) -> None:
        roles = tmp_path / "roles"
        task_dir = roles / "homepage" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "main.yml").write_text("- block:\n  - import_role:\n      name: nginx\n")
        task_dir2 = roles / "nginx" / "tasks"
        task_dir2.mkdir(parents=True)
        (task_dir2 / "main.yml").write_text("---\n")

        result = subprocess.run(
            [sys.executable, str(_ROLE_DEPS_PATH), "nginx"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "homepage"
