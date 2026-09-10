"""Collect and validate branch outcomes for Ansible ``when`` expressions."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ansible._internal._datatag._tags import Origin
from ansible.parsing.dataloader import DataLoader


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


def _walk_when_values(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) == "when":
                yield from child if isinstance(child, list) else [child]
            yield from _walk_when_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_when_values(child)


def production_condition_paths(roles: Iterable[str] | None = None, *, include_site: bool = False) -> list[Path]:
    """Return production task files in the requested coverage scope."""
    selected_roles = set(roles or ())
    paths = [
        path
        for path in Path("roles").glob("*/tasks/*.yml")
        if not path.name.startswith("_") and (not selected_roles or path.parts[1] in selected_roles)
    ]
    if include_site:
        paths.append(Path("site.yml"))
    return sorted(paths)


def inventory_conditions(paths: Iterable[Path]) -> set[ConditionKey]:
    """Load every declared ``when`` expression from production task files."""
    loader = DataLoader()
    conditions: set[ConditionKey] = set()
    for path in paths:
        document = loader.load_from_file(str(path.resolve()))
        conditions.update(condition_key(value) for value in _walk_when_values(document))
    return conditions


def append_outcomes(path: Path, outcomes: Iterable[ConditionOutcome], *, phase: str) -> None:
    """Append observed outcomes as compact JSON lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for outcome in outcomes:
            handle.write(json.dumps(asdict(outcome) | {"phase": phase}, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def load_outcomes(paths: Iterable[Path]) -> dict[ConditionKey, set[bool]]:
    """Merge condition outcomes from callback JSONL reports."""
    merged: dict[ConditionKey, set[bool]] = {}
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                data: dict[str, Any] = json.loads(line)
                if error := data.get("error"):
                    raise ValueError(f"{path}:{line_number}: callback error: {error}")
                key = ConditionKey(**data["condition"])
                merged.setdefault(key, set()).add(bool(data["outcome"]))
    return merged
