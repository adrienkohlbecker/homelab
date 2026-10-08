"""Unit tests for test/testrole.py — idempotence regex, argparse, constants."""

import asyncio
from collections.abc import Coroutine
from pathlib import Path

import pytest
import testrole

# ---------------------------------------------------------------------------
# _count_changed_tasks — ANSI-aware PLAY RECAP parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (["PLAY RECAP *****", "lab  : ok=5  changed=3  unreachable=0  failed=0"], 3),
        (["PLAY RECAP *****", "lab  : ok=10  changed=0  unreachable=0  failed=0"], 0),
        (["PLAY RECAP *****", "lab  : ok=5  \x1b[0;33mchanged=2\x1b[0m  unreachable=0  failed=0"], 2),
        (
            [
                "PLAY RECAP *****",
                "lab  : ok=5  changed=1  unreachable=0  failed=0",
                "lab  : ok=3  changed=4  unreachable=0  failed=0",
            ],
            5,
        ),
        (["TASK [debug]", "ok: [lab]", ""], 0),
        ([], 0),
        (
            [
                "PLAY RECAP *****",
                "lab  : ok=5  changed=1  unreachable=0  failed=0",
                "PLAY RECAP *****",
                "lab  : ok=3  changed=2  unreachable=0  failed=0",
            ],
            3,
        ),
        (
            [
                "\x1b[0;32mlab\x1b[0m  : \x1b[0;32mok=10\x1b[0m  "
                "\x1b[0;33mchanged=7\x1b[0m  unreachable=0  "
                "\x1b[0;31mfailed=0\x1b[0m",
            ],
            7,
        ),
    ],
)
def test_count_changed_tasks(stdout: list[str], expected: int) -> None:
    assert testrole._count_changed_tasks(stdout) == expected


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_ansible_args_still_forward(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["testrole.py", "nginx", "--tags", "homepage"])
        args, pass_args, _role_config = testrole.parse_args()
        assert args.role == "nginx"
        assert pass_args == ["--tags", "homepage"]


@pytest.mark.parametrize(("machine_name", "expected_memory"), [("lab", 6144), ("minimal", None)])
def test_main_passes_role_memory_to_machine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    machine_name: str,
    expected_memory: int | None,
) -> None:
    role = tmp_path / "roles" / "fixture_role"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text("---\n")
    (role / "meta").mkdir()
    (role / "meta" / "test.yml").write_text("machines:\n  lab:\n    memory_mb: 6144\n  minimal:\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["testrole.py", "fixture_role", "--machine", machine_name])
    monkeypatch.setattr(testrole, "imagedir_for_host", lambda: tmp_path)
    monkeypatch.setattr(testrole, "sweep_stale_workdirs", lambda _: None)

    seen: dict = {}

    class FakeMachine:
        output_file = tmp_path / "test.log"

        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        def run(self, coro: Coroutine[object, object, None], label: str) -> int:
            asyncio.run(coro)
            return 0

    async def fake_run_test(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(testrole, "Machine", FakeMachine)
    monkeypatch.setattr(testrole, "run_test", fake_run_test)

    assert testrole.main() == 0
    assert seen["machine"] == machine_name
    assert seen["memory_mb"] == expected_memory
