# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# cspell:words orelse

import ast
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

LINE_LIMIT = 300
INIT = ".__init__"
TYPE_GUARDS = {
    f"{prefix}TYPE_CHECKING" for prefix in ("", "typing.", "typing_extensions.")
}

type ModuleMetrics = dict[str, int | float]
type Graph = dict[str, set[str]]


def aliases(nodes: Iterable[ast.AST]) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in nodes:
        match node:
            case ast.Import(names=items):
                for item in items:
                    root = item.name.partition(".")[0]
                    names[item.asname or root] = item.name if item.asname else root
            case ast.ImportFrom(level=0, module=module, names=items):
                names.update((i.asname or i.name, f"{module}.{i.name}") for i in items)
            case _:
                continue
    return names


def qualified(node: ast.AST | None, names: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return names.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{qualified(node.value, names)}.{node.attr}"
    return ""


def prefixes(name: str) -> Iterator[str]:
    while name:
        yield name
        name = name.rpartition(".")[0]


def type_only(test: ast.expr, names: dict[str, str]) -> bool:
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(type_only(value, names) for value in test.values)
    return qualified(test, names) in TYPE_GUARDS


def runtime_imports(tree: ast.Module) -> Iterator[ast.Import | ast.ImportFrom]:
    names = aliases(tree.body)
    pending: list[ast.AST] = [tree]
    while pending:
        match pending.pop():
            case ast.Import() | ast.ImportFrom() as node:
                yield node
            case ast.If(test=test, orelse=body) if type_only(test, names):
                pending.extend(body)
            case ast.If(test=ast.UnaryOp(op=ast.Not(), operand=test), body=body) if (
                type_only(test, names)
            ):
                pending.extend(body)
            case node:
                pending.extend(ast.iter_child_nodes(node))


def import_targets(name: str, node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.Import):
        return {item.name for item in node.names}
    package = name.split(".")
    if node.level >= len(package):
        return set()
    parent = package[: -node.level] if node.level else []
    base = ".".join([*parent, node.module or ""]).rstrip(".")
    return {base, *(f"{base}.{item.name}" for item in node.names if item.name != "*")}


def dependencies(name: str, tree: ast.Module, modules: dict[str, str]) -> set[str]:
    return {
        modules[prefix]
        for node in runtime_imports(tree)
        for target in import_targets(name, node)
        for prefix in prefixes(target)
        if prefix in modules
    } - {name}


def reachable(graph: Graph, starts: set[str]) -> set[str]:
    seen = starts.copy()
    while starts:
        starts = set().union(*(graph[name] for name in starts)) - seen
        seen.update(starts)
    return seen


def structure(
    sources: dict[str, ast.Module], tests: dict[str, ast.Module]
) -> tuple[dict[str, ModuleMetrics], int]:
    modules = {name.removesuffix(INIT): name for name in sources}
    graph = {name: dependencies(name, tree, modules) for name, tree in sources.items()}
    importers = Counter(target for targets in graph.values() for target in targets)
    cumulative = sum(len(reachable(graph, {name})) - 1 for name in graph)
    helpers = {name.removesuffix(INIT): f"test:{name}" for name in tests}
    known = helpers | modules
    test_graph = {f"test:{n}": dependencies(n, t, known) for n, t in tests.items()}
    combined = graph | test_graph
    test_reach: Counter[str] = Counter()
    for root in helpers.values():
        test_reach.update(reachable(combined, combined[root]) & sources.keys())
    metrics = {}
    for name, edges in graph.items():
        incoming, outgoing = importers[name], len(edges)
        metrics[name] = {
            "fan_in": incoming,
            "fan_out": outgoing,
            "instability": outgoing / max(1, incoming + outgoing),
            "test_files": test_reach[name],
        }
    return metrics, cumulative
