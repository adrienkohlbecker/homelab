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
    check_coverage,
    load_synthetic_outcomes,
    missing_outcomes,
    normalize_source_path,
    production_condition_paths,
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


def test_unknown_role_is_rejected_rather_than_scoping_to_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("- debug: {msg: example}\n  when: feature_enabled\n")

    assert production_condition_paths(["example"]) == [Path("roles/example/tasks/main.yml")]

    with pytest.raises(ValueError, match="no roles/<role>/tasks/.*: example_renamed"):
        production_condition_paths(["example", "example_renamed"])


def test_role_with_only_underscore_task_files_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "scaffold" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "_verify.yml").write_text("- debug: {msg: verify}\n")

    with pytest.raises(ValueError, match="scaffold"):
        production_condition_paths(["scaffold"])


def test_scope_filters_conditions_while_scenarios_stay_repository_wide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    for role, expression in (("scoped", "scoped_enabled"), ("other", "other_enabled")):
        tasks = tmp_path / "roles" / role / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "main.yml").write_text(f"- debug: {{msg: {role}}}\n  when: {expression}\n")
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - path: roles/other/tasks/main.yml
    expression: other_enabled
    cases:
      - outcome: true
        variables:
          other_enabled: true
"""
    )

    missing = check_coverage(["scoped"], [], scenario_path=scenarios)

    # Only the scoped role is expected, but the out-of-scope scenario was still
    # matched against live source -- a stale selector there fails from any cell.
    assert {condition.expression for condition in missing} == {"scoped_enabled"}

    scenarios.write_text(scenarios.read_text().replace("other_enabled", "renamed_away"))
    with pytest.raises(ValueError, match="does not match a current condition"):
        check_coverage(["scoped"], [], scenario_path=scenarios)


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


def test_synthetic_scenarios_evaluate_current_source_expression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- name: Example\n  debug:\n    msg: example\n  when: feature_enabled | default(false)\n"
    )
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - path: roles/example/tasks/main.yml
    expression: feature_enabled | default(false)
    cases:
      - outcome: false
        variables: {}
      - outcome: true
        variables:
          feature_enabled: true
"""
    )

    outcomes = load_synthetic_outcomes(scenarios)

    assert list(outcomes.values()) == [{False, True}]


def test_synthetic_scenario_rejects_stale_expression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("- debug: {msg: example}\n  when: current_expression\n")
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - path: roles/example/tasks/main.yml
    expression: old_expression
    cases:
      - outcome: true
        variables:
          old_expression: true
"""
    )

    with pytest.raises(ValueError, match="does not match a current condition"):
        load_synthetic_outcomes(scenarios)


def test_synthetic_scenario_rejects_wrong_expected_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("- debug: {msg: example}\n  when: feature_enabled\n")
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - path: roles/example/tasks/main.yml
    expression: feature_enabled
    cases:
      - outcome: false
        variables:
          feature_enabled: true
"""
    )

    with pytest.raises(ValueError, match="expected False but evaluated True"):
        load_synthetic_outcomes(scenarios)


def test_synthetic_scenario_requires_explicit_duplicate_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- debug: {msg: first}\n  when: feature_enabled\n- debug: {msg: second}\n  when: feature_enabled\n"
    )
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - path: roles/example/tasks/main.yml
    expression: feature_enabled
    cases:
      - outcome: true
        variables:
          feature_enabled: true
"""
    )

    with pytest.raises(ValueError, match="select one with line or set all: true"):
        load_synthetic_outcomes(scenarios)


def test_synthetic_scenario_can_select_duplicate_by_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- debug: {msg: first}\n  when: feature_enabled\n- debug: {msg: second}\n  when: feature_enabled\n"
    )
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - path: roles/example/tasks/main.yml
    line: 4
    expression: feature_enabled
    cases:
      - outcome: false
        variables:
          feature_enabled: false
"""
    )

    outcomes = load_synthetic_outcomes(scenarios)

    assert len(outcomes) == 1
    assert next(iter(outcomes)).line == 4
    assert list(outcomes.values()) == [{False}]
