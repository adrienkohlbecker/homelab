"""Collect and validate Ansible condition branches and loop iterations."""

from __future__ import annotations

import argparse
import fcntl
import json
import re
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
from jinja_coverage import (
    JinjaBranchKey,
    JinjaBranchOutcome,
    JinjaInventory,
    JinjaLoopKey,
    inventory_jinja,
    normalize_source_path,
)

SYNTHETIC_SCENARIOS_PATH = Path("test/condition_coverage.yml")
COVERAGE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CoverageProvenance:
    """Source-tree identity shared by every event in one coverage report."""

    schema: int
    source_sha: str
    architecture: str


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
    conditions: tuple[ConditionKey, ...] = ()


@dataclass(frozen=True, order=True)
class BlockKey:
    """Stable source identity for one branch-bearing block."""

    path: str
    line: int


@dataclass(frozen=True, order=True)
class BlockDefinition:
    """One block with rescue or always control flow."""

    key: BlockKey
    name: str
    normal_terminal: TaskKey
    has_rescue: bool
    has_always: bool


@dataclass(frozen=True, order=True)
class BlockEvent:
    """One observed branch-bearing block section callback."""

    block: BlockKey
    task: TaskKey
    section: str
    status: str
    after: str | None = None


@dataclass(frozen=True, order=True)
class BlockGap:
    """One unobserved control-flow outcome for a block."""

    block: BlockDefinition
    outcome: str


@dataclass(frozen=True, order=True)
class UntilKey:
    """Stable source identity for one ``until`` predicate group."""

    path: str
    line: int
    column: int
    expression: str


@dataclass(frozen=True, order=True)
class UntilOutcome:
    """One observed Boolean result for an ``until`` predicate group."""

    until: UntilKey
    outcome: bool


@dataclass(frozen=True, order=True)
class ResultPredicateKey:
    """Stable source identity for one dynamic result predicate group."""

    kind: str
    path: str
    line: int
    column: int
    expressions: tuple[str, ...]


@dataclass(frozen=True, order=True)
class ResultPredicateOutcome:
    """One observed aggregate result for a dynamic task predicate."""

    predicate: ResultPredicateKey
    outcome: bool


@dataclass(frozen=True, order=True)
class IncludeKey:
    """Stable source identity for one dynamic include declaration."""

    path: str
    line: int
    action: str


@dataclass(frozen=True, order=True)
class IncludeDefinition:
    """One dynamic include and the file it must expand."""

    key: IncludeKey
    name: str
    target: str


@dataclass(frozen=True, order=True)
class IncludeEvent:
    """One dynamic include expansion observed by Ansible."""

    include: IncludeKey
    target: str


@dataclass(frozen=True, order=True)
class ExitKey:
    """Stable source identity for one ``meta: end_role`` declaration."""

    path: str
    line: int


@dataclass(frozen=True, order=True)
class ExitDefinition:
    """One early role exit and its first fallthrough task."""

    key: ExitKey
    name: str
    fallthrough: TaskKey


@dataclass(frozen=True, order=True)
class ExitOutcome:
    """Whether one encountered early exit ended the role."""

    exit: ExitKey
    outcome: bool


@dataclass(frozen=True, order=True)
class ExitGap:
    """One unobserved branch of an early role exit."""

    exit: ExitDefinition
    outcome: str


_TASK_STRUCTURAL_KEYS = frozenset({"block", "rescue", "always"})
_TASK_NON_EXECUTING_ACTIONS = frozenset({"import_role", "import_tasks", "include_role", "include_tasks", "meta"})
_TASK_ATTRIBUTE_KEYS = frozenset(Task.fattributes).union(_TASK_STRUCTURAL_KEYS)
_INCLUDE_ACTIONS = frozenset({"include_role", "include_tasks"})


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
    expression = str(value)
    if origin is None and isinstance(value, list) and value:
        origin = Origin.get_tag(value[0])
        expression = str(value[0]) if len(value) == 1 else str([str(item) for item in value])
    if origin is None or origin.path is None or origin.line_num is None or origin.col_num is None:
        raise ValueError(f"loop has no complete YAML origin: {value!r}")
    return LoopKey(
        path=normalize_source_path(origin.path),
        line=origin.line_num,
        column=origin.col_num,
        expression=expression,
    )


def task_key_from_path(path: str) -> TaskKey:
    """Return a task key from Ansible's ``path:line`` callback identity."""
    source_path, separator, source_line = path.rpartition(":")
    if not separator or not source_path or not source_line.isdigit():
        raise ValueError(f"task has no complete source identity: {path!r}")
    return TaskKey(normalize_source_path(source_path), int(source_line))


def include_key_from_task(task) -> IncludeKey:
    """Return source identity for one runtime dynamic include task."""
    key = task_key_from_path(task.get_path())
    return IncludeKey(key.path, key.line, str(task.action).rsplit(".", 1)[-1])


def until_key(value: object) -> UntilKey:
    """Return the tagged YAML origin and source text for an ``until`` value."""
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ValueError("until predicate group is empty")
    origin = Origin.get_tag(values[0])
    if origin is None or origin.path is None or origin.line_num is None or origin.col_num is None:
        raise ValueError(f"until predicate has no complete YAML origin: {value!r}")
    expression = str(values[0]) if len(values) == 1 else str([str(item) for item in values])
    return UntilKey(
        path=normalize_source_path(origin.path),
        line=origin.line_num,
        column=origin.col_num,
        expression=expression,
    )


def result_predicate_key(kind: str, value: object) -> ResultPredicateKey | None:
    """Return source identity for a nonconstant changed/failed predicate group."""
    if kind not in {"changed_when", "failed_when"}:
        raise ValueError(f"unknown result predicate kind: {kind}")
    values = value if isinstance(value, list) else [value]
    if not values or all(isinstance(item, bool) for item in values):
        return None
    origins = (Origin.get_tag(item) for item in values)
    origin = next((item for item in origins if item is not None), None)
    if origin is None or origin.path is None or origin.line_num is None or origin.col_num is None:
        raise ValueError(f"{kind} predicate has no complete YAML origin: {value!r}")
    return ResultPredicateKey(
        kind=kind,
        path=normalize_source_path(origin.path),
        line=origin.line_num,
        column=origin.col_num,
        expressions=tuple(str(item) for item in values),
    )


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


def _walk_until_values(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) == "until":
                yield child
            yield from _walk_until_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_until_values(child)


def _walk_result_predicate_values(value: object) -> Iterator[tuple[str, object]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in {"changed_when", "failed_when"}:
                yield str(key), child
            yield from _walk_result_predicate_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_result_predicate_values(child)


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


def _task_action(task: dict[object, object]) -> str:
    if "block" in task:
        return "block"
    action_keys = [str(key) for key in task if str(key) not in _TASK_ATTRIBUTE_KEYS]
    if not action_keys and "action" in task:
        action_keys = [str(task["action"])]
    if len(action_keys) != 1:
        name = str(task.get("name", "<unnamed>"))
        raise ValueError(f"task {name!r} has {len(action_keys)} action keys: {', '.join(action_keys)}")
    return action_keys[0].rsplit(".", 1)[-1]


def _task_origin(task: dict[object, object]) -> TaskKey:
    origin = Origin.get_tag(task)
    if origin is None or origin.path is None or origin.line_num is None:
        raise ValueError(f"task has no complete YAML origin: {task!r}")
    return TaskKey(normalize_source_path(origin.path), origin.line_num)


def _task_conditions(task: dict[object, object]) -> tuple[ConditionKey, ...]:
    raw_conditions = task.get("when")
    if raw_conditions is None:
        return ()
    values = raw_conditions if isinstance(raw_conditions, list) else [raw_conditions]
    return tuple(condition_key(value) for value in values)


def _task_definition(
    task: dict[object, object],
    inherited_conditions: tuple[ConditionKey, ...] = (),
) -> TaskDefinition | None:
    if "block" in task:
        return None
    action = _task_action(task)
    if action in _TASK_NON_EXECUTING_ACTIONS:
        return None
    return TaskDefinition(
        key=_task_origin(task),
        action=action,
        name=str(task.get("name", "<unnamed>")),
        conditions=inherited_conditions + _task_conditions(task),
    )


def _walk_task_definitions(
    tasks: object,
    inherited_conditions: tuple[ConditionKey, ...] = (),
) -> Iterator[TaskDefinition]:
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError(f"task list entry must be a mapping: {task!r}")
        conditions = inherited_conditions + _task_conditions(task)
        if definition := _task_definition(task, inherited_conditions):
            yield definition
        for section in _TASK_STRUCTURAL_KEYS:
            yield from _walk_task_definitions(task.get(section), conditions)


def _action_value(task: dict[object, object], action: str) -> object:
    return next(value for key, value in task.items() if str(key).rsplit(".", 1)[-1] == action)


def _include_definition(task: dict[object, object]) -> IncludeDefinition | None:
    action = _task_action(task)
    if action not in _INCLUDE_ACTIONS:
        return None
    source = _task_origin(task)
    value = _action_value(task, action)
    if action == "include_tasks":
        raw_target = value.get("file") if isinstance(value, dict) else value
        target = Path(source.path).parent / str(raw_target)
    else:
        if not isinstance(value, dict):
            raise ValueError(f"include_role must use a mapping: {task!r}")
        role = value.get("name", value.get("role"))
        tasks_from = value.get("tasks_from", "main")
        raw_target = f"{role}:{tasks_from}"
        target = Path("roles") / str(role) / "tasks" / f"{tasks_from}.yml"
    if "{{" in str(raw_target) or "{%" in str(raw_target):
        raise ValueError(f"dynamic include target needs an explicit coverage target: {source.path}:{source.line}")
    return IncludeDefinition(
        key=IncludeKey(source.path, source.line, action),
        name=str(task.get("name", "<unnamed>")),
        target=target.as_posix(),
    )


def _first_execution_target(task: dict[object, object], loader: DataLoader) -> TaskKey | None:
    if definition := _task_definition(task):
        return definition.key
    if _task_action(task) != "import_tasks":
        return None
    source = _task_origin(task)
    value = _action_value(task, "import_tasks")
    raw_target = value.get("file") if isinstance(value, dict) else value
    if "{{" in str(raw_target) or "{%" in str(raw_target):
        raise ValueError(f"early-exit fallthrough import must be static: {source.path}:{source.line}")
    target = Path(source.path).parent / str(raw_target)
    document = loader.load_from_file(str(target.resolve()))
    return next(
        (
            execution
            for child in _document_task_mappings(document)
            if (execution := _first_execution_target(child, loader)) is not None
        ),
        None,
    )


def _walk_exit_definitions(tasks: object, loader: DataLoader) -> Iterator[ExitDefinition]:
    if not isinstance(tasks, list):
        return
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"task list entry must be a mapping: {task!r}")
        if _task_action(task) == "meta" and str(task.get("meta")) == "end_role":
            fallthrough = next(
                (
                    target
                    for sibling in tasks[index + 1 :]
                    if (target := _first_execution_target(sibling, loader)) is not None
                ),
                None,
            )
            source = _task_origin(task)
            if fallthrough is None:
                raise ValueError(f"meta: end_role has no observable fallthrough task: {source.path}:{source.line}")
            yield ExitDefinition(
                key=ExitKey(source.path, source.line),
                name=str(task.get("name", "<unnamed>")),
                fallthrough=fallthrough,
            )
        for section in _TASK_STRUCTURAL_KEYS:
            yield from _walk_exit_definitions(task.get(section), loader)


def _block_definition(task: dict[object, object]) -> BlockDefinition | None:
    if "block" not in task or not (task.get("rescue") or task.get("always")):
        return None
    origin = Origin.get_tag(task)
    if origin is None or origin.path is None or origin.line_num is None:
        raise ValueError(f"block has no complete YAML origin: {task!r}")
    primary_tasks = [
        definition
        for child in _walk_task_mappings(task["block"])
        if (definition := _task_definition(child)) is not None
    ]
    if not primary_tasks:
        raise ValueError(f"branch-bearing block has no executable primary task: {task!r}")
    return BlockDefinition(
        key=BlockKey(normalize_source_path(origin.path), origin.line_num),
        name=str(task.get("name", "<unnamed>")),
        normal_terminal=primary_tasks[-1].key,
        has_rescue=bool(task.get("rescue")),
        has_always=bool(task.get("always")),
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


def production_jinja_paths(
    roles: Iterable[str] | None = None,
    *,
    include_site: bool = False,
) -> tuple[list[Path], list[Path]]:
    """Return production inline-YAML and template-file Jinja sources."""
    selected_roles = None if roles is None else set(roles)
    task_paths = production_condition_paths(selected_roles, include_site=include_site)
    role_names = {path.parts[1] for path in task_paths if path.parts[0] == "roles"}
    inline_paths = set(task_paths)
    for role in role_names:
        for section in ("defaults", "vars"):
            inline_paths.update(Path("roles", role, section).glob("*.yml"))
    template_paths = {
        path
        for role in role_names
        for path in Path("roles", role, "templates").glob("**/*")
        if path.is_file() and not path.name.startswith(("_setup", "_verify", "_test"))
    }
    if include_site:
        inline_paths.update(Path("group_vars").glob("**/*.yml"))
        inline_paths.update(Path("host_vars").glob("**/*.yml"))
    return sorted(inline_paths), sorted(template_paths)


def inventory_role_jinja(roles: Iterable[str] | None = None, *, include_site: bool = False) -> JinjaInventory:
    """Inventory Jinja decisions and loops in the selected production scope."""
    inline_paths, template_paths = production_jinja_paths(roles, include_site=include_site)
    return inventory_jinja(inline_paths, template_paths)


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
        if (
            isinstance(document, list)
            and document
            and all(isinstance(entry, dict) and "hosts" in entry for entry in document)
        ):
            task_lists = [play.get(section) for play in document for section in ("pre_tasks", "tasks", "post_tasks")]
        else:
            task_lists = [document]
        for task_list in task_lists:
            for definition in _walk_task_definitions(task_list):
                if previous := tasks.get(definition.key):
                    raise ValueError(f"duplicate task source identity: {previous!r}, {definition!r}")
                tasks[definition.key] = definition
    return tasks


def inventory_blocks(paths: Iterable[Path]) -> dict[BlockKey, BlockDefinition]:
    """Load every production block with rescue or always control flow."""
    loader = DataLoader()
    blocks: dict[BlockKey, BlockDefinition] = {}
    for path in paths:
        document = loader.load_from_file(str(path.resolve()))
        for task in _document_task_mappings(document):
            if definition := _block_definition(task):
                if previous := blocks.get(definition.key):
                    raise ValueError(f"duplicate block source identity: {previous!r}, {definition!r}")
                blocks[definition.key] = definition
    return blocks


def inventory_untils(paths: Iterable[Path]) -> set[UntilKey]:
    """Load every production ``until`` predicate group."""
    loader = DataLoader()
    untils: set[UntilKey] = set()
    for path in paths:
        document = loader.load_from_file(str(path.resolve()))
        untils.update(until_key(value) for value in _walk_until_values(document))
    return untils


def inventory_result_predicates(paths: Iterable[Path]) -> set[ResultPredicateKey]:
    """Load every dynamic production ``changed_when`` and ``failed_when`` group."""
    loader = DataLoader()
    predicates: set[ResultPredicateKey] = set()
    for path in paths:
        document = loader.load_from_file(str(path.resolve()))
        for kind, value in _walk_result_predicate_values(document):
            if key := result_predicate_key(kind, value):
                predicates.add(key)
    return predicates


def inventory_includes(paths: Iterable[Path]) -> dict[IncludeKey, IncludeDefinition]:
    """Load every production dynamic include and its expected target."""
    loader = DataLoader()
    includes: dict[IncludeKey, IncludeDefinition] = {}
    for path in paths:
        document = loader.load_from_file(str(path.resolve()))
        for task in _document_task_mappings(document):
            if definition := _include_definition(task):
                if previous := includes.get(definition.key):
                    raise ValueError(f"duplicate include source identity: {previous!r}, {definition!r}")
                includes[definition.key] = definition
    return includes


def inventory_exits(paths: Iterable[Path]) -> dict[ExitKey, ExitDefinition]:
    """Load every production early role exit and its fallthrough task."""
    loader = DataLoader()
    exits: dict[ExitKey, ExitDefinition] = {}
    for path in paths:
        document = loader.load_from_file(str(path.resolve()))
        if (
            isinstance(document, list)
            and document
            and all(isinstance(entry, dict) and "hosts" in entry for entry in document)
        ):
            task_lists = [play.get(section) for play in document for section in ("pre_tasks", "tasks", "post_tasks")]
        else:
            task_lists = [document]
        for task_list in task_lists:
            for definition in _walk_exit_definitions(task_list, loader):
                if previous := exits.get(definition.key):
                    raise ValueError(f"duplicate early-exit source identity: {previous!r}, {definition!r}")
                exits[definition.key] = definition
    return exits


def append_outcomes(path: Path, outcomes: Iterable[ConditionOutcome], *, phase: str) -> None:
    """Append observed outcomes as compact JSON lines."""
    append_report_rows(path, (asdict(outcome) | {"phase": phase} for outcome in outcomes))


def append_report_rows(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Append complete JSONL records while excluding concurrent writers."""
    encoded = [(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode() for row in rows]
    if not encoded:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab", buffering=0) as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            for row in encoded:
                handle.write(row)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def append_report_provenance(path: Path, provenance: CoverageProvenance) -> None:
    """Start a report segment with its schema, source commit, and architecture."""
    append_report_rows(path, [{"provenance": asdict(provenance)}])


def append_report_error(path: Path, error: str) -> None:
    """Append a callback or worker error to fail the aggregate closed."""
    append_report_rows(path, [{"error": error}])


def append_jinja_branch_outcomes(path: Path, outcomes: Iterable[JinjaBranchOutcome], *, phase: str) -> None:
    """Append Jinja Boolean decisions observed during rendering."""
    append_report_rows(path, ({"jinja_branch": asdict(outcome), "phase": phase} for outcome in outcomes))


def append_jinja_loop_executions(path: Path, loops: Iterable[JinjaLoopKey], *, phase: str) -> None:
    """Append Jinja loops observed entering their body."""
    append_report_rows(path, ({"jinja_loop": asdict(loop), "phase": phase} for loop in loops))


def append_loop_executions(path: Path, loops: Iterable[LoopKey], *, phase: str) -> None:
    """Append loop declarations observed through per-item callbacks."""
    append_report_rows(path, ({"loop": asdict(loop), "phase": phase} for loop in loops))


def append_task_executions(path: Path, tasks: Iterable[TaskKey], *, phase: str) -> None:
    """Append tasks observed through non-skipped terminal callbacks."""
    append_report_rows(path, ({"task": asdict(task), "phase": phase} for task in tasks))


def append_block_events(path: Path, events: Iterable[BlockEvent], *, phase: str) -> None:
    """Append observed block section callbacks."""
    append_report_rows(path, ({"block_event": asdict(event), "phase": phase} for event in events))


def append_until_outcomes(path: Path, outcomes: Iterable[UntilOutcome], *, phase: str) -> None:
    """Append observed ``until`` predicate outcomes."""
    append_report_rows(path, ({"until": asdict(outcome), "phase": phase} for outcome in outcomes))


def append_result_predicate_outcomes(
    path: Path,
    outcomes: Iterable[ResultPredicateOutcome],
    *,
    phase: str,
) -> None:
    """Append observed dynamic task-result predicate outcomes."""
    append_report_rows(
        path,
        ({"result_predicate": asdict(outcome), "phase": phase} for outcome in outcomes),
    )


def append_include_events(path: Path, events: Iterable[IncludeEvent], *, phase: str) -> None:
    """Append observed dynamic include expansions."""
    append_report_rows(path, ({"include_event": asdict(event), "phase": phase} for event in events))


def append_exit_outcomes(path: Path, outcomes: Iterable[ExitOutcome], *, phase: str) -> None:
    """Append observed early role exit outcomes."""
    append_report_rows(path, ({"exit": asdict(outcome), "phase": phase} for outcome in outcomes))


def _report_rows(paths: Iterable[Path]) -> Iterator[tuple[Path, int, dict[str, Any]]]:
    expected_schema: int | None = None
    expected_source_sha: str | None = None
    for path in paths:
        report_architecture: str | None = None
        provenance_seen = False
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                data: dict[str, Any] = json.loads(line)
                if raw_provenance := data.get("provenance"):
                    try:
                        provenance = CoverageProvenance(**raw_provenance)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{path}:{line_number}: invalid coverage provenance: {exc}") from exc
                    if type(provenance.schema) is not int:
                        raise ValueError(f"{path}:{line_number}: coverage schema must be an integer")
                    if not re.fullmatch(r"[0-9a-f]{40,64}", provenance.source_sha):
                        raise ValueError(f"{path}:{line_number}: invalid coverage source SHA")
                    if not provenance.architecture:
                        raise ValueError(f"{path}:{line_number}: coverage architecture is empty")
                    if expected_schema is None:
                        expected_schema = provenance.schema
                    elif provenance.schema != expected_schema:
                        raise ValueError(
                            f"{path}:{line_number}: mixed coverage schemas: {expected_schema} and {provenance.schema}"
                        )
                    if provenance.schema != COVERAGE_SCHEMA_VERSION:
                        raise ValueError(
                            f"{path}:{line_number}: unsupported coverage schema {provenance.schema}; "
                            f"expected {COVERAGE_SCHEMA_VERSION}"
                        )
                    if expected_source_sha is None:
                        expected_source_sha = provenance.source_sha
                    elif provenance.source_sha != expected_source_sha:
                        raise ValueError(
                            f"{path}:{line_number}: mixed coverage source SHAs: "
                            f"{expected_source_sha} and {provenance.source_sha}"
                        )
                    if report_architecture is None:
                        report_architecture = provenance.architecture
                    elif provenance.architecture != report_architecture:
                        raise ValueError(
                            f"{path}:{line_number}: mixed coverage architectures in one report: "
                            f"{report_architecture} and {provenance.architecture}"
                        )
                    provenance_seen = True
                    continue
                if error := data.get("error"):
                    raise ValueError(f"{path}:{line_number}: callback error: {error}")
                if not provenance_seen:
                    raise ValueError(f"{path}:{line_number}: unprovenanced coverage record")
                if not {
                    "condition",
                    "loop",
                    "task",
                    "block_event",
                    "until",
                    "result_predicate",
                    "include_event",
                    "exit",
                    "jinja_branch",
                    "jinja_loop",
                }.intersection(data):
                    raise ValueError(f"{path}:{line_number}: unknown coverage record")
                yield path, line_number, data
        if not provenance_seen:
            raise ValueError(f"{path}: coverage report has no provenance")


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


def load_block_events(paths: Iterable[Path]) -> set[BlockEvent]:
    """Merge block events from callback JSONL reports."""
    events: set[BlockEvent] = set()
    for _path, _line_number, data in _report_rows(paths):
        if "block_event" not in data:
            continue
        event = data["block_event"]
        events.add(
            BlockEvent(
                block=BlockKey(**event["block"]),
                task=TaskKey(**event["task"]),
                section=event["section"],
                status=event["status"],
                after=event.get("after"),
            )
        )
    return events


def load_until_outcomes(paths: Iterable[Path]) -> dict[UntilKey, set[bool]]:
    """Merge ``until`` outcomes from callback JSONL reports."""
    outcomes: dict[UntilKey, set[bool]] = {}
    for _path, _line_number, data in _report_rows(paths):
        if "until" not in data:
            continue
        outcome = data["until"]
        key = UntilKey(**outcome["until"])
        outcomes.setdefault(key, set()).add(bool(outcome["outcome"]))
    return outcomes


def load_result_predicate_outcomes(
    paths: Iterable[Path],
) -> dict[ResultPredicateKey, set[bool]]:
    """Merge dynamic task-result predicate outcomes from callback reports."""
    outcomes: dict[ResultPredicateKey, set[bool]] = {}
    for _path, _line_number, data in _report_rows(paths):
        if "result_predicate" not in data:
            continue
        outcome = data["result_predicate"]
        raw_key = outcome["predicate"]
        key = ResultPredicateKey(
            kind=raw_key["kind"],
            path=raw_key["path"],
            line=raw_key["line"],
            column=raw_key["column"],
            expressions=tuple(raw_key["expressions"]),
        )
        outcomes.setdefault(key, set()).add(bool(outcome["outcome"]))
    return outcomes


def load_include_events(paths: Iterable[Path]) -> set[IncludeEvent]:
    """Merge dynamic include expansions from callback reports."""
    events: set[IncludeEvent] = set()
    for _path, _line_number, data in _report_rows(paths):
        if "include_event" not in data:
            continue
        event = data["include_event"]
        key = IncludeKey(**event["include"])
        events.add(IncludeEvent(key, event["target"]))
    return events


def load_exit_outcomes(paths: Iterable[Path]) -> dict[ExitKey, set[bool]]:
    """Merge early role exit outcomes from callback reports."""
    outcomes: dict[ExitKey, set[bool]] = {}
    for _path, _line_number, data in _report_rows(paths):
        if "exit" not in data:
            continue
        outcome = data["exit"]
        key = ExitKey(**outcome["exit"])
        outcomes.setdefault(key, set()).add(bool(outcome["outcome"]))
    return outcomes


def load_jinja_branch_outcomes(paths: Iterable[Path]) -> dict[JinjaBranchKey, set[bool]]:
    """Merge Jinja decision outcomes from callback reports."""
    outcomes: dict[JinjaBranchKey, set[bool]] = {}
    for _path, _line_number, data in _report_rows(paths):
        if "jinja_branch" not in data:
            continue
        outcome = data["jinja_branch"]
        key = JinjaBranchKey(**outcome["branch"])
        outcomes.setdefault(key, set()).add(bool(outcome["outcome"]))
    return outcomes


def load_executed_jinja_loops(paths: Iterable[Path]) -> set[JinjaLoopKey]:
    """Merge Jinja loops whose body was entered in any report."""
    return {
        JinjaLoopKey(**data["jinja_loop"]) for _path, _line_number, data in _report_rows(paths) if "jinja_loop" in data
    }


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
        kind = scenario.get("kind", "when")
        if kind not in {"when", "until", "changed_when", "failed_when", "task"}:
            raise ValueError(f"{path}: scenario {index} has unknown kind: {kind}")
        if kind != "when":
            continue
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


def load_synthetic_result_predicate_outcomes(
    path: Path,
    predicates: set[ResultPredicateKey] | None = None,
) -> dict[ResultPredicateKey, set[bool]]:
    """Evaluate declared synthetic cases for dynamic result predicates."""
    document = yaml.safe_load(path.read_text()) or {}
    scenarios = document.get("scenarios", [])
    if not isinstance(scenarios, list):
        raise ValueError(f"{path}: scenarios must be a list")

    if predicates is None:
        predicates = inventory_result_predicates(production_condition_paths(include_site=True))
    outcomes: dict[ResultPredicateKey, set[bool]] = {}
    loader = DataLoader()
    for index, scenario in enumerate(scenarios, 1):
        if not isinstance(scenario, dict):
            raise ValueError(f"{path}: scenario {index} must be a mapping")
        kind = scenario.get("kind", "when")
        if kind not in {"when", "until", "changed_when", "failed_when", "task"}:
            raise ValueError(f"{path}: scenario {index} has unknown kind: {kind}")
        if kind not in {"changed_when", "failed_when"}:
            continue
        source_path = str(scenario.get("path", ""))
        raw_expressions = scenario.get("expression", "")
        expressions = raw_expressions if isinstance(raw_expressions, list) else [raw_expressions]
        normalized_expressions = tuple(_normalized_expression(str(expression)) for expression in expressions)
        source_line = scenario.get("line")
        if source_line is not None and not isinstance(source_line, int):
            raise ValueError(f"{path}: scenario {index} line must be an integer")
        all_matches = scenario.get("all", False)
        if not isinstance(all_matches, bool):
            raise ValueError(f"{path}: scenario {index} all must be a Boolean")
        matches = {
            predicate
            for predicate in predicates
            if predicate.kind == kind
            and predicate.path == source_path
            and tuple(_normalized_expression(expression) for expression in predicate.expressions)
            == normalized_expressions
            and (source_line is None or predicate.line == source_line)
        }
        if not matches:
            joined = ", ".join(normalized_expressions)
            raise ValueError(
                f"{path}: scenario {index} does not match a current {kind} predicate: {source_path}: {joined}"
            )
        if len(matches) > 1 and not all_matches:
            lines = ", ".join(str(predicate.line) for predicate in sorted(matches))
            raise ValueError(f"{path}: scenario {index} matches lines {lines}; select one with line or set all: true")

        cases = scenario.get("cases", [])
        if not isinstance(cases, list) or not cases:
            raise ValueError(f"{path}: scenario {index} cases must be a non-empty list")
        source_expressions = next(iter(matches)).expressions
        trusted_expressions = [TrustedAsTemplate().tag(expression) for expression in source_expressions]
        for case_index, case in enumerate(cases, 1):
            if not isinstance(case, dict) or not isinstance(case.get("outcome"), bool):
                raise ValueError(f"{path}: scenario {index} case {case_index} requires a Boolean outcome")
            variables = case.get("variables", {})
            if not isinstance(variables, dict):
                raise ValueError(f"{path}: scenario {index} case {case_index} variables must be a mapping")
            engine = TemplateEngine(loader, variables=variables)
            actual = all(engine.evaluate_conditional(expression) for expression in trusted_expressions)
            if actual is not case["outcome"]:
                raise ValueError(
                    f"{path}: scenario {index} case {case_index} expected {case['outcome']} but evaluated {actual}"
                )
            for predicate in matches:
                outcomes.setdefault(predicate, set()).add(actual)
    return outcomes


def load_synthetic_until_outcomes(
    path: Path,
    untils: set[UntilKey] | None = None,
) -> dict[UntilKey, set[bool]]:
    """Evaluate declared synthetic cases against current ``until`` predicates."""
    document = yaml.safe_load(path.read_text()) or {}
    scenarios = document.get("scenarios", [])
    if not isinstance(scenarios, list):
        raise ValueError(f"{path}: scenarios must be a list")

    if untils is None:
        untils = inventory_untils(production_condition_paths(include_site=True))
    outcomes: dict[UntilKey, set[bool]] = {}
    loader = DataLoader()
    for index, scenario in enumerate(scenarios, 1):
        if not isinstance(scenario, dict):
            raise ValueError(f"{path}: scenario {index} must be a mapping")
        kind = scenario.get("kind", "when")
        if kind not in {"when", "until", "changed_when", "failed_when", "task"}:
            raise ValueError(f"{path}: scenario {index} has unknown kind: {kind}")
        if kind != "until":
            continue
        source_path = str(scenario.get("path", ""))
        expression = _normalized_expression(str(scenario.get("expression", "")))
        source_line = scenario.get("line")
        if source_line is not None and not isinstance(source_line, int):
            raise ValueError(f"{path}: scenario {index} line must be an integer")
        all_matches = scenario.get("all", False)
        if not isinstance(all_matches, bool):
            raise ValueError(f"{path}: scenario {index} all must be a Boolean")
        matches = {
            until
            for until in untils
            if until.path == source_path
            and _normalized_expression(until.expression) == expression
            and (source_line is None or until.line == source_line)
        }
        if not matches:
            raise ValueError(
                f"{path}: scenario {index} does not match a current until predicate: {source_path}: {expression}"
            )
        if len(matches) > 1 and not all_matches:
            lines = ", ".join(str(until.line) for until in sorted(matches))
            raise ValueError(f"{path}: scenario {index} matches lines {lines}; select one with line or set all: true")

        cases = scenario.get("cases", [])
        if not isinstance(cases, list) or not cases:
            raise ValueError(f"{path}: scenario {index} cases must be a non-empty list")
        trusted_expression = TrustedAsTemplate().tag(next(iter(matches)).expression)
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
            for until in matches:
                outcomes.setdefault(until, set()).add(actual)
    return outcomes


def load_synthetic_task_reachability(
    path: Path,
    tasks: dict[TaskKey, TaskDefinition] | None = None,
) -> set[TaskKey]:
    """Prove selected conditional tasks reachable under declared variables."""
    document = yaml.safe_load(path.read_text()) or {}
    scenarios = document.get("scenarios", [])
    if not isinstance(scenarios, list):
        raise ValueError(f"{path}: scenarios must be a list")

    if tasks is None:
        tasks = inventory_tasks(production_condition_paths(include_site=True))
    reachable: set[TaskKey] = set()
    loader = DataLoader()
    for index, scenario in enumerate(scenarios, 1):
        if not isinstance(scenario, dict):
            raise ValueError(f"{path}: scenario {index} must be a mapping")
        kind = scenario.get("kind", "when")
        if kind not in {"when", "until", "changed_when", "failed_when", "task"}:
            raise ValueError(f"{path}: scenario {index} has unknown kind: {kind}")
        if kind != "task":
            continue
        source_path = str(scenario.get("path", ""))
        candidates = [task for task in tasks.values() if task.key.path == source_path]
        if not candidates:
            raise ValueError(f"{path}: scenario {index} does not match a current task file: {source_path}")
        cases = scenario.get("cases", [])
        if not isinstance(cases, list) or not cases:
            raise ValueError(f"{path}: scenario {index} cases must be a non-empty list")
        for case_index, case in enumerate(cases, 1):
            if not isinstance(case, dict):
                raise ValueError(f"{path}: scenario {index} case {case_index} must be a mapping")
            names = case.get("tasks", [])
            if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
                raise ValueError(f"{path}: scenario {index} case {case_index} tasks must be a non-empty string list")
            if len(names) != len(set(names)):
                raise ValueError(f"{path}: scenario {index} case {case_index} repeats a task name")
            selected: list[TaskDefinition] = []
            for name in names:
                matches = [task for task in candidates if task.name == name]
                if len(matches) != 1:
                    raise ValueError(
                        f"{path}: scenario {index} case {case_index} task {name!r} matched {len(matches)} definitions"
                    )
                selected.append(matches[0])
            variables = case.get("variables", {})
            if not isinstance(variables, dict):
                raise ValueError(f"{path}: scenario {index} case {case_index} variables must be a mapping")
            engine = TemplateEngine(loader, variables=variables)
            for task in selected:
                if not task.conditions:
                    raise ValueError(
                        f"{path}: scenario {index} case {case_index} cannot synthesize unconditional task {task.name!r}"
                    )
                if not all(
                    engine.evaluate_conditional(TrustedAsTemplate().tag(condition.expression))
                    for condition in task.conditions
                ):
                    raise ValueError(
                        f"{path}: scenario {index} case {case_index} does not make task {task.name!r} reachable"
                    )
                reachable.add(task.key)
    return reachable


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
    """Render production tasks with neither runtime nor reachability coverage."""
    lines = [f"{len(missing)} Ansible task(s) neither executed nor proved synthetically reachable:"]
    lines.extend(f"  {task.key.path}:{task.key.line}: [{task.action}] {task.name}" for task in sorted(missing))
    return "\n".join(lines)


def format_block_gaps(missing: set[BlockGap]) -> str:
    """Render unobserved block control-flow outcomes."""
    lines = [f"{len(missing)} Ansible block outcome(s) lack coverage:"]
    lines.extend(
        f"  {gap.block.key.path}:{gap.block.key.line}: missing {gap.outcome}: {gap.block.name}"
        for gap in sorted(missing)
    )
    return "\n".join(lines)


def format_missing_until_outcomes(missing: dict[UntilKey, set[bool]]) -> str:
    """Render retry predicates missing their false or true outcome."""
    lines = [f"{len(missing)} Ansible until predicate(s) lack retry branch coverage:"]
    for until, outcomes in sorted(missing.items()):
        labels = ", ".join(str(outcome).lower() for outcome in sorted(outcomes))
        expression = " ".join(until.expression.split())
        lines.append(f"  {until.path}:{until.line}:{until.column}: missing {labels}: {expression}")
    return "\n".join(lines)


def format_missing_result_predicate_outcomes(
    missing: dict[ResultPredicateKey, set[bool]],
) -> str:
    """Render result predicates missing their false or true outcome."""
    lines = [f"{len(missing)} Ansible result predicate(s) lack Boolean branch coverage:"]
    for predicate, outcomes in sorted(missing.items()):
        labels = ", ".join(str(outcome).lower() for outcome in sorted(outcomes))
        expressions = " and ".join(" ".join(expression.split()) for expression in predicate.expressions)
        lines.append(
            f"  {predicate.path}:{predicate.line}:{predicate.column}: {predicate.kind} missing {labels}: {expressions}"
        )
    return "\n".join(lines)


def format_unexpanded_includes(missing: set[IncludeDefinition]) -> str:
    """Render dynamic includes that never expanded to their expected target."""
    lines = [f"{len(missing)} Ansible dynamic include(s) never expanded to the expected target:"]
    lines.extend(
        f"  {include.key.path}:{include.key.line}: [{include.key.action}] {include.name} -> {include.target}"
        for include in sorted(missing)
    )
    return "\n".join(lines)


def format_exit_gaps(missing: set[ExitGap]) -> str:
    """Render early role exits missing their exit or fallthrough path."""
    lines = [f"{len(missing)} Ansible early-exit path(s) lack coverage:"]
    lines.extend(
        f"  {gap.exit.key.path}:{gap.exit.key.line}: missing {gap.outcome}: {gap.exit.name}" for gap in sorted(missing)
    )
    return "\n".join(lines)


def _format_jinja_location(key: JinjaBranchKey | JinjaLoopKey) -> str:
    root = f"{key.path}:{key.root_line}:{key.root_column}"
    return f"{root} template-line {key.template_line}"


def format_missing_jinja_branch_outcomes(missing: dict[JinjaBranchKey, set[bool]]) -> str:
    """Render Jinja decisions missing their false or true outcome."""
    lines = [f"{len(missing)} Jinja branch(es) lack Boolean coverage:"]
    for branch, outcomes in sorted(missing.items()):
        labels = ", ".join(str(outcome).lower() for outcome in sorted(outcomes))
        lines.append(f"  {_format_jinja_location(branch)}: {branch.kind} missing {labels}: {branch.expression}")
    return "\n".join(lines)


def format_unexecuted_jinja_loops(missing: set[JinjaLoopKey]) -> str:
    """Render Jinja loops that never entered their body."""
    lines = [f"{len(missing)} Jinja loop(s) never iterated:"]
    lines.extend(f"  {_format_jinja_location(loop)}: {loop.expression}" for loop in sorted(missing))
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
    scenario_path: Path | None = SYNTHETIC_SCENARIOS_PATH,
) -> set[TaskDefinition]:
    """Return tasks neither executed nor proven reachable as a conditional path."""
    scope = {path.as_posix() for path in production_condition_paths(roles, include_site=include_site)}
    tasks = inventory_tasks(production_condition_paths(include_site=True))
    expected = {key: task for key, task in tasks.items() if task.key.path in scope}
    synthetic = (
        load_synthetic_task_reachability(scenario_path, tasks)
        if scenario_path is not None and scenario_path.exists()
        else set()
    )
    covered = load_executed_tasks(reports).union(synthetic)
    missing_keys = set(expected).difference(covered)
    return {expected[key] for key in missing_keys}


def check_block_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
) -> set[BlockGap]:
    """Return branch-bearing block outcomes absent from all reports."""
    expected = inventory_blocks(production_condition_paths(roles, include_site=include_site))
    events = load_block_events(reports)
    gaps: set[BlockGap] = set()
    for block in expected.values():
        block_events = {event for event in events if event.block == block.key}
        normal_covered = (
            any(event.section == "always" and event.after == "normal" for event in block_events)
            if block.has_always
            else any(
                event.section == "block" and event.task == block.normal_terminal and event.status in {"ok", "skipped"}
                for event in block_events
            )
        )
        if not normal_covered:
            gaps.add(BlockGap(block, "normal"))
        if block.has_rescue and not any(event.section == "rescue" for event in block_events):
            gaps.add(BlockGap(block, "rescue"))
        if block.has_always:
            for predecessor in ("normal", "rescued" if block.has_rescue else "failed"):
                if not any(event.section == "always" and event.after == predecessor for event in block_events):
                    gaps.add(BlockGap(block, f"always_after_{predecessor}"))
    return gaps


def check_until_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
    scenario_path: Path | None = SYNTHETIC_SCENARIOS_PATH,
) -> dict[UntilKey, set[bool]]:
    """Return false/true outcomes absent for production retry predicates."""
    scope = {path.as_posix() for path in production_condition_paths(roles, include_site=include_site)}
    untils = inventory_untils(production_condition_paths(include_site=True))
    expected = {until for until in untils if until.path in scope}
    observed = load_until_outcomes(reports)
    synthetic = (
        load_synthetic_until_outcomes(scenario_path, untils)
        if scenario_path is not None and scenario_path.exists()
        else {}
    )
    for until, outcomes in synthetic.items():
        observed.setdefault(until, set()).update(outcomes)
    both = {False, True}
    return {
        until: both.difference(observed.get(until, set())) for until in expected if observed.get(until, set()) != both
    }


def check_result_predicate_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
    scenario_path: Path | None = SYNTHETIC_SCENARIOS_PATH,
) -> dict[ResultPredicateKey, set[bool]]:
    """Return false/true outcomes absent for dynamic result predicates."""
    scope = {path.as_posix() for path in production_condition_paths(roles, include_site=include_site)}
    predicates = inventory_result_predicates(production_condition_paths(include_site=True))
    expected = {predicate for predicate in predicates if predicate.path in scope}
    synthetic = (
        load_synthetic_result_predicate_outcomes(scenario_path, predicates)
        if scenario_path is not None and scenario_path.exists()
        else {}
    )
    observed = load_result_predicate_outcomes(reports)
    for predicate, outcomes in synthetic.items():
        observed.setdefault(predicate, set()).update(outcomes)
    both = {False, True}
    return {
        predicate: both.difference(observed.get(predicate, set()))
        for predicate in expected
        if observed.get(predicate, set()) != both
    }


def check_include_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
) -> set[IncludeDefinition]:
    """Return dynamic includes not observed expanding to their expected target."""
    expected = inventory_includes(production_condition_paths(roles, include_site=include_site))
    observed = load_include_events(reports)
    return {
        definition
        for definition in expected.values()
        if IncludeEvent(definition.key, definition.target) not in observed
    }


def check_exit_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
) -> set[ExitGap]:
    """Return early role exits missing their exit or fallthrough path."""
    expected = inventory_exits(production_condition_paths(roles, include_site=include_site))
    outcomes = load_exit_outcomes(reports)
    executed_tasks = load_executed_tasks(reports)
    gaps: set[ExitGap] = set()
    for definition in expected.values():
        observed = outcomes.get(definition.key, set())
        if True not in observed:
            gaps.add(ExitGap(definition, "exit"))
        if False not in observed or definition.fallthrough not in executed_tasks:
            gaps.add(ExitGap(definition, "fallthrough"))
    return gaps


def check_jinja_branch_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
) -> dict[JinjaBranchKey, set[bool]]:
    """Return false/true outcomes absent for production Jinja decisions."""
    expected = inventory_role_jinja(roles, include_site=include_site).branches
    observed = load_jinja_branch_outcomes(reports)
    both = {False, True}
    return {
        branch: both.difference(observed.get(branch, set()))
        for branch in expected
        if observed.get(branch, set()) != both
    }


def check_jinja_loop_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
) -> set[JinjaLoopKey]:
    """Return production Jinja loops that never entered their body."""
    expected = inventory_role_jinja(roles, include_site=include_site).loops
    return set(expected).difference(load_executed_jinja_loops(reports))


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
        block_gaps = check_block_coverage(args.roles, args.reports, include_site=args.include_site)
        missing_until_outcomes = check_until_coverage(args.roles, args.reports, include_site=args.include_site)
        missing_result_predicate_outcomes = check_result_predicate_coverage(
            args.roles,
            args.reports,
            include_site=args.include_site,
        )
        unexpanded_includes = check_include_coverage(args.roles, args.reports, include_site=args.include_site)
        exit_gaps = check_exit_coverage(args.roles, args.reports, include_site=args.include_site)
        missing_jinja_branch_outcomes = check_jinja_branch_coverage(
            args.roles,
            args.reports,
            include_site=args.include_site,
        )
        unexecuted_jinja_loops = check_jinja_loop_coverage(
            args.roles,
            args.reports,
            include_site=args.include_site,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Condition coverage report error: {exc}", file=sys.stderr)
        return 1
    if missing:
        print(format_missing_outcomes(missing), file=sys.stderr)
    if unexecuted_loops:
        print(format_unexecuted_loops(unexecuted_loops), file=sys.stderr)
    if unexecuted_tasks:
        print(format_unexecuted_tasks(unexecuted_tasks), file=sys.stderr)
    if block_gaps:
        print(format_block_gaps(block_gaps), file=sys.stderr)
    if missing_until_outcomes:
        print(format_missing_until_outcomes(missing_until_outcomes), file=sys.stderr)
    if missing_result_predicate_outcomes:
        print(
            format_missing_result_predicate_outcomes(missing_result_predicate_outcomes),
            file=sys.stderr,
        )
    if unexpanded_includes:
        print(format_unexpanded_includes(unexpanded_includes), file=sys.stderr)
    if exit_gaps:
        print(format_exit_gaps(exit_gaps), file=sys.stderr)
    if missing_jinja_branch_outcomes:
        print(format_missing_jinja_branch_outcomes(missing_jinja_branch_outcomes), file=sys.stderr)
    if unexecuted_jinja_loops:
        print(format_unexecuted_jinja_loops(unexecuted_jinja_loops), file=sys.stderr)
    if (
        missing
        or unexecuted_loops
        or unexecuted_tasks
        or block_gaps
        or missing_until_outcomes
        or missing_result_predicate_outcomes
        or unexpanded_includes
        or exit_gaps
        or missing_jinja_branch_outcomes
        or unexecuted_jinja_loops
    ):
        return 1
    print(
        "Every selected Ansible condition evaluated both true and false, every loop iterated, "
        "every task ran or was proved synthetically reachable, every block path executed, "
        "every retry and result predicate evaluated false and true, "
        "every dynamic include expanded, and every early exit took both paths."
        " Every Jinja decision evaluated both true and false, and every Jinja loop iterated."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
