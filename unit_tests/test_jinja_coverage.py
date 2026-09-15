"""Tests for Jinja control-flow coverage collection."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from ansible._internal._datatag._tags import Origin, TrustedAsTemplate
from condition_coverage import (
    COVERAGE_SCHEMA_VERSION,
    CoverageProvenance,
    JinjaBranchKey,
    JinjaBranchOutcome,
    JinjaLoopKey,
    append_jinja_branch_outcomes,
    append_jinja_loop_executions,
    append_report_provenance,
    check_jinja_branch_coverage,
    check_jinja_loop_coverage,
    format_missing_jinja_branch_outcomes,
    format_unexecuted_jinja_loops,
    inventory_role_jinja,
    load_executed_jinja_loops,
    load_jinja_branch_outcomes,
    production_jinja_paths,
    repository_source_sha,
)
from jinja2 import Environment
from jinja_coverage import instrument_jinja_tree, normalize_source_path


def test_normalize_source_path_handles_variable_trees() -> None:
    assert normalize_source_path("/tmp/staged/group_vars/all/main.yml") == "group_vars/all/main.yml"
    assert normalize_source_path("/tmp/staged/host_vars/example.yml") == "host_vars/example.yml"


def test_inventory_covers_inline_role_variables_and_template_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    defaults = tmp_path / "roles" / "example" / "defaults"
    templates = tmp_path / "roles" / "example" / "templates"
    tasks.mkdir(parents=True)
    defaults.mkdir()
    templates.mkdir()
    (tasks / "main.yml").write_text(
        """\
- copy:
    content: |
      {% if inline_enabled %}yes{% else %}no{% endif %}
    dest: /tmp/example
"""
    )
    (defaults / "main.yml").write_text(
        "choice: \"{{ 'yes' if default_enabled else 'no' }}\"\n"
        "filtered_choice: \"{{ default_enabled | ternary('yes', 'no') }}\"\n"
    )
    (templates / "example.j2").write_text(
        """\
#jinja2: lstrip_blocks: True
{% if first %}
first
{% elif second %}
second
{% endif %}
{% for item in items if item.enabled %}{{ item.name }}{% else %}empty{% endfor %}
"""
    )
    (templates / "_verify_fixture.j2").write_text("{% if fixture %}fixture{% endif %}\n")

    inline_paths, template_paths = production_jinja_paths(["example"])
    inventory = inventory_role_jinja(["example"])

    assert inline_paths == [
        Path("roles/example/defaults/main.yml"),
        Path("roles/example/tasks/main.yml"),
    ]
    assert template_paths == [Path("roles/example/templates/example.j2")]
    assert {branch.kind for branch in inventory.branches} == {
        "if",
        "elif",
        "ternary",
        "ternary_filter",
        "for_filter",
        "for_else",
    }
    assert {branch.path for branch in inventory.branches} == {
        "roles/example/defaults/main.yml",
        "roles/example/tasks/main.yml",
        "roles/example/templates/example.j2",
    }
    assert len(inventory.loops) == 1


def test_ast_instrumentation_preserves_output_and_records_control_flow() -> None:
    source = Origin(
        path="/tmp/staged/roles/example/templates/example.j2",
        line_num=1,
        col_num=1,
    ).tag(
        TrustedAsTemplate().tag(
            "{% for item in items if item.enabled %}{% if item.show %}{{ item.name }}{% endif %}"
            '{% else %}empty{% endfor %}:{{ "yes" if flag else "no" }}:{{ flag | ternary("on", "off") }}'
        )
    )
    branches: list[tuple[JinjaBranchKey, bool]] = []
    loops: list[JinjaLoopKey] = []
    environment = Environment()
    environment.filters["ternary"] = lambda value, true_value, false_value: true_value if value else false_value
    tree = environment.parse(source)
    inventory = instrument_jinja_tree(tree, source)

    def record_branch(payload: tuple[Any, ...], value: object) -> bool:
        outcome = bool(value)
        branches.append((JinjaBranchKey(*payload), outcome))
        return outcome

    def record_loop(payload: tuple[Any, ...]) -> None:
        loops.append(JinjaLoopKey(*payload))

    environment._record_jinja_branch = record_branch  # type: ignore[attr-defined]
    environment._record_jinja_loop = record_loop  # type: ignore[attr-defined]
    template = environment.from_string(tree)

    first = template.render(
        items=[
            {"enabled": True, "show": True, "name": "shown"},
            {"enabled": False, "show": True, "name": "filtered"},
        ],
        flag=False,
    )
    second = template.render(items=[], flag=True)

    assert first == "shown:no:off"
    assert second == "empty:yes:on"
    assert {outcome for _key, outcome in branches} == {False, True}
    assert set(loops) == set(inventory.loops)
    assert {key for key, _outcome in branches} == set(inventory.branches)


def test_callback_records_inline_and_file_template_events_across_workers(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    report = tmp_path / "coverage.jsonl"
    template = tmp_path / "example.j2"
    template.write_text(
        """\
{% if file_enabled %}yes{% else %}no{% endif %}
{% for value in values %}{{ value }}{% endfor %}
"""
    )
    playbook = tmp_path / "playbook.yml"
    playbook.write_text(
        f"""\
- hosts: all
  gather_facts: false
  tasks:
    - debug:
        msg: "{{% if item %}}yes{{% else %}}no{{% endif %}}"
      loop: [false, true]
    - debug:
        msg: "{{{{ item | ternary('yes', 'no') }}}}"
      loop: [false, true]
    - template:
        src: {template}
        dest: {tmp_path / "false.txt"}
      vars:
        file_enabled: false
        values: []
    - template:
        src: {template}
        dest: {tmp_path / "true.txt"}
      vars:
        file_enabled: true
        values: [one]
"""
    )
    env = os.environ | {
        "ANSIBLE_CALLBACK_PLUGINS": str(repo_root / "callback_plugins"),
        "ANSIBLE_CALLBACKS_ENABLED": "condition_coverage",
        "ANSIBLE_CONDITION_COVERAGE_FILE": str(report),
        "ANSIBLE_CONDITION_COVERAGE_PHASE": "converge",
        "ANSIBLE_STRATEGY": "linear",
    }

    subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    branch_outcomes = load_jinja_branch_outcomes([report])
    assert {key.path for key in branch_outcomes} == {
        normalize_source_path(str(playbook)),
        normalize_source_path(str(template)),
    }
    assert all(outcomes == {False, True} for outcomes in branch_outcomes.values())
    assert {loop.path for loop in load_executed_jinja_loops([report])} == {normalize_source_path(str(template))}


def test_jinja_gate_requires_both_branch_outcomes_and_a_loop_iteration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = tmp_path / "roles" / "example" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        """\
- debug:
    msg: "{% if enabled %}{% for item in items %}{{ item }}{% endfor %}{% endif %}"
"""
    )
    inventory = inventory_role_jinja(["example"])
    branch = next(iter(inventory.branches))
    loop = next(iter(inventory.loops))
    report = tmp_path / "coverage.jsonl"

    assert check_jinja_branch_coverage(["example"], []) == {branch: {False, True}}
    assert check_jinja_loop_coverage(["example"], []) == {loop}
    assert "if missing false, true" in format_missing_jinja_branch_outcomes({branch: {False, True}})
    assert "Jinja loop(s) never iterated" in format_unexecuted_jinja_loops({loop})

    append_report_provenance(
        report,
        CoverageProvenance(COVERAGE_SCHEMA_VERSION, repository_source_sha(), "x86_64"),
    )
    append_jinja_branch_outcomes(
        report,
        [JinjaBranchOutcome(branch, False), JinjaBranchOutcome(branch, True)],
        phase="converge",
    )
    append_jinja_loop_executions(report, [loop], phase="converge")

    assert check_jinja_branch_coverage(["example"], [report]) == {}
    assert check_jinja_loop_coverage(["example"], [report]) == set()
