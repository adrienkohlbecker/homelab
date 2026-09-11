"""Collect and validate Ansible condition branches and loop iterations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

# Private ansible internals, deliberately: nothing public exposes a loaded
# value's YAML origin (file/line/column), which is the only stable identity a
# `when` expression has, nor a conditional evaluator the synthetic scenarios can
# drive offline. Both moved under ansible._internal in core 2.19 and carry no
# compatibility promise -- an ansible bump that breaks the gate starts here.
from ansible._internal._datatag._tags import Origin, TrustedAsTemplate
from ansible._internal._templating._engine import TemplateEngine
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.task import Task

SYNTHETIC_SCENARIOS_PATH = Path("test/condition_coverage.yml")


@dataclass(frozen=True, order=True)
class ConditionKey:
    """Stable source identity for one declared ``when`` expression."""

    path: str
    line: int
    column: int
    expression: str


@dataclass(frozen=True)
class ConditionOutcome:
    """One observed Boolean result for a condition."""

    condition: ConditionKey
    outcome: bool


@dataclass(frozen=True, order=True)
class LoopKey:
    """Stable source identity for one declared task loop."""

    path: str
    line: int
    column: int
    expression: str


@dataclass(frozen=True, order=True)
class TaskKey:
    """Stable source identity for one executable task."""

    path: str
    line: int


@dataclass(frozen=True, order=True)
class TaskDefinition:
    """One inventoried executable task and its diagnostic metadata."""

    key: TaskKey
    action: str
    name: str


_TASK_STRUCTURAL_KEYS = frozenset({"block", "rescue", "always"})
_TASK_NON_EXECUTING_ACTIONS = frozenset({"import_role", "import_tasks", "include_role", "include_tasks", "meta"})
_TASK_ATTRIBUTE_KEYS = frozenset(Task.fattributes).union(_TASK_STRUCTURAL_KEYS)


def normalize_source_path(path: str) -> str:
    """Normalize original and staged Ansible paths to repository-relative paths."""
    normalized = Path(path).as_posix()
    if marker := "/roles/" if "/roles/" in normalized else None:
        return f"roles/{normalized.split(marker, 1)[1]}"
    if normalized.endswith("/site.yml") or normalized == "site.yml":
        return "site.yml"
    return normalized


def condition_key(value: object) -> ConditionKey:
    """Return the tagged YAML origin and source text for one condition value."""
    origin = Origin.get_tag(value)
    if origin is None or origin.path is None or origin.line_num is None or origin.col_num is None:
        raise ValueError(f"condition has no complete YAML origin: {value!r}")
    return ConditionKey(
        path=normalize_source_path(origin.path),
        line=origin.line_num,
        column=origin.col_num,
        expression=str(value),
    )


def loop_key(value: object) -> LoopKey:
    """Return the tagged YAML origin and source text for one loop value."""
    origin = Origin.get_tag(value)
    if origin is None or origin.path is None or origin.line_num is None or origin.col_num is None:
        raise ValueError(f"loop has no complete YAML origin: {value!r}")
    return LoopKey(
        path=normalize_source_path(origin.path),
        line=origin.line_num,
        column=origin.col_num,
        expression=str(value),
    )


def task_key_from_path(path: str) -> TaskKey:
    """Return a task key from Ansible's ``path:line`` callback identity."""
    source_path, separator, source_line = path.rpartition(":")
    if not separator or not source_path or not source_line.isdigit():
        raise ValueError(f"task has no complete source identity: {path!r}")
    return TaskKey(normalize_source_path(source_path), int(source_line))


def evaluated_outcomes(
    conditions: Sequence[object],
    false_condition: object | None = None,
) -> list[ConditionOutcome]:
    """Resolve outcomes from Ansible's ordered, short-circuit condition list."""
    if false_condition is None:
        return [ConditionOutcome(condition_key(condition), True) for condition in conditions]

    false_text = str(false_condition)
    outcomes: list[ConditionOutcome] = []
    for condition in conditions:
        is_false = str(condition) == false_text
        outcomes.append(ConditionOutcome(condition_key(condition), not is_false))
        if is_false:
            return outcomes
    raise ValueError(f"false condition is absent from task condition list: {false_text!r}")


def _walk_when_values(value: object) -> Iterator[tuple[object, str | None]]:
    """Yield each ``when`` condition with the name of the task or block declaring it."""
    if isinstance(value, dict):
        name = next((str(child) for key, child in value.items() if str(key) == "name"), None)
        for key, child in value.items():
            if str(key) == "when":
                for condition in child if isinstance(child, list) else [child]:
                    yield condition, name
            yield from _walk_when_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_when_values(child)


def _walk_loop_values(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) == "loop" or str(key).startswith("with_"):
                yield child
            yield from _walk_loop_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_loop_values(child)


def _walk_task_mappings(tasks: object) -> Iterator[dict[object, object]]:
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError(f"task list entry must be a mapping: {task!r}")
        yield task
        for section in _TASK_STRUCTURAL_KEYS:
            yield from _walk_task_mappings(task.get(section))


def _document_task_mappings(document: object) -> Iterator[dict[object, object]]:
    if document is None:
        return
    if not isinstance(document, list):
        raise ValueError("Ansible task document must be a list")
    if document and all(isinstance(entry, dict) and "hosts" in entry for entry in document):
        for play in document:
            for section in ("pre_tasks", "tasks", "post_tasks"):
                yield from _walk_task_mappings(play.get(section))
        return
    yield from _walk_task_mappings(document)


def _task_definition(task: dict[object, object]) -> TaskDefinition | None:
    if "block" in task:
        return None
    action_keys = [str(key) for key in task if str(key) not in _TASK_ATTRIBUTE_KEYS]
    if not action_keys and "action" in task:
        action_keys = [str(task["action"])]
    if len(action_keys) != 1:
        name = str(task.get("name", "<unnamed>"))
        raise ValueError(f"task {name!r} has {len(action_keys)} action keys: {', '.join(action_keys)}")
    action = action_keys[0]
    if action in _TASK_NON_EXECUTING_ACTIONS:
        return None
    origin = Origin.get_tag(task)
    if origin is None or origin.path is None or origin.line_num is None:
        raise ValueError(f"task has no complete YAML origin: {task!r}")
    return TaskDefinition(
        key=TaskKey(normalize_source_path(origin.path), origin.line_num),
        action=action,
        name=str(task.get("name", "<unnamed>")),
    )


def production_condition_paths(roles: Iterable[str] | None = None, *, include_site: bool = False) -> list[Path]:
    """Return production task files in the requested coverage scope.

    Raises when a requested role contributes no task file. An empty scope would
    otherwise expect nothing and report full coverage, so a renamed or misspelled
    role silently retires its own gate -- exactly when the gate should shout.
    """
    selected_roles = None if roles is None else set(roles)
    paths = [
        path
        for path in Path("roles").glob("*/tasks/*.yml")
        if not path.stem.startswith(("_setup", "_verify", "_test"))
        and (selected_roles is None or path.parts[1] in selected_roles)
    ]
    if selected_roles is not None and (unknown := selected_roles.difference(path.parts[1] for path in paths)):
        raise ValueError(f"no roles/<role>/tasks/*.yml for requested role(s): {', '.join(sorted(unknown))}")
    if include_site and Path("site.yml").exists():
        paths.append(Path("site.yml"))
    return sorted(paths)


def inventory_conditions(paths: Iterable[Path]) -> dict[ConditionKey, str | None]:
    """Map every declared ``when`` expression in the given files to its task name.

    The name (None for an unnamed task or block) lets synthetic scenarios pick
    one of several identical expressions without pinning a line number.
    """
    loader = DataLoader()
    conditions: dict[ConditionKey, str | None] = {}
    for path in paths:
        document = loader.load_from_file(str(path.resolve()))
        conditions.update((condition_key(value), name) for value, name in _walk_when_values(document))
    return conditions


def inventory_loops(paths: Iterable[Path]) -> set[LoopKey]:
    """Load every declared task loop from production task files."""
    loader = DataLoader()
    loops: set[LoopKey] = set()
    for path in paths:
        document = loader.load_from_file(str(path.resolve()))
        loops.update(loop_key(value) for value in _walk_loop_values(document))
    return loops


def inventory_tasks(paths: Iterable[Path]) -> dict[TaskKey, TaskDefinition]:
    """Load every executable task from production task files."""
    loader = DataLoader()
    tasks: dict[TaskKey, TaskDefinition] = {}
    for path in paths:
        document = loader.load_from_file(str(path.resolve()))
        for task in _document_task_mappings(document):
            if definition := _task_definition(task):
                if previous := tasks.get(definition.key):
                    raise ValueError(f"duplicate task source identity: {previous!r}, {definition!r}")
                tasks[definition.key] = definition
    return tasks


def append_outcomes(path: Path, outcomes: Iterable[ConditionOutcome], *, phase: str) -> None:
    """Append observed outcomes as compact JSON lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for outcome in outcomes:
            handle.write(json.dumps(asdict(outcome) | {"phase": phase}, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def append_loop_executions(path: Path, loops: Iterable[LoopKey], *, phase: str) -> None:
    """Append loop declarations observed through per-item callbacks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for loop in loops:
            handle.write(json.dumps({"loop": asdict(loop), "phase": phase}, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def append_task_executions(path: Path, tasks: Iterable[TaskKey], *, phase: str) -> None:
    """Append tasks observed through non-skipped terminal callbacks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps({"task": asdict(task), "phase": phase}, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def _report_rows(paths: Iterable[Path]) -> Iterator[tuple[Path, int, dict[str, Any]]]:
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                data: dict[str, Any] = json.loads(line)
                if error := data.get("error"):
                    raise ValueError(f"{path}:{line_number}: callback error: {error}")
                if not {"condition", "loop", "task"}.intersection(data):
                    raise ValueError(f"{path}:{line_number}: unknown coverage record")
                yield path, line_number, data


def load_outcomes(paths: Iterable[Path]) -> dict[ConditionKey, set[bool]]:
    """Merge condition outcomes from callback JSONL reports."""
    merged: dict[ConditionKey, set[bool]] = {}
    for _path, _line_number, data in _report_rows(paths):
        if "condition" not in data:
            continue
        key = ConditionKey(**data["condition"])
        merged.setdefault(key, set()).add(bool(data["outcome"]))
    return merged


def load_executed_loops(paths: Iterable[Path]) -> set[LoopKey]:
    """Merge loop executions from callback JSONL reports."""
    return {LoopKey(**data["loop"]) for _path, _line_number, data in _report_rows(paths) if "loop" in data}


def load_executed_tasks(paths: Iterable[Path]) -> set[TaskKey]:
    """Merge executed tasks from callback JSONL reports."""
    return {TaskKey(**data["task"]) for _path, _line_number, data in _report_rows(paths) if "task" in data}


def _normalized_expression(expression: str) -> str:
    return " ".join(expression.split())


def load_synthetic_outcomes(
    path: Path,
    conditions: dict[ConditionKey, str | None] | None = None,
) -> dict[ConditionKey, set[bool]]:
    """Evaluate declared synthetic cases against current source expressions.

    Scenarios are matched against the whole repository, not the caller's
    coverage scope, so a stale selector fails the gate from any cell. Callers
    that already hold that inventory pass it in; the parse is ~160 files.

    A scenario selects by path and expression, narrowed by ``task`` (the
    declaring task's name) when the expression repeats within the file.
    ``line`` still narrows too, but shifts with every edit above it. ``all``
    (default 1) is the exact number of conditions the selector must match.
    """
    document = yaml.safe_load(path.read_text()) or {}
    scenarios = document.get("scenarios", [])
    if not isinstance(scenarios, list):
        raise ValueError(f"{path}: scenarios must be a list")

    if conditions is None:
        conditions = inventory_conditions(production_condition_paths(include_site=True))
    outcomes: dict[ConditionKey, set[bool]] = {}
    loader = DataLoader()
    for index, scenario in enumerate(scenarios, 1):
        if not isinstance(scenario, dict):
            raise ValueError(f"{path}: scenario {index} must be a mapping")
        source_path = str(scenario.get("path", ""))
        expression = _normalized_expression(str(scenario.get("expression", "")))
        source_line = scenario.get("line")
        if source_line is not None and not isinstance(source_line, int):
            raise ValueError(f"{path}: scenario {index} line must be an integer")
        source_task = scenario.get("task")
        if source_task is not None and not isinstance(source_task, str):
            raise ValueError(f"{path}: scenario {index} task must be a string")
        # `all` pins how many identical conditions one scenario covers, so a
        # new copy of the expression fails here instead of being absorbed.
        expected_matches = scenario.get("all", 1)
        if isinstance(expected_matches, bool) or not isinstance(expected_matches, int) or expected_matches < 1:
            raise ValueError(f"{path}: scenario {index} all must be the expected match count")
        matches = {
            condition
            for condition in conditions
            if condition.path == source_path
            and _normalized_expression(condition.expression) == expression
            and (source_line is None or condition.line == source_line)
            and (source_task is None or conditions[condition] == source_task)
        }
        if not matches:
            task = f" in task {source_task!r}" if source_task is not None else ""
            raise ValueError(
                f"{path}: scenario {index} does not match a current condition: {source_path}: {expression}{task}"
            )
        if len(matches) != expected_matches:
            lines = ", ".join(str(condition.line) for condition in sorted(matches))
            raise ValueError(
                f"{path}: scenario {index} matches {len(matches)} condition(s) at lines {lines} but expects "
                f"{expected_matches}; narrow it with task or set all to the reviewed count"
            )

        cases = scenario.get("cases", [])
        if not isinstance(cases, list) or not cases:
            raise ValueError(f"{path}: scenario {index} cases must be a non-empty list")
        source_expression = next(iter(matches)).expression
        trusted_expression = TrustedAsTemplate().tag(source_expression)
        for case_index, case in enumerate(cases, 1):
            if not isinstance(case, dict) or not isinstance(case.get("outcome"), bool):
                raise ValueError(f"{path}: scenario {index} case {case_index} requires a Boolean outcome")
            variables = case.get("variables", {})
            if not isinstance(variables, dict):
                raise ValueError(f"{path}: scenario {index} case {case_index} variables must be a mapping")
            actual = TemplateEngine(loader, variables=variables).evaluate_conditional(trusted_expression)
            if actual is not case["outcome"]:
                raise ValueError(
                    f"{path}: scenario {index} case {case_index} expected {case['outcome']} but evaluated {actual}"
                )
            for condition in matches:
                outcomes.setdefault(condition, set()).add(actual)
    return outcomes


def merge_outcomes(*sources: dict[ConditionKey, set[bool]]) -> dict[ConditionKey, set[bool]]:
    """Union outcomes from runtime and synthetic test sources."""
    merged: dict[ConditionKey, set[bool]] = {}
    for source in sources:
        for condition, outcomes in source.items():
            merged.setdefault(condition, set()).update(outcomes)
    return merged


def missing_outcomes(
    expected: Iterable[ConditionKey],
    observed: dict[ConditionKey, set[bool]],
) -> dict[ConditionKey, set[bool]]:
    """Return the Boolean results not observed for each expected condition."""
    both = {False, True}
    return {
        condition: both.difference(observed.get(condition, set()))
        for condition in expected
        if observed.get(condition, set()) != both
    }


def format_missing_outcomes(missing: dict[ConditionKey, set[bool]]) -> str:
    """Render uncovered condition outcomes as source-oriented diagnostics."""
    lines = [f"{len(missing)} Ansible condition(s) lack Boolean branch coverage:"]
    for condition, outcomes in sorted(missing.items()):
        labels = ", ".join(str(outcome).lower() for outcome in sorted(outcomes))
        expression = " ".join(condition.expression.split())
        lines.append(f"  {condition.path}:{condition.line}:{condition.column}: missing {labels}: {expression}")
    return "\n".join(lines)


def format_unexecuted_loops(missing: set[LoopKey]) -> str:
    """Render loops that never produced an item callback."""
    lines = [f"{len(missing)} Ansible loop(s) never iterated:"]
    for loop in sorted(missing):
        expression = " ".join(loop.expression.split())
        lines.append(f"  {loop.path}:{loop.line}:{loop.column}: {expression}")
    return "\n".join(lines)


def format_unexecuted_tasks(missing: set[TaskDefinition]) -> str:
    """Render production tasks that never completed without being skipped."""
    lines = [f"{len(missing)} Ansible task(s) never executed:"]
    lines.extend(f"  {task.key.path}:{task.key.line}: [{task.action}] {task.name}" for task in sorted(missing))
    return "\n".join(lines)


def check_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
    scenario_path: Path | None = SYNTHETIC_SCENARIOS_PATH,
) -> dict[ConditionKey, set[bool]]:
    """Compare production conditions with outcomes merged from test reports."""
    # Scope first so an unknown role raises before the repository-wide parse.
    # Conditions carry repository-relative paths, so the scope is a path filter
    # over one inventory pass rather than a second load of the same files.
    scope = {path.as_posix() for path in production_condition_paths(roles, include_site=include_site)}
    conditions = inventory_conditions(production_condition_paths(include_site=True))
    expected = {condition for condition in conditions if condition.path in scope}
    synthetic = (
        load_synthetic_outcomes(scenario_path, conditions)
        if scenario_path is not None and scenario_path.exists()
        else {}
    )
    observed = merge_outcomes(load_outcomes(reports), synthetic)
    return missing_outcomes(expected, observed)


def check_loop_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
) -> set[LoopKey]:
    """Return production task loops that did not iterate in any report."""
    expected = inventory_loops(production_condition_paths(roles, include_site=include_site))
    return expected.difference(load_executed_loops(reports))


def check_task_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
) -> set[TaskDefinition]:
    """Return production tasks with no non-skipped terminal callback."""
    expected = inventory_tasks(production_condition_paths(roles, include_site=include_site))
    missing_keys = set(expected).difference(load_executed_tasks(reports))
    return {expected[key] for key in missing_keys}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--roles",
        required=True,
        type=lambda value: [role for role in value.split(",") if role],
        help="Comma-separated production roles whose conditions must be covered",
    )
    parser.add_argument(
        "--include-site",
        action="store_true",
        help="Also require both outcomes for conditions declared directly in site.yml",
    )
    parser.add_argument("reports", nargs="+", type=Path, help="Callback JSONL report files to merge")
    return parser.parse_args()


def main() -> int:
    """Check condition and loop coverage from the command line."""
    args = _parse_args()
    try:
        missing = check_coverage(args.roles, args.reports, include_site=args.include_site)
        unexecuted_loops = check_loop_coverage(args.roles, args.reports, include_site=args.include_site)
        unexecuted_tasks = check_task_coverage(args.roles, args.reports, include_site=args.include_site)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Condition coverage report error: {exc}", file=sys.stderr)
        return 1
    if missing:
        print(format_missing_outcomes(missing), file=sys.stderr)
    if unexecuted_loops:
        print(format_unexecuted_loops(unexecuted_loops), file=sys.stderr)
    if unexecuted_tasks:
        print(format_unexecuted_tasks(unexecuted_tasks), file=sys.stderr)
    if missing or unexecuted_loops or unexecuted_tasks:
        return 1
    print("Every selected Ansible condition evaluated both true and false, every loop iterated, and every task ran.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
