"""Tests for Ansible condition and loop coverage collection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from ansible._internal._datatag._tags import Origin
from condition_coverage import (
    COVERAGE_SCHEMA_VERSION,
    SYNTHETIC_SCENARIOS_PATH,
    BlockDefinition,
    BlockEvent,
    BlockGap,
    BlockKey,
    ConditionKey,
    ConditionOutcome,
    CoverageProvenance,
    ExitGap,
    ExitKey,
    ExitOutcome,
    IncludeDefinition,
    IncludeEvent,
    IncludeKey,
    LoopKey,
    ResultPredicateKey,
    ResultPredicateOutcome,
    TaskDefinition,
    TaskKey,
    UntilKey,
    UntilOutcome,
    append_block_events,
    append_exit_outcomes,
    append_include_events,
    append_loop_executions,
    append_outcomes,
    append_report_provenance,
    append_report_rows,
    append_result_predicate_outcomes,
    append_task_executions,
    append_until_outcomes,
    check_block_coverage,
    check_coverage,
    check_exit_coverage,
    check_include_coverage,
    check_loop_coverage,
    check_result_predicate_coverage,
    check_task_coverage,
    check_until_coverage,
    evaluated_outcomes,
    format_block_gaps,
    format_exit_gaps,
    format_missing_outcomes,
    format_missing_result_predicate_outcomes,
    format_missing_until_outcomes,
    format_unexecuted_loops,
    format_unexecuted_tasks,
    format_unexpanded_includes,
    inventory_blocks,
    inventory_conditions,
    inventory_exits,
    inventory_includes,
    inventory_loops,
    inventory_result_predicates,
    inventory_tasks,
    inventory_untils,
    load_block_events,
    load_executed_loops,
    load_executed_tasks,
    load_exit_outcomes,
    load_include_events,
    load_outcomes,
    load_result_predicate_outcomes,
    load_synthetic_outcomes,
    load_synthetic_result_predicate_outcomes,
    load_synthetic_task_reachability,
    load_synthetic_until_outcomes,
    load_until_outcomes,
    missing_outcomes,
    normalize_source_path,
    production_condition_paths,
    repository_source_sha,
)

import callback_plugins.condition_coverage as condition_coverage_callback

_SOURCE_SHA = repository_source_sha()


def _start_report(path: Path, *, architecture: str = "x86_64", source_sha: str = _SOURCE_SHA) -> None:
    append_report_provenance(
        path,
        CoverageProvenance(
            schema=COVERAGE_SCHEMA_VERSION,
            source_sha=source_sha,
            architecture=architecture,
        ),
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


def test_reports_merge_matching_source_across_architectures(tmp_path: Path) -> None:
    condition = ConditionKey("roles/example/tasks/main.yml", 10, 9, "enabled")
    x86_report = tmp_path / "x86_64.jsonl"
    arm_report = tmp_path / "aarch64.jsonl"
    _start_report(x86_report, architecture="x86_64")
    _start_report(arm_report, architecture="aarch64")
    append_outcomes(x86_report, [ConditionOutcome(condition, False)], phase="converge")
    append_outcomes(arm_report, [ConditionOutcome(condition, True)], phase="converge")

    assert load_outcomes([x86_report, arm_report]) == {condition: {False, True}}


def test_reports_reject_mixed_source_shas(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _start_report(first)
    _start_report(second, source_sha="2" * 40)

    with pytest.raises(ValueError, match="mixed coverage source SHAs"):
        load_outcomes([first, second])


def test_reports_reject_source_sha_from_another_checkout(tmp_path: Path) -> None:
    report = tmp_path / "stale.jsonl"
    _start_report(report, source_sha="2" * 40)

    with pytest.raises(ValueError, match="does not match current checkout"):
        load_outcomes([report])


def test_reports_reject_mixed_schemas(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _start_report(first)
    append_report_provenance(
        second,
        CoverageProvenance(
            schema=COVERAGE_SCHEMA_VERSION + 1,
            source_sha=_SOURCE_SHA,
            architecture="aarch64",
        ),
    )

    with pytest.raises(ValueError, match="mixed coverage schemas"):
        load_outcomes([first, second])


def test_reports_reject_legacy_unprovenanced_records(tmp_path: Path) -> None:
    report = tmp_path / "legacy.jsonl"
    append_outcomes(
        report,
        [ConditionOutcome(ConditionKey("roles/example/tasks/main.yml", 10, 9, "enabled"), True)],
        phase="converge",
    )

    with pytest.raises(ValueError, match="unprovenanced coverage record"):
        load_outcomes([report])


def test_report_rows_allow_concurrent_process_writers(tmp_path: Path) -> None:
    report = tmp_path / "concurrent.jsonl"
    children: list[int] = []
    for worker in range(4):
        pid = os.fork()
        if pid == 0:
            append_report_rows(report, ({"worker": worker, "event": event} for event in range(100)))
            os._exit(0)
        children.append(pid)

    assert all(os.waitpid(pid, 0)[1] == 0 for pid in children)
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    assert len(rows) == 400
    assert {(row["worker"], row["event"]) for row in rows} == {
        (worker, event) for worker in range(4) for event in range(100)
    }


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

    assert {condition.expression for condition in conditions} == {
        "scalar_enabled",
        "first_enabled",
        "second_enabled",
    }


def test_inventory_reads_modern_and_legacy_loops(tmp_path: Path) -> None:
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
- debug: {msg: modern}
  loop: "{{ modern_items }}"
- debug: {msg: legacy}
  with_items:
    - first
    - second
"""
    )

    loops = inventory_loops([source])

    assert {loop.expression for loop in loops} == {
        "{{ modern_items }}",
        "['first', 'second']",
    }


def test_loop_key_normalizes_ansible_runtime_list_wrapper() -> None:
    expression = Origin(
        path="/tmp/staged/roles/example/tasks/main.yml",
        line_num=24,
        col_num=9,
    ).tag("{{ example_items }}")

    assert condition_coverage_callback.loop_key([expression]) == LoopKey(
        "roles/example/tasks/main.yml", 24, 9, "{{ example_items }}"
    )


def test_inventory_reads_executable_tasks_but_not_structural_actions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
- name: Plain
  debug: {msg: plain}
- name: Dynamic action
  action: "{{ module_name }}"
  args: {path: /tmp/example}
- name: Static import
  import_tasks: imported.yml
- name: Dynamic include
  include_tasks: included.yml
- name: Block container
  block:
    - name: Primary
      command: /bin/true
  rescue:
    - name: Recovery
      fail: {msg: recovered}
  always:
    - name: Cleanup
      file: {path: /tmp/example, state: absent}
- name: Meta action
  meta: end_role
"""
    )

    tasks = inventory_tasks([source])

    assert {(task.key.line, task.action, task.name) for task in tasks.values()} == {
        (1, "debug", "Plain"),
        (3, "{{ module_name }}", "Dynamic action"),
        (12, "command", "Primary"),
        (15, "fail", "Recovery"),
        (18, "file", "Cleanup"),
    }


def test_block_coverage_requires_normal_rescue_and_always_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
- name: Protected operation
  block:
    - name: Primary
      command: /bin/true
  rescue:
    - name: Recover
      debug: {msg: recovered}
  always:
    - name: Cleanup
      debug: {msg: cleanup}
"""
    )
    report = tmp_path / "coverage.jsonl"
    definition = BlockDefinition(
        BlockKey("roles/example/tasks/main.yml", 1),
        "Protected operation",
        TaskKey("roles/example/tasks/main.yml", 3),
        True,
        True,
    )

    assert inventory_blocks([source]) == {definition.key: definition}
    assert check_block_coverage(["example"], []) == {
        BlockGap(definition, "normal"),
        BlockGap(definition, "rescue"),
        BlockGap(definition, "always_after_normal"),
        BlockGap(definition, "always_after_rescued"),
    }

    _start_report(report)
    append_block_events(
        report,
        [
            BlockEvent(definition.key, definition.normal_terminal, "block", "ok"),
            BlockEvent(definition.key, TaskKey(definition.key.path, 6), "rescue", "ok"),
            BlockEvent(
                definition.key,
                TaskKey(definition.key.path, 9),
                "always",
                "ok",
                "normal",
            ),
            BlockEvent(
                definition.key,
                TaskKey(definition.key.path, 9),
                "always",
                "ok",
                "rescued",
            ),
        ],
        phase="verify",
    )

    assert len(load_block_events([report])) == 4
    assert check_block_coverage(["example"], [report]) == set()
    assert "missing rescue: Protected operation" in format_block_gaps({BlockGap(definition, "rescue")})


def test_rescue_only_block_uses_terminal_primary_for_normal_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
- name: Protected operation
  block:
    - name: Primary
      command: /bin/true
  rescue:
    - name: Recover
      debug: {msg: recovered}
"""
    )
    report = tmp_path / "coverage.jsonl"
    definition = next(iter(inventory_blocks([source]).values()))
    _start_report(report)
    append_block_events(
        report,
        [
            BlockEvent(definition.key, definition.normal_terminal, "block", "ok"),
            BlockEvent(definition.key, TaskKey(definition.key.path, 6), "rescue", "ok"),
        ],
        phase="verify",
    )

    assert check_block_coverage(["example"], [report]) == set()


def test_until_coverage_requires_retry_and_success_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
- name: Retry operation
  command: /bin/true
  register: retry_result
  until: retry_result is succeeded
  retries: 2
"""
    )
    report = tmp_path / "coverage.jsonl"
    until = UntilKey("roles/example/tasks/main.yml", 4, 10, "retry_result is succeeded")

    assert inventory_untils([source]) == {until}
    assert check_until_coverage(["example"], []) == {until: {False, True}}
    assert "missing false, true: retry_result is succeeded" in format_missing_until_outcomes({until: {False, True}})

    _start_report(report)
    append_until_outcomes(
        report,
        [UntilOutcome(until, False), UntilOutcome(until, True)],
        phase="converge",
    )

    assert load_until_outcomes([report]) == {until: {False, True}}
    assert check_until_coverage(["example"], [report]) == {}


def test_until_key_normalizes_ansible_runtime_list_wrapper() -> None:
    expression = Origin(
        path="/tmp/staged/roles/example/tasks/main.yml",
        line_num=24,
        col_num=10,
    ).tag("retry_result is succeeded")

    assert condition_coverage_callback.until_key([expression]) == UntilKey(
        "roles/example/tasks/main.yml", 24, 10, "retry_result is succeeded"
    )


def test_synthetic_until_scenario_evaluates_current_expression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- command: /bin/true\n  register: retry_result\n  until: retry_result.rc == 0\n  retries: 2\n"
    )
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - kind: until
    path: roles/example/tasks/main.yml
    expression: retry_result.rc == 0
    cases:
      - outcome: false
        variables:
          retry_result: {rc: 1}
      - outcome: true
        variables:
          retry_result: {rc: 0}
"""
    )

    outcomes = load_synthetic_until_outcomes(scenarios)

    assert list(outcomes.values()) == [{False, True}]
    assert check_until_coverage(["example"], [], scenario_path=scenarios) == {}
    assert load_synthetic_outcomes(scenarios) == {}
    assert load_synthetic_result_predicate_outcomes(scenarios) == {}


def test_inventory_reads_only_dynamic_result_predicates(tmp_path: Path) -> None:
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
- command: /bin/true
  changed_when: dynamic_change
  failed_when:
    - result.rc != 0
    - force_failure
- command: /bin/true
  changed_when: false
  failed_when: false
"""
    )

    predicates = inventory_result_predicates([source])

    assert {(predicate.kind, predicate.expressions) for predicate in predicates} == {
        ("changed_when", ("dynamic_change",)),
        ("failed_when", ("result.rc != 0", "force_failure")),
    }


def test_result_predicate_coverage_requires_false_and_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text("- command: /bin/true\n  changed_when: dynamic_change\n")
    report = tmp_path / "coverage.jsonl"
    predicate = next(iter(inventory_result_predicates([source])))

    assert check_result_predicate_coverage(["example"], [], scenario_path=None) == {predicate: {False, True}}
    rendered = format_missing_result_predicate_outcomes({predicate: {False, True}})
    assert "changed_when missing false, true: dynamic_change" in rendered

    _start_report(report)
    append_result_predicate_outcomes(
        report,
        [
            ResultPredicateOutcome(predicate, False),
            ResultPredicateOutcome(predicate, True),
        ],
        phase="converge",
    )

    assert load_result_predicate_outcomes([report]) == {predicate: {False, True}}
    assert check_result_predicate_coverage(["example"], [report], scenario_path=None) == {}


def test_callback_records_loop_result_predicates_per_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "coverage.jsonl"
    monkeypatch.setenv("ANSIBLE_CONDITION_COVERAGE_FILE", str(report))
    predicate = Origin(
        path="/tmp/staged/roles/example/tasks/main.yml",
        line_num=24,
        col_num=17,
    ).tag("dynamic_change")
    loop = Origin(
        path="/tmp/staged/roles/example/tasks/main.yml",
        line_num=25,
        col_num=9,
    ).tag("{{ example_items }}")
    task = SimpleNamespace(
        _parent=None,
        loop=loop,
        loop_with=None,
        until=None,
        when=[],
        changed_when=[predicate],
        failed_when=[],
        get_path=lambda: "/tmp/staged/roles/example/tasks/main.yml:20",
    )
    callback = condition_coverage_callback.CallbackModule()

    callback.v2_runner_on_ok(cast(Any, SimpleNamespace(task=task, result={"changed": True})))
    callback.v2_runner_item_on_ok(cast(Any, SimpleNamespace(task=task, result={"changed": False})))
    callback.v2_runner_item_on_ok(cast(Any, SimpleNamespace(task=task, result={"changed": True})))

    key = ResultPredicateKey(
        "changed_when",
        "roles/example/tasks/main.yml",
        24,
        17,
        ("dynamic_change",),
    )
    assert load_result_predicate_outcomes([report]) == {key: {False, True}}


def test_callback_records_failed_when_from_terminal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "coverage.jsonl"
    monkeypatch.setenv("ANSIBLE_CONDITION_COVERAGE_FILE", str(report))
    predicate = Origin(
        path="/tmp/staged/roles/example/tasks/main.yml",
        line_num=24,
        col_num=16,
    ).tag("force_failure")
    task = SimpleNamespace(
        _parent=None,
        loop=None,
        loop_with=None,
        until=None,
        when=[],
        changed_when=[],
        failed_when=[predicate],
        get_path=lambda: "/tmp/staged/roles/example/tasks/main.yml:20",
    )
    callback = condition_coverage_callback.CallbackModule()

    callback.v2_runner_on_ok(cast(Any, SimpleNamespace(task=task, result={})))
    callback.v2_runner_on_failed(cast(Any, SimpleNamespace(task=task, result={})))

    key = ResultPredicateKey(
        "failed_when",
        "roles/example/tasks/main.yml",
        24,
        16,
        ("force_failure",),
    )
    assert load_result_predicate_outcomes([report]) == {key: {False, True}}


def test_include_coverage_requires_expected_task_and_role_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
- name: Load task file
  ansible.builtin.include_tasks:
    file: included.yml
- name: Load helper role
  include_role:
    name: helper
    tasks_from: install
"""
    )
    report = tmp_path / "coverage.jsonl"
    task_include = IncludeDefinition(
        IncludeKey("roles/example/tasks/main.yml", 1, "include_tasks"),
        "Load task file",
        "roles/example/tasks/included.yml",
    )
    role_include = IncludeDefinition(
        IncludeKey("roles/example/tasks/main.yml", 4, "include_role"),
        "Load helper role",
        "roles/helper/tasks/install.yml",
    )

    assert set(inventory_includes([source]).values()) == {task_include, role_include}
    assert check_include_coverage(["example"], []) == {task_include, role_include}
    assert "Load task file -> roles/example/tasks/included.yml" in format_unexpanded_includes({task_include})

    _start_report(report)
    append_include_events(
        report,
        [
            IncludeEvent(task_include.key, task_include.target),
            IncludeEvent(role_include.key, role_include.target),
        ],
        phase="converge",
    )

    assert len(load_include_events([report])) == 2
    assert check_include_coverage(["example"], [report]) == set()


def test_early_exit_coverage_requires_exit_and_executed_fallthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    source = tasks / "main.yml"
    source.write_text(
        """\
- name: Protected sequence
  block:
    - name: End role conditionally
      meta: end_role
      when: take_exit
    - name: Continue through imported tasks
      import_tasks: apply.yml
"""
    )
    (tasks / "apply.yml").write_text("- name: Apply change\n  command: /bin/true\n")
    report = tmp_path / "coverage.jsonl"
    definition = next(iter(inventory_exits([source]).values()))

    assert definition.fallthrough == TaskKey("roles/example/tasks/apply.yml", 1)
    assert check_exit_coverage(["example"], []) == {
        ExitGap(definition, "exit"),
        ExitGap(definition, "fallthrough"),
    }
    assert "missing exit: End role conditionally" in format_exit_gaps({ExitGap(definition, "exit")})

    _start_report(report)
    append_exit_outcomes(
        report,
        [ExitOutcome(definition.key, False), ExitOutcome(definition.key, True)],
        phase="converge",
    )
    append_task_executions(report, [definition.fallthrough], phase="converge")

    assert load_exit_outcomes([report]) == {definition.key: {False, True}}
    assert check_exit_coverage(["example"], [report]) == set()


def test_callback_records_include_expansion_and_early_exit_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "coverage.jsonl"
    monkeypatch.setenv("ANSIBLE_CONDITION_COVERAGE_FILE", str(report))
    include_task = SimpleNamespace(
        action="ansible.builtin.include_tasks",
        get_path=lambda: "/tmp/staged/roles/example/tasks/main.yml:10",
    )
    included_file = SimpleNamespace(
        _task=include_task,
        _is_role=False,
        _filename="/tmp/staged/roles/example/tasks/included.yml",
    )
    exit_task = SimpleNamespace(
        action="ansible.builtin.meta",
        _parent=None,
        loop=None,
        loop_with=None,
        when=[],
        _get_meta=lambda: "end_role",
        get_path=lambda: "/tmp/staged/roles/example/tasks/main.yml:20",
    )
    ordinary_task = SimpleNamespace(action="debug", _get_meta=lambda: None)
    callback = condition_coverage_callback.CallbackModule()

    callback.v2_playbook_on_include(cast(Any, included_file))
    callback.v2_playbook_on_task_start(cast(Any, exit_task), False)
    callback.v2_runner_on_skipped(cast(Any, SimpleNamespace(task=exit_task, result={})))
    callback.v2_playbook_on_task_start(cast(Any, ordinary_task), False)
    callback.v2_playbook_on_task_start(cast(Any, exit_task), False)
    callback.v2_playbook_on_task_start(cast(Any, ordinary_task), False)

    include = IncludeKey("roles/example/tasks/main.yml", 10, "include_tasks")
    exit_key = ExitKey("roles/example/tasks/main.yml", 20)
    assert load_include_events([report]) == {IncludeEvent(include, "roles/example/tasks/included.yml")}
    assert load_exit_outcomes([report]) == {exit_key: {False, True}}


def test_inventory_accepts_an_empty_task_file(tmp_path: Path) -> None:
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text("# Intentionally empty.\n")

    assert inventory_tasks([source]) == {}


def test_production_paths_include_helpers_but_not_test_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    for name in (
        "main.yml",
        "_trim_timer.yml",
        "_setup.yml",
        "_setup_extra.yml",
        "_verify.yml",
        "_verify_more.yml",
    ):
        (tasks / name).write_text("- debug: {msg: example}\n")

    assert production_condition_paths(["example"]) == [
        Path("roles/example/tasks/_trim_timer.yml"),
        Path("roles/example/tasks/main.yml"),
    ]


def test_loop_coverage_requires_an_observed_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text("- debug: {msg: '{{ item }}'}\n  loop: '{{ example_items }}'\n")
    report = tmp_path / "coverage.jsonl"

    loop = next(iter(inventory_loops([source])))
    assert check_loop_coverage(["example"], []) == {loop}
    assert "main.yml:2:9: {{ example_items }}" in format_unexecuted_loops({loop})

    _start_report(report)
    append_loop_executions(report, [loop], phase="converge")

    assert load_executed_loops([report]) == {loop}
    assert check_coverage(["example"], [report], scenario_path=None) == {}
    assert check_loop_coverage(["example"], [report]) == set()


def test_callback_counts_item_callback_but_not_empty_loop_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "coverage.jsonl"
    monkeypatch.setenv("ANSIBLE_CONDITION_COVERAGE_FILE", str(report))
    loop = Origin(
        path="/tmp/staged/roles/example/tasks/main.yml",
        line_num=20,
        col_num=9,
    ).tag("{{ example_items }}")
    result = SimpleNamespace(
        task=SimpleNamespace(
            loop=loop,
            loop_with=None,
            when=[],
            get_path=lambda: "/tmp/staged/roles/example/tasks/main.yml:20",
        ),
        result={},
    )
    callback = condition_coverage_callback.CallbackModule()

    callback.v2_runner_on_skipped(cast(Any, result))
    assert len(report.read_text().splitlines()) == 1

    callback.v2_runner_item_on_ok(cast(Any, result))
    callback.v2_runner_item_on_ok(cast(Any, result))

    assert load_executed_loops([report]) == {LoopKey("roles/example/tasks/main.yml", 20, 9, "{{ example_items }}")}
    assert load_executed_tasks([report]) == {TaskKey("roles/example/tasks/main.yml", 20)}
    assert len(report.read_text().splitlines()) == 3


def test_callback_correlates_always_with_normal_and_rescued_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "coverage.jsonl"
    monkeypatch.setenv("ANSIBLE_CONDITION_COVERAGE_FILE", str(report))
    block_path = "/tmp/staged/roles/example/tasks/main.yml:10"

    def task(name: str, line: int, uuid: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            _uuid=uuid,
            _parent=None,
            loop=None,
            loop_with=None,
            when=[],
            get_path=lambda: f"/tmp/staged/roles/example/tasks/main.yml:{line}",
        )

    primary = task("Primary", 12, "primary")
    rescue = task("Rescue", 15, "rescue")
    always = task("Always", 18, "always")
    parent = SimpleNamespace(
        _uuid="block",
        _parent=None,
        block=[primary],
        rescue=[rescue],
        always=[always],
        get_path=lambda: block_path,
    )
    for child in (primary, rescue, always):
        child._parent = parent
    host = SimpleNamespace(get_name=lambda: "example")

    def result(child: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(task=child, result={}, _host=host)

    callback = condition_coverage_callback.CallbackModule()
    callback.v2_runner_on_ok(cast(Any, result(primary)))
    callback.v2_runner_on_ok(cast(Any, result(always)))
    callback.v2_runner_on_failed(cast(Any, result(primary)))
    callback.v2_runner_on_ok(cast(Any, result(rescue)))
    callback.v2_runner_on_ok(cast(Any, result(always)))

    always_events = {event.after for event in load_block_events([report]) if event.section == "always"}
    assert always_events == {"normal", "rescued"}


def test_callback_records_retry_false_then_terminal_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "coverage.jsonl"
    monkeypatch.setenv("ANSIBLE_CONDITION_COVERAGE_FILE", str(report))
    until = Origin(
        path="/tmp/staged/roles/example/tasks/main.yml",
        line_num=24,
        col_num=10,
    ).tag("retry_result is succeeded")
    task = SimpleNamespace(
        _parent=None,
        loop=None,
        loop_with=None,
        until=until,
        when=[],
        get_path=lambda: "/tmp/staged/roles/example/tasks/main.yml:20",
    )
    host = SimpleNamespace(get_name=lambda: "example")
    result = SimpleNamespace(task=task, result={"attempts": 1}, _host=host)
    callback = condition_coverage_callback.CallbackModule()

    callback.v2_runner_retry(cast(Any, result))
    result.result = {"attempts": 2}
    callback.v2_runner_on_ok(cast(Any, result))

    assert load_until_outcomes([report]) == {
        UntilKey("roles/example/tasks/main.yml", 24, 10, "retry_result is succeeded"): {
            False,
            True,
        }
    }


def test_callback_does_not_record_suppressed_retry_exhaustion_as_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "coverage.jsonl"
    monkeypatch.setenv("ANSIBLE_CONDITION_COVERAGE_FILE", str(report))
    until = Origin(
        path="/tmp/staged/roles/example/tasks/main.yml",
        line_num=24,
        col_num=10,
    ).tag("retry_result is succeeded")
    task = SimpleNamespace(
        _parent=None,
        loop=None,
        loop_with=None,
        until=until,
        when=[],
        get_path=lambda: "/tmp/staged/roles/example/tasks/main.yml:20",
    )
    host = SimpleNamespace(get_name=lambda: "example")
    result = SimpleNamespace(task=task, result={"attempts": 1}, _host=host)
    callback = condition_coverage_callback.CallbackModule()

    callback.v2_runner_retry(cast(Any, result))
    result.result = {"attempts": 2}
    callback.v2_runner_retry(cast(Any, result))
    result.result = {"failed_when_result": False, "attempts": 2}
    callback.v2_runner_on_ok(cast(Any, result))

    assert load_until_outcomes([report]) == {
        UntilKey("roles/example/tasks/main.yml", 24, 10, "retry_result is succeeded"): {False}
    }


def test_task_coverage_requires_a_non_skipped_terminal_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text("- name: Example\n  debug: {msg: example}\n")
    report = tmp_path / "coverage.jsonl"
    expected = TaskDefinition(TaskKey("roles/example/tasks/main.yml", 1), "debug", "Example")

    assert check_task_coverage(["example"], []) == {expected}
    assert "main.yml:1: [debug] Example" in format_unexecuted_tasks({expected})

    _start_report(report)
    append_task_executions(report, [expected.key], phase="converge")

    assert load_executed_tasks([report]) == {expected.key}
    assert check_task_coverage(["example"], [report]) == set()


def test_synthetic_task_reachability_requires_all_effective_conditions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\
- name: Optional tasks
  when: feature_enabled
  block:
    - name: Conditional action
      command: /bin/true
      when: service_ready
"""
    )
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - kind: task
    path: roles/example/tasks/main.yml
    cases:
      - tasks:
          - Conditional action
        variables:
          feature_enabled: true
          service_ready: true
"""
    )

    reachable = load_synthetic_task_reachability(scenarios)

    assert len(reachable) == 1
    assert check_task_coverage(["example"], [], scenario_path=scenarios) == set()
    assert load_synthetic_result_predicate_outcomes(scenarios) == {}


def test_synthetic_task_reachability_rejects_unconditional_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "roles" / "example" / "tasks" / "main.yml"
    source.parent.mkdir(parents=True)
    source.write_text("- name: Always runs\n  command: /bin/true\n")
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - kind: task
    path: roles/example/tasks/main.yml
    cases:
      - tasks: [Always runs]
        variables: {}
"""
    )

    with pytest.raises(ValueError, match="cannot synthesize unconditional task"):
        load_synthetic_task_reachability(scenarios)


def test_unknown_role_is_rejected_rather_than_scoping_to_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("- debug: {msg: example}\n  when: feature_enabled\n")

    assert production_condition_paths(["example"]) == [Path("roles/example/tasks/main.yml")]

    with pytest.raises(ValueError, match=r"no roles/<role>/tasks/.*: example_renamed"):
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


def test_repository_scenarios_match_current_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate only reads the scenario file after a VM matrix has run, so a
    # selector left stale by a task edit must fail here first.
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)

    assert load_synthetic_outcomes(SYNTHETIC_SCENARIOS_PATH)
    assert load_synthetic_until_outcomes(SYNTHETIC_SCENARIOS_PATH)
    assert load_synthetic_result_predicate_outcomes(SYNTHETIC_SCENARIOS_PATH)
    assert load_synthetic_task_reachability(SYNTHETIC_SCENARIOS_PATH)


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


def test_synthetic_result_predicate_scenario_evaluates_expression_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("- command: /bin/true\n  failed_when:\n    - result.rc != 0\n    - force_failure\n")
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - kind: failed_when
    path: roles/example/tasks/main.yml
    expression:
      - result.rc != 0
      - force_failure
    cases:
      - outcome: false
        variables:
          result: {rc: 0}
          force_failure: true
      - outcome: true
        variables:
          result: {rc: 1}
          force_failure: true
"""
    )

    outcomes = load_synthetic_result_predicate_outcomes(scenarios)

    assert list(outcomes.values()) == [{False, True}]
    assert load_synthetic_outcomes(scenarios) == {}


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

    with pytest.raises(ValueError, match=r"matches 2 condition\(s\) at lines 2, 4 but expects 1"):
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


def test_synthetic_scenario_can_select_duplicate_by_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- name: First\n  debug: {msg: first}\n  when: feature_enabled\n"
        "- name: Second\n  debug: {msg: second}\n  when: feature_enabled\n"
    )
    scenarios = tmp_path / "scenarios.yml"
    scenarios.write_text(
        """\
scenarios:
  - path: roles/example/tasks/main.yml
    task: Second
    expression: feature_enabled
    cases:
      - outcome: false
        variables:
          feature_enabled: false
"""
    )

    outcomes = load_synthetic_outcomes(scenarios)

    assert [condition.line for condition in outcomes] == [6]

    # A shifted line leaves the task selector valid; a renamed task does not.
    (tasks / "main.yml").write_text("# shifted\n" + (tasks / "main.yml").read_text())
    assert [condition.line for condition in load_synthetic_outcomes(scenarios)] == [7]

    (tasks / "main.yml").write_text((tasks / "main.yml").read_text().replace("Second", "Renamed"))
    with pytest.raises(ValueError, match=r"does not match a current condition: .* in task 'Second'"):
        load_synthetic_outcomes(scenarios)


def test_synthetic_scenario_all_pins_the_match_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    source = tasks / "main.yml"
    copy = "- debug: {msg: copy}\n  when: feature_enabled\n"
    source.write_text(copy * 2)
    scenarios = tmp_path / "scenarios.yml"
    scenario = """\
scenarios:
  - path: roles/example/tasks/main.yml
    expression: feature_enabled
    all: {all}
    cases:
      - outcome: true
        variables:
          feature_enabled: true
"""
    scenarios.write_text(scenario.format(all=2))

    assert len(load_synthetic_outcomes(scenarios)) == 2

    # A third copy of the expression must be reviewed, not silently covered.
    source.write_text(copy * 3)
    with pytest.raises(ValueError, match=r"matches 3 condition\(s\) at lines 2, 4, 6 but expects 2"):
        load_synthetic_outcomes(scenarios)

    scenarios.write_text(scenario.format(all="true"))
    with pytest.raises(ValueError, match="all must be the expected match count"):
        load_synthetic_outcomes(scenarios)
