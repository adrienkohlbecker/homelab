"""Collect and validate branch outcomes for Ansible ``when`` expressions."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml
from ansible._internal._datatag._tags import Origin, TrustedAsTemplate
from ansible._internal._templating._engine import TemplateEngine
from ansible.parsing.dataloader import DataLoader

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
    selected_roles = None if roles is None else set(roles)
    paths = [
        path
        for path in Path("roles").glob("*/tasks/*.yml")
        if not path.name.startswith("_") and (selected_roles is None or path.parts[1] in selected_roles)
    ]
    if include_site and Path("site.yml").exists():
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


def _normalized_expression(expression: str) -> str:
    return " ".join(expression.split())


def load_synthetic_outcomes(path: Path) -> dict[ConditionKey, set[bool]]:
    """Evaluate declared synthetic cases against current source expressions."""
    document = yaml.safe_load(path.read_text()) or {}
    scenarios = document.get("scenarios", [])
    if not isinstance(scenarios, list):
        raise ValueError(f"{path}: scenarios must be a list")

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
        all_matches = scenario.get("all", False)
        if not isinstance(all_matches, bool):
            raise ValueError(f"{path}: scenario {index} all must be a Boolean")
        matches = {
            condition
            for condition in conditions
            if condition.path == source_path
            and _normalized_expression(condition.expression) == expression
            and (source_line is None or condition.line == source_line)
        }
        if not matches:
            raise ValueError(
                f"{path}: scenario {index} does not match a current condition: {source_path}: {expression}"
            )
        if len(matches) > 1 and not all_matches:
            lines = ", ".join(str(condition.line) for condition in sorted(matches))
            raise ValueError(
                f"{path}: scenario {index} matches lines {lines}; select one with line or set all: true"
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


def check_coverage(
    roles: Iterable[str],
    reports: Iterable[Path],
    *,
    include_site: bool = False,
    scenario_path: Path | None = SYNTHETIC_SCENARIOS_PATH,
) -> dict[ConditionKey, set[bool]]:
    """Compare production conditions with outcomes merged from test reports."""
    expected = inventory_conditions(production_condition_paths(roles, include_site=include_site))
    synthetic = load_synthetic_outcomes(scenario_path) if scenario_path is not None and scenario_path.exists() else {}
    observed = merge_outcomes(load_outcomes(reports), synthetic)
    return missing_outcomes(expected, observed)


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
    """Check condition coverage from the command line."""
    args = _parse_args()
    try:
        missing = check_coverage(args.roles, args.reports, include_site=args.include_site)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Condition coverage report error: {exc}", file=sys.stderr)
        return 1
    if missing:
        print(format_missing_outcomes(missing), file=sys.stderr)
        return 1
    print("Every selected Ansible condition evaluated both true and false.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
