"""Record Ansible condition outcomes and loop iterations during tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ansible.plugins.callback import CallbackBase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "test"))

from condition_coverage import (
    LoopKey,
    TaskKey,
    append_loop_executions,
    append_outcomes,
    append_task_executions,
    evaluated_outcomes,
    loop_key,
    task_key_from_path,
)


class CallbackModule(CallbackBase):
    """Write condition and loop coverage when the harness supplies an output path."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "condition_coverage"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self) -> None:
        super().__init__()
        self._recorded_loops: set[tuple[Path, LoopKey]] = set()
        self._recorded_tasks: set[tuple[Path, TaskKey]] = set()

    def _record(self, result, *, skipped: bool = False, for_item: bool = False, unreachable: bool = False) -> None:
        output = os.environ.get("ANSIBLE_CONDITION_COVERAGE_FILE")
        if not output:
            return

        task = result.task
        try:
            path = Path(output)
            phase = os.environ.get("ANSIBLE_CONDITION_COVERAGE_PHASE", "unknown")
            if not skipped and not unreachable:
                task_key = task_key_from_path(task.get_path())
                task_marker = (path, task_key)
                if task_marker not in self._recorded_tasks:
                    append_task_executions(path, [task_key], phase=phase)
                    self._recorded_tasks.add(task_marker)
            if for_item and (task.loop or task.loop_with):
                loop = loop_key(task.loop)
                marker = (path, loop)
                if marker not in self._recorded_loops:
                    append_loop_executions(path, [loop], phase=phase)
                    self._recorded_loops.add(marker)

            # A looped task evaluates its `when` once per item, so the per-item
            # callbacks carry the outcomes and the aggregate would double-count
            # them. An empty loop emits no item callback, so its declaration is
            # left unrecorded and fails the loop-coverage gate.
            if not for_item and (task.loop or task.loop_with):
                return
            conditions = list(task.when)
            if not conditions:
                return

            false_condition = result.result.get("false_condition") if skipped else None
            if skipped and false_condition is None:
                return
            append_outcomes(
                path,
                evaluated_outcomes(conditions, false_condition),
                phase=phase,
            )
        except Exception as exc:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            with Path(output).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"error": str(exc)}, sort_keys=True, separators=(",", ":")))
                handle.write("\n")

    def v2_runner_on_ok(self, result) -> None:
        self._record(result)

    def v2_runner_on_failed(self, result, ignore_errors=False) -> None:
        self._record(result)

    def v2_runner_on_unreachable(self, result) -> None:
        self._record(result, unreachable=True)

    def v2_runner_on_skipped(self, result) -> None:
        self._record(result, skipped=True)

    def v2_runner_item_on_ok(self, result) -> None:
        self._record(result, for_item=True)

    def v2_runner_item_on_failed(self, result) -> None:
        self._record(result, for_item=True)

    def v2_runner_item_on_skipped(self, result) -> None:
        self._record(result, skipped=True, for_item=True)
