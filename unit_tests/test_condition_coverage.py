"""Tests for Ansible ``when`` branch coverage collection."""

from __future__ import annotations

from pathlib import Path

import pytest
from ansible._internal._datatag._tags import Origin
from condition_coverage import (
    ConditionKey,
    ConditionOutcome,
    evaluated_outcomes,
    format_missing_outcomes,
    inventory_conditions,
    missing_outcomes,
    normalize_source_path,
)


def _condition(text: str, line: int) -> object:
    return Origin(path="/tmp/staged/roles/example/tasks/main.yml", line_num=line, col_num=9).tag(text)


def test_normalize_source_path_strips_staging_prefix() -> None:
    assert normalize_source_path("/tmp/staged/roles/example/tasks/main.yml") == "roles/example/tasks/main.yml"
    assert normalize_source_path("/tmp/staged/site.yml") == "site.yml"


def test_success_records_every_condition_true() -> None:
    first = _condition("first", 10)
    second = _condition("second", 11)

    assert evaluated_outcomes([first, second]) == [
        ConditionOutcome(ConditionKey("roles/example/tasks/main.yml", 10, 9, "first"), True),
        ConditionOutcome(ConditionKey("roles/example/tasks/main.yml", 11, 9, "second"), True),
    ]


def test_skip_records_prior_true_and_short_circuit_false() -> None:
    first = _condition("first", 10)
    second = _condition("second", 11)
    third = _condition("third", 12)

    assert evaluated_outcomes([first, second, third], "second") == [
        ConditionOutcome(ConditionKey("roles/example/tasks/main.yml", 10, 9, "first"), True),
        ConditionOutcome(ConditionKey("roles/example/tasks/main.yml", 11, 9, "second"), False),
    ]


def test_unknown_false_condition_is_rejected() -> None:
    with pytest.raises(ValueError, match="absent from task condition list"):
        evaluated_outcomes([_condition("first", 10)], "other")


def test_inventory_reads_scalar_and_list_conditions(tmp_path: Path) -> None:
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    source = tasks / "main.yml"
    source.write_text(
        """\
- name: Scalar
  debug:
    msg: scalar
  when: scalar_enabled
- name: List
  debug:
    msg: list
  when:
    - first_enabled
    - second_enabled
"""
    )

    conditions = inventory_conditions([source])

    assert {condition.expression for condition in conditions} == {"scalar_enabled", "first_enabled", "second_enabled"}


def test_missing_outcomes_requires_true_and_false() -> None:
    complete = ConditionKey("roles/example/tasks/main.yml", 10, 9, "complete")
    true_only = ConditionKey("roles/example/tasks/main.yml", 20, 9, "true_only")
    unseen = ConditionKey("roles/example/tasks/main.yml", 30, 9, "unseen")

    missing = missing_outcomes(
        {complete, true_only, unseen},
        {complete: {False, True}, true_only: {True}},
    )

    assert missing == {true_only: {False}, unseen: {False, True}}
    rendered = format_missing_outcomes(missing)
    assert "main.yml:20:9: missing false: true_only" in rendered
    assert "main.yml:30:9: missing false, true: unseen" in rendered
