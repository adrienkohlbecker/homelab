"""Record Boolean outcomes for Ansible ``when`` expressions during tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ansible.plugins.callback import CallbackBase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "test"))

from condition_coverage import append_outcomes, evaluated_outcomes


class CallbackModule(CallbackBase):
    """Write condition outcomes when the harness supplies an output path."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "condition_coverage"
    CALLBACK_NEEDS_ENABLED = True

    def _record(self, result, *, skipped: bool = False) -> None:
        output = os.environ.get("ANSIBLE_CONDITION_COVERAGE_FILE")
        if not output:
            return

        task = result.task
        if task.loop or task.loop_with:
            return
        conditions = list(task.when)
        if not conditions:
            return

        false_condition = result.result.get("false_condition") if skipped else None
        if skipped and false_condition is None:
            return

        try:
            append_outcomes(
                Path(output),
                evaluated_outcomes(conditions, false_condition),
                phase=os.environ.get("ANSIBLE_CONDITION_COVERAGE_PHASE", "unknown"),
            )
        except Exception as exc:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            with Path(output).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"error": str(exc)}, sort_keys=True, separators=(",", ":")))
                handle.write("\n")

    def _record_item(self, result, *, skipped: bool = False) -> None:
        task = result.task
        loop = task.loop
        loop_with = task.loop_with
        task.loop = None
        task.loop_with = None
        try:
            self._record(result, skipped=skipped)
        finally:
            task.loop = loop
            task.loop_with = loop_with

    def v2_runner_on_ok(self, result) -> None:
        self._record(result)

    def v2_runner_on_failed(self, result, ignore_errors=False) -> None:
        self._record(result)

    def v2_runner_on_unreachable(self, result) -> None:
        self._record(result)

    def v2_runner_on_skipped(self, result) -> None:
        self._record(result, skipped=True)

    def v2_runner_item_on_ok(self, result) -> None:
        self._record_item(result)

    def v2_runner_item_on_failed(self, result) -> None:
        self._record_item(result)

    def v2_runner_item_on_skipped(self, result) -> None:
        self._record_item(result, skipped=True)
