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

    def _record(self, result, *, skipped: bool = False, for_item: bool = False) -> None:
        output = os.environ.get("ANSIBLE_CONDITION_COVERAGE_FILE")
        if not output:
            return

        task = result.task
        # A looped task evaluates its `when` once per item, so the per-item
        # callbacks carry the outcomes and the aggregate would double-count
        # them. The cost is that an empty loop emits no item callback at all and
        # records nothing -- which fails closed, since a condition with no
        # observed outcome is reported as missing both.
        if not for_item and (task.loop or task.loop_with):
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

    def v2_runner_on_ok(self, result) -> None:
        self._record(result)

    def v2_runner_on_failed(self, result, ignore_errors=False) -> None:
        self._record(result)

    def v2_runner_on_unreachable(self, result) -> None:
        self._record(result)

    def v2_runner_on_skipped(self, result) -> None:
        self._record(result, skipped=True)

    def v2_runner_item_on_ok(self, result) -> None:
        self._record(result, for_item=True)

    def v2_runner_item_on_failed(self, result) -> None:
        self._record(result, for_item=True)

    def v2_runner_item_on_skipped(self, result) -> None:
        self._record(result, skipped=True, for_item=True)
