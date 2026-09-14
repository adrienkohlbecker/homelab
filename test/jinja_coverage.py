"""Inventory and instrument Jinja control flow rendered by Ansible."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from ansible._internal._datatag._tags import Origin
from ansible._internal._templating._engine import TemplateEngine
from ansible._internal._templating._jinja_bits import AnsibleEnvironment, TemplateOverrides
from ansible.parsing.dataloader import DataLoader
from jinja2 import nodes
from jinja2.visitor import NodeTransformer, NodeVisitor


@dataclass(frozen=True, order=True)
class JinjaBranchKey:
    """Stable source identity for one Jinja Boolean decision."""

    path: str
    root_line: int
    root_column: int
    template_line: int
    ordinal: int
    kind: str
    expression: str


@dataclass(frozen=True)
class JinjaBranchOutcome:
    """One observed Boolean result for a Jinja decision."""

    branch: JinjaBranchKey
    outcome: bool


@dataclass(frozen=True, order=True)
class JinjaLoopKey:
    """Stable source identity for one Jinja ``for`` loop."""

    path: str
    root_line: int
    root_column: int
    template_line: int
    ordinal: int
    expression: str


@dataclass(frozen=True)
class JinjaInventory:
    """Jinja branch and loop declarations found in production sources."""

    branches: frozenset[JinjaBranchKey]
    loops: frozenset[JinjaLoopKey]


@dataclass
class _JinjaAnnotations:
    branches: dict[int, JinjaBranchKey]
    filters: dict[int, JinjaBranchKey]
    else_branches: dict[int, JinjaBranchKey]
    loops: dict[int, JinjaLoopKey]

    @property
    def inventory(self) -> JinjaInventory:
        return JinjaInventory(
            frozenset((*self.branches.values(), *self.filters.values(), *self.else_branches.values())),
            frozenset(self.loops.values()),
        )


def normalize_source_path(path: str) -> str:
    """Normalize original and staged Ansible paths to repository-relative paths."""
    normalized = Path(path).as_posix()
    for root in ("roles", "group_vars", "host_vars"):
        marker = f"/{root}/"
        if marker in normalized:
            return f"{root}/{normalized.split(marker, 1)[1]}"
        if normalized.startswith(f"{root}/"):
            return normalized
    if normalized.endswith("/site.yml") or normalized == "site.yml":
        return "site.yml"
    return normalized


def _source_identity(source: str, filename: str | None) -> tuple[str, int, int] | None:
    origin = Origin.get_tag(source)
    source_path = origin.path if origin is not None and origin.path else filename
    if not source_path:
        return None
    return (
        normalize_source_path(source_path),
        origin.line_num if origin is not None and origin.line_num is not None else 1,
        origin.col_num if origin is not None and origin.col_num is not None else 1,
    )


class _CoverageAnnotator(NodeVisitor):
    def __init__(self, source: str, identity: tuple[str, int, int]) -> None:
        self._source_lines = source.splitlines()
        self._path, self._root_line, self._root_column = identity
        self._ordinals: defaultdict[tuple[str, int], int] = defaultdict(int)
        self._elif_nodes: set[int] = set()
        self.annotations = _JinjaAnnotations({}, {}, {}, {})

    def annotate(self, tree: nodes.Template) -> None:
        self._elif_nodes = {id(child) for branch in tree.find_all(nodes.If) for child in branch.elif_}
        self.visit(tree)

    def _expression(self, line: int) -> str:
        if 0 < line <= len(self._source_lines):
            return " ".join(self._source_lines[line - 1].split())
        return ""

    def _branch_key(self, node: nodes.Node, kind: str) -> JinjaBranchKey:
        ordinal_key = kind, node.lineno
        ordinal = self._ordinals[ordinal_key]
        self._ordinals[ordinal_key] += 1
        return JinjaBranchKey(
            self._path,
            self._root_line,
            self._root_column,
            node.lineno,
            ordinal,
            kind,
            self._expression(node.lineno),
        )

    def _loop_key(self, node: nodes.For) -> JinjaLoopKey:
        ordinal_key = "for", node.lineno
        ordinal = self._ordinals[ordinal_key]
        self._ordinals[ordinal_key] += 1
        return JinjaLoopKey(
            self._path,
            self._root_line,
            self._root_column,
            node.lineno,
            ordinal,
            self._expression(node.lineno),
        )

    def visit_If(self, node: nodes.If, *args: Any, **kwargs: Any) -> None:
        self.annotations.branches[id(node)] = self._branch_key(
            node,
            "elif" if id(node) in self._elif_nodes else "if",
        )
        self.generic_visit(node, *args, **kwargs)

    def visit_CondExpr(self, node: nodes.CondExpr, *args: Any, **kwargs: Any) -> None:
        self.annotations.branches[id(node)] = self._branch_key(node, "ternary")
        self.generic_visit(node, *args, **kwargs)

    def visit_For(self, node: nodes.For, *args: Any, **kwargs: Any) -> None:
        self.annotations.loops[id(node)] = self._loop_key(node)
        if node.test is not None:
            self.annotations.filters[id(node)] = self._branch_key(node.test, "for_filter")
        if node.else_:
            self.annotations.else_branches[id(node)] = self._branch_key(node, "for_else")
        self.generic_visit(node, *args, **kwargs)


def _annotate_jinja_tree(tree: nodes.Template, source: str, filename: str | None = None) -> _JinjaAnnotations:
    if (identity := _source_identity(source, filename)) is None:
        return _JinjaAnnotations({}, {}, {}, {})
    annotator = _CoverageAnnotator(source, identity)
    annotator.annotate(tree)
    return annotator.annotations


def annotate_jinja_tree(tree: nodes.Template, source: str, filename: str | None = None) -> JinjaInventory:
    """Return deterministic coverage keys for one parsed Jinja tree."""
    return _annotate_jinja_tree(tree, source, filename).inventory


def _payload(key: JinjaBranchKey | JinjaLoopKey) -> tuple[object, ...]:
    return tuple(getattr(key, field) for field in key.__dataclass_fields__)


class _CoverageTransformer(NodeTransformer):
    def __init__(self, annotations: _JinjaAnnotations) -> None:
        self._annotations = annotations

    @staticmethod
    def _call(name: str, key: JinjaBranchKey | JinjaLoopKey, value: nodes.Expr | None = None) -> nodes.Call:
        args: list[nodes.Expr] = [nodes.Const(_payload(key))]
        if value is not None:
            args.append(value)
        return cast(
            nodes.Call,
            nodes.Call(nodes.EnvironmentAttribute(name), args, [], None, None).set_lineno(key.template_line),
        )

    @classmethod
    def _statement(cls, name: str, key: JinjaBranchKey | JinjaLoopKey) -> nodes.ExprStmt:
        return cast(nodes.ExprStmt, nodes.ExprStmt(cls._call(name, key)).set_lineno(key.template_line))

    def visit_If(self, node: nodes.If, *args: Any, **kwargs: Any) -> nodes.If:
        node = cast(nodes.If, self.generic_visit(node, *args, **kwargs))
        if key := self._annotations.branches.get(id(node)):
            node.test = self._call("_record_jinja_branch", key, cast(nodes.Expr, node.test))
        return node

    def visit_CondExpr(self, node: nodes.CondExpr, *args: Any, **kwargs: Any) -> nodes.CondExpr:
        node = cast(nodes.CondExpr, self.generic_visit(node, *args, **kwargs))
        if key := self._annotations.branches.get(id(node)):
            node.test = self._call("_record_jinja_branch", key, cast(nodes.Expr, node.test))
        return node

    def visit_For(self, node: nodes.For, *args: Any, **kwargs: Any) -> nodes.For:
        node = cast(nodes.For, self.generic_visit(node, *args, **kwargs))
        if key := self._annotations.filters.get(id(node)):
            node.test = self._call("_record_jinja_branch", key, cast(nodes.Expr, node.test))
        if key := self._annotations.loops.get(id(node)):
            node.body.insert(0, self._statement("_record_jinja_loop", key))
        if key := self._annotations.else_branches.get(id(node)):
            node.body.insert(0, nodes.ExprStmt(self._call("_record_jinja_branch", key, nodes.Const(True))))
            node.else_.insert(0, nodes.ExprStmt(self._call("_record_jinja_branch", key, nodes.Const(False))))
        return node


def instrument_jinja_tree(tree: nodes.Template, source: str, filename: str | None = None) -> JinjaInventory:
    """Annotate and instrument one parsed tree without changing rendered output."""
    annotations = _annotate_jinja_tree(tree, source, filename)
    _CoverageTransformer(annotations).visit(tree)
    return annotations.inventory


def _iter_inline_templates(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_inline_templates(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_inline_templates(child)
    elif isinstance(value, str) and ("{{" in value or "{%" in value):
        yield value


def _parse_inventory_source(source: str, loader: DataLoader) -> JinjaInventory:
    engine = TemplateEngine(loader)
    stripped, environment = engine._create_overlay(source, TemplateOverrides.DEFAULT)
    if Origin.get_tag(stripped) is None and (origin := Origin.get_tag(source)) is not None:
        stripped = replace(
            origin,
            line_num=(origin.line_num or 1) + 1,
            col_num=1,
        ).tag(stripped)
    tree = environment.parse(stripped)
    return annotate_jinja_tree(tree, stripped)


def inventory_jinja(
    inline_paths: Iterable[Path],
    template_paths: Iterable[Path],
) -> JinjaInventory:
    """Inventory control flow in inline strings and standalone templates."""
    loader = DataLoader()
    branches: set[JinjaBranchKey] = set()
    loops: set[JinjaLoopKey] = set()
    sources: list[str] = []
    for path in inline_paths:
        document = loader.load_from_file(str(path.resolve()))
        sources.extend(_iter_inline_templates(document))
    sources.extend(loader.get_text_file_contents(str(path.resolve())) for path in template_paths)
    for source in sources:
        inventory = _parse_inventory_source(source, loader)
        branches.update(inventory.branches)
        loops.update(inventory.loops)
    return JinjaInventory(frozenset(branches), frozenset(loops))


BranchSink = Callable[[JinjaBranchOutcome], None]
LoopSink = Callable[[JinjaLoopKey], None]


def install_jinja_coverage(branch_sink: BranchSink, loop_sink: LoopSink) -> None:
    """Instrument every subsequently compiled Ansible Jinja template."""
    if getattr(AnsibleEnvironment, "_jinja_coverage_installed", False):
        return
    original_parse = AnsibleEnvironment._parse
    original_create_overlay = TemplateEngine._create_overlay
    seen_branches: set[tuple[int, JinjaBranchKey, bool]] = set()
    seen_loops: set[tuple[int, JinjaLoopKey]] = set()

    def record_branch(payload: tuple[Any, ...], value: object) -> bool:
        outcome = bool(value)
        key = JinjaBranchKey(*payload)
        marker = os.getpid(), key, outcome
        if marker not in seen_branches:
            branch_sink(JinjaBranchOutcome(key, outcome))
            seen_branches.add(marker)
        return outcome

    def record_loop(payload: tuple[Any, ...]) -> None:
        key = JinjaLoopKey(*payload)
        marker = os.getpid(), key
        if marker not in seen_loops:
            loop_sink(key)
            seen_loops.add(marker)

    def parse(self: AnsibleEnvironment, source: str, *args: Any, **kwargs: Any) -> nodes.Template:
        tree = original_parse(self, source, *args, **kwargs)
        filename = kwargs.get("filename") or (args[1] if len(args) > 1 else None)
        instrument_jinja_tree(tree, source, filename)
        return tree

    def create_overlay(
        self: TemplateEngine,
        template: str,
        overrides: TemplateOverrides,
    ) -> tuple[str, AnsibleEnvironment]:
        stripped, environment = original_create_overlay(self, template, overrides)
        if Origin.get_tag(stripped) is None and (origin := Origin.get_tag(template)) is not None:
            stripped = replace(
                origin,
                line_num=(origin.line_num or 1) + 1,
                col_num=1,
            ).tag(stripped)
        return stripped, environment

    AnsibleEnvironment._record_jinja_branch = staticmethod(record_branch)  # type: ignore[attr-defined]
    AnsibleEnvironment._record_jinja_loop = staticmethod(record_loop)  # type: ignore[attr-defined]
    AnsibleEnvironment._parse = parse  # type: ignore[method-assign]
    AnsibleEnvironment._jinja_coverage_installed = True  # type: ignore[attr-defined]
    TemplateEngine._create_overlay = create_overlay  # type: ignore[method-assign]
