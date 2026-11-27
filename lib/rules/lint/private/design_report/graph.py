# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import ast
from collections import Counter
from collections.abc import Iterator

LINE_LIMIT = 300
INIT = ".__init__"
TYPE_GUARDS = {"TYPE_CHECKING", "typing.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING"}

type Counts = dict[str, int]
type ModuleMetrics = dict[str, int | float]
type Graph = dict[str, set[str]]


def aliases(tree: ast.Module) -> dict[str, str]:
    names = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                root = item.name.split(".")[0]
                names[item.asname or root] = item.name if item.asname else root
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for item in node.names:
                names[item.asname or item.name] = f"{node.module}.{item.name}"
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
    names = aliases(tree)
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If) and type_only(node.test, names):
            pending.extend(node.orelse)
        elif (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and type_only(node.test.operand, names)
        ):
            pending.extend(node.body)
        else:
            pending.extend(ast.iter_child_nodes(node))


def dependencies(name: str, tree: ast.Module, modules: dict[str, str]) -> set[str]:
    targets = set()
    for node in runtime_imports(tree):
        if isinstance(node, ast.Import):
            targets.update(item.name for item in node.names)
        else:
            package = name.split(".")[:-1]
            if node.level > len(package):
                continue
            parent = package[: len(package) - node.level + 1] if node.level else []
            base = ".".join([*parent, node.module or ""]).rstrip(".")
            targets.add(base)
            targets.update(f"{base}.{item.name}" for item in node.names if item.name != "*")
    return {
        modules[prefix] for target in targets for prefix in prefixes(target) if prefix in modules
    } - {name}


def reachable(graph: Graph, starts: set[str]) -> set[str]:
    seen: set[str] = set()
    pending = list(starts)
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(graph[name] - seen)
    return seen


def structure(
    sources: dict[str, ast.Module], tests: dict[str, ast.Module]
) -> tuple[dict[str, ModuleMetrics], int]:
    modules = {name.removesuffix(INIT): name for name in sources}
    graph = {name: dependencies(name, tree, modules) for name, tree in sources.items()}
    importers = Counter(target for targets in graph.values() for target in targets)
    cumulative = sum(len(reachable(graph, targets) - {name}) for name, targets in graph.items())

    # Test helpers can pass along dependencies without contributing source fan-in.
    test_modules = {name.removesuffix(INIT): f"test:{name}" for name in tests}
    combined = graph | {
        f"test:{name}": dependencies(name, tree, test_modules | modules)
        for name, tree in tests.items()
    }
    test_reach: Counter[str] = Counter()
    for root in test_modules.values():
        test_reach.update(reachable(combined, combined[root]) & sources.keys())

    metrics = {}
    for name, targets in graph.items():
        incoming, outgoing = importers[name], len(targets)
        metrics[name] = {
            "fan_in": incoming,
            "fan_out": outgoing,
            "instability": outgoing / (incoming + outgoing) if incoming + outgoing else 0,
            "test_files": test_reach[name],
        }
    return metrics, cumulative
